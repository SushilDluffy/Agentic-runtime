"""Durable task execution with explicit external-operation outcomes."""

import asyncio
import json
import time
from collections.abc import Callable

import jsonschema
from filelock import Timeout as LockTimeout
from pydantic import ValidationError
from referencing import Registry

from .actions import ActionContext, ActionRejected, Agent, TransientError
from .contracts import (
    TERMINAL,
    WAITING,
    ActionDecision,
    AskDecision,
    BlockDecision,
    CheckResult,
    FinishDecision,
    Identity,
    Limits,
    ModelReply,
    Operation,
    PendingRequest,
    Reconciliation,
    RetrieveDecision,
    Task,
    TaskResult,
    TaskStatus,
    digest,
)
from .providers import ModelContext
from .store import Conflict, NotFound, Store


class BudgetExceeded(Exception):
    pass


class PermissionDenied(Exception):
    pass


class Runtime:
    def __init__(self, store: Store, agents: list[Agent]):
        self.store = store
        self.agents = {agent.name: agent for agent in agents}
        if len(self.agents) != len(agents):
            raise ValueError("Agent names must be unique")

    def submit(
        self, agent: str, objective: str, identity: Identity, limits: Limits | None = None
    ) -> Task:
        definition = self.agents[agent]
        task = Task(
            agent=agent,
            agent_version=definition.version,
            objective=objective,
            identity=identity,
            limits=limits or Limits(),
        )
        return self.store.create(task)

    def get(self, task_id: str, identity: Identity) -> Task:
        task = self.store.get(task_id)
        if task.identity != identity:
            raise PermissionDenied("Task belongs to another user or workspace")
        return task

    def result(self, task_id: str, identity: Identity) -> TaskResult:
        task = self.get(task_id, identity)
        review = [
            op.error or "External outcome unknown"
            for op in task.operations
            if op.state in ("unknown", "dispatched")
        ]
        if task.reason:
            review.append(task.reason)
        review.extend(check.detail for check in task.checks if not check.passed)
        return TaskResult(
            task_id=task.id,
            status=task.status,
            output=task.proposal,
            checks=task.checks,
            operations=task.operations,
            usage=task.usage,
            review_items=review,
        )

    def cancel(self, task_id: str, identity: Identity) -> Task:
        task = self.get(task_id, identity)
        if task.status in TERMINAL:
            return task

        def change(current):
            if current.status in TERMINAL:
                return
            current.cancel_requested = True
            if current.status in WAITING or current.status in ("queued", "blocked"):
                current.status = TaskStatus.CANCELLED
                current.pending = None
            current.reason = "Cancellation requested"

        return self.store.update(task_id, change, "task.cancellation_requested")

    async def respond(
        self,
        task_id: str,
        request_id: str,
        identity: Identity,
        value,
        *,
        input_digest: str | None = None,
    ) -> Task:
        task = self.get(task_id, identity)
        agent = self.agents[task.agent]
        if agent.can_respond and not await agent.can_respond(identity, task):
            raise PermissionDenied("Actor may no longer respond to this task")
        if len(json.dumps(value, allow_nan=False).encode()) > task.limits.artifact_bytes:
            raise ValueError("Reply exceeds the configured byte limit")
        payload = {"value": value, "input_digest": input_digest}

        def change(current):
            if request_id in current.replies:
                if current.replies[request_id] != payload:
                    raise Conflict("Request was already answered differently")
                return
            pending = current.pending
            if (
                current.status not in WAITING
                or not pending
                or pending.id != request_id
                or current.cancel_requested
            ):
                raise Conflict("Request is stale or task is inactive")
            if pending.expires_at <= time.time():
                raise Conflict("Request expired")
            if pending.kind == "approval":
                if type(value) is not bool:
                    raise ValueError("Approval must be a boolean")
                op = self._operation(current, pending.operation_id)
                if (
                    input_digest != pending.input_digest
                    or input_digest != op.input_digest
                    or digest(op.arguments) != input_digest
                ):
                    raise Conflict("Approval does not match the exact action arguments")
                if value:
                    op.approved_digest = input_digest
                else:
                    op.state = "denied"
                    op.error = "User rejected the action"
                    current.messages.append(
                        {"action": op.action, "operation_id": op.id, "outcome": "denied"}
                    )
            else:
                jsonschema.Draft202012Validator(pending.schema_, registry=Registry()).validate(
                    value
                )
                current.messages.append({"request_id": request_id, "user_input": value})
            current.replies[request_id] = payload
            current.pending = None
            current.status = TaskStatus.QUEUED

        return self.store.update(task_id, change, "request.answered", {"request_id": request_id})

    async def resume(self, task_id: str, identity: Identity) -> Task:
        task = self.get(task_id, identity)
        if task.status != TaskStatus.BLOCKED:
            raise Conflict("Only blocked tasks can be resumed")

        def change(current):
            if current.status != TaskStatus.BLOCKED or current.cancel_requested:
                raise Conflict("Task changed before resume")
            current.status = TaskStatus.QUEUED
            current.reason = None

        return self.store.update(task_id, change, "task.resumed")

    def _context(self, task, operation_id=""):
        agent = self.agents[task.agent]
        return ActionContext(
            task.id, task.identity, operation_id, agent.dependencies(task.identity)
        )

    @staticmethod
    def _operation(task, operation_id):
        return next(op for op in task.operations if op.id == operation_id)

    def _status(self, task_id, status, reason=None):
        def change(task):
            task.status = status
            task.reason = reason
            if status in TERMINAL:
                task.pending = None

        return self.store.update(task_id, change, "task." + status.value)

    def _message(self, task_id, message):
        return self.store.update(
            task_id, lambda task: task.messages.append(message), "task.feedback"
        )

    def _op_update(self, task_id, operation_id, **values):
        def change(task):
            op = self._operation(task, operation_id)
            for key, value in values.items():
                setattr(op, key, value)

        return self.store.update(
            task_id,
            change,
            "operation.updated",
            {"operation_id": operation_id, "state": values.get("state")},
        )

    def _finish_step(self, task_id):
        def change(task):
            if task.step_started_at is not None:
                task.usage.active_seconds += max(0, time.time() - task.step_started_at)
                task.step_started_at = None

        self.store.update(task_id, change, "step.finished")

    async def _invoke(self, task_id, function: Callable, kind="callback"):
        def begin(task):
            if task.usage.active_seconds >= task.limits.active_seconds:
                raise BudgetExceeded("Active execution budget exhausted")
            if kind == "model":
                if task.usage.model_calls >= task.limits.model_calls:
                    raise BudgetExceeded("Model call budget exhausted")
                task.usage.model_calls += 1
            if kind == "tool":
                if task.usage.tool_calls >= task.limits.tool_calls:
                    raise BudgetExceeded("Tool call budget exhausted")
                task.usage.tool_calls += 1
            task.step_started_at = time.time()

        task = self.store.update(task_id, begin, "step.started", {"kind": kind})
        timeout = min(
            task.limits.call_timeout, task.limits.active_seconds - task.usage.active_seconds
        )
        try:
            return await asyncio.wait_for(function(), timeout=timeout)
        finally:
            self._finish_step(task_id)

    def _compact_value(self, task, value):
        return self._prepare_value(task, value)[0]

    def _prepare_value(self, task, value):
        """Return persisted value and a trusted artifact ID when compaction actually occurred."""
        agent = self.agents[task.agent]
        value = agent.redact(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > task.limits.artifact_bytes:
            raise ValueError("Output exceeds artifact byte limit")
        if len(encoded) <= task.limits.inline_chars:
            return value, None
        artifact_id = self.store.put_artifact(task.id, value)
        reference = {
            "artifact_id": artifact_id,
            "preview": encoded[:256],
            "characters": len(encoded),
        }
        return reference, artifact_id

    def _model_context(self, task, application_context):
        agent = self.agents[task.agent]
        # Each message is independently complete. Pending operations live outside this history.
        used = 0
        messages = []
        for message in reversed(task.messages):
            clean = agent.redact(message)
            size = len(json.dumps(clean))
            if used + size > task.limits.context_chars:
                continue
            used += size
            messages.append(clean)
        return ModelContext(
            task.objective,
            agent.instructions,
            application_context,
            list(reversed(messages)),
            [a.schema() for a in agent.actions],
            task.decisions,
        )

    async def run(self, task_id: str) -> Task:
        """Trusted worker entry point. User-facing access goes through identity-checked methods."""
        lock = self.store.lock(task_id)
        try:
            lock.acquire(timeout=0)
        except LockTimeout:
            return self.store.get(task_id)
        try:
            task = self.store.get(task_id)
            if task.status in TERMINAL or task.status == TaskStatus.BLOCKED:
                return task
            if task.status in WAITING:
                if task.pending and task.pending.expires_at <= time.time():
                    return self._status(task_id, TaskStatus.FAILED, "User request expired")
                return task
            if task.agent not in self.agents:
                return self._status(task_id, TaskStatus.BLOCKED, "Agent definition unavailable")
            if task.agent_version != self.agents[task.agent].version:
                return self._status(task_id, TaskStatus.BLOCKED, "Agent version changed")
            if task.step_started_at:
                self._finish_step(task_id)
            if task.status != TaskStatus.VERIFYING:
                self._status(task_id, TaskStatus.RUNNING)
            try:
                await self._loop(task_id)
            except BudgetExceeded as exc:
                self._status(task_id, TaskStatus.FAILED, str(exc))
            except asyncio.CancelledError:
                # Persisted dispatched intent is reconciled by the next owner.
                raise
            except Exception as exc:
                # Exception messages may contain credentials or provider response bodies.
                self._status(task_id, TaskStatus.FAILED, f"Execution error ({type(exc).__name__})")
            return self.store.get(task_id)
        finally:
            lock.release()

    async def _loop(self, task_id):
        while True:
            task = self.store.get(task_id)
            if task.status in TERMINAL or task.status in WAITING or task.status == "blocked":
                return
            pending = next(
                (op for op in task.operations if op.state in ("prepared", "dispatched", "unknown")),
                None,
            )
            if pending:
                await self._execute(task, pending)
                continue
            if task.cancel_requested:
                self._status(task_id, TaskStatus.CANCELLED, "Task cancelled")
                return
            if task.status == TaskStatus.VERIFYING:
                await self._verify(task)
                continue
            agent = self.agents[task.agent]
            context = None
            if agent.context:
                context = await self._invoke(
                    task_id, lambda agent=agent, task=task: agent.context(self._context(task))
                )
                context = self._compact_value(task, context)
            reply = await self._model(task_id, context)
            # Acceptance of a decision and creation of its durable intent are atomic.
            await self._accept(task_id, reply)

    async def _model(self, task_id, context):
        task = self.store.get(task_id)
        agent = self.agents[task.agent]
        for attempt in range(task.limits.model_retries + 1):
            try:
                reply = await self._invoke(
                    task_id,
                    lambda: agent.provider.decide(
                        self._model_context(self.store.get(task_id), context)
                    ),
                    "model",
                )
                return ModelReply.model_validate(reply)
            except (TransientError, TimeoutError):
                if attempt == task.limits.model_retries:
                    raise
                await asyncio.sleep(task.limits.backoff_seconds * 2**attempt)
        raise AssertionError("Unreachable retry loop")

    @staticmethod
    def _validate_request_schema(schema, limit):
        if len(json.dumps(schema)) > limit:
            raise ValueError("Input schema exceeds size limit")
        pending = [schema]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "$ref" in value and not value["$ref"].startswith("#"):
                    raise ValueError("Input schemas must use local references")
                if "$dynamicRef" in value and not value["$dynamicRef"].startswith("#"):
                    raise ValueError("Input schemas must use local references")
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)

    async def _accept(self, task_id, reply):
        task = self.store.get(task_id)
        decision = reply.decision
        agent = self.agents[task.agent]
        op = None
        message = None
        if isinstance(decision, ActionDecision):
            try:
                action = agent.action(decision.action)
                args = action.input_type.model_validate(decision.arguments).model_dump(mode="json")
                if len(json.dumps(args).encode()) > task.limits.artifact_bytes:
                    raise ValueError("Arguments exceed byte limit")
                op = Operation(
                    action=action.name,
                    arguments=args,
                    input_digest=digest(args),
                    writes=action.writes,
                    action_version=action.signature(),
                )
            except (KeyError, ValidationError, ValueError):
                message = {
                    "error": "Unknown action or invalid arguments",
                    "action": decision.action,
                }
        elif isinstance(decision, AskDecision):
            try:
                jsonschema.Draft202012Validator.check_schema(decision.input_schema)
                self._validate_request_schema(decision.input_schema, task.limits.inline_chars)
            except (jsonschema.SchemaError, ValueError):
                message = {"error": "Invalid input request schema"}
        elif isinstance(decision, RetrieveDecision):
            try:
                text = self.store.artifact(task_id, decision.artifact_id)
                page = text[decision.offset : decision.offset + task.limits.inline_chars // 2]
                message = {
                    "artifact_id": decision.artifact_id,
                    "offset": decision.offset,
                    "page": page,
                    "next_offset": decision.offset + len(page),
                    "done": decision.offset + len(page) >= len(text),
                }
            except NotFound:
                message = {"error": "Artifact unavailable for this task"}

        proposal = None
        proposal_artifact_id = None
        if isinstance(decision, FinishDecision):
            proposal, proposal_artifact_id = self._prepare_value(task, decision.output)

        def change(current):
            current.usage.input_tokens += reply.input_tokens
            current.usage.output_tokens += reply.output_tokens
            current.decisions += 1
            if current.cancel_requested:
                current.status = TaskStatus.CANCELLED
                return
            if message is not None:
                current.messages.append(message)
            elif op:
                current.operations.append(op)
            elif isinstance(decision, AskDecision):
                current.pending = PendingRequest(
                    kind="input",
                    prompt=decision.question,
                    schema=decision.input_schema,
                    expires_at=time.time() + current.limits.wait_seconds,
                )
                current.status = TaskStatus.WAITING_INPUT
            elif isinstance(decision, FinishDecision):
                current.proposal = proposal
                current.proposal_artifact_id = proposal_artifact_id
                current.status = TaskStatus.VERIFYING
            elif isinstance(decision, BlockDecision):
                current.reason = decision.reason
                current.status = TaskStatus.BLOCKED

        self.store.update(task_id, change, "model.decision", {"kind": decision.kind})

    async def _execute(self, task, op):
        agent = self.agents[task.agent]
        try:
            action = agent.action(op.action)
        except KeyError:
            self._status(task.id, TaskStatus.BLOCKED, "Action definition unavailable")
            return
        if op.action_version != action.signature() or digest(op.arguments) != op.input_digest:
            self._status(task.id, TaskStatus.BLOCKED, "Action definition or payload changed")
            return
        ctx = self._context(task, op.id)
        args = action.input_type.model_validate(op.arguments)
        if op.state in ("dispatched", "unknown"):
            ready = await self._recover(task, op, action, ctx, args)
            if not ready:
                return
            task = self.store.get(task.id)
            op = self._operation(task, op.id)
        if task.cancel_requested:
            if op.state in ("unknown", "dispatched"):
                self._op_update(
                    task.id, op.id, state="unknown", error="Cancelled with an unresolved effect"
                )
            else:
                self._op_update(task.id, op.id, state="denied", error="Cancelled before dispatch")
            self._status(task.id, TaskStatus.CANCELLED, "Task cancelled")
            return
        if action.approval and op.approved_digest != op.input_digest:

            def request(current):
                if current.cancel_requested:
                    return
                current.pending = PendingRequest(
                    kind="approval",
                    prompt=f"Approve {op.action}",
                    operation_id=op.id,
                    input_digest=op.input_digest,
                    expires_at=time.time() + current.limits.wait_seconds,
                )
                current.status = TaskStatus.WAITING_APPROVAL

            self.store.update(task.id, request, "approval.requested", {"operation_id": op.id})
            return
        if agent.authorize:
            allowed = await self._invoke(task.id, lambda: agent.authorize(ctx, action, args))
            if not allowed:
                if op.state in ("unknown", "dispatched"):
                    self._op_update(
                        task.id, op.id, state="unknown", error="Retry denied; prior effect unknown"
                    )
                    self._status(task.id, TaskStatus.BLOCKED, "Retry permission denied")
                    return
                self._op_update(task.id, op.id, state="denied", error="Application denied action")
                self._message(task.id, {"action": op.action, "outcome": "denied"})
                return
        if op.attempts >= action.retries + 1:
            self._op_update(task.id, op.id, state="failed", error="Action retry limit exhausted")
            self._message(
                task.id, {"action": op.action, "outcome": "failed", "error": "Retry limit"}
            )
            return

        def dispatch(current):
            if current.cancel_requested:
                return
            operation = self._operation(current, op.id)
            operation.state = "dispatched"
            operation.attempts += 1

        latest = self.store.update(task.id, dispatch, "operation.dispatch", {"operation_id": op.id})
        if latest.cancel_requested:
            return
        try:
            result = await self._invoke(task.id, lambda: action.handler(ctx, args), "tool")
            result = action.output_type.model_validate(result).model_dump(mode="json")
            result = self._compact_value(task, result)
            self._applied(task.id, op.id, result)
        except ActionRejected:
            self._op_update(task.id, op.id, state="failed", error="Application rejected operation")
            self._message(task.id, {"action": op.action, "outcome": "rejected"})
        except BudgetExceeded:
            prior_state = "unknown" if op.state in ("dispatched", "unknown") else "prepared"
            self._op_update(task.id, op.id, state=prior_state, attempts=op.attempts)
            raise
        except asyncio.CancelledError:
            self._op_update(
                task.id, op.id, state="unknown", error="Worker interrupted during dispatch"
            )
            raise
        except Exception as exc:
            if action.writes:
                self._op_update(
                    task.id,
                    op.id,
                    state="unknown",
                    error=f"External outcome uncertain ({type(exc).__name__})",
                )
            elif isinstance(exc, (TransientError, TimeoutError)):
                self._op_update(task.id, op.id, state="prepared", error="Transient read failure")
                await asyncio.sleep(task.limits.backoff_seconds * 2**op.attempts)
            else:
                self._op_update(task.id, op.id, state="failed", error=type(exc).__name__)
                self._message(
                    task.id, {"action": op.action, "outcome": "failed", "error": type(exc).__name__}
                )

    def _applied(self, task_id, operation_id, result):
        def change(task):
            op = self._operation(task, operation_id)
            op.state = "applied"
            op.result = result
            op.error = None
            task.messages.append(
                {"action": op.action, "operation_id": op.id, "outcome": "applied", "result": result}
            )

        self.store.update(task_id, change, "operation.applied", {"operation_id": operation_id})

    async def _recover(self, task, op, action, ctx, args):
        if not action.writes:
            self._op_update(task.id, op.id, state="prepared")
            return True
        if action.reconcile:
            try:
                outcome = await self._invoke(task.id, lambda: action.reconcile(ctx, args))
                outcome = Reconciliation.model_validate(outcome)
                if outcome.outcome == "applied":
                    result = action.output_type.model_validate(outcome.result).model_dump(
                        mode="json"
                    )
                    self._applied(task.id, op.id, self._compact_value(task, result))
                    return False
                if outcome.outcome == "not_applied":
                    self._op_update(task.id, op.id, state="prepared")
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if action.idempotent and not task.cancel_requested and op.attempts <= action.retries:
            # Safe to repeat does not establish that the original effect did not occur.
            # Keep uncertainty durable if cancellation/crash/budget stops the retry.
            await asyncio.sleep(task.limits.backoff_seconds * 2**op.attempts)
            return True
        self._op_update(
            task.id, op.id, state="unknown", error="External outcome requires reconciliation"
        )
        status = TaskStatus.CANCELLED if task.cancel_requested else TaskStatus.BLOCKED
        self._status(task.id, status, "External outcome remains unknown")
        return False

    async def _verify(self, task):
        agent = self.agents[task.agent]
        # Checks validate the full output, while public task state keeps the compact reference.
        # Only a runtime-created persisted ID permits resolution; user-shaped JSON is not a ref.
        output = (
            json.loads(self.store.artifact(task.id, task.proposal_artifact_id))
            if task.proposal_artifact_id is not None
            else task.proposal
        )
        checks = []
        for check in agent.checks:
            if self.store.get(task.id).cancel_requested:
                self._status(task.id, TaskStatus.CANCELLED, "Cancelled during verification")
                return
            try:
                result = await self._invoke(
                    task.id, lambda check=check: check(self._context(task), output)
                )
                result = CheckResult.model_validate(result)
                result.evidence = self._compact_value(task, result.evidence)
            except (BudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as exc:
                result = CheckResult(
                    name=getattr(check, "__name__", "check"),
                    passed=False,
                    detail=f"Check execution failed ({type(exc).__name__})",
                )
            checks.append(result)

        def finish(current):
            current.checks = checks
            if current.cancel_requested:
                current.status = TaskStatus.CANCELLED
            elif checks and all(check.passed for check in checks):
                current.status = TaskStatus.SUCCEEDED
                current.reason = None
            else:
                current.status = TaskStatus.RUNNING
                current.messages.append({"checks": [c.model_dump(mode="json") for c in checks]})

        self.store.update(task.id, finish, "verification.finished")
