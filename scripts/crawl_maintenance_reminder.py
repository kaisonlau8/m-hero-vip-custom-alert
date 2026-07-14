#!/usr/bin/env python3
"""爬取 DMS 保养提醒任务列表并保存到 download/。

基线路程（可按 recordings/ 录制结果再微调选择器）：
  1. 附着共享浏览器 CDP
  2. 导航到 #/aftermarketMange/customerManagement/maintenanceReminderTask
  3. 点击查询（如有）→ 导出
  4. 轮询 download/ 新文件并重命名
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Error, Page, sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dfmc_browser_utils import (  # noqa: E402
    DEFAULT_TARGET_URL,
    acquire_export_lock,
    connect_browser_over_cdp,
    ensure_cdp_browser_running,
    find_dms_page,
    get_default_state_file,
    get_export_lock_path,
    get_session_home,
    release_export_lock,
)
from time_utils import beijing_strftime, beijing_today  # noqa: E402

REMINDER_ROUTE = "/aftermarketMange/customerManagement/maintenanceReminderTask"
DMS_HOST = "m-dms.dfmc.com.cn"
CRAWLER_OWNER = "vip_maintenance_reminder"


def validate_logged_in(page: Page) -> None:
    url = page.url
    if DMS_HOST not in url:
        raise RuntimeError(f"Browser not on DMS site. Current URL: {url}")
    if "/login" in url.lower():
        raise RuntimeError("Browser is on the login page. Log in first.")
    if page.locator("input[type='password']").count() > 0:
        raise RuntimeError("Login page detected. Log in first.")


def navigate_to_reminder_page(page: Page) -> None:
    try:
        page.evaluate(f"window.location.hash = '{REMINDER_ROUTE}'")
    except Error:
        current_url = page.url
        if "?code=" in current_url:
            code = current_url.split("?code=")[1].split("#")[0].split("&")[0]
            target = f"https://{DMS_HOST}/?code={code}#{REMINDER_ROUTE}"
            page.goto(target, wait_until="domcontentloaded", timeout=15_000)
        else:
            page.goto(
                f"https://{DMS_HOST}#{REMINDER_ROUTE}",
                wait_until="domcontentloaded",
                timeout=15_000,
            )

    try:
        page.wait_for_selector("section.mixButton, .el-table, .u-btn-left", timeout=20_000)
    except Error:
        page.wait_for_timeout(2_000)
    page.wait_for_timeout(800)


def click_query(page: Page) -> None:
    clicked = page.evaluate(
        """() => {
          const btns = document.querySelectorAll(
            'section.mixButton button, .u-btn-left button, .el-button'
          );
          for (const btn of btns) {
            const text = (btn.innerText || '').replace(/\\s+/g, '');
            if (!text) continue;
            if (text.includes('重置') || text.toLowerCase().includes('reset')) continue;
            if (text.includes('查询') || text.toLowerCase().includes('query')) {
              btn.click();
              return text;
            }
          }
          const primary = document.querySelector(
            'section.mixButton .right-btn button.el-button--primary, button.el-button--primary'
          );
          if (primary) {
            const t = (primary.innerText || '').replace(/\\s+/g, '');
            if (t.includes('导出') || t.toLowerCase().includes('export')) return '';
            primary.click();
            return t || 'primary';
          }
          return '';
        }"""
    )
    if clicked:
        print(f"  Clicked query ({clicked!r})")
        try:
            page.wait_for_selector(".el-table__body-wrapper tbody tr", timeout=15_000)
        except Error:
            print("  [WARN] table rows not visible within 15s")
        page.wait_for_timeout(800)
    else:
        print("  [INFO] 未找到查询按钮，直接尝试导出")


def click_export_and_capture(page: Page, cdp_session: Any, output_dir: Path) -> Optional[Path]:
    stamp = beijing_strftime("%Y%m%d_%H%M%S")
    save_path = output_dir / f"保养提醒任务列表_{stamp}.xlsx"
    before_files = {f.name: f.stat().st_mtime for f in output_dir.iterdir() if f.is_file()}

    try:
        cdp_session.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(output_dir), "eventsEnabled": True},
        )
    except Exception as exc:
        print(f"  [WARN] setDownloadBehavior failed: {exc}")

    clicked = page.evaluate(
        """() => {
          const btns = document.querySelectorAll(
            'section.mixButton button, .u-btn-left button, .el-button, button'
          );
          for (const btn of btns) {
            const text = (btn.innerText || '').trim();
            if (text.includes('导出') || text.toLowerCase().includes('export')) {
              btn.click();
              return text;
            }
          }
          return '';
        }"""
    )
    if not clicked:
        raise RuntimeError("导出按钮未找到（可用控制台录制器复盘页面后调整选择器）")
    print(f"  Clicked export ({clicked!r}), waiting for download...")

    deadline = time.monotonic() + 180
    new_file: Optional[Path] = None
    stable_size: Optional[int] = None
    stable_since = 0.0

    while time.monotonic() < deadline:
        time.sleep(0.5)
        for f in output_dir.iterdir():
            if not f.is_file() or f.name.startswith("crawl_manifest") or f.name.startswith("."):
                continue
            if f.suffix.lower() not in {".xlsx", ".xls", ".csv", ".crdownload"}:
                continue
            if f.name in before_files:
                try:
                    if f.stat().st_mtime <= before_files[f.name]:
                        continue
                except OSError:
                    continue
            if f.suffix.lower() == ".crdownload":
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            if new_file is None or f != new_file:
                new_file = f
                stable_size = size
                stable_since = time.monotonic()
                continue
            if size == stable_size and time.monotonic() - stable_since >= 2.0:
                break
        else:
            continue
        break

    if new_file is None:
        print("  No new file detected within 180s")
        return None

    try:
        if new_file != save_path:
            new_file.rename(save_path)
        print(f"  Saved: {save_path}")
        return save_path
    except OSError as exc:
        print(f"  Rename failed ({exc}), using: {new_file}")
        return new_file


def crawl(*, output_dir: Path | None = None, dry_run: bool = False) -> dict:
    plugin_root = PLUGIN_ROOT
    state_file = get_default_state_file(plugin_root)
    out_dir = output_dir or (plugin_root / "download")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Session home: {get_session_home(plugin_root)}")
    cdp_port = ensure_cdp_browser_running(state_file, plugin_root=plugin_root)
    print(f"Browser CDP port: {cdp_port}")

    filepath: Optional[Path] = None
    status = "unknown"

    try:
        with sync_playwright() as pw:
            browser = connect_browser_over_cdp(pw, cdp_port)
            context = browser.contexts[0]
            context.set_default_timeout(10_000)

            page = find_dms_page(context)
            if page is None:
                page = context.new_page()
                page.goto(DEFAULT_TARGET_URL, wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2_000)

            validate_logged_in(page)
            print("Navigating to maintenance reminder task page...")
            navigate_to_reminder_page(page)
            print("Page loaded.")

            if dry_run:
                status = "dry_run"
            else:
                cdp_session = context.new_cdp_session(page)
                lock_file = acquire_export_lock(plugin_root, CRAWLER_OWNER)
                try:
                    click_query(page)
                    filepath = click_export_and_capture(page, cdp_session, out_dir)
                    status = "ok" if filepath else "no_file"
                    if not filepath:
                        print("  Retrying export once...")
                        page.wait_for_timeout(2_000)
                        filepath = click_export_and_capture(page, cdp_session, out_dir)
                        status = "retried_ok" if filepath else "retry_failed"
                finally:
                    release_export_lock(lock_file, owner=CRAWLER_OWNER)
    except Exception as exc:
        print(f"Fatal error: {exc}")
        status = "fatal_error"
        try:
            release_export_lock(get_export_lock_path(plugin_root), owner=CRAWLER_OWNER)
        except Exception:
            pass

    manifest = {
        "crawledAt": beijing_strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "file": str(filepath) if filepath else "",
        "route": REMINDER_ROUTE,
    }
    manifest_path = out_dir / "crawl_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_path}")
    print(f"Status: {status}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl DMS maintenance reminder tasks")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    result = crawl(output_dir=output_dir, dry_run=args.dry_run)
    return 0 if result.get("status") in {"ok", "retried_ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
