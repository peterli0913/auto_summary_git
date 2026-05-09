"""Streamlit 交互界面: 上传输入 -> 自动/人工核对分类 -> 下载输出 -> 增量训练.

启动: streamlit run app.py
"""
from __future__ import annotations

import io
import json
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from hazard_pipeline.aggregator import aggregate_files
from hazard_pipeline.classifier import FullClassifier
from hazard_pipeline.excel_writer import write_output
from hazard_pipeline.feedback import (DEFAULT_FB_PATH, append_feedback,
                                       feedback_summary)
from hazard_pipeline.predict import predict_dataframe
from hazard_pipeline.schema import (HAZARD_TYPES, OTHER_LABEL, OUTPUT_COLUMNS,
                                     POSITIVE_TYPES)
from hazard_pipeline.train import train_pipeline

# ---------- 页面配置 ----------
st.set_page_config(page_title="跑冒滴漏与静电风险隐患分类", layout="wide")

DATA_DIR = Path("data")
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
MODEL_DIR = Path("models/current")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------- 工具函数 ----------

@st.cache_resource(show_spinner=False)
def load_model(model_dir: str = str(MODEL_DIR)):
    p = Path(model_dir)
    if not p.exists():
        return None
    return FullClassifier.load(p)


def reset_model_cache():
    load_model.clear()


def save_uploads(uploaded_files) -> list:
    """把上传到 streamlit 的文件落到磁盘并返回路径列表."""
    paths = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sub = UPLOAD_DIR / ts
    sub.mkdir(parents=True, exist_ok=True)
    for uf in uploaded_files:
        if uf is None:
            continue
        p = sub / uf.name
        p.write_bytes(uf.getbuffer())
        paths.append(p)
    return paths


# ---------- 状态初始化 ----------
if "df_pred" not in st.session_state:
    st.session_state.df_pred = None
if "human_corrections" not in st.session_state:
    st.session_state.human_corrections = {}  # {row_index: {隐患类型, 分类}}
if "uploaded_paths" not in st.session_state:
    st.session_state.uploaded_paths = []


# ---------- 侧栏 ----------
with st.sidebar:
    st.header("⚙️ 设置")
    model = load_model()
    if model is None:
        st.error("模型未训练. 请先运行: `python scripts/train_initial.py` 或在下方训练.")
    else:
        modes = list(model.hazard.threshold_modes.keys()) if model.hazard.threshold_modes else ["default"]
        default_idx = modes.index(model.hazard.default_mode) if model.hazard.default_mode in modes else 0
        chosen_mode = st.selectbox("阈值模式 (隐患类型)",
                                    options=modes, index=default_idx,
                                    help="balanced=最优整体准确率; strict=正类→其他<1%; balanced_strict=同时满足两约束")
        if chosen_mode in (model.hazard.threshold_modes or {}):
            model.hazard.set_mode(chosen_mode)
            info = model.hazard.threshold_modes[chosen_mode]
            st.caption(f"thr={info['threshold']:.3f}  "
                       f"OOF: 正类→其他={info['pos_to_other']*100:.2f}%  "
                       f"其他→正类={info['other_to_pos']}")

    use_rules = st.checkbox("启用关键词规则后处理", value=True,
                             help="规则可在小样本场景帮助提升 recall, 也可保证特殊高置信句子归类正确")

    work_mode = st.radio("工作模式",
                         ["自动分类 (全自动)", "人工核对 (低置信交互)"],
                         index=0)

    uncertain_thr = st.slider("人工核对触发阈值: 隐患 top1-top2 概率差", 0.0, 0.5, 0.20, 0.01)
    sub_uncertain_thr = st.slider("人工核对触发阈值: 分类 top1 概率", 0.3, 0.95, 0.55, 0.01)

    st.divider()
    st.subheader("📚 反馈数据状态")
    fb = feedback_summary(DEFAULT_FB_PATH)
    st.caption(f"已积累人工标签: {fb['count']} 条")
    if fb["count"]:
        st.caption(f"按隐患类型: {fb.get('by_hazard', {})}")

    st.divider()
    if st.button("🔁 用最新反馈重训模型", use_container_width=True):
        with st.spinner("正在训练..."):
            metrics = train_pipeline()
            reset_model_cache()
            st.session_state["last_metrics"] = metrics
        st.success("重训完成. 请刷新或重新上传数据.")

# ---------- 主区: 上传 + 运行 ----------
st.title("🏭 跑冒滴漏 / 静电 / 化学品暴露 隐患汇总分类系统")
st.caption("输入: 5 类原始 Excel; 输出: 与 跑冒滴漏与静电风险专项跟踪.xlsx 同格式的汇总文件")

