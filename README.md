# Agentic Runtime

An open-source Python runtime for agents embedded in applications.

Define the actions your application supports, give the agent a task, and expose progress, questions, approvals, and results through one persistent task lifecycle.

## Project status

Design stage. The first release is planned as `v0.1.0`. The V1 specification describes intended behavior and release criteria. Implementation and benchmark results are pending.

## First release

- A bounded agent loop with typed tools and structured results.
- Application-owned context, dependencies, and action authorization.
- Persistent tasks that can wait for a user and resume after a restart.
- Explicit handling of tool failures and uncertain external outcomes.
- Application-defined completion checks and evidence records.
- A Python API, a small FastAPI integration, and resumable progress events.
- A runnable support application and deterministic failure tests.

The design hypothesis is that these capabilities can share a small API that reduces the application-specific coordination code needed to ship an agent feature. The reference application and comparison implementation will test that hypothesis.

Read the [V1 specification](docs/V1.md) for components, boundaries, milestones, and acceptance criteria.

## License

MIT. Copyright 2026 SushilDluffy.
