"""WebVPN 的共享常量与小工具。

被 ujn_webvpn_login.py（登录并掏 JWT）和 build_litellm_config.py（生成代理配置）
共同依赖，所以单独成模块 —— 否则依赖方向会变成「登录脚本 import 生成脚本」，
两个本该平级的入口互相牵扯。

不 import 任何第三方包（cryptography 在函数内按需 import），
这样两个入口都能安全地 `import ujn_webvpn`。
"""

import json
import re
from pathlib import Path

WEBVPN_ORIGIN = "https://webvpn.ujn.edu.cn"

# state 文件里存放 JWT 的字段名。storage_state() 只写 cookies/origins，
# jwt 是我们额外塞进去的，读取方（build_litellm_config）按同一个常量取。
JWT_FIELD = "jwt"

# WebVPN 路径段的编码常量 —— 公开值，所有 wrdvpn 部署通用。
# 路径段 = "wrdvpnisthebest!" + AES-CTR(主机名, key=iv=该常量)。
# 即：它【只是主机名的编码】，不是凭据 —— 同一所学校所有用户的值都一样，
# 且单独拿到它而没有有效会话 Cookie 时，只会被弹回登录页。
WRD_CONSTANT = b"wrdvpnisthebest!"

# host_query 形如 vpn-12-o2-chat.ujn.edu.cn，真实主机名在 vpn-<端口>-o<1|2>- 之后。
_VPN_PREFIX_RE = re.compile(r"^vpn-\d+-o[12]-")


def webvpn_path_segment(host: str) -> str:
    """由主机名推导 WebVPN 的 /https/<段>/ 路径段（纯函数，不联网）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encryptor = Cipher(algorithms.AES(WRD_CONSTANT), modes.CTR(WRD_CONSTANT)).encryptor()
    ciphertext = encryptor.update(host.encode("utf-8")) + encryptor.finalize()
    return (WRD_CONSTANT + ciphertext).hex()


def extract_host_from_query(host_query: str) -> str:
    """从 host_query 里取出真实主机名（去掉 vpn-<端口>-o<1|2>- 前缀）。"""
    return _VPN_PREFIX_RE.sub("", host_query.strip(), count=1)


def webvpn_app_root(host_query: str) -> str:
    """由 host_query 推出 WebVPN 上的应用根 URL（结尾带 /）。

    登录后浏览器需要真的走到这个 URL 上，前端才会加载并把 JWT 写进 localStorage。
    主机名缺失时返回空串，调用方据此跳过 JWT 提取（不应因此让登录失败）。
    """
    host = extract_host_from_query(host_query or "")
    if not host or "." not in host:
        return ""
    return f"{WEBVPN_ORIGIN}/https/{webvpn_path_segment(host)}/"


# --- JWT 识别 ---------------------------------------------------------------

# JWT = 三段 base64url，头两段固定以 eyJ 开头（{"a... 的 base64）。
# 用「按值格式匹配」而不是「按 key 名取值」：实测 chat.ujn.edu.cn 的
# localStorage key 是 __2___3___2..._token 这类混淆串，每次部署都可能变，
# 硬编码 key 名必翻车。
JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def looks_like_jwt(value: str) -> bool:
    """判断一个字符串是否是 JWT（纯函数，供测试与本地校验用）。"""
    return bool(value) and bool(JWT_RE.match(value.strip()))


def read_state_jwt(state_file: Path) -> str | None:
    """从 state 文件里读 JWT。文件缺失/损坏/字段缺失都返回 None。

    这里【故意不做 JWT 形状校验】：形状校验只用于在浏览器里「发现」token
    （442 个 key 里挑出那一个），不该用来「否决」一个已经拿到的凭据。
    否则上游哪天换了 token 格式，读取方会悄悄回退到 config.yaml 的旧值，
    用户以为在用新 JWT 其实用的是旧的 —— 这种静默替换比失败更糟。
    调用方若想提示格式可疑，自行用 looks_like_jwt 判断，但不要替换取值。
    """
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    jwt = state.get(JWT_FIELD)
    if not isinstance(jwt, str) or not jwt.strip():
        return None
    return jwt.strip()


# 在浏览器里跑的 JS：按顺序在三个地方找形如 JWT 的值。
#
# 实测（2026-09-14，用一次性诊断脚本逐一验证过）：
#   chat.ujn.edu.cn 的 JWT 在 document.cookie 的 `token` 里，【不在 localStorage】。
#   localStorage 只有 6 个 key（theme/locale/__vpn_cur_uuid/vpn_version/...），无 token。
#   1.txt 里那份 543 次重复的 localStorage dump 是前端把 cookie 复制过去的副本，
#   不是源头 —— 照它去 localStorage 找会永远找不到。
#
# 但它是个 JS 设的 session cookie（httpOnly=false），
# 【不会】进 Playwright 的 context.cookies() / storage_state()。
# 这就是必须用 document.cookie 读、而不能靠 storage_state 的原因。
#
# 仍然保留 localStorage/sessionStorage 兜底：上游若把 token 挪回前端存储，
# 这里不必跟着改。
FIND_JWT_JS = """
() => {
    const re = /^eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$/;
    const ok = (v) => v && re.test(v.trim());

    // 1) cookie 里的 token=...（当前上游的实际位置）
    for (const part of document.cookie.split(';')) {
        const eq = part.indexOf('=');
        if (eq < 0) continue;
        const name = part.slice(0, eq).trim();
        const value = part.slice(eq + 1).trim();
        if (name === 'token' && ok(value)) return value;
    }

    // 2) 其余 cookie：值本身像 JWT 就算了（不依赖具体名字）
    for (const part of document.cookie.split(';')) {
        const eq = part.indexOf('=');
        if (eq < 0) continue;
        const value = part.slice(eq + 1).trim();
        if (ok(value)) return value;
    }

    // 3) 前端存储兜底
    for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
            const v = store.getItem(store.key(i));
            if (ok(v)) return v.trim();
        }
    }
    return null;
}
"""
