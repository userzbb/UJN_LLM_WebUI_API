# UJN_LLM_API

把济南大学 WebVPN 后面的 **ChatUJN**（Open WebUI + vLLM）封装成本机 LLM 接口，
让 **Claude Code、Codex、OpenCode** 以及任何 OpenAI / Anthropic 客户端都能直接接入，
并完整支持**工具调用**（并行工具、流式增量参数、`tool_result` 回传）。

---

# 中文说明

## 这是什么

ChatUJN 只能通过校园 WebVPN 访问，且**必须同时携带 WebVPN Cookie 和 JWT 令牌**——
两者缺一不可（实测：只给 Cookie 报 `403 Not authenticated`，只给 JWT 被重定向到登录页 `302`）。
而它本身只会说 **OpenAI Chat Completions**
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
                                    WebVPN Cookie + JWT 令牌
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
  api_key: "eyJhbGciOi..."              # ChatUJN 的 JWT 令牌，见下节
  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"
  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"
```

说明：

- `config.yaml` 已被 `.gitignore` 排除，**不要提交或分享**。
- `webvpn_api_base` 到 `/api` 结束，不含 `/chat/completions`。
- `webvpn_host_query` 是 URL 问号后面的部分，不含 `?`。

### `api_key` 填的是 JWT 令牌，不是 `sk-` 开头的 Key

上游现在只认 **JWT 令牌**（形如 `eyJhbGciOiJIUzI1NiIs...`，三段以 `.` 分隔）。
旧文档里写的 `sk-your-ujn-api-key` 是占位符，**早已不适用** —— 填上去会 `401`。

获取方式：登录 ChatUJN 后按 F12 → Network → 任意一个 `POST /api/chat/completions` 请求，
复制请求头里的 `Authorization: Bearer eyJ...`，**去掉 `Bearer ` 前缀**，只把 JWT 本身填进
`api_key`（代理转发时会自动补上 `Authorization: Bearer <api_key>`）。

> 🔍 **怎么确认填对了：** 该 JWT 的载荷只含一个用户 UUID，且**没有 `exp` 字段**
> ——也就是说它本身不会过期，失效只发生在你主动登出或上游清理会话时。

> ⚠ **不要把 JWT 或 Cookie 发给任何人。** 它的签名部分足以冒充你调用上游接口。
> 一旦泄露，去 ChatUJN 登出重登即可作废。

其实**不用手工抄**：`ujn_webvpn_login.py` 已经负责维持整个登录会话，
`run.ps1` 每次重新登录后会重新生成 `litellm_config.yaml`。上面这条只在你
想手工核对时用得上。唯一例外是**首次配置** —— 先按上面取一个填进 `config.yaml`，
让 `config.yaml` 自己也有一份可用的兜底值。

### `<opaque>` 那一段不用手抄 —— 会自动填

那个路径段**不是密钥**，而是**主机名的编码**：

```
路径段 = "wrdvpnisthebest!" + AES-CTR(主机名)
```

`wrdvpnisthebest!` 是所有 WebVPN 通用的公开常量，所以**同一个主机名永远得到同一个值**，
全校所有人一样，不含任何账号/Cookie 信息。只拿到它而没有有效会话，照样被弹回登录页。

因此 `webvpn_api_base` 直接留着 `<opaque>` 即可，脚本会用 `webvpn_host_query`
里的主机名把它算出来：

```yaml
  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"
  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"
```

想单独看看算出来是什么：

```powershell
uv run python build_litellm_config.py --webvpn-path chat.ujn.edu.cn
```

### 从浏览器获取 WebVPN 地址（可选，想核对时再看）

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

> ⚠ 不要把浏览器里的 `Authorization`、`Cookie` 或 JWT 令牌截图发人。

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
> 它们由 `build_litellm_config.py` **自动生成**（跟着 `models.yaml` 同步），
> 所以里面的模型清单不会写歪。改完 `models.yaml` 重新生成即可：

```powershell
uv run python build_litellm_config.py   # 同时刷新 litellm_config.yaml 和 clients/*

```

| 范本 | 给谁用 |
|---|---|
| `clients/claude-settings.json` | Claude Code —— 可直接作为 `~/.claude/settings.json` 的 `env` 段 |
| `clients/ccswitch.json` | CC Switch 供应商配置 |
| `clients/codex-config-snippet.toml` | Codex —— 合并进 `~/.codex/config.toml` |
| `clients/opencode.json` | OpenCode |

> ⚠ **两处易错点，范本已按实测填好：**
> Claude Code 的模型名**要带 `[1M]`**（不带只算 200k，会提前 auto-compact），
> 且 `ANTHROPIC_BASE_URL` **不带 `/v1`**；
> Codex 恰好相反 —— `base_url` **带 `/v1`**，模型名**不带 `[1M]`**。

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

**Codex 报 `reasoning_effort` 相关 `400`**
→ 上游对 effort 取值挑食（`deepseek-v41-flash` 拒绝 `medium`，报
`DeepSeek V4.1 reasoning_effort must be low, high, xhigh, max...`），
且 Codex 的 `reasoning: {effort, summary}` 会被 LiteLLM 整份 dict 塞进 `reasoning_effort`
（见 `litellm/responses/litellm_completion_transformation/transformation.py`），
上游收到 dict 后报 `literal_error`。

本项目已自动丢弃该字段 —— 确认每个部署都带：

```yaml
litellm_params:
  additional_drop_params: ["reasoning_effort"]
```

`build_litellm_config.py` 会自动写入，重新生成配置并重启即可。

> ⚠ 代价：**effort 档位不再实际调节推理强度**，上游用自身默认值。
> 实测丢弃前后无质量损失（同题采样，输出与 reasoning 长度同区间）。

**两个客户端的 effort 字段不一样**
→ Claude Code 用 `output_config.effort`，Codex 用 `reasoning: {effort, summary}`。
两者的形态现在都能通 —— 因为都统一丢弃了。

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

**`401 Unauthorized`**
→ JWT 令牌被拒（通常是登出过、或上游清理了会话）。`run.ps1` 会按需重新登录并重启；
手工排查时按上文「`api_key` 填的是 JWT 令牌」重新取一个填进 `config.yaml`。

**`403 Not authenticated`**
→ 请求没带 Cookie（只有 JWT 是不够的）。确认 `ujn_webvpn_state.json` 里
有 `wengine_vpn_ticketwebvpn_ujn_edu_cn`，并重新生成配置：

```powershell
uv run python build_litellm_config.py
```

**Claude Code 报连接错误**
→ 确认 `ANTHROPIC_BASE_URL` 是 `http://127.0.0.1:4000`（**无** `/v1`）。

**`run.ps1` 报语法错误 `unexpected }`**
→ 脚本必须存为带 BOM 的 UTF-8。仓库里的文件已带 BOM；若你手工编辑后报错，
用支持 BOM 的编辑器另存为「UTF-8 带 BOM」。

---

## License

MIT
