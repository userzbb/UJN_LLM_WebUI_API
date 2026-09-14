"""从 config.yaml + ujn_webvpn_state.json 生成 LiteLLM 的 litellm_config.yaml。

LiteLLM 负责全部协议转换（OpenAI / Anthropic / Responses）与工具调用，
本脚本只把 WebVPN 的 Cookie 和上游 URL 注入成 LiteLLM 的 provider 配置。

⚠ 生成的配置必须保持纯 ASCII：LiteLLM 用系统默认编码（Windows 上是 GBK）
   读取该文件，含非 ASCII 字符会导致启动时 UnicodeDecodeError。
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

from ujn_console import enable_utf8_stdout

# 路径段推导 / 主机名解析 / WebVPN 源站常量与登录脚本共用，放在 ujn_webvpn。
# 这里原名转出：WRD_CONSTANT 与 webvpn_path_segment 是本模块原有的公开名字，
# 外部（含既有测试）可能从 build_litellm_config 直接 import，不能因为搬家就断了。
from ujn_webvpn import (
    WEBVPN_ORIGIN,
    WRD_CONSTANT,
    extract_host_from_query,
    read_state_jwt,
    webvpn_path_segment,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
STATE_FILE = BASE_DIR / "ujn_webvpn_state.json"
MODELS_FILE = BASE_DIR / "models.yaml"
DEFAULT_OUT = BASE_DIR / "litellm_config.yaml"
CLIENTS_DIR = BASE_DIR / "clients"
DEFAULT_PORT = 4000

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

# 必须让 LiteLLM 绕过系统代理，否则关掉代理软件就 500。
#
# 实测根因（2026-09-14）：run.ps1 跑在 PowerShell 里，若 profile 设了
#   $env:HTTP_PROXY / $env:HTTPS_PROXY（FlClash 等工具很常见），
# LiteLLM 子进程会继承它们，把【所有】上游请求发给那个代理。
# 代理软件一关，7897 变成死地址，LiteLLM 仍往那儿发 -> 上游全部 500。
#
# 而 webvpn.ujn.edu.cn 解析到 202.194.65.6（国内教育网），实测直连 TLSv1.3 仅 0.2s，
# 根本不需要代理 —— 代理纯属多余，且是故障源。
#
# 用后缀 .ujn.edu.cn 而不是单个主机名：将来换 chat 之外的子系统也不必再改。
# 保持 localhost/127.0.0.1：LiteLLM 自己也要连本机。
NO_PROXY_VALUE = "localhost,127.0.0.1,.ujn.edu.cn"


def read_yaml_scalar(text: str, key: str) -> str | None:
    """从 YAML 文本里取一个标量值，嵌套任意深度都能找到。

    用真正的 YAML 解析器而不是正则：正则在合法写法上会悄悄出错。
    实测两类（README 的示例恰好踩中第一类）：
      - 行尾注释 `api_key: "sk-x"  # 注释` -> 旧正则返回 None，报「缺少 api_key」
      - 单引号   `api_key: 'sk-x'`        -> 旧正则连引号一起返回，密钥变成 "'sk-x'"
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"config.yaml 不是合法的 YAML: {exc}") from exc

    def dig(node: object) -> object:
        if isinstance(node, dict):
            if key in node:
                return node[key]
            for value in node.values():
                found = dig(value)
                if found is not None:
                    return found
        return None

    found = dig(data)
    return None if found is None else str(found)


def load_api_key(text: str, state_file: Path | None = None) -> str:
    """取上游 API Key（ChatUJN 的 JWT）。

    优先级：state 文件里的 jwt > config.yaml 的 api_key。

    state 优先的原因：ujn_webvpn_login.py 每次登录都会重新掏一份 JWT 写进 state，
    这条链路是自动的；config.yaml 里的 api_key 是手工抄的、会过期。
    反过来让 config.yaml 优先，等于手工值永远压着自动值，自动化形同虚设。

    config.yaml 的 api_key 仍然保留 —— 它是「没跑过登录脚本 / state 被删了」时的兜底。

    两者都存在且不同时告警：用户可能刚换过 JWT，需要知道实际用的是哪一份。

    state_file 默认 None 表示「运行时取模块常量 STATE_FILE」。
    不能写成 `state_file: Path = STATE_FILE` —— 默认参数在函数定义时求值，
    之后改 STATE_FILE 常量对它无效，测试没法替换路径。
    """
    if state_file is None:
        state_file = STATE_FILE

    from_state = read_state_jwt(state_file)
    from_config = read_yaml_scalar(text, "api_key")

    if from_state and from_config and from_state != from_config:
        print(
            "提示：state 文件里的 JWT 与 config.yaml 的 api_key 不一致，"
            "本次使用 state 里的（ujn_webvpn_login.py 自动提取）。\n"
            "      若想改用 config.yaml 那份，请删掉 state 文件里的 jwt 字段，"
            "或重跑登录脚本。",
            file=sys.stderr,
        )

    api_key = from_state or from_config
    if not api_key:
        raise SystemExit(
            "没有可用的 API Key。请先运行 ujn_webvpn_login.py 自动提取 JWT，\n"
            "或在 config.yaml 里填 api_key（ChatUJN 的 JWT 令牌，不带 Bearer 前缀）。"
        )
    return api_key


