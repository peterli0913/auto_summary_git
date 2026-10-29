"""读取 5 类输入 Excel, 汇总为统一格式 DataFrame.

设计要点:
- 5 类输入文件第一行是大标题 (例如 "监控巡查情况"), 第二行才是真正的列名,
  因此使用 `header=1` 读取.
- 字段映射严格按用户描述实施, 缺失列填 None.
- 自动识别输入文件类型: 优先按 sheet 名 (如 "监控巡查情况0") 与表头组合判断,
  退化时按文件名关键字匹配, 因而支持任意命名的新数据.
- 提供 `aggregate(file_paths)` 与 `aggregate_files(label2path)` 两个入口.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union
import warnings

import pandas as pd

from .schema import OUTPUT_COLUMNS, SOURCE_MAPPING, INPUT_KINDS, make_key


# 每种输入类型可识别的列指纹 (出现这些列名 -> 判定为该类型)
KIND_FINGERPRINTS: Dict[str, List[List[str]]] = {
    "监控巡查情况": [["监控画面情况"]],
    "巡查信息": [["巡查结果", "巡查类别"]],
    "统一日常值班报告": [["发现项", "值班日期"]],
    "每日巡查报告": [["问题描述", "部门/厂房"]],
    "隐患排查": [["巡查发现", "组织厂区"], ["巡查发现", "巡查主题"]],
}


def _safe_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    return s if s else None


def _parse_date(v) -> Optional[pd.Timestamp]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return ts
    except Exception:
        return None


def _read_with_offset(path: Union[str, Path]) -> pd.DataFrame:
    """读取并把第一行(大标题)作为 sheet 名, 第二行作为表头."""
    path = Path(path)
    # 尝试 header=1 (适用于 5 类原始输入), 失败则退化到 header=0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(path, header=1, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    # 去掉表头复制行 (有时第二行也包含同样的列名)
    if len(df) and all(str(df.iloc[0].get(c, "")).strip() == c for c in df.columns):
        df = df.iloc[1:].reset_index(drop=True)
    return df


def _detect_kind(df: pd.DataFrame, file_name: str = "") -> Optional[str]:
    cols = set(df.columns)
    for kind, fps in KIND_FINGERPRINTS.items():
        for fp in fps:
            if all(col in cols for col in fp):
                return kind
    # 退化: 文件名匹配
    for kind in INPUT_KINDS:
        if kind in file_name:
            return kind
    return None


# ------------------------ 各类输入的标准化函数 ------------------------

def _row_monitor(r: pd.Series) -> Dict[str, object]:
    return {
        "来源": SOURCE_MAPPING["监控巡查情况"]["source_label"],
        "巡查类型": "监控巡查情况",
        "日期": _parse_date(r.get("日期")),
        "厂区": _safe_str(r.get("厂区")),
        "属地": None,
        "责任区域": None,
        "事件描述": _safe_str(r.get("监控画面情况")),
        "原因分析（EHS）": None,
        "原因归类（EHS）": None,
        "整改结果": None,
    }


def _row_high_mgmt(r: pd.Series) -> Dict[str, object]:
    return {
        "来源": SOURCE_MAPPING["巡查信息"]["source_label"],
        "巡查类型": _safe_str(r.get("巡查类别"))
                    or SOURCE_MAPPING["巡查信息"]["default_inspection_type"],
        "日期": _parse_date(r.get("日期")),
        "厂区": _safe_str(r.get("厂区")),
        "属地": _safe_str(r.get("属地")),
        "责任区域": _safe_str(r.get("责任区域")),
        "事件描述": _safe_str(r.get("巡查结果")),
        "原因分析（EHS）": None,
        "原因归类（EHS）": None,
        "整改结果": _safe_str(r.get("整改结果")),
    }


def _row_daily_duty(r: pd.Series) -> Dict[str, object]:
    return {
        "来源": SOURCE_MAPPING["统一日常值班报告"]["source_label"],
        "巡查类型": _safe_str(r.get("巡查类别"))
                    or SOURCE_MAPPING["统一日常值班报告"]["default_inspection_type"],
        "日期": _parse_date(r.get("值班日期") or r.get("日期")),
        "厂区": _safe_str(r.get("厂区")),
        "属地": _safe_str(r.get("属地")),
        "责任区域": _safe_str(r.get("责任区域")),
        "事件描述": _safe_str(r.get("发现项")),
        "原因分析（EHS）": None,
        "原因归类（EHS）": _safe_str(r.get("原因分类")),
        "整改结果": _safe_str(r.get("整改结果")),
    }


def _row_workshop(r: pd.Series) -> Dict[str, object]:
    return {
        "来源": SOURCE_MAPPING["每日巡查报告"]["source_label"],
        "巡查类型": _safe_str(r.get("巡查类型"))
                    or SOURCE_MAPPING["每日巡查报告"]["default_inspection_type"],
        "日期": _parse_date(r.get("时间") or r.get("日期")),
        "厂区": _safe_str(r.get("厂区")),
        "属地": _safe_str(r.get("部门/厂房")),
        "责任区域": _safe_str(r.get("车间")),
        "事件描述": _safe_str(r.get("问题描述")),
        "原因分析（EHS）": None,
        "原因归类（EHS）": None,
        "整改结果": _safe_str(r.get("整改结果")),
    }


def _row_ehs(r: pd.Series) -> Dict[str, object]:
    return {
        "来源": SOURCE_MAPPING["隐患排查"]["source_label"],
        "巡查类型": _safe_str(r.get("巡查主题"))
                    or _safe_str(r.get("巡查类型"))
                    or SOURCE_MAPPING["隐患排查"]["default_inspection_type"],
        "日期": _parse_date(r.get("巡查日期") or r.get("日期")),
        "厂区": _safe_str(r.get("组织厂区")) or _safe_str(r.get("厂区")),
        "属地": None,
        "责任区域": _safe_str(r.get("隐患地点")),
        "事件描述": _safe_str(r.get("巡查发现")),
        "原因分析（EHS）": _safe_str(r.get("原因分析")),
        "原因归类（EHS）": _safe_str(r.get("原因分类")),
        "整改结果": _safe_str(r.get("纠正措施")),
    }


KIND_NORMALIZER = {
    "监控巡查情况": _row_monitor,
    "巡查信息": _row_high_mgmt,
    "统一日常值班报告": _row_daily_duty,
    "每日巡查报告": _row_workshop,
    "隐患排查": _row_ehs,
}


def normalize_one(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """把单个输入 DataFrame 转成统一字段 DataFrame (含 月份/周/key 占位)."""
    fn = KIND_NORMALIZER[kind]
    rows = [fn(r) for _, r in df.iterrows()]
    out = pd.DataFrame(rows)
    # 过滤掉事件描述为空的行 (没有可分类的文本)
    out = out[out["事件描述"].notna() & (out["事件描述"].astype(str).str.len() > 0)]
    out = out.reset_index(drop=True)
    # 占位列 (训练数据保留)
    out["隐患类型"] = None
    out["分类"] = None
    out["调查报告"] = None
    out["月份"], out["周"] = _date_to_month_week(out["日期"])
    out["key"] = out.apply(
        lambda r: make_key(_format_date(r["日期"]), r["厂区"], r["事件描述"]),
        axis=1,
    )
    return out[OUTPUT_COLUMNS]


def _format_date(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return ""
    if isinstance(ts, pd.Timestamp):
        return ts.strftime("%Y-%m-%d")
    return str(ts)


def _date_to_month_week(s: pd.Series) -> Tuple[List[Optional[str]], List[Optional[str]]]:
    months: List[Optional[str]] = []
    weeks: List[Optional[str]] = []
    cn_week = ["第一周", "第二周", "第三周", "第四周", "第五周", "第六周"]
    for ts in s:
        if ts is None or (isinstance(ts, float) and pd.isna(ts)):
            months.append(None)
            weeks.append(None)
            continue
        if not isinstance(ts, pd.Timestamp):
            ts = pd.to_datetime(ts, errors="coerce")
            if pd.isna(ts):
                months.append(None)
                weeks.append(None)
                continue
        month = ts.month
        # 同月内的"第几周": (day-1)//7 + 1
        week_idx = min((ts.day - 1) // 7, 5)
        months.append(f"{month}月")
        weeks.append(f"{month}月{cn_week[week_idx]}")
    return months, weeks


# ------------------------ 对外入口 ------------------------

def aggregate_files(file_paths: Iterable[Union[str, Path]]) -> pd.DataFrame:
    """传入若干文件路径, 自动识别类型并汇总."""
    parts: List[pd.DataFrame] = []
    for path in file_paths:
        path = Path(path)
        try:
            df = _read_with_offset(path)
        except Exception as e:
            warnings.warn(f"读取 {path} 失败: {e}")
            continue
        kind = _detect_kind(df, path.name)
        if kind is None:
            warnings.warn(f"无法识别 {path.name} 的类型, 已跳过")
            continue
        parts.append(normalize_one(df, kind))
    if not parts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    merged = pd.concat(parts, ignore_index=True)
    # 去重 (根据 key)
    merged = merged.drop_duplicates(subset=["key"]).reset_index(drop=True)
    return merged


def aggregate_labeled_inputs(label2path: Dict[str, Union[str, Path]]) -> pd.DataFrame:
    """显式指定 {输入类型: 文件路径}, 不依赖自动识别."""
    parts: List[pd.DataFrame] = []
    for kind, path in label2path.items():
        if kind not in KIND_NORMALIZER:
            raise ValueError(f"未知输入类型: {kind}")
        df = _read_with_offset(path)
        parts.append(normalize_one(df, kind))
    if not parts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(subset=["key"]).reset_index(drop=True)
    return merged
