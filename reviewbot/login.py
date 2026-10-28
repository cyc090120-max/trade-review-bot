"""Log in by hand on the Mac and save the login for the server's headless browser.

    ./login.sh                 # Tradovate 和 Discord 都登录一遍
    ./login.sh tradovate       # 只登录 Tradovate

Uses your real Google Chrome with its own profile in secrets/mac-profile, so next time you are
usually still logged in and only need to press Enter.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from .config import ROOT
from .session import SITES, looks_logged_in, session_file, state_file

PROFILE = ROOT / "secrets" / "mac-profile"


def save(page, site: str) -> None:
    domain = SITES[site]["domain"]
    state = page.context.storage_state(indexed_db=SITES[site]["indexed_db"])
    # the Mac profile is shared by both sites: keep only this site's cookies and storage
    state["cookies"] = [c for c in state["cookies"] if c["domain"].lstrip(".").endswith(domain)]
    state["origins"] = [o for o in state["origins"] if o["origin"].split("//")[-1].endswith(domain)]
    # Discord deletes window.sessionStorage / localStorage from the page, so this can come back empty
    items = page.evaluate("(() => { try { return Object.fromEntries(Object.entries(sessionStorage)); }"
                          " catch (e) { return {}; } })()")
    info = {  # names only, no values: tells us where each site keeps its login
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": page.url,
        "user_agent": page.evaluate("navigator.userAgent"),
        "cookie_domains": sorted({c["domain"] for c in state["cookies"]}),
        "local_storage_keys": {o["origin"]: sorted(i["name"] for i in o["localStorage"]) for o in state["origins"]},
        "indexed_db": {o["origin"]: [d["name"] for d in o.get("indexedDB", [])] for o in state["origins"]},
        "session_storage_keys": sorted(items),
    }
    files = {
        state_file(ROOT, site): state,
        session_file(ROOT, site): {"origin": page.evaluate("location.origin"), "items": items},
        ROOT / "secrets" / f"{site}_info.json": info,
    }
    for path, data in files.items():
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)


def login(page, site: str) -> None:
    s = SITES[site]
    page.goto(s["url"], wait_until="domcontentloaded")
    print(f"\n▶ {s['name']}：请在弹出的 Chrome 窗口里登录（验证码、二次验证都在那个窗口里完成）。")
    print(f"  {s['done']}")
    while True:
        input("  登录好之后，回到这里按回车 ⏎ ")
        if looks_logged_in(page, wait_s=2):
            break
        print("  ⚠️ 看起来还没登录成功（页面上还有密码框，或者还停在登录页）。登录好再按回车。")
    save(page, site)
    print(f"  ✅ 已保存 {s['name']} 的登录状态")


def main() -> None:
    from playwright.sync_api import sync_playwright

    sites = sys.argv[1:] or list(SITES)
    unknown = [s for s in sites if s not in SITES]
    if unknown:
        sys.exit(f"不认识：{' '.join(unknown)}。可选：{' / '.join(SITES)}")
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, no_viewport=True)
        except Exception:
            print("没找到 Google Chrome，改用 Playwright 自带的 Chromium。")
            ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=False, no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for site in sites:
            login(page, site)
        ctx.close()


if __name__ == "__main__":
    main()
