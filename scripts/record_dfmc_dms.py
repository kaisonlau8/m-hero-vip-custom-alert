#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Error, Page, Playwright, sync_playwright


DEFAULT_TARGET_URL = "https://m-dms.dfmc.com.cn"
DEFAULT_BROWSER_CANDIDATES = {
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
}
SENSITIVE_NAME_PATTERN = re.compile(r"(pass|pwd|token|secret|otp|验证码|密码)", re.IGNORECASE)
DEFAULT_STATE_FILE_NAME = "browser-state.json"

INIT_SCRIPT = r"""
(() => {
  if (window.__dfmcDmsRecorderInstalled) {
    return;
  }
  window.__dfmcDmsRecorderInstalled = true;
  const tokenPrefix = "__dfmc_dms_page__";
  if (!window.name || !window.name.startsWith(tokenPrefix)) {
    const randomPart = Math.random().toString(36).slice(2, 10);
    window.name = `${tokenPrefix}${Date.now()}-${randomPart}`;
  }

  let eventSeq = 0;
  let domChangeSeq = 0;
  let scrollTimer = null;
  let snapshotTimer = null;

  const binding = (...args) => {
    if (typeof window.dfmcRecorderEmit !== "function") {
      return;
    }
    try {
      window.dfmcRecorderEmit(...args);
    } catch (error) {
      console.warn("dfmc recorder emit failed", error);
    }
  };

  const shortText = (value, maxLength = 200) => {
    if (typeof value !== "string") {
      return value;
    }
    if (value.length <= maxLength) {
      return value;
    }
    return value.slice(0, maxLength) + "...[truncated]";
  };

  const cssPath = (element) => {
    if (!(element instanceof Element)) {
      return null;
    }
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      const tag = current.tagName.toLowerCase();
      if (current.id) {
        parts.unshift(`#${CSS.escape(current.id)}`);
        break;
      }
      let selector = tag;
      if (current.classList && current.classList.length > 0) {
        selector += "." + Array.from(current.classList).slice(0, 2).map((token) => CSS.escape(token)).join(".");
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
        if (siblings.length > 1) {
          selector += `:nth-of-type(${siblings.indexOf(current) + 1})`;
        }
      }
      parts.unshift(selector);
      current = current.parentElement;
    }
    return parts.join(" > ");
  };

  const valueSummary = (element) => {
    if (!(element instanceof Element)) {
      return null;
    }
    const tagName = element.tagName.toLowerCase();
    const rawType = element.getAttribute("type") || "";
    const type = rawType.toLowerCase();
    const name = element.getAttribute("name") || "";
    const id = element.getAttribute("id") || "";
    if (type === "password" || /(pass|pwd|token|secret|otp|验证码|密码)/i.test(`${name} ${id}`)) {
      const value = "value" in element ? String(element.value || "") : "";
      return {
        masked: true,
        length: value.length
      };
    }
    if (tagName === "input" || tagName === "textarea" || tagName === "select") {
      return shortText(String(element.value || ""));
    }
    return null;
  };

  const describeElement = (element) => {
    if (!(element instanceof Element)) {
      return null;
    }
    return {
      tagName: element.tagName.toLowerCase(),
      id: element.id || null,
      name: element.getAttribute("name"),
      type: element.getAttribute("type"),
      role: element.getAttribute("role"),
      text: shortText((element.innerText || element.textContent || "").trim(), 120),
      href: element.getAttribute("href"),
      ariaLabel: element.getAttribute("aria-label"),
      placeholder: element.getAttribute("placeholder"),
      selector: cssPath(element),
      value: valueSummary(element)
    };
  };

  const emit = (eventType, detail = {}) => {
    if (location.href === "about:blank") {
      return;
    }
    binding({
      pageToken: window.name,
      eventType,
      detail,
      href: location.href,
      title: document.title,
      ts: new Date().toISOString(),
      readyState: document.readyState,
      sequence: ++eventSeq
    });
  };

  const scheduleSnapshotHint = (reason) => {
    if (snapshotTimer) {
      clearTimeout(snapshotTimer);
    }
    snapshotTimer = setTimeout(() => {
      emit("snapshot-hint", {
        reason,
        domChangeSeq: ++domChangeSeq,
        html: document.documentElement ? document.documentElement.outerHTML : "",
        text: document.body ? document.body.innerText : "",
        scroll: {
          x: window.scrollX,
          y: window.scrollY
        }
      });
    }, 900);
  };

  const trackedEvents = [
    "click",
    "dblclick",
    "contextmenu",
    "input",
    "change",
    "focus",
    "blur",
    "keydown",
    "keyup",
    "submit"
  ];

  trackedEvents.forEach((eventName) => {
    window.addEventListener(eventName, (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const detail = {
        name: eventName,
        element: describeElement(target)
      };
      if (event instanceof KeyboardEvent) {
        detail.key = event.key;
        detail.code = event.code;
      }
      emit("user-action", detail);
      scheduleSnapshotHint(`after-${eventName}`);
    }, true);
  });

  window.addEventListener("scroll", () => {
    if (scrollTimer) {
      clearTimeout(scrollTimer);
    }
    scrollTimer = setTimeout(() => {
      emit("user-action", {
        name: "scroll",
        x: window.scrollX,
        y: window.scrollY
      });
      scheduleSnapshotHint("after-scroll");
    }, 300);
  }, true);

  const wrapHistory = (name) => {
    const original = history[name];
    if (typeof original !== "function") {
      return;
    }
    history[name] = function(...args) {
      const result = original.apply(this, args);
      emit("navigation", {
        name,
        args
      });
      scheduleSnapshotHint(`history-${name}`);
      return result;
    };
  };

  wrapHistory("pushState");
  wrapHistory("replaceState");

  window.addEventListener("hashchange", () => {
    emit("navigation", { name: "hashchange" });
    scheduleSnapshotHint("hashchange");
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    emit("page-event", { name: "domcontentloaded" });
    scheduleSnapshotHint("domcontentloaded");
  }, true);

  window.addEventListener("load", () => {
    emit("page-event", { name: "load" });
    scheduleSnapshotHint("load");
  }, true);

  const observer = new MutationObserver((mutations) => {
    const meaningful = mutations.some((mutation) => {
      if (mutation.type === "childList") {
        return mutation.addedNodes.length > 0 || mutation.removedNodes.length > 0;
      }
      if (mutation.type === "characterData") {
        return true;
      }
      if (mutation.type === "attributes") {
        return mutation.attributeName !== "style";
      }
      return false;
    });
    if (meaningful) {
      scheduleSnapshotHint("mutation");
    }
  });

  const observe = () => {
    if (document.documentElement) {
      observer.observe(document.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true
      });
    }
  };

  observe();
})();
"""

