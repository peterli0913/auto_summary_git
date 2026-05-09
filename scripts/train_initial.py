"""一键训练: 从 跑冒滴漏与静电风险专项跟踪.xlsx 训练初始模型."""
import sys
from pathlib import Path

# 允许在仓库根目录直接执行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hazard_pipeline.train import train_pipeline


if __name__ == "__main__":
    train_pipeline()
