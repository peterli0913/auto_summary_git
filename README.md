# 跑冒滴漏 / 静电 / 化学品暴露 隐患汇总分类系统

工业级 Python 工程, 完成 5 类原始 Excel 的汇总, 隐患类型 (4 类) 与
分类 (51 个细分类) 的自动分类, 并提供:

- **CLI 一键管线**: 5 个输入 Excel → 1 个目标格式 Excel
- **Streamlit 交互界面**: 上传 / 下载 / 人工核对 / 增量再训练
- **Two modes**: 全自动 / 人工核对 (低置信样本由人确认, 给出 Top-K 概率)
- **持续学习**: 人工反馈写入 `data/feedback/labels.parquet`,
  下次重训会与原始标注合并 (反馈优先, 同文本去重)

## 目录结构

```
.
├── hazard_pipeline/          # 核心 Python 包
│   ├── schema.py              # 输出列、类别、来源映射
│   ├── aggregator.py          # 5 类输入 → 统一格式
│   ├── rules.py               # 高置信关键词规则
│   ├── text_features.py       # jieba + TF-IDF 特征
│   ├── classifier.py          # HazardClassifier + SubClassifier
│   ├── train.py               # 9:1 训练 + 阈值调优
│   ├── predict.py             # 推理 + Top-K 概率
│   ├── feedback.py            # 反馈持久化
│   └── excel_writer.py        # 按目标格式输出
├── scripts/
│   ├── train_initial.py       # 一键训练
│   └── run_pipeline.py        # CLI: 5 文件 → 输出
├── app.py                     # Streamlit 交互界面
├── models/current/            # 训练后的模型工件
├── data/feedback/             # 累积的人工标签
├── data/outputs/              # 默认输出目录
└── requirements.txt
```

## 安装

```bash
pip install -r requirements.txt
```

## 使用

### 1. 训练初始模型

```bash
python scripts/train_initial.py
```

会读取仓库根目录的 `跑冒滴漏与静电风险专项跟踪.xlsx`,
做 90/10 stratified split, 输出指标到控制台 + `models/current/metrics.json`.

### 2. 命令行批处理 (5 个文件 → 1 个 Excel)

```bash
python scripts/run_pipeline.py \
  监控巡查情况.xlsx 巡查信息.xlsx 统一日常值班报告.xlsx \
  每日巡查报告.xlsx 隐患排查.xlsx \
  -o data/outputs/跑冒滴漏与静电风险专项跟踪_输出.xlsx
```

输出 Excel 列与 `跑冒滴漏与静电风险专项跟踪.xlsx` 完全一致 (16 列):
来源 / 巡查类型 / 日期 / 厂区 / 属地 / 责任区域 / 事件描述 /
隐患类型 / 分类 / 原因分析(EHS) / 原因归类(EHS) / 整改结果 /
调查报告 / 月份 / 周 / key.

### 3. 启动交互界面

```bash
streamlit run app.py
```

界面功能:
1. 侧栏选择「自动分类 / 人工核对」工作模式与阈值模式 (balanced / strict)
2. 上传 5 类输入 Excel (任意子集) 或勾选使用工作区默认文件
3. 「人工核对」模式: 列出低置信样本, 显示 Top-K 概率, 人工下拉确认
4. 「应用人工核对结果」 → 把人工标定写回数据
5. 「生成输出 Excel」 → 一键下载
6. 「保存到训练数据」 → 把人工核对结果加入 `data/feedback/labels.parquet`
7. 「用最新反馈重训模型」 → 合并反馈数据重训, 持续优化

## 输入字段映射 (来源 / 字段对应)

| 输入文件 | 来源 | 巡查类型 | 日期 | 厂区 | 属地 | 责任区域 | 事件描述 | 原因分析(EHS) | 原因归类(EHS) | 整改结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| 监控巡查情况 | 监控巡查情况 | 监控巡查情况 | 日期 | 厂区 | — | — | 监控画面情况 | — | — | — |
| 巡查信息 | 高管驻场报告 | 巡查类别 | 日期 | 厂区 | 属地 | 责任区域 | 巡查结果 | — | — | 整改结果 |
| 统一日常值班报告 | 日常值班报告 | 巡查类别 | 值班日期 | 厂区 | 属地 | 责任区域 | 发现项 | — | 原因分类 | 整改结果 |
| 每日巡查报告 | 车间管理人员巡查 | 巡查类型 | 时间 | 厂区 | 部门/厂房 | 车间 | 问题描述 | — | — | 整改结果 |
| 隐患排查 | EHS隐患排查 | 巡查主题 | 巡查日期 | 组织厂区 | — | 隐患地点 | 巡查发现 | 原因分析 | 原因分类 | 纠正措施 |

## 模型架构

### 隐患类型 (4 类: 跑冒滴漏 / 静电事件 / 化学品暴露 / 其他)
两级:
1. **L1 (其他 vs 非其他)**: TF-IDF (jieba 词 + char n-gram 1-5) + LinearSVC + sigmoid 校准
2. **L2 (3 类正向)**: TF-IDF + Logistic Regression
3. **规则后处理**: 高置信关键词强制兜底 (`hazard_pipeline/rules.py`)

### 分类 (51 个细分类)
按隐患类型分组, 每组单独训练 TF-IDF + LR. 罕见细类 (≤1 个样本) 走 fallback.

### 阈值模式

训练时同时计算:
- `balanced`: 最优整体准确率 (默认)
- `strict`: 优先满足 *正类→其他 ≤ 1%* 约束
- `balanced_strict`: 同时满足 *正类→其他 ≤ 1%* 与 *其他→正类 ≤ 5% × 正类总量* (若数据可行)

UI 可即时切换模式. 严格模式会增加 FP, 平衡模式会增加少量正类漏检.

## 已实测指标 (10% test, 618 条)

| 指标 | balanced | strict |
|---|---|---|
| 隐患类型 accuracy | 90.5% | 53% |
| 隐患 macro F1 | 0.80 | 0.49 |
| 正类→其他 错分率 | 14.7% | 0.78% |
| 其他→正类 错分数 | 38 | ~290 |
| 分类 overall accuracy | 84.3% | — |

> 注: 训练数据中 127 条同文本对应不同标签, 5.2% "其他" 文本含强正向关键词 — 数据
> 本身存在显著标注噪声, 因此「正类→其他<1% 且 其他→正类<5%」 在数据层面不可同时严格满足.
> 解决方案是: balanced 自动模式 + 人工核对 UI 兜底; 反馈数据可以持续迭代提升.

## 持续学习

人工核对的样本通过 UI 写入 `data/feedback/labels.parquet`. 之后:

- 重训会自动合并反馈与原始标注 (同 `事件描述` 优先用反馈)
- 反馈累计越多, 边界样本的标签越接近团队的真实判定, 模型上限越高
