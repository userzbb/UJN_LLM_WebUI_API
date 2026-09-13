# UJN_LLM_API 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把济南大学 WebVPN 后面的 ChatUJN（Open WebUI + vLLM）封装成本机 `http://127.0.0.1:4000`，让 Claude Code、Codex、OpenCode 等客户端都能直接接入，并完整支持工具调用。

**Architecture:** 不再自写协议转换。用 **LiteLLM Proxy** 作为转换层，它原生同时提供 `/v1/chat/completions`、`/v1/messages`（Anthropic）、`/v1/responses`（Codex）三种入口。本项目只负责三件事：(1) Playwright 登录 WebVPN 拿 Cookie；(2) 把 Cookie + 上游 URL 生成为 LiteLLM 的 `config.yaml`；(3) 一键启动与刷新。核心难点已实测解决——LiteLLM 默认把 Anthropic/Responses 请求转成 **Responses 协议**发给上游，而 ChatUJN 只认 **Chat Completions 协议**，必须用两个开关强制桥接。

**Tech Stack:** Python 3.12 · uv · LiteLLM Proxy 1.100.1 · Playwright · PowerShell

**Spec:** 本计划即设计依据。所有关键结论均由 2026-09-13 对真实上游的实测得出（见「已验证事实」）。

---

## Global Constraints

- Python 版本：**3.12**（`uv venv --python 3.12`）；LiteLLM 1.100.1 已在 3.13 上出现安装损坏（长路径），不要用 3.13。
- 依赖管理：**只用 uv**。禁止 `pip install` 到全局环境。
- 生成的 `litellm_config.yaml` **必须是纯 ASCII**（无中文注释）。LiteLLM 用系统默认编码（本机 GBK）读取该文件，含非 ASCII 字符会直接启动崩溃：`UnicodeDecodeError: 'gbk' codec`。
- `litellm_config.yaml` **必须加入 `.gitignore`**——它内含实时 WebVPN Cookie 与 UJN API Key。
- 代理默认端口 **4000**（LiteLLM 默认），不为兼容旧项目而改。
- 所有面向用户的文档、注释、print 输出用**中文**；生成的配置文件内容用**英文/ASCII**。
- 旧项目 `D:\git_software\UJNAPI` **保持原样不动**（它正在为当前会话供 token）。只从它拷贝 `ujn_webvpn_login.py`。

---

## 已验证事实（不要重新调研，直接采信）

以下均为 2026-09-13 对本项目真实上游实测的结果：

| # | 事实 | 证据 |
|---|---|---|
| 1 | 上游是 **Open WebUI v0.5.16** + vLLM | `GET /config` → `{"name":"ChatUJN","version":"0.5.16"}` |
| 2 | 上游**只有** `/api/chat/completions`，无 Anthropic / Responses 路由 | `POST /v1/messages` → 405（SPA 兜底页） |
| 3 | **Cookie 是必需的**，光有 api-key 不行 | 无 Cookie → `302` 跳登录；带 Cookie → `200` |
| 4 | 上游**原生返回** `tool_calls`，支持并行 | `finish_reason: tool_calls`，一次返回 2 个工具 |
| 5 | 上游**真的增量流式** | 首字节 0.93s，12 个 ~4KB chunk，`transfer-encoding: chunked` |
| 6 | 工具参数**增量流式**下发 | `id`+`name` 先到，随后 `index` 分片的 `arguments` |
| 7 | 上游是**推理模型**，reasoning 占输出约 85% | 80 帧中 68 帧只有 `reasoning`；`1.Qwen3.5-27B` 的 `content` 为 0 字符 |
| 8 | LiteLLM `/v1/messages` 默认走 **Responses 适配器**，会 `KeyError: 'created_at'` | `experimental_pass_through/responses_adapters/handler.py:185` |
| 9 | LiteLLM `/v1/responses` 默认把 `input` 直接发给上游 → `KeyError: 'messages'` | 上游返回 `{"detail":"'messages'"}` |
| 10 | **两个开关同时设置**才能全部打通 | 见下方「关键配置」 |
| 11 | 上游 URL 的 `/chat/completions` 必须在 **query string 之前** | query 在前 → 405；在后 → 200 |
| 12 | LiteLLM 会在 URL 后再追加一次 `/chat/completions`（冗余但无害） | WebVPN 按路径路由，忽略 query 之后的内容 |

### 关键配置（实测通过）

```yaml
model_list:
  - model_name: <别名>
    litellm_params:
      model: openai/<上游模型id>
      api_base: <webvpn_api_base>/chat/completions?<webvpn_host_query>
      api_key: <ujn-api-key>
      use_chat_completions_api: true        # ← 开关 1（每个部署都要）
      extra_headers:
        Cookie: "<从 state 文件拼接>"
        Origin: "https://webvpn.ujn.edu.cn"
        Referer: "https://webvpn.ujn.edu.cn/"
        User-Agent: "Mozilla/5.0 ..."

litellm_settings:
  use_chat_completions_url_for_anthropic_messages: true   # ← 开关 2（全局）
  drop_params: true
  merge_reasoning_content_in_choices: true
```

**单用任一开关都会 400。** 两个都设才能让四个端点全通：

```
1. /v1/chat/completions      : 200
2. /v1/messages + tools      : 200  stop: tool_use | tools: [get_weather, get_time]
3. /v1/responses STREAM+tool : 200  status: completed | calls: [get_weather, get_time] | deltas: 8
```

