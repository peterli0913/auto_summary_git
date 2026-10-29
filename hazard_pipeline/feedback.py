"""人工反馈数据持久化 + 增量再训练支持.

数据格式: parquet, 列与训练数据保持一致:
    事件描述, 隐患类型, 分类, source (来源标注: human/auto), timestamp
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

DEFAULT_FB_PATH = Path("data/feedback/labels.parquet")
COLUMNS = ["事件描述", "隐患类型", "分类", "source", "timestamp"]


def load_feedback(path: Path = DEFAULT_FB_PATH) -> Optional[pd.DataFrame]:
    if not Path(path).exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    keep = [c for c in COLUMNS if c in df.columns]
    df = df[keep].copy()
    if "事件描述" not in df.columns or "隐患类型" not in df.columns:
        return None
    df = df.dropna(subset=["事件描述", "隐患类型"]).reset_index(drop=True)
    return df


def append_feedback(records: List[dict], path: Path = DEFAULT_FB_PATH) -> int:
    """把人工确认/修正的标签追加到 feedback 文件.

    每条记录至少包含 事件描述, 隐患类型, 分类.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    new = []
    now = datetime.utcnow().isoformat()
    for r in records:
        if not r.get("事件描述") or not r.get("隐患类型"):
            continue
        new.append({
            "事件描述": str(r["事件描述"]),
            "隐患类型": str(r["隐患类型"]),
            "分类": str(r.get("分类") or "其他"),
            "source": str(r.get("source") or "human"),
            "timestamp": now,
        })
    if not new:
        return 0
    new_df = pd.DataFrame(new, columns=COLUMNS)
    if Path(path).exists():
        old = load_feedback(path)
        if old is not None and len(old):
            new_df = pd.concat([old, new_df], ignore_index=True)
    # 去重: 同一条事件描述只留最新
    new_df = new_df.drop_duplicates(subset=["事件描述"], keep="last")
    new_df.to_parquet(path, index=False)
    return len(new)


def feedback_summary(path: Path = DEFAULT_FB_PATH) -> dict:
    df = load_feedback(path)
    if df is None:
        return {"count": 0}
    return {
        "count": int(len(df)),
        "by_hazard": df["隐患类型"].value_counts().to_dict(),
    }