def load_proxy_settings(
    config_file: Path = CONFIG_FILE, state_file: Path | None = None
) -> dict[str, str]:
    if not config_file.exists():
        raise SystemExit(f"{config_file.name} 不存在，请先复制 config.yaml.example")
    text = config_file.read_text(encoding="utf-8")

    api_base = read_yaml_scalar(text, "webvpn_api_base")
    host_query = read_yaml_scalar(text, "webvpn_host_query")

    if not api_base:
        raise SystemExit("config.yaml 缺少 webvpn_api_base")

    api_key = load_api_key(text, state_file=state_file)

    # 允许 webvpn_api_base 里留 <opaque> 占位符：路径段是可推导的，
    # 直接用 host_query 里的主机名算出来，省得用户去浏览器 F12 里抄。
    if "<opaque>" in api_base:
        if not host_query:
            raise SystemExit(
                "webvpn_api_base 里是 <opaque>，但没有 webvpn_host_query 可供推导。\n"
                "请填上 webvpn_host_query（形如 vpn-12-o2-chat.ujn.edu.cn）。"
            )
        host = extract_host_from_query(host_query)
        api_base = api_base.replace("<opaque>", webvpn_path_segment(host))

    return {
        "api_base": api_base.rstrip("/"),
        "host_query": host_query or "",
        "api_key": api_key,
    }


def load_cookie_header(state_file: Path | None = None) -> str:
    # 同 load_api_key：默认参数不能用 STATE_FILE，否则定义时求值、无法替换。
    if state_file is None:
        state_file = STATE_FILE

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


def load_models(models_file: Path = MODELS_FILE) -> list[str]:
    """读取上游模型 id 列表。直接以原名对外暴露，不做别名映射。"""
    if not models_file.exists():
        raise SystemExit(f"{models_file.name} 不存在")
    data = yaml.safe_load(models_file.read_text(encoding="utf-8")) or {}
    models = [str(m).strip() for m in (data.get("models") or []) if str(m).strip()]
    if not models:
        raise SystemExit("models.yaml 里没有任何模型定义")
    return models


def merge_model_ids(current: list[str], upstream: list[str]) -> tuple[list[str], list[str], list[str]]:
    """把上游清单并入当前清单，返回 (合并结果, 新增, 消失)。

    models.yaml 的语义是「当前可调用的模型」：上游没有的必须删掉，
    否则代理会对外暴露一个必然 `Model not found` 的名字。

    保留存活模型的当前顺序、新模型追加在末尾（而不是直接用上游顺序）：
    这样每次同步的 diff 最小，review 时一眼能看出到底变了哪几个。
    """
    added = [m for m in upstream if m not in current]
    removed = [m for m in current if m not in upstream]
    merged = [m for m in current if m in upstream] + added
    return merged, added, removed


def render_models_yaml(models: list[str], snapshot_date: str) -> str:
    """生成 models.yaml 文本。

    用 yaml.safe_dump 而不是手工拼 `- {name}`：模型名来自服务端，
    可能含引号等字符，手工插值会产出非法 YAML（同 render_config 的处理）。
    """
    header = (
        "# 上游模型 id 列表。直接以原名对外暴露，不做别名映射。\n"
        "# 客户端（Claude Code / Codex / OpenCode）填的就是这里的名字。\n"
        f"# 本文件由 `uv run python build_litellm_config.py --sync-models` 生成。\n"
        f"# 快照时间: {snapshot_date} —— 上游会更新，此列表可能已过期。\n"
    )
    return header + yaml.safe_dump({"models": models}, allow_unicode=False, sort_keys=False)


