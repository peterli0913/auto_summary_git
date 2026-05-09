"""命令行: 输入 5 个文件 -> 输出 Excel."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hazard_pipeline.excel_writer import write_output
from hazard_pipeline.predict import run_pipeline
from hazard_pipeline.schema import OUTPUT_COLUMNS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", help="输入 Excel 文件路径 (5 类任意子集)")
    parser.add_argument("-o", "--output",
                        default="data/outputs/跑冒滴漏与静电风险专项跟踪_输出.xlsx")
    parser.add_argument("--model_dir", default="models/current")
    parser.add_argument("--no-rules", action="store_true", help="关闭规则后处理")
    args = parser.parse_args()

    df = run_pipeline(args.inputs, model_dir=args.model_dir,
                       use_rules=not args.no_rules)
    print(f"汇总并预测完成, 共 {len(df)} 条")
    if "隐患类型" in df.columns:
        print("隐患类型分布:")
        print(df["隐患类型"].value_counts().to_string())
    out_df = df[[c for c in OUTPUT_COLUMNS if c in df.columns]]
    path = write_output(out_df, args.output)
    print(f"已写出: {path}")


if __name__ == "__main__":
    main()
