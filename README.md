# Agentic Runtime

**Persistent agent tasks for Python applications.**

Give an agent your application's typed actions, authenticated user context, and completion checks. The runtime manages execution, durable questions and approvals, recovery, and a result record you can inspect.

**v0.1.1 is experimental.** The implementation, examples, failure tests, and scripted comparison are available. A six-scenario Claude smoke test passes; broader live-model evaluation and independent developer usability testing remain open. The package is installable from this repository; it has not been published to PyPI.

## What it handles

| Component | Behavior |
| --- | --- |
| Agent loop | Serial tool calls, validation, bounded retries, persistent usage and time budgets |
| Typed actions | Pydantic inputs/outputs, stable operation IDs, explicit read/write behavior |
| Application integration | Scoped dependencies, current context, authorization before dispatch |
| Durable tasks | SQLite state and events, restart recovery, single-host task locks |
| Human decisions | Structured input, approval bound to exact arguments, duplicate reply handling |
| Recovery | Reconcile ambiguous writes, reuse idempotency keys, block unresolved outcomes |
| Verification | Typed final outputs with bounded repair; application checks gate success |
| Usage evidence | Durable model attempts, rejected-response tokens, explicit unknowns, optional cost estimates |
| API | Python API, worker, optional FastAPI router and reconnectable SSE |

The [Claude live smoke test](docs/CLAUDE_TEST.md) passed six synthetic scenarios at an estimated $0.186 in model usage.

The product hypothesis is that these behaviors are easier to ship through one small application API. The current comparison validates selected failure behavior; it does **not** establish an advantage over LangGraph.

## Run the support application

Python 3.11+ is required. From a checkout:

```bash
git clone https://github.com/SushilDluffy/Agentic-runtime.git
cd Agentic-runtime
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web,openai]'
python -m uvicorn examples.support.app:app --host 127.0.0.1 --port 8000
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Open http://127.0.0.1:8000. Start a task, provide a resolution, inspect the exact send arguments, and approve. The result includes the recorded actions and checks of the ticket and delivery state. Reloading retains the current task; restarting the server retains pending requests.

The default scripted provider needs no key. Delivery is simulated in a separate local application database; this demo never sends email. The fixed demo identity is for loopback development only.

Run the same workflow without a browser:

```bash
python -m examples.support.run --directory .demo-normal
python -m examples.support.run --directory .demo-lost --fault lost_response
python -m examples.support.run --directory .demo-unknown --fault unknown
python -m examples.projects.run
```

A lost response recovers the recorded send without a second send. An unresolvable outcome blocks with an explicit unknown operation. CLI examples automatically answer their scripted prompts and approvals; the UI exposes them for human review.

For real model decisions, set `OPENAI_API_KEY` and `OPENAI_MODEL` in your environment before starting the web application. Choose a tool-calling model available to your account. This makes paid API calls. The runtime path, tools, approvals, and checks stay the same. The OpenAI path has mocked SDK tests; the [Claude live smoke test](docs/CLAUDE_TEST.md) records our first live-model results.

## Test with Claude

Install the optional adapter with `python -m pip install -e '.[anthropic]'`. The library reads `ANTHROPIC_API_KEY`, or the live test runner asks for it through a hidden terminal prompt. Never put credentials in command arguments, reports, or commits.

```bash
python -m examples.support.live --budget 1
```

The runner uses Claude Sonnet 5.5 and synthetic support data. It tests completion, rejection, reopening a waiting task, lost-response recovery, unknown outcomes, and cancellation before sending. It stops at the first unexpected failure and writes a report to `.demo-live/results.json`.

Before each inference call it counts input tokens, adds headroom, and reserves the full maximum output cost against the batch budget. It caps model calls, disables SDK retries, and retains reservations after failed requests. This process-local guard is not an account-wide billing cap. Rates are explicit in `examples/support/live.py` and must be reviewed when prices change. Re-running starts a new batch budget; reported costs are estimates, not a balance query.

For the web demo, set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL=claude-sonnet-5-5` instead of `OPENAI_MODEL`. The web demo uses runtime call and time limits; the dollar reservation guard belongs to the CLI test runner.

