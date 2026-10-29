"""Streamlit 交互界面: 上传输入 -> 自动/人工核对分类 -> 下载输出 -> 增量训练.

启动: streamlit run app.py

部署 (Streamlit Community Cloud):
    见 DEPLOY.md
"""
from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
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
from hazard_pipeline.predict import load_model_auto, predict_dataframe
from hazard_pipeline.schema import (HAZARD_TYPES, OTHER_LABEL, OUTPUT_COLUMNS,
                                     POSITIVE_TYPES)
from hazard_pipeline.semi_supervised import (load_pseudo_labels,
                                                self_train_pipeline)
from hazard_pipeline.train import (DEFAULT_MODEL_DIR, ENHANCED_MODEL_DIR,
                                     train_pipeline)


# 仓库自带的 5 个原始输入 (用于「使用工作区默认文件」与自训练默认输入)
DEFAULT_WORKSPACE_INPUTS = [
    "监控巡查情况.xlsx", "巡查信息.xlsx", "统一日常值班报告.xlsx",
    "每日巡查报告.xlsx", "隐患排查.xlsx",
]
DEFAULT_GOLD = "跑冒滴漏与静电风险专项跟踪.xlsx"

# ---------- 页面配置 ----------
st.set_page_config(page_title="跑冒滴漏与静电风险隐患分类", layout="wide")

DATA_DIR = Path("data")
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
PSEUDO_PATH = DATA_DIR / "feedback" / "pseudo_labels.parquet"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

VARIANT_DIRS = {
    "standard": DEFAULT_MODEL_DIR,
    "enhanced": ENHANCED_MODEL_DIR,
}
VARIANT_LABELS = {
    "standard": "标准模型 (TF-IDF + LR/SVC, 快, 100MB)",
    "enhanced": "加强模型 (TF-IDF + 中文 SBERT 拼接, 略慢, ~150MB)",
}


# ---------- 工具函数 ----------

@st.cache_resource(show_spinner=False)
def load_model_cached(model_dir: str):
    p = Path(model_dir)
    if not p.exists():
        return None
    return load_model_auto(p)


def reset_model_cache():
    load_model_cached.clear()


def _model_trained(variant: str) -> bool:
    p = VARIANT_DIRS[variant] / "hazard" / "meta.json"
    return p.exists()


def bootstrap_default_model():
    """Cloud 友好启动: 绝不在 import / 首次渲染时做重训练.

    之前的实现会在首次启动时跑 train_pipeline (含 5-fold OOF),
    在 Streamlit Community Cloud 1GB 内存上极易 OOM / 超时,
    导致页面一直停在 "Deploying…".

    现在策略:
      1. 若仓库已提交 models/enhanced → 直接可用, 什么都不做
      2. 若 models/current 已存在 → 什么都不做
      3. 否则仅提示用户去「模型管理」面板点按钮训练
         (不在启动路径上阻塞)
    """
    if _model_trained("enhanced") or _model_trained("standard"):
        return False
    return False  # 永不自动重训; 留给 UI 按钮


def has_enhanced_deps() -> bool:
    """检查 sentence-transformers 与 torch 是否都已可用."""
    try:
        importlib.import_module("sentence_transformers")
        importlib.import_module("torch")
        return True
    except Exception:
        return False


def install_enhanced_deps() -> tuple[bool, str]:
    """运行时安装 torch (cpu) + sentence-transformers."""
    logs = []
    # 先装 cpu 版 torch (节省 600MB)
    cmd1 = [sys.executable, "-m", "pip", "install",
            "--quiet", "--no-cache-dir",
            "--index-url", "https://download.pytorch.org/whl/cpu",
            "torch"]
    p1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=900)
    logs.append("[torch] " + (p1.stdout or "") + (p1.stderr or ""))
    if p1.returncode != 0:
        return False, "\n".join(logs)
    cmd2 = [sys.executable, "-m", "pip", "install",
            "--quiet", "--no-cache-dir", "sentence-transformers"]
    p2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=900)
    logs.append("[sentence-transformers] " + (p2.stdout or "") + (p2.stderr or ""))
    if p2.returncode != 0:
        return False, "\n".join(logs)
    importlib.invalidate_caches()
    return True, "\n".join(logs)


def parse_uploaded_train_xlsx(uploaded_files) -> pd.DataFrame:
    """解析用户上传的标注 xlsx, 提取 [事件描述, 隐患类型, 分类] 三列."""
    rows = []
    for uf in uploaded_files:
        try:
            df = pd.read_excel(uf)
        except Exception:
            continue
        if "事件描述" not in df.columns or "隐患类型" not in df.columns:
            continue
        df = df.dropna(subset=["事件描述", "隐患类型"]).copy()
        if "分类" not in df.columns:
            df["分类"] = "其他"
        df["分类"] = df["分类"].fillna("其他").astype(str)
        for _, r in df.iterrows():
            rows.append({
                "事件描述": str(r["事件描述"]),
                "隐患类型": str(r["隐患类型"]),
                "分类": str(r["分类"]),
            })
    return pd.DataFrame(rows)


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
if "bootstrapped" not in st.session_state:
    # 不阻塞启动; 仅记录可用模型状态
    try:
        bootstrap_default_model()
        if _model_trained("enhanced"):
            st.session_state.bootstrapped = "enhanced ready (pre-committed)"
        elif _model_trained("standard"):
            st.session_state.bootstrapped = "standard ready"
        else:
            st.session_state.bootstrapped = "no model yet — use 模型管理 panel"
    except Exception as e:
        st.session_state.bootstrapped = f"bootstrap skipped: {e}"