def sync_models_file(upstream: list[str], models_file: Path, snapshot_date: str) -> tuple[list[str], list[str], list[str]]:
    """按上游清单更新 models.yaml。

    内容没变化时不写文件：避免每次同步都刷新 mtime、产生无意义的 git diff。
    返回 (合并结果, 新增, 消失)，供调用方打印。
    """
    current: list[str] = []
    if models_file.exists():
        data = yaml.safe_load(models_file.read_text(encoding="utf-8")) or {}
        current = [str(m).strip() for m in (data.get("models") or []) if str(m).strip()]

    merged, added, removed = merge_model_ids(current, upstream)
    if not added and not removed:
        return merged, added, removed

    models_file.write_text(render_models_yaml(merged, snapshot_date), encoding="utf-8")
    return merged, added, removed


def build_target_url(api_base: str, host_query: str) -> str:
    """构造上游 URL。

    ⚠ /chat/completions 必须出现在 query string 之前 —— WebVPN 按路径路由。
    实测：query 在前会 405；在后会 200。
    """
    url = f"{api_base}/chat/completions"
    if host_query:
        url = f"{url}?{host_query}"
    return url


# --- 客户端范本生成 ---------------------------------------------------------
# 范本从 models.yaml 派生，避免手写后与真实模型清单漂移（历史上就漂移过：
# ccswitch.json 缺了 FABLE 档与 [1M] 后缀，opencode.json 少一个模型）。

# 四档默认值。两个都是实测最快且 1M 上下文的模型。
TIER_FABLE  = "deepseek-v41-flash"
TIER_OPUS   = "deepseek-v41-flash"
TIER_SONNET = "GLM-5.3-Flash"
TIER_HAIKU  = "GLM-5.3-Flash"

# Claude Code 需要 [1M] 后缀才会把上下文按 1M 计（否则按 200k 提前 auto-compact）。
# 其他客户端不能带 —— 它们原样透传，上游查不到该名字会 400。
CLAUDE_SUFFIX = "[1M]"


def pick_tier_models(models: list[str]) -> dict[str, str]:
    """为四个档位挑模型；首选模型不在清单里时退回第一个可用的。"""
    if not models:
        raise SystemExit("models 为空，无法挑选档位模型")
    fallback = models[0]

    def choose(preferred: str) -> str:
        return preferred if preferred in models else fallback

    return {
        "fable":  choose(TIER_FABLE),
        "opus":   choose(TIER_OPUS),
        "sonnet": choose(TIER_SONNET),
        "haiku":  choose(TIER_HAIKU),
    }


def render_claude_settings(models: list[str], port: int = 4000) -> str:
    """Claude Code 的 settings.json 范本。

    BASE_URL 不带 /v1（带了会变成 /v1/v1/messages -> 404）。
    四个档位都带 [1M]。
    """
    tiers = pick_tier_models(models)
    base = f"http://127.0.0.1:{port}"

    settings = {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_DEFAULT_FABLE_MODEL":  tiers["fable"]  + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_OPUS_MODEL":   tiers["opus"]   + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": tiers["sonnet"] + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL":  tiers["haiku"]  + CLAUDE_SUFFIX,
            "ANTHROPIC_MODEL": tiers["fable"] + CLAUDE_SUFFIX,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
    }
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def render_ccswitch(models: list[str], port: int = 4000) -> str:
    """CC Switch 的供应商配置范本（供 Claude Code 多供应商切换时用）。"""
    tiers = pick_tier_models(models)
    base = f"http://127.0.0.1:{port}"

    cfg = {
        "name": "ujn-llm",
        "apiKey": "dummy",
        "baseURL": base,
        "meta": {"apiFormat": "anthropic"},
        "models": {
            "default": tiers["fable"],
            "fable":  tiers["fable"],
            "opus":   tiers["opus"],
            "sonnet": tiers["sonnet"],
            "haiku":  tiers["haiku"],
        },
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_DEFAULT_FABLE_MODEL":  tiers["fable"]  + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_OPUS_MODEL":   tiers["opus"]   + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": tiers["sonnet"] + CLAUDE_SUFFIX,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL":  tiers["haiku"]  + CLAUDE_SUFFIX,
            "ANTHROPIC_MODEL": tiers["fable"] + CLAUDE_SUFFIX,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        },
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def render_codex_toml(models: list[str], port: int = 4000) -> str:
    """Codex 的 config.toml 片段。

    与 Claude Code 相反：base_url 要带 /v1，模型名不能带 [1M]。
    Codex 只接受 wire_api = "responses"（"chat" 已被官方移除）。
    """
    tiers = pick_tier_models(models)
    model = tiers["fable"]

    return f'''# 把以下内容合并进 ~/.codex/config.toml
#
# 关键：current Codex 只接受 wire_api = "responses"，
# "chat" 已被官方移除（会报 "wire_api = "chat" is no longer supported"）。
# LiteLLM 的 /v1/responses 正是为此准备的。
#
# model 填上游真实模型名（见 models.yaml），不要填 gpt-*/claude-* 之类的别名。
# 【不要】加 [1M] 后缀 —— 那是 Claude Code 专用标记，Codex 会原样透传导致 400。

model = "{model}"
model_provider = "ujn"

[model_providers.ujn]
name = "UJN LLM API"
base_url = "http://127.0.0.1:{port}/v1"
env_key = "UJN_DUMMY_KEY"
wire_api = "responses"
request_max_retries = 3
stream_max_retries = 3
stream_idle_timeout_ms = 300000
'''


