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


# --- read_yaml_scalar: 必须与真正的 YAML 解析一致 ---------------------------
# 背景：这个正则解析器决定能否读出 config.yaml 里的 api_key / api_base。
# README 的示例恰好用了「双引号 + 行尾注释」这一组合，是最容易被用户抄到的写法。

def _yaml_get(text: str, key: str):
    """用真正的 YAML 解析器取嵌套任意深度的键值，作为对照标准。"""
    def dig(node):
        if isinstance(node, dict):
            if key in node:
                return node[key]
            for value in node.values():
                found = dig(value)
                if found is not None:
                    return found
        return None
    return dig(yaml.safe_load(text))


def test_read_yaml_scalar_handles_trailing_comment():
    """行尾注释是合法 YAML，且 README 示例就是这么写的。

    回归：旧正则 `([^"\n]+?)$` 要求值一直延伸到行尾，遇到注释返回 None，
    于是 load_proxy_settings 在一个完全正确的配置上报「缺少 api_key」并退出。
    """
    from build_litellm_config import read_yaml_scalar

    text = 'proxy:\n  api_key: "sk-real"   # 我的密钥\n'
    assert read_yaml_scalar(text, "api_key") == "sk-real"
    assert read_yaml_scalar(text, "api_key") == _yaml_get(text, "api_key")


def test_read_yaml_scalar_strips_single_quotes():
    """单引号是合法 YAML。旧正则只处理双引号，会把引号当值的一部分。

    后果：密钥变成 `'sk-real'`（含引号）发给上游 -> 401。
    """
    from build_litellm_config import read_yaml_scalar

    text = "proxy:\n  api_key: 'sk-real'\n"
    assert read_yaml_scalar(text, "api_key") == "sk-real"
    assert read_yaml_scalar(text, "api_key") == _yaml_get(text, "api_key")


def test_read_yaml_scalar_matches_real_yaml_parser():
    """与本文件其余部分一样，用真正的 YAML 解析器当标准答案。"""
    from build_litellm_config import read_yaml_scalar

    variants = [
        'proxy:\n  api_key: "sk-a"\n',
        "proxy:\n  api_key: 'sk-a'\n",
        "proxy:\n  api_key: sk-a\n",
        'proxy:\n  api_key: "sk-a"  # comment\n',
        "proxy:\n  api_key: 'sk-a'  # comment\n",
        'proxy:\n  api_key: "sk-a b c"\n',
        'proxy:\n  api_key: "sk-a#b"\n',
        'a:\n  b:\n    api_key: "sk-deep"\n',
    ]
    for text in variants:
        got = read_yaml_scalar(text, "api_key")
        want = _yaml_get(text, "api_key")
        assert got == want, f"{text!r}: got {got!r}, want {want!r}"


def test_load_proxy_settings_accepts_readme_style_config(tmp_path):
    """把 README 的写法填上真值后必须能正常读出——这正是用户会做的第一步。"""
    from build_litellm_config import load_proxy_settings

    config = tmp_path / "config.yaml"
    config.write_text(
        'username: "u"          # 学号\n'
        'password: "p"\n'
        "proxy:\n"
        '  api_key: "sk-real"    # ChatUJN 的 API Key\n'
        '  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/abc/api"   # 见 README\n'
        '  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"\n',
        encoding="utf-8",
    )

    settings = load_proxy_settings(config)
    assert settings["api_key"] == "sk-real"
    assert settings["api_base"] == "https://webvpn.ujn.edu.cn/https/abc/api"
    assert settings["host_query"] == "vpn-12-o2-chat.ujn.edu.cn"


# --- --sync-models: 让 models.yaml 跟上游保持一致 ---------------------------

def test_merge_model_ids_reports_additions_and_removals():
    from build_litellm_config import merge_model_ids

    merged, added, removed = merge_model_ids(
        current=["keep", "gone", "also-keep"],
        upstream=["keep", "also-keep", "brand-new"],
    )

    # 保留现有顺序，新模型追加在后面 —— 这样每次同步的 diff 最小
    assert merged == ["keep", "also-keep", "brand-new"]
    assert added == ["brand-new"]
    assert removed == ["gone"]


def test_merge_model_ids_is_a_no_op_when_already_in_sync():
    from build_litellm_config import merge_model_ids

    same = ["a", "b", "c"]
    merged, added, removed = merge_model_ids(same, list(same))

    assert (merged, added, removed) == (same, [], [])


def test_merge_model_ids_handles_empty_current():
    """models.yaml 被清空或在全新环境首次同步。"""
    from build_litellm_config import merge_model_ids

    merged, added, removed = merge_model_ids([], ["x", "y"])

    assert merged == ["x", "y"]
    assert added == ["x", "y"]
    assert removed == []


