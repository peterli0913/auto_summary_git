"""一键训练: 从 跑冒滴漏与静电风险专项跟踪.xlsx 训练初始模型.

支持两种 variant:
  --variant standard (default)  : TF-IDF + LR/SVC, 训练快, 模型小
  --variant enhanced            : sentence-transformer embedding + LR/SVC

可选半监督:
  --pseudo PATH                 : 启用伪标签合并训练 (data/feedback/pseudo_labels.parquet)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hazard_pipeline.train import (DEFAULT_GOLD_PATH, DEFAULT_MODEL_DIR,
                                     ENHANCED_MODEL_DIR, train_pipeline)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["standard", "enhanced"], default="standard")
    p.add_argument("--gold", default=str(DEFAULT_GOLD_PATH))
    p.add_argument("--model_dir", default=None)
    p.add_argument("--feedback", default="data/feedback/labels.parquet")
    p.add_argument("--pseudo", default=None,
                   help="伪标签 parquet 文件 (启用半监督)")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if args.model_dir is None:
        args.model_dir = (str(ENHANCED_MODEL_DIR) if args.variant == "enhanced"
                          else str(DEFAULT_MODEL_DIR))
    pseudo = Path(args.pseudo) if args.pseudo else None

    train_pipeline(
        gold_path=Path(args.gold),
        model_dir=Path(args.model_dir),
        feedback_path=Path(args.feedback),
        pseudo_path=pseudo,
        variant=args.variant,
        random_state=args.seed,
    )