def render_opencode(models: list[str], port: int = 4000) -> str:
    """OpenCode 配置范本，覆盖 models.yaml 全部模型。"""
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "ujn-llm": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "UJN LLM",
                "options": {
                    "baseURL": f"http://127.0.0.1:{port}/v1",
                    "apiKey": "dummy",
                },
                "models": {m: {"name": m} for m in models},
            }
        },
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def emit_client_templates(models: list[str], out_dir: Path, port: int = 4000) -> list[Path]:
    """把四份客户端范本写到 out_dir，返回写出的文件列表。

    内容不变时不重写（保持 mtime 稳定，避免无意义的 diff 抖动）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "claude-settings.json":      render_claude_settings(models, port),
        "ccswitch.json":             render_ccswitch(models, port),
        "codex-config-snippet.toml": render_codex_toml(models, port),
        "opencode.json":             render_opencode(models, port),
    }

    written: list[Path] = []
    for name, content in files.items():
        path = out_dir / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def render_config(settings: dict[str, str], cookie_header: str, models: list[str]) -> str:
    """生成 LiteLLM 配置文本。纯函数，不联网、不读文件。

    模型直接以原名对外暴露（model_name 就是上游 id），不做别名映射。

    用 yaml.safe_dump 而不是手工拼字符串：Cookie / api_key 的取值来自服务端，
    可能包含 `"` 等字符；手工插值会产出非法 YAML，导致 LiteLLM 启动时解析失败。
    """
    url = build_target_url(settings["api_base"], settings["host_query"])

    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = cookie_header

    model_list: list[dict] = []
    for upstream in models:
        model_list.append(
            {
                "model_name": upstream,
                "litellm_params": {
                    # 用 hosted_vllm 而不是 openai：LiteLLM 内部有
                    #   _RESPONSES_API_PROVIDERS = frozenset({"openai"})
                    # 只要是 openai provider，带 thinking 的 /v1/messages 请求就会被强制
                    # 路由到上游的 /responses 端点（本项目上游没有该端点，直接 400）。
                    # hosted_vllm 不在该集合中，且语义上更贴合真实后端（vLLM）。
                    "model": f"hosted_vllm/{upstream}",
                    "api_base": url,
                    "api_key": settings["api_key"],
                    # 开关 1：强制把 /v1/responses 桥接到上游的 chat/completions。
                    # 不加这条，Codex 的请求会把 input 直接发给上游 -> KeyError 'messages'。
                    "use_chat_completions_api": True,
                    "extra_headers": dict(headers),
                    # 丢弃 reasoning_effort：上游对取值挑食（deepseek 拒绝 medium），
                    # 且 Codex 的 reasoning={effort,summary} 会被 LiteLLM 整份 dict
                    # 塞进 reasoning_effort，直接 400。丢弃后上游用自身默认推理强度。
                    "additional_drop_params": ["reasoning_effort"],
                },
            }
        )

    config = {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            # 开关 2：强制 /v1/messages 走 chat/completions 而非 Responses 适配器。
            # 不加这条，Claude Code 的请求会 KeyError 'created_at'。
            "use_chat_completions_url_for_anthropic_messages": True,
            "merge_reasoning_content_in_choices": True,
        },
        "general_settings": {
            # 本机代理：不做鉴权。关掉鉴权的关键是 master_key 为 null
            # （LiteLLM 没有 "disable_auth" 这个字段——写上去是静默无效的）。
            # 真正的 UJN API Key 在上面按部署注入，客户端填 dummy 即可。
            "master_key": None,
        },
    }

    return yaml.safe_dump(config, allow_unicode=False, sort_keys=False, default_flow_style=False)


def fetch_upstream_models(settings: dict[str, str], cookie_header: str) -> list[dict]:
    """拉取上游的模型条目（含 max_model_len），保持上游返回顺序。"""
    import httpx

    url = build_target_url(settings["api_base"], settings["host_query"])
    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = cookie_header
    headers["Authorization"] = f"Bearer {settings['api_key']}"

    models_url = url.replace("/chat/completions", "/models")
    response = httpx.get(models_url, headers=headers, timeout=60, trust_env=False)
    response.raise_for_status()
    return list(response.json().get("data", []))


def list_upstream_models(settings: dict[str, str], cookie_header: str) -> None:
    """打印上游当前可用的模型 id（排查「模型下线」用）。"""
    for item in fetch_upstream_models(settings, cookie_header):
        print(f"  {item.get('id')}  (ctx={item.get('max_model_len')})")


def main() -> None:
    enable_utf8_stdout()

    parser = argparse.ArgumentParser(description="生成 LiteLLM 配置（注入 WebVPN Cookie）。")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--models-file", default=str(MODELS_FILE))
    parser.add_argument("--clients-dir", default=str(CLIENTS_DIR),
                        help="客户端范本的输出目录（默认 clients/）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="写进客户端范本的代理端口（默认 4000）")
    parser.add_argument("--list-upstream", action="store_true",
                        help="列出上游当前可用模型，然后退出")
    parser.add_argument("--sync-models", action="store_true",
                        help="按上游清单更新 models.yaml（增/删都会报告），然后退出")
    parser.add_argument("--webvpn-path", metavar="HOST",
                        help="由主机名推导 WebVPN 路径段（形如 chat.ujn.edu.cn），然后退出。"
                             "不需要 config.yaml")
    args = parser.parse_args()

    # 放在读 config.yaml 之前：这个查询是纯计算的，不该要求先配好凭据。
    if args.webvpn_path:
        host = extract_host_from_query(args.webvpn_path)
        print(f"host : {host}")
        print(f"path : https://webvpn.ujn.edu.cn/https/{webvpn_path_segment(host)}/api")
        print()
        print("提示：config.yaml 里可把 webvpn_api_base 写成带 <opaque> 的形式，")
        print("      脚本会根据 webvpn_host_query 自动填上这一段。")
        return

    settings = load_proxy_settings()
    cookie_header = load_cookie_header()

    if args.list_upstream:
        list_upstream_models(settings, cookie_header)
        return

    if args.sync_models:
        items = fetch_upstream_models(settings, cookie_header)
        upstream = [str(i.get("id")).strip() for i in items if str(i.get("id") or "").strip()]
        if not upstream:
            raise SystemExit("上游没有返回任何模型，未改动 models.yaml")

        models_file = Path(args.models_file)
        merged, added, removed = sync_models_file(upstream, models_file, date.today().isoformat())

        for name in added:
            print(f"  + {name}")
        for name in removed:
            print(f"  - {name}")
        if not added and not removed:
            print(f"{models_file.name} 已是最新（{len(merged)} 个模型），未改动")
        else:
            print(f"已更新 {models_file.name}: {len(merged)} 个模型")
            print("下一步：重新生成配置并重启代理")
            print("  uv run python build_litellm_config.py")
        return

    models = load_models()
    content = render_config(settings, cookie_header, models)

    # 双保险：LiteLLM 用 GBK 读这个文件，非 ASCII 会崩。
    non_ascii = [c for c in content if ord(c) > 127]
    if non_ascii:
        raise SystemExit(f"生成内容含非 ASCII 字符，会导致 LiteLLM 启动失败: {non_ascii[:5]}")

    out_path = Path(args.out)
    out_path.write_text(content, encoding="utf-8")

    print(f"已生成: {out_path}")
    print(f"  上游 URL : ...{build_target_url(settings['api_base'], settings['host_query'])[-70:]}")
    print(f"  Cookie   : {len(cookie_header)} 字符")
    print(f"  模型数   : {len(models)} 个")

    # 客户端范本跟着 models.yaml 一起更新，避免手写后漂移。
    client_dir = Path(args.clients_dir)
    written = emit_client_templates(models, client_dir, port=args.port)
    print(f"  客户端范本: {client_dir.name}/ 下 {len(written)} 个文件（模型清单已同步）")


if __name__ == "__main__":
    main()
