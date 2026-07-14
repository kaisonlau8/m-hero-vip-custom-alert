"""从飞书多维表格同步 VIP 客户清单与提醒人。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

load_dotenv(PLUGIN_ROOT / ".env")

from feishu_client import BASE_URL, _auth_headers  # noqa: E402
from time_utils import beijing_strftime  # noqa: E402

VIP_CACHE_PATH = PLUGIN_ROOT / "data" / "vip_cache.json"
RECIPIENTS_LIST_PATH = PLUGIN_ROOT / "data" / "recipients_list.json"
SYNC_STATE_PATH = PLUGIN_ROOT / ".runtime" / "bitable-sync-state.json"

VIP_FIELDS = ("VIN", "姓名", "客户类别", "VIP级别", "VIP属性", "车系")


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text is None and "name" in item:
                    text = item.get("name")
                if text is not None:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        # MultiSelect 用顿号拼接
        joined = "、".join(p.strip() for p in parts if str(p).strip())
        return joined
    if isinstance(value, dict):
        if "text" in value:
            return str(value.get("text") or "").strip()
        if "name" in value:
            return str(value.get("name") or "").strip()
    return str(value).strip()


def _normalize_phone(value: Any) -> str:
    phone = _field_text(value)
    phone = phone.replace("+86-", "").replace("+86", "").replace(" ", "").replace("-", "")
    if phone.endswith(".0") and phone[:-2].isdigit():
        phone = phone[:-2]
    if phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    return phone


def _bitable_ids() -> dict[str, str]:
    cfg = {
        "app_token": os.getenv("BITABLE_APP_TOKEN", "").strip(),
        "vip_table_id": os.getenv("BITABLE_VIP_TABLE_ID", "").strip(),
        "vip_view_id": os.getenv("BITABLE_VIP_VIEW_ID", "").strip(),
        "recipient_table_id": os.getenv("BITABLE_RECIPIENT_TABLE_ID", "").strip(),
        "recipient_view_id": os.getenv("BITABLE_RECIPIENT_VIEW_ID", "").strip(),
    }
    if not cfg["app_token"] or not cfg["vip_table_id"] or not cfg["recipient_table_id"]:
        raise RuntimeError(
            "缺少 BITABLE_APP_TOKEN / BITABLE_VIP_TABLE_ID / BITABLE_RECIPIENT_TABLE_ID"
        )
    return cfg


def fetch_bitable_records(table_id: str, view_id: str = "") -> list[dict]:
    app_token = _bitable_ids()["app_token"]
    records: list[dict] = []
    page_token = ""

    while True:
        params: dict[str, Any] = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=_auth_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"拉取多维表格失败: {payload.get('msg') or payload}")

        data = payload.get("data") or {}
        for item in data.get("items") or []:
            records.append(item.get("fields") or {})

        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break

    return records


def records_to_vip_cache(records: list[dict]) -> dict[str, dict]:
    """VIN → 通知所需字段。"""
    cache: dict[str, dict] = {}
    for fields in records:
        vin = _field_text(fields.get("VIN")).upper()
        if not vin:
            continue
        cache[vin] = {
            "vin": vin,
            "name": _field_text(fields.get("姓名")),
            "customer_category": _field_text(fields.get("客户类别")),
            "vip_level": _field_text(fields.get("VIP级别")),
            "vip_attrs": _field_text(fields.get("VIP属性")),
            "series": _field_text(fields.get("车系")),
        }
    return cache


def records_to_recipients(records: list[dict]) -> list[dict]:
    recipients: list[dict] = []
    seen: set[str] = set()
    for fields in records:
        name = _field_text(fields.get("提醒人姓名"))
        phone = _normalize_phone(fields.get("提醒人飞书电话号"))
        if not phone or phone in seen:
            continue
        seen.add(phone)
        recipients.append({"name": name, "phone": phone})
    return recipients


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_vip_cache() -> dict[str, dict]:
    if not VIP_CACHE_PATH.exists():
        return {}
    with open(VIP_CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("by_vin") or {}


def load_recipients_list() -> list[dict]:
    if not RECIPIENTS_LIST_PATH.exists():
        return []
    with open(RECIPIENTS_LIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("recipients") or []


def sync_all() -> dict:
    cfg = _bitable_ids()
    vip_records = fetch_bitable_records(cfg["vip_table_id"], cfg["vip_view_id"])
    recipient_records = fetch_bitable_records(
        cfg["recipient_table_id"], cfg["recipient_view_id"]
    )

    by_vin = records_to_vip_cache(vip_records)
    recipients = records_to_recipients(recipient_records)

    synced_at = beijing_strftime("%Y-%m-%d %H:%M:%S")
    _save_json(
        VIP_CACHE_PATH,
        {
            "synced_at": synced_at,
            "count": len(by_vin),
            "by_vin": by_vin,
        },
    )
    _save_json(
        RECIPIENTS_LIST_PATH,
        {
            "synced_at": synced_at,
            "count": len(recipients),
            "recipients": recipients,
        },
    )

    result = {
        "ok": True,
        "synced_at": synced_at,
        "vip_count": len(by_vin),
        "vip_record_count": len(vip_records),
        "recipient_count": len(recipients),
        "vip_cache": str(VIP_CACHE_PATH),
        "recipients_list": str(RECIPIENTS_LIST_PATH),
    }
    _save_json(SYNC_STATE_PATH, result)
    print(
        f"[bitable-sync] 完成: VIP {result['vip_count']} 人，"
        f"提醒人 {result['recipient_count']} 人"
    )
    return result


if __name__ == "__main__":
    print(json.dumps(sync_all(), ensure_ascii=False, indent=2))
