"""中文文本特征 (jieba 分词 + 字符 ngram + 领域 rule 特征)."""
from __future__ import annotations

import re
from typing import Iterable, List

import jieba
import jieba.posseg  # noqa: F401  (preload)
import numpy as np
import scipy.sparse as sp
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion

# 关闭 jieba 的初始化日志
jieba.setLogLevel("ERROR")


_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\u3000", " ")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def tokenize(text: str) -> List[str]:
    """jieba 切词 + 过滤空白. 保留单字, 不去停用词 (中文短文本信息密度高)."""
    text = normalize_text(text)
    if not text:
        return []
    return [t for t in jieba.lcut(text, cut_all=False) if t.strip()]


class JiebaTokenizer:
    """可序列化的 jieba 分词器 (lambda 不能 pickle)."""
    def __call__(self, text: str) -> List[str]:
        return tokenize(text)


def make_word_vectorizer(min_df: int = 2, ngram_range=(1, 2),
                         max_features: int = 60000) -> TfidfVectorizer:
    return TfidfVectorizer(
        tokenizer=JiebaTokenizer(),
        token_pattern=None,
        lowercase=False,
        ngram_range=ngram_range,
        min_df=min_df,
        max_features=max_features,
        sublinear_tf=True,
    )


def make_char_vectorizer(min_df: int = 2, ngram_range=(1, 5),
                         max_features: int = 120000) -> TfidfVectorizer:
    return TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=ngram_range,
        min_df=min_df,
        max_features=max_features,
        sublinear_tf=True,
    )


def make_combined_vectorizer() -> FeatureUnion:
    return FeatureUnion([
        ("word", make_word_vectorizer()),
        ("char", make_char_vectorizer()),
    ])


class RuleFeaturizer(BaseEstimator, TransformerMixin):
    """把领域规则匹配 0/1 作为额外稀疏特征喂给模型.

    使用 schema 中的隐患类型规则 + 子类规则关键词命中标记.
    """
    def __init__(self):
        self._patterns: List[tuple] = []  # [(name, compiled), ...]
        self._compiled = False

    def _compile(self):
        from .rules import STRONG_KEYWORDS, SUBCLASS_KEYWORDS
        pats = []
        for label, kws in STRONG_KEYWORDS.items():
            for kw in kws:
                pats.append((f"haz::{label}::{kw}", re.compile(kw)))
        for haz, subs in SUBCLASS_KEYWORDS.items():
            for sub, kws in subs.items():
                for kw in kws:
                    pats.append((f"sub::{haz}::{sub}::{kw}", re.compile(kw)))
        self._patterns = pats
        self._compiled = True

    def fit(self, X, y=None):
        if not self._compiled:
            self._compile()
        return self

    def transform(self, X):
        if not self._compiled:
            self._compile()
        rows, cols, data = [], [], []
        for i, t in enumerate(X):
            text = t if isinstance(t, str) else (str(t) if t is not None else "")
            for j, (_, pat) in enumerate(self._patterns):
                if pat.search(text):
                    rows.append(i); cols.append(j); data.append(1.0)
        n_features = len(self._patterns)
        return sp.csr_matrix((data, (rows, cols)), shape=(len(X), n_features))


def make_rich_vectorizer() -> FeatureUnion:
    """词 + 字符 + 规则 三路特征拼接, 用于更强模型."""
    return FeatureUnion([
        ("word", make_word_vectorizer()),
        ("char", make_char_vectorizer()),
        ("rules", RuleFeaturizer()),
    ])