### 上游模型清单（2026-09-13 实测）

| 上游 id | 上下文 |
|---|---|
| `GLM-5.3` | 131072 |
| `GLM-5.3-Flash` | 1048576 |
| `deepseek-v41-flash` | 1048576 |
| `deepseek-v4-flash` | 1048576 |
| `/models/Qwen3.8-Flash-Next` | 262144 |
| `Qwen3.8-27B` | 262144 |
| `Qwen3.6-27B` | 262144 |
| `1.Qwen3.5-27B` | — |

> 注意：旧项目里硬编码的 `/models/GLM-5___2-NVFP4` **已下线**（`HTTP 400 Model not found`）。本计划不继承该映射。

---

## File Structure

```
D:\git_software\UJN_LLM_API\
├── .gitignore                     # 排除密钥与生成物
├── README.md                      # 中英对照说明
├── pyproject.toml                 # uv 项目定义（依赖声明）
├── config.yaml.example            # 用户配置模板（提交）
├── config.yaml                    # 用户真实配置（gitignore）
├── ujn_webvpn_login.py            # Playwright 登录，产出 ujn_webvpn_state.json
├── build_litellm_config.py        # 【核心】state + config → litellm_config.yaml
├── litellm_config.yaml            # 生成物，含密钥（gitignore）
├── models.yaml                    # 模型别名映射（可编辑，提交）
├── run.ps1                        # 一键启动：登录 → 生成配置 → 启 LiteLLM
├── clients/
│   ├── ccswitch.json              # Claude Code / CC Switch 配置
│   └── opencode.json              # OpenCode 配置
└── tests/
    ├── test_build_config.py       # 配置生成单元测试
    └── smoke_test.py              # 端到端冒烟测试（真实上游）
```

**职责边界：**
- `ujn_webvpn_login.py` — 只负责登录，不关心代理。
- `build_litellm_config.py` — 纯函数式：读两个文件，产出 yaml 字符串。可单测，不联网。
- `models.yaml` — 数据与代码分离，改模型映射不用改 Python。
- `run.ps1` — 编排，不含业务逻辑。

---

## Task 1: 项目骨架与 git 仓库

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `config.yaml.example`

**Interfaces:**
- Consumes: 无
- Produces: 可被后续任务 `uv run` 的项目环境

- [ ] **Step 1: 初始化 git 仓库**

```bash
cd /d/git_software/UJN_LLM_API
git init -b main
```

- [ ] **Step 2: 写 `.gitignore`**

```gitignore
# 密钥与生成物 —— 绝不提交
config.yaml
litellm_config.yaml
ujn_webvpn_state.json

# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/

# 日志与调试
*.log
debug/
proxy_requests.log
proxy_stdout.log
proxy_stderr.log

# 编辑器
.vscode/
.idea/
```

- [ ] **Step 3: 写 `pyproject.toml`**

```toml
[project]
name = "ujn-llm-api"
version = "1.0.0"
description = "Local proxy exposing UJN ChatUJN (WebVPN) as OpenAI / Anthropic / Responses compatible APIs"
requires-python = ">=3.12,<3.13"
dependencies = [
    "litellm[proxy]==1.100.1",
    "playwright>=1.45.0",
    "PyYAML>=6.0.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = ["pytest>=8.0.0"]
```

- [ ] **Step 4: 写 `config.yaml.example`**

```yaml
username: "your_student_or_staff_id"
password: "your_webvpn_password"
proxy:
  api_key: "sk-your-ujn-api-key"
  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"
  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"
```

- [ ] **Step 5: 建环境并验证依赖可装**

```bash
uv sync
uv run python -c "import litellm, playwright, yaml, httpx; print('deps OK')"
```
Expected: `deps OK`

- [ ] **Step 6: 提交**

```bash
git add .gitignore pyproject.toml config.yaml.example
git commit -m "chore: project skeleton with uv and gitignore"
```

---

## Task 2: 登录脚本

**Files:**
- Create: `ujn_webvpn_login.py`（从旧项目拷贝）
- Create: `config.yaml`（从 example 拷贝，填真实值；被 gitignore）

**Interfaces:**
- Consumes: `config.yaml` 的 `username` / `password`
- Produces: `ujn_webvpn_state.json`，含 `cookies[]`，每项有 `name` / `value` / `domain` / `path`

- [ ] **Step 1: 拷贝登录脚本**

```bash
cp /d/git_software/UJNAPI/ujn_webvpn_login.py /d/git_software/UJN_LLM_API/
```
（该脚本已实测可用，逻辑：Playwright 打开 WebVPN → 填账号密码 → 提交 → 保存 storage_state。）

- [ ] **Step 2: 准备真实配置**

```bash
cp config.yaml.example config.yaml
```
然后编辑 `config.yaml`，填入真实 `username` / `password` / `api_key` / `webvpn_api_base` / `webvpn_host_query`。

- [ ] **Step 3: 装 Playwright 浏览器**

```bash
uv run playwright install chromium
```

- [ ] **Step 4: 验证登录产出 state 文件**

```bash
uv run python ujn_webvpn_login.py --headless
```
Expected: 输出 `Saved session state to: ...\ujn_webvpn_state.json`，且文件存在。

