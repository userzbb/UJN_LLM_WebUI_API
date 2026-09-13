import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_litellm_config import build_target_url, load_models, render_config

SETTINGS = {"api_base": "https://vpn.example.edu/api",
            "host_query": "host.q", "api_key": "sk-test"}


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
    config = yaml.safe_load(render_config(SETTINGS, "a=b; c=d", ["GLM-5.3", "Qwen3.6-27B"]))

    assert config["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"] is True
    for entry in config["model_list"]:
        assert entry["litellm_params"]["use_chat_completions_api"] is True


def test_render_config_uses_model_names_verbatim():
    """不做别名映射：model_name 就是上游原名，model 前缀是 hosted_vllm/。"""
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3", "/models/Qwen3.8-Flash-Next"]))

    names = [e["model_name"] for e in config["model_list"]]
    assert names == ["GLM-5.3", "/models/Qwen3.8-Flash-Next"]
    assert config["model_list"][0]["litellm_params"]["model"] == "hosted_vllm/GLM-5.3"


def test_render_config_does_not_route_openai_provider():
    """hosted_vllm 必须替代 openai：openai 在 _RESPONSES_API_PROVIDERS 里，
    带 thinking 的 /v1/messages 会被强制路由到上游不存在的 /responses 端点。"""
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3"]))

    for entry in config["model_list"]:
        assert not entry["litellm_params"]["model"].startswith("openai/")


def test_render_config_injects_cookie_and_browser_headers():
    config = yaml.safe_load(render_config(SETTINGS, "wengine_vpn_ticket=SECRET", ["GLM-5.3"]))
    headers = config["model_list"][0]["litellm_params"]["extra_headers"]

    assert headers["Cookie"] == "wengine_vpn_ticket=SECRET"
    assert headers["Origin"] == "https://webvpn.ujn.edu.cn"
    assert "Mozilla/5.0" in headers["User-Agent"]


def test_render_config_survives_quotes_in_header_values():
    """回归：Cookie / api_key 的取值来自服务端，可能含 `"`。

    手工拼接字符串会产出非法 YAML，导致 LiteLLM 启动时解析失败；
    改用 yaml.safe_dump 后必须能正确转义并原样取回。
    """
    settings = dict(SETTINGS, api_key='sk-with"quote')
    config = yaml.safe_load(render_config(settings, 'k=va"lue', ["GLM-5.3"]))
    params = config["model_list"][0]["litellm_params"]

    assert params["api_key"] == 'sk-with"quote'
    assert params["extra_headers"]["Cookie"] == 'k=va"lue'


def test_render_config_is_pure_ascii():
    """LiteLLM 用 GBK 读该文件，任何非 ASCII 都会导致启动崩溃。"""
    out = render_config(SETTINGS, "a=b", ["GLM-5.3"])

    non_ascii = [c for c in out if ord(c) > 127]
    assert not non_ascii, f"config must be ASCII-only, found {non_ascii[:5]}"


def test_render_config_unicode_false_when_non_ascii_model_name():
    """allow_unicode=False 保证输出纯 ASCII（配合上面的 GBK 约束）。"""
    out = render_config(SETTINGS, "a=b", ["/models/Qwen3.8-Flash-Next"])

    assert "Qwen3.8-Flash-Next" in out
    assert not [c for c in out if ord(c) > 127]


def test_load_models_reads_flat_name_list():
    """models.yaml 现在是扁平的名字列表，不再是 {upstream, aliases} 结构。"""
    names = load_models(Path(__file__).resolve().parent.parent / "models.yaml")

    assert names, "models.yaml should not be empty"
    assert all(isinstance(n, str) for n in names)
    assert "GLM-5.3" in names
