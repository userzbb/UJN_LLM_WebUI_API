"""JWT 自动提取链路：识别、state 读写、优先级、登录脚本的提取行为。

浏览器部分用假 page 打桩，不联网 —— 真实链路由 CLI 手工验证（见 README）。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ujn_webvpn import (
    FIND_JWT_JS,
    JWT_FIELD,
    extract_host_from_query,
    looks_like_jwt,
    read_state_jwt,
    webvpn_app_root,
)

# 【构造值，不是真凭据】——
# 形状对齐真实的那个（HS256 头 + 单键 payload + 43 字符签名，
# 真实 JWT 的载荷只有 {"id":"<uuid>"}、无 exp），但签名和 UUID 都是假的。
# 绝不要把真 JWT 写进测试：签名有效即等于可冒充调用上游接口。
SAMPLE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpZCI6IjAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMCJ9"
    ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


# --- JWT 识别 ---------------------------------------------------------------

def test_looks_like_jwt_accepts_a_well_formed_token():
    assert looks_like_jwt(SAMPLE_JWT)


def test_looks_like_jwt_rejects_the_obfuscated_key_noise():
    """真实 localStorage dump 里绝大部分键值对是噪声：值是常量 "2" 或 "true"。

    这些绝不能被认为是凭据 —— 按「值格式」匹配的全部意义就在这里。
    （这也是 shape 校验的唯一用途：在几百个噪声值里"发现"token。）
    """
    for noise in ["2", "true", "false", "zh-CN", "system", "", "   ", '{"uuid":2}']:
        assert not looks_like_jwt(noise), f"{noise!r} 不该被当成 JWT"


def test_looks_like_jwt_rejects_truncated_and_oversized_shapes():
    """两段式 / 四段式 / 带 Bearer 前缀都不是裸 JWT。"""
    assert not looks_like_jwt("eyJhbGciOiJIUzI1NiJ9.eyJpZCI6IngifQ")          # 两段
    assert not looks_like_jwt(SAMPLE_JWT + ".extra")                            # 四段
    assert not looks_like_jwt("Bearer " + SAMPLE_JWT)                           # 带前缀
    assert not looks_like_jwt("eyJhbGciOiJIUzI1NiJ9")                         # 只有头


def test_looks_like_jwt_tolerates_surrounding_whitespace():
    """localStorage 的值可能带空白，提取时 trim 掉即可。"""
    assert looks_like_jwt(f"  {SAMPLE_JWT}\n")


def test_find_jwt_js_scans_both_storages():
    """上游把 token 挪到 sessionStorage 或换 key 名时，JS 不应跟着改。"""
    assert "localStorage" in FIND_JWT_JS
    assert "sessionStorage" in FIND_JWT_JS
    # 按值匹配而不是按 key 名取值
    assert "re.test" in FIND_JWT_JS
    assert "getItem" in FIND_JWT_JS


# --- state 文件读写（登录脚本写 / 生成脚本读）--------------------------------

def test_read_state_jwt_round_trips(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], JWT_FIELD: SAMPLE_JWT}), encoding="utf-8")

    assert read_state_jwt(state) == SAMPLE_JWT


def test_read_state_jwt_returns_none_for_missing_file(tmp_path):
    """没跑过登录脚本时必须安静返回 None，由调用方回退到 config.yaml。"""
    assert read_state_jwt(tmp_path / "nope.json") is None


def test_read_state_jwt_returns_none_for_corrupt_file(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{ this is not json", encoding="utf-8")

    assert read_state_jwt(state) is None


def test_read_state_jwt_returns_none_when_field_absent(tmp_path):
    """纯 storage_state 输出（只有 cookies/origins）里没有 jwt 字段。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    assert read_state_jwt(state) is None


def test_read_state_jwt_returns_none_for_non_string_field(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: {"nested": "x"}}), encoding="utf-8")

    assert read_state_jwt(state) is None


def test_read_state_jwt_does_not_shape_check_the_value(tmp_path):
    """【关键】读取端故意不做 JWT 形状校验。

    形状校验只用于在浏览器里「发现」token（442 个 key 里挑出那一个），
    不该用来「否决」一个已经拿到的凭据。否则上游换了 token 格式后，
    读取方会悄悄回退到 config.yaml 的旧值 —— 用户以为在用新 JWT，
    实际用的是旧的。这种静默替换比直接失败更难排查。
    """
    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: "opaque-token-v2"}), encoding="utf-8")

    assert read_state_jwt(state) == "opaque-token-v2"