## Small Python API

This complete example runs with only the core dependencies. Save it as `quickstart.py`:

```python
import asyncio
from pydantic import BaseModel
from agentic_runtime import (
    Action, ActionDecision, Agent, CheckResult, FinishDecision,
    Identity, Runtime, SQLiteStore, ScriptedProvider,
)

class CountInput(BaseModel):
    text: str

class CountOutput(BaseModel):
    words: int

async def count_words(ctx, args):
    return CountOutput(words=len(args.text.split()))

async def verify(ctx, output):
    return CheckResult(
        name="word_count", passed=output == {"words": 3},
        detail="Compared output with the independently known input count.",
        evidence={"text": "Agents need checks", "expected_words": 3},
    )

agent = Agent(
    name="counter",
    provider=ScriptedProvider([
        ActionDecision(action="count_words", arguments={"text": "Agents need checks"}),
        FinishDecision(output={"words": 3}),
    ]),
    actions=[Action("count_words", CountInput, CountOutput, count_words)],
    checks=[verify],
    output_type=CountOutput,
)

async def main():
    runtime = Runtime(SQLiteStore("counter.sqlite"), [agent])
    identity = Identity(user_id="alice", workspace_id="example")
    task = runtime.submit("counter", "Count the words in: Agents need checks", identity)
    await runtime.run(task.id)
    print(runtime.result(task.id, identity).model_dump_json(indent=2))

asyncio.run(main())
```

Replace `ScriptedProvider(...)` with `OpenAIProvider(model="your-model")` after installing the `openai` extra. Business actions receive `ctx.task_id`, `ctx.identity`, `ctx.operation_id`, and `ctx.dependencies`. Use the operation ID for external idempotency or durable application deduplication.

## GitHub triage and the developer trial

The [GitHub issue-triage example](examples/github_triage/README.md) proposes labels and missing-information questions, with optional exact-action approval before adding labels. It runs on synthetic fixtures by default; real repository reads are opt-in. No comments are posted.

```bash
python -m pip install -e '.[github]'
python -m examples.github_triage.run
```

Our [internal developer trial](docs/DEVELOPER_TRIAL.md) found and fixed a large-output verification bug, exercised the installed package, and recorded live Claude runs. This is an author-led integration test, not independent evidence of developer-experience superiority.

## Guarantees and boundaries

- A task succeeds only after all registered completion checks pass. Their quality and coverage remain the application's responsibility.
- Task state and its event commit together. External service writes cannot share that transaction: adapters must implement truthful idempotency or reconciliation contracts.
- Unknown writes block rather than being silently assumed successful. There is no universal exactly-once guarantee.
- V1 targets one host with local SQLite and bounded concurrent tasks. Multi-host workers, untrusted-code sandboxes, and distributed leases are outside this release.
- Cancellation prevents later dispatch at runtime boundaries. It cannot undo an already dispatched external action.
- Tools are trusted asynchronous application code. Blocking Python code can prevent cooperative timeouts from working.
- Host applications supply authentication, membership checks, business permissions, rate limiting, and deployment supervision. The demo is not a production authentication system.

## Development and evidence

```bash
python -m pip install -e '.[dev,web,openai,anthropic,benchmark]'
python -m pytest -q
ruff check src tests examples benchmarks
ruff format --check src tests examples benchmarks
python -m benchmarks.run
python -m build
```

For the checked-in dependency resolution, use `uv sync --frozen --all-extras`, then `uv run --frozen --all-extras ...`. CI runs the tests on Python 3.11 and 3.12, verifies formatting, builds the distribution, and runs the scripted comparison.

The [V1 completion audit](docs/V1_READINESS.md) maps the implemented scope to its evidence and lists the remaining validation work.

See [the API guide](docs/API.md), [recovery and deployment contracts](docs/OPERATIONS.md), [validation report](docs/VALIDATION.md), and [V1 scope/status](docs/V1.md). [CONTRIBUTING.md](CONTRIBUTING.md) describes development expectations.

## License

MIT. Copyright 2026 SushilDluffy.
