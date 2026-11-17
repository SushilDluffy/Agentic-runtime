import asyncio
import json
import subprocess
import sys
import time

import pytest
from pydantic import BaseModel

from agentic_runtime import (
    Action,
    ActionDecision,
    Agent,
    AskDecision,
    CheckResult,
    Conflict,
    FinishDecision,
    Identity,
    Limits,
    PermissionDenied,
    Reconciliation,
    Runtime,
    ScriptedProvider,
    SQLiteStore,
    TaskStatus,
    TransientError,
    Worker,
)
from agentic_runtime.contracts import Operation, digest
from examples.support.domain import SupportDatabase, support_agent

IDENTITY = Identity(user_id="alice", workspace_id="acme")


class Args(BaseModel):
    value: int


class Result(BaseModel):
    value: int


async def passing(ctx, output):
    return CheckResult(name="result", passed=output == {"done": True}, detail="Expected output")


def make_runtime(tmp_path, *, handler=None, steps=None, check=passing, **action_options):
    async def default_handler(ctx, args):
        return Result(value=args.value)

    action = Action("work", Args, Result, handler or default_handler, **action_options)
    agent = Agent(
        "test",
        ScriptedProvider(
            steps
            or [
                ActionDecision(action="work", arguments={"value": 42}),
                FinishDecision(output={"done": True}),
            ]
        ),
        [action],
        [check],
    )
    return Runtime(SQLiteStore(tmp_path / "runtime.sqlite"), [agent])


def submit(runtime, limits=None):
    return runtime.submit("test", "Do the work", IDENTITY, limits)


async def test_happy_path_and_atomic_events(tmp_path):
    runtime = make_runtime(tmp_path)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.operations[0].result == {"value": 42}
    assert task.usage.model_calls == 2
    assert task.usage.tool_calls == 1
    events = runtime.store.events(task.id)
    assert len({e.id for e in events}) == len(events)
    assert events[-1].kind == "verification.finished"
    assert task.revision == len(events) - 1


async def test_restart_wait_and_duplicate_input(tmp_path):
    steps = [
        AskDecision(question="Value?", input_schema={"type": "integer"}),
        FinishDecision(output={"done": True}),
    ]
    first = make_runtime(tmp_path, steps=steps)
    task = await first.run(submit(first).id)
    restarted = make_runtime(tmp_path, steps=steps)
    await restarted.respond(task.id, task.pending.id, IDENTITY, 4)
    await restarted.respond(task.id, task.pending.id, IDENTITY, 4)
    with pytest.raises(Conflict):
        await restarted.respond(task.id, task.pending.id, IDENTITY, 5)
    done = await restarted.run(task.id)
    assert done.status == "succeeded"
    assert done.decisions == 2
    assert len(done.replies) == 1


async def test_approval_is_exact_and_duplicate_safe(tmp_path):
    runtime = make_runtime(tmp_path, writes=True, approval=True)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "waiting_approval"
    request = task.pending
    with pytest.raises(Conflict):
        await runtime.respond(task.id, request.id, IDENTITY, True, input_digest="wrong")
    await runtime.respond(task.id, request.id, IDENTITY, True, input_digest=request.input_digest)
    await runtime.respond(task.id, request.id, IDENTITY, True, input_digest=request.input_digest)
    task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert task.usage.tool_calls == 1


async def test_mutated_arguments_invalidate_approval(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime).id)
    runtime.store.update(
        task.id, lambda t: t.operations[0].arguments.update(value=99), "test.mutation"
    )
    with pytest.raises(Conflict):
        await runtime.respond(
            task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
        )
    assert runtime.store.get(task.id).usage.tool_calls == 0


async def test_denied_action_cannot_pass_domain_check(tmp_path):
    async def check(ctx, output):
        return CheckResult(name="effect", passed=False, detail="Effect required")

    runtime = make_runtime(tmp_path, approval=True, check=check)
    task = await runtime.run(submit(runtime).id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, False, input_digest=task.pending.input_digest
    )
    task = await runtime.run(task.id)
    assert task.status == "blocked"
    assert task.operations[0].state == "denied"
    assert task.usage.tool_calls == 0


