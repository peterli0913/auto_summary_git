"""统计图表: Plotly 绘制 + PNG/HTML 导出.

用 Plotly 而不是 matplotlib 的原因: 浏览器端渲染中文无需额外字体,
导出 PNG 时通过 kaleido; 若云端环境没有 kaleido/Chrome,
自动降级成 HTML (自带交互, 中文同样正常).
"""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd
import plotly.graph_objects as go

CHART_TYPES = ["竖向条形图", "横向条形图", "饼图", "环形图"]
_COLORWAY = ["#4C6FFF", "#00B8A9", "#F6A623", "#EF476F", "#8367C7",
             "#2EC4B6", "#FF9F1C", "#7A5195", "#3A86FF", "#06D6A0"]


def make_chart(counts: pd.DataFrame,
               chart_type: str = "竖向条形图",
               title: str = "关键词统计",
               show_values: bool = True) -> go.Figure:
    """counts 需含 '关键词' 与 '条目数' 两列."""
    labels = counts["关键词"].astype(str).tolist()
    values = counts["条目数"].astype(int).tolist()
    text = [str(v) for v in values] if show_values else None

    if chart_type == "横向条形图":
        fig = go.Figure(go.Bar(
            x=values, y=labels, orientation="h",
            text=text, textposition="outside",
            marker_color=_COLORWAY[0],
        ))
        fig.update_layout(xaxis_title="条目数", yaxis_title="关键词",
                          yaxis=dict(autorange="reversed"))
    elif chart_type in ("饼图", "环形图"):
        fig = go.Figure(go.Pie(
            labels=labels, values=values,
            hole=0.45 if chart_type == "环形图" else 0.0,
            textinfo="label+value+percent" if show_values else "label+percent",
            marker=dict(colors=_COLORWAY[:max(1, len(labels))]),
        ))
    else:  # 竖向条形图
        fig = go.Figure(go.Bar(
            x=labels, y=values,
            text=text, textposition="outside",
            marker_color=_COLORWAY[0],
        ))
        fig.update_layout(xaxis_title="关键词", yaxis_title="条目数")

    fig.update_layout(
        title=title,
        template="plotly_white",
        colorway=_COLORWAY,
        margin=dict(l=60, r=40, t=70, b=80),
        height=520,
        uniformtext_minsize=10,
        uniformtext_mode="hide",
    )
    return fig


def fig_to_png(fig: go.Figure, width: int = 1200, height: int = 650,
               scale: int = 2) -> Tuple[Optional[bytes], Optional[str]]:
    """尝试导出 PNG. 返回 (bytes, 错误信息); 失败时 bytes 为 None."""
    try:
        data = fig.to_image(format="png", width=width, height=height, scale=scale)
        return data, None
    except Exception as exc:  # noqa: BLE001 - 云端可能缺 kaleido/Chrome
        return None, f"{type(exc).__name__}: {exc}"


def fig_to_html(fig: go.Figure) -> bytes:
    """导出自包含 HTML (任何环境都能成功, 双击即可在浏览器打开)."""
    html = fig.to_html(include_plotlyjs="cdn", full_html=True)
    return html.encode("utf-8")
