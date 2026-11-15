import json

import httpx
import pytest
from openai import AsyncOpenAI

from agentic_runtime import OpenAIProvider, TransientError
from agentic_runtime.providers import ModelContext
from tests.test_runtime import make_runtime, submit


def response(name, args):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def test_official_client_drives_runtime_with_recorded_http(tmp_path):
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json=response("work", {"value": 9}))
        return httpx.Response(200, json=response("runtime_finish", {"output": {"done": True}}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)
        runtime = make_runtime(tmp_path)
        runtime.agents["test"].provider = OpenAIProvider("test-model", client=client)
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.operations[0].result == {"value": 9}
    assert task.usage.input_tokens == 20 and task.usage.output_tokens == 10
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["model"] == "test-model"
    assert "test-key" not in task.model_dump_json()


async def test_rate_limit_is_retried_by_runtime(tmp_path):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limited"}})
        return httpx.Response(200, json=response("runtime_finish", {"output": {"done": True}}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = make_runtime(tmp_path)
        runtime.agents["test"].provider = OpenAIProvider(
            "test", client=AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)
        )
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.usage.model_calls == 2


async def test_malformed_model_arguments_are_recoverable():
    data = response("work", {})
    data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{bad"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as http:
        provider = OpenAIProvider("test", client=AsyncOpenAI(api_key="test", http_client=http))
        with pytest.raises(TransientError):
            await provider.decide(ModelContext("task", "instructions", None, [], [], 0))
