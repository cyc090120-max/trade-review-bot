"""Saved logins (Tradovate, Discord) for the headless browser on the server.

The login itself is done by hand on the Mac with a visible browser (reviewbot/login.py, ./login.sh);
the result is saved under secrets/ and copied to the server.

    python -m reviewbot.session check              # 检查两个登录是否有效，失效就发 Discord 提醒
    python -m reviewbot.session check tradovate
    python -m reviewbot.session keepalive          # 续期 Tradovate 登录，定时任务每 20 分钟跑一次
"""
from __future__ import annotations

import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SITES = {
    "tradovate": {"name": "Tradovate", "url": "https://trader.tradovate.com/", "domain": "tradovate.com",
                  "indexed_db": True, "done": "看到交易界面（图表或账户信息）就算登录好了。",
                  # the web app lists your accounts right after it accepts the login (FN accounts are on demo)
                  "proof": r"tradovateapi\.com/v1/account/list"},
    "discord": {"name": "Discord", "url": "https://discord.com/channels/@me", "domain": "discord.com",
                "indexed_db": False, "done": "看到左边一列服务器图标就算登录好了。",
                # the web app only gets a 200 from its own-account endpoints when the login is valid
                "proof": r"/api/v\d+/users/@me"},
}


class LoginExpired(RuntimeError):
    pass


def relogin_hint(site: str) -> str:
    return f"{SITES[site]['name']} 登录已失效，请在 Mac 上打开终端运行：cd ~/trade-review-bot && ./login.sh {site}"


def state_file(root: Path, site: str) -> Path:
    return root / "secrets" / f"{site}_state.json"


def session_file(root: Path, site: str) -> Path:
    return root / "secrets" / f"{site}_session.json"


def looks_logged_in(page, wait_s: float = 12, proven=None) -> bool:
    """Logged out = the page lands on a login URL or shows a password box within wait_s seconds.
    With `proven` (a callable), it must also turn True before the deadline to count as logged in."""
    deadline = time.monotonic() + wait_s
    password = page.locator("input[type=password]").first
    while True:
        if re.search(r"/(login|signin|sign-in)\b", page.url, re.I):
            return False
        try:
            if password.is_visible():
                return False
        except Exception:  # page still navigating
            pass
        if proven and proven():
            return True
        if time.monotonic() >= deadline:
            return proven() if proven else True
        page.wait_for_timeout(500)


def _restore_session_js(origin: str, items: dict) -> str:
    # sessionStorage isn't part of Playwright's storage_state, so put it back before the site's own scripts run
    return (f"if (location.origin === {json.dumps(origin)}) {{"
            f" for (const [k, v] of Object.entries({json.dumps(items)}))"
            f" if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); }}")


@contextmanager
def open_page(root: Path, site: str):
    """Headless page that carries the login saved on the Mac."""
    from playwright.sync_api import sync_playwright

    from .card import launch_chromium

    st = state_file(root, site)
    if not st.exists():
        raise LoginExpired(f"还没有保存过 {SITES[site]['name']} 的登录。" + relogin_hint(site))
    with sync_playwright() as p:
        browser = launch_chromium(p)
        # headless Chromium says "HeadlessChrome" in its user agent; present it as a normal Chrome on Linux
        major = browser.version.split(".")[0]
        ua = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ctx = browser.new_context(storage_state=str(st), user_agent=ua, viewport={"width": 1440, "height": 900})
        sf = session_file(root, site)
        if sf.exists():
            saved = json.loads(sf.read_text(encoding="utf-8"))
            if saved.get("items"):
                ctx.add_init_script(script=_restore_session_js(saved["origin"], saved["items"]))
        try:
            yield ctx.new_page()
        finally:
            browser.close()


def read_session_storage(page) -> dict:
    # Discord deletes window.sessionStorage from the page, so this can come back empty
    return page.evaluate("(() => { try { return Object.fromEntries(Object.entries(sessionStorage)); }"
                         " catch (e) { return {}; } })()")


