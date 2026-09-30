"""Tool registry and the context a tool receives when it runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from ..config import Settings
    from ..store import Store


@dataclass
class RunControl:
    """Flags that control tools (sleep/finish) set to end the current wake-up."""

    stop: bool = False
    finished: bool = False
    next_run_at: float | None = None


@dataclass
class ToolContext:
    goal: dict
    store: "Store"
    settings: "Settings"
    workspace: Path
    control: RunControl = field(default_factory=RunControl)
    # Approvals requested during this wake-up that the model hasn't seen resolved yet: id -> (tool, args).
    open_approvals: dict[int, tuple[str, dict]] = field(default_factory=dict)


Handler = Callable[[ToolContext, dict], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    default_policy: str = "allow"  # allow | ask | deny
    control: bool = False  # control tools (sleep, finish, ...) bypass the permission layer
    source: str = "builtin"

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


class Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]


def params(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}
