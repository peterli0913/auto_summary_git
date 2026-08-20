"""数据库层: SQLite (标准库自带, 单文件, 可直接 SQL 查询).

选 SQLite 的原因:
  * 无需额外依赖 / 无需服务端, 一个 .db 文件即可随项目走
  * 支持 SQL LIKE, 十万级数据检索仍是毫秒级
  * 用 pandas 一行就能进出, 也方便别的工具直接打开

表结构 (与用户要求的 3 项主字段一致, 另存 2 个溯源字段):
    inspections(序号 INTEGER PK, 日期 TEXT, 巡查发现 TEXT, 来源文件 TEXT, 台账类型 TEXT)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .schema import (CONTENT_COL, DATE_COL, KIND_COL, PLANT_COL, SEQ_COL,
                     SOURCE_FILE_COL, UNKNOWN_PLANT)

TABLE = "inspections"
DEFAULT_DB_PATH = Path("data/inspection.db")

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    "{SEQ_COL}"          INTEGER PRIMARY KEY,
    "{DATE_COL}"         TEXT,
    "{PLANT_COL}"        TEXT,
    "{CONTENT_COL}"      TEXT NOT NULL,
    "{SOURCE_FILE_COL}"  TEXT,
    "{KIND_COL}"         TEXT
);
"""
_INDEX_SQL = [
    f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_date ON {TABLE}("{DATE_COL}");',
    f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_kind ON {TABLE}("{KIND_COL}");',
    f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_plant ON {TABLE}("{PLANT_COL}");',
]


def connect(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


#: 表里应有的列 (顺序无关)
EXPECTED_COLUMNS = [SEQ_COL, DATE_COL, PLANT_COL, CONTENT_COL,
                    SOURCE_FILE_COL, KIND_COL]


def _table_columns(conn: sqlite3.Connection) -> Optional[list]:
    """返回已存在表的列名; 表不存在则 None."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
        (TABLE,)).fetchone()
    if row is None:
        return None
    return [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE});")]


def schema_is_current(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> bool:
    """旧版本建的库没有「厂区」列, 直接往里写会报错. 这里判断是否需要重建."""
    path = Path(db_path)
    if not path.exists():
        return True
    try:
        with connect(path) as conn:
            cols = _table_columns(conn)
    except Exception:
        return False
    return cols is None or set(cols) == set(EXPECTED_COLUMNS)


def init_db(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
    """建表; 若已存在的表结构与当前版本不一致则重建.

    这张表只是上一次汇总结果的缓存, 重新上传即可再生成,
    所以结构变了直接丢弃重建, 比留个坏库更好.
    """
    with connect(db_path) as conn:
        cols = _table_columns(conn)
        if cols is not None and set(cols) != set(EXPECTED_COLUMNS):
            conn.execute(f"DROP TABLE {TABLE};")
        conn.execute(_CREATE_SQL)
        for sql in _INDEX_SQL:
            conn.execute(sql)
        conn.commit()


def build_dataframe(raw: pd.DataFrame,
                    dedupe_rows: bool = True,
                    sort_by_date: bool = True) -> pd.DataFrame:
    """把 reader 合并出的原始表整理成最终数据库表 (含连续序号).

    - 日期统一成 'YYYY-MM-DD' 字符串 (无法解析的留空)
    - 可选按 (日期, 巡查发现) 去重
    - 序号按最终顺序从 1 连续编号
    """
    df = raw.copy()
    for col in (DATE_COL, PLANT_COL, CONTENT_COL, SOURCE_FILE_COL, KIND_COL):
        if col not in df.columns:
            df[col] = None
    df[PLANT_COL] = df[PLANT_COL].fillna(UNKNOWN_PLANT)

    df[CONTENT_COL] = df[CONTENT_COL].astype("string").str.strip()
    df = df[df[CONTENT_COL].notna() & (df[CONTENT_COL].str.len() > 0)]

    dates = pd.to_datetime(df[DATE_COL], errors="coerce")
    if sort_by_date:
        order = dates.sort_values(kind="mergesort", na_position="last").index
        df = df.loc[order]
        dates = dates.loc[order]
    df[DATE_COL] = dates.dt.strftime("%Y-%m-%d")

    if dedupe_rows:
        # 同一条描述在不同厂区应算两条, 所以厂区也纳入去重键
        df = df.drop_duplicates(subset=[DATE_COL, PLANT_COL, CONTENT_COL],
                                keep="first")

    df = df.reset_index(drop=True)
    df.insert(0, SEQ_COL, range(1, len(df) + 1))
    return df[[SEQ_COL, DATE_COL, PLANT_COL, CONTENT_COL,
               SOURCE_FILE_COL, KIND_COL]]


def save(df: pd.DataFrame, db_path: Union[str, Path] = DEFAULT_DB_PATH,
         replace: bool = True) -> int:
    """写入数据库. replace=True 时整表覆盖 (重新整理数据的典型场景)."""
    init_db(db_path)
    with connect(db_path) as conn:
        if replace:
            conn.execute(f"DELETE FROM {TABLE};")
        df.to_sql(TABLE, conn, if_exists="append", index=False)
        conn.commit()
    return len(df)


def load(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> Optional[pd.DataFrame]:
    """读回整表; 库不存在、为空或结构过旧时返回 None."""
    path = Path(db_path)
    if not path.exists():
        return None
    if not schema_is_current(path):
        return None          # 旧结构缺「厂区」列, 让用户重新汇总一次
    try:
        with connect(path) as conn:
            df = pd.read_sql_query(
                f'SELECT * FROM {TABLE} ORDER BY "{SEQ_COL}";', conn)
    except Exception:
        return None
    return df if len(df) else None


def stats(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> dict:
    df = load(db_path)
    if df is None:
        return {"count": 0}
    dates = pd.to_datetime(df[DATE_COL], errors="coerce")
    return {
        "count": int(len(df)),
        "date_min": None if dates.isna().all() else str(dates.min().date()),
        "date_max": None if dates.isna().all() else str(dates.max().date()),
        "n_missing_date": int(dates.isna().sum()),
        "by_kind": df[KIND_COL].value_counts().to_dict() if KIND_COL in df else {},
        "by_plant": df[PLANT_COL].value_counts().to_dict() if PLANT_COL in df else {},
        "n_files": int(df[SOURCE_FILE_COL].nunique()) if SOURCE_FILE_COL in df else 0,
    }
