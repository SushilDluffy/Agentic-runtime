# Application API

The supported import surface is `agentic_runtime`. Optional HTTP integration lives in `agentic_runtime.integrations.fastapi`. Core imports do not require FastAPI or the OpenAI SDK.

## Define an agent

`Agent(name, provider, actions, checks, ...)` requires at least one application completion check. Each check is an async function `(ActionContext, proposed_output) -> CheckResult`. Inspect domain records belonging to the current task where possible; a plausible model answer is not evidence of an external effect. Check names, details, evidence, and observation timestamps are recorded. A failed check gives the model bounded repair feedback.

Optional callbacks:

| Callback | Signature and purpose |
| --- | --- |
| `dependencies` | Synchronous `(Identity) -> clients`; inject scoped, trusted application clients |
| `context` | Async `(ActionContext) -> JSON`; load current context before each model decision |
| `authorize` | Async `(ActionContext, Action, validated_args) -> bool`; check current permission before every dispatch, including after approval |
| `can_respond` | Async `(Identity, Task) -> bool`; additionally veto a reply after user permissions change |
| `redact` | Synchronous `(JSON) -> JSON`; clean tool output, context, model history, and HTTP responses |

The model's historical context can be stale. Actions must enforce their own current-state preconditions and conditional writes. The context callback is not a substitute for a transaction or a version check at dispatch.

`Action(name, input_type, output_type, handler, ...)` uses Pydantic models and an async handler `(ActionContext, input_model) -> output_model`. Execution fields are `writes`, `approval`, `idempotent`, `reconcile`, `retries` (additional attempts, default 2), and `version`. Action names beginning `runtime_` are reserved. `approval=True` asks the initiating user to approve exact validated arguments. Changing argument content or the action schema/recovery flags invalidates an existing operation. Bump `version` when implementation semantics change; function bodies are not automatically fingerprinted.

Use `ActionRejected` only when an action definitively produced no effect. Use `TransientError` for retryable read failures. A write exception is treated as uncertain regardless of exception type, except for `ActionRejected`. See the [recovery contract](OPERATIONS.md).

## Task lifecycle

```python
identity = Identity(user_id=authenticated_user.id, workspace_id=authorized_workspace.id)
task = runtime.submit("support", "Resolve T-100", identity, limits=Limits(model_calls=20))
task = await runtime.run(task.id)
```

`run` is a trusted worker method without a caller identity parameter. It returns at a wait, block, or terminal state. Never expose it directly to untrusted clients. `get`, `result`, `cancel`, `respond`, and `resume` check the initiating user **and** workspace. V1 does not support a different approver or shared task ownership.

```python
# Structured input: value must match task.pending.schema_.
await runtime.respond(task.id, task.pending.id, identity, {"resolution": "Link refreshed"})

# Approval: bind the response to the digest in the pending request.
await runtime.respond(
    task.id, task.pending.id, identity, True,
    input_digest=task.pending.input_digest,
)
# False rejects that operation. The agent may propose an alternative.
```

Responses queue the task; run it again or let a worker pick it up. Repeating the same request and payload is idempotent, including after completion. A different duplicate, expired request, or unanswered stale request raises `Conflict`. Only blocked tasks can be resumed with `await runtime.resume(id, identity)`. Resume preserves budgets and operation IDs. Failed/cancelled tasks are terminal.

`runtime.cancel(id, identity)` synchronously records cancellation. `runtime.result(id, identity)` returns current output, checks, operations, usage, and review items; it is available before completion and its status must be checked. A cancelled task may still have applied or unknown writes.

## Worker and limits

```python
stop = asyncio.Event()
await Worker(runtime, concurrency=4, poll_seconds=0.25).serve(stop)
```

Use a supervisor to restart the process. Worker scans queued, running, verifying, and waiting tasks; waiting tasks expire without a model call. `run_once()` supports tests or host schedulers. Tasks execute actions serially; concurrency is between tasks.

`Limits` sets `model_calls`, `tool_calls`, `call_timeout`, `active_seconds`, `wait_seconds`, `context_chars`, `inline_chars`, `artifact_bytes`, `model_retries`, and `backoff_seconds`. The call counters and measured active time persist. Model retries count as model calls; action attempts count as tool calls. Context, authorization, reconciliation, and check callbacks consume time but not tool-call counters. Human waiting has its own deadline. Active time covers invoked callbacks, not all storage or backoff time. A crash during an invocation conservatively charges elapsed downtime on recovery.

