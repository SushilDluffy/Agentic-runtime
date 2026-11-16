# First Claude live test

Run date: November 16, 2026. Model: `claude-sonnet-5-5`. Official Anthropic Python SDK 0.125.0. Python 3.12.14. See the [raw report](../benchmarks/claude-live-2026-11-16.json) for per-call usage, latency, operation outcomes, checks, and dependency versions.

## Results

| Scenario | Observed result | Simulated sends | Passed |
| --- | --- | ---: | --- |
| Normal completion | succeeded; note and delivery checks passed | 1 | Yes |
| Approval rejection | blocked; operation recorded as denied | 0 | Yes |
| Restart during input wait | same persisted request reopened, task completed | 1 | Yes |
| Send committed, response lost | reconciled original operation, task completed | 1 | Yes |
| External outcome cannot be established | blocked; operation remained unknown | 1 | Yes |
| Cancellation at approval wait | cancelled before sending | 0 | Yes |

For successful tasks the runner also checked that the user-provided reference code appeared in both the internal note and outgoing message. The unknown-outcome scenario deliberately commits a simulated delivery but hides its status, so one recorded send alongside a blocked task is the expected behavior.

**All six scenarios passed in one paid batch.** The suite of mocked/deterministic runtime, HTTP, provider, and budget tests separately passes 51 tests. No LangGraph live comparison was performed.

## Usage and estimated cost

| Measurement | Value |
| --- | ---: |
| Inference requests | 34 |
| Reported input tokens | 69,129 |
| Reported output tokens | 4,770 |
| Estimated model cost | $0.185958 |
| Conservative reservations retained by the guard | $0.555772 |
| Configured batch budget | $1.00 |

Calculation uses standard global uncached Sonnet 5.5 rates of $2/million input tokens and $10/million output tokens, checked against [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) on the run date. The estimate is `(69,129 × 2 + 4,770 × 10) / 1,000,000`. No caching or paid server tools were requested. Reservations are an admission-control estimate, not money charged. No account balance or invoice was queried.

Before inference, the runner calls the token-count endpoint, adds 25% plus 512 input tokens of headroom, and reserves the full configured output allowance. Requests above 8,000 reserved input tokens or 1,024 output tokens are rejected. The batch allows at most 36 inference requests. SDK retries are disabled; the runtime's retries are admitted and counted individually. Failed or lost responses retain their reservations. These limits are process-local; restarting the runner creates a new budget.

## Issues discovered and fixed before paid generation

1. This environment uses a SOCKS proxy. The SDK required the `httpx[socks]` dependency; it failed at client initialization before making a model request. The optional Anthropic dependency set now includes that support.
2. Sonnet 5.5 rejects `thinking.type=disabled`. Two free token-count preflights returned that validation error. The adapter now accepts explicit thinking configuration and otherwise leaves the model default intact; this runner uses `between_tools`. See the [official migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

The runner reports sanitized preflight errors and records response usage before validating the model's tool decision, so invalid or truncated responses are included in its usage estimate. Credentials were supplied through a hidden terminal prompt and were not written to source files, reports, or commits.

## Reproduce

```bash
python -m pip install -e '.[anthropic]'
python -m examples.support.live --budget 1
```

The runner reads `ANTHROPIC_API_KEY` or prompts without echoing the key. It stops at the first unexpected failure and saves `.demo-live/results.json`. Review the price table before future paid runs. The default synthetic application uses local databases and never sends real email.

## What this proves and does not prove

This demonstrates that real Claude decisions can drive our existing runtime through six controlled lifecycle scenarios. It is one run per scenario, with a narrow support workflow and an application instruction specifying the structured input form. It does not measure general planning ability, success across diverse tasks, or production reliability.

The restart case recreates the Runtime/SQLiteStore while waiting in the same process; the independent deterministic suite tests actual process death after an external commit. Cancellation here occurs at an approval wait, while deterministic tests separately cover cancellation during an action. Delivery is simulated, so actual providers' delayed commits, idempotency-key expiry, and status visibility remain untested. Browser visual validation, independent developer evaluation, and a live baseline comparison remain open.
