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
                     PLANT_COL, PLANT_COLUMN_PRIORITY, SOURCE_FILE_COL,
                     UNKNOWN_PLANT, Kind, content_candidates,
                     date_candidates, detect_kind, normalize_plant,
                     plant_from_filename)

#: 依次尝试的表头行号
HEADER_CANDIDATES: Tuple[int, ...] = (1, 0, 2)


def _preferred_engine() -> Optional[str]:
    """优先用 calamine (Rust 实现, 比 openpyxl 快 4 倍以上).

    没装就返回 None, 让 pandas 自己挑 (openpyxl / xlrd).
    """
    try:
        import python_calamine  # noqa: F401
        return "calamine"
    except Exception:
        return None


_ENGINE = _preferred_engine()


def _open_workbook(buf: Any) -> pd.ExcelFile:
    """打开工作簿; calamine 读不了的个别文件回退到默认引擎."""
    if _ENGINE:
        try:
            return pd.ExcelFile(buf, engine=_ENGINE)
        except Exception:
            if hasattr(buf, "seek"):
                buf.seek(0)
    return pd.ExcelFile(buf)


@dataclass
class ReadResult:
    """单个文件的解析结果 + 诊断信息 (供界面展示)."""
    file_name: str
    kind: str = Kind.UNKNOWN
    sheet_name: str = ""
    header_row: Optional[int] = None
    date_column: Optional[str] = None
    content_column: Optional[str] = None
    plant_source: str = ""          # "文件名" / 具体列名 / "未识别"
    n_rows: int = 0
    n_valid: int = 0
    n_date_parsed: int = 0
    n_plant_known: int = 0
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

def pick_plant_column(df: pd.DataFrame) -> Optional[str]:
    """选出厂区列: 按 整改厂区 > 厂区 > 组织厂区 的优先级."""
    cols = [str(c).strip() for c in df.columns]
    for name in PLANT_COLUMN_PRIORITY:
        if name in cols and df[name].notna().any():
            return name
    return None


def resolve_plants(df: pd.DataFrame, file_name: str) -> Tuple[pd.Series, str]:
    """给每行定一个厂区, 返回 (厂区列, 判定依据说明).

    优先级按用户要求:
      1. 文件名里出现厂区代号 -> 整个文件都算该厂区
      2. 否则看内容里的厂区列 (整改厂区 > 厂区 > 组织厂区)
    识别不出来的记成 "未识别", 不丢数据.
    """
    from_name = plant_from_filename(file_name)
    if from_name:
        return pd.Series([from_name] * len(df), index=df.index), "文件名"

    column = pick_plant_column(df)
    if column is None:
        return pd.Series([UNKNOWN_PLANT] * len(df), index=df.index), UNKNOWN_PLANT

    # 同一列里重复值很多, 先按唯一值算一遍再映射回去
    raw = df[column].astype("string")
    mapping = {v: (normalize_plant(v) or UNKNOWN_PLANT)
               for v in raw.dropna().unique()}
    plants = raw.map(mapping).fillna(UNKNOWN_PLANT)
    return plants, column


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

def _frame_from_grid(grid: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """把 header=None 读出来的整块表格, 按指定表头行切成正常 DataFrame."""
    cols = [str(c).strip() for c in grid.iloc[header_row].tolist()]
    # 同名列补后缀, 避免后续按列名取值时拿到 DataFrame
    seen: dict[str, int] = {}
    unique_cols = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            unique_cols.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            unique_cols.append(c)

    body = grid.iloc[header_row + 1:].copy()
    body.columns = unique_cols
    body = body.dropna(how="all").reset_index(drop=True)
    return body


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
            xl = _open_workbook(buf)

            # 每个 sheet 只整体读一次 (header=None), 再在内存里挑表头行.
            # 之前是 "探测读 + 全量读", 同一张表要解析两遍.
            best: Optional[Tuple[pd.DataFrame, str, int]] = None
            fallback: Optional[Tuple[pd.DataFrame, str, int]] = None
            for sheet in xl.sheet_names:
                try:
                    grid = xl.parse(sheet, header=None, dtype=object)
                except Exception:
                    continue
                if grid.empty:
                    continue
                if fallback is None:
                    fallback = (grid, sheet, 1 if len(grid) > 1 else 0)
                for header in HEADER_CANDIDATES:
                    if header >= len(grid):
                        continue
                    cols = [str(c).strip() for c in grid.iloc[header].tolist()]
                    if _has_known_content(cols):
                        best = (grid, sheet, header)
                        break
                if best:
                    break

            if best is None:
                if fallback is None:
                    result.error = "文件里没有可读取的工作表"
                    return result
                best = fallback

        grid, sheet, header = best
        df = _frame_from_grid(grid, header)
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
        plants, plant_source = resolve_plants(df, file_name)
        result.plant_source = plant_source

        out = pd.DataFrame({
            DATE_COL: dates.values,
            PLANT_COL: plants.values,
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
        result.n_plant_known = int((out[PLANT_COL] != UNKNOWN_PLANT).sum())
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
        merged = pd.DataFrame(columns=[DATE_COL, PLANT_COL, CONTENT_COL,
                                       SOURCE_FILE_COL, KIND_COL])
    return merged, results
