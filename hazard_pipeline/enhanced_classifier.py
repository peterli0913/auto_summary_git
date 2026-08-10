"""加强模型: TF-IDF (词 + 字符 ngram) + sentence-transformer 中文 embedding 拼接.

设计:
- TF-IDF (jieba 词 + char ngram 1-5) -> 稀疏特征 (~10 万维)
- SBERT (paraphrase-multilingual-MiniLM-L12-v2) -> 384 维稠密
- 拼接为 sparse + dense 混合输入 (用 scipy.sparse.hstack)
- SVC / LR 在混合特征上训练

混合模型的优点:
- TF-IDF 提供 领域术语 (如 "静电跨接断裂" 这种工业用词) 的精确判别
- SBERT 提供 语义级别 的相似度 (能识别 释义 / 同义词)
- 两者互补, 通常单独都更强

接口与 classifier.py 中的 HazardClassifier / SubClassifier / FullClassifier
基本一致, 可直接替换. 通过 FullClassifier.load() 时根据 meta 自动判断变体.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
import scipy.sparse as sp
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC

from .rules import rule_predict_hazard, rule_predict_subclass
from .schema import HAZARD_TYPES, OTHER_LABEL, POSITIVE_TYPES
from .text_features import make_combined_vectorizer, normalize_text


_DEFAULT_SBERT = "paraphrase-multilingual-MiniLM-L12-v2"
_SBERT_INSTANCES: Dict[str, "object"] = {}


def get_sbert(name: str = _DEFAULT_SBERT):
    """惰性加载 + 复用 (避免在多个分类器实例中重复加载)."""
    if name not in _SBERT_INSTANCES:
        from sentence_transformers import SentenceTransformer
        _SBERT_INSTANCES[name] = SentenceTransformer(name)
    return _SBERT_INSTANCES[name]


def encode_texts(texts: List[str], model_name: str = _DEFAULT_SBERT,
                  batch_size: int = 64, show_progress: bool = False) -> np.ndarray:
    model = get_sbert(model_name)
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    return model.encode(
        [normalize_text(t) for t in texts],
        batch_size=batch_size, show_progress_bar=show_progress,
        convert_to_numpy=True, normalize_embeddings=True,
    )


def _hstack_sparse_dense(X_sparse: sp.spmatrix, X_dense: np.ndarray) -> sp.csr_matrix:
    return sp.hstack([X_sparse, sp.csr_matrix(X_dense)], format="csr")


# ---------- 主分类器 ----------

@dataclass
class EnhancedHazardClassifier:
    other_threshold: float = 0.5
    is_other_clf: object = None       # CalibratedClassifierCV
    positive_clf: object = None       # LogisticRegression
    classes_positive_: List[str] = field(default_factory=list)
    threshold_modes: Dict[str, Dict[str, float]] = field(default_factory=dict)
    default_mode: str = "balanced"
    sbert_name: str = _DEFAULT_SBERT
    tfidf_vec: object = None          # combined TF-IDF FeatureUnion (拟合后)

    def _build_features(self, texts: List[str], fit: bool = False) -> sp.csr_matrix:
        """TF-IDF (sparse) hstack SBERT (dense) -> 混合稀疏矩阵."""
        if fit:
            self.tfidf_vec = make_combined_vectorizer()
            X_sparse = self.tfidf_vec.fit_transform(texts)
        else:
            X_sparse = self.tfidf_vec.transform(texts)
        X_dense = encode_texts(texts, self.sbert_name)
        return _hstack_sparse_dense(X_sparse, X_dense)

    def fit(self, texts: List[str], labels: List[str]) -> "EnhancedHazardClassifier":
        texts = [normalize_text(t) for t in texts]
        labels = list(labels)
        X = self._build_features(texts, fit=True)

        # 第一级: 其他 vs 非其他
        y_is_other = np.array([1 if l == OTHER_LABEL else 0 for l in labels])
        base = LinearSVC(C=1.0, max_iter=8000)
        self.is_other_clf = CalibratedClassifierCV(base, method="sigmoid", cv=3)
        self.is_other_clf.fit(X, y_is_other)

        # 第二级: 正类 3 类 (用 LR 配合 balanced)
        pos_mask = np.array([l in POSITIVE_TYPES for l in labels])
        if pos_mask.sum():
            X_pos = X[pos_mask]
            y_pos = [l for l, m in zip(labels, pos_mask) if m]
            self.positive_clf = LogisticRegression(
                max_iter=4000, C=4.0, class_weight="balanced", solver="lbfgs",
            ).fit(X_pos, y_pos)
            self.classes_positive_ = list(self.positive_clf.classes_)
        return self

    def _features(self, texts: List[str]) -> sp.csr_matrix:
        texts = [normalize_text(t) for t in texts]
        return self._build_features(texts, fit=False)

    def predict_proba_other(self, texts: List[str]) -> np.ndarray:
        X = self._features(texts)
        proba = self.is_other_clf.predict_proba(X)
        idx = list(self.is_other_clf.classes_).index(1)
        return proba[:, idx]

    def predict_proba_positive(self, texts: List[str]) -> Tuple[np.ndarray, List[str]]:
        X = self._features(texts)
        return self.positive_clf.predict_proba(X), list(self.positive_clf.classes_)

    def predict(self, texts: List[str], use_rules: bool = True
                ) -> Tuple[List[str], np.ndarray, np.ndarray, List[str], List[bool], List[str]]:
        if not texts:
            return [], np.array([]), np.empty((0, 0)), self.classes_positive_, [], []
        X = self._features(texts)
        proba_other = self.is_other_clf.predict_proba(X)
        idx_other = list(self.is_other_clf.classes_).index(1)
        p_other = proba_other[:, idx_other]
        if self.positive_clf is not None:
            pos_proba = self.positive_clf.predict_proba(X)
            pos_classes = list(self.positive_clf.classes_)
        else:
            pos_proba = np.zeros((len(texts), 0))
            pos_classes = []

        preds: List[str] = []
        rule_overridden: List[bool] = []
        model_only_preds: List[str] = []
        for i, t in enumerate(texts):
            if p_other[i] >= self.other_threshold:
                model_pred = OTHER_LABEL
            else:
                model_pred = pos_classes[int(np.argmax(pos_proba[i]))] if pos_classes else OTHER_LABEL
            model_only_preds.append(model_pred)

            final_pred = model_pred
            if use_rules:
                rule = rule_predict_hazard(t)
                if rule == OTHER_LABEL:
                    final_pred = OTHER_LABEL
                elif rule in POSITIVE_TYPES and model_pred == OTHER_LABEL and p_other[i] < 0.90:
                    final_pred = rule
            preds.append(final_pred)
            rule_overridden.append(final_pred != model_pred)
        return preds, p_other, pos_proba, pos_classes, rule_overridden, model_only_preds

    def set_mode(self, mode: str):
        if mode in self.threshold_modes:
            self.other_threshold = float(self.threshold_modes[mode]["threshold"])

    def save(self, path: Path):
        path = Path(path); path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.is_other_clf, path / "is_other.joblib")
        if self.positive_clf is not None:
            joblib.dump(self.positive_clf, path / "positive.joblib")
        joblib.dump(self.tfidf_vec, path / "tfidf_vec.joblib")
        meta = {
            "other_threshold": self.other_threshold,
            "classes_positive_": self.classes_positive_,
            "threshold_modes": self.threshold_modes,
            "default_mode": self.default_mode,
            "sbert_name": self.sbert_name,
            "variant": "enhanced",
        }
        (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "EnhancedHazardClassifier":
        path = Path(path)
        meta = json.loads((path / "meta.json").read_text())
        obj = cls(other_threshold=meta["other_threshold"],
                  sbert_name=meta.get("sbert_name", _DEFAULT_SBERT))
        obj.is_other_clf = joblib.load(path / "is_other.joblib")
        pos_path = path / "positive.joblib"
        obj.positive_clf = joblib.load(pos_path) if pos_path.exists() else None
        obj.tfidf_vec = joblib.load(path / "tfidf_vec.joblib")
        obj.classes_positive_ = meta.get("classes_positive_", [])
        obj.threshold_modes = meta.get("threshold_modes", {})
        obj.default_mode = meta.get("default_mode", "balanced")
        return obj


# ---------- 子分类器 (复用 SBERT embedding) ----------

@dataclass
class EnhancedSubClassifier:
    clfs: Dict[str, object] = field(default_factory=dict)
    classes_: Dict[str, List[str]] = field(default_factory=dict)
    fallback: Dict[str, str] = field(default_factory=dict)
    sbert_name: str = _DEFAULT_SBERT
    tfidf_vec: object = None

    def _build_features(self, texts: List[str], fit: bool = False) -> sp.csr_matrix:
        if fit:
            self.tfidf_vec = make_combined_vectorizer()
            X_sparse = self.tfidf_vec.fit_transform(texts)
        else:
            X_sparse = self.tfidf_vec.transform(texts)
        X_dense = encode_texts(texts, self.sbert_name)
        return _hstack_sparse_dense(X_sparse, X_dense)

    def fit(self, texts: List[str], hazards: List[str], subs: List[str]) -> "EnhancedSubClassifier":
        texts = [normalize_text(t) for t in texts]
        X_all = self._build_features(texts, fit=True)
        for hz in POSITIVE_TYPES:
            mask = np.array([h == hz for h in hazards])
            if mask.sum() == 0:
                continue
            ys = [s if (s and not pd.isna(s)) else "其他"
                  for s, m in zip(subs, mask) if m]
            unique = list(dict.fromkeys(ys))
            if len(unique) <= 1:
                self.fallback[hz] = unique[0] if unique else "其他"
                continue
            self.fallback[hz] = pd.Series(ys).value_counts().index[0]
            # 使用 indices 索引稀疏矩阵
            indices = np.where(mask)[0]
            X_h = X_all[indices]
            clf = LogisticRegression(
                max_iter=4000, C=4.0, class_weight="balanced", solver="lbfgs",
            ).fit(X_h, ys)
            self.clfs[hz] = clf
            self.classes_[hz] = list(clf.classes_)
        return self

    def predict(self, texts: List[str], hazards: List[str], use_rules: bool = True
                ) -> Tuple[List[str], List[Optional[np.ndarray]], List[List[str]],
                            List[bool], List[str]]:
        out: List[str] = []
        proba_list: List[Optional[np.ndarray]] = []
        cls_list: List[List[str]] = []
        rule_overridden: List[bool] = []
        model_only_preds: List[str] = []
        normed = [normalize_text(t) for t in texts]
        X_all = self._build_features(normed, fit=False)

        for i, (t, hz) in enumerate(zip(normed, hazards)):
            if hz == OTHER_LABEL:
                out.append("其他"); proba_list.append(None)
                cls_list.append(["其他"])
                rule_overridden.append(False); model_only_preds.append("其他")
                continue
            clf = self.clfs.get(hz)
            classes = self.classes_.get(hz, [])
            model_pred = self.fallback.get(hz, "其他")
            proba = None
            if clf is not None:
                proba = clf.predict_proba(X_all[i:i+1])[0]
                model_pred = classes[int(np.argmax(proba))]
            model_only_preds.append(model_pred)
            rule_sub = rule_predict_subclass(hz, t) if use_rules else None
            if rule_sub:
                out.append(rule_sub)
                proba_list.append(proba)
                cls_list.append(classes if classes else [rule_sub])
                rule_overridden.append(rule_sub != model_pred)
            else:
                out.append(model_pred)
                proba_list.append(proba)
                cls_list.append(classes if classes else [model_pred])
                rule_overridden.append(False)
        return out, proba_list, cls_list, rule_overridden, model_only_preds

    def save(self, path: Path):
        path = Path(path); path.mkdir(parents=True, exist_ok=True)
        for hz, clf in self.clfs.items():
            joblib.dump(clf, path / f"sub_{hz}.joblib")
        joblib.dump(self.tfidf_vec, path / "sub_tfidf_vec.joblib")
        meta = {"classes_": self.classes_, "fallback": self.fallback,
                "hazards": list(self.clfs.keys()),
                "sbert_name": self.sbert_name}
        (path / "sub_meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "EnhancedSubClassifier":
        path = Path(path)
        meta = json.loads((path / "sub_meta.json").read_text())
        obj = cls(classes_=meta.get("classes_", {}), fallback=meta.get("fallback", {}),
                  sbert_name=meta.get("sbert_name", _DEFAULT_SBERT))
        obj.tfidf_vec = joblib.load(path / "sub_tfidf_vec.joblib")
        for hz in meta.get("hazards", []):
            obj.clfs[hz] = joblib.load(path / f"sub_{hz}.joblib")
        return obj


@dataclass
class EnhancedFullClassifier:
    hazard: EnhancedHazardClassifier
    sub: EnhancedSubClassifier

    def predict(self, texts: List[str], use_rules: bool = True):
        haz, p_other, pos_proba, pos_classes, haz_rule_over, haz_model_only = \
            self.hazard.predict(texts, use_rules=use_rules)
        sub, sub_proba, sub_classes, sub_rule_over, sub_model_only = \
            self.sub.predict(texts, haz, use_rules=use_rules)
        return {
            "隐患类型": haz, "分类": sub,
            "p_other": p_other, "pos_proba": pos_proba, "pos_classes": pos_classes,
            "sub_proba": sub_proba, "sub_classes": sub_classes,
            "haz_rule_overridden": haz_rule_over,
            "sub_rule_overridden": sub_rule_over,
            "haz_model_only": haz_model_only,
            "sub_model_only": sub_model_only,
        }

    def save(self, path: Path):
        path = Path(path); path.mkdir(parents=True, exist_ok=True)
        self.hazard.save(path / "hazard")
        self.sub.save(path / "sub")
        (path / "variant.txt").write_text("enhanced")

    @classmethod
    def load(cls, path: Path) -> "EnhancedFullClassifier":
        path = Path(path)
        return cls(
            hazard=EnhancedHazardClassifier.load(path / "hazard"),
            sub=EnhancedSubClassifier.load(path / "sub"),
        )
