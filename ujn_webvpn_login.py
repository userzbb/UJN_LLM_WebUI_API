import argparse
import getpass
import os
import sys
from pathlib import Path

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOGIN_URL = "https://webvpn.ujn.edu.cn"
STATE_FILE = Path("ujn_webvpn_state.json")
CONFIG_FILE = Path("config.yaml")
DEBUG_DIR = Path("debug")


def load_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a YAML mapping.")

    return {str(key): "" if value is None else str(value) for key, value in data.items()}


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
    parser = argparse.ArgumentParser(description="Log in to UJN WebVPN and save browser session state.")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window.")
    parser.add_argument("--no-state", action="store_true", help="Do not reuse or save login state.")
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
            context.storage_state(path=str(state_file))
            print(f"Saved session state to: {state_file.resolve()}")

        if not args.headless:
            print("Browser will stay open. Press Enter to close it.")
            input()

        browser.close()


if __name__ == "__main__":
    main()
