"""VIP VIN 匹配 + 按区域/级别路由提醒人 + 角色分卡 + 任务编码去重。"""

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
from time_utils import beijing_strftime, ensure_beijing_tz  # noqa: E402
from tracking_bitable import (  # noqa: E402
    STATUS_DONE,
    is_supervisor_role,
    set_alert_triggered,
    upsert_tracking_from_task,
)

ensure_beijing_tz()

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


def _normalize_role(recipient: dict) -> str:
    role = (recipient.get("role") or "").strip()
    if role == "supervisor":
        return "supervisor"
    return "admin"


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
        "tracking_upserted": 0,
        "to_send": 0,
        "sent": 0,
        "failed": 0,
        "dry_run": dry_run,
        "details": [],
    }

    if not recipients and not test_phone:
        raise RuntimeError("无提醒人，请先同步多维表格「VIP 超级提醒」")

    test_targets: list[dict] = []
    if test_phone:
        oid = resolve_phone_to_open_id(test_phone) if not dry_run else f"dry_run:{test_phone}"
        if not oid and not dry_run:
            raise RuntimeError(f"测试手机号无法解析 open_id: {test_phone}")
        # 测试默认按督导，便于验证按钮与闭环
        test_targets = [
            {
                "name": "TEST",
                "open_id": oid or f"dry_run:{test_phone}",
                "role": "supervisor",
            }
        ]

    for item in matches:
        code = item["task_code"]
        already_sent = code in already

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
                    "task_code": code,
                    "vin": item["vin"],
                    "name": item.get("name"),
                    "region": item.get("region"),
                    "vip_level": item.get("vip_level"),
                    "status": "skipped_no_recipient",
                    "recipients": [],
                }
            )
            print(
                f"[skip] {code} 无匹配提醒人 "
                f"区域={item.get('region')} 级别={item.get('vip_level')}"
            )
            continue

        if already_sent:
            result["skipped_sent"] += 1

        detail = {
            "task_code": code,
            "vin": item["vin"],
            "name": item.get("name"),
            "region": item.get("region"),
            "vip_level": item.get("vip_level"),
            "status": "pending",
            "recipients": [],
            "tracking": [],
        }

        ok_any = False
        need_send = not already_sent
        send_attempts = 0

        for t in targets:
            role = _normalize_role(t)
            rname = t.get("name") or t.get("open_id") or ""
            oid = t.get("open_id") or ""
            tracking_record_id = ""
            tracking_status = ""

            if is_supervisor_role(role):
                try:
                    tr = upsert_tracking_from_task(item, t, dry_run=dry_run)
                    tracking_record_id = tr.get("record_id") or ""
                    tracking_status = tr.get("status") or ""
                    result["tracking_upserted"] += 1
                    detail["tracking"].append(
                        {
                            "supervisor": rname,
                            "record_id": tracking_record_id,
                            "created": tr.get("created"),
                            "status": tracking_status,
                        }
                    )
                    print(
                        f"[tracking] {code} → {rname} "
                        f"{'新建' if tr.get('created') else '补全'} "
                        f"status={tracking_status or '-'} id={tracking_record_id or '-'}"
                    )
                except Exception as e:
                    print(f"[tracking-fail] {code} → {rname}: {e}")
                    detail["tracking"].append(
                        {"supervisor": rname, "error": str(e)}
                    )

            # 已发过 IM，或督导已闭环：只补全跟踪，不再发卡
            if not need_send:
                detail["recipients"].append(
                    {"name": rname, "role": role, "action": "skip_already_sent"}
                )
                continue
            if is_supervisor_role(role) and tracking_status == STATUS_DONE:
                detail["recipients"].append(
                    {"name": rname, "role": role, "action": "skip_already_closed"}
                )
                continue

            card = build_vip_alert_card(
                item,
                role=role,
                tracking_record_id=(
                    tracking_record_id
                    if tracking_record_id
                    else ("dry_run" if dry_run and is_supervisor_role(role) else "")
                )
                if is_supervisor_role(role)
                else "",
            )
            send_attempts += 1

            if dry_run:
                detail["recipients"].append(
                    {
                        "name": rname,
                        "role": role,
                        "action": "dry_run",
                        "has_button": is_supervisor_role(role) and bool(tracking_record_id or dry_run),
                    }
                )
                print(
                    f"[dry-run] {code} VIN={item['vin']} {item.get('name')} "
                    f"{item.get('region')}/{item.get('vip_level')} "
                    f"→ {rname}({role})"
                )
                continue

            resp = send_card_message(oid, card)
            if resp:
                ok_any = True
                message_id = resp.get("message_id") or ""
                print(f"[sent] {code} → {rname}({role})")
                detail["recipients"].append(
                    {
                        "name": rname,
                        "role": role,
                        "action": "sent",
                        "message_id": message_id,
                    }
                )
                if is_supervisor_role(role) and tracking_record_id:
                    try:
                        set_alert_triggered(tracking_record_id, message_id=message_id)
                    except Exception as e:
                        print(f"[tracking-trigger-fail] {code}: {e}")
            else:
                print(f"[fail] {code} → {rname}({role})")
                detail["recipients"].append(
                    {"name": rname, "role": role, "action": "failed"}
                )
            time.sleep(0.35)

        if already_sent:
            detail["status"] = "skipped_sent_tracking_refreshed"
        elif dry_run:
            detail["status"] = "dry_run"
            if send_attempts:
                result["to_send"] += 1
        elif ok_any:
            recipient_names = [
                x.get("name")
                for x in detail["recipients"]
                if x.get("action") == "sent"
            ]
            already[code] = {
                "vin": item["vin"],
                "sent_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
                "name": item.get("name", ""),
                "region": item.get("region", ""),
                "vip_level": item.get("vip_level", ""),
                "recipients": recipient_names,
            }
            result["sent"] += 1
            result["to_send"] += 1
            detail["status"] = "sent"
        elif send_attempts == 0:
            # 无需发送（例如督导均已闭环）
            detail["status"] = "no_send_needed"
            if not dry_run and code not in already:
                already[code] = {
                    "vin": item["vin"],
                    "sent_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
                    "name": item.get("name", ""),
                    "region": item.get("region", ""),
                    "vip_level": item.get("vip_level", ""),
                    "recipients": [],
                    "note": "tracking_closed_or_empty",
                }
        else:
            result["failed"] += 1
            result["to_send"] += 1
            detail["status"] = "failed"

        result["details"].append(detail)

    if not dry_run and (result["sent"] or result["tracking_upserted"]):
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
