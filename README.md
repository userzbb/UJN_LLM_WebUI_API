# UJN_LLM_API

把济南大学 WebVPN 后面的 **ChatUJN**（Open WebUI + vLLM）封装成本机 LLM 接口，
让 **Claude Code、Codex、OpenCode** 以及任何 OpenAI / Anthropic 客户端都能直接接入，
并完整支持**工具调用**（并行工具、流式增量参数、`tool_result` 回传）。

Expose UJN's WebVPN-only ChatUJN as a local LLM API for Claude Code, Codex, OpenCode,
and any OpenAI/Anthropic-compatible client — with full tool-calling support.

---

# 中文说明

## 这是什么

ChatUJN 只能通过校园 WebVPN 访问，且**必须同时携带 WebVPN Cookie 和 API Key**——
只给 API Key 会被重定向到登录页（实测 `302`）。而它本身只会说 **OpenAI Chat Completions**
一种协议，Claude Code（Anthropic Messages）和 Codex（Responses API）都听不懂。

本项目用一个 **LiteLLM 代理**做协议转换，把三种协议统一到本机 `http://127.0.0.1:4000`。

## 架构

```text
Claude Code   ──Anthropic /v1/messages──┐
Codex CLI     ──Responses /v1/responses─┤
OpenCode      ──OpenAI /v1/chat/...─────┼──→ http://127.0.0.1:4000
OpenAI SDK    ──OpenAI /v1/chat/...─────┘         │
                                                  ↓
                                    LiteLLM Proxy（协议转换 + 工具调用）
                                                  │
                                    WebVPN Cookie + UJN API Key
                                                  ↓
                                    ChatUJN（Open WebUI v0.5.16 + vLLM）
```

## 端点一览

| 端点 | 协议 | 用途 |
|---|---|---|
| `GET /health/liveliness` | — | 健康检查 |
| `GET /v1/models` | OpenAI | 模型列表 |
| `POST /v1/chat/completions` | OpenAI | OpenCode、OpenAI SDK |
| `POST /v1/messages` | Anthropic | Claude Code |
| `POST /v1/messages/count_tokens` | Anthropic | token 计数 |
| `POST /v1/responses` | OpenAI Responses | Codex CLI |

## 环境准备

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12。

```powershell
uv sync
uv run playwright install chromium
```

## 配置

```powershell
Copy-Item config.yaml.example config.yaml
```

编辑 `config.yaml`：

```yaml
username: "your_student_or_staff_id"    # WebVPN 账号
password: "your_webvpn_password"        # WebVPN 密码
proxy:
  api_key: "sk-your-ujn-api-key"        # ChatUJN 的 API Key
  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"
  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"
```

说明：

- `config.yaml` 已被 `.gitignore` 排除，**不要提交或分享**。
- `webvpn_api_base` 到 `/api` 结束，不含 `/chat/completions`。
- `webvpn_host_query` 是 URL 问号后面的部分，不含 `?`。

### 从浏览器获取 WebVPN 地址

1. 打开 <https://webvpn.ujn.edu.cn> 并登录。
2. 通过 WebVPN 打开 ChatUJN 页面。
3. 按 F12 → Network → 过滤 Fetch/XHR。
4. 在页面上发一句话。
5. 找到 `POST /api/chat/completions`，查看 Request URL。

看到的 URL 形如：

```text
https://webvpn.ujn.edu.cn/https/<opaque>/api/chat/completions?vpn-12-o2-chat.ujn.edu.cn
```

拆成配置：`.../api` 为 `webvpn_api_base`，`?` 之后为 `webvpn_host_query`。

> ⚠ 不要把浏览器里的 `Authorization`、`Cookie` 或 API Key 截图发人。

## 一键启动

```powershell
.\run.ps1
```

若 PowerShell 拦截执行策略：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

`run.ps1` 会：

1. 先做一次 headless WebVPN 登录刷新（最多 3 次）。
2. 读 Cookie 生成 `litellm_config.yaml`。
3. 启动后台定时任务，每 30 分钟刷新登录态并重新生成配置。
4. 前台启动 LiteLLM 代理（`http://127.0.0.1:4000`）。

