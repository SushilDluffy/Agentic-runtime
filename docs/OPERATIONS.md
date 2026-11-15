# Recovery and deployment contracts

## Task state is durable; external effects are separate

SQLite transactions commit a task mutation and its event together. Before invoking an action, the runtime persists an operation with immutable validated arguments, a digest, an action signature, and a stable operation ID. It then records dispatch before calling application code. A process can die between any of these steps. A dispatched operation therefore means the effect **may** have occurred, even when no result was persisted.

| Recorded outcome | Next execution |
| --- | --- |
| `prepared` | Check approval and current authorization, then dispatch using the same ID |
| `applied` | Reuse the persisted result; do not invoke the handler again |
| `dispatched` / `unknown`, read action | Retry within the attempt budget |
| `dispatched` / `unknown`, write action | Reconcile, or retry under a declared idempotency contract |
| Unresolved write | Preserve `unknown` and block the task |
| `failed` / `denied` | Feed the recorded failure to the model; no automatic replay of that operation |

The runtime does not provide exactly-once delivery across arbitrary services. A new model-selected action gets a new operation ID, so application business constraints must also prevent semantically duplicate operations where necessary.

## Write adapter contract

Pass `ctx.operation_id` to the external service as its idempotency key, or store it atomically with the effect in your domain database. `idempotent=True` is an assertion by the adapter author, not automatic deduplication. The key must be bound to the exact payload and retained for at least the entire possible retry/recovery lifetime. Document provider retention and status-query consistency. If the service's key expires, stop retries and reconcile manually.

An async reconciliation callback `(ctx, args) -> Reconciliation` returns:

- `applied`, with a result conforming to the action's output model, only after confirming the operation's effect.
- `not_applied` only when an authoritative result rules out a delayed original commit. An eventually consistent “not found” is insufficient.
- `unknown` when the outcome cannot be established. A callback failure is also treated conservatively.

Example:

```python
async def reconcile_send(ctx, args):
    record = await ctx.dependencies.delivery.lookup(ctx.operation_id)
    if record.confirmed:
        return Reconciliation(outcome="applied", result={"delivery_id": record.id})
    return Reconciliation(outcome="unknown")
```

A write without reconciliation or idempotency can complete on its first attempt, but an ambiguous failure blocks. An idempotent action can safely repeat its original request within its retry limit, including when reconciliation remains unknown. Authorization is checked again before repeating a handler. Reconciliation may inspect a previously dispatched effect even when permission to create new effects has been revoked.

Handlers must validate current permissions and state in their own transactions where those can change between authorization and effect. The support example uses version checks and an operation table in the same transaction as its write. That controlled simulation cannot prove a real delivery provider's semantics.

## Restart and cancellation

Run one supervised worker process on a single host. A per-task OS file lock excludes competing local runners; process death releases the lock. SQLite and the lock directory must be on a local filesystem with working locking, addressed by the same canonical path. Network filesystems, multiple hosts, and multi-container distributed locking are unsupported. Do not remove lock files while any runner is active.

On restart, the worker picks up running/verifying tasks and unresolved operations, while input and approval requests retain their IDs and expiry times. Runtime budgets are not reset. A crash during an invocation leaves a start marker; recovery conservatively charges the entire elapsed interval, including downtime. This can exhaust the active-time budget and require review rather than resumption.

Cancellation records intent. The runtime checks it before dispatch and before accepting success. It cannot recall an already dispatched request. An in-flight action's applied or unknown outcome stays in the result. A cancelled task is terminal: an unknown effect must be resolved through the application's external reconciliation procedure. Do not create a replacement write just because the task is cancelled.

To resolve a blocked task: inspect its operation ID, exact arguments, events, and domain/provider records; restore a trustworthy reconciliation path; then call `resume`. If code/schema semantics changed, restore the compatible agent/action definition or perform a deliberate application migration. Bump `Agent.version` for incompatible agent behavior and `Action.version` for changed handler behavior. Never rewrite approval digests to make stale work pass.

## Persistence, security, and maintenance

V1 schema version is 1. Unknown future schema versions are rejected. There is no general migration framework yet. Back up with SQLite's backup API, or stop all writers and take a consistent snapshot; do not copy only the main database while WAL writes are active. Back up domain state separately. Restore and recovery must preserve external operation IDs.

The store contains objectives, action arguments, replies, check records, and bounded outputs. Treat it as sensitive application data. Restrict file permissions and access, and use host-managed disk encryption when required. Dependency objects and credentials are not serialized by the runtime, but values a callback returns or a user submits can still leak secrets. Exception records use types rather than arbitrary messages.

The configurable redactor cleans context/tool output and public API results; it is not an encryption layer or a complete data-loss-prevention system. It does not rewrite the raw input needed to execute actions. Trusted Python store access bypasses HTTP ownership and redaction. Do not expose the store directly to users.

Large outputs are stored in SQLite as task-scoped artifacts. `SQLiteStore.prune_artifacts(before=<UTC timestamp>)` explicitly removes artifacts belonging to terminal tasks older than the cutoff. It leaves task/event metadata, including now-expired artifact references. No automatic task/event deletion, quotas, archival service, or orphan-artifact sweep is included. Set retention and storage monitoring in the host.

All handlers run with worker process permissions. Use trusted async functions and cancellation-aware SDK clients; CPU work, blocking I/O, and subprocesses need application-owned isolation and limits. `asyncio.wait_for` cannot forcibly stop a blocking or cancellation-suppressing handler. The runtime is not a sandbox.

## Host checklist

Authenticate users and validate current workspace membership; inject scoped clients; define meaningful state-based checks; configure limits and request rate controls; implement correct write recovery; bind the demo only to loopback; supervise and monitor the worker; back up both databases; review terminal tasks with unknown effects. These are application deployment responsibilities, not additional model prompts.
