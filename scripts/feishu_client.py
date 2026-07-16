"""飞书 API 客户端 — HeroClaw 认证、手机号解析、卡片消息。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from time_utils import beijing_strftime, ensure_beijing_tz  # noqa: E402

ensure_beijing_tz()

BASE_URL = "https://open.feishu.cn/open-apis"

_token_cache: dict = {"token": None, "expires_at": 0}
_recipients_cache_path = Path(__file__).resolve().parent.parent / "config" / "recipients.json"
_phone_to_open_id: dict = {}


def _get_app_credentials() -> tuple[str, str]:
    app_id = os.getenv("APP_ID", "")
    app_secret = os.getenv("APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("缺少 APP_ID 或 APP_SECRET，请在 .env 中配置")
    return app_id, app_secret


def get_tenant_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    app_id, app_secret = _get_app_credentials()
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {data}")

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200) - 300
    return _token_cache["token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_tenant_token()}"}


def _load_recipients_cache() -> None:
    global _phone_to_open_id
    if _recipients_cache_path.exists():
        with open(_recipients_cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            _phone_to_open_id = {
                e["phone"]: e["open_id"] for e in data.get("entries", []) if e.get("phone") and e.get("open_id")
            }


def _save_recipients_cache() -> None:
    entries = [
        {
            "phone": k,
            "open_id": v,
            "resolved_at": beijing_strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        }
        for k, v in _phone_to_open_id.items()
    ]
    _recipients_cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_recipients_cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"last_updated": beijing_strftime("%Y-%m-%d"), "entries": entries},
            f,
            ensure_ascii=False,
            indent=2,
        )


def resolve_phone_to_open_id(phone: str) -> str | None:
    phone = phone.lstrip("+").replace("-", "").replace(" ", "")
    if phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    if not _phone_to_open_id:
        _load_recipients_cache()
    if phone in _phone_to_open_id:
        return _phone_to_open_id[phone]

    resp = requests.post(
        f"{BASE_URL}/contact/v3/users/batch_get_id?user_id_type=open_id",
        headers=_auth_headers(),
        json={"mobiles": [phone], "emails": [], "include_resigned": False},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        print(f"[WARN] 解析手机号 {phone} 失败: {data.get('msg')}")
        return None

    user_list = data.get("data", {}).get("user_list", [])
    if not user_list:
        print(f"[WARN] 手机号 {phone} 未找到飞书用户")
        return None

    open_id = user_list[0].get("user_id") or user_list[0].get("open_id")
    if open_id:
        _phone_to_open_id[phone] = open_id
        _save_recipients_cache()
    return open_id


def send_card_message(open_id: str, card: dict) -> dict | None:
    return _send_message(open_id, msg_type="interactive", content=json.dumps(card))


def send_text_message(open_id: str, text: str) -> dict | None:
    return _send_message(open_id, msg_type="text", content=json.dumps({"text": text}))


def _send_message(open_id: str, msg_type: str, content: str) -> dict | None:
    try:
        resp = requests.post(
            f"{BASE_URL}/im/v1/messages?receive_id_type=open_id",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"receive_id": open_id, "msg_type": msg_type, "content": content},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            print(f"[ERROR] 发送消息失败: {data.get('msg')}")
            return None
        return data.get("data")
    except requests.RequestException as e:
        print(f"[ERROR] 发送消息异常: {e}")
        return None


def build_vip_alert_card(payload: dict) -> dict:
    """构建 VIP 保养提醒卡片。"""
    fields = [
        ("门店名称", payload.get("store_name", "")),
        ("区域", payload.get("region", "")),
        ("VIN", payload.get("vin", "")),
        ("姓名", payload.get("name", "")),
        ("客户类别", payload.get("customer_category", "")),
        ("VIP 级别", payload.get("vip_level", "")),
        ("VIP 属性", payload.get("vip_attrs", "")),
        ("车系", payload.get("series", "")),
        ("任务类型", payload.get("task_type", "")),
        ("创建日期", payload.get("created_at", "")),
        ("任务编码", payload.get("task_code", "")),
    ]
    lines = "\n".join(f"**{k}**：{v}" for k, v in fields if v not in (None, ""))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "VIP 客户保养提醒"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": lines}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "同一任务编码仅提醒一次 · HeroClaw",
                    }
                ],
            },
        ],
    }