> ⚠ **LiteLLM 只在启动时读取一次 `litellm_config.yaml`。** 后台刷新了 Cookie 后，
> 需要**重启 `run.ps1`** 才会生效。这是实测结论：运行中修改文件里的 Cookie，
> 服务仍会用旧值请求上游。

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:4000/health/liveliness
uv run python tests/smoke_test.py
```

## 工具调用

已实测支持（`tests/smoke_test.py` 覆盖）：

- **并行工具调用** —— 一次返回多个 `tool_use` / `function_call`
- **流式增量参数** —— Anthropic `input_json_delta`、Responses `response.function_call_arguments.delta`
- **`tool_result` 回传** —— 多轮工具调用闭环
- **`tool_choice`** —— `auto` / `any` / `tool`（Anthropic）与 `auto` / `required`（OpenAI）

### 两个必须同时设置的开关

这是本项目最关键的一点。LiteLLM 默认会把 Anthropic / Responses 请求转成 **Responses 协议**
发给上游，而 ChatUJN 只认 **Chat Completions**，结果就是 400。必须在配置里同时设置：

```yaml
model_list:
  - model_name: GLM-5.3                        # 上游原名
    litellm_params:
      model: hosted_vllm/GLM-5.3               # 不用 openai/，见下方说明
      use_chat_completions_api: true           # ← 开关 1（每个部署）
litellm_settings:
  use_chat_completions_url_for_anthropic_messages: true   # ← 开关 2（全局）
```

`build_litellm_config.py` 已自动写入这两项，**不要手工删除**。
漏掉任何一个，对应客户端就会收到 `HTTP 400`。

## 客户端接入

> 📖 **完整配置指南见 [`docs/客户端配置指南.md`](docs/客户端配置指南.md)**
> —— 里面列出了所有可直接填写 Base URL / API Key / 模型 ID，以及每个客户端的逐步配置。

> `clients/` 下的文件是**参考范本**，请根据你的环境自行配置 CC Switch 与各工具。

### Claude Code（直连，无需 CC Switch）

LiteLLM 原生支持 Anthropic 协议，Claude Code 可以直连：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
claude
```

> ⚠ `ANTHROPIC_BASE_URL` **不要**带 `/v1`。LiteLLM 的 `/v1/messages` 挂在根路径下，
> 客户端会自己补 `/v1`。这跟旧项目不同（旧项目靠 CC Switch 转换，所以填 `/v1`）。

`clients/ccswitch.json` 供需要多供应商切换时参考。

### Codex CLI

把 `clients/codex-config-snippet.toml` 合并进 `~/.codex/config.toml`，
并设置环境变量（值无意义，只要存在）：

```powershell
setx UJN_DUMMY_KEY "dummy"
```

> ⚠ Codex 现在**只接受** `wire_api = "responses"`，`"chat"` 已被官方移除
> （会报 `wire_api = "chat" is no longer supported`）。

### OpenCode

把 `clients/opencode.json` 复制到项目目录或 `~/.config/opencode/opencode.json`。

### 本地直连

若开了系统代理或梯子，让本地地址直连：

```powershell
$env:NO_PROXY = "localhost,127.0.0.1"
$env:no_proxy = "localhost,127.0.0.1"
```

## 模型列表

模型**直接以原名对外暴露**，不做别名映射 —— 客户端填的就是 `models.yaml` 里的名字：

```yaml
models:
  - deepseek-v41-flash
  - GLM-5.3
  - GLM-5.3-Flash
```

当前可用（实测于 **2026-09-14**，共 8 个）——**按上下文从大到小**。
下列 8 个**都能调用**，只是推荐程度不同；`不推荐` ≠ `不可用`。

> ⚠ **上游会更新**（模型会下线、新增、改名），上表是 2026-09-14 的快照，
> 不代表永远有效。以 `--list-upstream` 的实时输出为准（见下文）。

| 模型 ID | 上下文 | 说明 |
|---|---|---|
| `deepseek-v41-flash` | **1M** | ⭐ 推荐主力：最新一代 + 上下文最大 |
| `GLM-5.3-Flash` | **1M** | ⭐ 推荐：最快 |
| `deepseek-v4-flash` | **1M** | 可用 |
| `Qwen3.8-27B` | 256K | 可用（Claude Code 用不了，见下） |
| `Qwen3.6-27B` | 256K | 同上 |
| `/models/Qwen3.8-Flash-Next` | 256K | 同上 |
| `GLM-5.3` | 128K | 可用但不推荐：唯一不是 1M，且最慢 |
| `1.Qwen3.5-27B` | — | 最不推荐：最旧（3.5 世代） |

