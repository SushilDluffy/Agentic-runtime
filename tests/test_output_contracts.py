"""Final output schemas constrain proposals without replacing application verification."""

import json
from datetime import date

import httpx
import jsonschema
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from agentic_runtime import (
    AnthropicProvider,
    AskDecision,
    CheckResult,
    FinishDecision,
    Limits,
    OpenAIProvider,
    Runtime,
    ScriptedProvider,
    SQLiteStore,
)
from tests.test_anthropic import response as claude_response
from tests.test_providers import response as openai_response
from tests.test_runtime import IDENTITY, make_runtime, submit


class Done(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    done: bool


class Item(BaseModel):
    label: str
    children: list["Item"] = []


class Report(BaseModel):
    items: list[Item]


async def test_invalid_output_repairs_before_business_checks(tmp_path):
    observed = []

    async def verify(ctx, output):
        observed.append(output)
        return CheckResult(name="business", passed=output["done"], detail="Observed business state")

    runtime = make_runtime(
        tmp_path,
        check=verify,
        steps=[
            FinishDecision(output={"wrong": "secret"}),
            FinishDecision(output={"done": False}),
            FinishDecision(output={"done": True}),
        ],
    )
    runtime.agents["test"].output_type = Done
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.decisions == task.usage.model_calls == 3
    assert observed == [{"done": False}, {"done": True}]
    assert task.checks[0].name == "business" and task.checks[0].passed
    assert task.messages[0]["error"] == "Final output does not match the required schema"
    assert any(e["location"] == ["done"] for e in task.messages[0]["validation_errors"])
    assert "secret" not in task.model_dump_json()


async def test_schema_repair_is_bounded_and_never_false_success(tmp_path):
    async def should_not_run(ctx, output):
        raise AssertionError("Business checks cannot see invalid output")

    runtime = make_runtime(tmp_path, check=should_not_run)
    runtime.agents["test"].output_type = Done
    runtime.agents["test"].provider = ScriptedProvider(
        lambda context: FinishDecision(output={"done": "yes"})
    )
    task = await runtime.run(submit(runtime, Limits(model_calls=2)).id)
    assert task.status == "failed" and task.reason == "Model call budget exhausted"
    assert task.usage.model_calls == 2 and task.checks == []


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_provider_sends_nested_output_schema_with_resolvable_references(tmp_path, vendor):
    payloads = []
    output = {"items": [{"label": "a", "children": [{"label": "b"}]}]}

    async def verify(ctx, output):
        return CheckResult(
            name="tree",
            passed=output["items"][0]["children"][0]["label"] == "b",
            detail="Nested result",
        )

    def handle(request):
        payloads.append(json.loads(request.content))
        data = (
            claude_response(arguments={"output": output})
            if vendor == "anthropic"
            else openai_response("runtime_finish", {"output": output})
        )
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = (
            AnthropicProvider("test", client=AsyncAnthropic(api_key="test", http_client=http))
            if vendor == "anthropic"
            else OpenAIProvider("test", client=AsyncOpenAI(api_key="test", http_client=http))
        )
        runtime = make_runtime(tmp_path, check=verify)
        runtime.agents["test"].provider = provider
        runtime.agents["test"].output_type = Report
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    tools = payloads[0]["tools"]
    if vendor == "anthropic":
        schema = next(t["input_schema"] for t in tools if t["name"] == "runtime_finish")
    else:
        schema = next(
            t["function"]["parameters"] for t in tools if t["function"]["name"] == "runtime_finish"
        )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"output": output}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"output": {"items": [{"wrong": True}]}}, schema)
    assert (
        task.proposal["items"][0]["children"][0]["children"] == []
    )  # Pydantic defaults normalized.


async def test_contract_survives_wait_restart_and_changed_contract_blocks(tmp_path):
    runtime = make_runtime(
        tmp_path, steps=[AskDecision(question="Ready?"), FinishDecision(output={"done": True})]
    )
    agent = runtime.agents["test"]
    agent.output_type = Done
    task = await runtime.run(submit(runtime).id)
    restarted = Runtime(SQLiteStore(tmp_path / "runtime.sqlite"), [agent])
    await restarted.respond(task.id, task.pending.id, IDENTITY, {})
    agent.output_type = Report
    blocked = await restarted.run(task.id)
    assert blocked.status == "blocked" and blocked.reason == "Agent output contract changed"
    assert blocked.usage.model_calls == 1
    agent.output_type = Done
    await restarted.resume(task.id, IDENTITY)
    done = await restarted.run(task.id)
    assert done.status == "succeeded" and done.output_schema == Done.model_json_schema()


async def test_typed_large_output_is_validated_full_after_restart(tmp_path):
    output = {"items": [{"label": "long " * 1200}]}
    seen = []

    async def verify(ctx, result):
        seen.append(result)
        return CheckResult(
            name="full",
            passed=len(result["items"][0]["label"]) == 6000,
            detail="Full artifact inspected",
        )

    runtime = make_runtime(tmp_path, check=verify)
    agent = runtime.agents["test"]
    agent.output_type = Report
    agent.provider = ScriptedProvider([FinishDecision(output=output)])
    task = submit(runtime)
    reply = await runtime._model(task.id, None)
    await runtime._accept(task.id, reply)
    assert runtime.store.get(task.id).proposal_artifact_id is not None
    restarted = Runtime(SQLiteStore(tmp_path / "runtime.sqlite"), [agent])
    done = await restarted.run(task.id)
    assert done.status == "succeeded" and len(seen) == 1
    assert done.usage.model_calls == 1
    assert "artifact_id" in done.proposal


async def test_redaction_cannot_turn_invalid_persisted_output_into_success(tmp_path):
    called = []

    async def passing(ctx, output):
        called.append(output)
        return CheckResult(name="always", passed=True, detail="Would otherwise pass")

    runtime = make_runtime(tmp_path, check=passing, steps=[FinishDecision(output={"done": True})])
    agent = runtime.agents["test"]
    agent.output_type = Done
    agent.redact = lambda value: {} if value == {"done": True} else value
    task = await runtime.run(submit(runtime).id)
    assert task.status != "succeeded" and called == []
    assert task.checks[0].name == "output_contract" and not task.checks[0].passed


async def test_json_output_preserves_aliases_and_strict_date_contract(tmp_path):
    class DatedReport(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        due_date: date = Field(alias="dueDate")

    seen = []

    async def verify(ctx, output):
        seen.append(output)
        return CheckResult(
            name="date", passed=output == {"dueDate": "2026-11-18"}, detail="JSON date"
        )

    runtime = make_runtime(
        tmp_path, check=verify, steps=[FinishDecision(output={"dueDate": "2026-11-18"})]
    )
    runtime.agents["test"].output_type = DatedReport
    task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded" and seen == [{"dueDate": "2026-11-18"}]
