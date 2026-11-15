"""Application-specific LangGraph baseline using the same tools, model and checks.

Preparation and approval are checkpointed before external effects. Write recovery
uses the application's reconciliation callback. This baseline deliberately uses
those capabilities rather than relying on graph replay to deduplicate a write.
"""

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agentic_runtime import ActionContext, CheckResult, Identity
from agentic_runtime.contracts import DECISION_ADAPTER, digest, new_id
from agentic_runtime.providers import ModelContext


class State(TypedDict, total=False):
    id: str
    objective: str
    identity: dict
    messages: list
    decision_index: int
    decision: dict
    operation: dict
    operations: list
    checks: list
    status: str
    output: object
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    approved: bool


def build_graph(agent, checkpointer):
    def context(state, operation_id=""):
        identity = Identity.model_validate(state["identity"])
        return ActionContext(state["id"], identity, operation_id, agent.dependencies(identity))

    async def decide(state):
        if state["model_calls"] >= 30:
            return {"status": "failed"}
        app_context = await agent.context(context(state)) if agent.context else None
        reply = await agent.provider.decide(
            ModelContext(
                state["objective"],
                agent.instructions,
                app_context,
                state["messages"],
                [a.schema() for a in agent.actions],
                state["decision_index"],
            )
        )
        return {
            "decision": reply.decision.model_dump(mode="json"),
            "decision_index": state["decision_index"] + 1,
            "model_calls": state["model_calls"] + 1,
            "input_tokens": state["input_tokens"] + reply.input_tokens,
            "output_tokens": state["output_tokens"] + reply.output_tokens,
        }

    def route(state):
        if state["status"] == "failed":
            return "done"
        return {"action": "prepare", "ask": "ask", "finish": "verify"}.get(
            state["decision"]["kind"], "blocked"
        )

    def prepare(state):
        decision = DECISION_ADAPTER.validate_python(state["decision"])
        action = agent.action(decision.action)
        arguments = action.input_type.model_validate(decision.arguments).model_dump(mode="json")
        return {
            "operation": {
                "id": new_id(),
                "action": action.name,
                "arguments": arguments,
                "digest": digest(arguments),
            },
            "approved": False,
        }

    def approval(state):
        action = agent.action(state["operation"]["action"])
        if action.approval:
            answer = interrupt({"kind": "approval", "operation": state["operation"]})
            if answer.get("digest") != state["operation"]["digest"]:
                return {"status": "blocked"}
            if answer.get("approve") is not True:
                return {"status": "blocked"}
        return {"approved": True}

    async def act(state):
        if state["status"] == "blocked":
            return {}
        operation = state["operation"]
        action = agent.action(operation["action"])
        args = action.input_type.model_validate(operation["arguments"])
        ctx = context(state, operation["id"])
        if agent.authorize and not await agent.authorize(ctx, action, args):
            return {"status": "blocked"}
        try:
            result = await action.handler(ctx, args)
            result = action.output_type.model_validate(result).model_dump(mode="json")
        except Exception:
            if not action.reconcile:
                return {"status": "blocked", "tool_calls": state["tool_calls"] + 1}
            recovered = await action.reconcile(ctx, args)
            if recovered.outcome != "applied":
                return {"status": "blocked", "tool_calls": state["tool_calls"] + 1}
            result = recovered.result
        record = {
            "action": action.name,
            "operation_id": operation["id"],
            "outcome": "applied",
            "result": result,
        }
        return {
            "messages": state["messages"] + [record],
            "operations": state["operations"] + [record],
            "tool_calls": state["tool_calls"] + 1,
        }

    def ask(state):
        answer = interrupt({"kind": "input", "question": state["decision"]["question"]})
        return {"messages": state["messages"] + [{"user_input": answer}]}

    async def verify(state):
        output = state["decision"]["output"]
        checks = [
            CheckResult.model_validate(await check(context(state), output))
            for check in agent.checks
        ]
        passed = checks and all(c.passed for c in checks)
        return {
            "checks": [c.model_dump(mode="json") for c in checks],
            "output": output,
            "status": "succeeded" if passed else "running",
            "messages": state["messages"]
            + [{"checks": [c.model_dump(mode="json") for c in checks]}],
        }

    graph = StateGraph(State)
    graph.add_node("decide", decide)
    graph.add_node("prepare", prepare)
    graph.add_node("approval", approval)
    graph.add_node("act", act)
    graph.add_node("ask", ask)
    graph.add_node("verify", verify)
    graph.add_node("blocked", lambda state: {"status": "blocked"})
    graph.add_edge(START, "decide")
    graph.add_conditional_edges(
        "decide",
        route,
        {"prepare": "prepare", "ask": "ask", "verify": "verify", "blocked": "blocked", "done": END},
    )
    graph.add_edge("prepare", "approval")
    graph.add_edge("approval", "act")
    graph.add_conditional_edges("act", lambda s: END if s["status"] == "blocked" else "decide")
    graph.add_edge("ask", "decide")
    graph.add_conditional_edges("verify", lambda s: END if s["status"] == "succeeded" else "decide")
    graph.add_edge("blocked", END)
    return graph.compile(checkpointer=checkpointer)


def initial_state(task_id, objective, identity):
    # JSON round-trip keeps the benchmark input identical across persistence implementations.
    return json.loads(
        json.dumps(
            {
                "id": task_id,
                "objective": objective,
                "identity": identity.model_dump(),
                "messages": [],
                "operations": [],
                "decision_index": 0,
                "status": "running",
                "checks": [],
                "model_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
    )
