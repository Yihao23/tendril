import time

from fastapi.testclient import TestClient

from tendril.config import Settings
from tendril.server import create_app

from .test_agent import ScriptedLLM, call, reply


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_with_approval(tmp_path):
    llm = ScriptedLLM(
        reply("On it.", call("run_shell", command="echo hi"), call("sleep", minutes=60)),
        reply(None, call("notify", message="shell said hi"), call("finish", summary="ok")),
    )
    settings = Settings(home=tmp_path, model="fake", tick_seconds=0.1, policy={"run_shell": "ask"})
    with TestClient(create_app(settings, llm)) as client:
        gid = client.post("/api/goals", json={"text": "say hi"}).json()["id"]
        assert wait_for(lambda: client.get("/api/approvals").json())
        [approval] = client.get("/api/approvals").json()

        decided = client.post(f"/api/approvals/{approval['id']}", json={"approve": True}).json()
        assert "hi" in decided["result"]

        assert wait_for(lambda: client.get(f"/api/goals/{gid}").json()["goal"]["status"] == "done")
        messages = [n["message"] for n in client.get("/api/notifications").json()]
        assert "shell said hi" in messages
        assert client.get("/").status_code == 200
        assert {"name": "run_shell", "policy": "ask"}.items() <= next(
            t for t in client.get("/api/tools").json() if t["name"] == "run_shell").items()


def test_rejects_bad_interval(tmp_path):
    settings = Settings(home=tmp_path, model="fake", tick_seconds=0.1)
    with TestClient(create_app(settings, ScriptedLLM())) as client:
        assert client.post("/api/goals", json={"text": "x", "every": "soon"}).status_code == 400


def test_logo_is_served(tmp_path):
    settings = Settings(home=tmp_path, model="fake", tick_seconds=0.1)
    with TestClient(create_app(settings, ScriptedLLM())) as client:
        resp = client.get("/logo.svg")
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("image/svg+xml")
        assert 'href="/logo.svg"' in client.get("/").text