def test_render_models_yaml_round_trips_through_load_models(tmp_path):
    """生成的 models.yaml 必须能被 load_models 原样读回。"""
    from build_litellm_config import render_models_yaml

    path = tmp_path / "models.yaml"
    path.write_text(render_models_yaml(["GLM-5.3", "/models/Qwen3.8-Flash-Next", "1.Qwen3.5-27B"],
                                       "2026-09-14"), encoding="utf-8")

    assert load_models(path) == ["GLM-5.3", "/models/Qwen3.8-Flash-Next", "1.Qwen3.5-27B"]


def test_render_models_yaml_survives_hostile_model_name(tmp_path):
    """模型名来自服务端，可能含引号等字符。

    手工拼 `- {name}` 会产出非法 YAML；走 yaml.safe_dump 必须能安全转义。
    （同 render_config 里 Cookie 的处理思路。）
    """
    from build_litellm_config import render_models_yaml

    nasty = 'weird"name: [x]'
    path = tmp_path / "models.yaml"
    path.write_text(render_models_yaml(["ok", nasty], "2026-09-14"), encoding="utf-8")

    assert load_models(path) == ["ok", nasty]


def test_render_models_yaml_records_the_snapshot_date(tmp_path):
    """上游会变，文件里必须留下「这是哪天的快照」。"""
    from build_litellm_config import render_models_yaml

    text = render_models_yaml(["GLM-5.3"], "2026-09-14")

    assert "2026-09-14" in text


def test_sync_models_does_not_write_when_nothing_changed(tmp_path):
    """已同步时不应改动文件 —— 避免每次同步都产生无意义的 mtime/diff 抖动。"""
    from build_litellm_config import sync_models_file

    path = tmp_path / "models.yaml"
    original = 'models:\n  - a\n  - b\n'
    path.write_text(original, encoding="utf-8")

    merged, added, removed = sync_models_file(["a", "b"], path, "2026-09-14")

    assert (added, removed) == ([], [])
    assert path.read_text(encoding="utf-8") == original


def test_sync_models_writes_new_file_when_changed(tmp_path):
    from build_litellm_config import sync_models_file

    path = tmp_path / "models.yaml"
    path.write_text("models:\n  - old\n", encoding="utf-8")

    merged, added, removed = sync_models_file(["old", "new"], path, "2026-09-14")

    assert added == ["new"] and removed == []
    assert load_models(path) == ["old", "new"]


# --- WebVPN 路径段：可从主机名推导，不是密钥 --------------------------------

def test_webvpn_path_segment_matches_the_live_config():
    """金标准：这个值来自真实 config.yaml，独立核对过。

    路径段 = "wrdvpnisthebest!" + AES-CTR(host)，密钥/IV 都是那个公开常量。
    它只取决于主机名，不含任何账号/Cookie 信息，因此不是凭据。
    """
    from build_litellm_config import webvpn_path_segment

    assert webvpn_path_segment("chat.ujn.edu.cn") == (
        "77726476706e69737468656265737421f3ff40886925625e300d8db9d6562d"
    )


def test_webvpn_path_segment_is_deterministic():
    """同一主机名永远得到同一结果 —— 没有任何随机/会话成分。"""
    from build_litellm_config import webvpn_path_segment

    assert webvpn_path_segment("chat.ujn.edu.cn") == webvpn_path_segment("chat.ujn.edu.cn")


def test_webvpn_path_segment_differs_per_host():
    from build_litellm_config import webvpn_path_segment

    assert webvpn_path_segment("chat.ujn.edu.cn") != webvpn_path_segment("other.ujn.edu.cn")


def test_webvpn_path_segment_starts_with_the_public_constant():
    """前 16 字节是 ASCII 常量 —— 这正说明它不是随机密钥。"""
    from build_litellm_config import webvpn_path_segment

    segment = webvpn_path_segment("chat.ujn.edu.cn")

    assert bytes.fromhex(segment)[:16] == b"wrdvpnisthebest!"
    assert segment.isalnum() and segment == segment.lower()


def test_extract_host_from_query_strips_vpn_prefix():
    """host_query 形如 vpn-12-o2-chat.ujn.edu.cn，真实主机名在后面。"""
    from build_litellm_config import extract_host_from_query

    assert extract_host_from_query("vpn-12-o2-chat.ujn.edu.cn") == "chat.ujn.edu.cn"


def test_extract_host_from_query_passes_through_plain_host():
    """已经是裸主机名时原样返回，别把开头的字母吃掉。"""
    from build_litellm_config import extract_host_from_query

    assert extract_host_from_query("chat.ujn.edu.cn") == "chat.ujn.edu.cn"
