"""训练 + 评估.

流程:
1. 读取 跑冒滴漏与静电风险专项跟踪.xlsx (或 训练集合_综合.xlsx) 的全部标注数据,
   合并 data/feedback/labels.parquet 中的人工反馈 (高优先级覆盖).
2. 9:1 stratified 切分; 测试集严格不参与任何训练 / 阈值选择.
3. 训练 HazardClassifier (其他/非其他二分类 + 三分类) + SubClassifier.
4. 在 训练集合内部 再做 cross-val 选 other_threshold:
     - 目标 1: 正类→其他 错分比 < 1%
     - 目标 2: 其他→正类 错分数 < 5% × 正类总量
   评估时只在 test 集汇报最终指标.
5. 保存模型到 models/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score)
from sklearn.model_selection import StratifiedKFold, train_test_split

from .classifier import FullClassifier, HazardClassifier, SubClassifier
from .feedback import load_feedback
from .rules import rule_predict_hazard
from .schema import HAZARD_TYPES, OTHER_LABEL, OUTPUT_COLUMNS, POSITIVE_TYPES

DEFAULT_GOLD_PATH = Path("跑冒滴漏与静电风险专项跟踪.xlsx")
DEFAULT_MODEL_DIR = Path("models/current")


# ------- 数据加载 -------

def load_training_data(gold_path: Path = DEFAULT_GOLD_PATH,
                       feedback_path: Path = Path("data/feedback/labels.parquet"),
                       ) -> pd.DataFrame:
    df = pd.read_excel(gold_path)
    if "事件描述" not in df.columns:
        raise ValueError(f"{gold_path} 缺少 '事件描述' 列")
    df = df.dropna(subset=["事件描述", "隐患类型"]).copy()
    df["事件描述"] = df["事件描述"].astype(str)
    df["隐患类型"] = df["隐患类型"].astype(str)
    df["分类"] = df["分类"].fillna("其他").astype(str)
    df = df[df["隐患类型"].isin(HAZARD_TYPES)]
    df = df.reset_index(drop=True)

    fb = load_feedback(feedback_path)
    if fb is not None and len(fb):
        # 反馈数据优先 (按事件描述去重)
        df = pd.concat([df, fb], ignore_index=True)
        df = df.drop_duplicates(subset=["事件描述"], keep="last").reset_index(drop=True)
    return df


# ------- 阈值调优 (在 train 折内部做 CV) -------

def tune_other_threshold(clf: HazardClassifier, texts: List[str], labels: List[str],
                         pos_to_other_budget: float = 0.01,
                         other_to_pos_budget_ratio: float = 0.05,
                         ) -> float:
    """扫描阈值, 选取符合两个约束的最严格阈值 (使其他错分到正类最少).

    pos_to_other_budget: P(正类被预测为其他) 上限 (默认 1%)
    other_to_pos_budget_ratio: 其他被错分到正类的数量, 不超过 正类总量 × 此比例 (默认 5%)
    """
    p_other = clf.predict_proba_other(texts)
    if clf.positive_pipe is not None:
        pos_proba, pos_classes = clf.predict_proba_positive(texts)
    else:
        pos_proba, pos_classes = np.zeros((len(texts), 0)), []

    y = np.array(labels)
    n_pos = int((y != OTHER_LABEL).sum())
    fp_budget = max(1, int(round(other_to_pos_budget_ratio * n_pos)))

    best_threshold = 0.5
    best_fp = 1e9
    for thr in np.linspace(0.05, 0.95, 91):
        pred = []
        for i in range(len(texts)):
            if p_other[i] >= thr:
                pred.append(OTHER_LABEL)
            else:
                if pos_classes:
                    pred.append(pos_classes[int(np.argmax(pos_proba[i]))])
                else:
                    pred.append(OTHER_LABEL)
        pred = np.array(pred)
        is_pos = y != OTHER_LABEL
        # 1) 正类被预测为 其他 (在正类样本内部的比例)
        if is_pos.sum() == 0:
            pos_to_other_rate = 0.0
        else:
            pos_to_other_rate = ((pred == OTHER_LABEL) & is_pos).sum() / is_pos.sum()
        # 2) 其他被预测为正类的数量
        is_other = y == OTHER_LABEL
        other_to_pos_count = ((pred != OTHER_LABEL) & is_other).sum()

        if pos_to_other_rate <= pos_to_other_budget and other_to_pos_count <= fp_budget:
            # 选 fp 最少 (最保守) 的, 平局选 thr 较大 (更倾向 其他, 避免噪声)
            if other_to_pos_count < best_fp or (
                other_to_pos_count == best_fp and thr > best_threshold
            ):
                best_fp = int(other_to_pos_count)
                best_threshold = float(thr)

    return best_threshold


# ------- 评估 -------

def evaluate(full: FullClassifier, df_test: pd.DataFrame) -> dict:
    texts = df_test["事件描述"].tolist()
    y_haz = df_test["隐患类型"].tolist()
    y_sub = df_test["分类"].tolist()
    res = full.predict(texts, use_rules=True)
    pred_haz = res["隐患类型"]
    pred_sub = res["分类"]

    y_haz_arr = np.array(y_haz)
    pred_haz_arr = np.array(pred_haz)
    is_pos = y_haz_arr != OTHER_LABEL
    is_other = y_haz_arr == OTHER_LABEL
    if is_pos.sum():
        pos_to_other_rate = float(((pred_haz_arr == OTHER_LABEL) & is_pos).sum()) / float(is_pos.sum())
    else:
        pos_to_other_rate = 0.0
    other_to_pos_count = int(((pred_haz_arr != OTHER_LABEL) & is_other).sum())
    other_to_pos_ratio = other_to_pos_count / max(1, int(is_pos.sum()))

    macro_f1 = f1_score(y_haz, pred_haz, average="macro", labels=HAZARD_TYPES, zero_division=0)
    haz_acc = accuracy_score(y_haz, pred_haz)
    sub_acc = accuracy_score(y_sub, pred_sub)

    # 分隐患类型的细分类准确率
    sub_acc_per_haz = {}
    for hz in HAZARD_TYPES:
        mask = y_haz_arr == hz
        if mask.sum() == 0:
            continue
        sub_acc_per_haz[hz] = float(accuracy_score(
            np.array(y_sub)[mask], np.array(pred_sub)[mask]))

    metrics = {
        "n_test": int(len(df_test)),
        "n_pos_test": int(is_pos.sum()),
        "n_other_test": int(is_other.sum()),
        "hazard_accuracy": float(haz_acc),
        "hazard_macro_f1": float(macro_f1),
        "正类→其他_错分率": pos_to_other_rate,
        "其他→正类_错分数": other_to_pos_count,
        "其他→正类_占正类比例": other_to_pos_ratio,
        "分类_overall_accuracy": float(sub_acc),
        "分类_per_hazard_accuracy": sub_acc_per_haz,
        "hazard_confusion": confusion_matrix(
            y_haz, pred_haz, labels=HAZARD_TYPES).tolist(),
        "hazard_labels": HAZARD_TYPES,
    }
    metrics["hazard_classification_report"] = classification_report(
        y_haz, pred_haz, labels=HAZARD_TYPES, zero_division=0, output_dict=True)
    return metrics


# ------- 主入口 -------

def train_pipeline(gold_path: Path = DEFAULT_GOLD_PATH,
                   model_dir: Path = DEFAULT_MODEL_DIR,
                   feedback_path: Path = Path("data/feedback/labels.parquet"),
                   random_state: int = 42,
                   test_size: float = 0.1,
                   ) -> dict:
    df = load_training_data(gold_path, feedback_path)
    print(f"加载 {len(df)} 条标注数据")
    print(df["隐患类型"].value_counts().to_string())

    train_df, test_df = train_test_split(
        df, test_size=test_size, random_state=random_state,
        stratify=df["隐患类型"],
    )
    print(f"切分: train={len(train_df)}  test={len(test_df)}")

    haz_clf = HazardClassifier()
    haz_clf.fit(train_df["事件描述"].tolist(), train_df["隐患类型"].tolist())

    # 5-fold OOF 预测, 用于无信息泄露的阈值调优
    print("交叉验证选阈值 (避免使用 test 数据)...")
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=random_state)
    oof_p_other = np.zeros(len(train_df))
    oof_pos_proba = None
    oof_pos_classes = None
    train_texts = train_df["事件描述"].tolist()
    train_labels = train_df["隐患类型"].tolist()
    train_idx_arr = np.arange(len(train_df))
    for fold, (tr_i, va_i) in enumerate(skf.split(train_idx_arr, train_labels), 1):
        sub_clf = HazardClassifier()
        sub_clf.fit(
            [train_texts[i] for i in tr_i],
            [train_labels[i] for i in tr_i],
        )
        va_texts = [train_texts[i] for i in va_i]
        oof_p_other[va_i] = sub_clf.predict_proba_other(va_texts)
        if sub_clf.positive_pipe is not None:
            pp, pc = sub_clf.predict_proba_positive(va_texts)
            if oof_pos_proba is None:
                oof_pos_classes = pc
                oof_pos_proba = np.zeros((len(train_df), len(pc)))
            oof_pos_proba[va_i] = pp
        print(f"  fold {fold} done")

    # 用 OOF 概率扫描阈值
    class _Stub:
        pass

    # 直接复用 tune_other_threshold 的逻辑, 但用 OOF 概率
    # 在 closure 里手动实现 (避免改动函数签名)
    y = np.array(train_labels)
    n_pos = int((y != OTHER_LABEL).sum())
    fp_budget = max(1, int(round(0.05 * n_pos)))
    pos_to_other_budget = 0.01

    # 预先计算规则结果, 让阈值调优反映真实推理
    rule_results = [rule_predict_hazard(t) for t in train_texts]
    pos_classes_arr = np.array(oof_pos_classes) if oof_pos_classes else None

    def _apply(thr):
        pred = []
        for i in range(len(train_texts)):
            r = rule_results[i]
            if oof_p_other[i] >= thr:
                model_pred = OTHER_LABEL
            else:
                model_pred = (pos_classes_arr[int(np.argmax(oof_pos_proba[i]))]
                              if pos_classes_arr is not None else OTHER_LABEL)
            if r == OTHER_LABEL:
                model_pred = OTHER_LABEL
            elif r in POSITIVE_TYPES and model_pred == OTHER_LABEL and oof_p_other[i] < 0.90:
                model_pred = r
            pred.append(model_pred)
        return np.array(pred)

    # ----- 同时计算多种阈值 (保存到模型的 metadata, 可在 UI 切换) -----
    threshold_modes = {}

    # 模式 A: 严格 - 优先满足 正类→其他 ≤ 1%, 在该约束下最小化 FP
    strict_thr, strict_fp, strict_pto = None, 10**9, 1.0
    for thr in np.linspace(0.05, 0.99, 95):
        pred = _apply(thr)
        is_pos = y != OTHER_LABEL
        is_other = y == OTHER_LABEL
        pto = ((pred == OTHER_LABEL) & is_pos).sum() / is_pos.sum() if is_pos.sum() else 0.0
        otp = int(((pred != OTHER_LABEL) & is_other).sum())
        if pto <= pos_to_other_budget and otp < strict_fp:
            strict_fp = otp; strict_thr = float(thr); strict_pto = pto

    # 模式 B: 严格双约束 (若可行)
    both_thr, both_fp, both_pto = None, 10**9, 1.0
    for thr in np.linspace(0.05, 0.99, 95):
        pred = _apply(thr)
        is_pos = y != OTHER_LABEL
        is_other = y == OTHER_LABEL
        pto = ((pred == OTHER_LABEL) & is_pos).sum() / is_pos.sum() if is_pos.sum() else 0.0
        otp = int(((pred != OTHER_LABEL) & is_other).sum())
        if pto <= pos_to_other_budget and otp <= fp_budget:
            if otp < both_fp:
                both_fp = otp; both_thr = float(thr); both_pto = pto

    # 模式 C: 平衡 (最大化 accuracy, 兼顾 macro_f1)
    bal_thr, bal_score, bal_pto, bal_fp, bal_acc = 0.5, -1.0, 1.0, 10**9, 0.0
    for thr in np.linspace(0.05, 0.95, 91):
        pred = _apply(thr)
        acc = (pred == y).mean()
        # 简单 macro recall (无需依赖 sklearn)
        recs = []
        for cls in HAZARD_TYPES:
            mask = y == cls
            if mask.sum():
                recs.append(((pred == cls) & mask).sum() / mask.sum())
        macro_rec = float(np.mean(recs)) if recs else 0.0
        score = acc * 0.6 + macro_rec * 0.4
        if score > bal_score:
            bal_score = score; bal_thr = float(thr); bal_acc = acc
            bal_pto = ((pred == OTHER_LABEL) & (y != OTHER_LABEL)).sum() / max(1, (y != OTHER_LABEL).sum())
            bal_fp = int(((pred != OTHER_LABEL) & (y == OTHER_LABEL)).sum())

    if both_thr is not None:
        threshold_modes["balanced_strict"] = {"threshold": both_thr, "pos_to_other": both_pto, "other_to_pos": both_fp}
    if strict_thr is not None:
        threshold_modes["strict"] = {"threshold": strict_thr, "pos_to_other": strict_pto, "other_to_pos": strict_fp}
    threshold_modes["balanced"] = {"threshold": bal_thr, "pos_to_other": bal_pto, "other_to_pos": bal_fp, "accuracy": bal_acc}

    # 默认使用 balanced (最大准确率); 若双约束可行优先使用 balanced_strict
    if both_thr is not None:
        haz_clf.other_threshold = both_thr
        default_mode = "balanced_strict"
    else:
        haz_clf.other_threshold = bal_thr
        default_mode = "balanced"
    print(f"\n阈值模式 (OOF):")
    for mode, info in threshold_modes.items():
        print(f"  [{mode}] thr={info['threshold']:.3f}  "
              f"正类→其他={info['pos_to_other']*100:.2f}%  其他→正类={info['other_to_pos']}"
              + (f"  acc={info.get('accuracy',0):.3f}" if 'accuracy' in info else ""))
    print(f"默认使用模式: {default_mode}  (other_threshold = {haz_clf.other_threshold:.3f})")
    print(f"  约束预算: 正类→其他 < {pos_to_other_budget*100:.0f}%   其他→正类 ≤ {fp_budget}")
    haz_clf.threshold_modes = threshold_modes
    haz_clf.default_mode = default_mode

    # 细分类
    sub_clf = SubClassifier()
    sub_clf.fit(
        train_df["事件描述"].tolist(),
        train_df["隐患类型"].tolist(),
        train_df["分类"].fillna("其他").tolist(),
    )

    full = FullClassifier(hazard=haz_clf, sub=sub_clf)
    full.save(model_dir)
    print(f"模型已保存到 {model_dir}")

    metrics = evaluate(full, test_df)
    metrics["other_threshold"] = haz_clf.other_threshold
    metrics["default_mode"] = haz_clf.default_mode
    metrics["threshold_modes"] = haz_clf.threshold_modes
    metrics["n_train"] = int(len(train_df))
    print("\n=== 测试集评估 ===")
    for k, v in metrics.items():
        if k in {"hazard_confusion", "hazard_classification_report"}:
            continue
        print(f"  {k}: {v}")
    print("\nhazard confusion (rows=true, cols=pred):")
    print(pd.DataFrame(metrics["hazard_confusion"],
                       index=HAZARD_TYPES, columns=HAZARD_TYPES))
    Path(model_dir).mkdir(parents=True, exist_ok=True)
    (Path(model_dir) / "metrics.json").write_text(json.dumps(metrics,
                                                              ensure_ascii=False,
                                                              indent=2))
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=str(DEFAULT_GOLD_PATH))
    parser.add_argument("--model_dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--feedback", default="data/feedback/labels.parquet")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    train_pipeline(
        gold_path=Path(args.gold),
        model_dir=Path(args.model_dir),
        feedback_path=Path(args.feedback),
        random_state=args.seed,
    )
