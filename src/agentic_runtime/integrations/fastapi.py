"""Mount the router inside an authenticated host application."""

import asyncio
import json
from collections.abc import Callable

import jsonschema
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..contracts import TERMINAL, WAITING, Identity, Limits
from ..runtime import PermissionDenied, Runtime
from ..store import Conflict, NotFound


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    objective: str = Field(min_length=1, max_length=16000)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: JsonValue
    input_digest: str | None = None


def create_router(
    runtime: Runtime,
    identity_dependency: Callable,
    *,
    prefix="/tasks",
    limits: Limits | None = None,
) -> APIRouter:
    """The host supplies trusted Identity and server-owned resource limits."""
    router = APIRouter(prefix=prefix)

    def public(value, agent_name):
        data = value.model_dump(mode="json", by_alias=True)
        return runtime.agents[agent_name].redact(data)

    def owned(task_id, identity):
        try:
            return runtime.get(task_id, identity)
        except (NotFound, PermissionDenied):
            raise HTTPException(404, "Task not found") from None

    @router.post("", status_code=201)
    async def submit(body: SubmitBody, identity: Identity = Depends(identity_dependency)):
        if body.agent not in runtime.agents:
            raise HTTPException(404, "Agent not found")
        task = runtime.submit(body.agent, body.objective, identity, limits=limits)
        return public(task, task.agent)

    @router.get("/{task_id}")
    async def status(task_id: str, identity: Identity = Depends(identity_dependency)):
        task = owned(task_id, identity)
        return public(task, task.agent)

    @router.get("/{task_id}/result")
    async def result(task_id: str, identity: Identity = Depends(identity_dependency)):
        task = owned(task_id, identity)
        return public(runtime.result(task_id, identity), task.agent)

    @router.post("/{task_id}/cancel")
    async def cancel(task_id: str, identity: Identity = Depends(identity_dependency)):
        task = owned(task_id, identity)
        return public(runtime.cancel(task_id, identity), task.agent)

    @router.post("/{task_id}/resume")
    async def resume(task_id: str, identity: Identity = Depends(identity_dependency)):
        task = owned(task_id, identity)
        try:
            resumed = await runtime.resume(task_id, identity)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        return public(resumed, task.agent)

    @router.post("/{task_id}/requests/{request_id}")
    async def respond(
        task_id: str,
        request_id: str,
        body: ReplyBody,
        identity: Identity = Depends(identity_dependency),
    ):
        task = owned(task_id, identity)
        try:
            updated = await runtime.respond(
                task_id, request_id, identity, body.value, input_digest=body.input_digest
            )
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionDenied:
            raise HTTPException(403, "Response permission denied") from None
        except (ValueError, jsonschema.ValidationError):
            raise HTTPException(422, "Reply does not satisfy the request schema") from None
        return public(updated, task.agent)

    @router.get("/{task_id}/artifacts/{artifact_id}")
    async def artifact(
        task_id: str, artifact_id: str, identity: Identity = Depends(identity_dependency)
    ):
        task = owned(task_id, identity)
        try:
            data = json.loads(runtime.store.artifact(task_id, artifact_id))
        except NotFound:
            raise HTTPException(404, "Artifact not found or retention expired") from None
        return runtime.agents[task.agent].redact(data)

    @router.get("/{task_id}/events")
    async def events(
        task_id: str,
        request: Request,
        after: int = 0,
        last_event_id: str | None = Header(default=None),
        identity: Identity = Depends(identity_dependency),
    ):
        task = owned(task_id, identity)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Event cursor must be a nonnegative integer") from None

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = runtime.store.events(task_id, after=cursor)
                for event in batch:
                    cursor = event.id
                    data = public(event, task.agent)
                    yield f"id: {event.id}\nevent: progress\ndata: {json.dumps(data)}\n\n"
                current = runtime.get(task_id, identity)
                if not batch and (
                    current.status in TERMINAL
                    or current.status in WAITING
                    or current.status == "blocked"
                ):
                    yield "event: idle\ndata: {}\n\n"
                    return
                if not batch:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
