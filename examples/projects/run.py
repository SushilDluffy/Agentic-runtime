import asyncio
import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from agentic_runtime import (
    Action,
    ActionDecision,
    Agent,
    CheckResult,
    FinishDecision,
    Identity,
    Runtime,
    ScriptedProvider,
    SQLiteStore,
)


class WorkItem(BaseModel):
    title: str


class CreatedItem(BaseModel):
    id: str
    title: str


async def main(directory=".demo-projects"):
    directory = Path(directory)
    directory.mkdir(exist_ok=True, parents=True)
    application_db = directory / "projects.sqlite"
    with sqlite3.connect(application_db) as db:
        db.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, task TEXT, title TEXT)")

    async def create_item(ctx, args):
        with sqlite3.connect(application_db) as db:
            db.execute(
                "INSERT OR IGNORE INTO items VALUES (?,?,?)",
                (ctx.operation_id, ctx.task_id, args.title),
            )
            row = db.execute("SELECT title FROM items WHERE id=?", (ctx.operation_id,)).fetchone()
        return CreatedItem(id=ctx.operation_id, title=row[0])

    async def verify(ctx, output):
        with sqlite3.connect(application_db) as db:
            titles = {
                row[0] for row in db.execute("SELECT title FROM items WHERE task=?", (ctx.task_id,))
            }
        return CheckResult(
            name="release_items",
            passed=titles == {"Test release", "Write changelog"},
            detail="Both release work items exist",
            evidence=sorted(titles),
        )

    provider = ScriptedProvider(
        [
            ActionDecision(action="create_item", arguments={"title": "Test release"}),
            ActionDecision(action="create_item", arguments={"title": "Write changelog"}),
            FinishDecision(output={"summary": "Release work items created"}),
        ]
    )
    agent = Agent(
        "projects",
        provider,
        [Action("create_item", WorkItem, CreatedItem, create_item, writes=True, idempotent=True)],
        [verify],
    )
    runtime = Runtime(SQLiteStore(directory / "runtime.sqlite"), [agent])
    identity = Identity(user_id="demo-user", workspace_id="demo-project")
    task = runtime.submit("projects", "Prepare the release work items", identity)
    await runtime.run(task.id)
    print(json.dumps(runtime.result(task.id, identity).model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
