"""关键词表达式解析与筛选.

语法 (只有两个运算符, 与需求一致):
    A          -> 巡查发现 里包含 A
    A&B        -> 同时包含 A 和 B
    A|B        -> 包含 A 或 B 之一
    A&B&C      -> 同时包含 A、B、C
    A|B|C      -> 三者之一
    A&B|C      -> & 优先于 | , 等价于 (A&B) | C

实现方式: 先按 | 拆成若干"或组", 每组再按 & 拆成"与项",
即析取范式 (DNF). 全角 ＆ ｜ 也识别.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import pandas as pd

from .schema import CONTENT_COL

#: 全角/别名 -> 标准运算符
_NORMALIZE = {
    "＆": "&",
    "｜": "|",
    "∥": "|",
    "＋": "&",
}


class EmptyExpression(ValueError):
    """表达式里没有任何有效关键词."""


@dataclass(frozen=True)
class Expression:
    """解析后的表达式 (或组的列表, 每个或组是一串必须同时命中的关键词)."""
    or_groups: List[List[str]]
    raw: str

    def describe(self) -> str:
        """给界面看的自然语言解释."""
        parts = []
        for group in self.or_groups:
            if len(group) == 1:
                parts.append(f"包含「{group[0]}」")
            else:
                parts.append("同时包含 " + " 且 ".join(f"「{t}」" for t in group))
        if len(parts) == 1:
            return parts[0]
        return "满足以下任一条件: " + "；或 ".join(parts)

    @property
    def terms(self) -> List[str]:
        """去重后的全部关键词 (用于高亮)."""
        seen: list[str] = []
        for group in self.or_groups:
            for t in group:
                if t not in seen:
                    seen.append(t)
        return seen


def normalize(expr: str) -> str:
    text = str(expr or "")
    for src, dst in _NORMALIZE.items():
        text = text.replace(src, dst)
    return text.strip()


def parse(expr: str) -> Expression:
    """把表达式串解析成 DNF. 空表达式抛 EmptyExpression."""
    text = normalize(expr)
    if not text:
        raise EmptyExpression("请输入至少一个关键词")

    or_groups: List[List[str]] = []
    for chunk in text.split("|"):
        terms = [t.strip() for t in chunk.split("&")]
        terms = [t for t in terms if t]
        if terms:
            or_groups.append(terms)

    if not or_groups:
        raise EmptyExpression("请输入至少一个关键词")
    return Expression(or_groups=or_groups, raw=text)


def build_mask(series: pd.Series, expression: Expression,
               case_sensitive: bool = False) -> pd.Series:
    """对文本列生成布尔掩码."""
    text = series.astype("string").fillna("")
    if not case_sensitive:
        haystack = text.str.lower()
    else:
        haystack = text

    total = pd.Series(False, index=series.index)
    for group in expression.or_groups:
        group_mask = pd.Series(True, index=series.index)
        for term in group:
            needle = term if case_sensitive else term.lower()
            group_mask &= haystack.str.contains(needle, regex=False, na=False)
        total |= group_mask
    return total


def filter_dataframe(df: pd.DataFrame, expr: str,
                     content_col: str = CONTENT_COL,
                     case_sensitive: bool = False) -> tuple[pd.DataFrame, Expression]:
    """按表达式筛选. 返回 (命中的行, 解析后的表达式)."""
    expression = parse(expr)
    if df is None or len(df) == 0:
        return df.iloc[0:0] if df is not None else pd.DataFrame(), expression
    mask = build_mask(df[content_col], expression, case_sensitive=case_sensitive)
    return df[mask].copy(), expression


def count_matches(df: pd.DataFrame, expr: str,
                  content_col: str = CONTENT_COL,
                  case_sensitive: bool = False) -> int:
    """只要个数 (统计功能用). 表达式非法时返回 0."""
    try:
        expression = parse(expr)
    except EmptyExpression:
        return 0
    if df is None or len(df) == 0:
        return 0
    return int(build_mask(df[content_col], expression,
                          case_sensitive=case_sensitive).sum())


def count_many(df: pd.DataFrame, exprs: Sequence[str],
               content_col: str = CONTENT_COL,
               case_sensitive: bool = False) -> pd.DataFrame:
    """批量统计多个关键词/表达式的命中数, 返回 关键词/条目数/占比 表."""
    total = 0 if df is None else len(df)
    rows = []
    for e in exprs:
        n = count_matches(df, e, content_col=content_col,
                          case_sensitive=case_sensitive)
        rows.append({
            "关键词": e,
            "条目数": n,
            "占比": (n / total) if total else 0.0,
        })
    out = pd.DataFrame(rows, columns=["关键词", "条目数", "占比"])
    return out