st.subheader("1. 上传输入文件")
st.caption("可上传任意子集. 系统会自动识别每个文件的类型 (监控巡查情况 / 巡查信息 / 统一日常值班报告 / 每日巡查报告 / 隐患排查).")
uploaded = st.file_uploader("选择 .xlsx 文件 (支持多选)",
                              type=["xlsx", "xls"], accept_multiple_files=True)

col_a, col_b, col_c = st.columns([1, 1, 4])
with col_a:
    use_workspace = st.checkbox("使用工作区默认 5 个文件", value=False,
                                 help="勾选后忽略上传, 使用仓库根目录中的输入文件")

with col_b:
    run_btn = st.button("🚀 汇总并分类", type="primary", use_container_width=True)

if run_btn:
    if model is None:
        st.error("模型未加载, 无法运行")
    else:
        if use_workspace:
            paths = [Path("监控巡查情况.xlsx"), Path("巡查信息.xlsx"),
                     Path("统一日常值班报告.xlsx"), Path("每日巡查报告.xlsx"),
                     Path("隐患排查.xlsx")]
            paths = [p for p in paths if p.exists()]
        else:
            paths = save_uploads(uploaded or [])
        if not paths:
            st.warning("请上传至少一个文件 (或勾选使用默认文件).")
        else:
            with st.spinner(f"汇总 {len(paths)} 个文件 + 分类中..."):
                df = aggregate_files(paths)
                df_pred = predict_dataframe(df, model, use_rules=use_rules)
            st.session_state.df_pred = df_pred
            st.session_state.uploaded_paths = [str(p) for p in paths]
            st.session_state.human_corrections = {}
            st.success(f"完成! 共 {len(df_pred)} 条 (去重后).")


