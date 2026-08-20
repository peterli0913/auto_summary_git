"""收集待处理的 Excel: 支持 单个/多个文件、ZIP 压缩包(整个文件夹)、服务器文件夹路径.

浏览器的文件选择框不能直接上传"文件夹", 所以提供 3 条路径:
  1. 多文件上传  —— 在选择框里框选文件夹内的所有 xlsx (可多选)
  2. ZIP 上传    —— 把文件夹压缩成 zip 上传, 这里递归解出所有 xlsx
  3. 文件夹路径  —— 直接填服务器/仓库里的目录 (本地运行或仓库自带数据时最方便)
三者可以混用, 最终汇成一个列表.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Union

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
ZIP_SUFFIXES = {".zip"}

#: (显示名, 数据源) — 数据源可以是 Path 或 bytes
Source = Tuple[str, Union[Path, bytes]]


def _is_excel(name: str) -> bool:
    return Path(name).suffix.lower() in EXCEL_SUFFIXES


def _is_hidden(name: str) -> bool:
    """跳过 macOS/Windows 的临时与元数据文件."""
    parts = Path(name).parts
    base = Path(name).name
    return (base.startswith("~$") or base.startswith(".")
            or "__MACOSX" in parts or any(p.startswith(".") for p in parts))


def from_zip_bytes(data: bytes, zip_name: str = "upload.zip") -> List[Source]:
    """递归取出 zip 里所有 Excel (保留相对路径作为显示名)."""
    out: List[Source] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # zip 里的中文名可能是 cp437 误解码, 尝试修复
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("gbk")
                except Exception:
                    pass
            if _is_hidden(name) or not _is_excel(name):
                continue
            out.append((name, zf.read(info)))
    return out


def from_folder(folder: Union[str, Path], recursive: bool = True) -> List[Source]:
    """扫描本地/服务器文件夹里的所有 Excel."""
    root = Path(folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {root}")
    pattern = "**/*" if recursive else "*"
    out: List[Source] = []
    for p in sorted(root.glob(pattern)):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if _is_hidden(rel) or not _is_excel(p.name):
            continue
        out.append((rel, p))
    return out


def from_uploads(uploaded_files: Sequence) -> List[Source]:
    """处理 Streamlit file_uploader 的返回值 (Excel 直接用, zip 自动展开)."""
    out: List[Source] = []
    for uf in uploaded_files or []:
        if uf is None:
            continue
        name = getattr(uf, "name", "uploaded")
        try:
            data = uf.getvalue()
        except Exception:
            uf.seek(0)
            data = uf.read()
        suffix = Path(name).suffix.lower()
        if suffix in ZIP_SUFFIXES:
            out.extend(from_zip_bytes(data, zip_name=name))
        elif _is_excel(name):
            out.append((name, data))
    return out


def dedupe(sources: Iterable[Source]) -> List[Source]:
    """按显示名去重 (同名文件只保留第一个)."""
    seen: set[str] = set()
    out: List[Source] = []
    for name, src in sources:
        key = Path(name).name
        if key in seen:
            continue
        seen.add(key)
        out.append((name, src))
    return out


def collect(uploaded_files: Sequence | None = None,
            folder_paths: Sequence[Union[str, Path]] | None = None,
            ) -> Tuple[List[Source], List[str]]:
    """汇总所有来源. 返回 (sources, 警告信息列表)."""
    warnings_: List[str] = []
    sources: List[Source] = []

    if uploaded_files:
        try:
            sources.extend(from_uploads(uploaded_files))
        except Exception as exc:  # noqa: BLE001
            warnings_.append(f"处理上传文件出错: {exc}")

    for folder in folder_paths or []:
        folder = str(folder).strip()
        if not folder:
            continue
        try:
            found = from_folder(folder)
            if not found:
                warnings_.append(f"文件夹里没有 Excel: {folder}")
            sources.extend(found)
        except Exception as exc:  # noqa: BLE001
            warnings_.append(f"读取文件夹失败 {folder}: {exc}")

    before = len(sources)
    sources = dedupe(sources)
    if len(sources) < before:
        warnings_.append(f"按文件名去重, 跳过 {before - len(sources)} 个重复文件")
    return sources, warnings_
