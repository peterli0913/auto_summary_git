"""按 跑冒滴漏与静电风险专项跟踪.xlsx 完全一致的格式输出."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from .schema import OUTPUT_COLUMNS


def write_output(df: pd.DataFrame, path: Path,
                 sheet_name: str = "Sheet1") -> Path:
    """输出 Excel, 列顺序 / 名称与目标文件一致."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    for col in OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[OUTPUT_COLUMNS]

    # 日期统一为 yyyy-mm-dd 字符串 (与目标文件一致)
    if "日期" in out.columns:
        out["日期"] = out["日期"].apply(_format_date)

    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        out.to_excel(writer, sheet_name=sheet_name, index=False)
        wb = writer.book
        ws = writer.sheets[sheet_name]
        # 简单列宽设置 (可读性)
        widths = {
            "来源": 14, "巡查类型": 12, "日期": 12, "厂区": 10,
            "属地": 10, "责任区域": 10, "事件描述": 60,
            "隐患类型": 10, "分类": 14,
            "原因分析（EHS）": 18, "原因归类（EHS）": 14,
            "整改结果": 24, "调查报告": 16,
            "月份": 6, "周": 10, "key": 40,
        }
        for i, col in enumerate(OUTPUT_COLUMNS):
            ws.set_column(i, i, widths.get(col, 12))
        # 表头加粗
        bold = wb.add_format({"bold": True})
        ws.set_row(0, None, bold)
    return path


def _format_date(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        # 已经是 yyyy-mm-dd 形式
        try:
            ts = pd.to_datetime(v, errors="coerce")
        except Exception:
            return v
        if pd.isna(ts):
            return v
        return ts.strftime("%Y-%m-%d")
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return str(v)