MANUAL_SNAPSHOT_SCRIPT = r"""
(() => {
  if (typeof window.dfmcRecorderEmit !== "function") {
    return false;
  }
  const tokenPrefix = "__dfmc_dms_page__";
  if (!window.name || !window.name.startsWith(tokenPrefix)) {
    const randomPart = Math.random().toString(36).slice(2, 10);
    window.name = `${tokenPrefix}${Date.now()}-${randomPart}`;
  }
  window.dfmcRecorderEmit({
    pageToken: window.name,
    eventType: "snapshot-hint",
    detail: {
      reason: "manual-prime",
      domChangeSeq: 0,
      html: document.documentElement ? document.documentElement.outerHTML : "",
      text: document.body ? document.body.innerText : "",
      scroll: {
        x: window.scrollX,
        y: window.scrollY
      }
    },
    href: location.href,
    title: document.title,
    ts: new Date().toISOString(),
    readyState: document.readyState,
    sequence: 0
  });
  return true;
})();
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "session"


def safe_stem(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "snapshot"


def detect_browser(preferred: str, explicit_path: Optional[str]) -> Path:
    candidates: list[tuple[str, Optional[str]]] = []
    if explicit_path:
        candidates.append(("explicit", explicit_path))
    env_browser = os.environ.get("DFMC_DMS_BROWSER_EXECUTABLE")
    if env_browser:
        candidates.append(("env", env_browser))
    if preferred in DEFAULT_BROWSER_CANDIDATES:
        candidates.append((preferred, DEFAULT_BROWSER_CANDIDATES[preferred]))
    for name, path in DEFAULT_BROWSER_CANDIDATES.items():
        if name != preferred:
            candidates.append((name, path))

    for _, path in candidates:
      if path and Path(path).exists():
        return Path(path)

    options = "\n".join(f"- {path}" for path in DEFAULT_BROWSER_CANDIDATES.values())
    raise FileNotFoundError(
        "No supported browser executable was found.\n"
        "Pass --browser-executable or set DFMC_DMS_BROWSER_EXECUTABLE.\n"
        f"Tried:\n{options}"
    )


@dataclass
class SnapshotArtifact:
    snapshot_id: int
    page_id: int
    reason: str
    timestamp: str
    url: str
    title: str
    html_path: str
    text_path: str
    screenshot_path: Optional[str]
    metadata_path: str


@dataclass
class PageState:
    page_token: str
    page_id: int
    page_dir: Path
    created_at: str
    last_known_url: str = ""
    last_known_title: str = ""
    snapshot_count: int = 0
    last_snapshot_at: float = 0.0
    last_snapshot_reason: str = ""
    snapshots: list[SnapshotArtifact] = field(default_factory=list)


class SessionRecorder:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.pages_root = session_dir / "pages"
        self.events_path = session_dir / "events.jsonl"
        self.summary_path = session_dir / "summary.json"
        self.started_at = iso_timestamp(now_utc())
        self.ended_at: Optional[str] = None
        self.browser_executable = ""
        self.target_url = ""
        self.event_count = 0
        self.snapshot_count = 0
        self.page_count = 0
        self._page_states: dict[int, PageState] = {}
        self._page_tokens: dict[str, int] = {}
        self._attached_page_keys: set[int] = set()
        self._next_page_id = 1
        self._next_snapshot_id = 1
        self._lock = threading.RLock()
        self.pages_root.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def set_runtime_info(self, browser_executable: Path, target_url: str) -> None:
        self.browser_executable = str(browser_executable)
        self.target_url = target_url

    def register_page_token(
        self,
        page_token: str,
        known_url: str = "",
        known_title: str = "",
    ) -> PageState:
        with self._lock:
            page_id = self._page_tokens.get(page_token)
            if page_id is not None:
                state = self._page_states[page_id]
                if known_url:
                    state.last_known_url = known_url
                if known_title:
                    state.last_known_title = known_title
                return state

            page_id = self._next_page_id
            self._next_page_id += 1
            page_dir = self.pages_root / f"page-{page_id:03d}"
            (page_dir / "snapshots").mkdir(parents=True, exist_ok=True)

            state = PageState(
                page_token=page_token,
                page_id=page_id,
                page_dir=page_dir,
                created_at=iso_timestamp(now_utc()),
                last_known_url=known_url,
                last_known_title=known_title,
            )
            self._page_tokens[page_token] = page_id
            self._page_states[page_id] = state
            self.page_count = len(self._page_states)

        self.write_event(
            {
                "kind": "page-created",
                "pageId": state.page_id,
                "pageToken": state.page_token,
                "timestamp": iso_timestamp(now_utc()),
                "url": known_url,
                "title": known_title,
            }
        )
        return state

    def safe_page_url(self, page: Page) -> str:
        try:
            return page.url
        except Error:
            return ""

    def safe_page_title(self, page: Page) -> str:
        try:
            return page.title()
        except Error:
            return ""

    def write_event(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.event_count += 1

    def record_browser_event(self, kind: str, detail: dict[str, Any]) -> None:
        event = {
            "kind": kind,
            "timestamp": iso_timestamp(now_utc()),
        }
        event.update(detail)
        self.write_event(event)

    def mark_page_attached(self, page: Page) -> bool:
        page_key = id(page)
        with self._lock:
            if page_key in self._attached_page_keys:
                return False
            self._attached_page_keys.add(page_key)
            return True

    def snapshot_from_payload(self, state: PageState, payload: dict[str, Any]) -> None:
        detail = dict(payload.get("detail") or {})
        reason = detail.get("reason") or payload.get("eventType") or "snapshot"
        html = detail.pop("html", "") or ""
        text = detail.pop("text", "") or ""
        url = payload.get("href") or state.last_known_url
        title = payload.get("title") or state.last_known_title
        if url:
            state.last_known_url = url
        if title:
            state.last_known_title = title

        now_monotonic = time.monotonic()
        with self._lock:
            if state.last_snapshot_reason == reason and now_monotonic - state.last_snapshot_at < 0.75:
                return
            state.last_snapshot_reason = reason
            state.last_snapshot_at = now_monotonic
            snapshot_id = self._next_snapshot_id
            self._next_snapshot_id += 1
            state.snapshot_count += 1

        timestamp = payload.get("ts") or iso_timestamp(now_utc())
        stem = f"{state.snapshot_count:04d}-{safe_stem(reason)}"
        snapshot_dir = state.page_dir / "snapshots"
        html_path = snapshot_dir / f"{stem}.html"
        text_path = snapshot_dir / f"{stem}.txt"
        metadata_path = snapshot_dir / f"{stem}.json"

        html_path.write_text(html, encoding="utf-8")
        text_path.write_text(text, encoding="utf-8")

        metadata = {
            "snapshotId": snapshot_id,
            "pageId": state.page_id,
            "pageToken": state.page_token,
            "timestamp": timestamp,
            "reason": reason,
            "detail": detail,
            "url": url,
            "title": title,
            "readyState": payload.get("readyState"),
            "htmlFile": html_path.name,
            "textFile": text_path.name,
            "screenshotFile": None,
            "source": "binding-payload",
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        artifact = SnapshotArtifact(
            snapshot_id=snapshot_id,
            page_id=state.page_id,
            reason=reason,
            timestamp=timestamp,
            url=metadata["url"],
            title=metadata["title"],
            html_path=str(html_path.relative_to(self.session_dir)),
            text_path=str(text_path.relative_to(self.session_dir)),
            screenshot_path=None,
            metadata_path=str(metadata_path.relative_to(self.session_dir)),
        )
        with self._lock:
            state.snapshots.append(artifact)
            self.snapshot_count += 1

        self.write_event(
            {
                "kind": "snapshot",
                "timestamp": timestamp,
                "pageId": state.page_id,
                "pageToken": state.page_token,
                "snapshotId": snapshot_id,
                "reason": reason,
                "detail": detail,
                "url": metadata["url"],
                "title": metadata["title"],
                "artifacts": {
                    "html": artifact.html_path,
                    "text": artifact.text_path,
                    "screenshot": None,
                    "metadata": artifact.metadata_path,
                },
            }
        )

    def record_js_event(self, payload: dict[str, Any]) -> None:
        try:
            page_token = payload.get("pageToken") or ""
            if not page_token:
                self.write_event(
                    {
                        "kind": "malformed-browser-event",
                        "timestamp": iso_timestamp(now_utc()),
                        "payload": payload,
                    }
                )
                return

            payload_url = payload.get("href") or ""
            payload_title = payload.get("title") or ""
            state = self.register_page_token(page_token, known_url=payload_url, known_title=payload_title)
            if payload_url:
                state.last_known_url = payload_url
            if payload_title:
                state.last_known_title = payload_title
            detail = payload.get("detail") or {}
            logged_detail = dict(detail)
            if payload.get("eventType") == "snapshot-hint":
                logged_detail.pop("html", None)
                logged_detail.pop("text", None)
            event = {
                "kind": "browser-event",
                "timestamp": payload.get("ts") or iso_timestamp(now_utc()),
                "pageId": state.page_id,
                "pageToken": state.page_token,
                "eventType": payload.get("eventType"),
                "url": payload_url,
                "title": payload_title,
                "readyState": payload.get("readyState"),
                "sequence": payload.get("sequence"),
                "detail": logged_detail,
            }
            self.write_event(event)

            event_type = payload.get("eventType")
            if event_type == "snapshot-hint":
                self.snapshot_from_payload(state, payload)
        except Exception as exc:
            self.write_event(
                {
                    "kind": "recorder-error",
                    "timestamp": iso_timestamp(now_utc()),
                    "error": str(exc),
                    "payloadSummary": {
                        "eventType": payload.get("eventType"),
                        "href": payload.get("href"),
                        "title": payload.get("title"),
                        "pageToken": payload.get("pageToken"),
                    },
                }
            )

    def finalize(self) -> None:
        self.ended_at = iso_timestamp(now_utc())
        summary = {
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "browserExecutable": self.browser_executable,
            "targetUrl": self.target_url,
            "eventCount": self.event_count,
            "pageCount": self.page_count,
            "snapshotCount": self.snapshot_count,
            "eventsFile": str(self.events_path.relative_to(self.session_dir)),
            "pages": [
                {
                    "pageId": state.page_id,
                    "pageToken": state.page_token,
                    "createdAt": state.created_at,
                    "lastKnownUrl": state.last_known_url,
                    "lastKnownTitle": state.last_known_title,
                    "snapshotCount": len(state.snapshots),
                    "snapshotFiles": [
                        {
                            "snapshotId": artifact.snapshot_id,
                            "reason": artifact.reason,
                            "timestamp": artifact.timestamp,
                            "html": artifact.html_path,
                            "text": artifact.text_path,
                            "screenshot": artifact.screenshot_path,
                            "metadata": artifact.metadata_path,
                        }
                        for artifact in state.snapshots
                    ],
                }
                for state in sorted(self._page_states.values(), key=lambda item: item.page_id)
            ],
        }
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def attach_page_listeners(recorder: SessionRecorder, page: Page) -> None:
    if not recorder.mark_page_attached(page):
        return

    def on_domcontentloaded() -> None:
        recorder.record_browser_event(
            "page-domcontentloaded",
            {
                "url": page.url,
            },
        )

    def on_load() -> None:
        recorder.record_browser_event(
            "page-load",
            {
                "url": page.url,
            },
        )

    def on_close() -> None:
        recorder.record_browser_event(
            "page-closed",
            {
                "url": page.url,
            },
        )

    def on_frame_navigated(frame: Any) -> None:
        try:
            if frame != page.main_frame:
                return
        except Error:
            return
        recorder.record_browser_event(
            "frame-navigated",
            {
                "url": frame.url,
            },
        )

    page.on("domcontentloaded", lambda: on_domcontentloaded())
    page.on("load", lambda: on_load())
    page.on("close", lambda: on_close())
    page.on("framenavigated", lambda frame: on_frame_navigated(frame))


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch a browser for DFMC DMS and record all user actions plus page snapshots."
    )
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL)
    parser.add_argument("--session-name", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--browser-profile-dir", default="")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--attach-port", type=int, default=0)
    parser.add_argument("--attach-existing", action="store_true")
    parser.add_argument("--skip-open", action="store_true")
    parser.add_argument(
        "--stop-file",
        default="",
        help="If set, creating this file stops the recorder (for Web console / background).",
    )
    parser.add_argument("--browser", choices=sorted(DEFAULT_BROWSER_CANDIDATES.keys()), default="chrome")
    parser.add_argument("--browser-executable", default="")
    return parser


def build_session_dir(root: Path, session_name: str) -> Path:
    timestamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    suffix = slugify(session_name) if session_name else "session"
    session_dir = root / "recordings" / f"{timestamp}-{suffix}"
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir


def get_runtime_dir(plugin_root: Path) -> Path:
    try:
        from dfmc_browser_utils import get_runtime_dir as shared_runtime_dir
        return shared_runtime_dir(plugin_root)
    except Exception:
        runtime_dir = plugin_root / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir


def get_default_state_file(plugin_root: Path) -> Path:
    try:
        from dfmc_browser_utils import get_default_state_file as shared_state_file
        return shared_state_file(plugin_root)
    except Exception:
        return get_runtime_dir(plugin_root) / DEFAULT_STATE_FILE_NAME


def get_default_browser_profile_dir(plugin_root: Path) -> Path:
    try:
        from dfmc_browser_utils import get_browser_profile_dir
        return get_browser_profile_dir(plugin_root)
    except Exception:
        profile = plugin_root / ".browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        return profile


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def launch_browser_process(browser_executable: Path, user_data_dir: Path, port: int) -> subprocess.Popen[Any]:
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser_executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--new-window",
        "--no-first-run",
        "--disable-popup-blocking",
        "--window-size=1440,960",
        "about:blank",
    ]
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def write_browser_state(state_file: Path, payload: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_browser_state(state_file: Path) -> dict[str, Any]:
    return json.loads(state_file.read_text(encoding="utf-8"))


def close_context_safely(context: Optional[BrowserContext]) -> None:
    if context is None:
        return
    try:
        context.close()
    except Error:
        pass


def close_browser_safely(browser: Optional[Browser]) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except Error:
        pass


def close_process_safely(process: Optional[subprocess.Popen[Any]]) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def connect_browser_over_cdp(playwright: Playwright, port: int, timeout_seconds: float = 15.0) -> Browser:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Failed to connect to Chrome over CDP on port {port}: {last_error}")


def inject_recorder_into_page(page: Page) -> None:
    try:
        page.evaluate(INIT_SCRIPT)
    except Error:
        return
    try:
        page.evaluate(MANUAL_SNAPSHOT_SCRIPT)
    except Error:
        return


def main() -> int:
    parser = create_argument_parser()
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parent.parent
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else plugin_root
    session_dir = build_session_dir(output_root, args.session_name)
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else get_default_state_file(plugin_root)
    )
    user_data_dir = (
        Path(args.browser_profile_dir).expanduser().resolve()
        if args.browser_profile_dir
        else get_default_browser_profile_dir(plugin_root)
    )
    stop_file = Path(args.stop_file).expanduser().resolve() if args.stop_file else None
    if stop_file and stop_file.exists():
        stop_file.unlink(missing_ok=True)

    browser_executable = detect_browser(args.browser, args.browser_executable or None)
    recorder = SessionRecorder(session_dir)
    recorder.set_runtime_info(browser_executable, args.target_url)

    print(f"Session directory: {session_dir}")
    print(f"Browser executable: {browser_executable}")
    print(f"Opening: {args.target_url}")
    if stop_file:
        print(f"Stop file: {stop_file} (create this file or send SIGTERM to stop)")
    else:
        print("Close the browser window or press Ctrl+C to stop recording.")

    should_stop = threading.Event()

    browser_holder: dict[str, Optional[Browser]] = {"browser": None}

    def request_stop(signum: int, _: Any) -> None:
        print(f"Received signal {signum}, closing recorder...")
        should_stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    browser_process: Optional[subprocess.Popen[Any]] = None

    try:
        with sync_playwright() as playwright:
            attach_mode = args.attach_existing or args.attach_port > 0
            if attach_mode:
                if args.attach_port > 0:
                    cdp_port = args.attach_port
                elif state_file.exists():
                    state_payload = read_browser_state(state_file)
                    cdp_port = int(state_payload["port"])
                else:
                    raise FileNotFoundError(
                        f"No running browser state found at {state_file}. Start the login browser first."
                    )
            else:
                cdp_port = find_free_port()
                browser_process = launch_browser_process(browser_executable, user_data_dir, cdp_port)
                write_browser_state(
                    state_file,
                    {
                        "port": cdp_port,
                        "pid": browser_process.pid if browser_process else None,
                        "browserExecutable": str(browser_executable),
                        "browserProfileDir": str(user_data_dir),
                        "targetUrl": args.target_url,
                        "startedAt": iso_timestamp(now_utc()),
                    },
                )
            browser = connect_browser_over_cdp(playwright, cdp_port)
            browser_holder["browser"] = browser
            context = browser.contexts[0]
            context.set_default_timeout(5_000)
            context.expose_function("dfmcRecorderEmit", recorder.record_js_event)
            context.add_init_script(INIT_SCRIPT)
            context.on("page", lambda page: attach_page_listeners(recorder, page))

            for existing_page in context.pages:
                attach_page_listeners(recorder, existing_page)
                inject_recorder_into_page(existing_page)

            if attach_mode:
                recorder.record_browser_event(
                    "attached-to-existing-browser",
                    {
                        "port": cdp_port,
                    },
                )
            elif not args.skip_open:
                first_page = context.new_page()
                attach_page_listeners(recorder, first_page)
                try:
                    first_page.goto(args.target_url, wait_until="commit", timeout=15_000)
                except Error as exc:
                    recorder.record_browser_event(
                        "goto-error",
                        {
                            "url": args.target_url,
                            "error": str(exc),
                        },
                    )
                first_page.wait_for_timeout(2_000)
                recorder.record_browser_event(
                    "initial-navigation-issued",
                    {
                        "url": args.target_url,
                    },
                )
                inject_recorder_into_page(first_page)

            while not should_stop.is_set():
                try:
                    if stop_file and stop_file.exists():
                        print("Stop file detected, closing recorder...")
                        break
                    if browser is None or not browser.is_connected():
                        break
                    if len(context.pages) == 0:
                        break
                    context.pages[0].wait_for_timeout(500)
                except Error:
                    break
    except KeyboardInterrupt:
        print("Recorder interrupted by user.")
    finally:
        if stop_file and stop_file.exists():
            stop_file.unlink(missing_ok=True)
        if not (args.attach_existing or args.attach_port > 0):
            close_context_safely(context)
            close_browser_safely(browser)
            close_process_safely(browser_process)
        recorder.finalize()
        print(f"Recording finished. Summary: {recorder.summary_path}")
        print(f"Events: {recorder.events_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
