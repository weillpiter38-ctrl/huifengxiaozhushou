"""崩溃归因：把 error.log 里的报错按 MOD 归类。

思路很朴素：Paradox 的错误行几乎都会带上出错文件路径，而路径里就含着
MOD 的目录名（`mod/ugc_123456/...` 或 `mod/<文件夹>/...`）。把这些路径
反查成 MOD，再按条数与错误类型排序，就能给出「最可能是谁」的候选。
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_FILE_BYTES = 24 * 1024 * 1024
MAX_LINES = 4000
SAMPLE_LIMIT = 6

# 每类只留一个可读名字，UI 直接显示
KIND_RULES = (
    ("missing_localisation", re.compile(r"missing locali[sz]ation|localisation.*not found", re.I)),
    ("unknown_trigger", re.compile(r"unknown trigger|invalid trigger|unknown effect|invalid effect", re.I)),
    ("duplicate", re.compile(r"duplicate|already defined|redefin", re.I)),
    ("parse_error", re.compile(r"parse error|unexpected token|syntax error", re.I)),
    ("exception", re.compile(r"exception|stack trace|access violation|assert", re.I)),
    ("missing_file", re.compile(r"could not (?:open|find)|cannot open|no such file|missing file|not found", re.I)),
)
ERROR_HINT = re.compile(r"error|exception|failed|crash|assert|warning|invalid|could not|not found|unsupported", re.I)
MOD_PATH = re.compile(r"mod[\\/]([^\\/\s\"]+?)(?:[\\/]|\.mod)", re.I)
MISSING_FILE = re.compile(r"could not open file:\s*([^,\s]+)", re.I)


def _kind_of(line):
    for name, pattern in KIND_RULES:
        if pattern.search(line):
            return name
    return "other"


def _mod_index(mods):
    """目录名 -> mod，用于把日志路径反查成 MOD。"""
    index = {}
    for mod in mods:
        for key in (mod.get("dir_name"), mod.get("folder")):
            if key:
                index[str(key).casefold()] = mod
        directory = str(mod.get("dir") or "")
        if directory:
            index[Path(directory).name.casefold()] = mod
        steam_id = str(mod.get("steam_id") or "")
        if steam_id.isdigit():
            index[("ugc_" + steam_id).casefold()] = mod
    return index



def _owner_by_file(rel, mods, cache):
    """报错行只给了相对路径时，看看哪个 MOD 里真有这个文件。"""
    key = rel.casefold()
    if key in cache:
        return cache[key]
    owner = None
    hits = 0
    for mod in mods:
        directory = mod.get("dir")
        if not directory:
            continue
        try:
            if (Path(directory) / rel).exists():
                hits += 1
                owner = mod
                if hits > 1:
                    owner = None
                    break
        except OSError:
            continue
    cache[key] = owner if hits == 1 else None
    return cache[key]

def _read(path):
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-MAX_LINES:]


def collect(docs, mods, extra_logs=()):
    """返回 {files, total_errors, mapped, unmapped, dumps, ...}。"""
    docs = Path(docs)
    index = _mod_index(mods or [])
    by_id = {str(mod.get("id")): mod for mod in (mods or [])}
    logs = [docs / "logs" / "error.log", docs / "logs" / "game.log"]
    logs.extend(Path(item) for item in extra_logs)
    files = []
    buckets = {}
    unmapped = []
    file_cache = {}
    total = 0
    for path in logs:
        lines = _read(path)
        if not lines:
            continue
        hits = 0
        for line in lines:
            if not ERROR_HINT.search(line):
                continue
            hits += 1
            total += 1
            match = MOD_PATH.search(line)
            owner = index.get(match.group(1).casefold()) if match else None
            if owner is None:
                missing = MISSING_FILE.search(line)
                if missing:
                    owner = _owner_by_file(missing.group(1).strip('"\''), mods or [], file_cache)
            if owner is None:
                if len(unmapped) < 40:
                    unmapped.append({"file": path.name, "line": line.strip()[:240]})
                continue
            key = str(owner.get("id"))
            bucket = buckets.setdefault(key, {"count": 0, "kinds": {}, "samples": []})
            bucket["count"] += 1
            kind = _kind_of(line)
            bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
            if len(bucket["samples"]) < SAMPLE_LIMIT:
                bucket["samples"].append(line.strip()[:240])
        try:
            stat = path.stat()
        except OSError:
            stat = None
        files.append({
            "name": path.name,
            "size": stat.st_size if stat else 0,
            "mtime": int(stat.st_mtime * 1000) if stat else 0,
            "lines": len(lines),
            "errors": hits,
        })
    mapped = []
    for mod_id, bucket in buckets.items():
        mod = by_id.get(mod_id) or {}
        mapped.append({
            "mod_id": mod_id,
            "name": mod.get("name") or mod_id,
            "count": bucket["count"],
            "kinds": dict(sorted(bucket["kinds"].items(), key=lambda kv: -kv[1])),
            "samples": bucket["samples"],
        })
    mapped.sort(key=lambda row: (-row["count"], row["name"].casefold()))
    dumps = []
    crash_dir = docs / "crashes"
    if crash_dir.is_dir():
        try:
            dumps = sorted(
                (item.name for item in crash_dir.iterdir() if item.is_file()),
                key=lambda name: (crash_dir / name).stat().st_mtime,
                reverse=True,
            )[:10]
        except OSError:
            dumps = []
    newest = max((item["mtime"] for item in files), default=0)
    return {
        "ok": True,
        "docs": str(docs),
        "files": files,
        "total_errors": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "dumps": dumps,
        "newest_mtime": newest,
        "suspects": [row for row in mapped[:8]],
    }
