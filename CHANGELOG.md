# Changelog

## Unreleased — 2026-11-17

Add a GitHub issue-triage reference application and an internal developer-trial report. Fix verification of large proposals by resolving runtime-owned task artifacts before application checks; preserve compact public results. Add 18 regression/integration cases (69 total), read-only GitHub evidence, installed-wheel validation, and labelled Claude fixture runs. No real GitHub issue writes were performed.

## Unreleased — 2026-11-16

Add the native Anthropic provider, configurable thinking mode, a CLI smoke-test runner with preflight token counting and conservative budget reservations, and Claude selection in the support web demo. Add nine adapter/budget test cases (51 total). Six synthetic live scenarios passed on Claude Sonnet 5.5 using 34 inference calls at an estimated $0.185958. See `docs/CLAUDE_TEST.md`; this does not establish production reliability or baseline superiority.

## 0.1.0 — 2026-11-15

Initial experimental implementation: durable SQLite tasks and events; typed application actions; bounded agent execution; structured input and exact-action approval; cancellation and uncertain-write reconciliation; state-based completion checks; scoped artifacts; scripted and OpenAI providers; polling worker; optional FastAPI/SSE integration; support UI and project-management example; deterministic failure suite and LangGraph comparison.

Live-model evaluation, independent developer testing, visual browser validation, and PyPI publication are pending. See `docs/VALIDATION.md` for evidence and limitations.
