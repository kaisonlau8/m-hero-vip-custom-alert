#!/usr/bin/env python3
"""DMS automatic re-login (IAM password login + Tencent slider captcha).

Credentials come only from the env file (default ~/m-hero-run/dms-login.env, mode 600):
  DMS_LOGIN_USERNAME / DMS_LOGIN_PASSWORD / DMS_AUTO_LOGIN=1
Secrets are never printed or logged.

Safety:
- fcntl lock (runtime/dms-auto-login.lock) so only one login runs;
- never runs while refresh_block_reason() is set (export lock / registered crawl /
  timetable window, e.g. 08:27 until the 08:30 district crawl finishes) nor 08:25-09:15;
- bounded attempts with backoff, cooldown after a failed episode;
- alert only via the district-form Feishu webhook (existing failure path), rate-limited.
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import subprocess
import time
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ENV_FILE = Path(os.environ.get("DMS_LOGIN_ENV_FILE", "/home/ubuntu/m-hero-run/dms-login.env"))
LOGIN_HOST = "iam-admin.m-hero.com"
DMS_HOST = "m-dms.dfmc.com.cn"
DMS_ORIGIN = "https://m-dms.dfmc.com.cn/"
MAX_ATTEMPTS = int(os.environ.get("DMS_AUTO_LOGIN_MAX_ATTEMPTS", "3"))
BACKOFF = [60, 180, 300]
CAPTCHA_TRIES = 4
COOLDOWN_SEC = 30 * 60
ALERT_MIN_INTERVAL = 2 * 3600
HARD_QUIET = ("08:25", "09:15")
DISTRICT_PY = "/home/ubuntu/m-hero-run/venvs/mhero_district_form/bin/python"
DISTRICT_SCRIPTS = "/home/ubuntu/m-hero-prod/mhero_district_form/scripts"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[auto-login {_now()}] {msg}", flush=True)


def load_creds() -> tuple[str, str, bool]:
    vals: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            vals[k.strip()] = v
    user = vals.get("DMS_LOGIN_USERNAME", "")
    pw = vals.get("DMS_LOGIN_PASSWORD", "")
    enabled = vals.get("DMS_AUTO_LOGIN", "1") not in {"0", "false", "no", ""}
    return user, pw, enabled


# ---------------------------------------------------------------- captcha
def _find_captcha_frame(page):
    for f in page.frames:
        if "turing.captcha" in (f.url or "") or "captcha.qcloud" in (f.url or ""):
            try:
                if f.locator("#slideBg").count():
                    return f
            except Exception:
                pass
    return None


def _decode(content: bytes):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_UNCHANGED)


def find_gap_offset(page, frame) -> float:
    """Return horizontal drag distance (CSS px) using OpenCV edge template matching."""
    import cv2
    import numpy as np

    info = frame.evaluate("""() => {
      const bg = document.querySelector('#slideBg');
      const url = s => (s.match(/url\\("?(.*?)"?\\)/)||[])[1]||'';
      const br = bg.getBoundingClientRect();
      const items = [...document.querySelectorAll('.tc-fg-item')].filter(e => {
        const r = e.getBoundingClientRect(); return !e.classList.contains('tc-slider-normal') &&
          r.width > 30 && Math.abs(r.width - r.height) < 6; });
      const p = items[0]; const pr = p.getBoundingClientRect(); const ps = getComputedStyle(p);
      return {bgUrl: url(getComputedStyle(bg).backgroundImage), bgW: br.width, bgL: br.left, bgT: br.top,
        pUrl: url(ps.backgroundImage), pL: pr.left, pT: pr.top, pW: pr.width, pH: pr.height,
        pos: ps.backgroundPosition, size: ps.backgroundSize};
    }""")
    req = page.context.request
    bg = _decode(req.get(info["bgUrl"]).body())
    sprite = _decode(req.get(info["pUrl"]).body())
    if bg is None or sprite is None:
        raise RuntimeError("captcha image decode failed")
    if bg.ndim == 3 and bg.shape[2] == 4:
        bg = cv2.cvtColor(bg, cv2.COLOR_BGRA2BGR)
    sb = bg.shape[1] / info["bgW"]  # natural px per css px (bg)
    sw = float(info["size"].split()[0].replace("px", ""))
    ss = sprite.shape[1] / sw
    px, py = [float(v.replace("px", "")) for v in info["pos"].split()]
    x0, y0 = int(round(-px * ss)), int(round(-py * ss))
    w, h = int(round(info["pW"] * ss)), int(round(info["pH"] * ss))
    piece = sprite[y0:y0 + h, x0:x0 + w]
    tw, th = max(8, int(round(info["pW"] * sb))), max(8, int(round(info["pH"] * sb)))
    piece = cv2.resize(piece, (tw, th), interpolation=cv2.INTER_AREA)
    if piece.ndim == 3 and piece.shape[2] == 4:
        alpha = piece[:, :, 3]
        tpl = cv2.Canny(alpha, 50, 150)
    else:
        tpl = cv2.Canny(cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY), 50, 150)
    pyn = int(round((info["pT"] - info["bgT"]) * sb))
    pxn = int(round((info["pL"] - info["bgL"]) * sb))
    pad = 6
    y1, y2 = max(0, pyn - pad), min(bg.shape[0], pyn + th + pad)
    band = cv2.cvtColor(bg[y1:y2], cv2.COLOR_BGR2GRAY)
    band = cv2.GaussianBlur(band, (3, 3), 0)
    edges = cv2.Canny(band, 60, 180)
    xs = pxn + tw // 2  # gap is always right of the piece
    region = edges[:, xs:]
    res = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    gap_x = xs + loc[0]
    dist = (gap_x - pxn) / sb
    log(f"captcha gap: score={score:.2f} distance={dist:.1f}px")
    return dist


def human_drag(page, frame, distance: float) -> None:
    iframe_box = None
    for el in page.locator("iframe").all():
        try:
            if el.is_visible() and el.content_frame == frame:
                iframe_box = el.bounding_box(); break
        except Exception:
            continue
    if iframe_box is None:
        iframe_box = page.locator("iframe").filter(visible=True).last.bounding_box()
    hb = frame.locator(".tc-slider-normal").bounding_box()  # relative to page already
    if hb is None:
        raise RuntimeError("slider handle not found")
    # Playwright returns element boxes in main-frame coordinates, even for iframes.
    sx = hb["x"] + hb["width"] / 2 + random.uniform(-4, 4)
    sy = hb["y"] + hb["height"] / 2 + random.uniform(-3, 3)
    page.mouse.move(sx - random.uniform(30, 60), sy + random.uniform(-15, 15), steps=8)
    page.mouse.move(sx, sy, steps=6)
    time.sleep(random.uniform(0.15, 0.4))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.2))
    overshoot = random.uniform(3, 8)
    target = distance + overshoot
    steps = random.randint(28, 42)
    y = sy
    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 3
        y += random.uniform(-0.8, 0.8)
        page.mouse.move(sx + target * ease, y)
        time.sleep(random.uniform(0.008, 0.03) + (0.03 if t > 0.85 else 0))
    for k in range(1, 4):
        page.mouse.move(sx + target - overshoot * k / 3, y + random.uniform(-0.5, 0.5))
        time.sleep(random.uniform(0.05, 0.12))
    time.sleep(random.uniform(0.1, 0.3))
    page.mouse.up()


def solve_captcha(page, tries: int = CAPTCHA_TRIES) -> bool:
    for i in range(1, tries + 1):
        deadline = time.time() + 45  # captcha iframe can load slowly
        frame = None
        while time.time() < deadline and frame is None:
            frame = _find_captcha_frame(page)
            if frame is None:
                time.sleep(0.5)
        if frame is None:
            log("captcha frame not present")
            return False
        try:
            frame.wait_for_function(
                "() => { const b=document.querySelector('#slideBg'); return b && b.getBoundingClientRect().height>50 && getComputedStyle(b).backgroundImage.includes('url'); }",
                timeout=15000)
            time.sleep(random.uniform(0.6, 1.2))
            dist = find_gap_offset(page, frame)
            human_drag(page, frame, dist)
        except Exception as exc:
            log(f"captcha try {i} error: {type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(2.5)
        if _find_captcha_frame(page) is None or _captcha_passed(frame):
            log(f"captcha passed on try {i}")
            return True
        log(f"captcha try {i} not accepted")
        try:
            frame.locator("#reload").click(timeout=3000)
        except Exception:
            pass
        time.sleep(random.uniform(1.5, 3))
    return False


def _captcha_passed(frame) -> bool:
    try:
        return bool(frame.evaluate("() => { const s=document.querySelector('.tc-cover.tc-success'); return !!s && getComputedStyle(s).display!=='none' && s.offsetHeight>0; }"))
    except Exception:
        return True  # frame detached => captcha closed


# ---------------------------------------------------------------- login form
TENANT_KEYWORD = os.environ.get("DMS_LOGIN_TENANT", "猛士")


def select_tenant(page) -> None:
    """The account only works under the 猛士 tenant: 「指定租户登录」 -> pick 猛士汽车科技公司."""
    sel = page.locator("#main #tenant-select")
    if not (sel.count() and sel.is_visible()):
        page.get_by_text("指定租户登录", exact=True).filter(visible=True).first.click()
        sel.wait_for(state="visible", timeout=15000)
    value = page.evaluate(
        "k => { const s=document.querySelector('#main #tenant-select');"
        " const o=[...s.options].find(o=>o.value && o.text.includes(k)); return o ? o.value : ''; }",
        TENANT_KEYWORD)
    if not value:
        raise RuntimeError(f"tenant containing {TENANT_KEYWORD!r} not found")
    sel.select_option(value)
    time.sleep(random.uniform(0.3, 0.6))
    if page.locator("#main #tenant-select").input_value() != value:
        raise RuntimeError("tenant selection did not stick")
    log(f"tenant selected: {value}")


def fill_login_form(page, user: str, pw: str) -> None:
    page.wait_for_selector("#username-input", state="visible", timeout=45000)
    time.sleep(random.uniform(0.5, 1.0))
    select_tenant(page)
    page.locator("#username-input").click()
    page.locator("#username-input").fill("")
    page.locator("#username-input").type(user, delay=random.randint(40, 90))
    pw_box = [e for e in page.locator("input[type=password]").all() if e.is_visible()][0]
    pw_box.click()
    pw_box.fill("")
    pw_box.type(pw, delay=random.randint(40, 90))
    time.sleep(random.uniform(0.3, 0.7))
    page.get_by_text("登录", exact=True).filter(visible=True).last.click()


def login_on_page(page, user: str, pw: str, *, submit_wait: int = 40) -> dict[str, Any]:
    """Fill form, solve captcha, wait for redirect. Returns {'ok', 'stage', 'detail'}."""
    # Always start from a fresh login page: a stale captcha overlay from a previous
    # attempt would otherwise intercept clicks on the form.
    page.goto(DMS_ORIGIN, wait_until="commit", timeout=60000)
    fill_login_form(page, user, pw)
    if not solve_captcha(page):
        return {"ok": False, "stage": "captcha", "detail": "captcha not solved"}
    end = time.time() + submit_wait
    while time.time() < end:
        url = page.url or ""
        host = urlparse(url).netloc  # portal URL carries the DMS host in its query string
        if host == DMS_HOST and "/login" not in url.lower():
            time.sleep(3)
            return {"ok": True, "stage": "done", "detail": url.split("?")[0][:80]}
        try:
            txt = page.evaluate("document.body.innerText") or ""
        except Exception:
            txt = ""
        if "password error" in txt.lower() or "密码错误" in txt:
            return {"ok": False, "stage": "bad_password", "detail": "portal says username/password error"}
        time.sleep(1)
    try:
        body = page.evaluate("document.body.innerText")[:300].replace("\n", " ")
        if pw:
            body = body.replace(pw, "***")
    except Exception:
        body = ""
    return {"ok": False, "stage": "submit", "detail": f"no redirect; page says: {body[:160]}"}


# ---------------------------------------------------------------- orchestration
def _state_path(plugin_root: Path) -> Path:
    from dfmc_browser_utils import get_runtime_dir
    return get_runtime_dir(plugin_root) / "auto-login-state.json"


def _load_state(plugin_root: Path) -> dict:
    try:
        return json.loads(_state_path(plugin_root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(plugin_root: Path, st: dict) -> None:
    p = _state_path(plugin_root)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _in_hard_quiet() -> bool:
    hm = datetime.now().strftime("%H:%M")
    return HARD_QUIET[0] <= hm < HARD_QUIET[1]


def send_alert(text: str) -> dict:
    code = (
        "import sys,json; sys.path.insert(0, %r); from feishu_webhook import post_webhook;"
        "t=sys.stdin.read();"
        "print(json.dumps(post_webhook({'msg_type':'interactive','card':{'header':{'template':'red',"
        "'title':{'tag':'plain_text','content':'DMS 自动补登录失败'}},'elements':[{'tag':'div','text':"
        "{'tag':'lark_md','content':t}}]}}), ensure_ascii=False))" % DISTRICT_SCRIPTS
    )
    try:
        out = subprocess.run([DISTRICT_PY, "-c", code], input=text, capture_output=True, text=True,
                             timeout=60, cwd=str(Path(DISTRICT_SCRIPTS).parent))
        log(f"alert sent: {out.stdout.strip()[:160]} {out.stderr.strip()[-160:]}")
        return {"ok": out.returncode == 0}
    except Exception as exc:
        log(f"alert error: {exc}")
        return {"ok": False}


def _main_login_page(port: int):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
    ctx = browser.contexts[0]
    try:
        ctx.grant_permissions(["local-network-access"], origin=f"https://{LOGIN_HOST}")
    except Exception:
        pass
    page = None
    for pg in ctx.pages:
        u = pg.url or ""
        if LOGIN_HOST in u or DMS_HOST in u:
            page = pg
            if LOGIN_HOST in u:
                break
    if page is None:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, page


def ensure_logged_in(plugin_root: Path, port: int, *, reason: str = "") -> str:
    """Called when the shared session looks logged out. Returns a short result string."""
    from dfmc_browser_utils import get_runtime_dir, inspect_dms_login, refresh_block_reason

    user, pw, enabled = load_creds()
    if not enabled:
        return "auto_login:disabled"
    if not user or not pw:
        log(f"no credentials in {ENV_FILE}")
        return "auto_login:no_creds"
    block = refresh_block_reason(plugin_root)
    if block:
        log(f"skip (blocked: {block})")
        return f"auto_login:skipped:{block}"
    if _in_hard_quiet():
        log("skip (quiet window around 08:30 crawl)")
        return "auto_login:skipped:quiet"
    st = _load_state(plugin_root)
    if time.time() < float(st.get("cooldownUntil") or 0):
        return "auto_login:cooldown"

    lock_path = get_runtime_dir(plugin_root) / "dms-auto-login.lock"
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "auto_login:busy"
        log(f"session logged out ({reason}); starting auto login, max {MAX_ATTEMPTS} attempts")
        last = {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if refresh_block_reason(plugin_root) or _in_hard_quiet():
                log("abort: crawl window started")
                return "auto_login:aborted:crawl_window"
            snap = inspect_dms_login(port)
            if snap.get("status") == "logged_in":
                log("session already logged in")
                return "auto_login:already"
            pwh = None
            try:
                pwh, page = _main_login_page(port)
                last = login_on_page(page, user, pw)
            except Exception as exc:
                last = {"ok": False, "stage": "exception", "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
            finally:
                if pwh:
                    try:
                        pwh.stop()
                    except Exception:
                        pass
            if last.get("ok"):
                snap = inspect_dms_login(port)
                if snap.get("status") == "logged_in":
                    log(f"login OK on attempt {attempt}")
                    _save_state(plugin_root, {"lastResult": "ok", "lastAt": _now(), "attempts": attempt,
                                              "lastAlertAt": st.get("lastAlertAt", 0)})
                    return "auto_login:ok"
                last = {"ok": False, "stage": "verify", "detail": f"status={snap.get('status')}"}
            log(f"attempt {attempt} failed at {last.get('stage')}: {last.get('detail')}")
            if last.get("stage") == "bad_password":
                break  # retrying a rejected password only risks locking the account
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)] * random.uniform(0.9, 1.2))
        st.update({"lastResult": f"failed:{last.get('stage')}", "lastAt": _now(),
                   "cooldownUntil": time.time() + COOLDOWN_SEC})
        if time.time() - float(st.get("lastAlertAt") or 0) >= ALERT_MIN_INTERVAL:
            send_alert(f"DMS 登录已掉线，自动补登录 {MAX_ATTEMPTS} 次都失败（最后一步：{last.get('stage')}）。\n"
                       f"时间：{_now()}\n请在 VNC 浏览器里手动登录。")
            st["lastAlertAt"] = time.time()
        _save_state(plugin_root, st)
        return f"auto_login:failed:{last.get('stage')}"


if __name__ == "__main__":
    import argparse
    import sys
    os.environ.setdefault("DFMC_DMS_SESSION_HOME", "/home/ubuntu/m-hero-run/dms-session")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dfmc_browser_utils import read_browser_state, get_default_state_file
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    port = int(read_browser_state(get_default_state_file(root)).get("port") or 0)
    from dfmc_browser_utils import inspect_dms_login
    s = inspect_dms_login(port).get("status")
    print("status:", s)
    if s == "logged_out" and not a.check_only:
        print(ensure_logged_in(root, port, reason="manual"))
