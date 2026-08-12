"""VIP 客户维保跟踪表：按任务+督导 upsert，按钮回写闭环状态。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
load_dotenv(PLUGIN_ROOT / ".env")

from time_utils import beijing_now, beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()

BASE_URL = "https://open.feishu.cn/open-apis"
BJ = ZoneInfo("Asia/Shanghai")

STATUS_PENDING = "待提醒门店"
STATUS_DONE = "已提醒门店"

# 每日 upsert 可刷新的业务列（不含闭环字段）
BUSINESS_FIELDS = (
    "任务编码",
    "VIN",
    "客户姓名",
    "VIP级别",
    "VIP属性",
    "客户类别",
    "车系",
    "门店编码",
    "门店名称",
    "区域",
    "任务类型",
    "任务状态",
    "售后车系",
    "用车人名称",
    "用车人电话",
    "车主名称",
    "车主电话",
    "下次预约时间",
    "预约单号",
    "任务创建日期",
    "到期日期",
    "首次回访日期",
    "首次回访人",
    "DMS有效状态",
    "关闭时间",
    "最近同步时间",
)

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0, "app_id": None}
_fallback_warned = False
_force_main_app = False  # 跟踪应用无权限后，后续请求直接用 HeroClaw


def _main_credentials() -> tuple[str, str]:
    app_id = os.getenv("APP_ID", "").strip()
    app_secret = os.getenv("APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺少 APP_ID 或 APP_SECRET")
    return app_id, app_secret


def _tracking_credentials() -> tuple[str, str, bool]:
    """返回 (app_id, app_secret, is_fallback)。"""
    global _fallback_warned
    tid = os.getenv("TRACKING_APP_ID", "").strip()
    tsec = os.getenv("TRACKING_APP_SECRET", "").strip()
    fallback = os.getenv("TRACKING_FALLBACK_TO_MAIN", "1").strip() not in (
        "0",
        "false",
        "False",
        "no",
    )
    if tid and tsec:
        return tid, tsec, False
    if fallback:
        if not _fallback_warned:
            print("[tracking] WARN 未配置 TRACKING_APP_*，回退使用 HeroClaw APP_ID")
            _fallback_warned = True
        aid, asec = _main_credentials()
        return aid, asec, True
    raise RuntimeError("缺少 TRACKING_APP_ID / TRACKING_APP_SECRET")


def _tracking_ids() -> dict[str, str]:
    app_token = os.getenv("TRACKING_BITABLE_APP_TOKEN", "").strip()
    table_id = os.getenv("TRACKING_TABLE_ID", "").strip()
    if not app_token or not table_id:
        raise RuntimeError("缺少 TRACKING_BITABLE_APP_TOKEN / TRACKING_TABLE_ID")
    return {"app_token": app_token, "table_id": table_id}


def _get_token(force_main: bool = False) -> str:
    if force_main:
        app_id, app_secret = _main_credentials()
    else:
        app_id, app_secret, _ = _tracking_credentials()

    if (
        _token_cache["token"]
        and _token_cache.get("app_id") == app_id
        and time.time() < _token_cache["expires_at"]
    ):
        return _token_cache["token"]

    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"跟踪表 token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200) - 300
    _token_cache["app_id"] = app_id
    return _token_cache["token"]


def _auth_headers(force_main: bool = False) -> dict:
    return {"Authorization": f"Bearer {_get_token(force_main=force_main)}"}


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """优先跟踪应用；权限不足且允许回退时改用 HeroClaw。"""
    global _fallback_warned, _force_main_app
    urls = f"{BASE_URL}{path}"
    app_id, _, is_already_fallback = _tracking_credentials()
    allow_fallback = (
        not is_already_fallback
        and os.getenv("TRACKING_FALLBACK_TO_MAIN", "1").strip()
        not in ("0", "false", "False", "no")
    )
    force_main = _force_main_app or is_already_fallback

    for attempt in range(2):
        headers = {
            **_auth_headers(force_main=force_main),
            "Content-Type": "application/json",
        }
        resp = requests.request(
            method,
            urls,
            headers=headers,
            params=params,
            json=json_body,
            timeout=30,
        )
        try:
            payload = resp.json()
        except Exception:
            resp.raise_for_status()
            raise
        code = payload.get("code")
        if code == 0:
            return payload
        # 权限不足 → 回退 HeroClaw，并记住后续直连
        if (
            attempt == 0
            and allow_fallback
            and not force_main
            and code in (99991672, 99991663, 1254043)
        ):
            if not _fallback_warned:
                print(
                    f"[tracking] WARN 应用 {app_id} 无多维表权限 (code={code})，"
                    "回退 HeroClaw；请为 TRACKING_APP 开通 bitable:app"
                )
                _fallback_warned = True
            _force_main_app = True
            force_main = True
            _token_cache["token"] = None
            continue
        raise RuntimeError(f"跟踪表 API 失败: {payload.get('msg') or payload}")
    raise RuntimeError("跟踪表 API 失败: 重试耗尽")


def _dt_to_ms(value: datetime | None = None) -> int:
    dt = value or beijing_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ)
    return int(dt.timestamp() * 1000)


def _task_business_fields(task: dict) -> dict[str, Any]:
    return {
        "任务编码": (task.get("task_code") or "").strip(),
        "VIN": (task.get("vin") or "").strip().upper(),
        "客户姓名": task.get("name") or "",
        "VIP级别": task.get("vip_level") or "",
        "VIP属性": task.get("vip_attrs") or "",
        "客户类别": task.get("customer_category") or "",
        "车系": task.get("series") or "",
        "门店编码": task.get("store_code") or "",
        "门店名称": task.get("store_name") or "",
        "区域": task.get("region") or "",
        "任务类型": task.get("task_type") or "",
        "任务状态": task.get("task_status") or "",
        "售后车系": task.get("aftersales_series") or "",
        "用车人名称": task.get("driver_name") or "",
        "用车人电话": task.get("driver_phone") or "",
        "车主名称": task.get("owner_name") or "",
        "车主电话": task.get("owner_phone") or "",
        "下次预约时间": task.get("next_appointment_at") or "",
        "预约单号": task.get("appointment_no") or "",
        "任务创建日期": task.get("created_at") or "",
        "到期日期": task.get("due_at") or "",
        "首次回访日期": task.get("first_followup_at") or "",
        "首次回访人": task.get("first_followup_by") or "",
        "DMS有效状态": task.get("dms_valid_status") or "",
        "关闭时间": task.get("closed_at") or "",
        "最近同步时间": beijing_strftime("%Y-%m-%d %H:%M:%S"),
    }


def _field_text(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("name") or ""
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "、".join(p.strip() for p in parts if str(p).strip())
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def find_tracking_record(task_code: str, supervisor_open_id: str) -> dict | None:
    """按任务编码筛选后本地匹配督导 open_id。"""
    ids = _tracking_ids()
    task_code = (task_code or "").strip()
    supervisor_open_id = (supervisor_open_id or "").strip()
    if not task_code or not supervisor_open_id:
        return None

    page_token = ""
    while True:
        body: dict[str, Any] = {
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": "任务编码",
                        "operator": "is",
                        "value": [task_code],
                    }
                ],
            },
            "page_size": 100,
        }
        if page_token:
            body["page_token"] = page_token
        payload = _request(
            "POST",
            f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records/search",
            json_body=body,
        )
        data = payload.get("data") or {}
        for item in data.get("items") or []:
            fields = item.get("fields") or {}
            users = fields.get("督导") or []
            if isinstance(users, dict):
                users = [users]
            for u in users:
                if not isinstance(u, dict):
                    continue
                oid = str(u.get("id") or u.get("open_id") or "").strip()
                if oid == supervisor_open_id:
                    return {
                        "record_id": item.get("record_id") or "",
                        "fields": fields,
                        "status": _field_text(fields.get("跟踪状态")),
                        "message_id": _field_text(fields.get("飞书消息ID")),
                    }
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return None


def upsert_tracking_from_task(
    task: dict,
    supervisor: dict,
    *,
    dry_run: bool = False,
) -> dict:
    """新建或补全业务字段；不重置闭环列。"""
    open_id = (supervisor.get("open_id") or "").strip()
    name = supervisor.get("name") or open_id
    business = _task_business_fields(task)
    task_code = business["任务编码"]
    if not task_code or not open_id:
        raise RuntimeError("upsert 需要 task_code 与督导 open_id")

    existing = None if dry_run else find_tracking_record(task_code, open_id)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "created": True,
            "record_id": "",
            "status": STATUS_PENDING,
            "supervisor": name,
            "task_code": task_code,
        }

    ids = _tracking_ids()
    if existing:
        # 刷新业务列（含清空）；保留闭环字段
        patch = {k: business.get(k) or "" for k in BUSINESS_FIELDS}
        if patch:
            _request(
                "PUT",
                f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records/{existing['record_id']}",
                json_body={"fields": patch},
            )
        return {
            "ok": True,
            "created": False,
            "record_id": existing["record_id"],
            "status": existing.get("status") or "",
            "message_id": existing.get("message_id") or "",
            "supervisor": name,
            "task_code": task_code,
        }

    fields = {
        **{k: v for k, v in business.items() if v not in (None, "")},
        "督导": [{"id": open_id}],
        "跟踪状态": STATUS_PENDING,
    }
    payload = _request(
        "POST",
        f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records",
        json_body={"fields": fields},
    )
    record = (payload.get("data") or {}).get("record") or {}
    return {
        "ok": True,
        "created": True,
        "record_id": record.get("record_id") or "",
        "status": STATUS_PENDING,
        "message_id": "",
        "supervisor": name,
        "task_code": task_code,
    }


def set_alert_triggered(
    record_id: str,
    *,
    message_id: str = "",
    triggered_at: datetime | None = None,
) -> None:
    """首次发卡成功后写入提醒触发时间与消息 ID（仅填空）。"""
    if not record_id:
        return
    ids = _tracking_ids()
    existing = get_record(record_id)
    fields_cur = (existing or {}).get("fields") or {}
    patch: dict[str, Any] = {}
    if not fields_cur.get("提醒触发时间"):
        patch["提醒触发时间"] = _dt_to_ms(triggered_at)
    if message_id and not _field_text(fields_cur.get("飞书消息ID")):
        patch["飞书消息ID"] = message_id
    if not patch:
        return
    _request(
        "PUT",
        f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records/{record_id}",
        json_body={"fields": patch},
    )


def get_record(record_id: str) -> dict | None:
    if not record_id:
        return None
    ids = _tracking_ids()
    payload = _request(
        "GET",
        f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records/{record_id}",
    )
    rec = (payload.get("data") or {}).get("record")
    if not rec:
        return None
    fields = rec.get("fields") or {}
    return {
        "record_id": rec.get("record_id") or record_id,
        "fields": fields,
        "status": _field_text(fields.get("跟踪状态")),
        "message_id": _field_text(fields.get("飞书消息ID")),
        "task_code": _field_text(fields.get("任务编码")),
    }


def mark_store_reminded(
    record_id: str,
    *,
    confirmer_open_id: str,
    confirmed_at: datetime | None = None,
) -> dict:
    """督导点击「已提醒门店」回写。"""
    if not record_id:
        raise RuntimeError("缺少 record_id")
    ids = _tracking_ids()
    fields: dict[str, Any] = {
        "跟踪状态": STATUS_DONE,
        "督导确认时间": _dt_to_ms(confirmed_at),
    }
    if confirmer_open_id:
        fields["确认人"] = [{"id": confirmer_open_id}]
    _request(
        "PUT",
        f"/bitable/v1/apps/{ids['app_token']}/tables/{ids['table_id']}/records/{record_id}",
        json_body={"fields": fields},
    )
    return {
        "ok": True,
        "record_id": record_id,
        "status": STATUS_DONE,
        "confirmed_at": beijing_strftime("%Y-%m-%d %H:%M:%S"),
    }


def is_supervisor_role(role: str | None) -> bool:
    return (role or "").strip() == "supervisor"
