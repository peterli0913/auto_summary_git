"""推理工具: 加载模型, 处理 5 类输入并产出统一 DataFrame."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

from .aggregator import aggregate_files
from .classifier import FullClassifier
from .schema import OUTPUT_COLUMNS, POSITIVE_TYPES, OTHER_LABEL


def load_model_auto(model_dir: Union[str, Path]):
    """根据 model_dir 中的 variant.txt 自动选择 standard / enhanced 加载."""
    p = Path(model_dir)
    variant_file = p / "variant.txt"
    variant = "standard"
    if variant_file.exists():
        variant = variant_file.read_text().strip()
    if variant == "enhanced":
        from .enhanced_classifier import EnhancedFullClassifier
        return EnhancedFullClassifier.load(p)
    return FullClassifier.load(p)


def predict_dataframe(df: pd.DataFrame, model,
                      use_rules: bool = True,
                      hazard_review_top1_max: float = 0.85,
                      hazard_review_diff_min: float = 0.30,
                      sub_review_top1_max: float = 0.70,
                      ) -> pd.DataFrame:
    """对已聚合好的统一格式 DataFrame 进行隐患类型 / 分类预测.

    review 触发条件:
      * 隐患类型: top1-top2 < hazard_review_diff_min  OR  top1 < hazard_review_top1_max
                  OR  规则与模型原始预测冲突 (haz_rule_overridden)
      * 分类:     top1 < sub_review_top1_max
                  OR  规则与模型原始预测冲突 (sub_rule_overridden)
    """
    df = df.copy().reset_index(drop=True)
    texts = df["事件描述"].fillna("").astype(str).tolist()
    res = model.predict(texts, use_rules=use_rules)

    df["隐患类型"] = res["隐患类型"]
    df["分类"] = res["分类"]

    p_other = np.array(res["p_other"]) if len(res["p_other"]) else np.zeros(len(df))
    pos_proba = np.array(res["pos_proba"]) if len(res["pos_proba"]) else np.zeros((len(df), 0))
    pos_classes = res["pos_classes"]

    df["p_hazard_其他"] = p_other
    for i, cls in enumerate(pos_classes):
        df[f"p_hazard_{cls}"] = (1.0 - p_other) * pos_proba[:, i]

    haz_labels = ["其他"] + pos_classes
    if pos_classes:
        haz_probs = np.column_stack(
            [p_other] + [(1.0 - p_other) * pos_proba[:, i] for i in range(len(pos_classes))]
        )
    else:
        haz_probs = p_other.reshape(-1, 1)
    top1_idx = np.argmax(haz_probs, axis=1)
    top1_prob = haz_probs[np.arange(len(df)), top1_idx]
    haz_probs_2 = haz_probs.copy()
    haz_probs_2[np.arange(len(df)), top1_idx] = -1
    top2_idx = np.argmax(haz_probs_2, axis=1)
    top2_prob = haz_probs[np.arange(len(df)), top2_idx]
    df["hazard_top1_label"] = [haz_labels[i] for i in top1_idx]
    df["hazard_top1_prob"] = top1_prob
    df["hazard_top2_label"] = [haz_labels[i] for i in top2_idx]
    df["hazard_top2_prob"] = top2_prob

    sub_top1, sub_top1_p, sub_top2, sub_top2_p = [], [], [], []
    for sub_pred, proba, classes in zip(res["分类"], res["sub_proba"], res["sub_classes"]):
        if proba is None or len(proba) == 0:
            sub_top1.append(sub_pred); sub_top1_p.append(1.0)
            sub_top2.append(None); sub_top2_p.append(0.0)
            continue
        order = np.argsort(-np.asarray(proba))
        sub_top1.append(classes[order[0]])
        sub_top1_p.append(float(proba[order[0]]))
        if len(order) > 1:
            sub_top2.append(classes[order[1]])
            sub_top2_p.append(float(proba[order[1]]))
        else:
            sub_top2.append(None); sub_top2_p.append(0.0)
    df["sub_top1_label"] = sub_top1
    df["sub_top1_prob"] = sub_top1_p
    df["sub_top2_label"] = sub_top2
    df["sub_top2_prob"] = sub_top2_p

    haz_rule_over = res.get("haz_rule_overridden") or [False] * len(df)
    sub_rule_over = res.get("sub_rule_overridden") or [False] * len(df)
    df["haz_rule_overridden"] = haz_rule_over
    df["sub_rule_overridden"] = sub_rule_over
    df["haz_model_only"] = res.get("haz_model_only") or df["隐患类型"].tolist()
    df["sub_model_only"] = res.get("sub_model_only") or df["分类"].tolist()

    haz_diff = (df["hazard_top1_prob"] - df["hazard_top2_prob"]).abs()
    df["hazard_uncertain"] = (
        (haz_diff < hazard_review_diff_min)
        | (df["hazard_top1_prob"] < hazard_review_top1_max)
        | df["haz_rule_overridden"].astype(bool)
    )
    df["sub_uncertain"] = (
        (df["sub_top1_prob"] < sub_review_top1_max)
        | df["sub_rule_overridden"].astype(bool)
    )
    return df


def run_pipeline(input_files: Sequence[Union[str, Path]],
                 model_dir: Union[str, Path] = "models/current",
                 use_rules: bool = True) -> pd.DataFrame:
    """端到端: 5 个输入文件 -> 统一格式 + 预测.

    自动识别 model_dir 中的 variant (standard/enhanced).
    """
    df = aggregate_files(input_files)
    if not len(df):
        return df
    model = load_model_auto(model_dir)
    return predict_dataframe(df, model, use_rules=use_rules)
