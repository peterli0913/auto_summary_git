"""导出 Excel (内存字节流, 供 Streamlit download_button 使用)."""
from __future__ import annotations

import io
from typing import Optional, Sequence

import pandas as pd

from .schema import CORE_COLUMNS

#: 各列建议宽度
_WIDTHS = {
    "序号": 8,
    "日期": 12,
    "巡查发现": 90,
    "来源文件": 40,
    "台账类型": 14,
    "关键词": 24,
    "条目数": 10,
    "占比": 10,
}


def to_excel_bytes(df: pd.DataFrame,
                   sheet_name: str = "数据",
                   columns: Optional[Sequence[str]] = None,
                   extra_sheets: Optional[dict] = None) -> bytes:
    """把 DataFrame 写成 xlsx 字节流 (带表头样式/列宽/冻结首行/自动筛选)."""
    out = df.copy()
    if columns:
        keep = [c for c in columns if c in out.columns]
        out = out[keep]

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        _write_sheet(writer, out, sheet_name)
        for name, extra_df in (extra_sheets or {}).items():
            _write_sheet(writer, extra_df, name)
    buffer.seek(0)
    return buffer.getvalue()


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, sheet_name: str) -> None:
    safe_name = str(sheet_name)[:31] or "Sheet1"
    df.to_excel(writer, sheet_name=safe_name, index=False)
    wb = writer.book
    ws = writer.sheets[safe_name]

    header_fmt = wb.add_format({
        "bold": True, "bg_color": "#D9E1F2", "border": 1,
        "align": "center", "valign": "vcenter",
    })
    wrap_fmt = wb.add_format({"text_wrap": True, "valign": "top"})
    pct_fmt = wb.add_format({"num_format": "0.0%", "valign": "top"})

    for idx, col in enumerate(df.columns):
        ws.write(0, idx, str(col), header_fmt)
        width = _WIDTHS.get(str(col), 16)
        if str(col) == "占比":
            ws.set_column(idx, idx, width, pct_fmt)
        elif width >= 40:
            ws.set_column(idx, idx, width, wrap_fmt)
        else:
            ws.set_column(idx, idx, width)

    ws.freeze_panes(1, 0)
    if len(df.columns) and len(df):
        ws.autofilter(0, 0, len(df), len(df.columns) - 1)


def core_only(df: pd.DataFrame) -> pd.DataFrame:
    """只保留 序号/日期/巡查发现 三列 (需求里的标准输出)."""
    keep = [c for c in CORE_COLUMNS if c in df.columns]
    return df[keep].copy()
