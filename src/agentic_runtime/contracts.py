"""Persisted public contracts. All timestamps are UTC epoch seconds."""

import hashlib
import json
import time
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


def new_id() -> str:
    return uuid4().hex


def digest(value: JsonValue) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
WAITING = {TaskStatus.WAITING_INPUT, TaskStatus.WAITING_APPROVAL}


class Identity(Contract):
    user_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class Limits(Contract):
    model_calls: int = Field(default=30, ge=1)
    tool_calls: int = Field(default=50, ge=1)
    call_timeout: float = Field(default=30, gt=0)
    active_seconds: float = Field(default=300, gt=0)
    wait_seconds: float = Field(default=86400, gt=0)
    context_chars: int = Field(default=24000, ge=2048)
    inline_chars: int = Field(default=4000, ge=256)
    artifact_bytes: int = Field(default=2_000_000, ge=1024)
    model_retries: int = Field(default=2, ge=0, le=10)
    backoff_seconds: float = Field(default=0.1, ge=0, le=60)


class Usage(Contract):
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    active_seconds: float = 0


class ActionDecision(Contract):
    kind: Literal["action"] = "action"
    action: str
    arguments: dict[str, JsonValue]


class AskDecision(Contract):
    kind: Literal["ask"] = "ask"
    question: str = Field(min_length=1, max_length=4000)
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})


class FinishDecision(Contract):
    kind: Literal["finish"] = "finish"
    output: JsonValue


class BlockDecision(Contract):
    kind: Literal["block"] = "block"
    reason: str = Field(min_length=1, max_length=4000)


class RetrieveDecision(Contract):
    kind: Literal["retrieve"] = "retrieve"
    artifact_id: str
    offset: int = Field(default=0, ge=0)


Decision = Annotated[
    ActionDecision | AskDecision | FinishDecision | BlockDecision | RetrieveDecision,
    Field(discriminator="kind"),
]
DECISION_ADAPTER = TypeAdapter(Decision)


class ModelReply(Contract):
    decision: Decision
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Operation(Contract):
    id: str = Field(default_factory=new_id)
    action: str
    arguments: dict[str, JsonValue]
    input_digest: str
    action_version: str
    writes: bool
    state: Literal["prepared", "dispatched", "applied", "failed", "denied", "unknown"] = "prepared"
    attempts: int = 0
    approved_digest: str | None = None
    result: JsonValue = None
    error: str | None = None
    recorded_at: float = Field(default_factory=time.time)


class PendingRequest(Contract):
    id: str = Field(default_factory=new_id)
    kind: Literal["input", "approval"]
    prompt: str
    schema_: dict = Field(default_factory=lambda: {"type": "object"}, alias="schema")
    operation_id: str | None = None
    input_digest: str | None = None
    expires_at: float


class CheckResult(Contract):
    name: str
    passed: bool
    detail: str
    evidence: JsonValue = None
    checked_at: float = Field(default_factory=time.time)


class Task(Contract):
    id: str = Field(default_factory=new_id)
    agent: str
    agent_version: str
    identity: Identity
    objective: str = Field(min_length=1, max_length=16000)
    status: TaskStatus = TaskStatus.QUEUED
    revision: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    limits: Limits = Field(default_factory=Limits)
    usage: Usage = Field(default_factory=Usage)
    messages: list[dict[str, JsonValue]] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    pending: PendingRequest | None = None
    replies: dict[str, JsonValue] = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    proposal: JsonValue = None
    proposal_artifact_id: str | None = None
    reason: str | None = None
    cancel_requested: bool = False
    step_started_at: float | None = None
    decisions: int = 0


class Event(Contract):
    id: int
    task_id: str
    kind: str
    data: JsonValue
    created_at: float


class Reconciliation(Contract):
    outcome: Literal["applied", "not_applied", "unknown"]
    result: JsonValue = None
    detail: str = ""


class TaskResult(Contract):
    task_id: str
    status: TaskStatus
    output: JsonValue
    checks: list[CheckResult]
    operations: list[Operation]
    usage: Usage
    review_items: list[str]
