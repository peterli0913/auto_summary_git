"""读取单个巡查台账 Excel, 自动识别表头行 / 日期列 / 巡查发现列.

这些台账有共同特点:
  * 第 1 行是一个合并的大标题 (例如 "隐患排查"), 真正的列名在第 2 行
  * 但也可能直接第 1 行就是列名 (导出方式不同)
所以这里对 header ∈ (1, 0, 2) 依次尝试, 选出能找到 "巡查发现" 列的那一个.
"""
from __future__ import annotations

import io
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

import pandas as pd

from .schema import (CONTENT_COL, DATE_COL, DATE_COLUMN_BLOCKLIST, KIND_COL,
                     Kind, SOURCE_FILE_COL, content_candidates,
                     date_candidates, detect_kind)

#: 依次尝试的表头行号
HEADER_CANDIDATES: Tuple[int, ...] = (1, 0, 2)


@dataclass
class ReadResult:
    """单个文件的解析结果 + 诊断信息 (供界面展示)."""
    file_name: str
    kind: str = Kind.UNKNOWN
    sheet_name: str = ""
    header_row: Optional[int] = None
    date_column: Optional[str] = None
    content_column: Optional[str] = None
    n_rows: int = 0
    n_valid: int = 0
    n_date_parsed: int = 0
    error: Optional[str] = None
    frame: Optional[pd.DataFrame] = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None and self.n_valid > 0


# ---------------- 日期解析 ----------------

def parse_date_series(s: pd.Series) -> pd.Series:
    """把任意形态的日期列解析成 datetime64 (无法解析的成 NaT).

    覆盖: datetime 对象 / '2026/3/20' / '2026/7/13 0:00:00' /
          '2026-03-20' / Excel 序列号 (如 45000)
    """
    if s is None or len(s) == 0:
        return pd.Series([], dtype="datetime64[ns]")

    raw = s.copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(raw, errors="coerce")

    # Excel 序列号兜底: 仍为 NaT 但原值是较大的纯数字
    missing = parsed.isna()
    if missing.any():
        numeric = pd.to_numeric(raw[missing], errors="coerce")
        # Excel 日期序列号的合理范围 (1990-01-01 ~ 2100-01-01)
        valid = numeric.between(32874, 73415)
        if valid.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                converted = pd.to_datetime(
                    numeric[valid], unit="D", origin="1899-12-30", errors="coerce")
            parsed.loc[converted.index] = converted
    return parsed


def _score_date_column(s: pd.Series) -> float:
    """返回该列能被解析为日期的比例 (0~1)."""
    non_null = s.dropna()
    if non_null.empty:
        return 0.0
    sample = non_null.head(400)
    parsed = parse_date_series(sample)
    return float(parsed.notna().sum()) / float(len(sample))


def pick_date_column(df: pd.DataFrame, kind: str) -> Optional[str]:
    """选出最合适的日期列: 先按类型的已知列名, 再按名字里含 日期/时间 的列打分."""
    cols = [str(c).strip() for c in df.columns]

    for name in date_candidates(kind):
        if name in cols and name not in DATE_COLUMN_BLOCKLIST:
            if _score_date_column(df[name]) >= 0.5:
                return name

    # 打分挑选: 名字含 日期/时间 且不在黑名单
    scored: List[Tuple[float, str]] = []
    for c in cols:
        if c in DATE_COLUMN_BLOCKLIST:
            continue
        if ("日期" in c) or ("时间" in c):
            scored.append((_score_date_column(df[c]), c))
    if scored:
        scored.sort(key=lambda x: -x[0])
        if scored[0][0] >= 0.5:
            return scored[0][1]

    # 最后兜底: 任何一列解析率 >= 0.8
    fallback: List[Tuple[float, str]] = []
    for c in cols:
        if c in DATE_COLUMN_BLOCKLIST:
            continue
        fallback.append((_score_date_column(df[c]), c))
    if fallback:
        fallback.sort(key=lambda x: -x[0])
        if fallback[0][0] >= 0.8:
            return fallback[0][1]
    return None


# ---------------- 巡查发现列 ----------------

def pick_content_column(df: pd.DataFrame, kind: str) -> Optional[str]:
    """选出 "巡查发现" 列: 先按已知列名, 再兜底选平均文本最长的一列."""
    cols = [str(c).strip() for c in df.columns]
    for name in content_candidates(kind):
        if name in cols and df[name].notna().any():
            return name

    # 兜底: 平均字符数最长的文本列 (排除日期/人名/状态这类短字段)
    best: Optional[Tuple[float, str]] = None
    for c in cols:
        if ("日期" in c) or ("时间" in c):
            continue
        ser = df[c].dropna().astype(str)
        if ser.empty:
            continue
        avg_len = float(ser.str.len().mean())
        if avg_len < 8:  # 太短的不可能是描述
            continue
        if best is None or avg_len > best[0]:
            best = (avg_len, c)
    return best[1] if best else None


