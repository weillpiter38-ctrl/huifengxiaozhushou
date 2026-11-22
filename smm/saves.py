"""Read Stellaris save metadata and compare its mod list with the current playset."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

MAX_META_BYTES = 4 * 1024 * 1024
SAVE_ROOT = "save games"


def _meta_text(zf):
    for name in ("meta", "./meta"):
        try:
            info = zf.getinfo(name)
        except KeyError:
            continue
        if info.file_size > MAX_META_BYTES:
            continue
        with zf.open(info) as handle:
            return handle.read().decode("utf-8-sig", "replace")
    return ""


def _field(text, name):
    match = re.search(rf'^{name}\s*=\s*"([^"]*)"', text, re.M)
    return match.group(1) if match else ""


def _block(text, name):
    match = re.search(rf"^{name}\s*=\s*\{{(.*?)\}}", text, re.M | re.S)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def list_saves(docs):
    root = Path(docs) / SAVE_ROOT
    if not root.is_dir():
        return []
    rows = []
    for path in root.rglob("*.sav"):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({
            "path": str(path),
            "name": path.stem,
            "folder": path.parent.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime * 1000),
        })
    rows.sort(key=lambda item: item["mtime"], reverse=True)
    return rows[:100]


def read_meta(path, docs):
    root = (Path(docs) / SAVE_ROOT).resolve()
    target = Path(path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("存档路径不在群星存档目录内。")
    if not target.is_file() or target.suffix.lower() != ".sav":
        raise ValueError("存档文件不存在。")
    try:
        with zipfile.ZipFile(target) as zf:
            text = _meta_text(zf)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"无法读取存档：{exc}") from exc
    if not text:
        raise ValueError("存档里没有 meta 信息。")
    return {
        "name": _field(text, "name") or target.stem,
        "date": _field(text, "date"),
        "version": _field(text, "version"),
        "mods": _block(text, "mods"),
        "dlcs": _block(text, "required_dlcs"),
        "path": str(target),
    }
