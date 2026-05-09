"""半监督自训练 (self-training).

策略:
1. 用当前模型对未标注数据 (聚合后的输入文件) 推理.
2. 取 高置信度 的样本作为伪标签:
   - 隐患类型: top1_prob >= haz_conf  AND  top1-top2 >= 0.5
   - 分类: 隐患类型为正类时 sub_top1_prob >= sub_conf
3. 把伪标签合并到训练数据中, 标注 source='pseudo' (避免和真实/人工反馈混淆).
4. 重新训练.

设计要点:
- 只在 高置信 样本上贴标签 (避免传播错误)
- 伪标签优先级低于真实标签 + 人工反馈 (在 train 时如果同文本有真实标签会优先)
- 自训练可以多轮迭代, 但通常 1-2 轮足够
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

import numpy as np
import pandas as pd

from .aggregator import aggregate_files
from .feedback import COLUMNS as FB_COLS
from .schema import POSITIVE_TYPES, OTHER_LABEL


def make_pseudo_labels(unlabeled_df: pd.DataFrame, model,
                        haz_conf: float = 0.92,
                        haz_diff: float = 0.5,
                        sub_conf: float = 0.85,
                        use_rules: bool = True,
                        ) -> pd.DataFrame:
    """对已聚合的 DataFrame (含 事件描述) 打伪标签, 仅返回置信度满足条件的样本."""
    if not len(unlabeled_df):
        return pd.DataFrame(columns=["事件描述", "隐患类型", "分类"])

    texts = unlabeled_df["事件描述"].fillna("").astype(str).tolist()
    res = model.predict(texts, use_rules=use_rules)

    pred_haz = np.array(res["隐患类型"])
    pred_sub = np.array(res["分类"])
    p_other = np.array(res["p_other"]) if len(res["p_other"]) else np.zeros(len(texts))
    pos_proba = np.array(res["pos_proba"]) if len(res["pos_proba"]) else np.zeros((len(texts), 0))
    pos_classes = res["pos_classes"]

    # 计算 hazard top1 / top2 概率
    if pos_classes:
        haz_probs = np.column_stack(
            [p_other] + [(1.0 - p_other) * pos_proba[:, i] for i in range(len(pos_classes))]
        )
    else:
        haz_probs = p_other.reshape(-1, 1)
    sorted_haz = -np.sort(-haz_probs, axis=1)
    haz_top1 = sorted_haz[:, 0]
    haz_top2 = sorted_haz[:, 1] if sorted_haz.shape[1] > 1 else np.zeros_like(haz_top1)

    # 子类 top1 概率
    sub_top1 = []
    for sub_pred, proba in zip(res["分类"], res["sub_proba"]):
        if proba is None or len(proba) == 0:
            sub_top1.append(1.0)
        else:
            sub_top1.append(float(np.max(proba)))
    sub_top1 = np.array(sub_top1)

    # 选取高置信样本
    confident_haz = (haz_top1 >= haz_conf) & ((haz_top1 - haz_top2) >= haz_diff)
    confident_sub = (pred_haz == OTHER_LABEL) | (sub_top1 >= sub_conf)
    confident = confident_haz & confident_sub

    out = pd.DataFrame({
        "事件描述": texts,
        "隐患类型": pred_haz,
        "分类": pred_sub,
        "_haz_top1": haz_top1,
        "_haz_top2": haz_top2,
        "_sub_top1": sub_top1,
        "_confident": confident,
    })
    pseudo = out[out["_confident"]].copy()
    return pseudo


def write_pseudo_to_feedback(pseudo: pd.DataFrame,
                              path: Union[str, Path] = "data/feedback/pseudo_labels.parquet"):
    """把伪标签独立保存 (与人工反馈分开). 训练时可选择性合并."""
    if not len(pseudo):
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "事件描述": pseudo["事件描述"],
        "隐患类型": pseudo["隐患类型"],
        "分类": pseudo["分类"],
        "source": ["pseudo"] * len(pseudo),
        "timestamp": [pd.Timestamp.utcnow().isoformat()] * len(pseudo),
    })
    if path.exists():
        old = pd.read_parquet(path)
        df = pd.concat([old, df], ignore_index=True)
    df = df.drop_duplicates(subset=["事件描述"], keep="last")
    df.to_parquet(path, index=False)
    return len(df)


def load_pseudo_labels(path: Union[str, Path] = "data/feedback/pseudo_labels.parquet"
                        ) -> Optional[pd.DataFrame]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def self_train_pipeline(input_files: Iterable[Union[str, Path]],
                         model,
                         haz_conf: float = 0.92,
                         haz_diff: float = 0.5,
                         sub_conf: float = 0.85,
                         pseudo_path: Union[str, Path] = "data/feedback/pseudo_labels.parquet",
                         use_rules: bool = True,
                         ) -> dict:
    """端到端: 5 输入聚合 -> 伪标签 -> 写入 pseudo file. 不直接重训, 由调用方触发."""
    df = aggregate_files(input_files)
    pseudo = make_pseudo_labels(df, model, haz_conf=haz_conf, haz_diff=haz_diff,
                                  sub_conf=sub_conf, use_rules=use_rules)
    n_total = len(df)
    n_conf = len(pseudo)
    n_written = write_pseudo_to_feedback(pseudo, pseudo_path) if n_conf else 0
    by_hz = pseudo["隐患类型"].value_counts().to_dict() if n_conf else {}
    return {
        "n_total": int(n_total),
        "n_confident": int(n_conf),
        "n_written": int(n_written),
        "by_hazard": by_hz,
        "thresholds": {"haz_conf": haz_conf, "haz_diff": haz_diff, "sub_conf": sub_conf},
        "pseudo_path": str(pseudo_path),
    }