# ---------- 侧栏 ----------
with st.sidebar:
    st.header("⚙️ 设置")

    st.subheader("🧠 模型变体")
    # Cloud 默认用 standard: 仓库已提交 11MB 模型, 无需 torch, 秒开.
    # enhanced 需要额外装 sentence-transformers (在「模型管理」里一键装).
    _variant_options = ["standard", "enhanced"]
    _default_variant_idx = 0
    variant_choice = st.radio(
        "选择模型",
        options=_variant_options,
        index=_default_variant_idx,
        format_func=lambda v: VARIANT_LABELS[v],
        help="加强模型用 sentence-transformer 中文 embedding + TF-IDF 拼接, "
             "在 holdout 上 其他→正类 错误更少, 但推理速度略慢. "
             "Cloud 上 enhanced 需先在「模型管理」安装依赖.",
    )
    model_dir = str(VARIANT_DIRS[variant_choice])
    model = None
    load_err = None
    try:
        if Path(model_dir).exists():
            # enhanced 变体需要 torch/sentence-transformers; 未装时给出友好提示
            if variant_choice == "enhanced" and not has_enhanced_deps():
                load_err = ("enhanced 依赖未安装. 请展开主区「🛠 模型管理」→ "
                            "点「📦 安装加强模型依赖」, 或先切到 standard.")
            else:
                model = load_model_cached(model_dir)
    except Exception as e:
        load_err = f"加载失败: {e}"

    if model is None:
        st.error(load_err or (
            f"模型 ({variant_choice}) 未就绪. 请展开主区「🛠 模型管理」训练, "
            f"或确认部署分支是 `cursor/hazard-classifier-pipeline-3238` "
            f"(main 分支没有 app.py)."
        ))
    else:
        st.success(f"已加载: {variant_choice}")
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
    ps = load_pseudo_labels(PSEUDO_PATH)
    n_pseudo = len(ps) if ps is not None else 0
    st.caption(f"伪标签数: {n_pseudo} 条")

    st.divider()
    use_pseudo = st.checkbox("重训时合并伪标签 (半监督)", value=(n_pseudo > 0),
                               help="把模型自己高置信度的样本作为额外训练数据")
    if st.button("🔁 用最新反馈/伪标签重训当前变体", use_container_width=True):
        with st.spinner("正在训练..."):
            metrics = train_pipeline(
                model_dir=Path(model_dir),
                variant=variant_choice,
                pseudo_path=(PSEUDO_PATH if use_pseudo else None),
            )
            reset_model_cache()
            st.session_state["last_metrics"] = metrics
        st.success("重训完成. 请刷新页面.")

    st.divider()
    st.subheader("🤖 半监督: 自动生成伪标签")
    haz_conf = st.slider("隐患 top1 置信度阈值", 0.5, 0.99, 0.92, 0.01)
    sub_conf = st.slider("分类 top1 置信度阈值", 0.5, 0.99, 0.85, 0.01)
    if st.button("🌱 用当前模型 + 当前已上传输入打伪标签",
                  use_container_width=True,
                  disabled=(model is None or not st.session_state.uploaded_paths)):
        with st.spinner("打伪标签中..."):
            info = self_train_pipeline(
                st.session_state.uploaded_paths, model,
                haz_conf=haz_conf, sub_conf=sub_conf,
                pseudo_path=PSEUDO_PATH, use_rules=True,
            )
        st.success(
            f"已生成 {info['n_confident']} 条高置信伪标签 "
            f"(共聚合 {info['n_total']}). 写入 {info['n_written']} 条."
        )
        st.json(info["by_hazard"])

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

