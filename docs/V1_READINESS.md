# V1 completion audit

November 18, 2026 · implementation version **0.1.1** · experimental, single-host Python runtime.

The eight V1 implementation components are complete within the deployment boundaries below. The two gaps found in the internal developer trial, per-attempt usage accounting and a typed final-output contract, are closed. This is a source/package implementation milestone. Independent product validation and public package publication remain separate gates.

## Scope and evidence

| V1 component | Implemented behavior | Evidence |
| --- | --- | --- |
| Agent execution | Serial decisions/actions, persisted call budgets and active-time accounting, bounded retries, cancellation | `tests/test_runtime.py`, model SDK tests |
| Typed actions | Pydantic input/output models, durable operation IDs, exact argument digests, attempt counts and timestamped transition events | Action validation, approval-change and retry tests |
| Application context | Scoped dependencies, user/workspace ownership, authorization before every dispatch, bounded history and task-owned artifacts | Ownership, permission revocation, artifact isolation tests |
| Persistent lifecycle | SQLite state/events, local process locks, wait/restart/recovery, format-1 upgrade to format 2 | Atomicity, competing process, real process-crash and upgrade tests |
| Human interaction | Durable structured questions, expiring requests, exact-action approval, idempotent replies, rejection | Runtime and HTTP lifecycle tests |
| Recovery | Bounded read retries, write reconciliation/idempotency contracts, explicit unknown outcomes | Real crash after a simulated external commit; lost-response, cancellation and unresolved-effect scenarios |
| Results and evidence | Optional typed final outputs, bounded schema repair, mandatory application checks, full artifact checks, per-model-attempt usage and optional cost estimates | `tests/test_output_contracts.py`, `tests/test_model_accounting.py` |
| Application API | Public Python API, polling worker, optional FastAPI routes, reconnectable SSE and support UI | API ownership/event replay tests; visual UI validation still pending |

## Closed in v0.1.1

- Every runtime model dispatch creates a durable record before invocation. Reported tokens from rejected or truncated responses count toward totals. Interrupted/lost responses remain explicitly unknown. Settlement is idempotent and independent of decision acceptance.
- Host-configured decimal token prices produce estimates with per-call coverage. Cache buckets are preserved and not double-counted. Missing rates or usage do not become a claimed complete zero-cost result.
- `Agent.output_type` supplies the final tool schema to both model adapters and validates proposals in the runtime. Invalid proposals receive bounded repair feedback; business checks remain mandatory. Changed contracts block old tasks until compatibility is restored or deliberately migrated.
- The GitHub triage integration uses the output contract directly. Existing support and project examples remain compatible.
- Store format 2 loads old records with additive defaults and retains their evidence. It does not invent historical usage records. Stop old workers and back up before opening an existing database with the new runtime; old runtime versions cannot open the upgraded format.

## Validation performed

- **93 tests pass** on local Python 3.12, including 24 new accounting, output-contract and upgrade cases.
- Ruff lint and formatting pass.
- **18/18 scripted comparison runs** meet expectations; [raw evidence](../benchmarks/v1-completion-2026-11-18.json). This does not establish superiority over LangGraph.
- Source and wheel distributions build successfully. A clean wheel installation runs the documented quickstart and the fixture-based GitHub triage workflow.
- CI repeats lint, tests, scripted comparison and distribution builds on Python 3.11 and 3.12.
- No additional paid model calls or real GitHub issue writes were made for this completion pass. Earlier Claude smoke/trial reports remain historical evidence, not new measurements of v0.1.1.

## Remaining release validation

| Gate | Current status | Why it remains |
| --- | --- | --- |
| Independent developer exercise | Pending | The existing trial was author-led; it cannot measure outside-user usability |
| Held-out live-model evaluation and comparable baseline | Pending | Existing live smoke runs and scripted comparisons do not establish general reliability or differentiation |
| Real external write failure testing | Pending | Controlled simulated services do not prove every provider's idempotency and delayed-status behavior |
| Visual support-UI review | Pending | The available browser could not reach the development server; API behavior is tested |
| PyPI publication | Not performed | Distribution artifacts and source installation are available; no public registry release claimed |

## V1 boundary and V2 planning

V1 uses local SQLite, one host, trusted async application tools and host-supplied authentication, permissions and domain state. It does not offer multi-host execution, a code sandbox, automatic semantic correctness, universal exactly-once effects, an account-wide spending cap, or billing reconciliation. Model/tool call and time limits are enforced; dollar estimates are accounting evidence, not spend admission controls.

The implementation can be frozen here for outside-user trials. Choose V2 priorities from concrete friction in those trials. Candidates already identified are a reusable durable spend-admission guard and a hosted-application recipe for persisting task scope and access configuration. Multi-host storage, richer context management and delegated agents need an application requirement before entering the scope. This audit does not pre-approve a V2 feature list.
