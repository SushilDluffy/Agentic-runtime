"""Application-owned functions and dependency injection."""

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .contracts import CheckResult, Identity, JsonValue, ModelUsage, Reconciliation, Task, digest


class TransientError(Exception):
    """A retryable model/read failure, or a write with an uncertain outcome."""


class ModelResponseError(TransientError):
    """A rejected model response that may still have billable, reported token usage."""

    def __init__(self, message: str, *, usage: ModelUsage | None = None):
        super().__init__(message)
        self.usage = usage


class ActionRejected(Exception):
    """The application rejected the action before creating any external effect."""


@dataclass(frozen=True)
class ActionContext:
    task_id: str
    identity: Identity
    operation_id: str
    dependencies: Any


@dataclass(frozen=True)
class Action:
    name: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    handler: Callable[[ActionContext, Any], Awaitable[BaseModel]]
    description: str = ""
    writes: bool = False
    approval: bool = False
    idempotent: bool = False
    reconcile: Callable[[ActionContext, Any], Awaitable[Reconciliation]] | None = None
    retries: int = 2
    version: str = "1"

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", self.name):
            raise ValueError("Action names must be short identifiers")
        if self.name.startswith("runtime_"):
            raise ValueError("runtime_ is reserved for runtime tools")
        if not inspect.iscoroutinefunction(self.handler):
            raise TypeError("Action handlers must be async to support bounded execution")
        if self.retries < 0 or self.retries > 10:
            raise ValueError("retries must be between 0 and 10")

    def signature(self) -> str:
        """Changing execution semantics invalidates pending operations and approvals."""
        return digest(
            {
                "version": self.version,
                "name": self.name,
                "input": self.input_type.model_json_schema(),
                "output": self.output_type.model_json_schema(),
                "writes": self.writes,
                "approval": self.approval,
                "idempotent": self.idempotent,
                "reconciliation": self.reconcile is not None,
                "retries": self.retries,
            }
        )

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_type.model_json_schema(),
        }


@dataclass
class Agent:
    name: str
    provider: Any
    actions: list[Action]
    checks: list[Callable[[ActionContext, JsonValue], Awaitable[CheckResult]]]
    instructions: str = "Complete the user's task using the available actions."
    version: str = "1"
    dependencies: Callable[[Identity], Any] = lambda identity: None
    context: Callable[[ActionContext], Awaitable[JsonValue]] | None = None
    authorize: Callable[[ActionContext, Action, BaseModel], Awaitable[bool]] | None = None
    can_respond: Callable[[Identity, Task], Awaitable[bool]] | None = None
    redact: Callable[[JsonValue], JsonValue] = lambda value: value
    output_type: type[BaseModel] | None = None
    _actions: dict[str, Action] = field(init=False, repr=False)

    def __post_init__(self):
        self._actions = {action.name: action for action in self.actions}
        if len(self._actions) != len(self.actions):
            raise ValueError("Action names must be unique")
        if not self.checks:
            raise ValueError("Register at least one application completion check")
        if not self.name or not self.version:
            raise ValueError("Agent name and version are required")
        if self.output_type is not None and (
            not isinstance(self.output_type, type) or not issubclass(self.output_type, BaseModel)
        ):
            raise TypeError("output_type must be a Pydantic model class")

    def action(self, name: str) -> Action:
        return self._actions[name]
