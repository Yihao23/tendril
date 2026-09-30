"""Built-in tools: web, files, shell, memory, and the control tools that end a wake-up."""

from __future__ import annotations

import asyncio
import importlib.util
import html
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .base import Registry, Tool, ToolContext, params

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_OUTPUT = 12000
MAX_FETCH = MAX_OUTPUT  # small models ask for huge pages; the agent clips tool results to this anyway


# --- helpers -----------------------------------------------------------------


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer",
             "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table"}

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.base_url = base_url
        self.link: tuple[str, int] | None = None  # (href, index in parts where the anchor text starts)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self.base_url and not self.skip:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.link = (urljoin(self.base_url, href), len(self.parts))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self.link:
            href, start = self.link
            self.link = None
            text = "".join(self.parts[start:]).strip()
            if text:  # render as a markdown link so the model can cite real URLs
                self.parts[start:] = [f"[{text}]({href})"]

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(source: str, base_url: str | None = None) -> str:
    """Readable text from HTML; with base_url, links are kept as [text](absolute-url)."""
    parser = _TextExtractor(base_url)
    parser.feed(source)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated, {len(text) - limit} more chars]"


def _resolve(ctx: ToolContext, path: str) -> Path:
    root = ctx.workspace.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path {path!r} is outside the goal workspace")
    return target


# --- web ---------------------------------------------------------------------


async def web_search(ctx: ToolContext, args: dict) -> str:
    query, limit = args["query"], int(args.get("max_results", 5))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": BROWSER_UA}) as client:
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</(?:a|div)>',
        re.S,
    )
    results = []
    for href, title, snippet in pattern.findall(resp.text):
        href = html.unescape(href)
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query)["uddg"][0]
        results.append(f"- {_strip_tags(title)}\n  {href}\n  {_strip_tags(snippet)}")
        if len(results) >= limit:
            break
    return "\n".join(results) or "No results (the search backend may be rate limiting; try web_fetch on a known URL)."


async def web_fetch(ctx: ToolContext, args: dict) -> str:
    url, max_chars = args["url"], min(int(args.get("max_chars", 8000)), MAX_FETCH)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": BROWSER_UA}) as client:
        resp = await client.get(url)
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype:
        body = html_to_text(resp.text, str(resp.url) if args.get("links", True) else None)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        body = resp.text
    else:
        return f"HTTP {resp.status_code}; non-text content ({ctype or 'unknown type'}, {len(resp.content)} bytes)"
    return f"HTTP {resp.status_code} {resp.url}\n\n" + _truncate(body, max_chars)


