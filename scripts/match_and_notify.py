"""VIP VIN 匹配 + 按区域/级别路由提醒人 + 任务编码去重。"""

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


def select_recipients_for_alert(
    recipients: list[dict],
    *,
    region: str,
    vip_level: str,
) -> list[dict]:
    """区域 ∩ 提醒级别（精确匹配 VIP级别 字符串）。"""
    region = (region or "").strip()
    vip_level = (vip_level or "").strip()
    selected: list[dict] = []
    seen: set[str] = set()
    for r in recipients:
        open_id = (r.get("open_id") or "").strip()
        if not open_id or open_id in seen:
            continue
        regions = r.get("regions") or []
        levels = r.get("levels") or []
        if region and region not in regions:
            continue
        if vip_level and vip_level not in levels:
            continue
        # 缺区域或级别时不广播，避免误发
        if not region or not vip_level:
            continue
        seen.add(open_id)
        selected.append(r)
    return selected


def notify_matches(
    matches: list[dict],
    recipients: list[dict],
    *,
    dry_run: bool = False,
    test_phone: str | None = None,
) -> dict:
    sent_store = load_sent_tasks()
    already = sent_store.setdefault("tasks", {})

    result = {
        "matched": len(matches),
        "skipped_sent": 0,
        "skipped_no_recipient": 0,
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

    if not recipients and not test_phone:
        raise RuntimeError("无提醒人，请先同步多维表格「VIP 超级提醒」")

    test_targets: list[dict] = []
    if test_phone:
        oid = resolve_phone_to_open_id(test_phone) if not dry_run else f"dry_run:{test_phone}"
        if not oid and not dry_run:
            raise RuntimeError(f"测试手机号无法解析 open_id: {test_phone}")
        test_targets = [{"name": "TEST", "open_id": oid or f"dry_run:{test_phone}"}]

    # 先按路由筛一遍，统计 to_send
    routed: list[tuple[dict, list[dict]]] = []
    for item in pending:
        if test_targets:
            targets = test_targets
        else:
            targets = select_recipients_for_alert(
                recipients,
                region=item.get("region") or "",
                vip_level=item.get("vip_level") or "",
            )
        if not targets:
            result["skipped_no_recipient"] += 1
            result["details"].append(
                {
                    "task_code": item["task_code"],
                    "vin": item["vin"],
                    "name": item.get("name"),
                    "region": item.get("region"),
                    "vip_level": item.get("vip_level"),
                    "status": "skipped_no_recipient",
                    "recipients": [],
                }
            )
            print(
                f"[skip] {item['task_code']} 无匹配提醒人 "
                f"区域={item.get('region')} 级别={item.get('vip_level')}"
            )
            continue
        routed.append((item, targets))

    result["to_send"] = len(routed)

    for item, targets in routed:
        card = build_vip_alert_card(item)
        recipient_names = [t.get("name") or t.get("open_id") for t in targets]
        detail = {
            "task_code": item["task_code"],
            "vin": item["vin"],
            "name": item.get("name"),
            "region": item.get("region"),
            "vip_level": item.get("vip_level"),
            "status": "pending",
            "recipients": recipient_names,
        }

        if dry_run:
            detail["status"] = "dry_run"
            result["details"].append(detail)
            print(
                f"[dry-run] {item['task_code']} VIN={item['vin']} "
                f"{item.get('name')} {item.get('region')}/{item.get('vip_level')} "
                f"→ {', '.join(recipient_names)}"
            )
            continue

        ok_any = False
        for t in targets:
            oid = t.get("open_id") or ""
            rname = t.get("name") or oid
            resp = send_card_message(oid, card)
            if resp:
                ok_any = True
                print(f"[sent] {item['task_code']} → {rname}")
            else:
                print(f"[fail] {item['task_code']} → {rname}")
            time.sleep(0.35)

        if ok_any:
            already[item["task_code"]] = {
                "vin": item["vin"],
                "sent_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
                "name": item.get("name", ""),
                "region": item.get("region", ""),
                "vip_level": item.get("vip_level", ""),
                "recipients": recipient_names,
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
        "recipient_count": len(recipients),
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
