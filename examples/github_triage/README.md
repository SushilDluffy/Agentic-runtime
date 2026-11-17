# GitHub issue triage

A reference application using the public `agentic_runtime` API. It reads a bounded set of issues, proposes existing labels, identifies missing information, and returns a typed report. Optional label additions require approval of the exact issue, labels, and source revision. It does not post comments or create issues.

## Run locally

From a repository checkout, using Python 3.11+:

```bash
python -m pip install -e '.[github]'
python -m examples.github_triage.run
```

The default uses three explicitly synthetic issues and deterministic decisions. It verifies application integration without spending model credits or touching GitHub. The report is `.demo-triage/report.json`.

Exercise approved simulated writes and recovery:

```bash
python -m examples.github_triage.run --apply-labels --approve-fixtures --fault lost_response
python -m examples.github_triage.run --apply-labels --approve-fixtures --fault unknown
```

The unknown case intentionally exits nonzero with a blocked task. Automatic approval is only accepted for fixtures; it is rejected when `--repo` is supplied.

Read the public project's current open issues:

```bash
python -m examples.github_triage.run --repo SushilDluffy/Agentic-runtime
```

An empty repository completes without a model. Nonempty real repositories require a live model; the fixture script is not a classifier. Choose at most ten explicit issues with `--issues 1 2 3`, or use the first five open issues by omitting that flag. Pull requests are excluded. The list operation is bounded to ten pages; exceeding that bound fails rather than silently claiming a complete scan.

For real model decisions:

```bash
python -m pip install -e '.[github,anthropic]'
python -m examples.github_triage.run --model claude-sonnet-5-5
python -m examples.github_triage.run --repo OWNER/REPO --issues 1 2 --model claude-sonnet-5-5
```

Set `ANTHROPIC_API_KEY` in your environment or enter it at the hidden terminal prompt. Each invocation uses the evaluation runner's process-local $0.25 reservation limit, at most 16 admitted inference calls, and at most eight runtime model calls. An invocation is a separate budget. This is not an account-wide billing cap. The helper comes from `examples.support.live` and is not part of the installed runtime API. Raw per-call usage is included in the report, including truncated responses.

The default is read-only, including with a GitHub token. For private reads, set `GITHUB_TOKEN` with appropriate repository access. To deliberately add labels to a real repository, also supply `--apply-labels`; every proposed change prompts locally before dispatch. No live label writes were made during the recorded trial.

## Responsibilities

- `domain.py`: GitHub REST client, pagination, typed issue/report contracts, content revision hashes.
- `app.py`: runtime integration, issue scope, authorization, write preconditions, reconciliation, completion checks.
- `fixtures.py`: fake HTTP service and deterministic fixture decisions.
- `run.py`: CLI, identity, worker invocation, approval interaction, report writing, optional evaluation budget wrapper.

Application code imports runtime functionality from `agentic_runtime`, without private submodule imports. The package's `github` extra supplies HTTP dependencies for this source-checkout example; the example itself is not shipped inside the core wheel.

## Verification and recovery boundaries

The checks verify that every selected issue appears exactly once, source content revisions still match, proposed labels exist, and requested labels are observable in apply mode. They do **not** prove summary accuracy or label suitability. Review those judgements yourself. The model sees issue text, not repository implementation files, and should attribute unverified assertions to the reporter.

The issue revision hashes repository, number, title, body, and state. Labels are excluded because changes are additive. The adapter checks the revision before writing, but this is not an atomic compare-and-set: another actor can edit after that read. A final revision check can detect a mismatch, but cannot undo a write already performed.

Writes use GitHub's additive label endpoint, never label replacement. On an ambiguous response, reconciliation observes the desired labels. Their presence proves current state, not which actor caused it. Absence is unknown because the original request might still complete; the runtime blocks instead of blindly retrying. No external idempotency key or universal exactly-once guarantee is claimed. See [GitHub label endpoints](https://docs.github.com/en/rest/issues/labels).

This CLI is one local developer session with a single bound issue scope. It is not a multi-user web service. Production hosts must authenticate users, persist task-specific scope/access configuration, resolve scoped credentials, and provide their own task-resume UI. The current CLI creates a new task on each invocation; the integration tests demonstrate reopening the store while retaining the same agent configuration. Existing report/task IDs can be inspected through the public Python API.

Public reports include issue text. Avoid publishing private repository reports, tokens, or customer data. The recorded trial uses public empty-repository metadata and synthetic issues only.
