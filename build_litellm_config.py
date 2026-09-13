"""从 config.yaml + ujn_webvpn_state.json 生成 LiteLLM 的 litellm_config.yaml。

LiteLLM 负责全部协议转换（OpenAI / Anthropic / Responses）与工具调用，
本脚本只把 WebVPN 的 Cookie 和上游 URL 注入成 LiteLLM 的 provider 配置。

⚠ 生成的配置必须保持纯 ASCII：LiteLLM 用系统默认编码（Windows 上是 GBK）
   读取该文件，含非 ASCII 字符会导致启动时 UnicodeDecodeError。
"""

import argparse
import json
import re
from pathlib import Path

import yaml

from ujn_console import enable_utf8_stdout

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
    """生成 LiteLLM 配置文本。纯函数，不联网、不读文件。

    用 yaml.safe_dump 而不是手工拼字符串：Cookie / api_key 的取值来自服务端，
    可能包含 `"` 等字符；手工插值会产出非法 YAML，导致 LiteLLM 启动时解析失败。
    """
    url = build_target_url(settings["api_base"], settings["host_query"])

    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = cookie_header

    model_list: list[dict] = []
    for entry in models:
        upstream = entry.get("upstream")
        if not upstream:
            continue
        for alias in entry.get("aliases") or []:
            model_list.append(
                {
                    "model_name": alias,
                    "litellm_params": {
                        "model": f"openai/{upstream}",
                        "api_base": url,
                        "api_key": settings["api_key"],
                        # 开关 1：强制把 /v1/responses 桥接到上游的 chat/completions。
                        # 不加这条，Codex 的请求会把 input 直接发给上游 -> KeyError 'messages'。
                        "use_chat_completions_api": True,
                        "extra_headers": dict(headers),
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
            # Local-only proxy. The real UJN API key is injected above.
            "disable_auth": True,
            "master_key": None,
        },
    }

    return yaml.safe_dump(config, allow_unicode=False, sort_keys=False, default_flow_style=False)


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
    enable_utf8_stdout()

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
