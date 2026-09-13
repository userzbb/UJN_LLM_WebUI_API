"""从 config.yaml + ujn_webvpn_state.json 生成 LiteLLM 的 litellm_config.yaml。

LiteLLM 负责全部协议转换（OpenAI / Anthropic / Responses）与工具调用，
本脚本只把 WebVPN 的 Cookie 和上游 URL 注入成 LiteLLM 的 provider 配置。

⚠ 生成的配置必须保持纯 ASCII：LiteLLM 用系统默认编码（Windows 上是 GBK）
   读取该文件，含非 ASCII 字符会导致启动时 UnicodeDecodeError。
"""

import argparse
import json
from datetime import date
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
    parser.add_argument("--list-upstream", action="store_true",
                        help="列出上游当前可用模型，然后退出")
    parser.add_argument("--sync-models", action="store_true",
                        help="按上游清单更新 models.yaml（增/删都会报告），然后退出")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
