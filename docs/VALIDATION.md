# v0.1.0 validation

Measured locally on November 15, 2026, using Python 3.12. The checked-in `uv.lock` records the resolved environment. This report distinguishes tested implementation behavior from outstanding product validation.

## Automated coverage

The local suite passes **51 tests**. It covers atomic task/event updates, restart during a user wait, duplicate input and approvals, changed arguments and action semantics, revoked authorization, lost responses, idempotent retry keys, unknown-outcome blocking, recovery after resume, cancellation during an action, competing runners (including process-level lock exclusion), preservation of unknown effects when a retry is cancelled or denied, budget persistence, failed verification, bounded read timeouts, artifact isolation, input-schema restrictions, HTTP ownership, server-owned limits, event replay, and the official OpenAI SDK request/response boundary.

One test runs a real subprocess that exits immediately after the external simulation commits a send, before the runtime persists its result. Restart reconciles the original operation and verifies exactly one recorded send. Other failure tests use controlled async handlers and explicit persisted boundary states. They are not a substitute for testing the actual external systems an application will use.

The support and project-management CLI examples run through the public API. The support UI and optional router are implemented; HTTP behavior is tested through ASGI. Browser visual verification remains pending because the available browser could not connect to the development server. No visual verification is claimed.

The OpenAI adapter is tested using the official SDK with mocked HTTP transport, including payload formatting, token accounting, rate-limit retries, and malformed tool-call responses. No live OpenAI calls were made because no API key was available. On November 16, 2026, a separate [Claude live smoke test](CLAUDE_TEST.md) passed all six synthetic scenarios with Sonnet 5.5. This adds integration evidence, not a statistical reliability or competitive benchmark.

## LangGraph comparison

Run:

```bash
python -m benchmarks.run --repeat 3 --output benchmarks/results.json
```

The checked-in report contains 18 scripted runs: two engines, three failure scenarios, and three ticket/resolution variations. Both use the same application actions, scripted decisions, and completion checks. The LangGraph implementation uses `StateGraph`, SQLite checkpointing, separate prepare/approval/effect nodes, `interrupt`, and `Command(resume=...)`; it includes explicit reconciliation instead of blindly replaying an external write.

| Scenario | Agentic Runtime | LangGraph baseline | Recorded sends per run |
| --- | --- | --- | --- |
| Normal completion | 3/3 succeeded | 3/3 succeeded | 1 |
| Write committed, response lost | 3/3 succeeded | 3/3 succeeded | 1 |
| External outcome remains unknown | 3/3 blocked as expected | 3/3 blocked as expected | 1 |

All 18 runs met their scenario expectations, with no false success in the unresolved-outcome scenario. Inspect [raw results](../benchmarks/results.json) for versions, calls, interventions, latency, and usage fields. Scripted token counts are zero and must not be interpreted as live-model cost.

This is a **behavioral comparison of a small shared workflow**. It does not measure implementation effort, developer usability, production throughput, or complete feature parity. The baseline's budgets and recovery behavior cover the compared scenarios; it does not implement all runtime limits, cancellation, artifact handling, HTTP integration, or permission lifecycle features. Local timing includes storage/scheduler noise. The three variations are fixtures, not a blinded held-out intelligence evaluation. There is no evidence here that this framework outperforms LangGraph.

For an opt-in live run after selecting a model and providing `OPENAI_API_KEY`:

```bash
python -m benchmarks.run --model YOUR_MODEL --repeat 3 --output live-results.json
```

That command makes paid model calls. Review task success, unexpected questions, false successes, repeated semantic effects, and billed usage; do not assume the scripted trajectory generalizes. The benchmark's automatic responses match the support fixture schema and will not answer arbitrary model-generated questions.

## Still required for stronger release claims

1. Repeat live-model scenarios with recorded model/version configuration and a genuinely held-out task set.
2. Exercise real provider idempotency expiration, delayed status visibility, and network failures.
3. Have an independent developer build the second application from the documentation, then record where help was required and measure coordination effort against the baseline.
4. Visually exercise the support UI in a reachable browser and test the host's production authentication and deployment setup.

The core implementation is available as an experimental source release. These remaining checks gate claims about live reliability, developer experience, and production suitability.
