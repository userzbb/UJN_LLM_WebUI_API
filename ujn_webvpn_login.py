import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ujn_console import enable_utf8_stdout
from ujn_webvpn import (
    FIND_JWT_JS,
    JWT_FIELD,
    WEBVPN_ORIGIN,
    looks_like_jwt,
    read_state_jwt,
    webvpn_app_root,
)


LOGIN_URL = WEBVPN_ORIGIN
STATE_FILE = Path("ujn_webvpn_state.json")
CONFIG_FILE = Path("config.yaml")
DEBUG_DIR = Path("debug")

# 导航到应用页后等 JWT 出现的时间。页面要自己跑完 SSO 才会设那个 token cookie，
# 给宽一点；超时只是「这次没掏到」，不影响登录本身。
JWT_WAIT_MS = 15000


def load_config(path: Path) -> dict[str, str]:
    """把 config.yaml 展平成 {键: 字符串值}，嵌套任意深度都拍平。

    必须递归：webvpn_host_query 在 config.yaml 里位于 proxy: 之下，
    只取顶层会读不到它，于是算不出应用根 URL、JWT 提取永远跳过。
    键冲突时先到先得（顶层优先），避免深层同名键盖掉显式的顶层配置。
    """
    if not path.exists():
        return {}

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a YAML mapping.")

    flat: dict[str, str] = {}

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            name = str(key)
            if isinstance(value, dict):
                walk(value)
            elif name not in flat:
                flat[name] = "" if value is None else str(value)

    walk(data)
    return flat


def get_credentials(config: dict[str, str]) -> tuple[str, str]:
    username = (
        config.get("username")
        or config.get("UJN_USERNAME")
        or os.getenv("UJN_USERNAME")
        or input("UJN username: ").strip()
    )
    password = (
        config.get("password")
        or config.get("UJN_PASSWORD")
        or os.getenv("UJN_PASSWORD")
        or getpass.getpass("UJN password: ")
    )

    if not username or not password:
        print("Username and password are required.", file=sys.stderr)
        sys.exit(2)

    return username, password


def click_first(page, selectors: list[str], timeout: int = 1200) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.click()
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def fill_first(page, selectors: list[str], value: str, timeout: int = 1500) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def fill_login_form(page, username: str, password: str) -> None:
    user_filled = fill_first(
        page,
        [
            "input[name='username']",
            "input[name='user']",
            "input[name='account']",
            "input[name='loginName']",
            "input[id*='user' i]",
            "input[id*='account' i]",
            "input[type='text']",
        ],
        username,
    )
    pass_filled = fill_first(
        page,
        [
            "input[name='password']",
            "input[name='pwd']",
            "input[id*='pass' i]",
            "input[type='password']",
        ],
        password,
    )

    if not user_filled or not pass_filled:
        raise RuntimeError("Could not find the username/password fields on the login page.")


def submit_login(page) -> None:
    clicked = click_first(
        page,
        [
            "button[type='submit']",
            "input[type='submit']",
            ".login-button",
            ".btn-login",
            "button",
        ],
        timeout=1800,
    )

    if not clicked:
        page.keyboard.press("Enter")


def is_login_page(page) -> bool:
    if "/login" in page.url:
        return True
    try:
        return page.locator("input[type='password']").count() > 0
    except Exception:
        return False


def wait_for_login(page) -> bool:
    for _ in range(5):
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightTimeoutError:
            pass
        if not is_login_page(page):
            return True
        page.wait_for_timeout(1000)
    return not is_login_page(page)


def try_chat_login(page, username: str, password: str) -> bool:
    """在 ChatUJN 自己的 /auth 登录页上提交智慧计大账号。

    返回 True 表示表单已提交（不代表登录成功 —— 成功与否由外层轮询
    token 是否出现判断）。任何异常都吞掉返回 False：这里失败只意味着
    "这次没掏到 JWT"，绝不能让登录脚本以非零退出。

    实测表单结构（2026-09-16）：input[type=text] + input[type=password]
    + button[type=submit]，双模式按钮（智慧计大 / 本地账号），默认即账号密码。
    """
    try:
        page.locator("input[type='text']").first.fill(username, timeout=3000)
        page.locator("input[type='password']").first.fill(password, timeout=3000)
        page.locator("button[type='submit']").first.click(timeout=3000)
        return True
    except Exception as exc:
        print(
            f"  警告：ChatUJN 登录表单填写失败（{type(exc).__name__}），"
            f"可能是页面改版",
            file=sys.stderr,
        )
        return False


