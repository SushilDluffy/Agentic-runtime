"""Run deterministic scenarios or opt into paid live-model comparisons."""

import argparse
import asyncio
import importlib.metadata
import json
import tempfile
import time
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from agentic_runtime import Identity, OpenAIProvider, Runtime, SQLiteStore
from agentic_runtime.contracts import new_id
from examples.support.domain import SupportDatabase, support_agent

from .langgraph_support import build_graph, initial_state


async def runtime_case(directory, fault, ticket_id, resolution, provider):
    identity = Identity(user_id="benchmark", workspace_id="test")
    domain = SupportDatabase(directory / "domain.sqlite", fault=fault)
    domain.seed(identity, ticket_id)
    runtime = Runtime(
        SQLiteStore(directory / "runtime.sqlite"),
        [support_agent(domain, ticket_id=ticket_id, provider=provider)],
    )
    task = runtime.submit(
        "support", f"Resolve {ticket_id}, record a note and send the resolution", identity
    )
    started = time.perf_counter()
    task = await runtime.run(task.id)
    interventions = 0
    while task.pending and interventions < 10:
        pending = task.pending
        value = {"resolution": resolution} if pending.kind == "input" else True
        await runtime.respond(
            task.id, pending.id, identity, value, input_digest=pending.input_digest
        )
        interventions += 1
        task = await runtime.run(task.id)
    return {
        "engine": "agentic-runtime",
        "status": task.status,
        "sends": domain.count_sends(),
        "model_calls": task.usage.model_calls,
        "tool_calls": task.usage.tool_calls,
        "interventions": interventions,
        "input_tokens": task.usage.input_tokens,
        "output_tokens": task.usage.output_tokens,
        "seconds": round(time.perf_counter() - started, 4),
    }


async def graph_case(directory, fault, ticket_id, resolution, provider):
    identity = Identity(user_id="benchmark", workspace_id="test")
    domain = SupportDatabase(directory / "domain.sqlite", fault=fault)
    domain.seed(identity, ticket_id)
    agent = support_agent(domain, ticket_id=ticket_id, provider=provider)
    task_id = new_id()
    started = time.perf_counter()
    interventions = 0
    async with AsyncSqliteSaver.from_conn_string(str(directory / "graph.sqlite")) as saver:
        graph = build_graph(agent, saver)
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 100}
        state = await graph.ainvoke(
            initial_state(
                task_id, f"Resolve {ticket_id}, record a note and send the resolution", identity
            ),
            config,
        )
        while state.get("__interrupt__") and interventions < 10:
            request = state["__interrupt__"][0].value
            answer = {"resolution": resolution}
            if request["kind"] == "approval":
                answer = {"approve": True, "digest": request["operation"]["digest"]}
            interventions += 1
            state = await graph.ainvoke(Command(resume=answer), config)
    return {
        "engine": "langgraph",
        "status": state["status"],
        "sends": domain.count_sends(),
        "model_calls": state["model_calls"],
        "tool_calls": state["tool_calls"],
        "interventions": interventions,
        "input_tokens": state["input_tokens"],
        "output_tokens": state["output_tokens"],
        "seconds": round(time.perf_counter() - started, 4),
    }


async def main(output, model, repeat):
    results = []
    with tempfile.TemporaryDirectory() as root:
        for iteration in range(repeat):
            for fault in ["none", "lost_response", "unknown"]:
                for engine, execute in [("runtime", runtime_case), ("langgraph", graph_case)]:
                    directory = Path(root) / f"{iteration}-{fault}-{engine}"
                    directory.mkdir()
                    provider = OpenAIProvider(model) if model else None
                    row = await execute(
                        directory,
                        fault,
                        f"T-{100 + iteration}",
                        f"Resolution {iteration}: the invoice link is available.",
                        provider,
                    )
                    row.update(fault=fault, iteration=iteration)
                    expected = "blocked" if fault == "unknown" else "succeeded"
                    row["scenario_passed"] = row["status"] == expected and row["sends"] == 1
                    row["false_success"] = row["status"] == "succeeded" and expected == "blocked"
                    results.append(row)
    report = {
        "mode": "live" if model else "scripted",
        "model": model,
        "versions": {
            p: importlib.metadata.version(p)
            for p in ["agentic-runtime", "langgraph", "langgraph-checkpoint-sqlite", "openai"]
        },
        "scenarios": results,
        "limitations": [
            "Scripted decisions measure runtime mechanics only.",
            "The baseline implements the compared workflow with a smaller feature scope.",
            "Wall time includes local storage and scheduler variability.",
            "No developer usability or implementation-time measurement is included.",
        ],
    }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(report, indent=2) + "\n")
    passed = sum(row["scenario_passed"] for row in results)
    print(f"{passed}/{len(results)} scenario runs passed. {output}")
    if not all(row["scenario_passed"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/results.json")
    parser.add_argument("--model", help="Opt into paid OpenAI calls using OPENAI_API_KEY")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    asyncio.run(main(args.output, args.model, args.repeat))