# ---------- 主区: 结果展示 + 人工核对 ----------
df_pred = st.session_state.df_pred
if df_pred is not None and len(df_pred):
    st.subheader("2. 分类结果")
    c1, c2, c3, c4 = st.columns(4)
    counts = df_pred["隐患类型"].value_counts()
    c1.metric("总条数", len(df_pred))
    c2.metric("跑冒滴漏", int(counts.get("跑冒滴漏", 0)))
    c3.metric("静电事件", int(counts.get("静电事件", 0)))
    c4.metric("化学品暴露", int(counts.get("化学品暴露", 0)))

    st.write("**隐患类型分布**")
    st.bar_chart(counts)

    st.write("**预测结果预览** (前 200 条)")
    show_cols = ["来源", "巡查类型", "日期", "厂区", "属地", "责任区域",
                 "事件描述", "隐患类型", "分类",
                 "hazard_top1_prob", "sub_top1_prob"]
    show_cols = [c for c in show_cols if c in df_pred.columns]
    st.dataframe(df_pred[show_cols].head(200), height=350, use_container_width=True)

    # ---------- 人工核对 ----------
    if work_mode.startswith("人工核对"):
        st.subheader("3. 人工核对低置信样本")
        # 重新用当前阈值算 uncertain mask
        haz_diff = (df_pred["hazard_top1_prob"] - df_pred["hazard_top2_prob"]).abs()
        haz_uncertain = haz_diff < uncertain_thr
        sub_uncertain = df_pred["sub_top1_prob"] < sub_uncertain_thr
        uncertain_mask = haz_uncertain | sub_uncertain
        uncertain_df = df_pred[uncertain_mask]
        st.caption(f"共 {len(uncertain_df)} 条需要人工核对 (隐患不确定: {int(haz_uncertain.sum())},  分类不确定: {int(sub_uncertain.sum())})")

        max_show = st.slider("一次核对样本数上限", 5, 200, 30, 5)
        for i, (idx, row) in enumerate(uncertain_df.head(max_show).iterrows()):
            with st.expander(
                f"#{idx}  来源={row['来源']}  日期={row['日期']}  "
                f"模型: 隐患={row['隐患类型']} ({row['hazard_top1_prob']:.2f}) / "
                f"分类={row['分类']} ({row['sub_top1_prob']:.2f})",
                expanded=(i < 3),
            ):
                st.markdown(f"**事件描述**: {row['事件描述']}")
                # 隐患类型选择
                haz_options = HAZARD_TYPES
                # 显示概率排名
                p_other = row.get("p_hazard_其他", None)
                proba_text = []
                for cls in HAZARD_TYPES:
                    col = f"p_hazard_{cls}"
                    if col in row.index:
                        proba_text.append(f"{cls}={row[col]:.3f}")
                st.caption("候选概率: " + "  |  ".join(proba_text))

                cur_haz = st.session_state.human_corrections.get(int(idx), {}).get("隐患类型", row["隐患类型"])
                cur_sub = st.session_state.human_corrections.get(int(idx), {}).get("分类", row["分类"])
                colh, cols = st.columns(2)
                with colh:
                    new_haz = st.selectbox("人工标定 隐患类型",
                                            options=haz_options,
                                            index=haz_options.index(cur_haz)
                                                  if cur_haz in haz_options else 3,
                                            key=f"haz_{idx}")
                with cols:
                    # 子类: 给出 top-3 + 其它, 也允许自定义
                    cls_list = ["其他"]
                    sub_top1 = row.get("sub_top1_label")
                    sub_top2 = row.get("sub_top2_label")
                    for cand in [sub_top1, sub_top2]:
                        if cand and cand not in cls_list:
                            cls_list.append(cand)
                    if cur_sub not in cls_list:
                        cls_list.append(cur_sub)
                    new_sub = st.selectbox("人工标定 分类", options=cls_list,
                                            index=cls_list.index(cur_sub)
                                                  if cur_sub in cls_list else 0,
                                            key=f"sub_{idx}")
                    new_sub_custom = st.text_input("(可选) 自定义分类",
                                                    key=f"subc_{idx}")
                    if new_sub_custom.strip():
                        new_sub = new_sub_custom.strip()
                if (new_haz, new_sub) != (row["隐患类型"], row["分类"]):
                    st.session_state.human_corrections[int(idx)] = {
                        "隐患类型": new_haz, "分类": new_sub,
                    }
        if st.button("应用人工核对结果到分类"):
            for idx, corr in st.session_state.human_corrections.items():
                df_pred.at[idx, "隐患类型"] = corr.get("隐患类型", df_pred.at[idx, "隐患类型"])
                df_pred.at[idx, "分类"] = corr.get("分类", df_pred.at[idx, "分类"])
            st.session_state.df_pred = df_pred
            st.success(f"已应用 {len(st.session_state.human_corrections)} 条修改.")

    # ---------- 下载 + 反馈 ----------
    st.subheader("4. 下载结果 / 保存反馈")
    out_df = df_pred[[c for c in OUTPUT_COLUMNS if c in df_pred.columns]].copy()
    out_path = OUTPUT_DIR / f"跑冒滴漏与静电风险专项跟踪_输出_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    if st.button("📥 生成输出 Excel"):
        write_output(out_df, out_path)
        st.success(f"已生成: {out_path}")
        with open(out_path, "rb") as f:
            st.download_button("点击下载 Excel", data=f.read(),
                                file_name=out_path.name,
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if work_mode.startswith("人工核对") and st.session_state.human_corrections:
        if st.button("💾 把人工核对的样本加入训练数据 (用于下次重训)"):
            records = []
            for idx, corr in st.session_state.human_corrections.items():
                row = df_pred.loc[idx]
                records.append({
                    "事件描述": row["事件描述"],
                    "隐患类型": corr.get("隐患类型", row["隐患类型"]),
                    "分类": corr.get("分类", row["分类"]),
                    "source": "human",
                })
            n = append_feedback(records)
            st.success(f"已写入 {n} 条人工反馈到 {DEFAULT_FB_PATH}")

# ---------- 显示训练指标 ----------
metrics_path = MODEL_DIR / "metrics.json"
if metrics_path.exists():
    with st.expander("📊 训练评估指标 (10% 测试集)", expanded=False):
        m = json.loads(metrics_path.read_text())
        col1, col2 = st.columns(2)
        col1.metric("隐患类型准确率", f"{m.get('hazard_accuracy', 0):.2%}")
        col1.metric("隐患 macro F1", f"{m.get('hazard_macro_f1', 0):.3f}")
        col1.metric("分类总体准确率", f"{m.get('分类_overall_accuracy', 0):.2%}")
        col2.metric("正类→其他 错分率", f"{m.get('正类→其他_错分率', 0):.2%}",
                     help="目标 < 1%")
        col2.metric("其他→正类 错分数", m.get('其他→正类_错分数', 0),
                     help=f"测试集 (10%); 目标 ≤ 5% × 正类数")
        col2.metric("其他→正类 / 正类比例", f"{m.get('其他→正类_占正类比例', 0):.2%}")
        st.write("**阈值模式 (训练时记录)**:")
        for mode, info in (m.get("threshold_modes") or {}).items():
            st.write(f"- `{mode}`: thr={info['threshold']:.3f}  "
                     f"OOF 正类→其他={info['pos_to_other']*100:.2f}%  "
                     f"其他→正类={info['other_to_pos']}")
        st.write("**Confusion Matrix (rows=真, cols=预测)**:")
        cm = m.get("hazard_confusion") or []
        if cm:
            st.dataframe(pd.DataFrame(cm, index=HAZARD_TYPES, columns=HAZARD_TYPES))