# ---------- 主区: 模型管理 (折叠) ----------
with st.expander("🛠 模型管理 (训练 / 安装加强模型 / 上传新训练集)", expanded=False):
    st.write("**当前模型状态**")
    cols_m = st.columns(2)
    with cols_m[0]:
        st.write(f"`standard`: " + ("✅ 已训练" if _model_trained("standard") else "❌ 未训练"))
        if not _model_trained("standard"):
            if st.button("🧠 训练标准模型", key="train_std"):
                with st.spinner("训练 standard 模型..."):
                    train_pipeline(variant="standard", model_dir=DEFAULT_MODEL_DIR)
                reset_model_cache()
                st.success("完成. 请刷新页面.")
    with cols_m[1]:
        deps_ok = has_enhanced_deps()
        enh_trained = _model_trained("enhanced")
        st.write(f"`enhanced` 依赖 (torch + sentence-transformers): "
                  + ("✅ 已安装" if deps_ok else "❌ 未安装"))
        st.write(f"`enhanced` 模型: " + ("✅ 已训练" if enh_trained else "❌ 未训练"))
        if not deps_ok:
            if st.button("📦 安装加强模型依赖 (~3-5 分钟, 一次性)",
                          key="install_enh"):
                with st.spinner("安装 torch (cpu) + sentence-transformers ..."):
                    ok, log = install_enhanced_deps()
                if ok:
                    st.success("依赖安装完成. 请点击页面右上角的 ⋮ → Rerun, 或按 R 键.")
                    st.code(log[-800:] if log else "", language="bash")
                else:
                    st.error("安装失败. 部分日志:")
                    st.code(log[-2000:] if log else "", language="bash")
                    st.info(
                        "如果在 Streamlit Cloud 上无法运行时安装, 可改在 "
                        "`requirements.txt` 中取消 `sentence-transformers` 与 `torch` "
                        "两行的注释, 重新部署即可."
                    )
        else:
            label = "🧠 训练加强模型 (~2 分钟)" if not enh_trained else "🔁 重训加强模型"
            if st.button(label, key="train_enh"):
                with st.spinner("训练 enhanced 模型 (会下载 SBERT 权重 ~120MB)..."):
                    train_pipeline(variant="enhanced", model_dir=ENHANCED_MODEL_DIR)
                reset_model_cache()
                st.success("加强模型训练完成. 请在侧栏切换为 enhanced.")

    st.divider()
    st.write("**📤 上传新训练数据 (与原训练集合并后重训)**")
    st.caption(
        "上传任意数量的 Excel, 文件需包含列 `事件描述` 与 `隐患类型` (可选 `分类`). "
        "上传后会写入人工反馈库 (data/feedback/labels.parquet), "
        "重训时与原始 `跑冒滴漏与静电风险专项跟踪.xlsx` + 已有反馈合并 (按事件描述去重, 反馈优先)."
    )
    upl_train = st.file_uploader(
        "选择训练数据 Excel", type=["xlsx", "xls"],
        accept_multiple_files=True, key="train_upload",
    )
    train_variant_target = st.radio(
        "重训目标",
        options=[v for v in ["standard", "enhanced"]
                 if v == "standard" or has_enhanced_deps()],
        horizontal=True, key="retrain_variant_target",
        help="enhanced 只有在依赖已安装后才会出现",
    )
    if upl_train and st.button("🔁 合并并重训", key="merge_retrain"):
        new_df = parse_uploaded_train_xlsx(upl_train)
        if not len(new_df):
            st.error("没有解析到有效数据 (需含 `事件描述` + `隐患类型` 两列)")
        else:
            st.info(f"解析到 {len(new_df)} 条新标注. 写入反馈库 ...")
            with st.expander("查看新标注隐患分布"):
                st.write(new_df["隐患类型"].value_counts())
            n = append_feedback(
                new_df.assign(source="uploaded_train").to_dict("records"),
                DEFAULT_FB_PATH,
            )
            st.success(f"已写入 {n} 条到 {DEFAULT_FB_PATH}.")
            target_dir = VARIANT_DIRS[train_variant_target]
            with st.spinner(f"用合并数据重训 {train_variant_target} ..."):
                metrics = train_pipeline(
                    variant=train_variant_target, model_dir=target_dir,
                    pseudo_path=(PSEUDO_PATH if PSEUDO_PATH.exists() else None),
                )
            reset_model_cache()
            st.success("重训完成 ✅")
            st.json({k: v for k, v in metrics.items()
                      if k in {"hazard_accuracy", "hazard_macro_f1",
                               "分类_overall_accuracy", "n_train", "n_test"}})

    st.divider()
    st.write("**🌱 半监督自训练 (用工作区默认 5 输入打伪标签 + 重训)**")
    st.caption("基于当前模型对仓库自带 5 个原始 xlsx 打高置信伪标签, 然后合并重训当前选中的变体.")
    if st.button("🌱 一键自训练 (打伪标签 + 重训)", key="self_train_btn",
                  disabled=(model is None)):
        existing = [p for p in DEFAULT_WORKSPACE_INPUTS if Path(p).exists()]
        if not existing:
            st.error("仓库根目录未找到默认输入文件")
        else:
            with st.spinner("打伪标签..."):
                info = self_train_pipeline(
                    existing, model,
                    pseudo_path=PSEUDO_PATH, use_rules=True,
                )
            st.success(
                f"伪标签生成: {info['n_confident']} / {info['n_total']} 条 "
                f"(类型分布: {info['by_hazard']})"
            )
            with st.spinner(f"用伪标签重训 {variant_choice} ..."):
                train_pipeline(
                    variant=variant_choice, model_dir=Path(model_dir),
                    pseudo_path=PSEUDO_PATH,
                )
            reset_model_cache()
            st.success("重训完成 ✅. 请刷新页面看新指标.")


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
metrics_path = Path(model_dir) / "metrics.json"
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
