"""One wake-up of a goal: build context, let the model call tools, then decide when to wake next."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .config import Settings, format_interval
from .guard import Guard
from .llm import ChatModel
from .store import Store
from .tools.base import Registry, RunControl, ToolContext

log = logging.getLogger("tendril.agent")

SYSTEM_PROMPT = """\
You are Tendril, an autonomous agent that works on the user's goals in the background.
You are not in a live chat: each run is one "wake-up". The user may be away for hours.

How to work:
- Make concrete progress with your tools every wake-up. Don't just describe what you would do.
- Save anything you'll need later with `remember`; your context is rebuilt from memory and recent
  history each wake-up, so older details will fall out of view.
- Put longer deliverables (reports, drafts, code) in files with `write_file`, then `notify` the user
  with a short summary and the file name.
- Use `notify` sparingly: for results, blockers, or questions only the user can answer.
- Some tools need the user's approval. When a call is queued, don't retry it; continue with other
  work or `sleep`. The outcome will appear in your history after the user decides.
- End every wake-up by calling `sleep` (to continue later) or `finish` (when the goal is fully done).
  Recurring goals wake up on their schedule anyway; use `sleep` only to come back sooner.
"""

MAX_TOOL_RESULT = 12000
MAX_EVENT_CHARS = 4000
MAX_HISTORY_CHARS = 600
FAILURE_BACKOFF = 300
MAX_FAILURES = 5


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class Agent:
    def __init__(self, settings: Settings, store: Store, registry: Registry, guard: Guard, llm: ChatModel):
        self.settings = settings
        self.store = store
        self.registry = registry
        self.guard = guard
        self.llm = llm

    def workspace(self, goal_id: int) -> Path:
        path = self.settings.workspaces / str(goal_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- a single wake-up -------------------------------------------------------

    async def run(self, goal_id: int) -> None:
        goal = self.store.get_goal(goal_id)
        if not goal or goal["status"] != "active":  # paused/deleted while queued for a slot
            return
        run_no = goal["run_count"] + 1
        # next_run_at=None while running, so any wake request that arrives mid-run is detectable.
        self.store.update_goal(goal_id, status="running", next_run_at=None, run_count=run_no)
        self.store.add_event(goal_id, "run", f"Wake-up #{run_no}")
        ctx = ToolContext(goal=goal, store=self.store, settings=self.settings,
                          workspace=self.workspace(goal_id), control=RunControl())
        try:
            await self._loop(ctx)
        except Exception as e:
            log.exception("goal %s failed", goal_id)
            self._fail(goal_id, e)
            return
        self._finalize(goal_id, ctx.control)

    async def _loop(self, ctx: ToolContext) -> None:
        goal_id = ctx.goal["id"]
        messages: list[dict] = [
            {"role": "system", "content": "\n\n".join(filter(None, [SYSTEM_PROMPT, self.settings.extra_instructions]))},
            {"role": "user", "content": self.build_context(ctx.goal)},
        ]
        tools = self.registry.schemas()
        for _ in range(self.settings.max_steps):
            update = self._approval_updates(ctx)
            if update:
                messages.append({"role": "user", "content": update})
            msg = await self.llm.chat(messages, tools)
            entry: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.content:
                self.store.add_event(goal_id, "agent", msg.content)
            if not msg.tool_calls:
                messages.append(entry)
                return
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
            messages.append(entry)
            for tc in msg.tool_calls:
                result = await self.call_tool(ctx, tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": _clip(result, MAX_TOOL_RESULT)})
            if ctx.control.stop:
                return
        self.store.add_event(goal_id, "error", f"Step limit ({self.settings.max_steps}) reached for this wake-up")

    async def call_tool(self, ctx: ToolContext, name: str, raw_args: str | None) -> str:
        goal_id = ctx.goal["id"]
        tool = self.registry.get(name)
        if tool is None:
            return f"error: unknown tool {name!r}"
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return f"error: invalid arguments for {name}: {e}"

        self.store.add_event(goal_id, "tool_call", f"{name}({_clip(json.dumps(args, ensure_ascii=False), 300)})",
                             {"tool": name, "args": args})
        decision = self.guard.decide(tool)
        if decision == "deny":
            result = f"{name} is denied by the user's policy. Do not try it again."
        elif decision == "ask":
            duplicate = next((i for i, (t, a) in ctx.open_approvals.items() if t == name and a == args), None)
            if duplicate is not None:
                result = (f"This exact call is already waiting for approval as #{duplicate}. Do not request it "
                          "again; continue with other work or sleep.")
            else:
                approval_id = self.store.add_approval(goal_id, name, args)
                ctx.open_approvals[approval_id] = (name, args)
                self.store.notify(goal_id, f"Goal #{goal_id} wants to run {name} (approval #{approval_id})")
                result = (f"Queued for the user's approval as #{approval_id}. If they decide during this "
                          "wake-up you'll get the result here; otherwise it will be in your history next time. "
                          "Continue with other work or sleep.")
        else:
            result = await self._execute(ctx, name, args)
        self.store.add_event(goal_id, "tool_result", _clip(result, MAX_EVENT_CHARS))
        return result

    def _approval_updates(self, ctx: ToolContext) -> str | None:
        """Tell the model about approvals it requested this wake-up that the user has since decided."""
        lines = []
        for approval_id in list(ctx.open_approvals):
            approval = self.store.get_approval(approval_id)
            if not approval or approval["status"] in ("pending", "approved"):  # approved = still executing
                continue
            del ctx.open_approvals[approval_id]
            if approval["status"] == "denied":
                lines.append(f"The user denied #{approval_id} ({approval['tool']}). Don't request it again.")
            else:
                lines.append(f"The user approved #{approval_id} ({approval['tool']}). Result:\n"
                             f"{_clip(approval['result'] or '', MAX_TOOL_RESULT)}")
        return "[Approval updates]\n" + "\n\n".join(lines) if lines else None

    async def _execute(self, ctx: ToolContext, name: str, args: dict) -> str:
        tool = self.registry.get(name)
        if tool is None:
            return f"error: tool {name!r} is no longer available"
        try:
            return await tool.handler(ctx, args)
        except KeyError as e:
            return f"error: missing argument {e} for {name}"
        except Exception as e:
            return f"error: {name} failed: {e!r}"

    def _finalize(self, goal_id: int, control: RunControl) -> None:
        goal = self.store.get_goal(goal_id)
        if not goal:  # deleted mid-run
            return
        now = time.time()
        if control.finished:
            self.store.update_goal(goal_id, status="done", next_run_at=None, fail_count=0, last_error=None)
            return
        candidates = [t for t in (control.next_run_at,
                                  now + goal["interval_s"] if goal["interval_s"] else None,
                                  goal["next_run_at"]) if t is not None]  # goal.next_run_at = woken mid-run
        next_run = min(candidates) if candidates else None
        if goal["status"] == "paused":
            self.store.update_goal(goal_id, next_run_at=next_run, fail_count=0, last_error=None)
        else:
            self.store.update_goal(goal_id, status="active" if next_run else "idle", next_run_at=next_run,
                                   fail_count=0, last_error=None)

    def _fail(self, goal_id: int, error: Exception) -> None:
        goal = self.store.get_goal(goal_id)
        if not goal:
            return
        fails = goal["fail_count"] + 1
        self.store.add_event(goal_id, "error", f"Wake-up failed: {error!r}")
        if fails >= MAX_FAILURES:
            self.store.update_goal(goal_id, status="paused", fail_count=fails, last_error=repr(error))
            self.store.notify(goal_id, f"Goal #{goal_id} paused after {fails} failed wake-ups: {error!r}")
        else:
            status = "paused" if goal["status"] == "paused" else "active"
            self.store.update_goal(goal_id, status=status, fail_count=fails, last_error=repr(error),
                                   next_run_at=time.time() + FAILURE_BACKOFF * fails)

    # --- context ---------------------------------------------------------------

    def build_context(self, goal: dict) -> str:
        goal_id = goal["id"]
        now = datetime.now().astimezone()
        lines = [
            f"# Goal #{goal_id}",
            goal["text"],
            "",
            f"Current time: {now:%Y-%m-%d %H:%M %Z} ({now:%A}). This is wake-up #{goal['run_count'] + 1}.",
        ]
        if goal["interval_s"]:
            lines.append(f"This goal recurs every {format_interval(goal['interval_s'])}.")
        else:
            lines.append("This goal does not recur: if you neither sleep nor finish, you stay idle until the user writes.")

        shared, own = self.store.notes(None), self.store.notes(goal_id)
        if shared or own:
            lines += ["", "## Memory"]
            lines += [f"- (shared) {n['content']}" for n in shared]
            lines += [f"- {n['content']}" for n in own]

        pending = self.store.list_approvals(status="pending", goal_id=goal_id)
        if pending:
            lines += ["", "## Waiting for user approval"]
            lines += [f"- #{a['id']} {a['tool']} {_clip(a['args'], 200)}" for a in pending]

        files = sorted(p.name for p in self.workspace(goal_id).iterdir())[:50]
        if files:
            lines += ["", "## Workspace files", ", ".join(files)]

        events = [e for e in self.store.events(goal_id, limit=40)]
        if events:
            lines += ["", "## Recent history (oldest first)"]
            for e in events:
                stamp = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
                lines.append(f"[{stamp}] {e['kind']}: {_clip(e['content'], MAX_HISTORY_CHARS)}")
        return "\n".join(lines)

    # --- approvals -------------------------------------------------------------

    async def resolve_approval(self, approval_id: int, approve: bool) -> dict:
        approval = self.store.get_approval(approval_id)
        if not approval:
            raise KeyError(approval_id)
        if not self.store.claim_approval(approval_id, "approved" if approve else "denied"):
            raise ValueError(f"approval #{approval_id} is already {approval['status']}")
        goal_id = approval["goal_id"]
        goal = self.store.get_goal(goal_id)
        if approve and goal:
            ctx = ToolContext(goal=goal, store=self.store, settings=self.settings,
                              workspace=self.workspace(goal_id), control=RunControl())
            result = await self._execute(ctx, approval["tool"], json.loads(approval["args"]))
            self.store.finish_approval(approval_id, result)
            self.store.add_event(goal_id, "approval",
                                 f"User approved #{approval_id} ({approval['tool']}). Result:\n{_clip(result, MAX_EVENT_CHARS)}")
        elif goal:
            self.store.add_event(goal_id, "approval", f"User denied #{approval_id} ({approval['tool']}).")
        self.store.wake(goal_id)
        return self.store.get_approval(approval_id)
