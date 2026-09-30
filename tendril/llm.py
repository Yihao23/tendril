"""Thin wrapper over any OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import re
from typing import Any, Protocol

from .config import Settings

# Reasoning models served by older runtimes (e.g. Qwen3 on Ollama < 0.9) put their chain of
# thought inline; keep it out of the event log and out of the history sent back to the model.
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


class ChatModel(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict]) -> Any:
        """Return an assistant message with `.content` and `.tool_calls`."""


class LLM:
    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI

        self.model = settings.model
        self.max_tokens = settings.max_tokens
        # The SDK retries timed-out calls from scratch, so a too-short timeout on slow hardware turns
        # one long reply into several wasted ones; the timeout has to fit the slowest expected reply.
        self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key or "not-needed",
                                  timeout=settings.timeout)

    async def chat(self, messages: list[dict], tools: list[dict]) -> Any:
        if not self.model:
            raise RuntimeError("no model configured: set [model].model in ~/.tendril/tendril.toml or TENDRIL_MODEL")
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        if msg.content and "<think>" in msg.content:
            msg.content = _THINK.sub("", msg.content).strip() or None
        return msg
