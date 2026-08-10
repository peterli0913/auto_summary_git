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
from typing import List, Optional, Sequence

import numpy as np
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


def build_haystack(series: pd.Series, case_sensitive: bool = False) -> np.ndarray:
    """把待搜索的文本列预处理成 numpy 对象数组.

    用 Python 原生 `in` 而不是 pandas .str.contains / pyarrow match_substring:
    这些短文本上实测原生 `in` 快 3 倍左右 (68k 行: 10.5ms -> 3.7ms).
    预处理结果可以复用, 避免每次查询都重新 lower 一遍.
    """
    text = series.astype("string").fillna("")
    if not case_sensitive:
        text = text.str.lower()
    return text.to_numpy(dtype=object)


def mask_from_haystack(haystack: np.ndarray, expression: Expression,
                       case_sensitive: bool = False) -> np.ndarray:
    """在预处理好的数组上求布尔掩码.

    两处剪枝:
      * 与(&): 每多一个关键词就把候选集收窄一次, 后面的关键词只在
        前面已命中的行里找 —— 关键词越多越快
      * 或(|): 已经命中的行不再参与后续或组的匹配
    """
    n = len(haystack)
    total = np.zeros(n, dtype=bool)
    if n == 0:
        return total

    for group in expression.or_groups:
        candidates = np.nonzero(~total)[0]
        if candidates.size == 0:
            break
        for term in group:
            needle = term if case_sensitive else term.lower()
            subset = haystack[candidates]
            keep = np.fromiter((needle in s for s in subset),
                               dtype=bool, count=len(subset))
            candidates = candidates[keep]
            if candidates.size == 0:
                break
        total[candidates] = True
    return total


def build_mask(series: pd.Series, expression: Expression,
               case_sensitive: bool = False) -> pd.Series:
    """对文本列生成布尔掩码 (内部走 build_haystack + mask_from_haystack)."""
    haystack = build_haystack(series, case_sensitive=case_sensitive)
    mask = mask_from_haystack(haystack, expression, case_sensitive=case_sensitive)
    return pd.Series(mask, index=series.index)


def _resolve_haystack(df: pd.DataFrame, content_col: str, case_sensitive: bool,
                      haystack: Optional[np.ndarray]) -> np.ndarray:
    if haystack is not None:
        return haystack
    return build_haystack(df[content_col], case_sensitive=case_sensitive)


def filter_dataframe(df: pd.DataFrame, expr: str,
                     content_col: str = CONTENT_COL,
                     case_sensitive: bool = False,
                     haystack: Optional[np.ndarray] = None,
                     ) -> tuple[pd.DataFrame, Expression]:
    """按表达式筛选. 返回 (命中的行, 解析后的表达式).

    haystack 可以传入 build_haystack 的结果复用, 省掉重复预处理.
    """
    expression = parse(expr)
    if df is None or len(df) == 0:
        return df.iloc[0:0] if df is not None else pd.DataFrame(), expression
    hay = _resolve_haystack(df, content_col, case_sensitive, haystack)
    mask = mask_from_haystack(hay, expression, case_sensitive=case_sensitive)
    return df.loc[mask].copy(), expression


def count_matches(df: pd.DataFrame, expr: str,
                  content_col: str = CONTENT_COL,
                  case_sensitive: bool = False,
                  haystack: Optional[np.ndarray] = None) -> int:
    """只要个数 (统计功能用). 表达式非法时返回 0."""
    try:
        expression = parse(expr)
    except EmptyExpression:
        return 0
    if df is None or len(df) == 0:
        return 0
    hay = _resolve_haystack(df, content_col, case_sensitive, haystack)
    return int(mask_from_haystack(hay, expression,
                                  case_sensitive=case_sensitive).sum())


def count_many(df: pd.DataFrame, exprs: Sequence[str],
               content_col: str = CONTENT_COL,
               case_sensitive: bool = False,
               haystack: Optional[np.ndarray] = None) -> pd.DataFrame:
    """批量统计多个关键词/表达式的命中数, 返回 关键词/条目数/占比 表."""
    total = 0 if df is None else len(df)
    hay = (_resolve_haystack(df, content_col, case_sensitive, haystack)
           if total else None)
    rows = []
    for e in exprs:
        n = (count_matches(df, e, content_col=content_col,
                           case_sensitive=case_sensitive, haystack=hay)
             if total else 0)
        rows.append({
            "关键词": e,
            "条目数": n,
            "占比": (n / total) if total else 0.0,
        })
    return pd.DataFrame(rows, columns=["关键词", "条目数", "占比"])