def extract_jwt(page, host_query: str, username: str = "", password: str = "") -> str | None:
    """走到 chat 应用页，把 JWT 掏出来。

    返回 None 表示这次没掏到（上游改版、主机名缺失、页面没等到），
    调用方应只告警、不因此判定登录失败 —— Cookie 已经拿到手了。

    为什么必须走应用页：WebVPN 门户页和 chat 应用是两个不同的源路径，
    token 是 chat 应用的前端自己设的。

    为什么读 document.cookie 而不是 localStorage：
    实测 JWT 在名为 `token` 的 cookie 里，localStorage 里根本没有（详见
    ujn_webvpn.FIND_JWT_JS 的注释）。

    为什么不用 context.storage_state() 拿：
    它是 JS 设的 session cookie，不进 Playwright 的 cookie jar，
    storage_state() 里【不会】有它。

    应用层的第二道登录（实测 2026-09-16 才暴露）：WebVPN 会话有效 ≠ ChatUJN
    已登录。ChatUJN 的静默 SSO 过期后，应用页跳到自己的 /auth 要求显式输入
    智慧计大账号 —— 此时既没有 token cookie，代理打上游也会被弹回登录页
    HTML（LiteLLM 解 JSON 失败 -> 500）。所以这里要在密码框出现时
    用 config.yaml 的凭据再登一次。
    """
    app_root = webvpn_app_root(host_query)
    if not app_root:
        print(
            "  警告：config.yaml 里没有 webvpn_host_query，跳过 JWT 提取"
            "（Cookie 已保存，可在 config.yaml 填 api_key 兜底）",
            file=sys.stderr,
        )
        return None

    try:
        page.goto(app_root, wait_until="domcontentloaded", timeout=JWT_WAIT_MS)
    except PlaywrightTimeoutError:
        # 页面没加载完不代表没救 —— 继续轮询试试，token 常常早就设好了。
        pass
    except Exception as exc:
        print(f"  警告：打开应用页失败（{type(exc).__name__}），跳过 JWT 提取", file=sys.stderr)
        return None

    # 轮询而不是死等满。每次 evaluate 都要 try：应用页会自己跳 /auth，
    # 跳转瞬间执行上下文被销毁，evaluate 会抛 "Execution context was destroyed"。
    # 那不是错误，等页面稳定后重试即可。
    #
    # 轮询中还可能发现"需要显式登录"（ChatUJN 自己的 /auth 登录页）——
    # 那就用应用凭据登一次再继续等。SvelteKit 客户端路由下表单渲染晚于
    # domcontentloaded，所以"等表单"必须发生在同一轮询里，不能一次性判断。
    logged_in = False
    for _ in range(JWT_WAIT_MS // 500):
        try:
            jwt = page.evaluate(FIND_JWT_JS)
        except Exception:
            jwt = None
        if jwt and looks_like_jwt(jwt):
            return jwt

        if (
            not logged_in
            and username
            and password
            and page.locator("input[type='password']").count() > 0
        ):
            if try_chat_login(page, username, password):
                logged_in = True
            # 登录失败（凭据错/表单变了）不 return —— 继续轮询到超时，
            # 万一表单其实提交成功了，token 还是能掏到。

        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

    print(
        "  警告：应用页里没找到 JWT（cookie 与前端存储都查过了），"
        "请检查上游是否改了 token 的存放方式",
        file=sys.stderr,
    )
    return None


def save_state(context, state_file: Path, jwt: str | None) -> None:
    """写 storage_state，并把 JWT 一并写进顶层 jwt 字段。

    storage_state() 只产出 cookies/origins —— 它会整份重写文件，
    把上一次存的 jwt 字段抹掉。所以这里必须先读出来备份，再补回去。

    jwt 为 None 时沿用上一次的值，而不是抹掉：JWT 没有 exp（登一次长期有效），
    run.ps1 每 1800s 会调一次本脚本，若某次提取因网络抖动失败就把 JWT 抹掉，
    整条链路会悄悄退到 config.yaml 的 api_key 上 —— 换凭据比用旧凭据更难排查。
    调用方负责在沿用旧值时告警。

    返回实际写入文件的那个 JWT（可能是沿用的旧值），供调用方打印。
    """
    previous = read_state_jwt(state_file)

    context.storage_state(path=str(state_file))

    effective = jwt or previous
    if not effective:
        return None

    state = json.loads(state_file.read_text(encoding="utf-8"))
    state[JWT_FIELD] = effective
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return effective


def save_debug_artifacts(page, prefix: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe_prefix = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in prefix)
    html_path = DEBUG_DIR / f"{safe_prefix}.html"
    screenshot_path = DEBUG_DIR / f"{safe_prefix}.png"
    html_path.write_text(page.content(), encoding="utf-8", errors="replace")
    page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"Saved debug HTML to: {html_path.resolve()}", file=sys.stderr)
    print(f"Saved debug screenshot to: {screenshot_path.resolve()}", file=sys.stderr)