- [ ] **Step 5: 验证 state 里有 webvpn Cookie**

```bash
uv run python -c "
import json
s=json.load(open('ujn_webvpn_state.json',encoding='utf-8'))
c=[x for x in s.get('cookies',[]) if 'webvpn.ujn.edu.cn' in x.get('domain','')]
print('webvpn cookies:', len(c))
assert c, 'no webvpn cookies found'
print('names:', [x['name'] for x in c])
"
```
Expected: `webvpn cookies: 5` 左右，names 含 `wengine_vpn_ticketwebvpn_ujn_edu_cn`

- [ ] **Step 6: 提交**

```bash
git add ujn_webvpn_login.py
git commit -m "feat: add WebVPN login script"
```

---

## Task 3: 模型别名映射文件

**Files:**
- Create: `models.yaml`

**Interfaces:**
- Consumes: 无
- Produces: `models.yaml`，结构为 `models: [{upstream: str, aliases: [str]}]`，供 Task 4 读取

- [ ] **Step 1: 写 `models.yaml`**

```yaml
# 上游模型 id -> 客户端可见的别名列表。
# 别名让客户端可以用熟悉的模型名（如 claude-opus-4-1）驱动上游模型。
# 上游模型清单会变，用 `uv run python build_litellm_config.py --list-upstream` 查看当前值。
models:
  - upstream: GLM-5.3
    aliases: [GLM-5.3, claude-opus-4-1, gpt-5.1-codex]
  - upstream: deepseek-v41-flash
    aliases: [deepseek-v41-flash, claude-sonnet-4-5]
  - upstream: 1.Qwen3.5-27B
    aliases: [1.Qwen3.5-27B, claude-haiku-4-5]
  - upstream: Qwen3.6-27B
    aliases: [Qwen3.6-27B]
  - upstream: GLM-5.3-Flash
    aliases: [GLM-5.3-Flash]
  - upstream: deepseek-v4-flash
    aliases: [deepseek-v4-flash]
  - upstream: Qwen3.8-27B
    aliases: [Qwen3.8-27B]
  - upstream: /models/Qwen3.8-Flash-Next
    aliases: [/models/Qwen3.8-Flash-Next]
```

- [ ] **Step 2: 提交**

```bash
git add models.yaml
git commit -m "feat: add model alias mapping"
```

---

## Task 4: 配置生成器

**Files:**
- Create: `build_litellm_config.py`
- Test: `tests/test_build_config.py`

**Interfaces:**
- Consumes: `config.yaml`（proxy 段）、`ujn_webvpn_state.json`（cookies）、`models.yaml`
- Produces:
  - `render_config(settings: dict, cookie_header: str, models: list[dict]) -> str` — 纯函数，返回 YAML 文本
  - `build_target_url(api_base: str, host_query: str) -> str` — 返回 `<api_base>/chat/completions?<host_query>`
  - `load_cookie_header(state_file: Path) -> str` — 返回 `"k=v; k2=v2"`
  - `load_models(models_file: Path) -> list[dict]`
  - CLI：`python build_litellm_config.py [--out PATH] [--list-upstream]`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_build_config.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_litellm_config import build_target_url, render_config


def test_build_target_url_puts_path_before_query():
    """关键：/chat/completions 必须在 ? 之前，否则上游 405。"""
    url = build_target_url("https://vpn.example.edu/https/tok/api", "vpn-12-o2-host.edu.cn")
    assert url == "https://vpn.example.edu/https/tok/api/chat/completions?vpn-12-o2-host.edu.cn"
    assert url.index("/chat/completions") < url.index("?")


def test_build_target_url_without_query():
    url = build_target_url("https://vpn.example.edu/api", "")
    assert url == "https://vpn.example.edu/api/chat/completions"


def test_render_config_sets_both_bridge_flags():
    """单用任一开关都会 400，两个都必须出现。"""
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3", "claude-opus-4-1"]}]
    out = render_config(settings, "a=b; c=d", models)

    assert "use_chat_completions_url_for_anthropic_messages: true" in out
    assert "use_chat_completions_api: true" in out


def test_render_config_injects_cookie_and_browser_headers():
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3"]}]
    out = render_config(settings, "wengine_vpn_ticket=SECRET", models)

    assert 'Cookie: "wengine_vpn_ticket=SECRET"' in out
    assert 'Origin: "https://webvpn.ujn.edu.cn"' in out
    assert "Mozilla/5.0" in out


def test_render_config_is_pure_ascii():
    """LiteLLM 用 GBK 读该文件，任何非 ASCII 都会导致启动崩溃。"""
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3"]}]
    out = render_config(settings, "a=b", models)

    non_ascii = [c for c in out if ord(c) > 127]
    assert not non_ascii, f"config must be ASCII-only, found {non_ascii[:5]}"


