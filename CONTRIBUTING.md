# Contributing

Use Python 3.11+ and install the repository with `python -m pip install -e '.[dev,web,openai,benchmark]'`, or use `uv sync --frozen --all-extras` for the locked environment.

Before proposing a change, run pytest, Ruff lint and formatting checks, and `python -m build`. Changes to dispatch, approval, storage, or recovery need a regression scenario showing the failure boundary. Use deterministic handlers for reproducibility; separately label any paid live-model evaluation. Do not commit credentials, task databases, or customer records.

Keep the public API small. New abstractions should have a concrete application caller. Document recovery semantics and limits rather than assuming an external write is safe to retry. A successful task must continue to mean that the registered application checks passed.

The reference examples are application code, outside the installed core package. `docs/API.md` describes the supported surface; `docs/OPERATIONS.md` explains the single-host boundary. API compatibility is experimental in 0.1.x: incompatible persisted agent/action behavior must be explicitly versioned.