推荐主力：`deepseek-v41-flash`（1M 上下文 + 最新一代）。

### 查看当前可用模型

**遇到 `Model not found` 就是上游清单变了。** 两步查清：

```powershell
# 1) 上游实际有哪些（权威，带上下文长度）
uv run python build_litellm_config.py --list-upstream

# 2) 本代理对外暴露了哪些（客户端能填的名字以此为准）
(Invoke-RestMethod http://127.0.0.1:4000/v1/models).data.id
```

**用返回列表里的 id 作为模型名。** 或者直接一键同步：

```powershell
uv run python build_litellm_config.py --sync-models
```

它按上游清单更新 `models.yaml`，并报告新增/下线的模型；**已是最新时不动文件**。
同步完重新生成配置并重启：

```powershell
uv run python build_litellm_config.py
# 然后 Ctrl+C 停掉代理，重跑 run.ps1
```

### ⚠ Qwen 系列不能用在 Claude Code 里

Qwen 系（`Qwen3.8-27B`、`Qwen3.6-27B`、`/models/Qwen3.8-Flash-Next`、`1.Qwen3.5-27B`）
**在 Claude Code 里必然 `400`**，与配置无关：

```
400: System message must be at the beginning.
```

Claude Code 把 `Available agent types...` 以 `role: "system"` 消息塞进 `messages` 的**第 2 条**，
而上游 vLLM 对 Qwen 系强制要求 system 消息在最前面（deepseek / GLM 系则宽容）。
这是 Claude Code 的行为，代理层无法修正。**OpenAI 兼容客户端（OpenCode、SDK）用 Qwen 正常。**

## 常见问题

**`400` 且错误里含 `'messages'` 或 `'created_at'`**
→ 两个 bridge 开关缺了一个。重新生成配置：

```powershell
uv run python build_litellm_config.py
```

**Claude Code 开启 thinking 时报 `400`**
→ 确认 `litellm_config.yaml` 里的 model 前缀是 `hosted_vllm/` 而不是 `openai/`。
LiteLLM 内部有 `_RESPONSES_API_PROVIDERS = frozenset({"openai"})`：只要 provider 是
`openai`，带 thinking 的 `/v1/messages` 请求就会被强制路由到上游的 `/responses` 端点，
而 ChatUJN 没有该端点，直接 400。`hosted_vllm` 不在该集合里，且语义上更贴合真实后端（vLLM）。

**`Model not found`**
→ 上游模型下线了。运行 `--list-upstream` 查看当前清单，更新 `models.yaml`。

**启动崩溃 `UnicodeDecodeError: 'gbk' codec`**
→ `litellm_config.yaml` 里混入了非 ASCII 字符。LiteLLM 用系统默认编码读它，
生成脚本已做校验，不要手工往该文件里写中文。

**`302` / `502`**
→ WebVPN Cookie 过期。重新登录并重启：

```powershell
uv run python ujn_webvpn_login.py --headless
.\run.ps1
```

**Claude Code 报连接错误**
→ 确认 `ANTHROPIC_BASE_URL` 是 `http://127.0.0.1:4000`（**无** `/v1`）。

**`run.ps1` 报语法错误 `unexpected }`**
→ 脚本必须存为带 BOM 的 UTF-8。仓库里的文件已带 BOM；若你手工编辑后报错，
用支持 BOM 的编辑器另存为「UTF-8 带 BOM」。

---

# English Guide

## What This Is

ChatUJN is reachable only through the campus WebVPN, and it requires **both** a WebVPN
cookie **and** an API key — the key alone redirects to a login page (measured: `302`).
It also speaks only **OpenAI Chat Completions**, which Claude Code (Anthropic Messages)
and Codex (Responses API) cannot use.

This project runs a **LiteLLM proxy** that translates all three protocols to a single local
endpoint at `http://127.0.0.1:4000`.