async def browse(ctx: ToolContext, args: dict) -> str:
    """Render a JavaScript-heavy page with a real headless browser (needs the `browser` extra)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "browse is unavailable: install with `pip install 'tendril[browser]' && playwright install chromium`."
    url, max_chars = args["url"], min(int(args.get("max_chars", 8000)), MAX_FETCH)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=BROWSER_UA)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            text = await page.inner_text("body")
            if args.get("screenshot"):
                shot = _resolve(ctx, args["screenshot"])
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot), full_page=True)
            return f"{page.url}\n\n" + _truncate(text, max_chars)
        finally:
            await browser.close()


# --- files -------------------------------------------------------------------


async def read_file(ctx: ToolContext, args: dict) -> str:
    return _truncate(_resolve(ctx, args["path"]).read_text(errors="replace"))


async def write_file(ctx: ToolContext, args: dict) -> str:
    target = _resolve(ctx, args["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if args.get("append") else "w") as f:
        f.write(args["content"])
    return f"wrote {len(args['content'])} chars to {args['path']}"


async def list_files(ctx: ToolContext, args: dict) -> str:
    root = ctx.workspace.resolve()
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    return "\n".join(files) or "(workspace is empty)"


# --- shell -------------------------------------------------------------------


async def run_shell(ctx: ToolContext, args: dict) -> str:
    command, timeout = args["command"], int(args.get("timeout", 120))
    workspace = ctx.workspace.resolve()
    if ctx.settings.shell_sandbox == "docker":
        argv = ["docker", "run", "--rm", "-v", f"{workspace}:/work", "-w", "/work",
                ctx.settings.shell_image, "sh", "-c", command]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"timed out after {timeout}s"
    text = out.decode(errors="replace")
    return f"exit code {proc.returncode}\n" + (text[-MAX_OUTPUT:] if len(text) > MAX_OUTPUT else text)


# --- memory ------------------------------------------------------------------


async def remember(ctx: ToolContext, args: dict) -> str:
    shared = args.get("scope") == "shared"
    note_id = ctx.store.add_note(args["note"], None if shared else ctx.goal["id"])
    return f"saved note #{note_id}" + (" (shared with all goals)" if shared else "")


async def recall(ctx: ToolContext, args: dict) -> str:
    rows = ctx.store.search_notes(args["query"])
    return "\n".join(f"#{r['id']} (goal {r['goal_id'] or 'shared'}): {r['content']}" for r in rows) or "no matching notes"


# --- control -----------------------------------------------------------------


async def notify(ctx: ToolContext, args: dict) -> str:
    ctx.store.notify(ctx.goal["id"], args["message"])
    return "user notified"


async def sleep(ctx: ToolContext, args: dict) -> str:
    minutes = max(1.0, float(args["minutes"]))
    ctx.control.next_run_at = time.time() + minutes * 60
    ctx.control.stop = True
    return f"will wake up again in {minutes:g} minutes"


async def finish(ctx: ToolContext, args: dict) -> str:
    ctx.control.finished = ctx.control.stop = True
    ctx.store.notify(ctx.goal["id"], f"Goal #{ctx.goal['id']} finished: {args['summary']}")
    return "goal marked as done"


def register_builtin(registry: Registry) -> None:
    s = {"type": "string"}
    tools = [
        Tool("web_search", "Search the web. Returns titles, URLs and snippets.",
             params({"query": s, "max_results": {"type": "integer", "default": 5}}, ["query"]), web_search),
        Tool("web_fetch", "Fetch a URL and return its readable text. Links are shown as [text](url); "
             "cite those URLs, never invent one.",
             params({"url": s, "max_chars": {"type": "integer", "default": 8000, "maximum": MAX_FETCH},
                     "links": {"type": "boolean", "default": True, "description": "keep link URLs"}}, ["url"]),
             web_fetch),
        Tool("browse", "Open a URL in a headless browser (for JavaScript-heavy pages) and return the page text. "
             "Optionally save a full-page screenshot into the workspace.",
             params({"url": s, "max_chars": {"type": "integer"}, "screenshot": {**s, "description": "workspace path, e.g. shots/home.png"}}, ["url"]),
             browse),
        Tool("read_file", "Read a text file from this goal's workspace.", params({"path": s}, ["path"]), read_file),
        Tool("write_file", "Write (or append to) a text file in this goal's workspace. Use it for reports and drafts.",
             params({"path": s, "content": s, "append": {"type": "boolean", "default": False}}, ["path", "content"]),
             write_file),
        Tool("list_files", "List files in this goal's workspace.", params({}), list_files),
        Tool("run_shell", "Run a shell command inside this goal's workspace directory.",
             params({"command": s, "timeout": {"type": "integer", "default": 120}}, ["command"]), run_shell,
             default_policy="ask"),
        Tool("remember", "Save a note to long-term memory. It appears in your context on every future wake-up. "
             "scope='shared' makes it visible to all goals (e.g. user preferences).",
             params({"note": s, "scope": {"type": "string", "enum": ["goal", "shared"], "default": "goal"}}, ["note"]),
             remember),
        Tool("recall", "Search all saved memory notes.", params({"query": s}, ["query"]), recall),
        Tool("notify", "Send the user a message. Use for results, questions, or anything needing their attention.",
             params({"message": s}, ["message"]), notify, control=True),
        Tool("sleep", "End this wake-up and wake again after the given number of minutes.",
             params({"minutes": {"type": "number"}, "reason": s}, ["minutes"]), sleep, control=True),
        Tool("finish", "Mark the goal as fully accomplished and stop working on it.",
             params({"summary": s}, ["summary"]), finish, control=True),
    ]
    # Don't offer the model a tool it can't use: each failed attempt costs a whole model turn.
    if importlib.util.find_spec("playwright") is None:
        tools = [t for t in tools if t.name != "browse"]
    for tool in tools:
        registry.add(tool)