# ---------------- 主读取逻辑 ----------------

def _dedupe_header_row(df: pd.DataFrame) -> pd.DataFrame:
    """有些导出会让第一行数据重复列名, 去掉它."""
    if len(df) == 0:
        return df
    first = df.iloc[0]
    same = sum(1 for c in df.columns if str(first.get(c, "")).strip() == str(c).strip())
    if same >= max(2, len(df.columns) // 2):
        return df.iloc[1:].reset_index(drop=True)
    return df


def _has_known_content(columns: Sequence[Any]) -> bool:
    cols = {str(c).strip() for c in columns}
    from .schema import KNOWN_CONTENT_COLUMNS
    return any(c in cols for c in KNOWN_CONTENT_COLUMNS)


def read_inspection_excel(source: Union[str, Path, io.BytesIO, bytes],
                          file_name: Optional[str] = None) -> ReadResult:
    """读取一个巡查台账 Excel, 返回统一 3 列 DataFrame + 诊断信息.

    source 可以是路径, 也可以是上传得到的 bytes / BytesIO.
    """
    if file_name is None:
        file_name = Path(str(source)).name if isinstance(source, (str, Path)) else "uploaded.xlsx"
    result = ReadResult(file_name=file_name)

    # 统一成可重复读取的 BytesIO (ExcelFile 会消耗流)
    if isinstance(source, (bytes, bytearray)):
        buf: Any = io.BytesIO(source)
    elif isinstance(source, io.BytesIO):
        source.seek(0)
        buf = io.BytesIO(source.read())
    else:
        buf = source

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            xl = pd.ExcelFile(buf)
            sheet_names = xl.sheet_names

            best: Optional[Tuple[pd.DataFrame, str, int]] = None
            for sheet in sheet_names:
                for header in HEADER_CANDIDATES:
                    try:
                        probe = xl.parse(sheet, header=header, nrows=6, dtype=object)
                    except Exception:
                        continue
                    if _has_known_content(probe.columns):
                        full = xl.parse(sheet, header=header, dtype=object)
                        best = (full, sheet, header)
                        break
                if best:
                    break

            # 没匹配到已知列名 -> 用第一个 sheet 的 header=1 兜底
            if best is None:
                sheet = sheet_names[0]
                header = 1
                try:
                    full = xl.parse(sheet, header=header, dtype=object)
                except Exception:
                    header = 0
                    full = xl.parse(sheet, header=header, dtype=object)
                best = (full, sheet, header)

        df, sheet, header = best
        df.columns = [str(c).strip() for c in df.columns]
        df = _dedupe_header_row(df)

        result.sheet_name = sheet
        result.header_row = header
        result.n_rows = len(df)
        result.kind = detect_kind(file_name=file_name, sheet_name=sheet,
                                  columns=list(df.columns))

        content_col = pick_content_column(df, result.kind)
        date_col = pick_date_column(df, result.kind)
        result.content_column = content_col
        result.date_column = date_col

        if content_col is None:
            result.error = "未能识别 '巡查发现' 列"
            return result

        content = df[content_col].astype("string").str.strip()
        dates = (parse_date_series(df[date_col]) if date_col
                 else pd.Series([pd.NaT] * len(df), index=df.index))

        out = pd.DataFrame({
            DATE_COL: dates.values,
            CONTENT_COL: content.values,
            SOURCE_FILE_COL: file_name,
            KIND_COL: result.kind,
        })
        # 丢掉空描述 (没有可分析的内容)
        out = out[out[CONTENT_COL].notna() & (out[CONTENT_COL].str.len() > 0)]
        out = out.reset_index(drop=True)

        result.frame = out
        result.n_valid = len(out)
        result.n_date_parsed = int(out[DATE_COL].notna().sum())
        return result

    except Exception as exc:  # noqa: BLE001 - 单个坏文件不应中断整批
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def read_many(sources: Sequence[Tuple[str, Union[str, Path, bytes, io.BytesIO]]]
              ) -> Tuple[pd.DataFrame, List[ReadResult]]:
    """批量读取, 返回 (合并后的 DataFrame, 每个文件的诊断结果).

    sources: [(file_name, path_or_bytes), ...]
    """
    frames: List[pd.DataFrame] = []
    results: List[ReadResult] = []
    for name, src in sources:
        res = read_inspection_excel(src, file_name=name)
        results.append(res)
        if res.frame is not None and len(res.frame):
            frames.append(res.frame)

    if frames:
        merged = pd.concat(frames, ignore_index=True)
    else:
        from .schema import ALL_COLUMNS
        merged = pd.DataFrame(columns=[c for c in ALL_COLUMNS if c != "序号"])
    return merged, results
