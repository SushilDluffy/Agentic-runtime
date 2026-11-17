"""Run fixture triage, or read public GitHub issues with an optional live model."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from decimal import Decimal
from getpass import getpass
from pathlib import Path
from types import SimpleNamespace

import httpx

from agentic_runtime import AnthropicProvider, Identity, Limits, Runtime, SQLiteStore

from .app import Access, triage_agent
from .domain import GitHub
from .fixtures import FakeGitHub, scripted


async def main(args):
    if args.approve_fixtures and args.repo:
        raise ValueError("Automatic approvals are limited to synthetic fixtures")
    if args.apply_labels and args.repo and not os.environ.get("GITHUB_TOKEN"):
        raise ValueError("Set GITHUB_TOKEN for explicit approved label writes")
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    fixture = FakeGitHub(fault=args.fault) if not args.repo else None
    repository = args.repo or "example/triage"
    async with AsyncExitStack() as stack:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if args.repo and os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=headers,
                timeout=20,
                transport=httpx.MockTransport(fixture.handle) if fixture else None,
            )
        )
        github = GitHub(repository, http)
        numbers = tuple(
            args.issues or (sorted(fixture.issues) if fixture else await github.open_numbers())
        )
        snapshot = await github.snapshot(numbers)
        guard = None
        if args.model and numbers:
            from anthropic import AsyncAnthropic

            # Evaluation-only helper; the application itself imports only the public SDK.
            from examples.support.live import GuardedMessages

            key = os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API key (hidden): ")
            client = await stack.enter_async_context(AsyncAnthropic(api_key=key, max_retries=0))
            key = None
            guard = GuardedMessages(client, args.model, budget=Decimal("0.25"), max_calls=16)
            provider = AnthropicProvider(
                args.model,
                client=SimpleNamespace(messages=guard),
                thinking={"type": "between_tools"},
            )
        elif fixture or not numbers:
            provider = scripted(snapshot, apply=args.apply_labels)
        else:
            raise ValueError(
                "Select --model for real issue triage; fixture scripts aren't classifiers"
            )
        identity = Identity(user_id="local-developer", workspace_id=repository)
        access = Access(identity, github, numbers, allow_writes=args.apply_labels)
        runtime = Runtime(
            SQLiteStore(directory / "runtime.sqlite"), [triage_agent(access, provider)]
        )
        task = runtime.submit(
            "github_triage",
            "Review the selected issues, suggest labels, "
            "identify missing information, and prepare a triage summary.",
            identity,
            Limits(model_calls=8, call_timeout=60, active_seconds=240),
        )
        task = await runtime.run(task.id)
        for _ in range(12):
            if not task.pending:
                break
            pending = task.pending
            if pending.kind != "approval":
                break  # Leave unexpected questions for explicit human input.
            op = next(op for op in task.operations if op.id == pending.operation_id)
            print(json.dumps({"approval_required": op.arguments}, indent=2), flush=True)
            approve = bool(fixture and args.approve_fixtures)
            if not approve:
                approve = input("Approve this exact label addition? [y/N] ").lower() == "y"
            await runtime.respond(
                task.id, pending.id, identity, approve, input_digest=pending.input_digest
            )
            task = await runtime.run(task.id)
            if not approve:
                break
        result = runtime.result(task.id, identity).model_dump(mode="json")
        report = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "repository": repository,
            "python_version": platform.python_version(),
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("agentic-runtime", "httpx", "pydantic")
            },
            "source": "synthetic fixtures" if fixture else "live GitHub REST reads",
            "model": args.model if guard else "scripted lifecycle decisions",
            "selected_issues": list(numbers),
            "writes_enabled": args.apply_labels,
            "result": result,
            "estimated_usd": round(sum(r.get("estimated_usd", 0) for r in guard.records), 8)
            if guard
            else 0,
            "api_calls": guard.records if guard else [],
            "reserved_usd": float(guard.reserved) if guard else 0,
            "preflight_errors": guard.preflight_errors if guard else [],
            "review": "Human review of classification quality is still required",
        }
        if fixture:
            report["simulated_writes"] = fixture.writes
            report["http_requests"] = fixture.requests
        destination = Path(args.output) if args.output else directory / "report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": task.status,
                    "source": report["source"],
                    "selected_issues": list(numbers),
                    "estimated_usd": report["estimated_usd"],
                    "report": str(destination),
                }
            ),
            flush=True,
        )
        return 0 if task.status == "succeeded" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/repository; omit for synthetic fixtures")
    parser.add_argument("--issues", nargs="+", type=int)
    parser.add_argument("--model", choices=["claude-sonnet-5-5"])
    parser.add_argument("--directory", default=".demo-triage")
    parser.add_argument("--output")
    parser.add_argument("--apply-labels", action="store_true")
    parser.add_argument("--approve-fixtures", action="store_true")
    parser.add_argument("--fault", choices=["none", "lost_response", "unknown"], default="none")
    args = parser.parse_args()
    if args.repo and args.fault != "none":
        parser.error("Fault injection is only supported for synthetic fixtures")
    try:
        raise SystemExit(asyncio.run(main(args)))
    except (ValueError, httpx.HTTPError) as exc:
        # Do not print SDK/client objects or request headers containing credentials.
        raise SystemExit(
            f"Triage setup failed ({type(exc).__name__}); check configuration"
        ) from None
