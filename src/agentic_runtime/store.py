"""SQLite transactions atomically persist task mutations and their events."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from filelock import FileLock

from .contracts import Event, JsonValue, Task, new_id


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Store(Protocol):
    def create(self, task: Task) -> Task: ...
    def get(self, task_id: str) -> Task: ...
    def update(
        self, task_id: str, change: Callable[[Task], None], kind: str, data: JsonValue = None
    ) -> Task: ...
    def list_tasks(self, statuses: list[str] | None = None) -> list[Task]: ...
    def events(self, task_id: str, after: int = 0, limit: int = 100) -> list[Event]: ...
    def lock(self, task_id: str) -> FileLock: ...
    def put_artifact(self, task_id: str, value: JsonValue) -> str: ...
    def artifact(self, task_id: str, artifact_id: str) -> str: ...


class SQLiteStore:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed database for persistent tasks")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / (self.path.name + ".locks")
        self.lock_dir.mkdir(exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported store schema version {version}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _event(self, db, task_id, kind, data):
        db.execute(
            "INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)",
            (task_id, kind, json.dumps(data, allow_nan=False), time.time()),
        )

    def create(self, task: Task) -> Task:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?)",
                (task.id, task.status, task.model_dump_json(by_alias=True)),
            )
            self._event(db, task.id, "task.created", {"status": task.status})
        return task

    def get(self, task_id: str) -> Task:
        with self._connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(task_id)
        return Task.model_validate_json(row[0])

    def update(self, task_id, change, kind, data=None) -> Task:
        with self._transaction() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound(task_id)
            task = Task.model_validate_json(row[0])
            before = task.model_dump_json(by_alias=True)
            change(task)
            if task.model_dump_json(by_alias=True) == before:
                return task
            task.revision += 1
            task.updated_at = time.time()
            # Validate nested mutations before committing.
            task = Task.model_validate_json(task.model_dump_json(by_alias=True))
            db.execute(
                "UPDATE tasks SET status=?, data=? WHERE id=?",
                (task.status, task.model_dump_json(by_alias=True), task_id),
            )
            self._event(db, task_id, kind, data if data is not None else {"status": task.status})
        return task

    def list_tasks(self, statuses=None):
        with self._connect() as db:
            if statuses:
                slots = ",".join("?" for _ in statuses)
                rows = db.execute(f"SELECT data FROM tasks WHERE status IN ({slots})", statuses)
            else:
                rows = db.execute("SELECT data FROM tasks")
            return [Task.model_validate_json(row[0]) for row in rows]

    def events(self, task_id, after=0, limit=100):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,task_id,kind,data,created_at FROM events "
                "WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                (task_id, after, min(max(limit, 1), 1000)),
            )
            return [
                Event(id=r[0], task_id=r[1], kind=r[2], data=json.loads(r[3]), created_at=r[4])
                for r in rows
            ]

    def lock(self, task_id):
        # Hash arbitrary caller IDs so lock paths cannot escape the directory.
        from hashlib import sha256

        return FileLock(self.lock_dir / (sha256(task_id.encode()).hexdigest() + ".lock"))

    def put_artifact(self, task_id, value):
        artifact_id = new_id()
        with self._connect() as db:
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?)",
                (artifact_id, task_id, json.dumps(value, allow_nan=False)),
            )
        return artifact_id

    def artifact(self, task_id, artifact_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM artifacts WHERE id=? AND task_id=?", (artifact_id, task_id)
            ).fetchone()
        if row is None:
            raise NotFound(artifact_id)
        return row[0]

    def prune_artifacts(self, before: float) -> int:
        """Explicit maintenance for terminal tasks older than a UTC timestamp."""
        with self._transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM tasks WHERE status IN ('succeeded','failed','cancelled') "
                    "AND json_extract(data, '$.updated_at') < ?",
                    (before,),
                )
            ]
            count = 0
            for task_id in ids:
                count += db.execute("DELETE FROM artifacts WHERE task_id=?", (task_id,)).rowcount
            return count
