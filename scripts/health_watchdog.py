#!/usr/bin/env python3
"""监控 VIP 保养提醒控制台；挂掉时通过 HeroClaw 通知指定人。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dotenv import load_dotenv

load_dotenv(PLUGIN_ROOT / ".env")

from feishu_client import resolve_phone_to_open_id, send_text_message  # noqa: E402
from time_utils import beijing_strftime  # noqa: E402

STATE_PATH = PLUGIN_ROOT / ".runtime" / "health-watchdog-state.json"
DEFAULT_URL = f"http://{os.getenv('CONSOLE_HOST') or '127.0.0.1'}:{os.getenv('CONSOLE_PORT') or '9002'}/api/vip/status"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"healthy": True, "fail_streak": 0, "last_alert_at": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"healthy": True, "fail_streak": 0, "last_alert_at": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_health(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            data = json.loads(body)
            if not isinstance(data, dict):
                return False, "响应非 JSON 对象"
            return True, "ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def resolve_alert_open_id() -> str | None:
    open_id = (os.getenv("WATCHDOG_OPEN_ID") or "").strip()
    if open_id:
        return open_id
    phone = (os.getenv("WATCHDOG_MOBILE") or os.getenv("ADMIN_MOBILE") or "").strip()
    if not phone:
        phone = "19272720822"  # 刘明轩默认
    return resolve_phone_to_open_id(phone)


def maybe_alert(state: dict, *, ok: bool, detail: str, url: str, cooldown_sec: int) -> None:
    now = time.time()
    if ok:
        if not state.get("healthy", True):
            # 恢复通知
            oid = resolve_alert_open_id()
            if oid:
                send_text_message(
                    oid,
                    f"✅ VIP 保养提醒服务已恢复\n时间：{beijing_strftime('%Y-%m-%d %H:%M:%S')}\n探测：{url}",
                )
            print(f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] recovered, notified")
        state["healthy"] = True
        state["fail_streak"] = 0
        _save_state(state)
        return

    state["healthy"] = False
    state["fail_streak"] = int(state.get("fail_streak") or 0) + 1
    state["last_error"] = detail
    state["last_fail_at"] = beijing_strftime("%Y-%m-%d %H:%M:%S")

    last_alert = float(state.get("last_alert_at") or 0)
    # 连续失败 ≥2 次再告警，且遵守冷却
    if state["fail_streak"] >= 2 and (now - last_alert) >= cooldown_sec:
        oid = resolve_alert_open_id()
        if oid:
            send_text_message(
                oid,
                "🚨 VIP 保养提醒服务异常\n"
                f"时间：{beijing_strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"探测：{url}\n"
                f"原因：{detail}\n"
                f"连续失败：{state['fail_streak']} 次",
            )
            state["last_alert_at"] = now
            print(f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] ALERT sent: {detail}")
        else:
            print(f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] ALERT skipped: no open_id")
    else:
        print(
            f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] unhealthy "
            f"streak={state['fail_streak']}: {detail}"
        )
    _save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="VIP alert health watchdog")
    parser.add_argument("--url", default=os.getenv("WATCHDOG_URL") or DEFAULT_URL)
    parser.add_argument("--interval", type=int, default=int(os.getenv("WATCHDOG_INTERVAL") or "60"))
    parser.add_argument("--cooldown", type=int, default=int(os.getenv("WATCHDOG_COOLDOWN") or "1800"))
    parser.add_argument("--once", action="store_true", help="只探测一次")
    args = parser.parse_args()

    print(f"Watchdog start url={args.url} interval={args.interval}s cooldown={args.cooldown}s")
    while True:
        ok, detail = check_health(args.url)
        state = _load_state()
        maybe_alert(state, ok=ok, detail=detail, url=args.url, cooldown_sec=args.cooldown)
        if args.once:
            return 0 if ok else 1
        time.sleep(max(args.interval, 10))


if __name__ == "__main__":
    raise SystemExit(main())
