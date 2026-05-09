"""两级隐患分类器 + 细分类器.

结构:
  HazardClassifier: 隐患类型 (4 类). 内部两级:
    1) is_other_clf  : 二分类 (其他 vs 非其他), 输出概率, 阈值可调
    2) positive_clf  : 三分类 (跑冒滴漏/静电事件/化学品暴露)
  规则后处理 (rules.py) 在 predict 时兜底, 用于满足非对称错误约束.

  SubClassifier: 在每个隐患类型下做细分类 (51 种).
    - 训练时按 隐患类型 分组, 每组单独训练
    - 推理时根据预测的 隐患类型 走对应分支
    - "其他" 类直接置 "其他"

模型主体使用 TF-IDF (词 + 字 ngram) + Logistic Regression.
- LR 输出可校准概率, 便于设阈值 + Top-K 互动核对.
- 支持 incremental retrain (重训, 数据量小, 不需要 SGD).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from .rules import (
    PER_TYPE_PRIORITY,
    rule_predict_hazard,
    rule_predict_subclass,
)
from .schema import HAZARD_TYPES, OTHER_LABEL, POSITIVE_TYPES
from .text_features import make_combined_vectorizer, normalize_text


# ---------- 主分类器 ----------


@dataclass
class HazardClassifier:
    other_threshold: float = 0.5     # is_other 的判定阈值 (高于此判 其他)
    is_other_pipe: Optional[Pipeline] = None
    positive_pipe: Optional[Pipeline] = None
    classes_positive_: List[str] = field(default_factory=list)
    threshold_modes: Dict[str, Dict[str, float]] = field(default_factory=dict)
    default_mode: str = "balanced"

    def fit(self, texts: List[str], labels: List[str]) -> "HazardClassifier":
        texts = [normalize_text(t) for t in texts]
        labels = list(labels)

        # ---- 第一级: 其他 vs 非其他 ----
        # 使用 LinearSVC + sigmoid 校准, 比 LR 更好的边界 + 校准概率.
        # 不使用 balanced (会让正类过度被预测, 牺牲对其他的识别).
        y_is_other = np.array([1 if l == OTHER_LABEL else 0 for l in labels])
        base = LinearSVC(C=1.0, max_iter=4000)
        self.is_other_pipe = Pipeline([
            ("feat", make_combined_vectorizer()),
            ("clf", CalibratedClassifierCV(base, method="sigmoid", cv=3)),
        ])
        self.is_other_pipe.fit(texts, y_is_other)

        # ---- 第二级: 三分类 (仅在正类样本上训练) ----
        pos_mask = np.array([l in POSITIVE_TYPES for l in labels])
        pos_texts = [t for t, m in zip(texts, pos_mask) if m]
        pos_labels = [l for l, m in zip(labels, pos_mask) if m]
        if pos_texts:
            self.positive_pipe = Pipeline([
                ("feat", make_combined_vectorizer()),
                ("clf", LogisticRegression(
                    max_iter=4000,
                    C=4.0,
                    class_weight="balanced",
                    solver="lbfgs",
                )),
            ])
            self.positive_pipe.fit(pos_texts, pos_labels)
            self.classes_positive_ = list(self.positive_pipe.named_steps["clf"].classes_)
        return self

    # ------- 推理 -------
    def predict_proba_other(self, texts: List[str]) -> np.ndarray:
        """返回 P(其他) 数组."""
        assert self.is_other_pipe is not None
        proba = self.is_other_pipe.predict_proba([normalize_text(t) for t in texts])
        clf = self.is_other_pipe.named_steps["clf"]
        idx = list(clf.classes_).index(1)
        return proba[:, idx]

    def predict_proba_positive(self, texts: List[str]) -> Tuple[np.ndarray, List[str]]:
        """在正类三分类下返回概率矩阵和类别名."""
        assert self.positive_pipe is not None
        proba = self.positive_pipe.predict_proba([normalize_text(t) for t in texts])
        return proba, list(self.positive_pipe.named_steps["clf"].classes_)

    def predict(self, texts: List[str], use_rules: bool = True
                ) -> Tuple[List[str], np.ndarray, np.ndarray, List[str]]:
        """返回 (隐患类型预测, P(其他), P(正类三分类), 正类类别名).

        规则后处理:
          - 强关键词命中正类 -> 强制改为该正类 (满足"正类→其他<1%")
          - 强关键词命中 其他 (且未命中正类) -> 强制为 其他 (帮助降低 FP)
        """
        if not texts:
            return [], np.array([]), np.empty((0, 3)), self.classes_positive_

        normed = [normalize_text(t) for t in texts]
        p_other = self.predict_proba_other(normed)
        if self.positive_pipe is not None:
            pos_proba, pos_classes = self.predict_proba_positive(normed)
        else:
            pos_proba = np.zeros((len(normed), 0))
            pos_classes = []

        preds: List[str] = []
        for i, t in enumerate(normed):
            rule = rule_predict_hazard(t) if use_rules else None
            # 模型主决策
            if p_other[i] >= self.other_threshold:
                model_pred = OTHER_LABEL
            else:
                model_pred = pos_classes[int(np.argmax(pos_proba[i]))] if pos_classes else OTHER_LABEL

            # 规则后处理:
            #  - 规则其他: 直接强制 (在训练集上对正类零误伤)
            #  - 规则正类: 只在模型置信度不高时 override (避免规则在明显其他场景误报)
            if use_rules and rule:
                if rule == OTHER_LABEL:
                    model_pred = OTHER_LABEL
                elif rule in POSITIVE_TYPES and model_pred == OTHER_LABEL and p_other[i] < 0.90:
                    model_pred = rule
            preds.append(model_pred)
        return preds, p_other, pos_proba, pos_classes

    # ------- 序列化 -------
    def save(self, path: Path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.is_other_pipe, path / "is_other.joblib")
        if self.positive_pipe is not None:
            joblib.dump(self.positive_pipe, path / "positive.joblib")
        meta = {
            "other_threshold": self.other_threshold,
            "classes_positive_": self.classes_positive_,
            "threshold_modes": self.threshold_modes,
            "default_mode": self.default_mode,
        }
        (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "HazardClassifier":
        path = Path(path)
        meta = json.loads((path / "meta.json").read_text())
        obj = cls(other_threshold=meta["other_threshold"])
        obj.is_other_pipe = joblib.load(path / "is_other.joblib")
        pos_path = path / "positive.joblib"
        obj.positive_pipe = joblib.load(pos_path) if pos_path.exists() else None
        obj.classes_positive_ = meta.get("classes_positive_", [])
        obj.threshold_modes = meta.get("threshold_modes", {})
        obj.default_mode = meta.get("default_mode", "balanced")
        return obj

    def set_mode(self, mode: str):
        """切换阈值模式 (strict / balanced / balanced_strict)."""
        if mode in self.threshold_modes:
            self.other_threshold = float(self.threshold_modes[mode]["threshold"])


# ---------- 细分类器 ----------


@dataclass
class SubClassifier:
    pipes: Dict[str, Pipeline] = field(default_factory=dict)
    classes_: Dict[str, List[str]] = field(default_factory=dict)
    fallback: Dict[str, str] = field(default_factory=dict)

    def fit(self, texts: List[str], hazards: List[str], subs: List[str]) -> "SubClassifier":
        for hz in POSITIVE_TYPES:
            xs = [normalize_text(t) for t, h in zip(texts, hazards) if h == hz]
            ys = [s for s, h in zip(subs, hazards) if h == hz]
            ys = [s if (s and not pd.isna(s)) else "其他" for s in ys]
            if not xs:
                continue
            # 退化: 单一类 -> 跳过模型, 直接 fallback
            unique = list(dict.fromkeys(ys))
            if len(unique) <= 1:
                self.fallback[hz] = unique[0] if unique else "其他"
                continue
            # 计算最常见类用于 fallback
            self.fallback[hz] = pd.Series(ys).value_counts().index[0]
            pipe = Pipeline([
                ("feat", make_combined_vectorizer()),
                ("clf", LogisticRegression(
                    max_iter=4000, C=4.0, class_weight="balanced",
                    solver="lbfgs",
                )),
            ])
            pipe.fit(xs, ys)
            self.pipes[hz] = pipe
            self.classes_[hz] = list(pipe.named_steps["clf"].classes_)
        return self

    def predict(self, texts: List[str], hazards: List[str], use_rules: bool = True
                ) -> Tuple[List[str], List[Optional[np.ndarray]], List[List[str]]]:
        out: List[str] = []
        proba_list: List[Optional[np.ndarray]] = []
        cls_list: List[List[str]] = []
        for t, hz in zip(texts, hazards):
            t = normalize_text(t)
            if hz == OTHER_LABEL:
                out.append("其他")
                proba_list.append(None)
                cls_list.append(["其他"])
                continue
            rule_sub = rule_predict_subclass(hz, t) if use_rules else None
            if rule_sub:
                out.append(rule_sub)
                proba_list.append(None)
                cls_list.append([rule_sub])
                continue
            pipe = self.pipes.get(hz)
            if pipe is None:
                out.append(self.fallback.get(hz, "其他"))
                proba_list.append(None)
                cls_list.append([self.fallback.get(hz, "其他")])
                continue
            proba = pipe.predict_proba([t])[0]
            classes = self.classes_[hz]
            out.append(classes[int(np.argmax(proba))])
            proba_list.append(proba)
            cls_list.append(classes)
        return out, proba_list, cls_list

    def save(self, path: Path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        for hz, pipe in self.pipes.items():
            joblib.dump(pipe, path / f"sub_{hz}.joblib")
        meta = {"classes_": self.classes_, "fallback": self.fallback,
                "hazards": list(self.pipes.keys())}
        (path / "sub_meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "SubClassifier":
        path = Path(path)
        meta = json.loads((path / "sub_meta.json").read_text())
        obj = cls(classes_=meta.get("classes_", {}), fallback=meta.get("fallback", {}))
        for hz in meta.get("hazards", []):
            obj.pipes[hz] = joblib.load(path / f"sub_{hz}.joblib")
        return obj


# ---------- 顶层 wrapper ----------

@dataclass
class FullClassifier:
    hazard: HazardClassifier
    sub: SubClassifier

    def predict(self, texts: List[str], use_rules: bool = True):
        haz, p_other, pos_proba, pos_classes = self.hazard.predict(texts, use_rules=use_rules)
        sub, sub_proba, sub_classes = self.sub.predict(texts, haz, use_rules=use_rules)
        return {
            "隐患类型": haz,
            "分类": sub,
            "p_other": p_other,
            "pos_proba": pos_proba,
            "pos_classes": pos_classes,
            "sub_proba": sub_proba,
            "sub_classes": sub_classes,
        }

    def save(self, path: Path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        self.hazard.save(path / "hazard")
        self.sub.save(path / "sub")

    @classmethod
    def load(cls, path: Path) -> "FullClassifier":
        path = Path(path)
        return cls(
            hazard=HazardClassifier.load(path / "hazard"),
            sub=SubClassifier.load(path / "sub"),
        )
