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
    """把 README 的写法填上真值后必须能正常读出——这正是用户会做的第一步。

    必须显式传 state_file 指向不存在的文件：本用例验证的是【纯 config.yaml】
    这条路径，而真实 state 文件在开发机上通常存在且含 JWT，会盖过 api_key。
    不隔离的话，这个测试的通过与否取决于跑测试的机器 —— 那是最糟的一类测试。
    """
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

    settings = load_proxy_settings(config, state_file=tmp_path / "no-state.json")
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


def test_auto_sync_removes_models_disabled_by_upstream(tmp_path, capsys, monkeypatch):
    """生成配置前的自动同步必须能【删除】上游已下线的模型。

    实测背景（2026-09）：上游禁用了 deepseek-v41-flash / Qwen3.8-27B，
    本地 models.yaml 若不跟着删，代理会对外暴露一个必然 400
    "Model not found" 的名字，客户端表现为"莫名不能用"。
    """
    import build_litellm_config as b

    path = tmp_path / "models.yaml"
    path.write_text("models:\n  - alive\n  - deepseek-v41-flash\n  - Qwen3.8-27B\n",
                    encoding="utf-8")

    # 打桩：上游只返回 alive —— 模拟两个模型被禁用
    monkeypatch.setattr(b, "fetch_upstream_models", lambda *a, **k: [{"id": "alive"}])
    monkeypatch.setattr(b, "load_proxy_settings", lambda *a, **k: {
        "api_base": "https://x/api", "host_query": "", "api_key": "k"})
    monkeypatch.setattr(b, "load_cookie_header", lambda *a, **k: "c=1")
    monkeypatch.setattr(b, "render_config", lambda *a, **k: "stub: ascii\n")

    out = tmp_path / "out.yaml"
    monkeypatch.setattr("sys.argv", [
        "x", "--out", str(out),
        "--models-file", str(path),
        "--clients-dir", str(tmp_path / "clients"),
    ])
    b.main()

    assert load_models(path) == ["alive"], "被禁用的模型必须被自动删除"
    assert "deepseek-v41-flash" not in path.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    # 删除动作要让用户看得见，而不是静默改配置（删除是正常同步报告 -> stdout）
    assert "deepseek-v41-flash" in captured.out
    assert "Qwen3.8-27B" in captured.out


def test_auto_sync_failure_does_not_block_generation(tmp_path, capsys, monkeypatch):
    """拉取上游失败时必须沿用本地清单继续生成 —— 否则网络一抖，
    run.ps1 的重启路径整体失灵。"""
    import build_litellm_config as b

    path = tmp_path / "models.yaml"
    path.write_text("models:\n  - local-model\n", encoding="utf-8")

    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(b, "fetch_upstream_models", boom)
    monkeypatch.setattr(b, "load_proxy_settings", lambda *a, **k: {
        "api_base": "https://x/api", "host_query": "", "api_key": "k"})
    monkeypatch.setattr(b, "load_cookie_header", lambda *a, **k: "c=1")
    monkeypatch.setattr(b, "render_config", lambda *a, **k: "stub: ascii\n")

    out = tmp_path / "out.yaml"
    monkeypatch.setattr("sys.argv", [
        "x", "--out", str(out),
        "--models-file", str(path),
        "--clients-dir", str(tmp_path / "clients"),
    ])
    b.main()  # 不抛即通过

    assert "ConnectionError" in capsys.readouterr().err, "失败原因应打到 stderr"
    assert load_models(path) == ["local-model"], "本地清单不应被失败的同步破坏"


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


