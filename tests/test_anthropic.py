import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from anthropic import AsyncAnthropic

from agentic_runtime import AnthropicProvider, TransientError
from agentic_runtime.providers import ModelContext
from examples.support.live import GuardedMessages, SpendLimit
from tests.test_runtime import make_runtime, submit


def response(name="runtime_finish", arguments=None, *, stop_reason="tool_use"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "test-model",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_test",
                "name": name,
                "input": arguments if arguments is not None else {"output": {"done": True}},
            }
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


async def test_anthropic_official_sdk_drives_runtime(tmp_path):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        data = response("work", {"value": 7}) if len(requests) == 1 else response()
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)
        runtime = make_runtime(tmp_path)
        runtime.agents["test"].provider = AnthropicProvider(
            "test-model", client=client, thinking={"type": "between_tools"}
        )
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.operations[0].result == {"value": 7}
    assert task.usage.input_tokens == 200 and task.usage.output_tokens == 40
    assert requests[0]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert requests[0]["max_tokens"] == 1024
    assert requests[0]["thinking"] == {"type": "between_tools"}
    assert "test-key" not in task.model_dump_json()


@pytest.mark.parametrize("variant", ["truncated", "no_tool", "multiple_tools", "bad_arguments"])
async def test_anthropic_rejects_incomplete_or_ambiguous_decisions(variant):
    data = response()
    if variant == "truncated":
        data["stop_reason"] = "max_tokens"
    elif variant == "no_tool":
        data["content"] = [{"type": "text", "text": "Done"}]
        data["stop_reason"] = "end_turn"
    elif variant == "multiple_tools":
        data["content"].append(dict(data["content"][0], id="toolu_second"))
    else:
        data["content"][0]["input"] = {"wrong": True}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as http:
        provider = AnthropicProvider(
            "test-model", client=AsyncAnthropic(api_key="test", http_client=http, max_retries=0)
        )
        with pytest.raises(TransientError):
            await provider.decide(ModelContext("task", "instructions", None, [], [], 0))


async def test_anthropic_rate_limit_retry_is_runtime_owned(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "Rate limited"},
                },
            )
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = make_runtime(tmp_path)
        runtime.agents["test"].provider = AnthropicProvider(
            "test", client=AsyncAnthropic(api_key="test", http_client=http, max_retries=0)
        )
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert len(calls) == task.usage.model_calls == 2


async def test_live_budget_refuses_inference_before_spending():
    paths = []

    def transport(request):
        paths.append(request.url.path)
        assert request.url.path.endswith("count_tokens")
        return httpx.Response(200, json={"input_tokens": 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncAnthropic(api_key="test", http_client=http, max_retries=0)
        guard = GuardedMessages(client, "claude-sonnet-5-5", Decimal("0.001"))
        provider = AnthropicProvider("claude-sonnet-5-5", client=SimpleNamespace(messages=guard))
        with pytest.raises(SpendLimit):
            await provider.decide(ModelContext("task", "instructions", None, [], [], 0))
    assert len(paths) == 1 and guard.reserved == 0 and guard.records == []


async def test_live_budget_retains_reservation_after_failed_request():
    paths = []

    def transport(request):
        paths.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            500,
            json={
                "type": "error",
                "error": {"type": "api_error", "message": "sensitive body must not enter report"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncAnthropic(api_key="test", http_client=http, max_retries=0)
        guard = GuardedMessages(client, "claude-sonnet-5-5", max_calls=1)
        provider = AnthropicProvider("claude-sonnet-5-5", client=SimpleNamespace(messages=guard))
        context = ModelContext("task", "instructions", None, [], [], 0)
        with pytest.raises(TransientError):
            await provider.decide(context)
        reserved = guard.reserved
        with pytest.raises(SpendLimit):
            await provider.decide(context)
    assert reserved > 0 and guard.reserved == reserved
    assert len(paths) == 2  # One count call, one inference call; no SDK retry.
    assert "sensitive body" not in json.dumps(guard.records)


async def test_live_budget_records_usage_even_when_model_response_is_invalid():
    def transport(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response(stop_reason="max_tokens"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncAnthropic(api_key="test", http_client=http, max_retries=0)
        guard = GuardedMessages(client, "claude-sonnet-5-5")
        provider = AnthropicProvider("claude-sonnet-5-5", client=SimpleNamespace(messages=guard))
        with pytest.raises(TransientError):
            await provider.decide(ModelContext("task", "instructions", None, [], [], 0))
    assert guard.records[0]["input_tokens"] == 100
    assert guard.records[0]["estimated_usd"] == pytest.approx(0.0004)
