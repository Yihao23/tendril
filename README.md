<p align="center">
  <img src="tendril/web/logo.svg" alt="Tendril logo" width="112">
</p>

<h1 align="center">Tendril</h1>

<p align="center">
  An open-source, self-hosted, always-on AI agent.<br>
  开源、可自托管、全天候运行的 AI agent。
</p>

<p align="center"><a href="#english">English</a> | <a href="#中文">中文</a></p>

---

<a id="english"></a>

## English

**An open-source, self-hosted, always-on AI agent.** Give it a goal and it keeps working in the
background (researching, drafting, coding, checking things on a schedule) and messages you when
it has something to show or needs a decision.

It's an open alternative to hosted "background agent" products, with some differences:

- **Self-hosted.** Your goals, memory and files stay on your machine.
- **Any model.** Works with any OpenAI-compatible endpoint: Ollama, vLLM, DeepSeek, Qwen,
  OpenRouter, OpenAI, and others.
- **Permission layer.** Every tool call is `allow`, `ask` or `deny`. Risky calls wait for your approval.
- **Connects to apps through MCP.** Any MCP server's tools are added automatically.

### How it works

```
 you ──goal──▶  scheduler ──wake-up──▶  agent loop ──tool call──▶  guard ──allow──▶ tool
                   ▲                        │                        │
                   │                   sleep / finish               ask ──▶ approval queue ──▶ you
                   └──── next wake-up ◀─────┘
```

- A **goal** is a standing instruction such as "track these 5 competitors and send me a weekly
  report" or "find remote Rust jobs and keep a shortlist". It can repeat on a schedule (`--every 1d`).
- Each **wake-up** rebuilds context from the goal, saved memory notes, workspace files and
  recent history. The model then calls tools until it decides to `sleep` (come back later) or
  `finish`.
- Each goal gets its own **workspace** directory (`~/.tendril/workspaces/<id>`). File and shell
  tools are confined to it.
- The **guard** checks every call against your policy. `ask` calls are queued and the agent carries
  on with other work. If you decide during the same wake-up, the model gets the result right away;
  otherwise the result is in its history on the next wake-up. Either way the goal is woken up.
- If the server stops mid-run, goals that were running are resumed when it starts again.

#### Built-in tools

| Tool | Default | What it does |
|---|---|---|
| `web_search` | allow | DuckDuckGo search (best effort; swap in an MCP search server for reliability) |
| `web_fetch` | allow | Fetch a URL as readable text, with links kept as `[text](url)` |
| `browse` | allow | Headless Chromium for JS-heavy pages, with optional screenshots (only offered when the `[browser]` extra is installed) |
| `read_file` / `write_file` / `list_files` | allow | Workspace files |
| `run_shell` | **ask** | Shell command in the workspace (optionally inside Docker) |
| `remember` / `recall` | allow | Long-term memory, per goal or shared |
| `notify` / `sleep` / `finish` | always | Talk to you, schedule the next wake-up, complete the goal |
| `<server>__<tool>` | allow if read-only, else **ask** | Tools from your MCP servers |

### Quick start

```bash
pip install -e '.[mcp]'          # add ,browser for the headless browser tool
tendril init                     # writes ~/.tendril/tendril.toml
$EDITOR ~/.tendril/tendril.toml  # set [model] base_url + model (+ api_key)
tendril serve                    # agent + dashboard on http://127.0.0.1:7788
```

Example with a hosted API (fastest way to good results):

```toml
[model]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key = "sk-..."
```

The model must support tool calling. For a local model, check with `ollama show <model>`: the
`Capabilities` list must include `tools`.

Then, in another terminal (or use the dashboard):

```bash
tendril add "Every morning, check Hacker News for posts about local LLMs and send me the 3 best" --every 1d
tendril ls                  # goals, status, next wake-up
tendril show 1              # activity log
tendril say 1 "skip anything about benchmarks"
tendril inbox               # messages from your goals
tendril approvals           # tool calls waiting for you
tendril approve 3           # or: tendril deny 3
tendril tools               # every tool and its effective policy
```

#### Running a local model on CPU

Local models without a GPU work, but each step can take minutes. Settings that proved necessary
with Qwen3 8B on a CPU-only machine:

1. **Bigger context window.** Older Ollama versions default to 4096 tokens and silently cut off the
   start of the prompt (including the system prompt). Create a variant with more context:
   ```bash
   printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > Modelfile
   ollama create qwen3-8b-16k -f Modelfile
   ```
2. **Longer timeout, capped replies, no thinking:**
   ```toml
   [model]
   base_url = "http://localhost:11434/v1"
   model = "qwen3-8b-16k"
   timeout = 1800                    # a timed-out call is retried from scratch, so make it long enough
   max_tokens = 2048                 # stop a reasoning model from thinking forever
   extra_instructions = "/no_think"  # Qwen3: skip the thinking phase (faster, somewhat less smart)
   ```

