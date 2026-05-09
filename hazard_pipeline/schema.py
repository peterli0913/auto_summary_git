"""统一字段定义、类别常量、来源映射."""
from __future__ import annotations

# 与 跑冒滴漏与静电风险专项跟踪.xlsx 完全一致的列顺序
OUTPUT_COLUMNS = [
    "来源",
    "巡查类型",
    "日期",
    "厂区",
    "属地",
    "责任区域",
    "事件描述",
    "隐患类型",
    "分类",
    "原因分析（EHS）",
    "原因归类（EHS）",
    "整改结果",
    "调查报告",
    "月份",
    "周",
    "key",
]

# 隐患类型 4 类
HAZARD_TYPES = ["跑冒滴漏", "静电事件", "化学品暴露", "其他"]
POSITIVE_TYPES = ["跑冒滴漏", "静电事件", "化学品暴露"]
OTHER_LABEL = "其他"

# 输入文件 -> 来源 / 默认巡查类型 (用户需求)
SOURCE_MAPPING = {
    "监控巡查情况": {
        "source_label": "监控巡查情况",
        "default_inspection_type": "监控巡查情况",
    },
    "巡查信息": {
        "source_label": "高管驻场报告",
        # 巡查类型 来自每条记录的 "巡查类别"
        "default_inspection_type": "高管驻场",
    },
    "统一日常值班报告": {
        "source_label": "日常值班报告",
        "default_inspection_type": "生产值班",
    },
    "每日巡查报告": {
        "source_label": "车间管理人员巡查",
        "default_inspection_type": "每日巡查",
    },
    "隐患排查": {
        "source_label": "EHS隐患排查",
        "default_inspection_type": "日常巡查",
    },
}

INPUT_KINDS = list(SOURCE_MAPPING.keys())


def make_key(date: str, factory: str, desc: str) -> str:
    """与目标文件保持一致的 key 拼接方式 (日期|厂区|事件描述)."""
    parts = [str(date) if date is not None else "",
             str(factory) if factory is not None else "",
             str(desc) if desc is not None else ""]
    return "|".join(parts)
