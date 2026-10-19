"""门店联系人目录解析测试（离线，使用假记录）。"""

from __future__ import annotations

import pytest

from directory_sync import records_to_stores


@pytest.fixture()
def fake_records() -> list[dict]:
    """模拟飞书多维表格返回的门店联系人记录。"""
    return [
        {
            "店代码": "D001",
            "店名称": "一号店",
            "区域": "华中1区",
            "门店省": "湖北省",
            "门店市": "武汉市",
            "门店区": "江岸区",
            "督导": [{"name": "督导甲", "id": "ou_sup_1"}],
            "督导手机号": "+86-138-0000-0000",
            "门店售后业务负责人 1": [{"name": "负责人甲", "id": "ou_mgr_1"}],
            "售后业务负责人手机号 1": "+86-138-0000-0001",
            "门店售后业务负责人 3": [{"name": "负责人丙", "id": "ou_mgr_3"}],
            "售后业务负责人手机号 3": "",
            "门店售后业务负责人 4": [],
            "售后业务负责人手机号 4": "",
        },
        {
            "店代码": "D002",
            "店名称": "二号店",
            "区域": "西北区",
            "门店售后业务负责人2": [{"name": "负责人乙", "id": "ou_mgr_2"}],
            "售后业务负责人手机号2": "13800000002",
            "门店售后业务负责人 4": [{"name": "负责人丁", "id": "ou_mgr_4"}],
            "售后业务负责人手机号 4": "13800000004",
        },
        {
            "店代码": "D003",
            "店名称": "三号店",
            "区域": "华北区",
            "门店售后业务负责人": [{"name": "旧版负责人", "id": "ou_legacy"}],
            "售后业务负责人手机号": "+86-138-0000-0003",
        },
        {
            "店代码": "D004",
            "店名称": "无负责人店",
            "区域": "华南区",
            "门店售后业务负责人 1": [],
            "售后业务负责人手机号 1": "",
            "门店售后业务负责人 2": [],
            "售后业务负责人手机号 2": "",
        },
        {
            "店代码": "D005",
            "店名称": "重复负责人店",
            "门店售后业务负责人 1": [{"name": "重复负责人", "id": "ou_dup"}],
            "售后业务负责人手机号 1": "13800000005",
            "门店售后业务负责人 2": [{"name": "重复负责人副本", "id": "ou_dup"}],
            "售后业务负责人手机号 2": "13800000006",
        },
        {
            "店代码": "D006",
            "店名称": "同名负责人店",
            "门店售后业务负责人 1": [{"name": "同名负责人", "id": ""}],
            "门店售后业务负责人 2": [{"name": "同名负责人", "id": ""}],
        },
        {
            "店名称": "缺少店代码的记录",
            "门店售后业务负责人 1": [{"name": "应被忽略", "id": "ou_ignore"}],
        },
    ]


def test_records_to_stores_parses_all_manager_slots(fake_records):
    stores, missing = records_to_stores(fake_records)

    assert [store["code"] for store in stores] == ["D001", "D002", "D003", "D004", "D005", "D006"]
    assert missing == ["D004 无负责人店"]

    by_code = {store["code"]: store for store in stores}

    store_1 = by_code["D001"]
    assert store_1["region"] == "华中1区"
    assert store_1["supervisor"] == {
        "name": "督导甲",
        "phone": "13800000000",
        "open_id": "ou_sup_1",
    }
    assert store_1["service_managers"] == [
        {"name": "负责人甲", "phone": "13800000001", "open_id": "ou_mgr_1"},
        {"name": "负责人丙", "phone": "", "open_id": "ou_mgr_3"},
    ]
    assert store_1["service_manager"] == store_1["service_managers"][0]

    store_2 = by_code["D002"]
    assert store_2["service_managers"] == [
        {"name": "负责人乙", "phone": "13800000002", "open_id": "ou_mgr_2"},
        {"name": "负责人丁", "phone": "13800000004", "open_id": "ou_mgr_4"},
    ]
    assert store_2["service_manager"] == store_2["service_managers"][0]

    store_3 = by_code["D003"]
    assert store_3["service_managers"] == [
        {"name": "旧版负责人", "phone": "13800000003", "open_id": "ou_legacy"},
    ]
    assert store_3["service_manager"] == store_3["service_managers"][0]


def test_records_to_stores_deduplicates_managers(fake_records):
    stores, missing = records_to_stores(fake_records)
    by_code = {store["code"]: store for store in stores}

    assert by_code["D005"]["service_managers"] == [
        {"name": "重复负责人", "phone": "13800000005", "open_id": "ou_dup"},
    ]
    assert by_code["D006"]["service_managers"] == [
        {"name": "同名负责人", "phone": "", "open_id": ""},
    ]

    store_4 = by_code["D004"]
    assert store_4["service_manager"] == {"name": "", "phone": "", "open_id": ""}
    assert store_4["service_managers"] == []
    assert "D004 无负责人店" in missing


def test_slot1_numbered_and_legacy_not_mixed():
    from directory_sync import _parse_service_managers
    fields = {
        "门店售后业务负责人 1": [{"name": "新经理", "id": "ou_new"}],
        "门店售后业务负责人": [{"name": "旧经理", "id": "ou_old"}],
        "售后业务负责人手机号": "13900000000",
    }
    assert _parse_service_managers(fields) == [{"name": "新经理", "phone": "", "open_id": "ou_new"}]


def test_alias_empty_does_not_override_and_duplicate_merges_phone():
    from directory_sync import _parse_service_managers
    fields = {
        "门店售后业务负责人1": [{"name": "张三", "id": "ou_a"}],
        "门店售后业务负责人 1": None,
        "门店售后业务负责人 2": [{"name": "张三", "id": "ou_a"}],
        "售后业务负责人手机号 2": "13800000000",
    }
    assert _parse_service_managers(fields) == [{"name": "张三", "phone": "13800000000", "open_id": "ou_a"}]