def main() -> None:
    # 必须最先做：本脚本的警告是中文，而 stdout/stderr 在某些代码页（cp1252/ascii）
    # 下打印中文会抛 UnicodeEncodeError。那些警告都在"登录已成功"之后的路径上，
    # 异常冒泡出去就变成非零退出 -> run.ps1 判定登录失败并重试/退避，
    # 而实际上 Cookie 和 JWT 都已经拿到了。
    enable_utf8_stdout()

    parser = argparse.ArgumentParser(description="Log in to UJN WebVPN and save browser session state.")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window.")
    parser.add_argument("--no-state", action="store_true", help="Do not reuse or save login state.")
    parser.add_argument("--no-jwt", action="store_true",
                        help="Skip JWT extraction (only refresh Cookies).")
    parser.add_argument("--state-file", default=str(STATE_FILE), help="Path for Playwright storage state JSON.")
    parser.add_argument("--config", default=str(CONFIG_FILE), help="Path to YAML config with username/password.")
    parser.add_argument("--url", default=LOGIN_URL, help="Login URL.")
    parser.add_argument("--debug", action="store_true", help="Save screenshot and HTML when login fails.")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    state_file = Path(args.state_file)

    with sync_playwright() as p:
        launch_args = []
        if args.headless:
            launch_args.extend(
                [
                    "--no-proxy-server",
                    "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1",
                ]
            )
        browser = p.chromium.launch(headless=args.headless, args=launch_args)
        context_kwargs = {}
        if not args.no_state and state_file.exists():
            context_kwargs["storage_state"] = str(state_file)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        if not is_login_page(page):
            print(f"Already logged in: {page.url}")
        else:
            username, password = get_credentials(config)
            fill_login_form(page, username, password)
            submit_login(page)

            if not wait_for_login(page):
                if args.debug:
                    save_debug_artifacts(page, "webvpn-login-failed")
                print("Login did not complete. Check whether the account, password, or page selectors changed.", file=sys.stderr)
                browser.close()
                sys.exit(1)

            print(f"Logged in: {page.url}")

        if not args.no_state:
            # 先掏 JWT 再写 state：写 state 会覆盖整份文件，
            # 顺序反了会把刚拿到的新 JWT 丢掉。
            #
            # 已有的旧 JWT 不会被沿用 —— 每次登录都重新掏一份。
            # 上游签发新 token 后沿用旧的，正是最难排查的那类失效。
            jwt = None
            if not args.no_jwt:
                jwt = extract_jwt(
                    page,
                    config.get("webvpn_host_query", ""),
                    username=config.get("username", ""),
                    password=config.get("password", ""),
                )

            effective = save_state(context, state_file, jwt)
            print(f"Saved session state to: {state_file.resolve()}")

            if jwt:
                print(f"JWT saved: {jwt[:30]}...")
            elif effective:
                # 掏失败，沿用了上一次的值。明说一句，
                # 免得用户以为这是本次新拿到的凭据。
                print(
                    "  注意：本次未提取到 JWT，沿用了上一次保存的值"
                    f"（{effective[:30]}...）",
                    file=sys.stderr,
                )

        if not args.headless:
            print("Browser will stay open. Press Enter to close it.")
            input()

        browser.close()


if __name__ == "__main__":
    main()