def save_state(page, root: Path, site: str) -> None:
    """Write back refreshed cookies and session tokens so the login lasts as long as the site allows.
    Tradovate keeps its ~80-minute access token in sessionStorage and renews it while the page is open."""
    page.context.storage_state(path=str(state_file(root, site)), indexed_db=SITES[site]["indexed_db"])
    items = read_session_storage(page)
    origin = page.evaluate("location.origin")
    if items and origin.split("//")[-1].endswith(SITES[site]["domain"]):
        session_file(root, site).write_text(json.dumps({"origin": origin, "items": items}), encoding="utf-8")


def renew_tradovate(root: Path) -> str:
    """Renew the ~80-minute Tradovate token the way the web app does; returns the new expiration.
    The web app cannot recover once the token has expired, so this has to run well before that."""
    import requests

    sf = session_file(root, "tradovate")
    if not sf.exists():
        raise LoginExpired(relogin_hint("tradovate"))
    saved = json.loads(sf.read_text(encoding="utf-8"))
    auth = json.loads(saved["items"]["api_authenticator_state"])
    host = auth["apiHosts"][auth.get("environment", "demo")]
    r = requests.get(f"https://{host}/v1/auth/renewaccesstoken",
                     headers={"Authorization": f"Bearer {auth['token']}"}, timeout=30)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 401 or (r.ok and not data.get("accessToken")):
        raise LoginExpired(relogin_hint("tradovate"))
    r.raise_for_status()
    auth["token"], auth["expiration"] = data["accessToken"], data["expirationTime"]
    saved["items"]["api_authenticator_state"] = json.dumps(auth)
    sf.write_text(json.dumps(saved), encoding="utf-8")
    return data["expirationTime"]


def keepalive(root: Path, webhook: str) -> bool:
    """Run every 20 minutes. Alerts once when the login is gone, not on every run."""
    flag = root / "secrets" / "tradovate_alerted"
    try:
        exp = renew_tradovate(root)
    except LoginExpired as e:
        print(f"❌ {e}")
        if webhook and not flag.exists():
            from .push import send_text
            send_text(webhook, f"🔑 {e}")
            flag.touch()
        return False
    flag.unlink(missing_ok=True)
    print(f"✅ Tradovate 登录已续期，有效到 {exp}")
    return True


def check(root: Path, site: str) -> bool:
    try:
        with open_page(root, site) as page:
            proof, seen = SITES[site].get("proof"), []
            if proof:
                page.on("response", lambda r: r.ok and re.search(proof, r.url) and seen.append(r.url))
            page.goto(SITES[site]["url"], wait_until="domcontentloaded", timeout=60000)
            ok = looks_logged_in(page, wait_s=25, proven=(lambda: bool(seen)) if proof else None)
            if ok:
                save_state(page, root, site)
            return ok
    except LoginExpired:
        return False


def require(root: Path, site: str) -> None:
    if not check(root, site):
        raise LoginExpired(relogin_hint(site))


def main() -> None:
    from .config import load_config
    from .push import send_text

    args = sys.argv[1:]
    cfg = load_config()
    if args == ["keepalive"]:
        sys.exit(0 if keepalive(cfg["_root"], cfg["discord_webhook_url"]) else 1)
    if not args or args[0] != "check" or any(s not in SITES for s in args[1:]):
        sys.exit("用法：python -m reviewbot.session check [tradovate] [discord]\n"
                 "      python -m reviewbot.session keepalive      # 续期 Tradovate 登录（每 20 分钟自动运行）")
    bad = []
    for site in args[1:] or list(SITES):
        ok = check(cfg["_root"], site)
        print(f"{'✅' if ok else '❌'} {SITES[site]['name']} 登录{'有效' if ok else '已失效'}")
        if not ok:
            bad.append(site)
    if bad and cfg["discord_webhook_url"]:
        send_text(cfg["discord_webhook_url"], "🔑 " + "\n🔑 ".join(relogin_hint(s) for s in bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