def test_run_script_no_proxy_covers_ujn_and_both_loopbacks():
    """必须让 LiteLLM 绕过系统代理，否则用户关掉代理软件就 500。

    实测根因（2026-09-14）：run.ps1 在 PowerShell 里跑，若 profile 设了
    HTTP_PROXY/HTTPS_PROXY（FlClash 等），LiteLLM 子进程会继承它，把上游请求
    发给那个代理；代理软件一关就变成死地址 -> 上游全部 500。

    用死端口 9 模拟"代理已关"实测：
      NO_PROXY=localhost,127.0.0.1              -> HTTP 500（复现故障）
      NO_PROXY=localhost,127.0.0.1,.ujn.edu.cn  -> HTTP 200

    【直接解析 run.ps1】而不是断言某个 Python 常量：早先这里断言的是一个
    只存在于 Python 里、没有任何运行代码使用的副本 —— 那样即使 run.ps1 写错了
    （而它才是真正生效的地方），测试也照样通过。现在钉的是生效的那份。
    """
    import re
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "run.ps1").read_text(encoding="utf-8")
    match = re.search(r'^\s*\$parts\s*=\s*@\(([^)]*)\)', script, re.MULTILINE)
    assert match, "run.ps1 里找不到 $parts = @(...) 的 NO_PROXY 清单"

    entries = [e.strip().strip('"').strip("'") for e in match.group(1).split(",")]
    entries = [e for e in entries if e]

    # webvpn.ujn.edu.cn 必须被覆盖 —— 用后缀，换子系统时不必再改
    assert ".ujn.edu.cn" in entries, f"run.ps1 的 NO_PROXY 缺 .ujn.edu.cn: {entries}"
    # 两种回环都要：客户端有时按 IPv6 写法连本机，漏掉 ::1 会时好时坏
    assert "localhost" in entries
    assert "127.0.0.1" in entries
    assert "::1" in entries


def test_run_script_merges_rather_than_overwrites_no_proxy():
    """不能直接赋值 —— 会盖掉用户 profile 里已有的 NO_PROXY 例外。"""
    import re
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "run.ps1").read_text(encoding="utf-8")

    assert "$env:NO_PROXY = $parts -join" in script, "应把既有值并入后再写回"

    # 也不该再出现 no_proxy 的双写：Windows 环境变量不区分大小写，那是冗余。
    # 必须先剥掉注释再查 —— 注释里正解释"不要这么写"，会误伤。
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r'^\s*\$env:no_proxy\s*=', code, re.MULTILINE), (
        "Windows 上 NO_PROXY 与 no_proxy 是同一个变量，再设一遍是冗余"
    )


# --- run.sh（macOS / Linux）的 NO_PROXY ---------------------------------------
#
# 清单此时有两份（run.ps1 与 run.sh），比单份更容易腐坏 —— 下面除了各自
# 覆盖面的断言，还有一条专门钉「两份不许漂移」。

def _no_proxy_entries_from_run_sh() -> list[str]:
    """解析 run.sh 里【真正生效】的那份 NO_PROXY 清单。

    同 run.ps1 的理由见上面那条测试：清单在脚本里，不在 Python 常量里。
    `_merged` 之后还会被赋成 "$_merged,$_item"（并既有值），所以只取
    第一个、且不含 $ 的字面量 —— 那才是初始清单。
    """
    import re
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "run.sh").read_text(encoding="utf-8")
    match = re.search(r'^\s*_merged="([^"$]*)"', script, re.MULTILINE)
    assert match, 'run.sh 里找不到 _merged="..." 的 NO_PROXY 清单'
    return [e.strip() for e in match.group(1).split(",") if e.strip()]


def test_run_sh_no_proxy_covers_ujn_and_both_loopbacks():
    """run.sh 与 run.ps1 是同一类故障的两个入口，覆盖面必须一致。"""
    entries = _no_proxy_entries_from_run_sh()

    assert ".ujn.edu.cn" in entries, f"run.sh 的 NO_PROXY 缺 .ujn.edu.cn: {entries}"
    assert "localhost" in entries
    assert "127.0.0.1" in entries
    assert "::1" in entries