def test_render_config_emits_one_entry_per_alias():
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3", "claude-opus-4-1"]}]
    out = render_config(settings, "a=b", models)

    assert out.count("model_name: GLM-5.3") == 1
    assert out.count("model_name: claude-opus-4-1") == 1
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_build_config.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'build_litellm_config'`

- [ ] **Step 3: 实现 `build_litellm_config.py`**

```python
"""从 config.yaml + ujn_webvpn_state.json 生成 LiteLLM 的 litellm_config.yaml。

LiteLLM 负责全部协议转换（OpenAI / Anthropic / Responses）与工具调用，
本脚本只把 WebVPN 的 Cookie 和上游 URL 注入成 LiteLLM 的 provider 配置。

⚠ 生成的配置必须保持纯 ASCII：LiteLLM 用系统默认编码（Windows 上是 GBK）
   读取该文件，含非 ASCII 字符会导致启动时 UnicodeDecodeError。
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
STATE_FILE = BASE_DIR / "ujn_webvpn_state.json"
MODELS_FILE = BASE_DIR / "models.yaml"
DEFAULT_OUT = BASE_DIR / "litellm_config.yaml"

WEBVPN_ORIGIN = "https://webvpn.ujn.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
)

BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": WEBVPN_ORIGIN,
    "Referer": WEBVPN_ORIGIN + "/",
    "User-Agent": USER_AGENT,
}


def read_yaml_scalar(text: str, key: str) -> str | None:
    """从 YAML 文本中取一个标量值（避免依赖完整解析的嵌套结构）。"""
    match = re.search(rf'^\s*{key}\s*:\s*"?([^"\n]+?)"?\s*$', text, re.M)
    return match.group(1).strip() if match else None


def load_proxy_settings(config_file: Path = CONFIG_FILE) -> dict[str, str]:
    if not config_file.exists():
        raise SystemExit(f"{config_file.name} 不存在，请先复制 config.yaml.example")
    text = config_file.read_text(encoding="utf-8")

    api_base = read_yaml_scalar(text, "webvpn_api_base")
    host_query = read_yaml_scalar(text, "webvpn_host_query")
    api_key = read_yaml_scalar(text, "api_key")

    if not api_base:
        raise SystemExit("config.yaml 缺少 proxy.webvpn_api_base")
    if not api_key:
        raise SystemExit("config.yaml 缺少 proxy.api_key")

    return {
        "api_base": api_base.rstrip("/"),
        "host_query": host_query or "",
        "api_key": api_key,
    }


def load_cookie_header(state_file: Path = STATE_FILE) -> str:
    if not state_file.exists():
        raise SystemExit(f"{state_file.name} 不存在，请先运行 ujn_webvpn_login.py")

    state = json.loads(state_file.read_text(encoding="utf-8"))
    pairs: list[str] = []
    for cookie in state.get("cookies", []):
        if "webvpn.ujn.edu.cn" not in cookie.get("domain", ""):
            continue
        pairs.append(f'{cookie["name"]}={cookie["value"]}')

    if not pairs:
        raise SystemExit("state 文件里没有 webvpn.ujn.edu.cn 的 Cookie，请重新登录")

    return "; ".join(pairs)


def load_models(models_file: Path = MODELS_FILE) -> list[dict]:
    if not models_file.exists():
        raise SystemExit(f"{models_file.name} 不存在")
    data = yaml.safe_load(models_file.read_text(encoding="utf-8")) or {}
    models = data.get("models") or []
    if not models:
        raise SystemExit("models.yaml 里没有任何模型定义")
    return models


def build_target_url(api_base: str, host_query: str) -> str:
    """构造上游 URL。

    ⚠ /chat/completions 必须出现在 query string 之前 —— WebVPN 按路径路由。
    实测：query 在前会 405；在后会 200。
    """
    url = f"{api_base}/chat/completions"
    if host_query:
        url = f"{url}?{host_query}"
    return url


def render_config(settings: dict[str, str], cookie_header: str, models: list[dict]) -> str:
    """生成 LiteLLM 配置文本。纯函数，不联网、不读文件。"""
    url = build_target_url(settings["api_base"], settings["host_query"])

    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = cookie_header

    lines: list[str] = ["model_list:"]
    for entry in models:
        upstream = entry.get("upstream")
        if not upstream:
            continue
        for alias in entry.get("aliases") or []:
            lines += [
                f"  - model_name: {alias}",
                "    litellm_params:",
                f"      model: openai/{upstream}",
                f"      api_base: {url}",
                f"      api_key: {settings['api_key']}",
                # 开关 1：强制把 /v1/responses 桥接到上游的 chat/completions。
                # 不加这条，Codex 的请求会把 input 直接发给上游 -> KeyError 'messages'。
                "      use_chat_completions_api: true",
                "      extra_headers:",
            ]
            for key, value in headers.items():
                lines.append(f'        {key}: "{value}"')
            lines.append("")

    lines += [
        "litellm_settings:",
        "  drop_params: true",
        # 开关 2：强制 /v1/messages 走 chat/completions 而非 Responses 适配器。
        # 不加这条，Claude Code 的请求会 KeyError 'created_at'。
        "  use_chat_completions_url_for_anthropic_messages: true",
        "  merge_reasoning_content_in_choices: true",
        "",
        "general_settings:",
        "  # Local-only proxy. The real UJN API key is injected above.",
        "  disable_auth: true",
        "  master_key: null",
        "",
    ]
    return "\n".join(lines)


def list_upstream_models(settings: dict[str, str], cookie_header: str) -> None:
    """打印上游当前可用的模型 id（排查「模型下线」用）。"""
    import httpx

    url = build_target_url(settings["api_base"], settings["host_query"])
    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = cookie_header
    headers["Authorization"] = f"Bearer {settings['api_key']}"

    models_url = url.replace("/chat/completions", "/models")
    response = httpx.get(models_url, headers=headers, timeout=60, trust_env=False)
    response.raise_for_status()
    for item in response.json().get("data", []):
        print(f"  {item.get('id')}  (ctx={item.get('max_model_len')})")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="生成 LiteLLM 配置（注入 WebVPN Cookie）。")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--list-upstream", action="store_true",
                        help="列出上游当前可用模型，然后退出")
    args = parser.parse_args()

    settings = load_proxy_settings()
    cookie_header = load_cookie_header()

    if args.list_upstream:
        list_upstream_models(settings, cookie_header)
        return

    models = load_models()
    content = render_config(settings, cookie_header, models)

    # 双保险：LiteLLM 用 GBK 读这个文件，非 ASCII 会崩。
    non_ascii = [c for c in content if ord(c) > 127]
    if non_ascii:
        raise SystemExit(f"生成内容含非 ASCII 字符，会导致 LiteLLM 启动失败: {non_ascii[:5]}")

    out_path = Path(args.out)
    out_path.write_text(content, encoding="utf-8")

    total = sum(len(m.get("aliases") or []) for m in models)
    print(f"已生成: {out_path}")
    print(f"  上游 URL : ...{build_target_url(settings['api_base'], settings['host_query'])[-70:]}")
    print(f"  Cookie   : {len(cookie_header)} 字符")
    print(f"  模型别名 : {total} 个")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_build_config.py -v
```
Expected: 6 passed

- [ ] **Step 5: 用真实数据生成配置**

```bash
uv run python build_litellm_config.py
```
Expected: 输出 `已生成: ...\litellm_config.yaml`，`模型别名 : 12 个`

- [ ] **Step 6: 验证生成物被 gitignore**

```bash
git check-ignore -v litellm_config.yaml
```
Expected: 输出包含 `litellm_config.yaml`（确认被忽略）。若没有输出，说明 `.gitignore` 有问题，**必须修好再继续**——该文件含实时 Cookie。

- [ ] **Step 7: 提交**

```bash
git add build_litellm_config.py tests/test_build_config.py
git commit -m "feat: config generator with WebVPN cookie injection

Two bridge flags are required: use_chat_completions_api (per-deployment)
and use_chat_completions_url_for_anthropic_messages (global). Without
both, Claude Code and Codex receive HTTP 400 from the upstream."
```

---

## Task 5: 服务器启动与端到端验证

**Files:**
- Create: `tests/smoke_test.py`

**Interfaces:**
- Consumes: Task 4 产出的 `litellm_config.yaml`
- Produces: 一个可复用的冒烟测试脚本，`python tests/smoke_test.py [--base-url URL]`，全部通过时 exit 0

- [ ] **Step 1: 写冒烟测试**

```python
"""UJN_LLM_API 端到端冒烟测试（需要真实上游与运行中的 LiteLLM）。

