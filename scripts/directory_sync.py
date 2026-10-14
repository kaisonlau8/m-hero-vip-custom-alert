"""同步「参与考核特约店清单」，并读写本系统全国收件人。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PLUGIN_ROOT / ".env")

from feishu_client import BASE_URL, _auth_headers, cached_open_id, resolve_phone_to_open_id  # noqa: E402
from time_utils import beijing_strftime  # noqa: E402

DIRECTORY_PATH = PLUGIN_ROOT / "data" / "store_directory.json"
NATIONAL_PATH = PLUGIN_ROOT / "config" / "national_recipients.json"
SYNC_STATE_PATH = PLUGIN_ROOT / ".runtime" / "directory-sync-state.json"


def _directory_config() -> tuple[str, str, str]:
    app_token = os.getenv("DIRECTORY_BITABLE_APP_TOKEN", "").strip()
    table_id = os.getenv("DIRECTORY_TABLE_ID", "").strip()
    view_id = os.getenv("DIRECTORY_VIEW_ID", "").strip()
    if not app_token or not table_id:
        raise RuntimeError("缺少 DIRECTORY_BITABLE_APP_TOKEN 或 DIRECTORY_TABLE_ID")
    return app_token, table_id, view_id


def _field_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _field_text(item)
            if text:
                parts.append(text)
        return "".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "en_name"):
            if value.get(key):
                return str(value.get(key)).strip()
    return str(value).strip()


def _normalize_phone(value: Any) -> str:
    phone = _field_text(value).replace("+86-", "").replace("+86", "").replace(" ", "").replace("-", "")
    if phone.endswith(".0") and phone[:-2].isdigit():
        phone = phone[:-2]
    return phone


def _parse_user(value: Any) -> tuple[str, str]:
    items = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("en_name") or "").strip()
        open_id = str(item.get("id") or item.get("open_id") or "").strip()
        if name or open_id:
            return name, open_id
    return "", ""


def _fetch_records() -> list[dict]:
    app_token, table_id, view_id = _directory_config()
    records: list[dict] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500, "user_id_type": "open_id"}
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
            raise RuntimeError(f"拉取门店联系人失败: {payload.get('msg') or payload}")
        data = payload.get("data") or {}
        for item in data.get("items") or []:
            records.append(item.get("fields") or {})
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return records


def records_to_stores(records: list[dict]) -> tuple[list[dict], list[str]]:
    stores: list[dict] = []
    missing: list[str] = []
    for fields in records:
        code = _field_text(fields.get("店代码") or fields.get("门店编码"))
        if not code:
            continue
        sup_name, sup_open_id = _parse_user(fields.get("督导"))
        mgr_name, mgr_open_id = _parse_user(fields.get("门店售后业务负责人"))
        mgr_phone = _normalize_phone(fields.get("售后业务负责人手机号"))
        name = _field_text(fields.get("店名称") or fields.get("门店名称"))
        if not mgr_name and not mgr_open_id and not mgr_phone:
            missing.append(f"{code} {name}".strip())
        stores.append(
            {
                "code": code,
                "name": name,
                "region": _field_text(fields.get("区域")),
                "province": _field_text(fields.get("门店省")),
                "city": _field_text(fields.get("门店市")),
                "district": _field_text(fields.get("门店区")),
                "supervisor": {
                    "name": sup_name,
                    "phone": _normalize_phone(fields.get("督导手机号")),
                    "open_id": sup_open_id,
                },
                "service_manager": {
                    "name": mgr_name,
                    "phone": mgr_phone,
                    "open_id": mgr_open_id,
                },
            }
        )
    return stores, missing


def load_directory() -> dict:
    if not DIRECTORY_PATH.exists():
        return {"stores": [], "regions": []}
    with open(DIRECTORY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {"stores": [], "regions": []}


def load_sync_state() -> dict:
    if not SYNC_STATE_PATH.exists():
        return {}
    try:
        with open(SYNC_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sync_directory() -> dict:
    records = _fetch_records()
    stores, missing = records_to_stores(records)
    if not stores:
        raise RuntimeError("参与考核特约店清单未返回有效门店")
    regions = sorted({s["region"] for s in stores if s.get("region")})
    synced_at = beijing_strftime("%Y-%m-%d %H:%M:%S")
    DIRECTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DIRECTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"synced_at": synced_at, "stores": stores, "regions": regions},
            f,
            ensure_ascii=False,
            indent=2,
        )
    for label in missing:
        print(f"[directory-sync] 无售后负责人: {label}")
    result = {
        "ok": True,
        "synced_at": synced_at,
        "record_count": len(records),
        "store_count": len(stores),
        "region_count": len(regions),
        "missing_manager_count": len(missing),
    }
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SYNC_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[directory-sync] 门店 {result['store_count']} 家，区域 {result['region_count']} 个")
    return result


NATIONAL_LEVELS = ("S级", "A级", "B级")


def _normalize_levels(value: Any) -> list[str]:
    """客户级别。空列表表示全部。S级 会匹配「S级」和「S级（高管）」。"""
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else str(value).replace("，", ",").split(",")
    out: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in {"全部", "*"}:
            continue
        if text in {"S", "S级", "S级（高管）"}:
            text = "S级"
        elif text in {"A", "A级", "A级（中层）"}:
            text = "A级"
        elif text in {"B", "B级"}:
            text = "B级"
        if text not in out:
            out.append(text)
    return out


def national_level_matches(levels: list[str] | None, vip_level: str) -> bool:
    selected = _normalize_levels(levels or [])
    if not selected:
        return True
    current = str(vip_level or "").strip()
    if not current:
        return False
    return any(current == level or current.startswith(level) for level in selected)


def load_national_recipients() -> list[dict]:
    if not NATIONAL_PATH.exists():
        return []
    with open(NATIONAL_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("recipients") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        phone = str(item.get("phone") or "").strip()
        open_id = str(item.get("open_id") or "").strip()
        levels = _normalize_levels(item.get("levels"))
        if name or phone or open_id:
            out.append({"name": name, "phone": phone, "open_id": open_id, "levels": levels})
    return out


def save_national_recipients(items: list[dict]) -> list[dict]:
    cleaned = []
    for item in items:
        name = str(item.get("name") or "").strip()
        phone = str(item.get("phone") or "").replace("+86-", "").replace("+86", "").replace(" ", "").replace("-", "")
        open_id = str(item.get("open_id") or "").strip()
        levels = _normalize_levels(item.get("levels"))
        if not open_id and phone:
            open_id = resolve_phone_to_open_id(phone) or ""
        if not name and not phone and not open_id:
            continue
        cleaned.append({"name": name, "phone": phone, "open_id": open_id, "levels": levels})
    NATIONAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NATIONAL_PATH, "w", encoding="utf-8") as f:
        json.dump({"recipients": cleaned}, f, ensure_ascii=False, indent=2)
    return cleaned


def national_rows_for_page() -> list[dict]:
    rows = []
    for item in load_national_recipients():
        open_id = item.get("open_id") or ""
        if not open_id and item.get("phone"):
            open_id = cached_open_id(item["phone"])
        rows.append({**item, "open_id": open_id, "resolved": bool(open_id)})
    return rows
