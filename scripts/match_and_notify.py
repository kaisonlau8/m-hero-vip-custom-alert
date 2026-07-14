"""VIP VIN 匹配 + 按任务编码去重 + 飞书通知。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from bitable_sync import load_recipients_list, load_vip_cache  # noqa: E402
from feishu_client import (  # noqa: E402
    build_vip_alert_card,
    resolve_phone_to_open_id,
    send_card_message,
)
from import_excel import import_maintenance_reminder_xlsx  # noqa: E402
from time_utils import beijing_strftime  # noqa: E402

SENT_TASKS_PATH = PLUGIN_ROOT / "data" / "sent_tasks.json"


def load_sent_tasks() -> dict[str, Any]:
    if not SENT_TASKS_PATH.exists():
        return {"tasks": {}}
    with open(SENT_TASKS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "tasks" not in data:
        data = {"tasks": data if isinstance(data, dict) else {}}
    return data


def save_sent_tasks(data: dict[str, Any]) -> None:
    SENT_TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def match_vip_tasks(tasks: list[dict], vip_cache: dict[str, dict]) -> list[dict]:
    matched: list[dict] = []
    for task in tasks:
        vip = vip_cache.get(task["vin"])
        if not vip:
            continue
        matched.append(
            {
                **task,
                "name": vip.get("name", ""),
                "customer_category": vip.get("customer_category", ""),
                "vip_level": vip.get("vip_level", ""),
                "vip_attrs": vip.get("vip_attrs", ""),
                "series": vip.get("series", ""),
            }
        )
    return matched


def notify_matches(
    matches: list[dict],
    recipients: list[dict],
    *,
    dry_run: bool = False,
    test_phone: str | None = None,
) -> dict:
    sent_store = load_sent_tasks()
    already = sent_store.setdefault("tasks", {})

    if test_phone:
        recipients = [{"name": "TEST", "phone": test_phone}]

    result = {
        "matched": len(matches),
        "skipped_sent": 0,
        "to_send": 0,
        "sent": 0,
        "failed": 0,
        "dry_run": dry_run,
        "details": [],
    }

    pending = []
    for item in matches:
        code = item["task_code"]
        if code in already:
            result["skipped_sent"] += 1
            continue
        pending.append(item)

    result["to_send"] = len(pending)
    if not recipients:
        raise RuntimeError("无提醒人，请先同步多维表格「VIP 超级提醒」")

    open_ids: list[tuple[str, str, str]] = []
    for r in recipients:
        phone = r.get("phone") or ""
        oid = resolve_phone_to_open_id(phone) if not dry_run else f"dry_run:{phone}"
        if not oid:
            print(f"[WARN] 无法解析提醒人 {r.get('name')} / {phone}")
            continue
        open_ids.append((r.get("name") or "", phone, oid))

    if not open_ids and not dry_run:
        raise RuntimeError("提醒人手机号均未解析到飞书 open_id")

    for item in pending:
        card = build_vip_alert_card(item)
        detail = {
            "task_code": item["task_code"],
            "vin": item["vin"],
            "name": item.get("name"),
            "status": "pending",
        }

        if dry_run:
            detail["status"] = "dry_run"
            result["details"].append(detail)
            print(
                f"[dry-run] {item['task_code']} VIN={item['vin']} "
                f"{item.get('name')} {item.get('vip_level')}"
            )
            continue

        ok_any = False
        for rname, phone, oid in open_ids:
            resp = send_card_message(oid, card)
            if resp:
                ok_any = True
                print(f"[sent] {item['task_code']} → {rname or phone}")
            else:
                print(f"[fail] {item['task_code']} → {rname or phone}")
            time.sleep(0.35)

        if ok_any:
            already[item["task_code"]] = {
                "vin": item["vin"],
                "sent_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
                "name": item.get("name", ""),
            }
            result["sent"] += 1
            detail["status"] = "sent"
        else:
            result["failed"] += 1
            detail["status"] = "failed"
        result["details"].append(detail)

    if not dry_run and result["sent"]:
        sent_store["updated_at"] = beijing_strftime("%Y-%m-%d %H:%M:%S")
        save_sent_tasks(sent_store)

    return result


def run_match_and_notify(
    xlsx_path: str | Path,
    *,
    dry_run: bool = False,
    test_phone: str | None = None,
) -> dict:
    vip_cache = load_vip_cache()
    if not vip_cache:
        raise RuntimeError("VIP 缓存为空，请先运行 bitable_sync.py")

    tasks = import_maintenance_reminder_xlsx(xlsx_path)
    matches = match_vip_tasks(tasks, vip_cache)
    recipients = load_recipients_list()
    notify_result = notify_matches(
        matches, recipients, dry_run=dry_run, test_phone=test_phone
    )
    return {
        "xlsx": str(xlsx_path),
        "task_count": len(tasks),
        "vip_cache_count": len(vip_cache),
        **notify_result,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-phone", default="")
    args = parser.parse_args()
    out = run_match_and_notify(
        args.xlsx, dry_run=args.dry_run, test_phone=args.test_phone or None
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
