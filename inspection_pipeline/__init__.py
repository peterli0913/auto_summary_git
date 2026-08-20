"""inspection_pipeline: 巡查台账汇总 / 关键词筛选 / 统计工具包.

子模块:
- schema:     统一 3 字段定义 + 各类台账列名映射
- reader:     单个 Excel 智能解析 (表头行 / 日期列 / 巡查发现列 自动识别)
- collector:  收集待处理文件 (多文件上传 / ZIP 文件夹 / 服务器目录)
- database:   SQLite 存储层
- query:      关键词表达式 (& / |) 解析与筛选
- charts:     Plotly 统计图表 + PNG/HTML 导出
- exporter:   Excel 导出 (内存字节流)
"""

from . import (charts, collector, database, exporter, query, reader,  # noqa: F401
               schema)

__all__ = [
    "schema", "reader", "collector", "database", "query", "charts", "exporter",
]
