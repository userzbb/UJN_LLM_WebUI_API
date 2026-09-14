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
  api_key: "eyJhbGciOi..."              # 兜底 JWT，见下节（正常由脚本自动提取）
  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"
  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"
```

说明：

- `config.yaml` 已被 `.gitignore` 排除，**不要提交或分享**。
- `webvpn_api_base` 到 `/api` 结束，不含 `/chat/completions`。
- `webvpn_host_query` 是 URL 问号后面的部分，不含 `?`。
  **它是 JWT 自动提取必需的** —— 缺了它脚本算不出应用页 URL，会跳过提取并告警。

### JWT 会自动提取，不用手抄

上游只认 **JWT 令牌**（形如 `eyJhbGciOiJIUzI1NiIs...`，三段以 `.` 分隔）。
旧文档里写的 `sk-your-ujn-api-key` 是占位符，**早已不适用** —— 填上去会 `401`。

`ujn_webvpn_login.py` 登录成功后会顺路走到 chat 应用页，把 JWT 掏出来写进
`ujn_webvpn_state.json` 的顶层 `jwt` 字段；`build_litellm_config.py` 优先用 state 里
那一份，`config.yaml` 的 `api_key` 只是兜底。整条链路是自动的：

```
ujn_webvpn_login.py  ->  state.json { cookies, jwt }  ->  build_litellm_config.py  ->  litellm_config.yaml
```

**凭据优先级**：`state.jwt` > `config.yaml` 的 `api_key`。
两者都有且不同时脚本会在 stderr 告警，告诉你实际用的是哪一份。

**`config.yaml` 的 `api_key` 建议填一份作为兜底**（而不是留空）：正常情况下
自动提取的 JWT 会被优先使用，这个值用不到；但没跑过登录脚本、state 文件被删、
或提取失败时，就靠它顶上。

> 🔍 **怎么确认填对了：** 该 JWT 的载荷只含一个用户 UUID，且**没有 `exp` 字段**
> ——也就是说它本身不会过期，失效只发生在你主动登出或上游清理会话时。
> 也正因如此，提取失败时脚本**沿用**上一次的 JWT 而不是抹掉，避免误退到旧凭据。

> ⚠ **不要把 JWT 或 Cookie 发给任何人。** 它的签名部分足以冒充你调用上游接口。
> 一旦泄露，去 ChatUJN 登出重登即可作废。

手工提取（仅在自动路径失效、需要核对时）：登录 ChatUJN 后按 F12 → Console 里执行
`document.cookie`，复制 `token=` 后面的值填进 `api_key`。

> 📌 **JWT 在哪：** 它在名为 `token` 的 **cookie** 里，**不在 localStorage**。
> 用浏览器控制台 dump `localStorage` 会看到一堆 `__2___3___2..._token` 的混淆键，
> 那是前端把 cookie 复制过去的副本，不是源头 —— 照它去找会永远找不到。
> 附带一个坑：它是 JS 设的 session cookie，**不进 Playwright 的 cookie jar**，
> 所以 `context.storage_state()` 里没有它，只能读 `document.cookie`。

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
2. 读 Cookie 与 JWT 生成 `litellm_config.yaml`。
3. 启动后台定时任务，每 30 分钟刷新登录态（Cookie + JWT）并重新生成配置。
4. 前台启动 LiteLLM 代理（`http://127.0.0.1:4000`）。

**守护策略**：每 60s 探测一次上游，按结果决定动作 ——

| 探测结果 | 动作 |
|---|---|
| 正常 | 什么都不做（零中断） |
| `401` / `403` / `502` / `503` | WebVPN 会话失效 → 重新登录换凭据并重启 |
| 连不上 / 超时 | 只重启代理进程，**不重新登录** |
| `400` | 多半是模型名问题 → 不重启，只告警 |

> 关键在于第三行：网络抖动不该触发一次浏览器登录。登录本身要联网，
> 抖动期间大概率也失败，只会白白拖慢恢复。

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

（`run.ps1` 会自动设置这一节的内容，手工启动 LiteLLM 时才需要自己来。）

若开了系统代理或梯子，必须让**本地地址和校园网地址**都直连：

```powershell
$env:NO_PROXY = "localhost,127.0.0.1,.ujn.edu.cn"
$env:no_proxy = "localhost,127.0.0.1,.ujn.edu.cn"
```

> ⚠ **`.ujn.edu.cn` 不能漏。** 只写 `localhost,127.0.0.1` 时，LiteLLM 会把上游请求
> 发给你的代理软件；代理一关就变成死地址，上游全部 `500`（开着代理才正常）。
> 而 webvpn 是国内教育网地址，实测直连 0.2s，本来就不需要代理。

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
手工排查时重跑一次登录脚本刷新 JWT：

```powershell
uv run python ujn_webvpn_login.py --headless
uv run python build_litellm_config.py
```

若登录脚本报「未提取到 JWT」，见下一条。

**登录后报「未提取到 JWT」**
→ 应用页没能提供 JWT。按顺序查：

1. `config.yaml` 里有没有 `webvpn_host_query`（缺了它算不出应用页 URL）；
2. 账号在 ChatUJN 侧是否有权限（能手工登录 chat.ujn.edu.cn 吗）；
3. 上游是否改了 token 的存放方式 —— 在浏览器 Console 里执行
   `document.cookie`，看还有没有 `token=` 开头的那一段。

注意这个告警**不影响 Cookie 刷新**，登录本身仍是成功的（退出码 0）。
此时脚本会沿用上一次保存的 JWT。

**一关掉代理软件就 `500`（开着才正常）**
→ 你的 PowerShell profile 里设了 `HTTP_PROXY` / `HTTPS_PROXY`（FlClash 等常见），
`run.ps1` 继承它，LiteLLM 子进程再把**所有**上游请求发给那个代理。
代理软件一关，端口变成死地址 → 上游全部 500。

`webvpn.ujn.edu.cn` 解析到 `202.194.65.6`（国内教育网），实测直连 TLSv1.3 仅 0.2s，
**不需要代理**。`run.ps1` 已自动把 `.ujn.edu.cn` 并入 `NO_PROXY`，无需手工处理。

若你的代理软件跑在 TUN/全局模式，它可能无视 `NO_PROXY` 直接劫持流量 ——
那种情况请在代理软件里为 `*.ujn.edu.cn` 配一条直连规则。

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
