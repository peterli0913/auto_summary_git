"""hazard_pipeline: 工业级隐患汇总与分类工具包.

子模块:
- schema:        统一字段、类别常量
- aggregator:    5 类输入 -> 统一格式 DataFrame
- rules:         高置信关键词规则
- text_features: 中文 jieba + TF-IDF 特征
- classifier:    两级分类模型
- train:         训练 / 评估
- predict:       推理
- excel_writer:  按目标格式输出 Excel
- feedback:      人工反馈持久化 + 增量再训练
"""

__all__ = [
    "schema",
    "aggregator",
    "rules",
    "text_features",
    "classifier",
    "train",
    "predict",
    "excel_writer",
    "feedback",
]
