"""中文文本特征 (jieba 分词 + 字符 ngram)."""
from __future__ import annotations

import re
from typing import Iterable, List

import jieba
import jieba.posseg  # noqa: F401  (preload)
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
