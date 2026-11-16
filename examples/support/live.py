"""Small opt-in Claude evaluation using synthetic data and conservative budget admission."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from agentic_runtime import AnthropicProvider, Identity, Limits, Runtime, SQLiteStore

from .domain import SupportDatabase, support_agent

# Standard global, uncached USD per million tokens, checked 2026-11-16.
# https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {"claude-sonnet-5-5": ("2", "10"), "claude-haiku-5-5": ("0.10", "0.50")}
SCENARIOS = ("normal", "reject", "restart_input", "lost_response", "unknown", "cancel_wait")
RESOLUTION = "Your invoice download link has been refreshed. Reference INV-204."


class SpendLimit(Exception):
    """No inference is dispatched when the next reservation exceeds the batch budget."""


class GuardedMessages:
    """Sequential, process-local guard. Reservations are never refunded after dispatch.

    Count tokens first, add input headroom, reserve all possible output, disable SDK
    retries, and retain reservations for failed or lost responses. This is not an
    account-wide billing cap, and the provider's invoice remains authoritative.
    """

    def __init__(self, client, model, budget=Decimal("1"), max_calls=36):
        self.client = client.with_options(max_retries=0)
        self.model = model
        self.budget = Decimal(budget)
        self.max_calls = max_calls
        self.reserved = Decimal("0")
        self.records = []
        self.preflight_errors = []
        self.input_rate, self.output_rate = map(Decimal, PRICES[model])

    async def create(self, **request):
        if request["model"] != self.model:
            raise SpendLimit("Model differs from the configured price table")
        if len(self.records) >= self.max_calls:
            raise SpendLimit("Batch model-call limit reached")
        try:
            counted = await self.client.messages.count_tokens(
                **{k: v for k, v in request.items() if k != "max_tokens"}
            )
        except Exception as exc:
            import re

            body = getattr(exc, "body", None)
            message = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
            key = getattr(self.client, "api_key", None)
            if key:
                message = message.replace(key, "[redacted]")
            message = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[redacted]", message)
            self.preflight_errors.append(
                {
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                    "message": message[:400],
                }
            )
            raise
        # Stay below the Haiku long-context pricing boundary, even with headroom.
        reserved_input = int(counted.input_tokens * 1.25) + 512
        if reserved_input > 8000 or request["max_tokens"] > 1024:
            raise SpendLimit("Per-request token limit exceeded")
        reservation = (
            reserved_input * self.input_rate + request["max_tokens"] * self.output_rate
        ) / Decimal(1_000_000)
        if self.reserved + reservation > self.budget:
            raise SpendLimit("Batch dollar reservation limit reached")
        self.reserved += reservation
        record = {
            "counted_input_tokens": counted.input_tokens,
            "reserved_input_tokens": reserved_input,
            "max_output_tokens": request["max_tokens"],
            "reserved_usd": float(reservation),
        }
        self.records.append(record)
        started = time.monotonic()
        try:
            response = await self.client.messages.create(**request)
        except BaseException as exc:
            record.update(
                error_type=type(exc).__name__, http_status=getattr(exc, "status_code", None)
            )
            raise
        else:
            usage = response.usage
            input_tokens = usage.input_tokens
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            # This runner does not request prompt caching. Reject unexpected billing semantics.
            if cache_write or cache_read:
                record["error_type"] = "UnexpectedCacheUsage"
                self.budget = self.reserved
                raise SpendLimit("Unexpected cached usage requires price review")
            cost = (
                input_tokens * self.input_rate + usage.output_tokens * self.output_rate
            ) / Decimal(1_000_000)
            record.update(
                input_tokens=input_tokens,
                output_tokens=usage.output_tokens,
                estimated_usd=float(cost),
                stop_reason=response.stop_reason,
            )
            if input_tokens > reserved_input or usage.output_tokens > request["max_tokens"]:
                self.budget = self.reserved
                raise SpendLimit("Provider usage exceeded the reservation; stop for review")
            return response
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)


async def scenario(directory, name, provider):
    identity = Identity(user_id="live-test", workspace_id="synthetic")
    fault = name if name in ("lost_response", "unknown") else "none"
    database = SupportDatabase(directory / "domain.sqlite", fault=fault)
    database.seed(identity)
    agent = support_agent(database, provider=provider)
    agent.instructions += (
        " For the input form, ask for an object with one required string field named resolution."
        " Preserve the resolution's reference code in the note and outgoing response."
        " If sending is rejected, do not send or declare success; report the blocker."
    )
    runtime = Runtime(SQLiteStore(directory / "runtime.sqlite"), [agent])
    task = runtime.submit(
        "support",
        "Resolve ticket T-100: ask me for the resolution, record it, and request "
        "approval before sending. Verify the note and delivery.",
        identity,
        Limits(model_calls=8, tool_calls=8, model_retries=1, call_timeout=45, active_seconds=180),
    )
    restarted = False
    input_answered = False
    rejected = False
    cancelled = False
    intervention_kinds = []
    task = await runtime.run(task.id)
    for _ in range(4):
        pending = task.pending
        if not pending:
            break
        if name == "cancel_wait" and pending.kind == "approval":
            task = runtime.cancel(task.id, identity)
            cancelled = True
            intervention_kinds.append("cancel")
            break
        if pending.kind == "input":
            if input_answered:
                break  # Don't guess answers to unanticipated follow-up questions.
            if name == "restart_input":
                runtime = Runtime(SQLiteStore(directory / "runtime.sqlite"), [agent])
                recovered = runtime.get(task.id, identity)
                assert recovered.pending.id == pending.id
                restarted = True
            await runtime.respond(task.id, pending.id, identity, {"resolution": RESOLUTION})
            input_answered = True
            intervention_kinds.append("input")
        else:
            approve = name != "reject"
            await runtime.respond(
                task.id, pending.id, identity, approve, input_digest=pending.input_digest
            )
            rejected = not approve
            intervention_kinds.append("approve" if approve else "reject")
        task = await runtime.run(task.id)
        if rejected:
            break
    sends = database.count_sends()
    with database.connect() as db:
        rows = db.execute("SELECT payload FROM effects WHERE action='send'").fetchall()
        outgoing = [json.loads(row["payload"])["body"] for row in rows]
        notes = db.execute("SELECT note FROM tickets").fetchall()
    content_ok = bool(outgoing) and all("INV-204" in body for body in outgoing)
    content_ok = content_ok and any("INV-204" in row["note"] for row in notes)
    if name == "reject":
        passed = rejected and sends == 0 and task.status != "succeeded"
        passed = passed and any(op.state == "denied" for op in task.operations)
    elif name == "cancel_wait":
        passed = cancelled and task.status == "cancelled" and sends == 0
    elif name == "unknown":
        passed = task.status == "blocked" and sends == 1
        passed = passed and any(op.state == "unknown" for op in task.operations)
    else:
        passed = task.status == "succeeded" and sends == 1 and content_ok
        passed = passed and (name != "restart_input" or restarted)
    return {
        "scenario": name,
        "passed": passed,
        "status": task.status,
        "reason": task.reason,
        "sends": sends,
        "reference_preserved": content_ok,
        "restarted": restarted,
        "interventions": intervention_kinds,
        "usage": task.usage.model_dump(),
        "operations": [
            {"action": op.action, "state": op.state, "attempts": op.attempts}
            for op in task.operations
        ],
        "checks": [{"name": c.name, "passed": c.passed} for c in task.checks],
    }


async def main(args):
    from anthropic import AsyncAnthropic

    # A terminal prompt avoids putting credentials into command arguments/history/files.
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        from getpass import getpass

        key = getpass("Anthropic API key (hidden): ")
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "mode": "live",
        "python_version": platform.python_version(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("agentic-runtime", "anthropic", "httpx", "pydantic")
        },
        "model": args.model,
        "budget_usd": float(args.budget),
        "scenarios": [],
        "limitations": [
            "Synthetic tickets and simulated delivery; no real customer messages.",
            "One run per selected scenario; not a statistical reliability evaluation.",
            "Restart reopens persisted state in-process; process death is tested separately.",
            "Cost is estimated from reported usage; no account balance/billing API was queried.",
            "Reservations are process-local and not an account-wide spending cap.",
        ],
    }
    async with AsyncAnthropic(api_key=key, max_retries=0, timeout=45) as client:
        key = None
        guard = GuardedMessages(client, args.model, args.budget)
        thinking = {"type": "between_tools"} if args.model == "claude-sonnet-5-5" else None
        provider = AnthropicProvider(
            args.model, client=SimpleNamespace(messages=guard), thinking=thinking
        )
        with tempfile.TemporaryDirectory() as root:
            for name in args.scenarios:
                directory = Path(root) / name
                directory.mkdir()
                first = len(guard.records)
                try:
                    result = await scenario(directory, name, provider)
                except Exception as exc:
                    result = {"scenario": name, "passed": False, "error_type": type(exc).__name__}
                result["api_calls"] = guard.records[first:]
                report["scenarios"].append(result)
                report["reserved_usd"] = float(guard.reserved)
                report["preflight_errors"] = guard.preflight_errors
                report["estimated_usd"] = round(
                    sum(row.get("estimated_usd", 0) for row in guard.records), 8
                )
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "scenario": name,
                            "passed": result["passed"],
                            "status": result.get("status"),
                            "estimated_batch_usd": report["estimated_usd"],
                        }
                    ),
                    flush=True,
                )
                if not result["passed"]:
                    break  # Inspect the first unexpected failure before spending more.
    return (
        0
        if len(report["scenarios"]) == len(args.scenarios)
        and all(row["passed"] for row in report["scenarios"])
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=PRICES, default="claude-sonnet-5-5")
    parser.add_argument("--budget", type=Decimal, default=Decimal("1"))
    parser.add_argument("--output", default=".demo-live/results.json")
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+", default=list(SCENARIOS))
    args = parser.parse_args()
    if not args.budget.is_finite() or not Decimal("0") < args.budget <= Decimal("1"):
        parser.error("This quick-test runner accepts a budget greater than zero and at most $1")
    raise SystemExit(asyncio.run(main(args)))
