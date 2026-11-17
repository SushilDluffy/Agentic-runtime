"""Usage evidence must survive rejected responses, retries, interruption and restart."""

import asyncio
import json
import subprocess
import sys
from decimal import Decimal

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from agentic_runtime import (
    AnthropicProvider,
    FinishDecision,
    Limits,
    ModelReply,
    ModelResponseError,
    ModelUsage,
    OpenAIProvider,
    TokenPrices,
)
from tests.test_anthropic import response as claude_response
from tests.test_providers import response as openai_response
from tests.test_runtime import IDENTITY, make_runtime, submit


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
@pytest.mark.parametrize("failure", ["truncated", "malformed", "no_calls"])
async def test_rejected_responses_count_in_task_usage(tmp_path, vendor, failure):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        if vendor == "anthropic":
            data = claude_response()
            data["usage"].update(cache_read_input_tokens=40, cache_creation_input_tokens=10)
            if len(requests) == 1:
                if failure == "truncated":
                    data["stop_reason"] = "max_tokens"
                elif failure == "malformed":
                    data["content"][0]["input"] = {"wrong": "sensitive"}
                else:
                    data["content"] = []
        else:
            data = openai_response("runtime_finish", {"output": {"done": True}})
            data["usage"]["prompt_tokens_details"] = {"cached_tokens": 4}
            if len(requests) == 1:
                if failure == "truncated":
                    data["choices"][0]["finish_reason"] = "length"
                elif failure == "malformed":
                    data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{bad"
                else:
                    data["choices"] = []
        return httpx.Response(200, json=data)

    prices = TokenPrices(input=2, output=10, cache_read="0.2", cache_creation="2.5")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = make_runtime(tmp_path)
        if vendor == "anthropic":
            provider = AnthropicProvider(
                "test",
                client=AsyncAnthropic(api_key="test", http_client=http, max_retries=0),
                prices=prices,
            )
        else:
            provider = OpenAIProvider(
                "test",
                client=AsyncOpenAI(api_key="test", http_client=http, max_retries=0),
                prices=prices,
            )
        runtime.agents["test"].provider = provider
        task = await runtime.run(submit(runtime).id)
    assert task.status == "succeeded"
    assert task.usage.model_calls == len(task.model_calls) == 2
    assert task.decisions == 1
    assert [c.state for c in task.model_calls] == ["rejected", "returned"]
    assert task.usage.input_tokens == (300 if vendor == "anthropic" else 20)
    assert task.usage.output_tokens == (40 if vendor == "anthropic" else 10)
    assert task.usage.estimated_cost_usd == (
        Decimal("0.000866") if vendor == "anthropic" else Decimal("0.0001256")
    )
    assert task.usage.costed_model_calls == 2
    assert task.model_calls[0].usage.cache_read_input_tokens == (40 if vendor == "anthropic" else 4)
    assert "sensitive" not in task.model_dump_json()
    assert runtime.result(task.id, IDENTITY).model_calls == task.model_calls
    assert runtime.result(task.id, IDENTITY).review_items == []
    # Settlement is idempotent even after a new Runtime reads persisted data.
    restarted = make_runtime(tmp_path)
    restarted._settle_model_call(
        task.id, task.model_calls[0].id, "returned", ModelUsage(input_tokens=99)
    )
    assert restarted.store.get(task.id).usage == task.usage


async def test_usage_remains_counted_if_acceptance_fails(tmp_path):
    class Provider:
        async def decide(self, context):
            return ModelReply(
                decision=FinishDecision(output="x" * 2000), input_tokens=17, output_tokens=3
            )

    runtime = make_runtime(tmp_path)
    runtime.agents["test"].provider = Provider()
    task = await runtime.run(submit(runtime, Limits(artifact_bytes=1024)).id)
    assert task.status == "failed"
    assert task.usage.input_tokens == 17 and task.usage.output_tokens == 3
    assert task.model_calls[0].state == "returned"
    assert task.decisions == 0


@pytest.mark.parametrize("failure", ["timeout", "connection", "missing_usage"])
async def test_unreported_usage_is_not_claimed_as_zero(tmp_path, failure):
    class Provider:
        async def decide(self, context):
            if failure == "timeout":
                await asyncio.sleep(60)
            elif failure == "connection":
                raise ConnectionError("secret credentials")
            return ModelReply(decision=FinishDecision(output={"done": True}), usage_known=False)

    runtime = make_runtime(tmp_path)
    runtime.agents["test"].provider = Provider()
    task = await runtime.run(submit(runtime, Limits(call_timeout=0.01, model_retries=0)).id)
    assert len(task.model_calls) == 1 and task.model_calls[0].usage is None
    assert any(
        "Per-call usage unavailable for 1" in s
        for s in runtime.result(task.id, IDENTITY).review_items
    )
    assert "secret credentials" not in task.model_dump_json()


async def test_worker_cancellation_and_restart_keep_usage_unknown(tmp_path):
    started = asyncio.Event()

    class Provider:
        async def decide(self, context):
            started.set()
            await asyncio.Event().wait()

    runtime = make_runtime(tmp_path)
    runtime.agents["test"].provider = Provider()
    task = submit(runtime)
    job = asyncio.create_task(runtime.run(task.id))
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    task = runtime.store.get(task.id)
    assert task.model_calls[0].state == "interrupted"
    restarted = make_runtime(tmp_path, steps=[FinishDecision(output={"done": True})])
    done = await restarted.run(task.id)
    assert done.status == "succeeded" and done.usage.model_calls == 2
    assert done.model_calls[0].usage is None and done.model_calls[1].usage is not None


async def test_real_process_crash_during_model_call(tmp_path):
    runtime = make_runtime(tmp_path, steps=[FinishDecision(output={"done": True})])
    task = submit(runtime)
    code = """import asyncio, os, sys
from pathlib import Path
from tests.test_runtime import make_runtime
class Crash:
    async def decide(self, context):
        os._exit(93)
r = make_runtime(Path(sys.argv[1]))
r.agents['test'].provider = Crash()
asyncio.run(r.run(sys.argv[2]))
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(tmp_path), task.id],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 93, process.stderr
    before = runtime.store.get(task.id)
    assert before.model_calls[0].state == "dispatched"
    task = await runtime.run(task.id)
    assert task.status == "succeeded"
    assert task.usage.model_calls == 2 and len(task.model_calls) == 2
    assert task.model_calls[0].state == "interrupted"
    assert task.model_calls[0].error_type == "WorkerLost"
    assert task.model_calls[0].usage is None


async def test_retry_budget_has_one_ledger_record_per_dispatch(tmp_path):
    class Provider:
        async def decide(self, context):
            raise ModelResponseError("invalid", usage=ModelUsage(input_tokens=11, output_tokens=2))

    runtime = make_runtime(tmp_path)
    runtime.agents["test"].provider = Provider()
    task = await runtime.run(
        submit(runtime, Limits(model_calls=2, model_retries=5, backoff_seconds=0)).id
    )
    assert task.status == "failed" and task.reason == "Model call budget exhausted"
    assert task.usage.model_calls == len(task.model_calls) == 2
    assert task.usage.input_tokens == 22 and task.usage.output_tokens == 4
    assert task.decisions == 0


def test_estimates_require_prices_for_each_used_bucket():
    prices = TokenPrices(input=2, output=10)
    assert prices.estimate(ModelUsage(input_tokens=10, output_tokens=2)) == Decimal("0.00004")
    assert prices.estimate(ModelUsage(input_tokens=10, cache_read_input_tokens=1)) is None
    assert prices.estimate(ModelUsage(input_tokens=10, cache_creation_input_tokens=1)) is None
    with pytest.raises(ValueError):
        ModelUsage(input_tokens=10, cache_read_input_tokens=11)
    with pytest.raises(ValueError):
        TokenPrices(input="NaN", output=1)


async def test_v1_store_upgrade_preserves_old_totals_without_fabricating_calls(tmp_path):
    runtime = make_runtime(tmp_path, steps=[FinishDecision(output={"done": True})])
    task = submit(runtime)
    old = task.model_dump(mode="json")
    old.pop("model_calls")
    old.pop("output_schema")
    old["usage"].pop("estimated_cost_usd")
    old["usage"].pop("costed_model_calls")
    old["usage"].update(model_calls=2, input_tokens=100, output_tokens=20)
    events = runtime.store.events(task.id)
    with runtime.store._connect() as db:
        db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(old), task.id))
        db.execute("PRAGMA user_version=1")
    restarted = make_runtime(tmp_path, steps=[FinishDecision(output={"done": True})])
    assert restarted.store.events(task.id) == events
    with restarted.store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    done = await restarted.run(task.id)
    assert done.status == "succeeded" and done.usage.model_calls == 3
    assert len(done.model_calls) == 1 and done.usage.input_tokens == 100
    assert any(
        "Per-call usage unavailable for 2" in s
        for s in restarted.result(task.id, IDENTITY).review_items
    )


async def test_cancellation_during_model_reply_preserves_usage(tmp_path):
    runtime = make_runtime(tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()

    class Provider:
        async def decide(self, context):
            started.set()
            await finish.wait()
            return ModelReply(decision=FinishDecision(output={"done": True}), input_tokens=25)

    runtime.agents["test"].provider = Provider()
    task = submit(runtime)
    running = asyncio.create_task(runtime.run(task.id))
    await started.wait()
    runtime.cancel(task.id, IDENTITY)
    finish.set()
    done = await running
    assert done.status == "cancelled" and done.proposal is None
    assert done.usage.input_tokens == 25 and done.model_calls[0].state == "returned"
