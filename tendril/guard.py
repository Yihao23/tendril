"""Permission layer: decides whether a tool call runs, waits for the user, or is refused."""

from __future__ import annotations

from fnmatch import fnmatchcase

from .tools.base import Tool


class Guard:
    def __init__(self, policy: dict[str, str]):
        self.policy = policy

    def decide(self, tool: Tool) -> str:
        if tool.control:
            return "allow"
        if tool.name in self.policy:
            return self.policy[tool.name]
        for pattern, decision in self.policy.items():
            if fnmatchcase(tool.name, pattern):
                return decision
        return tool.default_policy
