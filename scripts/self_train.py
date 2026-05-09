"""自训练流程: 用当前模型对原始 5 输入 (或任意未标注 Excel) 打伪标签
然后追加到训练集合并重训.

Usage:
    python scripts/self_train.py --inputs file1.xlsx file2.xlsx ... \
        --variant standard|enhanced --haz_conf 0.92 --sub_conf 0.85
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hazard_pipeline.predict import load_model_auto
from hazard_pipeline.semi_supervised import self_train_pipeline
from hazard_pipeline.train import (DEFAULT_MODEL_DIR, ENHANCED_MODEL_DIR,
                                     train_pipeline)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--inputs", nargs="+", required=True,
                   help="未标注 Excel 文件路径 (5 类原始数据中的任意子集)")
    p.add_argument("--variant", choices=["standard", "enhanced"], default="standard")
    p.add_argument("--haz_conf", type=float, default=0.92)
    p.add_argument("--haz_diff", type=float, default=0.5)
    p.add_argument("--sub_conf", type=float, default=0.85)
    p.add_argument("--pseudo_path",
                   default="data/feedback/pseudo_labels.parquet")
    p.add_argument("--retrain", action="store_true",
                   help="伪标签生成后立即重训")
    p.add_argument("--no_rules", action="store_true")
    args = p.parse_args()

    model_dir = (str(ENHANCED_MODEL_DIR) if args.variant == "enhanced"
                 else str(DEFAULT_MODEL_DIR))
    print(f"加载模型: {model_dir}  (variant={args.variant})")
    model = load_model_auto(model_dir)

    print(f"对 {len(args.inputs)} 个文件打伪标签 ...")
    info = self_train_pipeline(
        args.inputs, model,
        haz_conf=args.haz_conf, haz_diff=args.haz_diff, sub_conf=args.sub_conf,
        pseudo_path=args.pseudo_path,
        use_rules=not args.no_rules,
    )
    print("\n=== 伪标签结果 ===")
    print(f"输入总数 (聚合后): {info['n_total']}")
    print(f"高置信样本数      : {info['n_confident']} "
          f"({info['n_confident']/max(1,info['n_total'])*100:.1f}%)")
    print(f"已写入伪标签库    : {info['n_written']} 条 -> {info['pseudo_path']}")
    print(f"按隐患类型分布    : {info['by_hazard']}")
    print(f"使用阈值          : {info['thresholds']}")

    if args.retrain:
        print("\n=== 用伪标签 + 真实标注 重训 ===")
        train_pipeline(
            model_dir=Path(model_dir),
            pseudo_path=Path(args.pseudo_path),
            variant=args.variant,
        )
