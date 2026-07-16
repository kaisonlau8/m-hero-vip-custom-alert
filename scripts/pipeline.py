"""VIP 保养提醒一键流水线：同步可选 → 爬取 → 匹配发送。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from bitable_sync import sync_all  # noqa: E402
from crawl_maintenance_reminder import crawl  # noqa: E402
from import_excel import find_latest_download  # noqa: E402
from match_and_notify import run_match_and_notify  # noqa: E402
from time_utils import beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()

MANIFEST_PATH = PLUGIN_ROOT / "data" / "run_manifest.json"


def run_pipeline(
    *,
    skip_crawl: bool = False,
    import_xlsx: str | None = None,
    sync_first: bool = False,
    dry_run: bool = False,
    test_phone: str | None = None,
) -> dict:
    steps: dict = {"started_at": beijing_strftime("%Y-%m-%d %H:%M:%S")}

    if sync_first:
        steps["sync"] = sync_all()

    xlsx_path: Path | None = None
    if import_xlsx:
        xlsx_path = Path(import_xlsx).expanduser().resolve()
        steps["crawl"] = {"status": "skipped_import", "file": str(xlsx_path)}
    elif skip_crawl:
        xlsx_path = find_latest_download(PLUGIN_ROOT / "download")
        if not xlsx_path:
            raise RuntimeError("download/ 下没有可用的 xlsx，请先爬取或指定 --import-xlsx")
        steps["crawl"] = {"status": "skipped_latest", "file": str(xlsx_path)}
    else:
        crawl_result = crawl(output_dir=PLUGIN_ROOT / "download")
        steps["crawl"] = crawl_result
        if crawl_result.get("file"):
            xlsx_path = Path(crawl_result["file"])
        if not xlsx_path or not xlsx_path.exists():
            raise RuntimeError(f"爬取失败: {crawl_result.get('status')}")

    notify = run_match_and_notify(
        xlsx_path, dry_run=dry_run, test_phone=test_phone
    )
    steps["notify"] = notify
    steps["finished_at"] = beijing_strftime("%Y-%m-%d %H:%M:%S")
    steps["ok"] = True

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    return steps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-crawl", action="store_true")
    parser.add_argument("--import-xlsx", default="")
    parser.add_argument("--sync-first", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-phone", default="")
    args = parser.parse_args()
    try:
        run_pipeline(
            skip_crawl=args.skip_crawl,
            import_xlsx=args.import_xlsx or None,
            sync_first=args.sync_first,
            dry_run=args.dry_run,
            test_phone=args.test_phone or None,
        )
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
