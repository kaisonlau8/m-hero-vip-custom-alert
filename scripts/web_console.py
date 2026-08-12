#!/usr/bin/env python3
"""Lightweight Web console for DMS browser login + online/keepalive monitoring."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent

# Load optional .env (DFMC_DMS_SESSION_HOME, etc.)
_env_file = PLUGIN_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        for line in _env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value

sys.path.insert(0, str(SCRIPT_DIR))

from time_utils import (  # noqa: E402
    beijing_iso,
    beijing_now,
    beijing_strftime,
    ensure_beijing_tz,
    from_timestamp_beijing,
)

ensure_beijing_tz()

from dfmc_browser_utils import (  # noqa: E402
    DEFAULT_BROWSER_CANDIDATES,
    DEFAULT_BROWSER_NAME,
    DEFAULT_TARGET_URL,
    SESSION_HOME_ENV,
    browser_label,
    cdp_is_ready,
    detect_browser,
    dms_page_alive,
    find_free_port,
    get_browser_profile_dir,
    get_runtime_dir,
    get_session_home,
    is_cdp_browser_command,
    process_is_running,
    recover_browser_state,
    resolve_executable_from_command,
    write_browser_state,
)

app = Flask(
    __name__,
    template_folder=str(PLUGIN_ROOT / "templates"),
)

# Session paths honor DFMC_DMS_SESSION_HOME for cross-crawler sharing
RUNTIME_DIR = get_runtime_dir(PLUGIN_ROOT)
BROWSER_PROFILE_DIR = get_browser_profile_dir(PLUGIN_ROOT)
SESSION_HOME = get_session_home(PLUGIN_ROOT)

keepalive_state = {
    "thread": None,
    "stop": False,
}

RECORDINGS_DIR = PLUGIN_ROOT / "recordings"
RECORDER_STATE_FILE = RUNTIME_DIR / "recorder-state.json"
RECORDER_STOP_FILE = RUNTIME_DIR / "stop-recording"

RECORDINGS_DIR = PLUGIN_ROOT / "recordings"
RECORDER_STATE_FILE = RUNTIME_DIR / "recorder-state.json"
RECORDER_STOP_FILE = RUNTIME_DIR / "stop-recording"
RECORDER_LOG_FILE = RUNTIME_DIR / "recorder.log"


def _now_iso() -> str:
    return beijing_iso()


def _pid_exists(pid: int) -> bool:
    return process_is_running(pid)


def _cdp_port_alive(port: int) -> bool:
    return cdp_is_ready(port)


def _dms_page_alive(port: int) -> bool:
    return dms_page_alive(port)


def _find_existing_browser() -> dict | None:
    """Scan for a CDP browser using this session's profile directory."""
    profile_dir = str(BROWSER_PROFILE_DIR)
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if profile_dir not in line:
                continue
            if not is_cdp_browser_command(line):
                continue

            parts = line.split(None, 1)
            new_pid = int(parts[0])
            cmd = parts[1] if len(parts) > 1 else ""

            new_port = 0
            for arg in cmd.split():
                if arg.startswith("--remote-debugging-port="):
                    new_port = int(arg.split("=")[1])
                    break

            if new_port and _cdp_port_alive(new_port):
                return {
                    "pid": new_pid,
                    "port": new_port,
                    "browserExecutable": resolve_executable_from_command(cmd),
                    "browserProfileDir": profile_dir,
                    "targetUrl": DEFAULT_TARGET_URL,
                    "startedAt": "",
                }
    except Exception:
        pass
    return None


def _load_browser_state() -> dict | None:
    path = RUNTIME_DIR / "browser-state.json"
    if not path.exists():
        return _find_existing_browser()

    with open(path, encoding="utf-8") as f:
        state = json.load(f)

    pid = state.get("pid", 0)
    port = state.get("port", 0)

    if pid and _pid_exists(pid) and port and _cdp_port_alive(port):
        return state

    recovered = _find_existing_browser()
    if recovered:
        write_browser_state(path, recovered)
        return recovered

    # Also try utils recovery (rewrites state file)
    port = recover_browser_state(path, PLUGIN_ROOT)
    if port and path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return state