async def test_authorization_is_rechecked_after_approval(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    allowed = True

    async def authorize(ctx, action, args):
        return allowed

    runtime.agents["test"].authorize = authorize
    task = await runtime.run(submit(runtime).id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    allowed = False
    task = await runtime.run(task.id)
    assert task.operations[0].state == "denied"
    assert task.usage.tool_calls == 0


async def test_reconciliation_after_lost_write_response(tmp_path):
    effects = {}

    async def handler(ctx, args):
        effects[ctx.operation_id] = {"value": args.value}
        raise TimeoutError

    async def reconcile(ctx, args):
        return Reconciliation(outcome="applied", result=effects[ctx.operation_id])

    runtime = make_runtime(tmp_path, handler=handler, writes=True, reconcile=reconcile)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert len(effects) == 1
    assert task.usage.tool_calls == 1


async def test_unknown_write_blocks_and_resume_reconciles(tmp_path):
    known = False

    async def handler(ctx, args):
        raise TimeoutError

    async def reconcile(ctx, args):
        if known:
            return Reconciliation(outcome="applied", result={"value": args.value})
        return Reconciliation(outcome="unknown")

    runtime = make_runtime(tmp_path, handler=handler, writes=True, reconcile=reconcile)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "blocked"
    assert task.operations[0].state == "unknown"
    assert runtime.result(task.id, IDENTITY).review_items
    known = True
    await runtime.resume(task.id, IDENTITY)
    task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert task.usage.tool_calls == 1


async def test_idempotent_retry_reuses_operation_id(tmp_path):
    seen = []

    async def handler(ctx, args):
        seen.append(ctx.operation_id)
        if len(seen) == 1:
            raise TimeoutError
        return Result(value=args.value)

    runtime = make_runtime(tmp_path, handler=handler, writes=True, idempotent=True)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert len(seen) == 2 and seen[0] == seen[1]


async def test_cancel_inflight_retains_applied_outcome(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(ctx, args):
        entered.set()
        await release.wait()
        return Result(value=args.value)

    runtime = make_runtime(tmp_path, handler=handler, writes=True)
    task = submit(runtime)
    running = asyncio.create_task(runtime.run(task.id))
    await entered.wait()
    runtime.cancel(task.id, IDENTITY)
    release.set()
    task = await running
    assert task.status == "cancelled"
    assert task.operations[0].state == "applied"
    assert task.usage.model_calls == 1


async def test_cancel_wait_makes_reply_stale(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime).id)
    runtime.cancel(task.id, IDENTITY)
    with pytest.raises(Conflict):
        await runtime.respond(
            task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
        )
    assert (await runtime.run(task.id)).status == "cancelled"


async def test_single_owner_under_competing_runners(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def handler(ctx, args):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return Result(value=args.value)

    runtime = make_runtime(tmp_path, handler=handler)
    competing = make_runtime(tmp_path, handler=handler)
    task = submit(runtime)
    first = asyncio.create_task(runtime.run(task.id))
    await entered.wait()
    other = await competing.run(task.id)
    assert other.status == "running"
    release.set()
    assert (await first).status == "succeeded"
    assert count == 1


async def test_budget_survives_restart(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime, Limits(model_calls=1)).id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    restarted = make_runtime(tmp_path, approval=True)
    task = await restarted.run(task.id)
    assert task.status == "failed"
    assert task.reason == "Model call budget exhausted"
    assert task.operations[0].state == "applied"
    assert task.usage.model_calls == 1


async def test_checks_repair_and_reverify(tmp_path):
    calls = 0

    async def check(ctx, output):
        nonlocal calls
        calls += 1
        return CheckResult(name="output", passed=output == 2, detail="Output must be 2")

    runtime = make_runtime(
        tmp_path, check=check, steps=[FinishDecision(output=1), FinishDecision(output=2)]
    )
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert calls == 2


async def test_read_retry_and_model_retry(tmp_path):
    attempts = 0

    async def handler(ctx, args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientError
        return Result(value=args.value)

    runtime = make_runtime(tmp_path, handler=handler)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded" and attempts == 2


async def test_artifact_paging_and_context_budget(tmp_path):
    class Large(BaseModel):
        data: str

    async def large(ctx, args):
        return Large(data="x" * 10000)

    runtime = make_runtime(tmp_path)
    agent = runtime.agents["test"]
    agent.actions = [Action("large", Args, Large, large)]
    agent.__post_init__()
    agent.provider = ScriptedProvider(
        [
            ActionDecision(action="large", arguments={"value": 1}),
            FinishDecision(output={"done": True}),
        ]
    )
    task = await runtime.run(submit(runtime, Limits(inline_chars=256, context_chars=2048)).id)
    artifact = task.operations[0].result["artifact_id"]
    assert len(json.loads(runtime.store.artifact(task.id, artifact))["data"]) == 10000
    assert task.status == "succeeded"
    task.messages.extend({"long": "x" * 1000} for _ in range(10))
    context = runtime._model_context(task, None)
    assert sum(len(json.dumps(m)) for m in context.messages) <= 2048


async def test_isolation_and_expiry(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime).id)
    with pytest.raises(PermissionDenied):
        runtime.get(task.id, Identity(user_id="alice", workspace_id="other"))
    runtime.store.update(
        task.id, lambda t: setattr(t.pending, "expires_at", time.time() - 1), "test.expire"
    )
    assert (await runtime.run(task.id)).reason == "User request expired"


async def test_transaction_rollback_and_schema_version(tmp_path):
    runtime = make_runtime(tmp_path)
    task = submit(runtime)
    before = runtime.store.events(task.id)

    def fail(current):
        current.reason = "Never committed"
        raise ValueError

    with pytest.raises(ValueError):
        runtime.store.update(task.id, fail, "bad")
    assert runtime.store.get(task.id).reason is None
    assert runtime.store.events(task.id) == before
    with runtime.store._connect() as db:
        db.execute("PRAGMA user_version=100")
    with pytest.raises(ValueError, match="Unsupported"):
        SQLiteStore(tmp_path / "runtime.sqlite")


async def test_completed_operation_is_not_replayed(tmp_path):
    calls = []

    async def handler(ctx, args):
        calls.append(ctx.operation_id)
        return Result(value=args.value)

    runtime = make_runtime(tmp_path, handler=handler)
    task = submit(runtime)
    op = Operation(
        action="work",
        arguments={"value": 42},
        input_digest=digest({"value": 42}),
        action_version="1",
        writes=False,
        state="applied",
        result={"value": 42},
    )

    def recoverable(current):
        current.operations.append(op)
        current.decisions = 1
        current.status = TaskStatus.RUNNING

    runtime.store.update(task.id, recoverable, "test.crash_boundary")
    assert (await runtime.run(task.id)).status == "succeeded"
    assert calls == []


async def test_actual_process_crash_after_external_commit(tmp_path):
    domain = SupportDatabase(tmp_path / "support.sqlite")
    domain.seed(IDENTITY)
    runtime = Runtime(SQLiteStore(tmp_path / "runtime.sqlite"), [support_agent(domain)])
    task = runtime.submit("support", "Resolve T-100", IDENTITY)
    task = await runtime.run(task.id)
    await runtime.respond(task.id, task.pending.id, IDENTITY, {"resolution": "Invoice fixed"})
    task = await runtime.run(task.id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    code = """import asyncio,sys
from pathlib import Path
from agentic_runtime import Runtime,SQLiteStore
from examples.support.domain import SupportDatabase,support_agent
p=Path(sys.argv[1])
r=Runtime(SQLiteStore(p/'runtime.sqlite'),[support_agent(SupportDatabase(p/'support.sqlite',fault='crash'))])
asyncio.run(r.run(sys.argv[2]))
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(tmp_path), task.id],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 91, process.stderr
    assert domain.count_sends() == 1
    task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert domain.count_sends() == 1
    assert task.operations[-1].state == "applied"


async def test_worker_processes_multiple_tasks(tmp_path):
    runtime = make_runtime(tmp_path)
    ids = [submit(runtime).id for _ in range(5)]
    await Worker(runtime, concurrency=2).run_once()
    assert all(runtime.store.get(t).status == "succeeded" for t in ids)


async def test_changed_action_contract_blocks_existing_approval(tmp_path):
    from dataclasses import replace

    runtime = make_runtime(tmp_path, approval=True, writes=True)
    task = await runtime.run(submit(runtime).id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    agent = runtime.agents["test"]
    agent.actions = [replace(agent.actions[0], idempotent=True)]
    agent.__post_init__()
    task = await runtime.run(task.id)
    assert task.status == "blocked"
    assert task.usage.tool_calls == 0


async def test_unknown_outcome_cancellation_is_explicit(tmp_path):
    async def handler(ctx, args):
        raise TimeoutError

    runtime = make_runtime(tmp_path, handler=handler, writes=True)
    task = await runtime.run(submit(runtime).id)
    assert task.status == "blocked"
    cancelled = runtime.cancel(task.id, IDENTITY)
    assert cancelled.status == "cancelled"
    assert runtime.result(task.id, IDENTITY).operations[0].state == "unknown"
    assert runtime.result(task.id, IDENTITY).review_items


async def test_tool_timeout_is_bounded_and_read_retries_exhaust(tmp_path):
    async def handler(ctx, args):
        await asyncio.sleep(10)
        return Result(value=1)

    runtime = make_runtime(tmp_path, handler=handler, retries=1)
    task = await runtime.run(submit(runtime, Limits(call_timeout=0.01, backoff_seconds=0)).id)
    assert task.operations[0].state == "failed"
    assert task.operations[0].attempts == 2
    assert task.usage.tool_calls == 2


async def test_missing_required_check_rejected_at_construction(tmp_path):
    with pytest.raises(ValueError, match="completion check"):
        Agent("invalid", ScriptedProvider([]), [], [])


async def test_input_schema_cannot_fetch_external_reference(tmp_path):
    runtime = make_runtime(
        tmp_path,
        steps=[
            AskDecision(question="Input?", input_schema={"$ref": "https://example.invalid/schema"}),
            FinishDecision(output={"done": True}),
        ],
    )
    task = await runtime.run(submit(runtime).id)
    assert task.pending is None
    assert task.messages[0]["error"] == "Invalid input request schema"


async def test_failing_check_cannot_claim_success(tmp_path):
    async def check(ctx, output):
        raise RuntimeError("private credential must not be persisted")

    runtime = make_runtime(tmp_path, check=check)
    task = await runtime.run(submit(runtime).id)
    assert task.status != "succeeded"
    assert "private credential" not in task.model_dump_json()
    assert task.checks[0].passed is False


async def test_unstarted_operation_survives_worker_interruption(tmp_path):
    runtime = make_runtime(tmp_path)
    task = submit(runtime)
    op = Operation(
        action="work",
        arguments={"value": 42},
        input_digest=digest({"value": 42}),
        action_version=runtime.agents["test"].action("work").signature(),
        writes=False,
    )

    def prepared(current):
        current.operations.append(op)
        current.decisions = 1
        current.status = TaskStatus.RUNNING

    runtime.store.update(task.id, prepared, "test.prepared")
    task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert task.operations[0].id == op.id
    assert task.operations[0].attempts == 1


async def test_idempotent_duplicate_reply_has_no_extra_event(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime).id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    before = runtime.store.events(task.id)
    await runtime.respond(
        task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
    )
    assert runtime.store.events(task.id) == before


async def test_all_support_failure_variants(tmp_path):
    for fault, expected in [
        ("none", "succeeded"),
        ("lost_response", "succeeded"),
        ("unknown", "blocked"),
    ]:
        directory = tmp_path / fault
        directory.mkdir()
        domain = SupportDatabase(directory / "domain.sqlite", fault=fault)
        domain.seed(IDENTITY)
        runtime = Runtime(SQLiteStore(directory / "runtime.sqlite"), [support_agent(domain)])
        task = runtime.submit("support", "Resolve ticket T-100", IDENTITY)
        task = await runtime.run(task.id)
        await runtime.respond(task.id, task.pending.id, IDENTITY, {"resolution": "Link fixed"})
        task = await runtime.run(task.id)
        await runtime.respond(
            task.id, task.pending.id, IDENTITY, True, input_digest=task.pending.input_digest
        )
        task = await runtime.run(task.id)
        assert task.status == expected
        assert domain.count_sends() == 1


async def test_budget_before_idempotent_retry_preserves_unknown_effect(tmp_path):
    async def handler(ctx, args):
        raise TimeoutError

    runtime = make_runtime(tmp_path, handler=handler, writes=True, idempotent=True)
    task = await runtime.run(submit(runtime, Limits(tool_calls=1, backoff_seconds=0)).id)
    assert task.status == "failed"
    assert task.operations[0].state == "unknown"
    assert task.operations[0].attempts == 1
    assert runtime.result(task.id, IDENTITY).review_items


async def test_cancel_during_idempotent_retry_preserves_unknown_effect(tmp_path, monkeypatch):
    async def handler(ctx, args):
        raise TimeoutError

    runtime = make_runtime(tmp_path, handler=handler, writes=True, idempotent=True)
    task = submit(runtime)

    async def cancel_at_backoff(seconds):
        runtime.cancel(task.id, IDENTITY)

    monkeypatch.setattr("agentic_runtime.runtime.asyncio.sleep", cancel_at_backoff)
    task = await runtime.run(task.id)
    assert task.status == "cancelled"
    assert task.operations[0].state == "unknown"
    assert task.usage.tool_calls == 1


async def test_revoked_retry_permission_preserves_unknown_effect(tmp_path):
    async def handler(ctx, args):
        raise TimeoutError

    runtime = make_runtime(tmp_path, handler=handler, writes=True, idempotent=True)
    calls = 0

    async def authorize(ctx, action, args):
        nonlocal calls
        calls += 1
        return calls == 1

    runtime.agents["test"].authorize = authorize
    task = await runtime.run(submit(runtime, Limits(backoff_seconds=0)).id)
    assert task.status == "blocked"
    assert task.operations[0].state == "unknown"
    assert task.usage.tool_calls == 1


async def test_task_lock_excludes_another_process(tmp_path):
    runtime = make_runtime(tmp_path)
    task = submit(runtime)
    code = """import sys
from agentic_runtime import SQLiteStore
from filelock import Timeout
lock=SQLiteStore(sys.argv[1]).lock(sys.argv[2])
try:
    lock.acquire(timeout=0)
except Timeout:
    sys.exit(23)
else:
    lock.release()
    sys.exit(0)
"""
    lock = runtime.store.lock(task.id)
    with lock:
        process = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code, str(runtime.store.path), task.id],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert process.returncode == 23, process.stderr
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(runtime.store.path), task.id],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr


async def test_large_proposal_is_resolved_for_checks_after_restart(tmp_path):
    from agentic_runtime import ModelReply

    output = {"content": "x" * 6000}

    async def check(ctx, proposed):
        return CheckResult(name="full_output", passed=proposed == output, detail="Full content")

    runtime = make_runtime(tmp_path, check=check)
    task = submit(runtime)
    await runtime._accept(task.id, ModelReply(decision=FinishDecision(output=output)))
    stored = runtime.store.get(task.id)
    assert stored.status == "verifying" and stored.proposal_artifact_id
    restarted = make_runtime(tmp_path, check=check)
    completed = await restarted.run(task.id)
    assert completed.status == "succeeded"
    assert completed.proposal["artifact_id"] == stored.proposal_artifact_id


async def test_user_output_resembling_artifact_is_not_dereferenced(tmp_path):
    output = {"artifact_id": "not-an-engine-reference", "preview": "sample", "characters": 6}

    async def check(ctx, proposed):
        return CheckResult(name="literal_output", passed=proposed == output, detail="Literal JSON")

    runtime = make_runtime(tmp_path, check=check, steps=[FinishDecision(output=output)])
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded" and task.proposal_artifact_id is None
