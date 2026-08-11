"""统一数据库字段定义 + 各类巡查台账的列名映射.

目标数据库只有 3 个主字段:
    序号 / 日期 / 巡查发现

不同来源台账里 "巡查发现" 的实际列名不同, 按文件名后缀区分:
    ...日常隐患排查...    -> 巡查发现
    ...高管驻场/高管驻厂...-> 巡查结果
    ...值班巡查...        -> 发现项
    ...全体人员巡查/生产人员巡查... -> 问题描述

为了让工具在未来的新台账上也能工作, 除了按文件名匹配, 还会:
  1. 按 sheet 名匹配 (隐患排查0 / 巡查信息0 / 统一日常值班报告0 / 每日巡查报告0)
  2. 按列名直接匹配候选集合
  3. 兜底: 选文本最长的那一列作为 "巡查发现"
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ---------- 输出数据库字段 ----------
SEQ_COL = "序号"
DATE_COL = "日期"
PLANT_COL = "厂区"
CONTENT_COL = "巡查发现"

#: 导出默认包含的主字段
CORE_COLUMNS: List[str] = [SEQ_COL, DATE_COL, PLANT_COL, CONTENT_COL]

#: 内部额外保留的溯源字段 (导出时可选勾选)
SOURCE_FILE_COL = "来源文件"
KIND_COL = "台账类型"
EXTRA_COLUMNS: List[str] = [SOURCE_FILE_COL, KIND_COL]

ALL_COLUMNS: List[str] = CORE_COLUMNS + EXTRA_COLUMNS


# ---------- 台账类型定义 ----------
# kind -> (文件名关键字, sheet 名关键字, 日期列候选, 巡查发现列候选)
class Kind:
    HAZARD = "日常隐患排查"
    EXEC = "高管驻场"
    DUTY = "值班巡查"
    STAFF = "全体人员巡查"
    UNKNOWN = "未识别"


KIND_SPECS: Dict[str, dict] = {
    Kind.HAZARD: {
        "filename_keys": ["日常隐患排查", "隐患排查"],
        "sheet_keys": ["隐患排查"],
        "date_cols": ["巡查日期"],
        "content_cols": ["巡查发现"],
    },
    Kind.EXEC: {
        # 用户描述写作 "高管驻场", 真实文件名是 "高管驻厂"; 两种都认
        "filename_keys": ["高管驻场", "高管驻厂", "巡查信息"],
        "sheet_keys": ["巡查信息"],
        "date_cols": ["日期"],
        "content_cols": ["巡查结果"],
    },
    Kind.DUTY: {
        "filename_keys": ["值班巡查", "统一日常值班报告", "日常值班"],
        "sheet_keys": ["统一日常值班报告", "值班报告"],
        "date_cols": ["值班日期", "日期"],
        "content_cols": ["发现项"],
    },
    Kind.STAFF: {
        "filename_keys": ["全体人员巡查", "生产人员巡查", "每日巡查报告", "全体--生产人员巡查"],
        "sheet_keys": ["每日巡查报告"],
        "date_cols": ["时间", "日期"],
        "content_cols": ["问题描述"],
    },
}

#: 所有已知的 "巡查发现" 列名 (按优先级; 用于按列名反查类型)
KNOWN_CONTENT_COLUMNS: List[str] = [
    "巡查发现", "巡查结果", "发现项", "问题描述",
    # 兼容其它可能的叫法
    "隐患描述", "发现问题", "问题", "检查发现", "描述",
]

#: 所有已知的日期列名 (按优先级). 注意排除 创建时间/修改时间 —
#: 那是记录写入时间而不是巡查时间.
KNOWN_DATE_COLUMNS: List[str] = [
    "巡查日期", "值班日期", "检查日期", "发现日期", "日期", "时间",
]

#: 明确不作为巡查日期使用的列
DATE_COLUMN_BLOCKLIST: List[str] = [
    "创建时间", "修改时间", "复核日期", "整改时限", "反馈时间", "更新时间",
]


# ---------- 厂区 ----------
#: 认可的厂区代号
PLANT_CODES: List[str] = [
    "TJ1", "TJ2", "TJ3", "TJ4", "TJ6",
    "FX1", "FX2",
    "DH1", "DH2", "DH3",
    "SH1", "SH2", "SH3", "SH4",
]

#: 没能归入上面任何一个代号时的标记 (不丢数据, 界面里也能筛)
UNKNOWN_PLANT = "未识别"

#: 厂区列候选. 按用户说明, 日常隐患排查以「整改厂区」为准;
#: 其它台账用「厂区」. 组织厂区 只作为最后兜底.
PLANT_COLUMN_PRIORITY: List[str] = ["整改厂区", "厂区", "组织厂区", "所属厂区"]

# 代号后面不能紧跟数字, 否则 TJ10_CSBT 会被误当成 TJ1;
# 后缀如 TJ4-CMMD / TJ4CMMD / TJ4_CSBT 都会归到 TJ4.
_PLANT_RE = re.compile("(" + "|".join(PLANT_CODES) + r")(?!\d)", re.IGNORECASE)


def normalize_plant(value: object) -> Optional[str]:
    """从任意文本里提取厂区代号; 取最先出现的那个, 保证一条数据只对应一个厂区."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _PLANT_RE.search(text)
    return m.group(1).upper() if m else None


def plant_from_filename(file_name: str) -> Optional[str]:
    """文件名里出现厂区代号 -> 整个文件都算这个厂区."""
    return normalize_plant(file_name)


def detect_kind(file_name: str = "", sheet_name: str = "",
                columns: List[str] | None = None) -> str:
    """依次用 文件名 -> sheet 名 -> 列名 判定台账类型."""
    columns = [str(c).strip() for c in (columns or [])]

    for kind, spec in KIND_SPECS.items():
        if any(k in file_name for k in spec["filename_keys"]):
            return kind
    for kind, spec in KIND_SPECS.items():
        if any(k in sheet_name for k in spec["sheet_keys"]):
            return kind
    # 按 "巡查发现" 列名反查
    for kind, spec in KIND_SPECS.items():
        if any(c in columns for c in spec["content_cols"]):
            return kind
    return Kind.UNKNOWN


def content_candidates(kind: str) -> List[str]:
    """给定类型, 返回 巡查发现 列的候选顺序 (类型专属优先, 再退到通用)."""
    preferred = KIND_SPECS.get(kind, {}).get("content_cols", [])
    rest = [c for c in KNOWN_CONTENT_COLUMNS if c not in preferred]
    return preferred + rest


def date_candidates(kind: str) -> List[str]:
    """给定类型, 返回 日期 列的候选顺序."""
    preferred = KIND_SPECS.get(kind, {}).get("date_cols", [])
    rest = [c for c in KNOWN_DATE_COLUMNS if c not in preferred]
    return preferred + rest