## Architecture

```text
Claude Code   ──Anthropic /v1/messages──┐
Codex CLI     ──Responses /v1/responses─┤
OpenCode      ──OpenAI /v1/chat/...─────┼──→ http://127.0.0.1:4000
OpenAI SDK    ──OpenAI /v1/chat/...─────┘         │
                                                  ↓
                                    LiteLLM Proxy (protocol translation + tool calls)
                                                  │
                                    WebVPN cookie + UJN API key
                                                  ↓
                                    ChatUJN (Open WebUI v0.5.16 + vLLM)
```

## Endpoints

| Endpoint | Protocol | Used by |
|---|---|---|
| `GET /health/liveliness` | — | health check |
| `GET /v1/models` | OpenAI | model list |
| `POST /v1/chat/completions` | OpenAI | OpenCode, OpenAI SDK |
| `POST /v1/messages` | Anthropic | Claude Code |
| `POST /v1/messages/count_tokens` | Anthropic | token counting |
| `POST /v1/responses` | OpenAI Responses | Codex CLI |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```powershell
uv sync
uv run playwright install chromium
Copy-Item config.yaml.example config.yaml
```

Edit `config.yaml` with your WebVPN credentials, ChatUJN API key, and the WebVPN URL
(found in the browser's Network tab — see the Chinese section above for the walkthrough).

`config.yaml` is git-ignored. Never commit or share it.

## Run

```powershell
.\run.ps1
```

If PowerShell blocks script execution:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

The launcher refreshes the WebVPN login, generates `litellm_config.yaml`, starts a
background job that refreshes the session every 30 minutes, and starts the proxy in the
foreground on port 4000.

> ⚠ **LiteLLM reads `litellm_config.yaml` once at startup.** After a background cookie
> refresh you must **restart `run.ps1`** for it to take effect. This is measured behavior:
> changing the cookie on disk while the server runs does not change what it sends upstream.

Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:4000/health/liveliness
uv run python tests/smoke_test.py
```

## Tool Calling

Measured working (covered by `tests/smoke_test.py`):

- **Parallel tool calls** — multiple `tool_use` / `function_call` in one response
- **Streaming incremental arguments** — Anthropic `input_json_delta`, Responses
  `response.function_call_arguments.delta`
- **`tool_result` round-trip** — multi-turn tool loops
- **`tool_choice`** — `auto` / `any` / `tool` (Anthropic), `auto` / `required` (OpenAI)

### The two flags that must both be set

This is the crux of the project. By default LiteLLM forwards Anthropic and Responses
requests as **Responses-protocol** to the upstream, but ChatUJN speaks only **Chat
Completions** — yielding HTTP 400. Both flags are required:

```yaml
model_list:
  - model_name: GLM-5.3                        # upstream name verbatim
    litellm_params:
      model: hosted_vllm/GLM-5.3               # not openai/ — see below
      use_chat_completions_api: true           # ← flag 1 (per deployment)
litellm_settings:
  use_chat_completions_url_for_anthropic_messages: true   # ← flag 2 (global)
```

`build_litellm_config.py` writes both automatically. Do not remove either one.

## Client Setup

> 📖 **Full configuration guide: [`docs/客户端配置指南.md`](docs/客户端配置指南.md)**
> — lists every Base URL / API key / model ID to paste, plus per-client steps (in Chinese).

> Files under `clients/` are **reference templates** — configure CC Switch and each CLI
> for your own environment.

### Claude Code (direct — CC Switch not required)

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
claude
```

> ⚠ `ANTHROPIC_BASE_URL` takes **no** `/v1` suffix. LiteLLM serves `/v1/messages` at the
> root and the client appends `/v1` itself.

### Codex CLI

Merge `clients/codex-config-snippet.toml` into `~/.codex/config.toml` and set:

```powershell
setx UJN_DUMMY_KEY "dummy"
```

> ⚠ Codex now accepts **only** `wire_api = "responses"`; `"chat"` was removed upstream.

### OpenCode

Copy `clients/opencode.json` into your project or `~/.config/opencode/opencode.json`.

## Model List

Models are exposed **under their upstream names verbatim** — no alias mapping. Clients use
exactly the names listed in `models.yaml`:

```yaml
models:
  - deepseek-v41-flash
  - GLM-5.3
  - GLM-5.3-Flash
```

Currently available (measured **2026-09-14**, 8 models) — **largest context first**.
All eight are callable; the notes rank preference only — `not recommended` ≠ `unavailable`.

> ⚠ **The upstream changes over time** (models are retired, added, or renamed). The table
> above is a 2026-09-14 snapshot, not a permanent guarantee. Trust `--list-upstream` — see below.

| Model ID | Context | Notes |
|---|---|---|
| `deepseek-v41-flash` | **1M** | ⭐ recommended main: newest generation, largest context |
| `GLM-5.3-Flash` | **1M** | ⭐ recommended: fastest |
| `deepseek-v4-flash` | **1M** | available |
| `Qwen3.8-27B` | 256K | available (unusable from Claude Code — see below) |
| `Qwen3.6-27B` | 256K | same |
| `/models/Qwen3.8-Flash-Next` | 256K | same |
| `GLM-5.3` | 128K | available but not recommended: only non-1M model, and slowest |
| `1.Qwen3.5-27B` | — | least recommended: oldest (3.5 generation) |

Recommended main: `deepseek-v41-flash` (1M context, newest generation).

### Listing the currently available models

**`Model not found` means the upstream list changed.** Two queries tell you what is there:

```powershell
# 1) What the upstream actually offers (authoritative, includes context length)
uv run python build_litellm_config.py --list-upstream

# 2) What this proxy exposes (the names clients may use)
(Invoke-RestMethod http://127.0.0.1:4000/v1/models).data.id
```

**Use an `id` from the returned list as the model name.** Or sync in one step:

```powershell
uv run python build_litellm_config.py --sync-models
```

This updates `models.yaml` from the upstream list and reports what was added or retired;
it leaves the file untouched when already in sync. Then regenerate and restart:

```powershell
uv run python build_litellm_config.py
# then Ctrl+C the proxy and re-run run.ps1
```

### ⚠ The Qwen family cannot be used from Claude Code

The Qwen models (`Qwen3.8-27B`, `Qwen3.6-27B`, `/models/Qwen3.8-Flash-Next`,
`1.Qwen3.5-27B`) **always `400` under Claude Code**, regardless of configuration:

```
400: System message must be at the beginning.
```

Claude Code puts its `Available agent types...` block in `messages` as a `role: "system"`
message at **index 1**, while the upstream vLLM requires a system message to come first for
Qwen models (deepseek/GLM tolerate it). This is Claude Code's behavior and cannot be fixed
in the proxy. **OpenAI-compatible clients (OpenCode, SDK) use Qwen fine.**

## Troubleshooting

**`400` mentioning `'messages'` or `'created_at'`** → one of the two bridge flags is
missing. Regenerate the config.

**Claude Code returns `400` when thinking is enabled** → check that the model prefix in
`litellm_config.yaml` is `hosted_vllm/`, not `openai/`. LiteLLM has
`_RESPONSES_API_PROVIDERS = frozenset({"openai"})`: with the `openai` provider, any
`/v1/messages` request carrying thinking is force-routed to the upstream's `/responses`
endpoint, which ChatUJN does not expose (400). `hosted_vllm` is absent from that set and
matches the real backend (vLLM).

**`Model not found`** → the upstream list changed (model retired or renamed). Check what the
upstream offers with `uv run python build_litellm_config.py --list-upstream`, and what this
proxy exposes with `(Invoke-RestMethod http://127.0.0.1:4000/v1/models).data.id`, then update
`models.yaml` and regenerate.

**Startup crash `UnicodeDecodeError: 'gbk' codec`** → `litellm_config.yaml` contains
non-ASCII characters. LiteLLM reads it with the system default codec; the generator
already validates this — never hand-write Chinese into that file.

**`302` / `502`** → WebVPN cookie expired. Re-login and restart.

**Claude Code connection error** → confirm `ANTHROPIC_BASE_URL` is
`http://127.0.0.1:4000` (**no** `/v1`).

**`run.ps1` syntax error `unexpected }`** → the script must be saved as UTF-8 **with BOM**.
The committed file has one; if you edit it, re-save with a BOM.

---

## License

MIT