`context_chars` bounds retained message history, not the whole provider prompt: objective, instructions, tool schemas, and application context are additional. Large outputs become `{artifact_id, preview, characters}`. A model can request bounded pages using `RetrieveDecision`. `runtime.store.artifact(task_id, artifact_id)` retrieves task-scoped JSON text for trusted application code.

## HTTP integration

Mount `create_router(runtime, identity_dependency, prefix="/tasks", limits=Limits(...))` in a FastAPI application. The dependency must construct a trusted `Identity` from the host's authentication and current membership. Do not accept identity or limits from the task request body. Run a Worker alongside the server or in a supervised process on the same host.

| Method | Endpoint | Body / response |
| --- | --- | --- |
| POST | `/tasks` | `{agent, objective}` → task, HTTP 201 |
| GET | `/tasks/{id}` | Current task including pending request |
| GET | `/tasks/{id}/result` | Structured result and review items |
| POST | `/tasks/{id}/requests/{request_id}` | `{value, input_digest?}` → updated task |
| POST | `/tasks/{id}/cancel` | Record cancellation |
| POST | `/tasks/{id}/resume` | Queue a blocked task |
| GET | `/tasks/{id}/artifacts/{artifact_id}` | Owned artifact JSON |
| GET | `/tasks/{id}/events` | SSE progress stream |

Other owners receive 404. Conflicting/stale replies receive 409, invalid input 422, and a reply rejected by `can_respond` receives 403. Host authentication failures come from the dependency. Add the host's normal CSRF and authorization controls for cookie-authenticated endpoints.

SSE uses `event: progress`, JSON event records, and durable monotonic `id` values. Reconnect with `Last-Event-ID` or `?after=<id>`; header takes precedence. IDs are global, so a task's sequence may contain gaps. After draining events at a wait, block, or terminal state, the stream sends `event: idle` and closes. Close the browser EventSource on idle, fetch current task/result, and reopen after a reply. The reference UI demonstrates this. Identity is resolved at connection establishment; hosts needing immediate membership revocation should limit stream lifetime or add their own revalidation.

## Providers

`ScriptedProvider` selects decisions by the persisted decision index, making restarts deterministic. It returns a blocker when its script is exhausted. `OpenAIProvider` uses the official async SDK, one tool call per decision, and disables SDK retries so the runtime owns the retry budget. It records usage reported by successful responses; calls whose responses are lost can incur unreported provider usage. No dollar estimate or billing reconciliation is implemented.

A custom provider implements `async decide(ModelContext) -> ModelReply`. Decisions are `ActionDecision`, `AskDecision`, `FinishDecision`, `BlockDecision`, and `RetrieveDecision`. Application tools, context, and tool results are untrusted model data; authorization and checks belong in application callbacks, not prompts.


### Claude / Anthropic

Install the `anthropic` extra and use `AnthropicProvider("claude-sonnet-5-5")`. It reads `ANTHROPIC_API_KEY` through the official async SDK. `max_output_tokens` defaults to 1024; SDK retries are disabled by the default client. A supplied `client` must manage its own retry policy. The adapter uses native Messages API tools with serial automatic tool selection. By default it leaves thinking configuration to the model; pass `thinking={"type": "between_tools"}` for Sonnet 5.5 when up-front thinking is unnecessary. This model rejects the older `disabled` setting. It rejects missing, multiple, malformed, or truncated tool decisions; the runtime's bounded model retry policy applies.

Each decision is a fresh request built from the durable application history snapshot, not a continuation of a native assistant/tool-result conversation. The adapter does not persist or replay model thinking blocks.

`provider.request(ModelContext)` exposes the exact request for host-side token counting and budget admission. The live example wraps the SDK Messages interface to count tokens, reserve budget, and record usage even when a response cannot become a valid decision. Core `Task.usage` counts usage from accepted provider replies, so malformed/lost responses can make that counter lower than billed usage. Cache reads/writes, if enabled through a custom client, count toward aggregate input tokens; the core counter alone does not distinguish their prices.

The optional runner uses synthetic fixtures and fixed price configuration. Its reservations survive errors within that invocation, but do not survive restarting the runner or cover other processes using the same key. Keep a margin and treat Anthropic billing as authoritative.