用法：
    uv run python tests/smoke_test.py
    uv run python tests/smoke_test.py --base-url http://127.0.0.1:4000
"""

import argparse
import json
import sys

import httpx

TOOLS = [
    {"name": "get_weather", "description": "Get weather for a city.",
     "input_schema": {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}},
    {"name": "get_time", "description": "Get time for a city.",
     "input_schema": {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}},
]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument("--chat-model", default="1.Qwen3.5-27B")
    parser.add_argument("--anthropic-model", default="claude-sonnet-4-5")
    parser.add_argument("--responses-model", default="gpt-5.1-codex")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=args.timeout, trust_env=False)
    results: list[bool] = []

    print("1. 健康检查")
    try:
        r = client.get(f"{base}/health/liveliness")
        results.append(check("health", r.status_code == 200))
    except Exception as exc:
        results.append(check("health", False, f"{type(exc).__name__}: {exc}"))

    print("2. OpenAI /v1/chat/completions")
    r = client.post(f"{base}/v1/chat/completions",
                    json={"model": args.chat_model, "max_tokens": 64,
                          "messages": [{"role": "user", "content": "hi"}]})
    results.append(check("chat completions", r.status_code == 200, f"HTTP {r.status_code}"))

    print("3. Anthropic /v1/messages + 并行工具调用")
    r = client.post(f"{base}/v1/messages",
                    json={"model": args.anthropic_model, "max_tokens": 600,
                          "tools": TOOLS, "tool_choice": {"type": "auto"},
                          "messages": [{"role": "user",
                                        "content": "weather and time in Jinan? use both tools"}]})
    ok = False
    detail = f"HTTP {r.status_code}"
    if r.status_code == 200:
        payload = r.json()
        uses = [b for b in payload.get("content") or [] if b.get("type") == "tool_use"]
        ok = payload.get("stop_reason") == "tool_use" and len(uses) >= 1
        detail = f"stop={payload.get('stop_reason')} tools={[b['name'] for b in uses]}"
    results.append(check("anthropic tools", ok, detail))

    print("4. Anthropic /v1/messages 流式 + 工具")
    saw_start = saw_json = False
    stop_reason = None
    with client.stream("POST", f"{base}/v1/messages",
                       json={"model": args.anthropic_model, "max_tokens": 600,
                             "tools": TOOLS, "tool_choice": {"type": "auto"},
                             "messages": [{"role": "user",
                                           "content": "weather in Jinan? use the tool"}],
                             "stream": True}) as r:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
                saw_start = True
            if (event.get("delta") or {}).get("type") == "input_json_delta":
                saw_json = True
            if etype == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason")
    results.append(check("anthropic stream tools", saw_start and saw_json and stop_reason == "tool_use",
                         f"start={saw_start} json_delta={saw_json} stop={stop_reason}"))

    print("5. Codex /v1/responses 流式 + 工具")
    names: list[str] = []
    arg_deltas = 0
    status = None
    body = {
        "model": args.responses_model, "stream": True, "store": False,
        "tool_choice": "auto", "include": ["reasoning.encrypted_content"],
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "weather and time in Jinan? use both tools"}]}],
        "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                   "parameters": t["input_schema"], "strict": False} for t in TOOLS],
    }
    with client.stream("POST", f"{base}/v1/responses", json=body) as r:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "response.completed":
                status = (event.get("response") or {}).get("status")
            if etype == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    names.append(item.get("name"))
            if etype == "response.function_call_arguments.delta":
                arg_deltas += 1
    results.append(check("codex responses tools", status == "completed" and bool(names),
                         f"status={status} calls={names} deltas={arg_deltas}"))

    print("6. Anthropic tool_result 回传")
    r = client.post(f"{base}/v1/messages",
                    json={"model": args.anthropic_model, "max_tokens": 400,
                          "tools": TOOLS[:1], "tool_choice": {"type": "auto"},
                          "messages": [
                              {"role": "user", "content": "weather in Jinan?"},
                              {"role": "assistant", "content": [
                                  {"type": "tool_use", "id": "toolu_1",
                                   "name": "get_weather", "input": {"city": "Jinan"}}]},
                              {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": "Sunny, 26C."}]},
                          ]})
    ok = False
    if r.status_code == 200:
        text = "".join(b.get("text") or "" for b in r.json().get("content") or []
                       if b.get("type") == "text")
        ok = bool(text.strip())
    results.append(check("tool_result roundtrip", ok, f"HTTP {r.status_code}"))

    passed = sum(1 for x in results if x)
    print(f"\n{passed}/{len(results)} passed")
    if passed < len(results):
        print("\n提示：若 anthropic/codex 的 tools 失败但 chat 通过，"
              "检查 litellm_config.yaml 里两个 bridge 开关是否都在。")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 启动服务器**

```bash
uv run litellm --config "$(pwd -W)/litellm_config.yaml" --port 4000 --host 127.0.0.1
```
Expected: 日志出现 `LiteLLM: Proxy initialized with Config, Set models:` 后列出全部别名。

> ⚠ 启动命令是 `litellm`（→ `litellm:run_server`）。不要用 `litellm-proxy`（那是客户端 TUI，不接受 `--config`），也不要用 `python -m litellm.proxy.proxy_server`（静默无操作，exit 0 但不启动）。

- [ ] **Step 3: 另开终端跑冒烟测试**

```bash
uv run python tests/smoke_test.py
```
Expected: 6 项全部 PASS，`6/6 passed`

- [ ] **Step 4: 提交**

```bash
git add tests/smoke_test.py
git commit -m "test: end-to-end smoke test for all four protocol endpoints"
```

---

## Task 6: 一键启动脚本

**Files:**
- Create: `run.ps1`

**Interfaces:**
- Consumes: `ujn_webvpn_login.py`、`build_litellm_config.py`
- Produces: 单命令启动全栈；后台每 30 分钟刷新 Cookie 并重新生成配置

- [ ] **Step 1: 写 `run.ps1`**

```powershell
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RefreshSeconds = 1800
$MaxLoginAttempts = 3
$Port = 4000

Set-Location $ProjectDir

# 本地直连，避免系统代理接管 127.0.0.1
$env:NO_PROXY = "localhost,127.0.0.1"
$env:no_proxy = "localhost,127.0.0.1"
$env:PYTHONIOENCODING = "utf-8"

function Invoke-WebVpnLogin {
    param([int] $MaxAttempts = 3)

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-Host "WebVPN login attempt $attempt/$MaxAttempts..."
        & uv run python .\ujn_webvpn_login.py --headless
        if ($LASTEXITCODE -eq 0) {
            Write-Host "WebVPN login succeeded."
            return $true
        }
        Write-Warning "WebVPN login failed (exit $LASTEXITCODE)."
        if ($attempt -lt $MaxAttempts) { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Update-LiteLlmConfig {
    & uv run python .\build_litellm_config.py
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to generate litellm_config.yaml"
    }
}

Write-Host "Starting UJN LLM API stack from $ProjectDir"

if (-not (Invoke-WebVpnLogin -MaxAttempts $MaxLoginAttempts)) {
    throw "Initial WebVPN login failed after $MaxLoginAttempts attempts."
}

Update-LiteLlmConfig

# 后台任务：定期刷新 Cookie 并重新生成配置。
# LiteLLM 支持 /health 热重载，但我们直接重启更稳妥 —— 见 README 的说明。
$refreshJob = Start-Job -Name "UJN-Cookie-Refresh" -ScriptBlock {
    param($ProjectDir, $RefreshSeconds, $MaxLoginAttempts)

    Set-Location $ProjectDir
    $env:NO_PROXY = "localhost,127.0.0.1"
    $env:no_proxy = "localhost,127.0.0.1"
    $env:PYTHONIOENCODING = "utf-8"

    while ($true) {
        Start-Sleep -Seconds $RefreshSeconds
        for ($attempt = 1; $attempt -le $MaxLoginAttempts; $attempt++) {
            Write-Output "Scheduled WebVPN refresh attempt $attempt/$MaxLoginAttempts..."
            & uv run python .\ujn_webvpn_login.py --headless
            if ($LASTEXITCODE -eq 0) {
                & uv run python .\build_litellm_config.py
                Write-Output "Cookie refreshed and config regenerated."
                break
            }
            if ($attempt -lt $MaxLoginAttempts) { Start-Sleep -Seconds 5 }
        }
    }
} -ArgumentList $ProjectDir, $RefreshSeconds, $MaxLoginAttempts

Write-Host "Started refresh job: $($refreshJob.Id)"
Write-Host "Starting LiteLLM proxy on http://127.0.0.1:$Port"
Write-Host "Press Ctrl+C to stop."

try {
    & uv run litellm --config "$ProjectDir\litellm_config.yaml" --port $Port --host 127.0.0.1
}
finally {
    Stop-Job -Id $refreshJob.Id -ErrorAction SilentlyContinue
    Remove-Job -Id $refreshJob.Id -Force -ErrorAction SilentlyContinue
}
```

- [ ] **Step 2: 验证脚本可执行**

```bash
powershell -ExecutionPolicy Bypass -File ./run.ps1
```
Expected: 登录成功 → 生成配置 → LiteLLM 在 4000 端口启动。Ctrl+C 后刷新任务被清理。

- [ ] **Step 3: 提交**

```bash
git add run.ps1
git commit -m "feat: one-command launcher with periodic cookie refresh"
```

---

## Task 7: 客户端配置

**Files:**
- Create: `clients/ccswitch.json`
- Create: `clients/opencode.json`

**Interfaces:**
- Consumes: Task 5 中运行的 4000 端口代理
- Produces: 可直接粘贴/复制到客户端的配置文件

- [ ] **Step 1: 写 `clients/ccswitch.json`**

LiteLLM 原生支持 Anthropic 协议，所以 Claude Code **可以直连**，不再需要 CC Switch 做协议转换。此文件仅用于 CC Switch 多供应商切换场景。

```json
{
  "apiKey": "dummy",
  "baseURL": "http://127.0.0.1:4000",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "Qwen3.5-27B",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "DeepSeek-V4.1-Flash",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-1",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "GLM-5.3",
    "NO_PROXY": "localhost,127.0.0.1",
    "no_proxy": "localhost,127.0.0.1"
  },
  "includeCoAuthoredBy": false,
  "meta": {
    "apiFormat": "anthropic"
  },
  "models": {
    "default": "claude-sonnet-4-5",
    "haiku": "claude-haiku-4-5",
    "opus": "claude-opus-4-1",
    "sonnet": "claude-sonnet-4-5"
  }
}
```

> 注意 `ANTHROPIC_BASE_URL` 是 **不带 `/v1`** 的根地址 —— LiteLLM 的 `/v1/messages` 挂在根下，客户端会自己补 `/v1`。这跟旧项目不同（旧项目靠 CC Switch 转换，所以填 `/v1`）。

- [ ] **Step 2: 写 `clients/opencode.json`**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ujn-llm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "UJN LLM",
      "options": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "dummy"
      },
      "models": {
        "GLM-5.3": { "name": "GLM-5.3" },
        "deepseek-v41-flash": { "name": "DeepSeek-V4.1-Flash" },
        "1.Qwen3.5-27B": { "name": "Qwen3.5-27B" },
        "Qwen3.6-27B": { "name": "Qwen3.6-27B" }
      }
    }
  }
}
```

- [ ] **Step 3: 手工验证 Claude Code 直连**

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 ANTHROPIC_AUTH_TOKEN=dummy claude
```
Expected: Claude Code 正常启动并能回答问题、调用工具。

- [ ] **Step 4: 提交**

```bash
git add clients/
git commit -m "feat: Claude Code and OpenCode client configs"
```

---

## Task 8: Codex 接入

**Files:**
- Modify: `~/.codex/config.toml`（用户主目录，不提交）
- Create: `clients/codex-config-snippet.toml`

**Interfaces:**
- Consumes: 4000 端口代理
- Produces: 可用的 Codex provider 配置

- [ ] **Step 1: 写配置片段 `clients/codex-config-snippet.toml`**

```toml
# 把以下内容合并进 ~/.codex/config.toml
#
# 关键：current Codex 只接受 wire_api = "responses"，
# "chat" 已被官方移除（会报 "wire_api = "chat" is no longer supported"）。
# LiteLLM 的 /v1/responses 正是为此准备的。

model = "gpt-5.1-codex"
model_provider = "ujn"

[model_providers.ujn]
name = "UJN LLM API"
base_url = "http://127.0.0.1:4000/v1"
env_key = "UJN_DUMMY_KEY"
wire_api = "responses"
request_max_retries = 3
stream_max_retries = 3
stream_idle_timeout_ms = 300000
```

- [ ] **Step 2: 设置环境变量**

```powershell
setx UJN_DUMMY_KEY "dummy"
```
（Codex 要求 `env_key` 指向的环境变量存在，值本身无意义——真正的 UJN key 已注入到 LiteLLM 配置里。）

- [ ] **Step 3: 合并配置并验证**

把 `clients/codex-config-snippet.toml` 的内容合并进 `~/.codex/config.toml`，然后：

```bash
codex exec "回复：PONG" --skip-git-repo-check
```
Expected: Codex 通过 LiteLLM 得到回复，不再 404/400。

- [ ] **Step 4: 验证工具调用**

```bash
codex exec "列出当前目录的文件" --skip-git-repo-check
```
Expected: Codex 调用 shell 工具并返回文件列表，证明 `/v1/responses` 的 function calling 通路正常。

- [ ] **Step 5: 提交**

```bash
git add clients/codex-config-snippet.toml
git commit -m "feat: Codex CLI provider config for responses API"
```

---

## Task 9: README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 面向用户的中英对照文档

- [ ] **Step 1: 写 `README.md`**

必须覆盖以下内容（中英对照，中文在前）：

1. **这是什么** —— 一句话：把 WebVPN 后的 ChatUJN 封装成本机多协议 LLM 接口。
2. **架构图**，明确三层：
   ```
   Claude Code / Codex / OpenCode / OpenAI SDK
     → http://127.0.0.1:4000
     → LiteLLM Proxy（协议转换 + 工具调用）
     → WebVPN Cookie + UJN API Key
     → ChatUJN (Open WebUI + vLLM)
   ```
3. **端点一览表**：

   | 端点 | 协议 | 用途 |
   |---|---|---|
   | `GET /health/liveliness` | — | 健康检查 |
   | `GET /v1/models` | OpenAI | 模型列表 |
   | `POST /v1/chat/completions` | OpenAI | OpenCode、OpenAI SDK |
   | `POST /v1/messages` | Anthropic | Claude Code |
   | `POST /v1/messages/count_tokens` | Anthropic | token 计数 |
   | `POST /v1/responses` | OpenAI Responses | Codex CLI |

4. **环境准备** —— `uv sync`、`uv run playwright install chromium`
5. **配置** —— 复制 `config.yaml.example`，说明每个字段，强调不要提交
6. **获取 WebVPN 地址** —— 从浏览器 DevTools 的 Network 里找 `POST /api/chat/completions`，拆成 `webvpn_api_base`（到 `/api` 结束）和 `webvpn_host_query`（`?` 之后）
7. **一键启动** —— `.\run.ps1`，说明它做四件事
8. **客户端接入** —— Claude Code（直连，`ANTHROPIC_BASE_URL` 不带 `/v1`）、Codex（`wire_api = "responses"`）、OpenCode
9. **工具调用** —— 已实测支持：并行工具、流式增量参数、`tool_result` 回传、`tool_choice: auto/any/tool`
10. **模型别名** —— 怎么改 `models.yaml`，用 `--list-upstream` 查上游当前模型
11. **常见问题**：
    - `400 'messages'` / `400 'created_at'` → **两个 bridge 开关缺了一个**，重新生成配置
    - `Model not found` → 上游模型下线了，跑 `--list-upstream` 看当前清单
    - 启动崩溃 `UnicodeDecodeError: 'gbk'` → `litellm_config.yaml` 里混入了非 ASCII 字符
    - `502` / `302` → Cookie 过期，重跑登录
    - Claude Code 报连接错误 → 确认 `ANTHROPIC_BASE_URL` 是 `http://127.0.0.1:4000`（**无** `/v1`）

- [ ] **Step 2: 校对无过时信息**

通读一遍，确认：没有出现 `F:\code\UJNTool`、没有 `xueman`、没有已下线的 `GLM-5___2-NVFP4`、端口统一为 4000。

```bash
grep -nE "F:\\\\code|xueman|GLM-5___2|8000" README.md || echo "clean"
```
Expected: `clean`

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: bilingual README covering setup, clients, and tool calling"
```

---

## Task 10: 清理与首次提交

**Files:**
- Verify: 仓库状态

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 干净的、可推送的仓库

- [ ] **Step 1: 确认无密钥文件被跟踪**

```bash
git status --porcelain
git ls-files | grep -E "config\.yaml$|litellm_config|state\.json" && echo "!!! 有密钥文件被跟踪" || echo "OK: 无密钥文件被跟踪"
```
Expected: `OK: 无密钥文件被跟踪`

- [ ] **Step 2: 全量测试通过**

```bash
uv run pytest tests/test_build_config.py -v
uv run python tests/smoke_test.py
```
Expected: 单元测试全过；冒烟测试 `6/6 passed`（需服务器运行中）

- [ ] **Step 3: 查看最终文件树**

```bash
git ls-files
```
Expected:
```
.gitignore
README.md
build_litellm_config.py
clients/ccswitch.json
clients/codex-config-snippet.toml
clients/opencode.json
config.yaml.example
models.yaml
pyproject.toml
run.ps1
tests/smoke_test.py
tests/test_build_config.py
ujn_webvpn_login.py
```

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "chore: final cleanup" || echo "nothing to commit"
```

---

## 可选后续（不在本计划范围）

- **日志查看**：LiteLLM 有内置 UI（`/ui`）和请求日志，可用于排查。
- **多账号/限流**：LiteLLM 支持 key 管理与预算，本机单用户场景暂不需要。
- **图片输入**：上游 `input_modalities` 只有 `text`，暂不支持视觉。
- **上游模型自动发现**：可由 `--list-upstream` 扩展为自动更新 `models.yaml`。
