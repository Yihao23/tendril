"""`tendril` command line. Everything except `init` and `serve` talks to the running server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

from .config import CONFIG_TEMPLATE, default_home, format_interval

DEFAULT_PORT = 7788


def _url() -> str:
    return os.environ.get("TENDRIL_URL", f"http://127.0.0.1:{DEFAULT_PORT}")


def api(method: str, path: str, body: dict | None = None):
    url = _url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except ValueError:
            detail = e.reason
        sys.exit(f"error: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach Tendril at {_url()} ({e.reason}). Start it with `tendril serve`.")


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


def _goal_line(g: dict) -> str:
    every = format_interval(g["interval_s"]) or "once"
    flag = f"  [{g['pending_approvals']} approval(s)]" if g.get("pending_approvals") else ""
    text = g["text"].splitlines()[0][:60]
    return f"#{g['id']:<4} {g['status']:<8} {every:<5} next {_when(g['next_run_at']):<11} {text}{flag}"


def cmd_init(args) -> None:
    home = default_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "tendril.toml"
    if path.exists():
        print(f"{path} already exists")
    else:
        path.write_text(CONFIG_TEMPLATE)
        print(f"wrote {path}; set [model] and run `tendril serve`")


def cmd_serve(args) -> None:
    import logging

    import uvicorn

    from .server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.host not in ("127.0.0.1", "localhost"):
        print("warning: the API has no authentication; only expose it on a trusted network", file=sys.stderr)
    uvicorn.run(create_app(), host=args.host, port=args.port)


def cmd_add(args) -> None:
    g = api("POST", "/api/goals", {"text": " ".join(args.text), "every": args.every})
    print(_goal_line(g))


def cmd_ls(args) -> None:
    goals = api("GET", "/api/goals")
    print("\n".join(_goal_line(g) for g in goals) or "no goals yet: tendril add \"...\"")


def cmd_show(args) -> None:
    data = api("GET", f"/api/goals/{args.id}")
    g = data["goal"]
    print(_goal_line(g))
    print(f"workspace: {data['workspace']}")
    if g["last_error"]:
        print(f"last error: {g['last_error']}")
    print()
    for e in data["events"][-args.n:]:
        content = e["content"] if args.full else e["content"][:300]
        print(f"[{_when(e['ts'])}] {e['kind']}: {content}")


def cmd_say(args) -> None:
    print(_goal_line(api("POST", f"/api/goals/{args.id}/message", {"text": " ".join(args.text)})))


def cmd_simple(action: str):
    def run(args) -> None:
        if action == "rm":
            api("DELETE", f"/api/goals/{args.id}")
            print(f"deleted #{args.id}")
        else:
            print(_goal_line(api("POST", f"/api/goals/{args.id}/{action}")))
    return run


def cmd_approvals(args) -> None:
    rows = api("GET", "/api/approvals?status=pending")
    for a in rows:
        print(f"#{a['id']:<4} goal #{a['goal_id']:<4} {a['tool']} {a['args']}")
    if not rows:
        print("nothing waiting for approval")


def cmd_decide(approve: bool):
    def run(args) -> None:
        a = api("POST", f"/api/approvals/{args.id}", {"approve": approve})
        print(f"#{a['id']} {a['status']}")
        if a.get("result"):
            print(a["result"])
    return run


def cmd_inbox(args) -> None:
    rows = api("GET", "/api/notifications" + ("" if args.all else "?unread=true"))
    for n in reversed(rows):
        goal = f"goal #{n['goal_id']}" if n["goal_id"] else ""
        print(f"[{_when(n['ts'])}] {goal}: {n['message']}")
    if not rows:
        print("inbox is empty")
    api("POST", "/api/notifications/read")


def cmd_tools(args) -> None:
    for t in api("GET", "/api/tools"):
        print(f"{t['policy']:<6} {t['name']:<32} {t['source']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tendril", description="An always-on background AI agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write a starter ~/.tendril/tendril.toml").set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the agent, scheduler and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("add", help="give Tendril a new goal")
    s.add_argument("text", nargs="+")
    s.add_argument("--every", help="recur, e.g. 30m, 6h, 1d")
    s.set_defaults(fn=cmd_add)

    sub.add_parser("ls", help="list goals").set_defaults(fn=cmd_ls)

    s = sub.add_parser("show", help="show a goal's activity log")
    s.add_argument("id", type=int)
    s.add_argument("-n", type=int, default=30, help="number of events")
    s.add_argument("--full", action="store_true", help="don't truncate events")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("say", help="send a message to a goal (wakes it up)")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_say)

    for action, help_text in (("run", "wake a goal now"), ("pause", "pause a goal"),
                              ("resume", "resume a paused goal"), ("rm", "delete a goal")):
        s = sub.add_parser(action, help=help_text)
        s.add_argument("id", type=int)
        s.set_defaults(fn=cmd_simple(action))

    sub.add_parser("approvals", help="list tool calls waiting for approval").set_defaults(fn=cmd_approvals)
    for name, approve in (("approve", True), ("deny", False)):
        s = sub.add_parser(name, help=f"{name} a pending tool call")
        s.add_argument("id", type=int)
        s.set_defaults(fn=cmd_decide(approve))

    s = sub.add_parser("inbox", help="read messages from your goals")
    s.add_argument("--all", action="store_true", help="include already-read messages")
    s.set_defaults(fn=cmd_inbox)

    sub.add_parser("tools", help="list tools and their permission policy").set_defaults(fn=cmd_tools)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