def _detect_available_browsers() -> list[dict]:
    env_path = os.environ.get("DFMC_DMS_BROWSER_EXECUTABLE")
    env_default = bool(env_path and Path(env_path).exists())
    browsers = []
    if env_default:
        browsers.append({
            "name": "custom",
            "path": env_path,
            "available": True,
            "label": f"Chromium ({Path(env_path).name})",
            "default": True,
        })
    for name, path in DEFAULT_BROWSER_CANDIDATES.items():
        browsers.append({
            "name": name,
            "label": browser_label(name),
            "available": Path(path).exists(),
            "default": (not env_default) and name == DEFAULT_BROWSER_NAME,
        })
    return browsers


def _load_keepalive_runtime() -> dict:
    path = RUNTIME_DIR / "keepalive-state.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


def _save_keepalive_runtime(data: dict) -> None:
    path = RUNTIME_DIR / "keepalive-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _keepalive_process_running() -> bool:
    state = _load_keepalive_runtime()
    pid = int(state.get("pid") or 0)
    if pid <= 0 or not process_is_running(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return False

    output = result.stdout.strip()
    if not output:
        return False
    stat = output.split(None, 1)[0]
    if "Z" in stat:
        return False
    return "keepalive_browser.py" in output


def _coerce_epoch(value) -> int:
    if value in (None, "", 0):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(datetime.fromisoformat(value).timestamp())
            except ValueError:
                return 0
    return 0


def _get_keepalive_info() -> dict:
    state = _load_keepalive_runtime()
    running = _keepalive_process_running()
    now_ts = int(time.time())
    next_refresh_at = _coerce_epoch(state.get("nextRefreshAt"))
    interval = int(state.get("interval") or 300)
    started_at = _coerce_epoch(state.get("startedAt"))
    if running and not next_refresh_at and started_at:
        next_refresh_at = started_at + interval
    return {
        "running": running,
        "pid": int(state.get("pid") or 0),
        "interval": interval,
        "started_at_epoch": started_at,
        "last_action_at_epoch": _coerce_epoch(state.get("lastActionAt")),
        "next_refresh_at_epoch": next_refresh_at,
        "seconds_left": max(next_refresh_at - now_ts, 0) if running and next_refresh_at else 0,
        "last_result": state.get("lastResult", ""),
        "cycle": int(state.get("cycle") or 0),
    }


def _stop_keepalive_process() -> None:
    state = _load_keepalive_runtime()
    pid = int(state.get("pid") or 0)
    if pid > 0 and _keepalive_process_running():
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    path = RUNTIME_DIR / "keepalive-state.json"
    if path.exists():
        path.unlink()


def _ensure_keepalive_process() -> bool:
    browser = _load_browser_state()
    if not browser:
        _stop_keepalive_process()
        return False

    port = int(browser.get("port") or 0)
    if not port or not _cdp_port_alive(port) or not _dms_page_alive(port):
        _stop_keepalive_process()
        return False

    if _keepalive_process_running():
        return True

    keepalive_script = PLUGIN_ROOT / "scripts" / "keepalive_browser.py"
    python_bin = PLUGIN_ROOT / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    try:
        now_ts = int(time.time())
        interval = 300
        proc = subprocess.Popen(
            [
                str(python_bin),
                str(keepalive_script),
                "--state-file",
                str(RUNTIME_DIR / "browser-state.json"),
                "--status-file",
                str(RUNTIME_DIR / "keepalive-state.json"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False

    _save_keepalive_runtime({
        "pid": proc.pid,
        "interval": interval,
        "startedAt": now_ts,
        "lastResult": "starting",
        "lastActionAt": 0,
        "nextRefreshAt": now_ts + interval,
    })
    return True


def _keepalive_watchdog_loop() -> None:
    while not keepalive_state["stop"]:
        try:
            _ensure_keepalive_process()
        except Exception:
            pass
        time.sleep(30)


def _start_keepalive_watchdog() -> None:
    thread = keepalive_state.get("thread")
    if thread and thread.is_alive():
        return
    keepalive_state["stop"] = False
    thread = threading.Thread(target=_keepalive_watchdog_loop, daemon=True)
    keepalive_state["thread"] = thread
    thread.start()


def _load_recorder_state() -> dict:
    if RECORDER_STATE_FILE.exists():
        try:
            data = json.loads(RECORDER_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_recorder_state(data: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RECORDER_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_recorder_state() -> None:
    RECORDER_STATE_FILE.unlink(missing_ok=True)
    RECORDER_STOP_FILE.unlink(missing_ok=True)


def _recorder_process_running() -> bool:
    state = _load_recorder_state()
    pid = int(state.get("pid") or 0)
    if pid <= 0 or not process_is_running(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return False
    return "record_dfmc_dms.py" in (result.stdout or "")


def _get_recorder_info() -> dict:
    state = _load_recorder_state()
    running = _recorder_process_running()
    if not running and state:
        # Stale state — clear quietly
        if state.get("pid"):
            _clear_recorder_state()
            state = {}
    return {
        "running": running,
        "pid": int(state.get("pid") or 0) if running else 0,
        "session_name": state.get("sessionName", ""),
        "session_dir": state.get("sessionDir", ""),
        "started_at": state.get("startedAt", ""),
        "log_file": state.get("logFile", ""),
    }


def _list_recordings(limit: int = 20) -> list[dict]:
    if not RECORDINGS_DIR.exists():
        return []
    items: list[dict] = []
    for path in sorted(RECORDINGS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        events = path / "events.jsonl"
        summary = path / "summary.json"
        event_count = 0
        if events.exists():
            try:
                event_count = sum(1 for _ in events.open(encoding="utf-8") if _.strip())
            except Exception:
                event_count = 0
        items.append({
            "name": path.name,
            "path": str(path),
            "modified": from_timestamp_beijing(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "event_count": event_count,
            "has_summary": summary.exists(),
        })
        if len(items) >= limit:
            break
    return items


def _start_recorder(session_name: str = "") -> dict:
    if _recorder_process_running():
        info = _get_recorder_info()
        return {
            "started": False,
            "error": "录制已在进行中",
            "recorder": info,
        }

    browser = _load_browser_state()
    if not browser:
        return {"started": False, "error": "请先启动登录浏览器"}
    port = int(browser.get("port") or 0)
    if not port or not _cdp_port_alive(port):
        return {"started": False, "error": "浏览器 CDP 不在线，请先启动登录"}

    python_bin = PLUGIN_ROOT / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    recorder_script = PLUGIN_ROOT / "scripts" / "record_dfmc_dms.py"
    state_file = RUNTIME_DIR / "browser-state.json"
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RECORDER_STOP_FILE.unlink(missing_ok=True)

    log_file = RUNTIME_DIR / "recorder.log"
    name = (session_name or "").strip() or beijing_strftime("console-%H%M%S")
    cmd = [
        str(python_bin),
        str(recorder_script),
        "--attach-existing",
        "--state-file",
        str(state_file),
        "--browser-profile-dir",
        str(BROWSER_PROFILE_DIR),
        "--session-name",
        name,
        "--stop-file",
        str(RECORDER_STOP_FILE),
        "--output-dir",
        str(PLUGIN_ROOT),
    ]

    log_fh = open(log_file, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(PLUGIN_ROOT),
        )
    except Exception as exc:
        log_fh.close()
        return {"started": False, "error": f"启动录制失败: {exc}"}

    # Wait briefly for session dir to appear
    time.sleep(1.5)
    session_dir = ""
    try:
        dirs = sorted(
            [p for p in RECORDINGS_DIR.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if dirs:
            session_dir = str(dirs[0])
    except Exception:
        pass

    payload = {
        "pid": proc.pid,
        "sessionName": name,
        "sessionDir": session_dir,
        "startedAt": _now_iso(),
        "logFile": str(log_file),
        "stopFile": str(RECORDER_STOP_FILE),
    }
    _save_recorder_state(payload)
    return {
        "started": True,
        "message": "已附着到当前浏览器开始录制，请在 DMS 中操作；结束后点击停止。",
        "recorder": _get_recorder_info(),
    }


def _stop_recorder() -> dict:
    state = _load_recorder_state()
    pid = int(state.get("pid") or 0)
    session_dir = state.get("sessionDir", "")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RECORDER_STOP_FILE.write_text("stop\n", encoding="utf-8")

    if pid > 0 and process_is_running(pid):
        # Give stop-file a moment, then SIGTERM
        for _ in range(10):
            time.sleep(0.3)
            if not process_is_running(pid):
                break
        if process_is_running(pid):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            for _ in range(10):
                time.sleep(0.3)
                if not process_is_running(pid):
                    break

    _clear_recorder_state()
    return {
        "stopped": True,
        "session_dir": session_dir,
        "message": "录制已停止" + (f"，输出目录: {session_dir}" if session_dir else ""),
    }


@app.route("/")
def dashboard():
    browser = _load_browser_state()
    browser_ok = False
    if browser:
        port = int(browser.get("port") or 0)
        browser_ok = bool(port and _cdp_port_alive(port) and _dms_page_alive(port))
        if browser_ok:
            _ensure_keepalive_process()

    keepalive_info = _get_keepalive_info()
    recorder_info = _get_recorder_info()
    recordings = _list_recordings(10)
    return render_template(
        "dashboard.html",
        browser=browser,
        browser_ok=browser_ok,
        keepalive_info=keepalive_info,
        recorder_info=recorder_info,
        recordings=recordings,
        available_browsers=_detect_available_browsers(),
        session_home=str(SESSION_HOME),
        session_home_env=SESSION_HOME_ENV,
        session_shared=bool((os.environ.get(SESSION_HOME_ENV) or "").strip()),
        Path=Path,
    )


@app.route("/api/status")
def api_status():
    browser = _load_browser_state()
    browser_info = {}
    browser_ok = False
    if browser:
        pid = browser.get("pid", 0)
        port = int(browser.get("port") or 0)
        browser_ok = bool(port and _cdp_port_alive(port) and _dms_page_alive(port))
        if browser_ok:
            _ensure_keepalive_process()
        browser_info = {
            "browser": Path(browser.get("browserExecutable", "")).stem,
            "pid": pid,
            "port": port,
            "started_at": browser.get("startedAt", ""),
            "alive": browser_ok,
            "cdp_alive": _cdp_port_alive(port) if port else False,
            "dms_page": _dms_page_alive(port) if port else False,
            "keepalive_running": _keepalive_process_running(),
        }
    keepalive_info = _get_keepalive_info()
    recorder_info = _get_recorder_info()
    return jsonify({
        "browser_ok": browser_ok,
        "browser_info": browser_info,
        "available_browsers": _detect_available_browsers(),
        "keepalive_running": keepalive_info["running"],
        "keepalive_info": keepalive_info,
        "recorder": recorder_info,
        "recordings": _list_recordings(10),
        "session_home": str(SESSION_HOME),
        "session_shared": bool((os.environ.get(SESSION_HOME_ENV) or "").strip()),
    })


@app.route("/api/recorder/start", methods=["POST"])
def api_recorder_start():
    payload = request.json or {}
    session_name = str(payload.get("session_name") or "").strip()
    result = _start_recorder(session_name)
    status = 200 if result.get("started") else 400
    return jsonify(result), status


@app.route("/api/recorder/stop", methods=["POST"])
def api_recorder_stop():
    return jsonify(_stop_recorder())


@app.route("/api/recorder/list")
def api_recorder_list():
    return jsonify({
        "recorder": _get_recorder_info(),
        "recordings": _list_recordings(30),
    })


@app.route("/api/browser/launch", methods=["POST"])
def api_browser_launch():
    browser_name = request.json.get("browser", DEFAULT_BROWSER_NAME) if request.json else DEFAULT_BROWSER_NAME

    existing_state = _load_browser_state()
    if existing_state and _cdp_port_alive(int(existing_state.get("port") or 0)):
        port = int(existing_state.get("port") or 0)
        if not _dms_page_alive(port):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new?https%3A%2F%2Fm-dms.dfmc.com.cn",
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
        write_browser_state(RUNTIME_DIR / "browser-state.json", existing_state)
        _ensure_keepalive_process()
        return jsonify({
            "launched": True,
            "reused": True,
            "browser": Path(existing_state["browserExecutable"]).stem if existing_state.get("browserExecutable") else "未知",
            "pid": existing_state["pid"],
            "port": port,
            "message": "浏览器已在运行，直接使用当前会话",
        })

    try:
        browser_executable = detect_browser(browser_name, None)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400

    port = find_free_port()
    browser_profile_dir = BROWSER_PROFILE_DIR
    target_url = DEFAULT_TARGET_URL

    cmd = [
        str(browser_executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={browser_profile_dir}",
        "--no-first-run",
        "--disable-default-apps",
        target_url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return jsonify({"error": f"启动浏览器失败: {exc}"}), 500

    time.sleep(3)

    if not _pid_exists(proc.pid):
        recovered = _find_existing_browser()
        if recovered and _cdp_port_alive(int(recovered.get("port") or 0)):
            write_browser_state(RUNTIME_DIR / "browser-state.json", recovered)
            return jsonify({
                "launched": True,
                "reused": True,
                "browser": Path(recovered["browserExecutable"]).stem if recovered.get("browserExecutable") else "未知",
                "pid": recovered["pid"],
                "port": recovered["port"],
                "message": "新浏览器因 profile 冲突退出，已自动连接到现有浏览器会话",
            })
        return jsonify({"error": "浏览器启动后立即退出（可能已有同 profile 浏览器在运行）"}), 500

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    write_browser_state(RUNTIME_DIR / "browser-state.json", {
        "port": port,
        "pid": proc.pid,
        "browserExecutable": str(browser_executable),
        "browserProfileDir": str(browser_profile_dir),
        "targetUrl": target_url,
        "startedAt": _now_iso(),
    })

    app_name = browser_executable.stem
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass

    _ensure_keepalive_process()

    return jsonify({
        "launched": True,
        "browser": app_name,
        "pid": proc.pid,
        "port": port,
        "message": f"{app_name} 已打开 DMS 系统，请在浏览器中手动登录",
    })


@app.route("/api/browser/disconnect", methods=["POST"])
def api_browser_disconnect():
    _stop_keepalive_process()
    state_file = RUNTIME_DIR / "browser-state.json"
    if state_file.exists():
        state_file.unlink()
    return jsonify({"disconnected": True})


# ── 飞书卡片回传（督导闭环）────────────────────────────────

def _feishu_verification_token() -> str:
    return (os.getenv("FEISHU_VERIFICATION_TOKEN") or "").strip()


def _feishu_encrypt_key() -> str:
    return (os.getenv("FEISHU_ENCRYPT_KEY") or "").strip()


def _decrypt_feishu_encrypt(encrypt_b64: str, encrypt_key: str) -> dict:
    """飞书事件加密包解密（AES-256-CBC + SHA256(key)）。"""
    import base64
    import hashlib

    from Crypto.Cipher import AES

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    raw = base64.b64decode(encrypt_b64)
    iv, ciphertext = raw[:16], raw[16:]
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    pad = plain[-1]
    if isinstance(pad, int) and 1 <= pad <= 16:
        plain = plain[:-pad]
    return json.loads(plain.decode("utf-8"))


def _maybe_decrypt_feishu_body(raw: bytes) -> dict:
    """解析回调 JSON；若含 encrypt 则用 FEISHU_ENCRYPT_KEY 解密。"""
    data = json.loads(raw.decode("utf-8") or "{}")
    encrypt = data.get("encrypt")
    if not encrypt:
        return data
    key = _feishu_encrypt_key()
    if not key:
        raise RuntimeError("收到加密回调但未配置 FEISHU_ENCRYPT_KEY")
    return _decrypt_feishu_encrypt(encrypt, key)


def _tracking_fields_to_payload(fields: dict) -> dict:
    def _t(key: str) -> str:
        val = fields.get(key)
        if val is None:
            return ""
        if isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("name") or ""))
                else:
                    parts.append(str(item))
            return "、".join(p for p in parts if p)
        if isinstance(val, dict):
            return str(val.get("text") or val.get("name") or "")
        return str(val)

    return {
        "store_name": _t("门店名称"),
        "store_code": _t("门店编码"),
        "region": _t("区域"),
        "vin": _t("VIN"),
        "name": _t("客户姓名"),
        "customer_category": _t("客户类别"),
        "vip_level": _t("VIP级别"),
        "vip_attrs": _t("VIP属性"),
        "series": _t("车系"),
        "aftersales_series": _t("售后车系"),
        "task_type": _t("任务类型"),
        "task_status": _t("任务状态"),
        "driver_name": _t("用车人名称"),
        "owner_name": _t("车主名称"),
        "next_appointment_at": _t("下次预约时间"),
        "due_at": _t("到期日期"),
        "created_at": _t("任务创建日期"),
        "task_code": _t("任务编码"),
    }


# 卡片回调去重：新旧回调可能各打一次
_card_callback_seen: dict[str, float] = {}
_card_callback_lock = threading.Lock()


def _card_callback_should_skip(record_id: str) -> bool:
    """同一 record_id 3 秒内重复回调直接快速成功。"""
    now = time.time()
    with _card_callback_lock:
        # 清理过期
        expired = [k for k, ts in _card_callback_seen.items() if now - ts > 60]
        for k in expired:
            _card_callback_seen.pop(k, None)
        prev = _card_callback_seen.get(record_id)
        if prev is not None and now - prev < 3:
            return True
        _card_callback_seen[record_id] = now
        return False


def _async_mark_store_reminded(
    record_id: str,
    *,
    open_id: str,
    message_id: str,
    card: dict,
) -> None:
    try:
        from tracking_bitable import mark_store_reminded
        from feishu_client import patch_card_message

        mark_store_reminded(record_id, confirmer_open_id=open_id)
        if message_id:
            try:
                patch_card_message(message_id, card)
            except Exception as exc:
                print(f"[feishu-card] async patch failed: {exc}")
    except Exception as exc:
        print(f"[feishu-card] async mark failed: {exc}")


@app.route("/feishu/card", methods=["GET", "POST"])
def feishu_card_callback():
    """HeroClaw 卡片回传：URL 校验 +「已提醒门店」闭环回写。

    兼容两种回调体：
    - 旧版：顶层 open_id / action / token
    - 新版 card.action.trigger：header + event.operator / event.action

    须在 3 秒内响应（否则客户端报 200341）。回写多维表与 PATCH 消息异步执行。
    """
    if request.method == "GET":
        return jsonify({"ok": True, "service": "vip-alert-card-callback"})

    raw = request.get_data()
    try:
        body = _maybe_decrypt_feishu_body(raw)
    except Exception as exc:
        print(f"[feishu-card] parse/decrypt failed: {exc}")
        return jsonify({"error": f"invalid body: {exc}"}), 400

    # URL 校验挑战（明文或解密后）
    if body.get("type") == "url_verification" or (
        body.get("challenge") and not body.get("action") and not body.get("event")
    ):
        challenge = body.get("challenge") or ""
        expected = _feishu_verification_token()
        token = (
            (body.get("token") or "").strip()
            or str((body.get("header") or {}).get("token") or "").strip()
        )
        if expected and token and token != expected:
            return jsonify({"error": "invalid verification token"}), 403
        return jsonify({"challenge": challenge})

    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    header = body.get("header") if isinstance(body.get("header"), dict) else {}

    expected = _feishu_verification_token()
    token = (
        (body.get("token") or "").strip()
        or str(header.get("token") or "").strip()
    )
    # 新版卡片交互的 event.token 是更新卡片凭证，不是 Verification Token
    if expected and token and token != expected:
        if not str(token).startswith("c-"):
            return jsonify({"error": "invalid token"}), 403

    action = body.get("action") if isinstance(body.get("action"), dict) else {}
    if not action and isinstance(event.get("action"), dict):
        action = event.get("action") or {}

    value = action.get("value")
    if value is None:
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {"raw": value}
    if not isinstance(value, dict):
        value = {}

    action_name = str(
        value.get("action")
        or value.get("Action")
        or action.get("name")
        or ""
    ).strip()
    record_id = str(
        value.get("record_id")
        or value.get("recordId")
        or ""
    ).strip()

    if action_name != "store_reminded" or not record_id:
        print(
            "[feishu-card] unknown action "
            f"keys={list(body.keys())} event_keys={list(event.keys())} "
            f"action_keys={list(action.keys())} value={value!r} "
            f"tag={action.get('tag')!r}"
        )
        return jsonify({"toast": {"type": "error", "content": "未知操作"}})

    open_id = (
        body.get("open_id")
        or (body.get("operator") or {}).get("open_id")
        or (event.get("operator") or {}).get("open_id")
        or ""
    )
    open_id = str(open_id).strip()

    ctx = event.get("context") if isinstance(event.get("context"), dict) else {}
    message_id = str(
        ctx.get("open_message_id")
        or body.get("open_message_id")
        or ""
    ).strip()

    from feishu_client import build_vip_alert_card

    confirmed_at = beijing_strftime("%Y-%m-%d %H:%M:%S")
    # 用回调 value 快速组卡，避免同步拉多维表超时（200341）
    payload = {
        "task_code": value.get("task_code") or "",
        "vin": value.get("vin") or "",
        "name": value.get("name") or "",
        "store_name": value.get("store_name") or "",
        "region": value.get("region") or "",
    }
    card = build_vip_alert_card(
        payload,
        role="supervisor",
        tracking_record_id=record_id,
        confirmed=True,
        confirmed_at=confirmed_at,
    )

    skip_dup = _card_callback_should_skip(record_id)
    if not skip_dup:
        threading.Thread(
            target=_async_mark_store_reminded,
            kwargs={
                "record_id": record_id,
                "open_id": open_id,
                "message_id": message_id,
                "card": card,
            },
            daemon=True,
        ).start()

    toast = {"type": "success", "content": "已记录：已提醒门店"}
    if body.get("schema") == "2.0" or header.get("event_type") == "card.action.trigger":
        return jsonify({"toast": toast, "card": {"type": "raw", "data": card}})
    # 旧版：直接返回卡片 JSON
    return jsonify(card)


# ── VIP 任务 ────────────────────────────────────────────────

_task_state = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "scheduler_thread": None,
    "scheduler_stop": False,
    "scheduler_running": False,
}


def _python_bin() -> Path:
    python_bin = PLUGIN_ROOT / ".venv" / "bin" / "python"
    return python_bin if python_bin.exists() else Path(sys.executable)


def _run_in_thread(fn, *args, **kwargs) -> None:
    def _wrapper():
        _task_state["running"] = True
        _task_state["last_error"] = None
        try:
            result = fn(*args, **kwargs)
            _task_state["last_result"] = result
        except Exception as exc:
            _task_state["last_error"] = str(exc)
            _task_state["last_result"] = {"ok": False, "error": str(exc)}
        finally:
            _task_state["running"] = False

    threading.Thread(target=_wrapper, daemon=True).start()


def _vip_status_payload() -> dict:
    sync_state = {}
    sync_path = RUNTIME_DIR / "bitable-sync-state.json"
    if sync_path.exists():
        try:
            sync_state = json.loads(sync_path.read_text(encoding="utf-8"))
        except Exception:
            sync_state = {}
    vip_cache = PLUGIN_ROOT / "data" / "vip_cache.json"
    sent = PLUGIN_ROOT / "data" / "sent_tasks.json"
    vip_count = 0
    sent_count = 0
    if vip_cache.exists():
        try:
            vip_count = int(json.loads(vip_cache.read_text(encoding="utf-8")).get("count") or 0)
        except Exception:
            pass
    if sent.exists():
        try:
            sent_count = len(json.loads(sent.read_text(encoding="utf-8")).get("tasks") or {})
        except Exception:
            pass
    downloads = sorted(
        (PLUGIN_ROOT / "download").glob("*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return {
        "task_running": _task_state["running"],
        "scheduler_running": _task_state["scheduler_running"],
        "last_error": _task_state["last_error"],
        "last_result": _task_state["last_result"],
        "sync_state": sync_state,
        "vip_count": vip_count,
        "sent_count": sent_count,
        "latest_download": downloads[0].name if downloads else "",
    }


@app.route("/api/vip/status")
def api_vip_status():
    return jsonify(_vip_status_payload())


@app.route("/api/vip/sync", methods=["POST"])
def api_vip_sync():
    if _task_state["running"]:
        return jsonify({"ok": False, "error": "已有任务在执行"}), 409

    def _job():
        from bitable_sync import sync_all

        return sync_all()

    _run_in_thread(_job)
    return jsonify({"ok": True, "message": "已开始同步多维表"})


@app.route("/api/vip/pipeline", methods=["POST"])
def api_vip_pipeline():
    if _task_state["running"]:
        return jsonify({"ok": False, "error": "已有任务在执行"}), 409
    payload = request.json or {}
    skip_crawl = bool(payload.get("skip_crawl"))
    dry_run = bool(payload.get("dry_run"))
    import_xlsx = str(payload.get("import_xlsx") or "").strip()
    test_phone = str(payload.get("test_phone") or "").strip()

    def _job():
        from pipeline import run_pipeline

        return run_pipeline(
            skip_crawl=skip_crawl,
            import_xlsx=import_xlsx or None,
            dry_run=dry_run,
            test_phone=test_phone or None,
        )

    _run_in_thread(_job)
    return jsonify({"ok": True, "message": "已开始流水线"})


@app.route("/api/vip/crawl", methods=["POST"])
def api_vip_crawl():
    if _task_state["running"]:
        return jsonify({"ok": False, "error": "已有任务在执行"}), 409

    def _job():
        from crawl_maintenance_reminder import crawl

        return crawl(output_dir=PLUGIN_ROOT / "download")

    _run_in_thread(_job)
    return jsonify({"ok": True, "message": "已开始爬取"})


def _scheduler_loop() -> None:
    from bitable_sync import sync_all
    from pipeline import run_pipeline

    fired = {"00:00": False, "09:00": False}
    while not _task_state["scheduler_stop"]:
        hm = beijing_now().strftime("%H:%M")
        if hm == "00:00" and not fired["00:00"] and not _task_state["running"]:
            fired["00:00"] = True
            try:
                _task_state["running"] = True
                _task_state["last_result"] = sync_all()
            except Exception as exc:
                _task_state["last_error"] = str(exc)
            finally:
                _task_state["running"] = False
        elif hm != "00:00":
            fired["00:00"] = False

        if hm == "09:00" and not fired["09:00"] and not _task_state["running"]:
            fired["09:00"] = True
            try:
                _task_state["running"] = True
                _task_state["last_result"] = run_pipeline()
            except Exception as exc:
                _task_state["last_error"] = str(exc)
            finally:
                _task_state["running"] = False
        elif hm != "09:00":
            fired["09:00"] = False
        time.sleep(30)
    _task_state["scheduler_running"] = False


@app.route("/api/vip/scheduler/start", methods=["POST"])
def api_vip_scheduler_start():
    if _task_state["scheduler_running"]:
        return jsonify({"ok": True, "message": "调度已在运行"})
    _task_state["scheduler_stop"] = False
    _task_state["scheduler_running"] = True
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    _task_state["scheduler_thread"] = t
    t.start()
    return jsonify({"ok": True, "message": "已启动定时等候（00:00 / 09:00）"})


@app.route("/api/vip/scheduler/stop", methods=["POST"])
def api_vip_scheduler_stop():
    _task_state["scheduler_stop"] = True
    _task_state["scheduler_running"] = False
    return jsonify({"ok": True, "message": "已停止定时等候"})


def main() -> int:
    parser = argparse.ArgumentParser(description="VIP 保养提醒 Web 控制台")
    default_port = int(os.getenv("CONSOLE_PORT") or "9002")
    parser.add_argument("--host", default=os.getenv("CONSOLE_HOST") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _start_keepalive_watchdog()
    shared = bool((os.environ.get(SESSION_HOME_ENV) or "").strip())
    print(f"VIP 保养提醒控制台: http://{args.host}:{args.port}")
    print(f"Session home: {SESSION_HOME}" + (" (shared via DFMC_DMS_SESSION_HOME)" if shared else " (plugin local)"))
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
