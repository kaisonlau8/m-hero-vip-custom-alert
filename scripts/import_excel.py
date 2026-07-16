"""解析 DMS 导出的保养提醒任务 Excel。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl

SHEET_NAME = "客户回访任务中心"
REQUIRED_COLUMNS = ("VIN", "任务编码", "任务类型", "创建日期")


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_maintenance_reminder_xlsx(xlsx_path: str | Path) -> list[dict]:
    path = Path(xlsx_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Excel 不存在: {path}")

    wb = openpyxl.load_workbook(path, data_only=True)
    if SHEET_NAME in wb.sheetnames:
        ws = wb[SHEET_NAME]
    else:
        ws = wb[wb.sheetnames[0]]

    header_row = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers = [_cell_str(h) for h in header_row]
    index = {name: i for i, name in enumerate(headers) if name}
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        wb.close()
        raise RuntimeError(f"Excel 缺少列 {missing}，实际表头: {headers}")

    tasks: list[dict] = []
    for r in range(2, ws.max_row + 1):
        row = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
        if not row:
            continue
        vin = _cell_str(row[index["VIN"]]).upper()
        task_code = _cell_str(row[index["任务编码"]])
        if not vin or not task_code:
            continue
        created = row[index["创建日期"]]
        created_at = _cell_str(created)
        if hasattr(created, "strftime"):
            created_at = created.strftime("%Y-%m-%d %H:%M:%S")
        store_name = ""
        if "门店名称" in index:
            store_name = _cell_str(row[index["门店名称"]])
        region = ""
        if "区域" in index:
            region = _cell_str(row[index["区域"]])
        tasks.append(
            {
                "vin": vin,
                "task_code": task_code,
                "task_type": _cell_str(row[index["任务类型"]]),
                "created_at": created_at,
                "store_name": store_name,
                "region": region,
            }
        )

    wb.close()
    return tasks


def find_latest_download(download_dir: str | Path) -> Path | None:
    directory = Path(download_dir)
    if not directory.exists():
        return None
    files = sorted(
        [
            p
            for p in directory.glob("*.xlsx")
            if p.is_file() and not p.name.startswith("~$")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        raise SystemExit("用法: python import_excel.py <xlsx>")
    data = import_maintenance_reminder_xlsx(path)
    print(json.dumps({"count": len(data), "sample": data[:3]}, ensure_ascii=False, indent=2))