Small models follow the process well (fetch, write, ask for approval, notify, finish), but
their summaries can be shallow or wrong. Use a stronger model for real work.

### Configuration

`~/.tendril/tendril.toml` (move it with `TENDRIL_HOME`). `TENDRIL_MODEL`, `TENDRIL_BASE_URL` and
`TENDRIL_API_KEY` override the `[model]` section.

```toml
[model]
timeout = 600                  # seconds per model call
max_tokens = 4096              # optional cap per reply
extra_instructions = ""        # appended to the system prompt

[agent]
max_steps = 12                 # tool-calling rounds per wake-up
max_concurrent = 2             # goals running at the same time

[policy]                       # allow | ask | deny; glob patterns work
run_shell = "ask"
"github__*" = "ask"
"gmail__send_*" = "deny"

[shell]
sandbox = "docker"             # run shell commands in a throwaway container

[mcp.servers.filesystem]       # stdio MCP server
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]

[mcp.servers.remote]           # streamable-HTTP MCP server
url = "https://example.com/mcp"
```

### Security notes

- The HTTP API has **no authentication**. It binds to `127.0.0.1` by default; keep it that way or
  put it behind an authenticating proxy.
- Without `sandbox = "docker"`, `run_shell` runs as your user. Its working directory is the
  workspace, but that is not a jail. Keep it on `ask`.
- Web pages and tool output can contain prompt injection. Treat `allow` as "I'm fine with the
  agent doing this even if a web page tells it to".

### Development

```bash
pip install -e '.[dev]'
pytest
```

Layout: `tendril/agent.py` (wake-up loop), `scheduler.py`, `guard.py` (permissions), `store.py`
(SQLite), `llm.py` (model client), `tools/` (built-ins + MCP bridge), `server.py` (API), `cli.py`,
`web/index.html` (dashboard).

### Roadmap

- Chat channels: Telegram / Slack / email, so you can reply to the agent from your phone
- Per-goal tool allowlists and spending/step budgets
- Context compaction (summarize old history instead of truncating it)
- Auth for the API and a multi-user mode

### License

MIT

---

<a id="中文"></a>

## 中文

**一个开源、可自托管、全天候运行的 AI agent。** 交给它一个目标，它就会在后台持续工作：调研、写文档、写代码、定期检查信息。有成果或者需要你拍板时，它会主动给你发消息。

它是托管式“后台 agent”产品的开源替代品，不同之处在于：

- **自托管**：任务、记忆和文件都保存在你自己的机器上。
- **模型随便换**：支持任何 OpenAI 兼容接口，比如 Ollama、vLLM、DeepSeek、Qwen、OpenRouter、OpenAI 等。
- **权限层**：每个工具调用都可以设为 `allow`（允许）、`ask`（需审批）或 `deny`（禁止），有风险的操作要等你批准才会执行。
- **通过 MCP 连接各种应用**：配置好的 MCP server，它的工具会自动加入。

### 工作原理

```
 你 ──目标──▶  调度器 ──唤醒──▶  agent 循环 ──工具调用──▶  权限层 ──allow──▶ 工具
                 ▲                    │                       │
                 │               sleep / finish              ask ──▶ 审批队列 ──▶ 你
                 └──── 下次唤醒 ◀─────┘
```

- **目标（goal）** 是一条长期指令，例如“跟踪这 5 家竞争对手，每周给我一份报告”或“找远程 Rust 岗位并维护一份候选清单”。可以设置为定期重复（`--every 1d`）。
- **每次唤醒** 时，它会根据目标、保存的记忆、工作区文件和最近的历史重新构建上下文，然后由模型调用工具，直到它决定 `sleep`（稍后再来）或 `finish`（完成）。
- 每个目标都有自己独立的 **工作区** 目录（`~/.tendril/workspaces/<id>`），文件和 shell 工具只能在里面操作。
- **权限层** 会按你的策略检查每一次调用。需要审批的调用会进入队列，agent 先去做别的事。如果你在同一轮唤醒里就做了决定，模型会立刻拿到结果；否则结果会出现在下一次唤醒的历史里。无论哪种情况，目标都会被唤醒。
- 如果服务在运行途中被停掉，重启后会自动恢复正在运行的目标。

#### 内置工具

| 工具 | 默认策略 | 作用 |
|---|---|---|
| `web_search` | allow | DuckDuckGo 搜索（尽力而为；要稳定可以换成 MCP 搜索服务） |
| `web_fetch` | allow | 抓取网页并转成可读文本，链接保留为 `[文字](网址)` |
| `browse` | allow | 用无头 Chromium 打开依赖 JS 的页面，可选截图（只有安装了 `[browser]` 扩展才会提供） |
| `read_file` / `write_file` / `list_files` | allow | 读写工作区文件 |
| `run_shell` | **ask** | 在工作区里执行 shell 命令（可选在 Docker 中运行） |
| `remember` / `recall` | allow | 长期记忆，可以按目标单独保存或全局共享 |
| `notify` / `sleep` / `finish` | 总是允许 | 给你发消息、安排下次唤醒、标记目标完成 |
| `<server>__<tool>` | 只读工具 allow，其余 **ask** | 来自 MCP server 的工具 |