def test_read_state_jwt_returns_none_for_blank_field(tmp_path):
    """空串/纯空白是「没拿到」，必须回退 —— 这与上面故意放行的非 JWT 不同。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: "   "}), encoding="utf-8")

    assert read_state_jwt(state) is None


# --- 应用根 URL 推导 --------------------------------------------------------

def test_webvpn_app_root_derives_from_host_query():
    """金标准：段值与 config.yaml 里的一致（独立核对过）。"""
    assert webvpn_app_root("vpn-12-o2-chat.ujn.edu.cn") == (
        "https://webvpn.ujn.edu.cn/https/"
        "77726476706e69737468656265737421f3ff40886925625e300d8db9d6562d/"
    )


def test_webvpn_app_root_trailing_slash_matters():
    """结尾的 / 不能少：少了会被 WebVPN 当作另一个路径。"""
    assert webvpn_app_root("vpn-12-o2-chat.ujn.edu.cn").endswith("/")


def test_webvpn_app_root_accepts_plain_host():
    assert webvpn_app_root("chat.ujn.edu.cn") == webvpn_app_root("vpn-12-o2-chat.ujn.edu.cn")


def test_webvpn_app_root_empty_when_host_missing():
    """host_query 没配时返回空串，调用方据此跳过提取而不是崩。"""
    assert webvpn_app_root("") == ""
    assert webvpn_app_root("vpn-12-o2-") == ""


def test_webvpn_app_root_rejects_hostless_string():
    """别把 "localhost" 这类没点号的主机名编出个假 URL。"""
    assert webvpn_app_root("localhost") == ""


# --- 登录脚本：config 展平 / 提取 / 保存 ------------------------------------

def test_load_config_flattens_nested_proxy_block(tmp_path):
    """【关键回归】webvpn_host_query 在 config.yaml 里位于 proxy: 之下。

    原实现只取顶层键，读不到它 -> 算不出应用根 URL -> JWT 提取永远静默跳过。
    这个 bug 不会报错，只会让自动化看起来"没生效"，所以必须钉住。
    """
    from ujn_webvpn_login import load_config

    config = tmp_path / "config.yaml"
    config.write_text(
        'username: "209900000001"\n'
        'password: "pw"\n'
        "proxy:\n"
        '  api_key: "sk-real"\n'
        '  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"\n',
        encoding="utf-8",
    )

    flat = load_config(config)

    assert flat["webvpn_host_query"] == "vpn-12-o2-chat.ujn.edu.cn"
    assert flat["username"] == "209900000001"
    assert flat["api_key"] == "sk-real"


def test_load_config_last_seen_wins_on_nested_and_top_level():
    """顶层与嵌套同名时，递归顺序决定谁生效 —— 这里钉住"先到先得"。
    （顶层先遍历，所以顶层优先。）"""
    from ujn_webvpn_login import load_config

    path = Path(__file__).parent / "_tmp_dup.yaml"
    try:
        path.write_text(
            'webvpn_host_query: "top"\nproxy:\n  webvpn_host_query: "nested"\n',
            encoding="utf-8",
        )
        assert load_config(path)["webvpn_host_query"] == "top"
    finally:
        path.unlink(missing_ok=True)


class FakePage:
    """打桩的 Playwright page。

    storage: 模拟 localStorage/sessionStorage 的键值对。
    raise_on_goto: 模拟导航超时。
    values: 依次返回的 evaluate 结果（模拟"JWT 稍后才出现"）。
    """

    def __init__(self, values, raise_on_goto=None, goto_error=None):
        self._values = list(values)
        self.goto_calls: list[str] = []
        self._raise_on_goto = raise_on_goto
        self._goto_error = goto_error

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        return None

    def evaluate(self, js):
        assert js == FIND_JWT_JS, "必须用共享的 FIND_JWT_JS，不要另写一份"
        if self._raise_on_goto:
            raise self._raise_on_goto
        return self._values.pop(0) if self._values else None

    def wait_for_timeout(self, ms):
        return None


def test_extract_jwt_navigates_to_app_root_and_returns_token():
    from ujn_webvpn_login import extract_jwt

    page = FakePage([None, SAMPLE_JWT])

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") == SAMPLE_JWT
    assert page.goto_calls == [webvpn_app_root("vpn-12-o2-chat.ujn.edu.cn")]


def test_extract_jwt_polls_until_token_appears():
    """token 不是页面一加载就有的，要轮询等待。"""
    from ujn_webvpn_login import extract_jwt

    page = FakePage([None, None, None, SAMPLE_JWT])

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") == SAMPLE_JWT
    assert not page._values, "拿到后应立刻停止轮询"


def test_extract_jwt_skips_navigation_when_host_missing():
    """没配 host_query 时不应导航到任何地方（会白等 15s），直接返回 None。"""
    from ujn_webvpn_login import extract_jwt

    page = FakePage([])

    assert extract_jwt(page, "") is None
    assert page.goto_calls == []


def test_extract_jwt_returns_none_on_goto_timeout():
    """导航超时只是"这次没掏到"，必须返回 None 而不是抛 —— 登录已经成功了。"""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from ujn_webvpn_login import extract_jwt

    page = FakePage([], goto_error=PlaywrightTimeoutError("timeout"))

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") is None


def test_extract_jwt_swallows_evaluate_errors():
    """页面结构变化导致 JS 报错时，同样不该让登录失败。"""
    from ujn_webvpn_login import extract_jwt

    page = FakePage([], raise_on_goto=RuntimeError("Script error"))

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") is None


def test_extract_jwt_returns_none_when_token_never_appears():
    from ujn_webvpn_login import extract_jwt

    page = FakePage([None] * 40)

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") is None


def test_extract_jwt_rejects_noise_values_seen_in_the_real_dump():
    """页面里全是 __2___3_..._token 这类混淆键，值可能是 "2"。

    只有真正形如 JWT 的值才算数。
    """
    from ujn_webvpn_login import extract_jwt

    page = FakePage(["2", "true", SAMPLE_JWT])

    assert extract_jwt(page, "vpn-12-o2-chat.ujn.edu.cn") == SAMPLE_JWT


class FakeContext:
    """打桩的 Playwright browser context。

    storage_state(path=...) 会写一份"只有 cookies/origins"的文件，
    忠实模拟真实 API 的行为（这正是必须额外补 jwt 字段的原因）。
    """

    def __init__(self, cookies=None):
        self._cookies = cookies or [{"name": "c", "value": "v"}]

    def storage_state(self, path):
        Path(path).write_text(
            json.dumps({"cookies": self._cookies, "origins": []}), encoding="utf-8"
        )


def test_save_state_writes_jwt_alongside_cookies(tmp_path):
    from ujn_webvpn_login import save_state

    state = tmp_path / "state.json"
    save_state(FakeContext(), state, SAMPLE_JWT)

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data[JWT_FIELD] == SAMPLE_JWT
    assert data["cookies"] == [{"name": "c", "value": "v"}]


def test_save_state_preserves_previous_jwt_when_extraction_failed(tmp_path):
    """【关键】storage_state() 会整份重写文件，把上一轮的 jwt 抹掉。

    run.ps1 每 1800s 调一次登录脚本；若某次提取因网络抖动失败就丢掉 JWT，
    整条链路会悄悄退到 config.yaml 的旧 api_key —— 换凭据比用旧凭据更难排查。
    """
    from ujn_webvpn_login import save_state

    state = tmp_path / "state.json"
    save_state(FakeContext(), state, SAMPLE_JWT)          # 第一轮：成功
    save_state(FakeContext(), state, None)              # 第二轮：提取失败

    assert read_state_jwt(state) == SAMPLE_JWT, "失败时不该抹掉已有 JWT"


def test_save_state_returns_the_effective_value(tmp_path):
    """调用方据此区分"本次掏到"和"沿用旧值"并分别提示。"""
    from ujn_webvpn_login import save_state

    state = tmp_path / "state.json"

    assert save_state(FakeContext(), state, SAMPLE_JWT) == SAMPLE_JWT
    assert save_state(FakeContext(), state, None) == SAMPLE_JWT


def test_save_state_prefers_fresh_jwt_over_previous(tmp_path):
    """拿到了新的就用新的 —— 上游签发新 token 后沿用旧的正是最难查的失效。"""
    from ujn_webvpn_login import save_state

    state = tmp_path / "state.json"
    fresh = SAMPLE_JWT[:-4] + "AAAA"

    save_state(FakeContext(), state, SAMPLE_JWT)
    save_state(FakeContext(), state, fresh)

    assert read_state_jwt(state) == fresh


def test_save_state_no_jwt_at_all_leaves_no_field(tmp_path):
    """从没掏到过 JWT 时不该写一个 null 字段进去。"""
    from ujn_webvpn_login import save_state

    state = tmp_path / "state.json"
    save_state(FakeContext(), state, None)

    assert JWT_FIELD not in json.loads(state.read_text(encoding="utf-8"))


# --- 生成脚本：JWT 来源优先级 ----------------------------------------------

def test_load_api_key_prefers_state_over_config(tmp_path, capsys):
    """state 优先：config.yaml 里手工抄的旧值不该压着自动提取的新值。"""
    from build_litellm_config import load_api_key

    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: SAMPLE_JWT}), encoding="utf-8")

    got = load_api_key('proxy:\n  api_key: "stale-manual"\n', state_file=state)

    assert got == SAMPLE_JWT
    err = capsys.readouterr().err
    assert "不一致" in err, "两者不同时必须告警，否则用户不知道用的是哪一份"


def test_load_api_key_falls_back_to_config_when_state_absent(tmp_path):
    """没跑过登录脚本时的兜底路径，必须仍然可用。"""
    from build_litellm_config import load_api_key

    got = load_api_key('proxy:\n  api_key: "sk-manual"\n',
                       state_file=tmp_path / "missing.json")

    assert got == "sk-manual"


def test_load_api_key_no_warning_when_both_agree(tmp_path, capsys):
    from build_litellm_config import load_api_key

    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: SAMPLE_JWT}), encoding="utf-8")

    got = load_api_key(f'proxy:\n  api_key: "{SAMPLE_JWT}"\n', state_file=state)

    assert got == SAMPLE_JWT
    assert capsys.readouterr().err == ""


def test_load_api_key_exits_when_neither_source_has_one(tmp_path, capsys):
    from build_litellm_config import load_api_key

    with pytest.raises(SystemExit) as exc:
        load_api_key("proxy:\n  api_key: null\n", state_file=tmp_path / "missing.json")

    msg = str(exc.value)
    assert "ujn_webvpn_login.py" in msg, "报错应指出先跑登录脚本这条自动路径"


def test_load_api_key_still_reads_readme_style_quoted_config(tmp_path):
    """回归：带行尾注释 / 单引号的写法必须仍能读出（read_yaml_scalar 的既有保证）。"""
    from build_litellm_config import load_api_key

    missing = tmp_path / "missing.json"
    assert load_api_key('proxy:\n  api_key: "sk-real"   # 注释\n', state_file=missing) == "sk-real"
    assert load_api_key("proxy:\n  api_key: 'sk-real'\n", state_file=missing) == "sk-real"


def test_load_proxy_settings_uses_state_jwt_end_to_end(tmp_path):
    """整体：只给 config.yaml 的 account/base，JWT 与会话由 state 提供。"""
    from build_litellm_config import load_proxy_settings

    config = tmp_path / "config.yaml"
    config.write_text(
        'username: "u"\n'
        'password: "p"\n'
        "proxy:\n"
        '  webvpn_api_base: "https://webvpn.ujn.edu.cn/https/<opaque>/api"\n'
        '  webvpn_host_query: "vpn-12-o2-chat.ujn.edu.cn"\n',
        encoding="utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps({JWT_FIELD: SAMPLE_JWT}), encoding="utf-8")

    import build_litellm_config as b

    # 显式传 state_file 而不是改模块常量 —— 后者对默认参数无效
    # （默认参数在定义时求值），这正是 load_api_key 改用 None 哨兵的原因。
    settings = load_proxy_settings(config, state_file=state)

    assert settings["api_key"] == SAMPLE_JWT
    # <opaque> 仍应被推导替换（这条路径与 JWT 无关，别被改坏）
    assert "<opaque>" not in settings["api_base"]
    assert "77726476706e69737468656265737421f3ff40886925625e300d8db9d6562d" in settings["api_base"]
