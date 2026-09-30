"""Settings: ~/.tendril/tendril.toml, overridable by environment variables."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_TEMPLATE = """\
# Tendril config.
# Env vars TENDRIL_MODEL / TENDRIL_BASE_URL / TENDRIL_API_KEY override [model].

[model]
# Any OpenAI-compatible endpoint. Examples:
#   Ollama:     base_url = "http://localhost:11434/v1"   model = "qwen3:8b"
#   DeepSeek:   base_url = "https://api.deepseek.com/v1" model = "deepseek-chat"
#   OpenRouter: base_url = "https://openrouter.ai/api/v1"
#   OpenAI:     base_url = ""  (empty = official endpoint)
base_url = "http://localhost:11434/v1"
model = ""
api_key = ""
timeout = 600           # seconds per model call; raise it for slow local (CPU) inference
# max_tokens = 4096     # cap each reply, so a reasoning model can't think forever
# extra_instructions = "/no_think"   # appended to the system prompt (this one turns off Qwen3 thinking)

[agent]
max_steps = 12        # tool-calling rounds per wake-up
max_concurrent = 2    # goals allowed to run at the same time
tick_seconds = 15     # how often the scheduler checks for due goals

[shell]
sandbox = "none"      # "none": run in the goal's workspace dir; "docker": run in a throwaway container
image = "python:3.12-slim"

# Permission layer. Values: allow | ask | deny. Keys are tool names; glob patterns work.
# "ask" queues the call until you approve it in the dashboard or with `tendril approve`.
[policy]
run_shell = "ask"
# "github__*" = "ask"
# web_search = "allow"

# MCP servers. Each server's tools appear as <server>__<tool>.
# Tools that declare readOnlyHint default to "allow", everything else to "ask".
# [mcp.servers.filesystem]
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
#
# [mcp.servers.remote]
# url = "https://example.com/mcp"
"""

POLICY_VALUES = {"allow", "ask", "deny"}


@dataclass
class Settings:
    home: Path
    model: str = ""
    base_url: str | None = None
    api_key: str = ""
    timeout: float = 600.0
    max_tokens: int | None = None
    extra_instructions: str = ""
    max_steps: int = 12
    max_concurrent: int = 2
    tick_seconds: float = 15.0
    shell_sandbox: str = "none"
    shell_image: str = "python:3.12-slim"
    policy: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, dict] = field(default_factory=dict)

    @property
    def config_path(self) -> Path:
        return self.home / "tendril.toml"

    @property
    def db_path(self) -> Path:
        return self.home / "tendril.db"

    @property
    def workspaces(self) -> Path:
        return self.home / "workspaces"


def default_home() -> Path:
    return Path(os.environ.get("TENDRIL_HOME", "~/.tendril")).expanduser()


def load_settings(home: Path | None = None) -> Settings:
    home = home or default_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "tendril.toml"
    cfg = tomllib.loads(path.read_text()) if path.exists() else {}

    model = cfg.get("model", {})
    agent = cfg.get("agent", {})
    shell = cfg.get("shell", {})
    policy = {str(k): str(v) for k, v in cfg.get("policy", {}).items()}
    bad = {k: v for k, v in policy.items() if v not in POLICY_VALUES}
    if bad:
        raise ValueError(f"invalid [policy] values in {path}: {bad} (use allow/ask/deny)")

    base_url = os.environ.get("TENDRIL_BASE_URL", model.get("base_url", "")) or None
    api_key = (
        os.environ.get("TENDRIL_API_KEY")
        or model.get("api_key")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    return Settings(
        home=home,
        model=os.environ.get("TENDRIL_MODEL", model.get("model", "")),
        base_url=base_url,
        api_key=api_key,
        timeout=float(model.get("timeout", 600)),
        max_tokens=int(model["max_tokens"]) if model.get("max_tokens") else None,
        extra_instructions=model.get("extra_instructions", ""),
        max_steps=int(agent.get("max_steps", 12)),
        max_concurrent=int(agent.get("max_concurrent", 2)),
        tick_seconds=float(agent.get("tick_seconds", 15)),
        shell_sandbox=shell.get("sandbox", "none"),
        shell_image=shell.get("image", "python:3.12-slim"),
        policy=policy,
        mcp_servers=cfg.get("mcp", {}).get("servers", {}),
    )


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_interval(text: str | None) -> int | None:
    """'90s', '30m', '2h', '1d', '1w' -> seconds. None/'' -> None."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", text.lower())
    if not m:
        raise ValueError(f"bad interval {text!r}; use e.g. 30m, 2h, 1d")
    seconds = int(float(m.group(1)) * _UNITS[m.group(2)])
    if seconds < 60:
        raise ValueError("interval must be at least 1 minute")
    return seconds


def format_interval(seconds: int | None) -> str:
    if not seconds:
        return ""
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
