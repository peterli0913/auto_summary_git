"""巡查台账汇总分析系统 (Streamlit).

三大功能:
  1. 数据汇总 —— 上传文件 / ZIP 文件夹 / 服务器目录, 自动识别并整理成
                 「序号 / 日期 / 巡查发现」数据库, 可导出 Excel
  2. 关键词筛选 —— 支持 A&B (同时含) 与 A|B (含其一), 可多级组合, 可导出 Excel
  3. 关键词统计 —— 维护关键词清单 (增/删/勾选), 生成图表并保存

启动:  streamlit run inspection_app.py
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from inspection_pipeline import charts, collector, database, exporter, query
from inspection_pipeline.reader import read_many
from inspection_pipeline.schema import (CONTENT_COL, CORE_COLUMNS, DATE_COL,
                                        KIND_COL, PLANT_CODES, PLANT_COL,
                                        SEQ_COL, SOURCE_FILE_COL,
                                        UNKNOWN_PLANT)

st.set_page_config(page_title="巡查台账汇总分析系统", page_icon="🔎", layout="wide")

DB_PATH = Path("data/inspection.db")
PREVIEW_ROWS = 500


# ──────────────────────────── 会话状态 ────────────────────────────
def init_state() -> None:
    ss = st.session_state
    ss.setdefault("db", None)              # 整理好的 DataFrame
    ss.setdefault("read_results", [])      # 每个文件的解析诊断
    ss.setdefault("collect_warnings", [])
    ss.setdefault("filtered", None)        # 筛选结果
    ss.setdefault("filter_expr_desc", "")
    ss.setdefault("keywords", [])           # 统计清单里的显示名
    ss.setdefault("kw_exprs", {})            # 显示名 -> 实际表达式
    ss.setdefault("chart_counts", None)      # 统计结果 DataFrame
    ss.setdefault("case_sensitive", False)
    ss.setdefault("loaded_from_disk", False)
    ss.setdefault("db_version", 0)      # 数据库每变一次 +1, 用来作废缓存
    ss.setdefault("_scope_cache", {})   # 范围键 -> (子集, 搜索数组)
    ss.setdefault("_count_cache", {})   # (表达式, token) -> 命中数
    ss.setdefault("filter_levels", [""])     # 多级筛选: 每级一个表达式
    ss.setdefault("filtered_levels_used", [])

    # 首次进入时自动读取上一次保存的数据库
    if ss.db is None and not ss.loaded_from_disk:
        ss.loaded_from_disk = True
        existing = database.load(DB_PATH)
        if existing is not None:
            ss.db = existing


init_state()


def set_db(df) -> None:
    """统一入口: 换数据库时同步作废搜索缓存."""
    st.session_state.db = df
    st.session_state.db_version += 1
    st.session_state._scope_cache = {}
    st.session_state._count_cache = {}


def plant_options() -> list[str]:
    """当前数据里出现过的厂区, 按约定代号顺序排列, 未识别放最后."""
    db = st.session_state.db
    if db is None or PLANT_COL not in db.columns:
        return []
    present = set(db[PLANT_COL].dropna().unique())
    out = [c for c in PLANT_CODES if c in present]
    if UNKNOWN_PLANT in present:
        out.append(UNKNOWN_PLANT)
    return out


def scope_key(plants: list[str]) -> tuple:
    """厂区范围的缓存键; 空选=全部厂区."""
    return ("ALL",) if not plants else tuple(sorted(plants))


def get_scope(plants: list[str]):
    """返回 (该厂区范围内的数据, 对应的搜索数组), 按范围缓存.

    搜索数组的预处理 (68k 条文本转小写) 只在范围或数据变化时做一次.
    """
    db = st.session_state.db
    key = (st.session_state.db_version, st.session_state.case_sensitive,
           scope_key(plants))
    cache = st.session_state._scope_cache
    if key not in cache:
        if db is None or not len(db):
            cache[key] = (db, None)
        else:
            sub = db if not plants else db[db[PLANT_COL].isin(plants)]
            hay = (query.build_haystack(
                sub[CONTENT_COL],
                case_sensitive=st.session_state.case_sensitive)
                if len(sub) else None)
            cache[key] = (sub, hay)
    return cache[key]


def cached_count(expr: str, plants: list[str]) -> int:
    """表达式在指定厂区范围内的命中数, 跨 rerun 复用.

    统计清单里每一行都要显示命中数; 不缓存的话每加一个关键词
    或勾一个框, 全部关键词都会被重新统计一遍.
    """
    key = (expr, st.session_state.db_version,
           st.session_state.case_sensitive, scope_key(plants))
    cache = st.session_state._count_cache
    if key not in cache:
        sub, hay = get_scope(plants)
        cache[key] = query.count_matches(
            sub, expr, case_sensitive=st.session_state.case_sensitive,
            haystack=hay)
    return cache[key]


def kw_expression(name: str) -> str:
    """显示名对应的实际表达式 (直接输入的关键词两者相同)."""
    return st.session_state.kw_exprs.get(name, name)


def add_keyword(name: str, expr: str) -> tuple[bool, str]:
    """加入统计清单. 返回 (是否成功, 提示)."""
    name = str(name).strip()
    expr = query.normalize(expr)
    if not name or not expr:
        return False, "名称和关键词都不能为空。"
    if name in st.session_state.keywords:
        return False, f"「{name}」已在清单中。"
    st.session_state.keywords.append(name)
    st.session_state.kw_exprs[name] = expr
    st.session_state[kw_key(name)] = True
    return True, f"已加入「{name}」。"


def kw_key(keyword: str) -> str:
    """关键词勾选状态的 session_state 键.

    直接把 checkbox 的 widget key 当作唯一状态源 —— 如果另外再存一份
    dict, 重跑时 widget 会用自己的旧值把 dict 覆盖回去,
    导致「全选 / 全不选」看起来没反应. 键里也不能带列表下标,
    否则删掉一个关键词后其余关键词的下标平移, 勾选状态会错位.
    """
    return f"kwchk::{keyword}"


def is_checked(keyword: str) -> bool:
    return bool(st.session_state.get(kw_key(keyword), True))


def set_all_checked(value: bool) -> None:
    for kw in st.session_state.keywords:
        st.session_state[kw_key(kw)] = value


# ── 重活缓存 ──────────────────────────────────────────────────────
# st.download_button 的 data= 和图表 PNG 都是"每次 rerun 都会重新算"的:
# 68k 行导出 Excel 约 1.8 秒、kaleido 出 PNG 约 1.2 秒, 于是随便点个勾选框
# 都要等 3 秒. 这里按内容签名缓存, 只有数据真的变了才重算.
# 下划线开头的参数不参与缓存键计算 (Streamlit 约定).

@st.cache_data(show_spinner=False, max_entries=3)
def summary_excel(_df, cols: tuple, _extra, token: tuple) -> bytes:
    return exporter.to_excel_bytes(_df[list(cols)], sheet_name="巡查汇总",
                                    extra_sheets=_extra)


@st.cache_data(show_spinner=False, max_entries=3)
def filtered_excel(_df, cols: tuple, _extra, token: tuple) -> bytes:
    return exporter.to_excel_bytes(_df[list(cols)], sheet_name="筛选结果",
                                    extra_sheets=_extra)


@st.cache_data(show_spinner=False, max_entries=3)
def counts_excel(_counts, token: tuple) -> bytes:
    return exporter.to_excel_bytes(_counts, sheet_name="关键词统计")


@st.cache_data(show_spinner=False, max_entries=3)
def chart_png(_fig, token: tuple):
    return charts.fig_to_png(_fig)


@st.cache_data(show_spinner=False, max_entries=3)
def chart_html(_fig, token: tuple) -> bytes:
    return charts.fig_to_html(_fig)


def parse_one_cached(name: str, src):
    """解析单个文件.

    这里刻意不加 st.cache_data: 缓存 68k 行数据要多占 20MB+ 内存,
    而换用 calamine 之后整批解析只要 1.5 秒左右, 缓存收益很小,
    在免费层的内存限制下不划算.
    """
    return read_many([(name, src)])


def repo_data_folders() -> list[str]:
    """列出仓库根目录里含 Excel 的子文件夹, 作为一键填入的候选."""
    out: list[str] = []
    for p in sorted(Path(".").iterdir()):
        if not p.is_dir() or p.name.startswith(".") or p.name in {
            "data", "models", "scripts", "inspection_pipeline", "hazard_pipeline",
            "__pycache__",
        }:
            continue
        if any(child.suffix.lower() in {".xlsx", ".xls", ".xlsm"}
               for child in p.iterdir() if child.is_file()):
            out.append(str(p))
    return out


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ──────────────────────────── 侧栏 ────────────────────────────
with st.sidebar:
    st.header("📚 数据库状态")
    db = st.session_state.db
    if db is None or not len(db):
        st.warning("尚无数据，请在「① 数据汇总」里上传并整理。")
    else:
        dates = pd.to_datetime(db[DATE_COL], errors="coerce")
        st.metric("总条目数", f"{len(db):,}")
        if dates.notna().any():
            st.caption(f"日期范围：{dates.min():%Y-%m-%d} ~ {dates.max():%Y-%m-%d}")
        if SOURCE_FILE_COL in db.columns:
            st.caption(f"来源文件：{db[SOURCE_FILE_COL].nunique()} 个")
        if KIND_COL in db.columns:
            with st.expander("按台账类型", expanded=False):
                st.dataframe(
                    db[KIND_COL].value_counts().rename_axis("台账类型")
                    .reset_index(name="条目数"),
                    hide_index=True, width="stretch",
                )
        if PLANT_COL in db.columns:
            with st.expander("按厂区", expanded=False):
                st.dataframe(
                    db[PLANT_COL].value_counts().rename_axis("厂区")
                    .reset_index(name="条目数"),
                    hide_index=True, width="stretch",
                )

    st.divider()
    st.subheader("⚙️ 全局选项")
    st.session_state.case_sensitive = st.checkbox(
        "关键词区分大小写", value=st.session_state.case_sensitive,
        help="中文不受影响；勾选后英文字母大小写需完全一致。",
    )

    st.divider()
    if st.button("🗑 清空数据库", width="stretch"):
        set_db(None)
        st.session_state.filtered = None
        st.session_state.chart_counts = None
        st.session_state.read_results = []
        if DB_PATH.exists():
            DB_PATH.unlink()
        st.success("已清空。")
        st.rerun()


st.title("🔎 巡查台账汇总分析系统")
st.caption(
    "支持 4 类台账自动识别：日常隐患排查（巡查发现）· 高管驻场（巡查结果）· "
    "值班巡查（发现项）· 全体人员巡查（问题描述）"
)

tab1, tab2, tab3 = st.tabs(["① 数据汇总", "② 关键词筛选", "③ 关键词统计"])


# ═══════════════════════════ 功能 1：数据汇总 ═══════════════════════════
with tab1:
    st.subheader("上传数据")
    st.caption(
        "三种方式可混用：**多选文件**（可一次框选整个文件夹里的所有 Excel）、"
        "**上传 ZIP**（把文件夹压缩后上传，会自动递归解开）、"
        "**填写文件夹路径**（本地运行或使用仓库自带数据时最方便）。"
    )
    st.info(
        "文件较多时，建议**先压缩成一个 ZIP 再上传**。xlsx 本身已是压缩格式，"
        "打包后体积只小 4% 左右，但上传请求数会从「每个文件一次」变成「只有一次」，"
        "在跨境或高延迟网络下明显更快。",
        icon="💡",
    )

    uploaded = st.file_uploader(
        "选择 Excel 文件（可多选）或 ZIP 压缩包",
        type=["xlsx", "xls", "xlsm", "zip"],
        accept_multiple_files=True,
        help="Excel 直接解析；ZIP 会递归取出里面所有 Excel。",
    )

    candidates = repo_data_folders()
    col_f1, col_f2 = st.columns([3, 2])
    with col_f1:
        folder_text = st.text_area(
            "文件夹路径（每行一个，可留空）",
            value="",
            placeholder="例如：各厂区所有巡查项---2026年1月1日至2026年7月31日",
            height=90,
        )
    with col_f2:
        if candidates:
            # 不预选任何文件夹：否则会和上面手填的路径一起被算进来，
            # 造成"多算了别的文件夹"这种难以察觉的错误
            picked = st.multiselect(
                "或从仓库已有文件夹中选择（默认不选）", options=candidates, default=[],
            )
        else:
            picked = []
            st.info("仓库根目录暂无含 Excel 的文件夹。")

    # 手填路径 + 勾选文件夹 合并去重，保持先后顺序
    folder_paths: list[str] = []
    for path in [line.strip() for line in folder_text.splitlines() if line.strip()] + picked:
        norm = str(Path(path))
        if norm not in folder_paths:
            folder_paths.append(norm)

    if folder_paths or uploaded:
        parts = []
        if uploaded:
            parts.append(f"{len(uploaded)} 个上传项")
        if folder_paths:
            parts.append("文件夹：" + "、".join(folder_paths))
        st.caption("本次将处理 —— " + "；".join(parts))

    col_b1, col_b2 = st.columns([1, 3])
    with col_b1:
        run_aggregate = st.button("🚀 开始汇总整理", type="primary",
                                   width="stretch")
    with col_b2:
        dedupe_rows = st.checkbox(
            "对完全重复的（日期 + 巡查发现）去重", value=True,
            help="不同台账之间偶有同一条记录重复录入。",
        )

    if run_aggregate:
        sources, warns = collector.collect(uploaded_files=uploaded,
                                            folder_paths=folder_paths)
        st.session_state.collect_warnings = warns
        if not sources:
            st.error("没有找到任何 Excel，请上传文件或填写正确的文件夹路径。")
        else:
            progress = st.progress(
                0.0, text=f"文件已上传完毕，开始解析 {len(sources)} 个文件…")
            frames, results = [], []
            t_start = time.perf_counter()
            last_tick = 0.0
            for i, (name, src) in enumerate(sources, start=1):
                merged_part, part_results = parse_one_cached(name, src)
                results.extend(part_results)
                if len(merged_part):
                    frames.append(merged_part)
                # 每刷新一次进度条都要往浏览器推一帧, 实测每次约 0.3 秒 ——
                # 文件多时这个开销比解析本身还大. 所以最多每 0.5 秒刷一次.
                now = time.perf_counter()
                if i == len(sources) or now - last_tick > 0.5:
                    last_tick = now
                    progress.progress(
                        i / len(sources),
                        text=f"解析中 ({i}/{len(sources)}) {Path(name).name}")
            parse_seconds = time.perf_counter() - t_start
            progress.empty()

            raw = (pd.concat(frames, ignore_index=True) if frames
                   else pd.DataFrame(columns=[DATE_COL, CONTENT_COL,
                                              SOURCE_FILE_COL, KIND_COL]))
            final = database.build_dataframe(raw, dedupe_rows=dedupe_rows)
            set_db(final)
            st.session_state.read_results = results
            st.session_state.filtered = None
            st.session_state.chart_counts = None
            try:
                database.save(final, DB_PATH)
            except Exception as exc:  # noqa: BLE001
                st.warning(f"数据库文件写入失败（不影响本次使用）：{exc}")
            st.success(
                f"完成！解析 {len(sources)} 个文件，"
                f"原始 {len(raw):,} 条 → 整理后 **{len(final):,}** 条"
                f"（解析耗时 {parse_seconds:.1f} 秒）。"
            )

    for warn in st.session_state.collect_warnings:
        st.warning(warn)

    # 解析诊断
    results = st.session_state.read_results
    if results:
        with st.expander(f"🔬 各文件识别详情（{len(results)} 个）", expanded=False):
            diag = pd.DataFrame([{
                "文件": r.file_name,
                "台账类型": r.kind,
                "表头行": (r.header_row + 1) if r.header_row is not None else None,
                "日期列": r.date_column or "—",
                "巡查发现列": r.content_column or "—",
                "厂区来源": r.plant_source or "—",
                "有效条目": r.n_valid,
                "日期解析成功": r.n_date_parsed,
                "厂区已识别": r.n_plant_known,
                "错误": r.error or "",
            } for r in results])
            st.dataframe(diag, hide_index=True, width="stretch")
            bad = [r for r in results if not r.ok]
            if bad:
                st.error(f"{len(bad)} 个文件未能解析，请检查上表「错误」列。")

    # 数据库预览 + 导出
    db = st.session_state.db
    if db is not None and len(db):
        st.divider()
        st.subheader("整理结果")
        m1, m2, m3, m4, m5 = st.columns(5)
        dates = pd.to_datetime(db[DATE_COL], errors="coerce")
        m1.metric("总条目数", f"{len(db):,}")
        m2.metric("厂区数", f"{db[PLANT_COL].nunique():,}")
        m3.metric("厂区未识别", f"{int((db[PLANT_COL] == UNKNOWN_PLANT).sum()):,}")
        m4.metric("最早日期", f"{dates.min():%Y-%m-%d}" if dates.notna().any() else "—")
        m5.metric("最晚日期", f"{dates.max():%Y-%m-%d}" if dates.notna().any() else "—")

        include_source = st.checkbox(
            "预览与导出时附带「来源文件 / 台账类型」两列", value=False,
            help="默认只输出需求要求的 序号 / 日期 / 巡查发现 三列。",
        )
        cols = list(CORE_COLUMNS) + (
            [SOURCE_FILE_COL, KIND_COL] if include_source else [])
        cols = [c for c in cols if c in db.columns]

        st.caption(f"下方仅预览前 {PREVIEW_ROWS} 条；下载的 Excel 为全部数据。")
        st.dataframe(db[cols].head(PREVIEW_ROWS), hide_index=True,
                     width="stretch", height=420)

        extra = {}
        if KIND_COL in db.columns:
            extra["按台账类型统计"] = (db[KIND_COL].value_counts()
                                     .rename_axis("台账类型")
                                     .reset_index(name="条目数"))
        if PLANT_COL in db.columns:
            extra["按厂区统计"] = (db[PLANT_COL].value_counts()
                                 .rename_axis("厂区")
                                 .reset_index(name="条目数"))
        extra = extra or None
        st.download_button(
            "📥 生成并下载汇总 Excel",
            data=summary_excel(db, tuple(cols), extra,
                               (st.session_state.db_version, tuple(cols))),
            file_name=f"巡查汇总_{len(db)}条_{timestamp()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )


# ═══════════════════════════ 功能 2：关键词筛选 ═══════════════════════════
with tab2:
    db = st.session_state.db
    if db is None or not len(db):
        st.info("请先在「① 数据汇总」里整理出数据。")
    else:
        st.subheader("关键词筛选")
        with st.expander("📖 语法说明", expanded=False):
            st.markdown(
                "- `物料` —— 「巡查发现」里包含 *物料*\n"
                "- `物料&泄漏` —— **同时**包含 *物料* 和 *泄漏*\n"
                "- `静电|接地` —— 包含 *静电* **或** *接地* 即算\n"
                "- `A&B&C` / `A|B|C` —— 支持多个 `&` 或 `|`\n"
                "- `A&B|C` —— `&` 优先于 `|`，等价于 `(A&B) | C`\n"
                "- 全角 `＆` `｜` 同样识别\n\n"
                "**多级筛选**：第 2 级在第 1 级的结果里继续筛，"
                "第 3 级在第 2 级的结果里继续筛……只填第 1 级就是普通单层筛选。"
            )

        opts = plant_options()
        sel_plants = st.multiselect(
            "厂区（不选=全部厂区）", options=opts, default=[],
            key="filter_plants",
            help="只在选中的厂区里筛选。",
        )
        scope_df, scope_hay = get_scope(sel_plants)
        scope_label = "全部厂区" if not sel_plants else "、".join(sel_plants)
        st.caption(f"当前范围：**{scope_label}**，共 {len(scope_df):,} 条")

        # ---- 多级筛选输入 ----
        levels = st.session_state.filter_levels
        for i in range(len(levels)):
            cols = st.columns([6, 1])
            with cols[0]:
                levels[i] = st.text_input(
                    f"第 {i + 1} 级关键词" + ("" if i == 0 else "（在上一级结果里继续筛）"),
                    value=levels[i], key=f"lvl_{i}",
                    placeholder="例如：阀门" if i == 0 else "例如：渗漏|管路",
                )
            with cols[1]:
                st.write("")
                if i > 0 and st.button("🗑", key=f"lvl_del_{i}", help="删除这一级"):
                    levels.pop(i)
                    st.rerun()

        b1, b2, b3 = st.columns([1.2, 1.2, 3])
        if b1.button("➕ 添加下一级", width="stretch"):
            levels.append("")
            st.rerun()
        if b2.button("↺ 重置层级", width="stretch"):
            st.session_state.filter_levels = [""]
            st.session_state.filtered = None
            st.rerun()
        do_filter = b3.button("🔍 筛选", type="primary")

        if do_filter:
            try:
                expression = query.combine_levels(levels)
                sub = query.filter_by_expression(
                    scope_df, expression,
                    case_sensitive=st.session_state.case_sensitive,
                    haystack=scope_hay)
                st.session_state.filtered = sub
                st.session_state.filter_expr_desc = expression.describe()
                st.session_state.filter_expr_raw = expression.raw
                st.session_state.filter_expr_flat = query.format_expression(expression)
                st.session_state.filtered_levels_used = [
                    query.normalize(x) for x in levels if str(x).strip()]
                st.session_state.filtered_plants = list(sel_plants)
            except query.EmptyExpression as exc:
                st.session_state.filtered = None
                st.error(str(exc))

        sub = st.session_state.filtered
        if sub is not None:
            total = len(scope_df)
            n = len(sub)
            used = st.session_state.filtered_levels_used
            c1, c2, c3 = st.columns([1, 1, 3])
            c1.metric("命中条目总数", f"{n:,}")
            c2.metric("占当前范围比例", f"{(n / total * 100 if total else 0):.2f}%")
            with c3:
                st.caption("筛选条件解释")
                st.info(
                    ("筛选层级：" + " → ".join(f"`{x}`" for x in used) + "\n\n"
                     if len(used) > 1 else "")
                    + st.session_state.filter_expr_desc
                )

            if n == 0:
                st.warning("没有命中任何条目，换个关键词试试。")
            else:
                keep = [c for c in CORE_COLUMNS if c in sub.columns]
                st.caption(f"下方仅预览前 {PREVIEW_ROWS} 条；下载的 Excel 为全部命中数据。")
                st.dataframe(sub[keep].head(PREVIEW_ROWS), hide_index=True,
                             width="stretch", height=420)

                raw_name = st.session_state.get("filter_expr_raw", "")
                safe = "".join(ch for ch in raw_name
                               if ch not in r'\/:*?"<>|') [:40] or "筛选"
                used_plants = st.session_state.get("filtered_plants") or []
                cond_sheet = {"筛选条件": pd.DataFrame([{
                    "筛选层级": " → ".join(used),
                    "等价表达式": st.session_state.get("filter_expr_flat", ""),
                    "条件解释": st.session_state.filter_expr_desc,
                    "厂区范围": "全部厂区" if not used_plants else "、".join(used_plants),
                    "命中条目数": n,
                    "范围内总条目数": total,
                }])}
                d1, d2 = st.columns([1, 1])
                with d1:
                    st.download_button(
                        "📥 生成并下载筛选结果 Excel",
                        data=filtered_excel(
                            sub, tuple(keep), cond_sheet,
                            (st.session_state.db_version,
                             st.session_state.get("filter_expr_flat", ""),
                             scope_key(used_plants), n)),
                        file_name=f"筛选_{safe}_{n}条_{timestamp()}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary", width="stretch",
                    )

                # ---- 把筛选结果加进统计清单 ----
                st.divider()
                st.markdown("**把这个筛选结果加入「③ 关键词统计」清单**")
                default_name = " → ".join(used) if used else "筛选结果"
                f1, f2 = st.columns([3, 1])
                with f1:
                    new_name = st.text_input(
                        "名称（可自定义，留空用默认）", value="",
                        placeholder=default_name, key="add_to_stats_name",
                    )
                with f2:
                    st.write("")
                    if st.button("➕ 加入统计清单", width="stretch"):
                        ok, msg = add_keyword(
                            new_name.strip() or default_name,
                            st.session_state.get("filter_expr_flat", ""))
                        (st.success if ok else st.warning)(msg)
                st.caption(
                    f"等价表达式：`{st.session_state.get('filter_expr_flat', '')}`"
                    "　（统计清单按这个表达式计数，与厂区选择相互独立）"
                )


# ═══════════════════════════ 功能 3：关键词统计 ═══════════════════════════
with tab3:
    db = st.session_state.db
    if db is None or not len(db):
        st.info("请先在「① 数据汇总」里整理出数据。")
    else:
        stat_plants = st.multiselect(
            "厂区（不选=全部厂区）", options=plant_options(), default=[],
            key="stat_plants", help="统计与图表只统计选中厂区的数据。",
        )
        stat_df, stat_hay = get_scope(stat_plants)
        stat_scope_label = "全部厂区" if not stat_plants else "、".join(stat_plants)
        st.caption(f"当前范围：**{stat_scope_label}**，共 {len(stat_df):,} 条")

        st.subheader("关键词清单")
        st.caption("同样支持 `&` 与 `|`，例如 `静电&接地`、`气味|扩散`；"
                   "「② 关键词筛选」里的多级结果也可以直接加进来。")

        with st.form("add_kw_form", clear_on_submit=True):
            fa, fb = st.columns([4, 1])
            with fa:
                new_kw = st.text_input("关键词", value="", label_visibility="collapsed",
                                        placeholder="输入关键词后点「添加」，或直接按回车")
            with fb:
                added = st.form_submit_button("➕ 添加", width="stretch")
        if added:
            kw = query.normalize(new_kw)
            ok, msg = add_keyword(kw, kw)
            if ok:
                st.rerun()
            st.warning(msg)

        keywords = st.session_state.keywords
        if not keywords:
            st.info("清单为空，先添加几个关键词吧。")
        else:
            ca, cb, cc = st.columns([1, 1, 4])
            if ca.button("全选", width="stretch"):
                set_all_checked(True)
                st.rerun()
            if cb.button("全不选", width="stretch"):
                set_all_checked(False)
                st.rerun()
            if cc.button("🗑 清空全部关键词"):
                for kw in list(keywords):
                    st.session_state.pop(kw_key(kw), None)
                st.session_state.keywords = []
                st.session_state.kw_exprs = {}
                st.session_state.chart_counts = None
                st.rerun()

            st.markdown("**勾选要在图表中显示的关键词**")
            head = st.columns([0.8, 3, 2.6, 1.4, 0.9])
            head[0].caption("显示")
            head[1].caption("名称")
            head[2].caption("表达式")
            head[3].caption(f"命中（{stat_scope_label}）")
            head[4].caption("删除")

            to_delete: str | None = None
            for kw in list(keywords):
                row = st.columns([0.8, 3, 2.6, 1.4, 0.9])
                # 不传 value: 让 checkbox 自己以 kw_key(kw) 为唯一状态源
                st.session_state.setdefault(kw_key(kw), True)
                row[0].checkbox("显示", key=kw_key(kw),
                                label_visibility="collapsed")
                expr = kw_expression(kw)
                row[1].write(kw)
                row[2].write("—" if expr == kw else f"`{expr}`")
                row[3].write(f"{cached_count(expr, stat_plants):,}")
                if row[4].button("🗑", key=f"kwdel::{kw}", help=f"删除「{kw}」"):
                    to_delete = kw

            if to_delete is not None:
                st.session_state.keywords = [
                    k for k in st.session_state.keywords if k != to_delete]
                st.session_state.kw_exprs.pop(to_delete, None)
                st.session_state.pop(kw_key(to_delete), None)
                st.session_state.chart_counts = None
                st.rerun()

            st.divider()
            st.subheader("统计图表")
            selected = [kw for kw in st.session_state.keywords if is_checked(kw)]

            g1, g2, g3 = st.columns([1.4, 1.4, 1.6])
            chart_type = g1.selectbox("图表类型", charts.CHART_TYPES, index=0)
            sort_desc = g2.checkbox("按条目数从多到少排序", value=True)
            show_values = g3.checkbox("在图上显示数值", value=True)

            if st.button("📊 生成图表", type="primary", disabled=not selected):
                total_in_scope = max(1, len(stat_df))
                counts = pd.DataFrame([{
                    "关键词": name,
                    "条目数": cached_count(kw_expression(name), stat_plants),
                } for name in selected])
                counts["占比"] = counts["条目数"] / total_in_scope
                if sort_desc:
                    counts = counts.sort_values("条目数", ascending=False,
                                                 kind="mergesort").reset_index(drop=True)
                st.session_state.chart_counts = counts
                st.session_state.chart_type = chart_type
                st.session_state.chart_show_values = show_values
                st.session_state.chart_scope_label = stat_scope_label
                st.session_state.chart_scope_total = len(stat_df)

            if not selected:
                st.warning("请至少勾选一个关键词。")

            counts = st.session_state.chart_counts
            if counts is not None and len(counts):
                fig = charts.make_chart(
                    counts,
                    chart_type=st.session_state.get("chart_type", chart_type),
                    title=("关键词统计（"
                           f"{st.session_state.get('chart_scope_label', '全部厂区')}"
                           f"，共 {st.session_state.get('chart_scope_total', len(db)):,} 条）"),
                    show_values=st.session_state.get("chart_show_values", True),
                )
                st.plotly_chart(fig, width="stretch")

                show = counts.copy()
                show["占比"] = (show["占比"] * 100).map(lambda v: f"{v:.2f}%")
                st.dataframe(show, hide_index=True, width="stretch")

                st.markdown("**保存图表**")
                # 图表签名: 内容不变就复用已生成的 PNG/HTML
                fig_token = (
                    st.session_state.get("chart_type", chart_type),
                    st.session_state.get("chart_show_values", True),
                    st.session_state.get("chart_scope_label", ""),
                    st.session_state.get("chart_scope_total", 0),
                    tuple(zip(counts["关键词"].tolist(), counts["条目数"].tolist())),
                )
                want_png = st.checkbox(
                    "准备高清 PNG（首次约 1 秒）", value=False,
                    help="服务器渲染 PNG 比较慢，所以默认不做。"
                         "图表右上角的相机图标本来就能直接存 PNG，是即时的。",
                )
                s1, s2, s3 = st.columns(3)
                with s1:
                    if want_png:
                        png, err = chart_png(fig, fig_token)
                        if png:
                            st.download_button(
                                "🖼 保存图表 (PNG)", data=png,
                                file_name=f"关键词统计_{timestamp()}.png",
                                mime="image/png", type="primary",
                                width="stretch",
                            )
                        else:
                            st.button("🖼 PNG 不可用", disabled=True,
                                       width="stretch",
                                       help=f"当前环境无法导出 PNG：{err}。"
                                            f"请用 HTML 或图表右上角相机按钮。")
                    else:
                        st.caption("需要高清 PNG 时勾选上面的选项；"
                                   "或直接点图表右上角的 📷。")
                with s2:
                    st.download_button(
                        "🌐 保存图表 (HTML)", data=chart_html(fig, fig_token),
                        file_name=f"关键词统计_{timestamp()}.html",
                        mime="text/html", width="stretch",
                        help="自包含网页，双击即可在浏览器打开，中文显示正常。",
                    )
                with s3:
                    st.download_button(
                        "📥 下载统计数据 (Excel)",
                        data=counts_excel(counts, fig_token),
                        file_name=f"关键词统计_{timestamp()}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        width="stretch",
                    )
