import json

import httpx
from fastapi import FastAPI, Request

from agentic_runtime import AskDecision, FinishDecision, Identity
from agentic_runtime.integrations.fastapi import create_router
from tests.test_runtime import make_runtime, submit


def api(runtime):
    app = FastAPI()

    def identity(request: Request):
        return Identity(user_id="alice", workspace_id=request.headers.get("X-Workspace", "acme"))

    app.include_router(create_router(runtime, identity))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_http_lifecycle_and_event_resume(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    async with api(runtime) as client:
        response = await client.post("/tasks", json={"agent": "test", "objective": "Work"})
        assert response.status_code == 201
        task_id = response.json()["id"]
        task = await runtime.run(task_id)
        snapshot = (await client.get(f"/tasks/{task_id}")).json()
        assert snapshot["pending"]["kind"] == "approval"
        reply = await client.post(
            f"/tasks/{task_id}/requests/{task.pending.id}",
            json={"value": True, "input_digest": task.pending.input_digest},
        )
        assert reply.status_code == 200
        await runtime.run(task_id)
        result = (await client.get(f"/tasks/{task_id}/result")).json()
        assert result["status"] == "succeeded"
        events = await client.get(f"/tasks/{task_id}/events")
        ids = [int(line[4:]) for line in events.text.splitlines() if line.startswith("id: ")]
        assert len(ids) > 2
        resumed = await client.get(
            f"/tasks/{task_id}/events", headers={"Last-Event-ID": str(ids[-2])}
        )
        assert f"id: {ids[-1]}\n" in resumed.text
        assert f"id: {ids[-2]}\n" not in resumed.text
        assert (
            await client.get(f"/tasks/{task_id}/events", headers={"Last-Event-ID": "bad"})
        ).status_code == 422


async def test_all_api_routes_enforce_workspace_ownership(tmp_path):
    runtime = make_runtime(tmp_path, approval=True)
    task = await runtime.run(submit(runtime).id)
    async with api(runtime) as client:
        headers = {"X-Workspace": "other"}
        for suffix in ["", "/result", "/events", "/artifacts/anything"]:
            response = await client.get(f"/tasks/{task.id}{suffix}", headers=headers)
            assert response.status_code == 404
        for suffix in ["/cancel", "/resume"]:
            response = await client.post(f"/tasks/{task.id}{suffix}", json={}, headers=headers)
            assert response.status_code == 404
        response = await client.post(
            f"/tasks/{task.id}/requests/{task.pending.id}", json={"value": True}, headers=headers
        )
        assert response.status_code == 404


async def test_invalid_input_and_stale_approval_return_useful_errors(tmp_path):
    runtime = make_runtime(
        tmp_path,
        steps=[
            AskDecision(question="Number?", input_schema={"type": "integer"}),
            FinishDecision(output={"done": True}),
        ],
    )
    task = await runtime.run(submit(runtime).id)
    async with api(runtime) as client:
        path = f"/tasks/{task.id}/requests/{task.pending.id}"
        assert (await client.post(path, json={"value": "bad"})).status_code == 422
        assert (await client.post(path, json={"value": 4})).status_code == 200
        assert (await client.post(path, json={"value": 5})).status_code == 409


async def test_redaction_and_artifact_ownership(tmp_path):
    runtime = make_runtime(tmp_path)
    task = submit(runtime)
    other = submit(runtime)
    artifact_id = runtime.store.put_artifact(task.id, {"secret": "hidden"})
    runtime.agents["test"].redact = lambda value: json.loads(
        json.dumps(value).replace("hidden", "[REDACTED]")
    )
    async with api(runtime) as client:
        response = await client.get(f"/tasks/{task.id}/artifacts/{artifact_id}")
        assert response.json() == {"secret": "[REDACTED]"}
        response = await client.get(f"/tasks/{other.id}/artifacts/{artifact_id}")
        assert response.status_code == 404
        response = await client.post(f"/tasks/{task.id}/cancel", json={})
        assert response.json()["status"] == "cancelled"


async def test_limits_are_server_owned(tmp_path):
    runtime = make_runtime(tmp_path)
    async with api(runtime) as client:
        response = await client.post(
            "/tasks", json={"agent": "test", "objective": "Work", "limits": {"model_calls": 100000}}
        )
        assert response.status_code == 422
