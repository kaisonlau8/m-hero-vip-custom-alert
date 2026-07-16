"""定时调度：00:00 同步多维表，09:00 爬取并提醒。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from bitable_sync import sync_all  # noqa: E402
from pipeline import run_pipeline  # noqa: E402
from time_utils import beijing_now, beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()


def run_midnight_bitable_sync() -> dict:
    print(f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] 00:00 同步多维表")
    return sync_all()


def run_morning_pipeline(
    *,
    skip_crawl: bool = False,
    import_xlsx: str | None = None,
    dry_run: bool = False,
    test_phone: str | None = None,
) -> dict:
    print(f"[{beijing_strftime('%Y-%m-%d %H:%M:%S')}] 09:00 流水线")
    return run_pipeline(
        skip_crawl=skip_crawl,
        import_xlsx=import_xlsx,
        sync_first=False,
        dry_run=dry_run,
        test_phone=test_phone,
    )


def run_scheduler_loop() -> None:
    print("定时调度已启动：00:00 同步多维表 · 09:00 爬取匹配发送")
    print("按 Ctrl+C 退出")
    fired = {"00:00": False, "09:00": False}

    while True:
        now = beijing_now()
        hm = now.strftime("%H:%M")

        if hm == "00:00" and not fired["00:00"]:
            try:
                run_midnight_bitable_sync()
            except Exception as exc:
                print(f"[ERROR] midnight sync: {exc}")
            fired["00:00"] = True
        elif hm != "00:00":
            fired["00:00"] = False

        if hm == "09:00" and not fired["09:00"]:
            try:
                run_morning_pipeline()
            except Exception as exc:
                print(f"[ERROR] morning pipeline: {exc}")
            fired["09:00"] = True
        elif hm != "09:00":
            fired["09:00"] = False

        time.sleep(30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--morning", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test-phone", default="")
    parser.add_argument("--skip-crawl", action="store_true")
    parser.add_argument("--import-xlsx", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.sync:
        print(json_dumps(run_midnight_bitable_sync()))
        return 0

    if args.morning or args.test:
        run_morning_pipeline(
            skip_crawl=args.skip_crawl,
            import_xlsx=args.import_xlsx or None,
            dry_run=args.dry_run,
            test_phone=(args.test_phone or None) if args.test else None,
        )
        return 0

    run_scheduler_loop()
    return 0


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