### 快速开始

```bash
pip install -e '.[mcp]'          # 需要无头浏览器工具的话加上 ,browser
tendril init                     # 生成 ~/.tendril/tendril.toml
$EDITOR ~/.tendril/tendril.toml  # 设置 [model] 的 base_url 和 model（以及 api_key）
tendril serve                    # 启动 agent 和网页面板：http://127.0.0.1:7788
```

使用托管 API 的例子（最快拿到好效果）：

```toml
[model]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key = "sk-..."
```

模型必须支持 tool calling。本地模型可以用 `ollama show <模型名>` 检查，`Capabilities` 里要有 `tools`。

然后在另一个终端里操作（也可以直接用网页面板）：

```bash
tendril add "每天早上看看 Hacker News 上关于本地大模型的帖子，挑 3 条最好的发给我" --every 1d
tendril ls                  # 目标列表、状态、下次唤醒时间
tendril show 1              # 查看活动日志
tendril say 1 "跳过和跑分相关的内容"
tendril inbox               # 查看目标发来的消息
tendril approvals           # 等待你审批的工具调用
tendril approve 3           # 或者：tendril deny 3
tendril tools               # 所有工具及其生效的权限策略
```

#### 在 CPU 上运行本地模型

没有 GPU 也能跑本地模型，但每一步可能要几分钟。在纯 CPU 机器上用 Qwen3 8B 实测后，下面这些设置是必需的：

1. **加大上下文窗口**：较老版本的 Ollama 默认只有 4096 token，超出时会静默截掉提示词开头（包括系统提示）。可以建一个上下文更大的版本：
   ```bash
   printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > Modelfile
   ollama create qwen3-8b-16k -f Modelfile
   ```
2. **延长超时、限制回复长度、关闭思考模式：**
   ```toml
   [model]
   base_url = "http://localhost:11434/v1"
   model = "qwen3-8b-16k"
   timeout = 1800                    # 超时的请求会从头重试，所以超时要设得足够长
   max_tokens = 2048                 # 防止推理模型无休止地思考
   extra_instructions = "/no_think"  # Qwen3：跳过思考阶段（更快，但推理能力稍弱）
   ```

小模型能很好地走完流程（抓取、写文件、申请审批、通知、结束），但总结可能比较浅，甚至出错。正式使用建议换更强的模型。

### 配置

配置文件是 `~/.tendril/tendril.toml`，可以用 `TENDRIL_HOME` 换位置。环境变量 `TENDRIL_MODEL`、`TENDRIL_BASE_URL`、`TENDRIL_API_KEY` 会覆盖 `[model]` 里的设置。

```toml
[model]
timeout = 600                  # 每次模型调用的超时时间（秒）
max_tokens = 4096              # 可选：单次回复的长度上限
extra_instructions = ""        # 追加到系统提示末尾的内容

[agent]
max_steps = 12                 # 每次唤醒最多调用几轮工具
max_concurrent = 2             # 同时运行的目标数量

[policy]                       # allow | ask | deny，支持通配符
run_shell = "ask"
"github__*" = "ask"
"gmail__send_*" = "deny"

[shell]
sandbox = "docker"             # shell 命令在一次性容器里运行

[mcp.servers.filesystem]       # stdio 方式的 MCP server
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]

[mcp.servers.remote]           # streamable-HTTP 方式的 MCP server
url = "https://example.com/mcp"
```

### 安全提示

- HTTP 接口**没有任何身份验证**。默认只监听 `127.0.0.1`，请保持这样，或者放在带认证的反向代理后面。
- 不设置 `sandbox = "docker"` 时，`run_shell` 以你的用户身份运行。它的工作目录是工作区，但这并不能限制它访问别的地方，请保持为 `ask`。
- 网页和工具输出里可能藏有提示词注入（prompt injection）。设置 `allow` 前请想清楚：就算网页诱导 agent 去做这件事，你也能接受吗？

### 开发

```bash
pip install -e '.[dev]'
pytest
```

代码结构：`tendril/agent.py`（唤醒循环）、`scheduler.py`（调度器）、`guard.py`（权限层）、`store.py`（SQLite 存储）、`llm.py`（模型客户端）、`tools/`（内置工具和 MCP 桥接）、`server.py`（API）、`cli.py`（命令行）、`web/index.html`（网页面板）。

### 路线图

- 聊天渠道：Telegram / Slack / 邮件，用手机就能回复 agent
- 按目标设置工具白名单，以及花费和步数预算
- 上下文压缩（把旧历史总结成摘要，而不是直接截断）
- API 身份验证和多用户模式

### 许可证

MIT
