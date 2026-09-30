"""HTTP API + web dashboard. Also hosts the scheduler, so `tendril serve` is the whole agent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from importlib.resources import files

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .agent import Agent
from .config import Settings, load_settings, parse_interval
from .guard import Guard
from .llm import LLM, ChatModel
from .scheduler import Scheduler
from .store import Store
from .tools import Registry, register_builtin
from .tools.mcp import MCPManager

log = logging.getLogger("tendril")


class GoalIn(BaseModel):
    text: str
    every: str | None = None


class MessageIn(BaseModel):
    text: str


class DecisionIn(BaseModel):
    approve: bool


def create_app(settings: Settings | None = None, llm: ChatModel | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    registry = Registry()
    register_builtin(registry)
    guard = Guard(settings.policy)
    agent = Agent(settings, store, registry, guard, llm or LLM(settings))
    scheduler = Scheduler(store, agent, settings.tick_seconds, settings.max_concurrent)
    mcp = MCPManager(settings.mcp_servers)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await mcp.start(registry)
        task = asyncio.create_task(scheduler.run_forever())
        log.info("Tendril running; model=%s base_url=%s", settings.model or "(unset)", settings.base_url or "default")
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await mcp.close()

    app = FastAPI(title="Tendril", lifespan=lifespan)
    app.state.store, app.state.agent, app.state.scheduler = store, agent, scheduler

    def goal_or_404(goal_id: int) -> dict:
        goal = store.get_goal(goal_id)
        if not goal:
            raise HTTPException(404, f"goal #{goal_id} not found")
        return goal

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (files("tendril") / "web" / "index.html").read_text()

    @app.get("/logo.svg")
    def logo() -> Response:
        return Response((files("tendril") / "web" / "logo.svg").read_bytes(), media_type="image/svg+xml")

    @app.get("/api/status")
    def status() -> dict:
        return {"model": settings.model, "base_url": settings.base_url, "home": str(settings.home),
                "mcp_errors": mcp.errors}

    @app.get("/api/goals")
    def list_goals() -> list[dict]:
        return store.list_goals()

    @app.post("/api/goals")
    def create_goal(body: GoalIn) -> dict:
        if not body.text.strip():
            raise HTTPException(400, "goal text is empty")
        try:
            interval = parse_interval(body.every)
        except ValueError as e:
            raise HTTPException(400, str(e))
        goal_id = store.create_goal(body.text.strip(), interval)
        store.add_event(goal_id, "user", body.text.strip())
        scheduler.poke()
        return store.get_goal(goal_id)

    @app.get("/api/goals/{goal_id}")
    def get_goal(goal_id: int) -> dict:
        return {
            "goal": goal_or_404(goal_id),
            "events": store.events(goal_id),
            "approvals": store.list_approvals(goal_id=goal_id),
            "notes": store.notes(goal_id),
            "workspace": str(agent.workspace(goal_id)),
        }

    @app.post("/api/goals/{goal_id}/message")
    def message(goal_id: int, body: MessageIn) -> dict:
        goal_or_404(goal_id)
        store.add_event(goal_id, "user", body.text)
        store.wake(goal_id, reopen=True)
        scheduler.poke()
        return store.get_goal(goal_id)

    @app.post("/api/goals/{goal_id}/run")
    def run_now(goal_id: int) -> dict:
        goal_or_404(goal_id)
        if not store.wake(goal_id, reopen=True):
            raise HTTPException(409, "goal is paused; resume it first")
        scheduler.poke()
        return store.get_goal(goal_id)

    @app.post("/api/goals/{goal_id}/pause")
    def pause(goal_id: int) -> dict:
        goal_or_404(goal_id)
        store.update_goal(goal_id, status="paused")
        return store.get_goal(goal_id)

    @app.post("/api/goals/{goal_id}/resume")
    def resume(goal_id: int) -> dict:
        goal = goal_or_404(goal_id)
        if goal["status"] == "paused":
            store.update_goal(goal_id, status="active", fail_count=0)
            store.wake(goal_id)
            scheduler.poke()
        return store.get_goal(goal_id)

    @app.delete("/api/goals/{goal_id}")
    def delete(goal_id: int) -> dict:
        goal_or_404(goal_id)
        store.delete_goal(goal_id)
        return {"deleted": goal_id}

    @app.get("/api/approvals")
    def approvals(status: str | None = "pending") -> list[dict]:
        return store.list_approvals(status=status or None)

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: int, body: DecisionIn) -> dict:
        try:
            result = await agent.resolve_approval(approval_id, body.approve)
        except KeyError:
            raise HTTPException(404, f"approval #{approval_id} not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        scheduler.poke()
        return result

    @app.get("/api/notifications")
    def notifications(unread: bool = False) -> list[dict]:
        return store.notifications(unread_only=unread)

    @app.post("/api/notifications/read")
    def mark_read() -> dict:
        store.mark_read()
        return {"ok": True}

    @app.get("/api/tools")
    def tools() -> list[dict]:
        return [{"name": t.name, "description": t.description, "source": t.source,
                 "policy": guard.decide(t)} for t in registry.tools.values()]

    return app
