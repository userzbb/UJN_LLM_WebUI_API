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
