# Internal developer adoption trial — November 17, 2026

## Method and scope

The framework's AI collaborator acted as a developer building a new GitHub issue-triage application from the public documentation and API. The collaborator already knew the runtime implementation. This is an author-led integration exercise, not an independent participant study, blinded trial, or evidence that onboarding is easier than LangGraph.

The application uses public top-level `agentic_runtime` imports. It supplies its own Pydantic contracts, GitHub client, permissions, source preconditions, and completion checks. Runtime internals were changed only after a new application test exposed a verification defect. No elapsed developer-time estimate is reported: the session included interruptions and did not use a controlled timing protocol.

See the [application guide](../examples/github_triage/README.md) for runnable commands and boundaries.

## What ran

| Exercise | Evidence | Scope |
| --- | --- | --- |
| Real GitHub reads | [Report](../benchmarks/triage-github-read-2026-11-17.json) | The repository had no open issues; the client fetched labels and verified the empty task |
| Approved writes with lost responses | [Report](../benchmarks/triage-fixture-recovery-2026-11-17.json) | HTTP simulation; one write per issue, no duplicate retry |
| Installed-wheel example | [Report](../benchmarks/triage-installed-wheel-2026-11-17.json) | Clean virtual environment installed the built wheel with its github extra |
| Claude triage, initial prompt | [Report](../benchmarks/triage-claude-2026-11-17.json) | Real model, three synthetic issues, read-only |
| Claude triage, attribution prompt | [Report](../benchmarks/triage-claude-attributed-2026-11-17.json) | Real model; preserves two truncated responses in usage evidence |
| Claude triage, concise prompt | [Report](../benchmarks/triage-claude-concise-2026-11-17.json) | Final prompt asks for short summaries and at most three questions |

No GitHub issue, comment, or label was created or changed during the trial. The fixture containing malicious instructions was never a real GitHub issue. Its external URL was input text only, and the application exposes no arbitrary network or credential-reading tool.

## Defect found and fixed

The initial integration suite passed 15 cases and failed one: a valid triage report exceeding the inline-output threshold became an artifact reference before completion checks ran. The application's check consequently received a reference object instead of the report and rejected an otherwise valid result.

The runtime now persists a dedicated `proposal_artifact_id` only when it actually stores a proposal as an artifact. Verification resolves that task-scoped artifact before invoking application checks. Public task/result responses keep the compact reference. A small user-authored object that resembles an artifact reference is treated as literal output, not automatically dereferenced.

Regression coverage includes a large valid application report, a restart in verifying state, and an output that resembles an artifact reference. Old stored tasks lacking the new optional field still deserialize. The runtime does not infer artifact ownership from arbitrary model output.

## Model findings

The first live run produced a structurally valid report and identified the vague issue's missing information. It also identified the injected instruction in the documentation fixture and did not request unrelated actions or invent the nonexistent `critical` label.

However, it repeated a reporter's claim about the README without reading the README. The application cannot independently verify such a claim because its only read capability is issue data. We revised the instruction to attribute unverified claims to the reporter. The follow-up improved attribution, but its longer report hit the 1,024-token output limit twice. Both truncated responses were rejected and retried within the existing budget. The guard's per-call ledger preserved their billed usage.

The final prompt additionally requests summaries under 50 words and at most three missing-information questions per issue. Its recorded run completed in two model calls with no truncation. All raw attempts are retained; the final result does not erase the earlier failures. This is a small prompt iteration, not a statistical quality or prompt-injection robustness evaluation.

Success here means the configured coverage/revision/label checks passed. Classification quality, misleading phrasing, and whether a reported bug is already documented remain human review responsibilities.

## Paid usage

| Run | Calls | Truncated responses | Estimated USD |
| --- | ---: | ---: | ---: |
| Initial | 2 | 0 | 0.018698 |
| Attribution revision | 4 | 2 | 0.048934 |
| Concise revision | 2 | 0 | 0.016538 |
| Total | 8 | 2 | **0.084170** |

The estimates include 17,635 reported input tokens and 4,890 output tokens, including the two rejected truncated responses. Each run used the existing preflight reservation guard; no provider account balance was queried. No model calls were needed for the empty live GitHub read or deterministic fixture runs. Pricing assumptions are documented in the shared guard and the earlier [Claude test report](CLAUDE_TEST.md).

## Developer friction

| Finding | Observed consequence | Decision |
| --- | --- | --- |
| Large proposals reached checks as references | A valid new application failed verification | Fixed in core with regression tests |
| No declared final-output model on Agent | Application embeds its schema in instructions and validates it in a check | Candidate for a small typed-output API |
| Task usage excludes rejected provider replies | Two billed truncated responses were absent from task token totals | Keep the per-call ledger; prioritize consistent core usage accounting |
| Dollar guard lives in a support example | New evaluation runner imports an example helper | Candidate for a reusable provider-budget component |
| GitHub recovery is service-specific | Application must implement freshness, additive writes, and conservative reconciliation | Document the contract; do not hide it in generic retries |
| Task-specific scope is application-owned | CLI binds one issue set; a hosted app needs persistent scope/access data | Add a documented hosted-application recipe before claiming easy production integration |
| Structural checks do not evaluate judgement | A successful task can contain an unverified factual claim | Show check coverage and human review requirements explicitly |

The application has 296 nonblank, non-comment-only lines across its domain adapter and runtime integration, plus 152 CLI lines, 101 fixture lines, and a one-line package initializer. These counts include docstrings and formatting.

The application still contains substantial domain and integration code. This trial does not justify describing agent integration as only a handful of lines.

## Validation and reproducibility

The full suite passes 69 tests: the previous 51, 16 GitHub integration cases, and two additional core artifact regressions. The existing 18 scripted support comparison runs continue to pass. Package build and clean-wheel execution pass. CI remains credential-free; it does not run paid model calls or modify GitHub issues.

Source size counts and paid usage are in [the trial summary](../benchmarks/developer-trial-2026-11-17.json). Source counts exclude blank lines and comment-only lines, include docstrings and formatting, and are descriptive rather than a productivity benchmark. There is no equivalent new LangGraph implementation or measured implementation-effort comparison in this trial.

## Next bounded work

Prioritize consistent model-call usage accounting and a typed final-output contract, using the failures above as acceptance tests. Then exercise a hosted application with persistent task scope and real selected issues. Independent developer feedback and a comparable baseline exercise are still needed to substantiate the differentiation claim.


## Follow-up — November 18, 2026

v0.1.1 implements the two priorities above: `Agent.output_type` and durable model-call usage accounting, including rejected/truncated responses and cache buckets. The triage app now declares `output_type=TriageReport` instead of duplicating the schema in its prompt. Original measured reports above remain unchanged; no new paid run or independent participant is implied. See [V1 readiness](V1_READINESS.md) for current validation.
