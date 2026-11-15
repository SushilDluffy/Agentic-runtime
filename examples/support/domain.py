"""Application database and delivery service, separate from runtime persistence."""

import asyncio
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentic_runtime import (
    Action,
    ActionDecision,
    ActionRejected,
    Agent,
    AskDecision,
    CheckResult,
    FinishDecision,
    Reconciliation,
    ScriptedProvider,
    TransientError,
)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str


class TicketOutput(BaseModel):
    ticket_id: str
    subject: str
    version: int
    note: str


class NoteInput(TicketInput):
    note: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=0)


class SendInput(TicketInput):
    body: str = Field(min_length=1, max_length=4000)


class SendOutput(BaseModel):
    delivery_id: str
    ticket_id: str
    sent: bool


class SupportDatabase:
    def __init__(self, path: str | Path, *, fault="none"):
        self.path = str(path)
        self.fault = fault
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tickets (
                    workspace TEXT, user_id TEXT, id TEXT, subject TEXT,
                    version INTEGER DEFAULT 0, note TEXT DEFAULT '',
                    PRIMARY KEY(workspace,user_id,id)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    operation_id TEXT PRIMARY KEY, workspace TEXT, user_id TEXT, task_id TEXT,
                    action TEXT, payload TEXT, result TEXT
                );
                CREATE TABLE IF NOT EXISTS permissions (
                    workspace TEXT, user_id TEXT, allowed INTEGER,
                    PRIMARY KEY(workspace,user_id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed(self, identity, ticket_id="T-100", subject="Customer cannot download their invoice"):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tickets(workspace,user_id,id,subject) VALUES (?,?,?,?)",
                (identity.workspace_id, identity.user_id, ticket_id, subject),
            )
            db.execute(
                "INSERT OR IGNORE INTO permissions VALUES (?,?,1)",
                (identity.workspace_id, identity.user_id),
            )

    def allowed(self, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT allowed FROM permissions WHERE workspace=? AND user_id=?",
                (identity.workspace_id, identity.user_id),
            ).fetchone()
        return bool(row and row[0])

    def ticket(self, ctx, ticket_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tickets WHERE workspace=? AND user_id=? AND id=?",
                (ctx.identity.workspace_id, ctx.identity.user_id, ticket_id),
            ).fetchone()
        if row is None:
            raise ActionRejected("Ticket unavailable")
        return TicketOutput(
            ticket_id=row["id"], subject=row["subject"], version=row["version"], note=row["note"]
        )

    def effect(self, ctx, action, payload):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM effects WHERE operation_id=?", (ctx.operation_id,)
            ).fetchone()
        if row is None:
            return None
        if (
            row["workspace"] != ctx.identity.workspace_id
            or row["user_id"] != ctx.identity.user_id
            or row["action"] != action
            or json.loads(row["payload"]) != payload
        ):
            raise ActionRejected("Idempotency key does not match the operation")
        return json.loads(row["result"])

    async def read_ticket(self, ctx, args):
        return self.ticket(ctx, args.ticket_id)

    async def add_note(self, ctx, args):
        prior = self.effect(ctx, "note", args.model_dump())
        if prior:
            return TicketOutput.model_validate(prior)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE tickets SET note=?,version=version+1 "
                "WHERE workspace=? AND user_id=? AND id=? AND version=?",
                (
                    args.note,
                    ctx.identity.workspace_id,
                    ctx.identity.user_id,
                    args.ticket_id,
                    args.expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise ActionRejected("Ticket version changed")
            row = db.execute(
                "SELECT * FROM tickets WHERE workspace=? AND user_id=? AND id=?",
                (ctx.identity.workspace_id, ctx.identity.user_id, args.ticket_id),
            ).fetchone()
            result = TicketOutput(
                ticket_id=row["id"],
                subject=row["subject"],
                version=row["version"],
                note=row["note"],
            )
            self._record(db, ctx, "note", args, result)
        return result

    def _record(self, db, ctx, action, args, result):
        db.execute(
            "INSERT INTO effects VALUES (?,?,?,?,?,?,?)",
            (
                ctx.operation_id,
                ctx.identity.workspace_id,
                ctx.identity.user_id,
                ctx.task_id,
                action,
                args.model_dump_json(),
                result.model_dump_json(),
            ),
        )

    async def send_response(self, ctx, args):
        prior = self.effect(ctx, "send", args.model_dump())
        if prior:
            return SendOutput.model_validate(prior)
        self.ticket(ctx, args.ticket_id)
        if self.fault == "slow":
            await asyncio.sleep(2)
        result = SendOutput(delivery_id=ctx.operation_id, ticket_id=args.ticket_id, sent=True)
        with self.connect() as db:
            self._record(db, ctx, "send", args, result)
        if self.fault in ("lost_response", "unknown"):
            raise TransientError("Delivery committed but its response was lost")
        if self.fault == "crash":
            os._exit(91)  # Test service deliberately simulates process death after commit.
        return result

    async def reconcile_send(self, ctx, args):
        if self.fault == "unknown":
            return Reconciliation(outcome="unknown", detail="Delivery lookup unavailable")
        prior = self.effect(ctx, "send", args.model_dump())
        if prior:
            return Reconciliation(outcome="applied", result=prior)
        # This local service commits synchronously and has no deferred server jobs.
        return Reconciliation(outcome="not_applied")

    def count_sends(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM effects WHERE action='send'").fetchone()[0]


def support_agent(database: SupportDatabase, *, provider=None, ticket_id="T-100"):
    async def context(ctx):
        ticket = database.ticket(ctx, ticket_id)
        return {"ticket": ticket.model_dump(), "workflow": "Read, add internal note, draft, send"}

    async def authorize(ctx, action, args):
        return database.allowed(ctx.identity)

    async def can_respond(identity, task):
        return database.allowed(identity)

    async def completion(ctx, output):
        ticket = database.ticket(ctx, ticket_id)
        with database.connect() as db:
            rows = db.execute(
                "SELECT payload,result FROM effects WHERE workspace=? AND user_id=? "
                "AND action='send' AND task_id=?",
                (ctx.identity.workspace_id, ctx.identity.user_id, ctx.task_id),
            ).fetchall()
        deliveries = [
            json.loads(r["result"])
            for r in rows
            if json.loads(r["payload"])["ticket_id"] == ticket_id
        ]
        return CheckResult(
            name="ticket_and_delivery",
            passed=bool(ticket.note and deliveries),
            detail="Internal note and recorded delivery are required",
            evidence={"ticket": ticket.model_dump(), "deliveries": deliveries},
        )

    def script(ctx):
        ticket = ctx.application_context["ticket"]
        resolution = "A fresh invoice download link is now available in your account."
        for message in ctx.messages:
            if "user_input" in message:
                resolution = message["user_input"]["resolution"]
        steps = [
            ActionDecision(action="read_ticket", arguments={"ticket_id": ticket_id}),
            AskDecision(
                question="What resolution should we send to the customer?",
                input_schema={
                    "type": "object",
                    "properties": {
                        "resolution": {"type": "string", "minLength": 1, "maxLength": 1000}
                    },
                    "required": ["resolution"],
                    "additionalProperties": False,
                },
            ),
            ActionDecision(
                action="add_note",
                arguments={
                    "ticket_id": ticket_id,
                    "note": resolution,
                    "expected_version": ticket["version"],
                },
            ),
            ActionDecision(
                action="send_response", arguments={"ticket_id": ticket_id, "body": resolution}
            ),
            FinishDecision(
                output={"summary": "Ticket investigated and response sent", "ticket_id": ticket_id}
            ),
        ]
        if ctx.decision_index >= len(steps):
            from agentic_runtime import BlockDecision

            return BlockDecision(reason="Review the failed checks or rejected action")
        return steps[ctx.decision_index]

    actions = [
        Action(
            "read_ticket",
            TicketInput,
            TicketOutput,
            database.read_ticket,
            description="Read a ticket and its current version",
        ),
        Action(
            "add_note",
            NoteInput,
            TicketOutput,
            database.add_note,
            writes=True,
            idempotent=True,
            description="Add the resolution note using the current ticket version",
        ),
        Action(
            "send_response",
            SendInput,
            SendOutput,
            database.send_response,
            writes=True,
            approval=True,
            reconcile=database.reconcile_send,
            description="Send the drafted resolution to the ticket's customer",
        ),
    ]
    return Agent(
        "support",
        provider or ScriptedProvider(script),
        actions,
        [completion],
        instructions="Investigate the supplied ticket, ask for the resolution, add an "
        "internal note, then send a response. Sending requires user approval.",
        context=context,
        authorize=authorize,
        can_respond=can_respond,
    )