def test_run_sh_and_run_ps1_share_the_same_no_proxy_list():
    """两个脚本的清单不许漂移。

    上一条提交把 NO_PROXY 从 Python 常量改成「直接解析生效的 run.ps1」，
    防的是"测试钉着副本、真正生效的那份写错了也照样通过"。同一种危险现在
    变成了两份：改了 run.ps1 忘了 run.sh（或反过来），谁都不会发现。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ps1 = (root / "run.ps1").read_text(encoding="utf-8")
    match = re.search(r"^\s*\$parts\s*=\s*@\(([^)]*)\)", ps1, re.MULTILINE)
    assert match, "run.ps1 里找不到 $parts = @(...) 的 NO_PROXY 清单"

    from_ps1 = {
        e.strip().strip('"').strip("'")
        for e in match.group(1).split(",")
        if e.strip()
    }
    assert from_ps1 == set(_no_proxy_entries_from_run_sh()), (
        "run.ps1 与 run.sh 的 NO_PROXY 清单已漂移 —— 两边都要覆盖同样的主机"
    )


def test_run_sh_merges_rather_than_overwrites_no_proxy():
    """不能直接赋值 —— 会盖掉用户 shell 启动文件里已有的例外。"""
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "run.sh").read_text(encoding="utf-8")

    assert "${NO_PROXY:-}" in script and "${no_proxy:-}" in script, (
        "应把用户已有的 NO_PROXY / no_proxy 并入后再写回"
    )


def test_run_sh_sets_both_no_proxy_spellings():
    """run.sh 必须【两个拼写都设】—— 与 run.ps1 的要求恰好相反。

    Linux/macOS 上 NO_PROXY 与 no_proxy 是两个互相独立的变量，各库认哪个
    不一定（curl 只认小写，Python requests 两个都认）。只设大写，curl 那条路
    仍会被系统代理接管。

    这条同时是防「照抄 run.ps1 的注释来清理」：run.ps1 里明写着
    「不要再写 no_proxy」，但那是 Windows 专属结论（环境变量不区分大小写），
    搬到 run.sh 就把功能弄坏了。
    """
    import re
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "run.sh").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )

    assert re.search(r'^\s*NO_PROXY="\$_merged"', code, re.MULTILINE), "缺 NO_PROXY 赋值"
    assert re.search(r'^\s*no_proxy="\$_merged"', code, re.MULTILINE), (
        "run.sh 必须同时设 no_proxy —— Linux/macOS 上它与 NO_PROXY 是两个独立变量"
    )


def test_client_templates_are_not_polluted_by_ujn_suffix():
    """客户端范本里的 NO_PROXY 只服务"连本机 4000"，别混进 .ujn.edu.cn。

    客户端不直连上游（只连 LiteLLM），加校园网后缀没意义。
    """
    import json

    from build_litellm_config import render_claude_settings

    env = json.loads(render_claude_settings(["m"]))["env"]

    assert "127.0.0.1" in env["NO_PROXY"]
    assert ".ujn.edu.cn" not in env["NO_PROXY"]
    # Windows 环境变量不区分大小写：同时写 NO_PROXY 和 no_proxy 是冗余，
    # 但生成的 JSON 是给用户的范本，保留两种拼写对跨平台用户无害 —— 只要值一致。
    if "no_proxy" in env:
        assert env["no_proxy"] == env["NO_PROXY"]


def test_client_no_proxy_includes_ipv6_loopback():
    """客户端范本必须含 ::1 —— 客户端有时按 IPv6 写法连本机。

    漏掉它会让「客户端 → 本机 4000」这条腿时好时坏：走 IPv4 字面量时正常，
    走 ::1 时仍被代理接管。这是最难查的一类间歇故障。
    """
    import json

    from build_litellm_config import CLIENT_NO_PROXY, render_claude_settings, render_ccswitch

    entries = [e.strip() for e in CLIENT_NO_PROXY.split(",")]
    assert "localhost" in entries
    assert "127.0.0.1" in entries
    assert "::1" in entries

    # 两个范本都要带上，别只改了一处
    claude_env = json.loads(render_claude_settings(["m"]))["env"]
    cc_env = json.loads(render_ccswitch(["m"]))["env"]
    assert claude_env["NO_PROXY"] == CLIENT_NO_PROXY
    assert cc_env["NO_PROXY"] == CLIENT_NO_PROXY


def test_shared_helpers_are_still_importable_from_build_module():
    """这些名字原属 build_litellm_config，2026-09 搬到了 ujn_webvpn。

    搬家时在 build 模块里重新导出，是为了不断掉外部与既有测试的 import。
    这条测试钉住那个承诺：以后清理 import 时别把这几个当"未使用"删掉。
    """
    from build_litellm_config import (  # noqa: F401
        WRD_CONSTANT,
        WEBVPN_ORIGIN,
        extract_host_from_query,
        webvpn_path_segment,
    )

    assert WRD_CONSTANT == b"wrdvpnisthebest!"
    assert WEBVPN_ORIGIN == "https://webvpn.ujn.edu.cn"


def test_extract_host_from_query_strips_vpn_prefix():
    """host_query 形如 vpn-12-o2-chat.ujn.edu.cn，真实主机名在后面。"""
    from build_litellm_config import extract_host_from_query

    assert extract_host_from_query("vpn-12-o2-chat.ujn.edu.cn") == "chat.ujn.edu.cn"


def test_extract_host_from_query_passes_through_plain_host():
    """已经是裸主机名时原样返回，别把开头的字母吃掉。"""
    from build_litellm_config import extract_host_from_query

    assert extract_host_from_query("chat.ujn.edu.cn") == "chat.ujn.edu.cn"


# --- reasoning_effort 必须被丢弃（双客户端兼容）----------------------------

def test_render_config_drops_reasoning_effort_on_every_deployment():
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3", "deepseek-v41-flash"]))
    assert config["model_list"], "model_list 不应为空"
    for entry in config["model_list"]:
        assert entry["litellm_params"]["additional_drop_params"] == ["reasoning_effort"], (
            f"{entry['model_name']} 缺少 additional_drop_params"
        )


def test_render_config_drop_params_is_a_list_not_a_string():
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3"]))
    drop = config["model_list"][0]["litellm_params"]["additional_drop_params"]
    assert isinstance(drop, list)
    assert all(isinstance(x, str) for x in drop)


def test_render_config_drop_params_stays_per_deployment():
    config = yaml.safe_load(render_config(SETTINGS, "a=b", ["GLM-5.3"]))
    assert "additional_drop_params" not in config["litellm_settings"]
    assert config["model_list"][0]["litellm_params"]["additional_drop_params"] == ["reasoning_effort"]


def test_render_config_still_pure_ascii_with_drop_params():
    out = render_config(SETTINGS, "a=b", ["GLM-5.3", "/models/Qwen3.8-Flash-Next"])
    non_ascii = [c for c in out if ord(c) > 127]
    assert not non_ascii, f"config must be ASCII-only, found {non_ascii[:5]}"


# --- 客户端范本生成（clients/*，从 models.yaml 派生，避免漂移）---------------

def test_pick_tier_models_prefers_the_fast_1m_models():
    """四档默认用实测最快且 1M 上下文的两个模型。

    （deepseek-v41-flash 已于 2026-09 被上游禁用，首选改为同家族的
    deepseek-v4-flash —— 上游 /models 实测 ctx=1048576。）
    """
    from build_litellm_config import pick_tier_models

    tiers = pick_tier_models(["GLM-5.3", "deepseek-v4-flash", "GLM-5.3-Flash"])

    assert tiers["fable"] == "deepseek-v4-flash"
    assert tiers["opus"] == "deepseek-v4-flash"
    assert tiers["sonnet"] == "GLM-5.3-Flash"
    assert tiers["haiku"] == "GLM-5.3-Flash"


def test_pick_tier_models_falls_back_when_preferred_absent():
    """上游改名/下线时不能崩 —— 退回到 models.yaml 里有的。"""
    from build_litellm_config import pick_tier_models

    tiers = pick_tier_models(["some-unknown-model"])

    assert tiers["fable"] == "some-unknown-model"
    assert tiers["haiku"] == "some-unknown-model"


def test_pick_tier_models_handles_single_model():
    """只有一个模型时四档全指向它，而不是报错或留空。"""
    from build_litellm_config import pick_tier_models

    tiers = pick_tier_models(["deepseek-v41-flash"])

    assert set(tiers.values()) == {"deepseek-v41-flash"}


def test_claude_settings_carry_the_1m_suffix():
    """Claude Code 的档位必须带 [1M]，否则它按 200k 算、提前 auto-compact。"""
    import json
    from build_litellm_config import render_claude_settings

    env = json.loads(render_claude_settings(["deepseek-v41-flash", "GLM-5.3-Flash"]))["env"]

    for key in ["ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_MODEL"]:
        assert key in env, f"缺少 {key}"
        assert env[key].endswith("[1M]"), f"{key}={env[key]} 应带 [1M]"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_claude_settings_base_url_has_no_v1():
    """Claude Code 的 BASE_URL 带 /v1 会变成 /v1/v1/messages -> 404。"""
    import json
    from build_litellm_config import render_claude_settings

    env = json.loads(render_claude_settings(["deepseek-v41-flash"]))["env"]

    assert not env["ANTHROPIC_BASE_URL"].endswith("/v1")


def test_codex_toml_has_v1_and_no_1m_suffix():
    """Codex 相反：/v1 要带，[1M] 不能带（它会原样透传 -> 400）。

    只看生效的配置行，跳过注释 —— 注释里会提到 [1M] 作为反例说明。
    """
    from build_litellm_config import render_codex_toml

    toml = render_codex_toml(["deepseek-v41-flash"])
    # 去掉注释行后，剩下的才是 Codex 真正会读的配置
    active = "\n".join(l for l in toml.splitlines() if not l.lstrip().startswith("#"))

    assert 'base_url = "http://127.0.0.1:4000/v1"' in active
    assert 'wire_api = "responses"' in active
    assert 'model = "deepseek-v41-flash"' in active
    assert "[1M]" not in active


def test_opencode_lists_every_model_verbatim():
    """范本要覆盖 models.yaml 全部模型，且不带 [1M]。"""
    import json
    from build_litellm_config import render_opencode

    models = ["deepseek-v41-flash", "/models/Qwen3.8-Flash-Next", "1.Qwen3.5-27B"]
    cfg = json.loads(render_opencode(models))
    listed = cfg["provider"]["ujn-llm"]["models"]

    assert list(listed.keys()) == models
    assert "[1M]" not in json.dumps(cfg)


def test_client_templates_accept_a_custom_port():
    """端口固定 4000，但函数要能改 —— 便于测试与将来换端口。"""
    import json
    from build_litellm_config import render_claude_settings, render_codex_toml

    assert ":4123" in json.loads(render_claude_settings(["m"], port=4123))["env"]["ANTHROPIC_BASE_URL"]
    assert ":4123" in render_codex_toml(["m"], port=4123)


def test_emit_client_templates_is_idempotent(tmp_path):
    """同一份 models.yaml 重复生成，内容不变 —— 否则每次同步都产生无意义 diff。"""
    from build_litellm_config import emit_client_templates

    models = ["deepseek-v41-flash", "GLM-5.3-Flash"]
    written1 = emit_client_templates(models, tmp_path)
    snapshot = {p.name: p.read_text(encoding="utf-8") for p in written1}

    written2 = emit_client_templates(models, tmp_path)
    snapshot2 = {p.name: p.read_text(encoding="utf-8") for p in written2}

    assert snapshot == snapshot2
    assert set(snapshot) == {"claude-settings.json", "ccswitch.json",
                             "codex-config-snippet.toml", "opencode.json"}


def test_emitted_json_files_are_parseable(tmp_path):
    """生成的 JSON 必须能被解析 —— 手工拼字符串最容易在这里翻车。"""
    import json
    from build_litellm_config import emit_client_templates

    for path in emit_client_templates(["deepseek-v41-flash"], tmp_path):
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))   # 不抛即通过
