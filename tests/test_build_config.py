import sys
from pathlib import Path

import yaml

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
    config = yaml.safe_load(render_config(settings, "a=b; c=d", models))

    assert config["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"] is True
    for entry in config["model_list"]:
        assert entry["litellm_params"]["use_chat_completions_api"] is True


def test_render_config_injects_cookie_and_browser_headers():
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3"]}]
    config = yaml.safe_load(render_config(settings, "wengine_vpn_ticket=SECRET", models))
    headers = config["model_list"][0]["litellm_params"]["extra_headers"]

    assert headers["Cookie"] == "wengine_vpn_ticket=SECRET"
    assert headers["Origin"] == "https://webvpn.ujn.edu.cn"
    assert "Mozilla/5.0" in headers["User-Agent"]


def test_render_config_survives_quotes_in_header_values():
    """回归：Cookie / api_key 的取值来自服务端，可能含 `"`。

    手工拼接字符串会产出非法 YAML，导致 LiteLLM 启动时解析失败；
    改用 yaml.safe_dump 后必须能正确转义并原样取回。
    """
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": 'sk-with"quote'}
    models = [{"upstream": "GLM-5.3", "aliases": ["GLM-5.3"]}]
    config = yaml.safe_load(render_config(settings, 'k=va"lue', models))
    params = config["model_list"][0]["litellm_params"]

    assert params["api_key"] == 'sk-with"quote'
    assert params["extra_headers"]["Cookie"] == 'k=va"lue'


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
    config = yaml.safe_load(render_config(settings, "a=b", models))

    names = [e["model_name"] for e in config["model_list"]]
    assert names == ["GLM-5.3", "claude-opus-4-1"]


def test_render_config_unicode_false_when_ascii():
    """allow_unicode=False 保证输出纯 ASCII（配合上面的 GBK 约束）。"""
    settings = {"api_base": "https://vpn.example.edu/api",
                "host_query": "host.q", "api_key": "sk-test"}
    models = [{"upstream": "/models/Qwen3.8-Flash-Next", "aliases": ["/models/Qwen3.8-Flash-Next"]}]
    out = render_config(settings, "a=b", models)

    assert "Qwen3.8-Flash-Next" in out
    assert not [c for c in out if ord(c) > 127]
