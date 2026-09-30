import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tendril.agent import Agent
from tendril.config import Settings, parse_interval
from tendril.guard import Guard
from tendril.store import Store
from tendril.tools import Registry, Tool, register_builtin


def call(name, **args):
    return SimpleNamespace(id=f"call_{name}_{time.time_ns()}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


class ScriptedLLM:
    """Replays a fixed list of assistant messages and records what it was sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools):
        self.seen.append(messages)
        return self.turns.pop(0)


@pytest.fixture
def env(tmp_path):
    settings = Settings(home=tmp_path, model="fake", policy={"run_shell": "ask"})
    store = Store(tmp_path / "t.db")
    registry = Registry()
    register_builtin(registry)

    def make(llm, policy=None):
        return Agent(settings, store, registry, Guard(policy or settings.policy), llm)

    return SimpleNamespace(settings=settings, store=store, registry=registry, make=make)


def test_run_writes_file_and_sleeps(env):
    llm = ScriptedLLM(
        reply("Drafting the report.", call("write_file", path="report.md", content="# hi")),
        reply(None, call("remember", note="report drafted"), call("sleep", minutes=30)),
    )
    agent = env.make(llm)
    gid = env.store.create_goal("write a report")
    asyncio.run(agent.run(gid))

    goal = env.store.get_goal(gid)
    assert (env.settings.workspaces / str(gid) / "report.md").read_text() == "# hi"
    assert goal["status"] == "active" and goal["run_count"] == 1
    assert 29 * 60 < goal["next_run_at"] - time.time() <= 30 * 60
    assert [n["content"] for n in env.store.notes(gid)] == ["report drafted"]

    # The next wake-up sees memory and history in its context.
    llm2 = ScriptedLLM(reply(None, call("finish", summary="done")))
    env.store.update_goal(gid, next_run_at=time.time())
    asyncio.run(env.make(llm2).run(gid))
    context = llm2.seen[0][1]["content"]
    assert "report drafted" in context and "write_file" in context and "report.md" in context
    assert env.store.get_goal(gid)["status"] == "done"
    assert "finished: done" in env.store.notifications()[0]["message"]


def test_ask_policy_queues_until_approved(env):
    llm = ScriptedLLM(reply(None, call("run_shell", command="echo hello > out.txt"), call("sleep", minutes=60)))
    agent = env.make(llm)
    gid = env.store.create_goal("run a command")
    asyncio.run(agent.run(gid))

    workspace = env.settings.workspaces / str(gid)
    assert not (workspace / "out.txt").exists()
    [pending] = env.store.list_approvals(status="pending")
    assert pending["tool"] == "run_shell"

    result = asyncio.run(agent.resolve_approval(pending["id"], approve=True))
    assert result["status"] == "executed" and "exit code 0" in result["result"]
    assert (workspace / "out.txt").read_text().strip() == "hello"
    # Approval wakes the goal right away instead of waiting out its 60-minute sleep.
    assert env.store.get_goal(gid)["next_run_at"] <= time.time()
    with pytest.raises(ValueError):
        asyncio.run(agent.resolve_approval(pending["id"], approve=True))


def test_deny_policy_and_glob(env):
    async def noop(ctx, args):
        raise AssertionError("must not run")

    env.registry.add(Tool("github__delete_repo", "", {"type": "object", "properties": {}}, noop))
    llm = ScriptedLLM(reply(None, call("github__delete_repo"), call("sleep", minutes=5)))
    agent = env.make(llm, policy={"github__*": "deny"})
    gid = env.store.create_goal("x")
    asyncio.run(agent.run(gid))
    results = [e["content"] for e in env.store.events(gid) if e["kind"] == "tool_result"]
    assert "denied" in results[0]


def test_workspace_escape_is_blocked(env):
    llm = ScriptedLLM(reply(None, call("write_file", path="../../evil.txt", content="x"), call("sleep", minutes=5)))
    gid = env.store.create_goal("x")
    asyncio.run(env.make(llm).run(gid))
    results = [e["content"] for e in env.store.events(gid) if e["kind"] == "tool_result"]
    assert "outside the goal workspace" in results[0]
    assert not (env.settings.home / "evil.txt").exists()


def test_idle_without_schedule_and_wake_on_message(env):
    gid = env.store.create_goal("chat")
    asyncio.run(env.make(ScriptedLLM(reply("Hello!"))).run(gid))
    assert env.store.get_goal(gid)["status"] == "idle"
    assert env.store.wake(gid)
    assert env.store.get_goal(gid)["status"] == "active"


def test_recurring_goal_uses_interval(env):
    gid = env.store.create_goal("daily digest", interval_s=86400)
    asyncio.run(env.make(ScriptedLLM(reply("nothing new"))).run(gid))
    goal = env.store.get_goal(gid)
    assert goal["status"] == "active" and goal["next_run_at"] - time.time() > 86000


def test_failures_back_off_then_pause(env):
    class Broken:
        async def chat(self, messages, tools):
            raise RuntimeError("model down")

    gid = env.store.create_goal("x")
    agent = env.make(Broken())
    for _ in range(5):
        env.store.update_goal(gid, next_run_at=time.time())
        asyncio.run(agent.run(gid))
    goal = env.store.get_goal(gid)
    assert goal["status"] == "paused" and "model down" in goal["last_error"]


def test_parse_interval():
    assert parse_interval("30m") == 1800
    assert parse_interval("1d") == 86400
    assert parse_interval(None) is None
    with pytest.raises(ValueError):
        parse_interval("5s")
    with pytest.raises(ValueError):
        parse_interval("soon")


def test_html_to_text_keeps_links():
    from tendril.tools.builtin import html_to_text

    src = '<p><a href="/item?id=1">Great story</a> <a href="#top">top</a> <a href="https://x.dev">x</a></p><script>no</script>'
    assert html_to_text(src, "https://news.example.com/") == \
        "[Great story](https://news.example.com/item?id=1) top [x](https://x.dev)"
    assert html_to_text(src) == "Great story top x"


def test_model_sees_approval_result_within_the_same_wake_up(env):
    class ApprovesMidRun(ScriptedLLM):
        async def chat(self, messages, tools):
            if len(self.seen) == 1:  # user approves while the model is still working
                [pending] = env.store.list_approvals(status="pending")
                await agent.resolve_approval(pending["id"], approve=True)
            return await super().chat(messages, tools)

    llm = ApprovesMidRun(
        reply(None, call("run_shell", command="echo 42")),
        reply(None, call("run_shell", command="echo 42")),  # impatient retry of the exact same call
        reply(None, call("finish", summary="got it")),
    )
    agent = env.make(llm)
    gid = env.store.create_goal("x")
    asyncio.run(agent.run(gid))

    assert len(env.store.list_approvals()) == 1  # the retry did not create a second approval
    update = next(m for m in llm.seen[2] if m["role"] == "user" and m["content"].startswith("[Approval updates]"))
    assert "42" in update["content"]


def test_exact_duplicate_ask_is_not_queued_twice(env):
    llm = ScriptedLLM(
        reply(None, call("run_shell", command="ls"), call("run_shell", command="ls"), call("sleep", minutes=5)),
    )
    gid = env.store.create_goal("x")
    asyncio.run(env.make(llm).run(gid))
    assert len(env.store.list_approvals()) == 1
    results = [e["content"] for e in env.store.events(gid) if e["kind"] == "tool_result"]
    assert "already waiting for approval as #1" in results[1]
