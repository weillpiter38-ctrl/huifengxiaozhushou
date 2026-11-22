"""Build the in-memory catalogue: mods, playsets, file conflicts, dependency checks."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import descriptor, paths
from .launcher_db import LauncherDB


# --- on-demand conflict preview -------------------------------------------------
# A scan ships only WHERE each MOD's copy of a conflicting file lives (holder rows).
# Reading the file bodies is a separate action: doing it inside the scan cost ~100x
# on a real library (400 holder files on a mechanical drive: 1.2 s -> 110 s, measured
# 2026-11-20). The UI asks for one path when the user expands it.
CONFLICT_DIFF_LINES = 12
CONFLICT_DIFF_CHARS = 160
CONFLICT_DIFF_MAX_BYTES = 512 * 1024


def _read_lines(path):
    """Read one MOD file for the preview; None when unreadable or too big."""
    try:
        if os.path.getsize(path) > CONFLICT_DIFF_MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read().splitlines()
    except (OSError, ValueError):
        return None


def _diff_rows(mine, theirs, cap=CONFLICT_DIFF_LINES):
    """Changed lines of one MOD's copy against the winning copy.

    ``op`` is one of:
      change  - this MOD has a different line where the winner has one
      add     - this MOD has an extra line the winner lacks
      missing - the winner has a line this MOD lacks (shown with the winner's number)
    """
    from difflib import SequenceMatcher

    rows = []
    if mine is None:
        return rows
    if theirs is None:
        for index, text in enumerate(mine[:cap], start=1):
            rows.append({"line": index, "text": _clip(text, CONFLICT_DIFF_CHARS), "op": "add"})
        return rows
    matcher = SequenceMatcher(a=theirs, b=mine, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            source, span, op = theirs, range(i1, i2), "missing"
        else:
            source, span = mine, range(j1, j2)
            op = "change" if tag == "replace" else "add"
        for index in span:
            if len(rows) >= cap:
                return rows
            rows.append({"line": index + 1,
                         "text": _clip(source[index], CONFLICT_DIFF_CHARS), "op": op})
    return rows


def _safe_relative(relative):
    """Reject anything that could escape the MOD directory before we open it."""
    raw = str(relative or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("缺少文件路径，无法读取冲突明细。")
    if raw.startswith("/") or ":" in raw:
        raise ValueError("文件路径不合法，已拒绝读取。")
    parts = raw.split("/")
    if ".." in parts or any(part in ("", ".") for part in parts):
        raise ValueError("文件路径不合法，已拒绝读取。")
    return raw


def conflict_preview(catalog, relative, mod_ids=None):
    """Per-holder code preview for ONE conflicting path. Never called from a scan."""
    path = _safe_relative(relative)
    payload = catalog.scan()
    entry = next((item for item in payload.get("conflicts") or [] if item.get("path") == path), None)
    if entry is None:
        raise ValueError("这条冲突已经不存在了，请重新分析后再看。")
    holders = list(entry.get("holders") or [])
    wanted = {str(item) for item in (mod_ids or []) if str(item).strip()}
    if wanted:
        holders = [holder for holder in holders if str(holder.get("id")) in wanted]
    if not holders:
        raise ValueError("没有可预览的 MOD 副本。")
    winner = entry.get("winner") or {}
    bodies = {}
    for holder in holders:
        file = str(holder.get("file") or "")
        bodies[holder.get("id")] = _read_lines(file) if file else None
    winner_lines = bodies.get(winner.get("id"))
    rows = []
    identical = 0
    for holder in holders:
        lines = bodies.get(holder.get("id"))
        is_winner = holder.get("id") == winner.get("id")
        same = (lines is not None and winner_lines is not None
                and lines == winner_lines and not is_winner)
        if same:
            identical += 1
        rows.append({
            "id": holder.get("id"),
            "name": holder.get("name"),
            "rank": holder.get("rank"),
            "enabled": bool(holder.get("enabled")),
            "file": holder.get("file"),
            "size": holder.get("size") or 0,
            "lines": len(lines) if lines is not None else 0,
            "identical": same,
            "unreadable": lines is None,
            "is_winner": is_winner,
            "diff": [] if same or is_winner else _diff_rows(lines, winner_lines),
        })
    return {
        "path": path,
        "winner": {"id": winner.get("id"), "name": winner.get("name"), "rank": winner.get("rank")},
        "winner_basis": entry.get("winner_basis"),
        "holders": rows,
        "identical_count": identical,
        "holders_total": entry.get("holders_total"),
        "clipped": bool(entry.get("holders_clipped")),
    }


THUMB_NAMES = ("thumbnail.jpg", "thumbnail.png", "thumbnail.webp")
IGNORED_RELATIVE = ("descriptor.mod", ".mod")
# How many conflicting mod names to ship per path; mod_count carries the rest.
# Each name is repeated for every holder of the path, so a 3000-MOD playset that
# shares a file would otherwise ship ~5 MB of repeated names alone (QA-T4 r3).
CONFLICT_NAMES_MAX = 4
CONFLICT_NAME_CLIP = 60
# Per-mod detail rows; conflict_paths_total keeps the real number.
CONFLICT_PATHS_MAX = 80
# Irony-style detail: for every conflicting path, which MODs ship that exact file,
# where the copy lives and which one the game actually loads last (winner).
# The holder rows live ONLY in the global "conflicts" list; putting them inside
# each mod's "conflict_paths" would multiply them by the number of holders again.
CONFLICT_HOLDERS_MAX = 8
CONFLICT_HOLDER_FILE_CLIP = 240
# Paths that carry the holder detail; must not exceed the payload slice below.
CONFLICT_DETAIL_MAX = 200
# Files every MOD ships but no MOD overrides in game: thumbnails, readmes, VCS
# metadata. Listing them as "conflicts" drowns the real ones (QA: 73 paths for 19
# MODs, of which 60 were thumbnail.png / readme.md / .git/*).
CONFLICT_SKIP_FILES = {"thumbnail.png", "thumbnail.jpg", "thumbnail.webp", "readme.md",
                       "readme.txt", "changelog.txt", "changelog.md", "steam_description.txt",
                       "license", "license.txt", ".gitignore", ".gitattributes",
                       "steam_appid.txt", "mod.png"}
CONFLICT_SKIP_DIRS = (".git/", ".github/", ".svn/", ".idea/", ".vscode/", "__macosx/")
# Launcher statuses that mean the entry is not usable on disk.
UNUSABLE_STATUS = {"not_installed", "unsubscribed", "failed", "missing", "removed",
                   "downloading", "installing", "updating"}


def _cache_key(path):
    """Cheap canonical cache key: resolve() costs a realpath syscall per call."""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):
        return str(path)


def _holder_sort_key(ranks):
    """Enabled MODs in load order first, disabled ones last (they cannot win a file)."""
    def key(mod):
        rank = ranks.get(mod.get("id"))
        return (rank is None, rank if rank is not None else 0, str(mod.get("id")))

    return key


def _load_tags(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(x)[:40] for x in value[:32]]


def _clip(value, limit=200):
    """Keep a payload field small: a corrupt row must not ship megabytes."""
    text = "" if value is None else str(value)
    return text[:limit]


def _human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


class Catalog:
    def __init__(self, docs=None, game=None, db_path=None, sandbox=False, workshop=None, game_key="stellaris"):
        self.docs = docs
        self.game = game
        self.game_key = game_key
        self.db_override = db_path
        self.sandbox = sandbox
        self._cache = None
        self._cache_at = 0.0
        self._thumbs = {}
        self.workshop = workshop
        self.mod_dir_override = None
        # Extra folders whose .mod files count as local mods even though the
        # launcher never sees them (sandbox mode writes into <data>/sandbox/mod).
        self.extra_mod_dirs = []
        self._analysis = None
        self._analysis_key = None
        self.cache_path = None
        self._index = {}
        self._measure_cache = {}
        self._thumb_cache = {}
        self._resolve_cache = {}
        self._parent_alive = {}
        self._bulk = None
        self._index_loaded = False
        self._index_dirty = False
        self._index_hits = 0
        self._index_misses = 0
        # One full scan is expensive (thousands of syscalls); when several HTTP
        # requests force-refresh at once they must share a single scan instead of
        # each replaying it under the GIL.
        self._scan_lock = threading.Lock()
        self._scan_done = 0
        self._scan_owner = None

    # ---------------------------------------------------------------- cache
    def set_cache_path(self, path):
        self.cache_path = Path(path)
        try:
            raw = self.cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if data.get("version") == 2:
                self._index = data.get("descriptors") or {}
                self._measure_cache = data.get("measures") or {}
                self._index_loaded = True
        except (OSError, ValueError):
            self._index = {}
            self._measure_cache = {}
        return self

    def _save_index(self):
        if not self.cache_path or not self._index_dirty:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 2, "descriptors": self._index, "measures": self._measure_cache}
            temp = self.cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, self.cache_path)
            self._index_dirty = False
        except OSError:
            pass

    def cache_stats(self):
        return {
            "path": str(self.cache_path or ""),
            "loaded": self._index_loaded,
            "descriptors": len(self._index),
            "measures": len(self._measure_cache),
            "hits": self._index_hits,
            "misses": self._index_misses,
        }

    def _parse_cached(self, path, known_stat=None):
        if known_stat is not None:
            mtime_ns, size = known_stat
        else:
            try:
                stat = path.stat()
            except OSError:
                return {}
            mtime_ns, size = stat.st_mtime_ns, stat.st_size
        key = _cache_key(path)
        entry = self._index.get(key)
        if entry and entry.get("mtime_ns") == mtime_ns and entry.get("size") == size:
            self._index_hits += 1
            return entry.get("parsed") or {}
        self._index_misses += 1
        parsed = descriptor.parse_file(path)
        self._index[key] = {"mtime_ns": mtime_ns, "size": size, "parsed": parsed}
        self._index_dirty = True
        return parsed

    # ---------------------------------------------------------------- paths
    def vision(self):
        info = paths.detect(self.docs, self.game, game_key=self.game_key)
        if self.mod_dir_override:
            info["mod_dir"] = str(self.mod_dir_override)
        if self.db_override:
            info["db"] = str(self.db_override)
        return info

    def invalidate(self):
        self._cache = None
        self._cache_at = 0.0
        self._analysis = None
        self._analysis_key = None

    # ---------------------------------------------------------------- scan
    def _record_db_stamp(self):
        """Remember the launcher DB file stamp the current cache was built from."""
        path = getattr(self, "_db_stamp_path", None)
        self._db_stamp = None
        if not path:
            return
        try:
            stat = os.stat(path)
        except OSError:
            return
        self._db_stamp = (stat.st_mtime_ns, stat.st_size)

    def _db_stamp_equal(self):
        """A second instance (or the Paradox launcher) may write the same launcher
        DB while we hold a scan in cache. Serve the 20 s cache only while that file
        is untouched -- a stat() is far cheaper than a full rescan."""
        path = getattr(self, "_db_stamp_path", None)
        known = getattr(self, "_db_stamp", None)
        if not path or known is None:
            return True
        try:
            stat = os.stat(path)
        except OSError:
            return False
        return known == (stat.st_mtime_ns, stat.st_size)

    def _mods_stamp_now(self):
        """Snapshot every scan root (real mod dir + sandbox/extra roots).

        Directory mtime is not reliable for "a file appeared inside it" on every
        filesystem, so the signature is the sorted set of entry names. One
        scandir per root costs well under a millisecond at 300 MODs and catches
        both additions and removals immediately."""
        out = {}
        for path in getattr(self, "_mods_stamp_paths", []) or []:
            try:
                names = []
                with os.scandir(path) as it:
                    for entry in it:
                        if entry.name.lower().endswith(".mod") or entry.is_dir():
                            names.append(entry.name)
            except OSError:
                out[path] = None
            else:
                names.sort()
                out[path] = tuple(names)
        return out

    def _mods_stamp_equal(self):
        """Adding/removing a MOD folder changes its parent directory mtime; the
        20 s in-process cache must not outlive that, otherwise a freshly imported
        local MOD stays invisible until the window expires. Sandbox runs scan
        extra roots too, so every root is compared, not just the real one."""
        paths = getattr(self, "_mods_stamp_paths", None)
        known = getattr(self, "_mods_stamp", None)
        if not paths:
            # Nothing recorded yet (fresh object or a test double): keep the old
            # permissive behaviour instead of forcing a rescan on every call.
            return True
        if known is None:
            return False
        return known == self._mods_stamp_now()

    def scan(self, force=False):
        if (not force and self._cache and time.time() - self._cache_at < 20
                and self._db_stamp_equal() and self._mods_stamp_equal()):
            return self._cache
        # Single-flight: threads that arrived before the current scan finished wait
        # for its result instead of repeating the same thousands of syscalls.
        if self._scan_owner == threading.get_ident():
            # A scan that calls back into scan() would otherwise wait on its own
            # lock forever; fail loudly instead of hanging the request thread.
            raise RuntimeError("扫描正在进行，不能在同一线程内重入 scan()。")
        seen = self._scan_done
        with self._scan_lock:
            self._scan_owner = threading.get_ident()
            try:
                if self._scan_done != seen and self._cache is not None:
                    return self._cache
                return self._scan_once(force)
            finally:
                self._scan_owner = None

    def _scan_once(self, force=False):
        info = self.vision()
        self._resolve_cache = {}
        self._parent_alive = {}
        self._bulk = None
        self._build_bulk(info.get("mod_dir"))
        self._db_stamp_path = info.get("db")
        roots = [info.get("mod_dir")]
        for entry in list(getattr(self, "extra_mod_dirs", []) or []):
            # extra_mod_dirs entries are (path, side) tuples; stat() needs the path.
            roots.append(entry[0] if isinstance(entry, tuple) else entry)
        seen_roots = []
        for root in roots:
            if root and str(root) not in seen_roots:
                seen_roots.append(str(root))
        self._mods_stamp_paths = seen_roots
        self._mods_stamp = self._mods_stamp_now()
        db = LauncherDB(info["db"])
        snap = db.snapshot()
        mods_by_id = {row["id"]: row for row in snap["mods"]}

        playsets = snap["playsets"]
        active = next((p for p in playsets if p.get("isActive")), playsets[0] if playsets else None)
        entries = [e for e in snap["entries"] if active and e["playsetId"] == active["id"]]
        entries.sort(key=lambda e: e.get("position") or 0)

        # The game does not read the playset table: it reads dlc_load.json, whose
        # "enabled_mods" array IS the load order (later entry overwrites earlier).
        # The launcher writes that array in playset position order, so both usually
        # agree - but a stale/hand-edited dlc_load.json (or loadOrder='alphabetical')
        # makes the arrangement a lie. Rank by the file the game actually reads and
        # report the mismatch instead of pretending the list order is the truth.
        load_order = self._load_order(info, snap, entries)

        installed_names = set()
        for row in snap["mods"]:
            if row.get("displayName"):
                installed_names.add(row["displayName"].lower())
            if row.get("name"):
                installed_names.add(row["name"].lower())

        mods = []
        for entry in entries:
            row = mods_by_id.get(entry["modId"])
            if not row:
                continue
            described = self._describe_mod(row, entry, info, installed_names)
            if described:
                mods.append(described)

        conflicts = self._attach_conflicts(mods, load_order)
        in_playset = {e["modId"] for e in entries}
        available = []
        for row in snap["mods"]:
            if active and row["id"] in in_playset:
                continue
            described = self._describe_available(row, info)
            if described:
                available.append(described)

        # Sandbox writes (import / merge) land in an extra folder the launcher
        # never registers, so they have no DB row: list them from the folder or
        # the user cannot see what they just imported and 一键清理 offers to
        # delete it as an orphan.
        known_dirs = set()
        for item in mods + available:
            try:
                known_dirs.add(str(Path(item["dir"]).resolve()).lower())
            except (KeyError, OSError, ValueError):
                continue
        for extra in list(getattr(self, "extra_mod_dirs", []) or []):
            # entries are (path, side): packs dropped straight into the real mod
            # folder by hand are `real`, packs in the sandbox mirror are `sandbox`.
            root, side = extra if isinstance(extra, tuple) else (extra, "sandbox")
            available.extend(self._describe_extra_dir(root, info, known_dirs, side=side))
        for item in mods:
            item.setdefault("side", "real")
        for item in available:
            item.setdefault("side", "real")

        valid_ids = {mod["id"] for mod in mods} | {mod["id"] for mod in available}
        for row in snap["mods"]:
            if row["id"] in valid_ids:
                continue
            if self._usable(row) and self._resolve_dir(row, info):
                valid_ids.add(row["id"])

        current_key = frozenset(mod["id"] for mod in mods)
        if self._analysis_key != current_key:
            self._analysis = None
            self._analysis_key = None

        stats = {
            "total": len(mods),
            "enabled": sum(1 for m in mods if m["enabled"]),
            "files": sum(m["file_count"] for m in mods),
            "bytes": sum(m["size_bytes"] for m in mods),
            "conflict_paths": len(conflicts),
            "compat_mismatch": sum(1 for m in mods if m["compat"] == "mismatch"),
            "missing_deps": sum(len(m["missing_deps"]) for m in mods),
            "hidden_invalid": sum(1 for row in snap["mods"] if row["id"] not in valid_ids),
        }

        payload = {
            "ok": True,
            "app": {"name": "群星 MOD 管理器", "version": "0.1.0", "sandbox": self.sandbox},
            "paths": info,
            "game": {"version": info["version"], "raw_version": info["raw_version"]},
            "playsets": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "active": bool(active) and p["id"] == active["id"],
                    "mod_count": sum(1 for e in snap["entries"]
                                     if e["playsetId"] == p["id"] and e["modId"] in valid_ids),
                    "enabled_count": sum(1 for e in snap["entries"]
                                         if e["playsetId"] == p["id"] and e["enabled"]
                                         and e["modId"] in valid_ids),
                }
                for p in playsets
            ],
            "active_playset": active["id"] if active else None,
            "mods": mods,
            "available": available,
            "conflicts": conflicts[:200],
            "load_order": {
                "source": load_order["source"],
                "mismatch": load_order["mismatch"],
                "count": load_order["count"],
                "entries": load_order["entries"][:600],
                "dlc_path": load_order["dlc_path"],
            },
            "analysis": self._analysis,
            "stats": stats,
            "runtime": _runtime(self.game_key),
            "scanned_at": int(time.time() * 1000),
        }
        self._cache = payload
        self._cache_at = time.time()
        self._scan_done += 1
        self._record_db_stamp()
        self._save_index()
        return payload

    def refresh(self):
        return self.scan(force=True)

    def analyse(self, force=False, deep=False):
        from . import analysis

        payload = self.scan()
        key = frozenset(mod["id"] for mod in payload["mods"])
        # 只要 MOD 集合没变就复用上次结果（force 不再无条件重算），
        # 想强制重算走 deep=True；并发调用在这里单飞，后来的直接拿同一份结果。
        if not deep and self._analysis and self._analysis_key == key:
            return {"ok": True, "analysis": self._analysis, "cached": True}
        lock = getattr(self, "_analysis_lock", None)
        if lock is None:
            import threading as _threading
            lock = _threading.Lock()
            self._analysis_lock = lock
        with lock:
            if not deep and self._analysis and self._analysis_key == key:
                return {"ok": True, "analysis": self._analysis, "cached": True}
            result = analysis.analyze(self)
            self._analysis = result["analysis"]
            self._analysis_key = key
            if self._cache is not None:
                self._cache["analysis"] = self._analysis
            return result

    # ------------------------------------------------------------- helpers
    def _build_bulk(self, mod_dir):
        """One walk of the MOD folder instead of one walk (plus one stat) per mod.

        Mod directories that live directly under <mod_dir>/ are indexed here:
        relative file names plus sizes. Dirs elsewhere (workshop, custom dirPath)
        still take the per-directory path.
        """
        root = str(mod_dir or "")
        if not root or not os.path.isdir(root):
            return
        root_key = _cache_key(root)
        buckets = {}
        loose = set()
        t0 = time.perf_counter()
        stack = [(root, None, "")]
        try:
            while stack:
                current, top, prefix = stack.pop()
                try:
                    entries = list(os.scandir(current))
                except OSError:
                    continue
                for entry in entries:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        if top is None:
                            name = entry.name.lower()
                            try:
                                info_stat = entry.stat()
                                mtime = info_stat.st_mtime_ns
                                ctime = info_stat.st_ctime
                            except OSError:
                                mtime = 0
                                ctime = 0
                            bucket = buckets.get(name)
                            if bucket is None:
                                bucket = buckets[name] = {"mtime_ns": mtime, "ctime": ctime, "files": [],
                                                          "count": 0, "size": 0, "files_set": set()}
                            stack.append((entry.path, name, ""))
                        else:
                            stack.append((entry.path, top, prefix + entry.name + "/"))
                        continue
                    if top is None:
                        # Loose files in <mod_dir> are the "ugc_<id>.mod" fallbacks.
                        loose.add(entry.name.lower())
                        continue
                    bucket = buckets[top]
                    relative = (prefix + entry.name).lower()
                    try:
                        file_stat = entry.stat()
                        size = file_stat.st_size
                        file_mtime = file_stat.st_mtime_ns
                    except OSError:
                        size = 0
                        file_mtime = 0
                    if relative == "descriptor.mod":
                        # Cached here so the parse check needs no second stat().
                        bucket["descriptor"] = (file_mtime, size)
                    bucket["files"].append((relative, size))
                    bucket["count"] += 1
                    bucket["size"] += size
        except OSError:
            return
        for bucket in buckets.values():
            if not bucket.get("files_set"):
                bucket["files_set"] = {item[0] for item in bucket["files"]}
        bulk = {"root": root_key, "buckets": buckets, "loose": loose,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
        self._bulk = bulk

    def _bulk_bucket(self, directory):
        bulk = self.__dict__.get("_bulk")
        if not bulk or not directory:
            return None
        try:
            parent = os.path.dirname(os.path.normpath(str(directory)))
        except (OSError, ValueError):
            return None
        if _cache_key(parent) != bulk["root"]:
            return None
        name = os.path.basename(os.path.normpath(str(directory))).lower()
        return bulk["buckets"].get(name)

    def _resolve_dir(self, row, info):
        directory = row.get("dirPath") or ""
        key = (directory, row.get("steamId"), info.get("workshop"))
        cached = self._resolve_cache.get(key)
        if cached is not None:
            return cached
        resolved = self._resolve_dir_uncached(row, info, directory)
        self._resolve_cache[key] = resolved
        return resolved

    def _resolve_dir_uncached(self, row, info, directory):
        if directory:
            if self._bulk_bucket(directory) is not None:
                # The bulk walk just listed this folder, so it exists: skip the stat.
                return directory
            try:
                # Local merged mods use a .zip path here; both files and dirs are valid.
                if Path(directory).exists():
                    return directory
            except OSError:
                pass
        steam_id = row.get("steamId")
        if steam_id and info.get("workshop"):
            candidate = Path(info["workshop"]) / str(steam_id)
            if self._parent_missing(candidate):
                return ""
            try:
                if candidate.exists():
                    return str(candidate)
            except OSError:
                pass
        return ""

    def _parent_missing(self, path):
        """One stat per parent folder instead of one failed stat per missing child."""
        parent = path.parent
        key = _cache_key(parent)
        known = self._parent_alive.get(key)
        if known is None:
            try:
                known = parent.is_dir()
            except OSError:
                known = False
            self._parent_alive[key] = known
        return not known

    @staticmethod
    def _usable(row):
        return str(row.get("status") or "").strip().lower() not in UNUSABLE_STATUS

    def _read_descriptor(self, row, info, directory=None):
        if directory is None:
            directory = self._resolve_dir(row, info)
        parsed = {}
        bucket = self._bulk_bucket(directory) if directory else None
        if directory:
            inner = Path(directory) / "descriptor.mod"
            known = bucket.get("files_set") if bucket else None
            if known is not None:
                # The bulk index already listed the folder: no extra is_file() stat.
                if "descriptor.mod" in known:
                    parsed = self._parse_cached(inner, bucket.get("descriptor"))
            elif inner.is_file():
                parsed = self._parse_cached(inner)
        steam_id = row.get("steamId")
        if not parsed.get("supported_version") and steam_id:
            loose = (self.__dict__.get("_bulk") or {}).get("loose")
            for name in (f"ugc_{steam_id}.mod", f"PDX_{steam_id}.mod"):
                if loose is not None and name.lower() not in loose:
                    continue
                candidate = Path(info["mod_dir"]) / name
                if candidate.is_file():
                    parsed = {**self._parse_cached(candidate), **{k: v for k, v in parsed.items() if v}}
                    break
        return parsed

    def _describe_mod(self, row, entry, info, installed_names):
        if not self._usable(row):
            return None
        directory = self._resolve_dir(row, info)
        if not directory:
            return None
        parsed = self._read_descriptor(row, info, directory)
        file_count, size_bytes = self._measure(directory)
        supported = parsed.get("supported_version") or row.get("requiredVersion") or ""
        compat = descriptor.check_supported(info.get("raw_version") or info.get("version"), supported)
        deps = parsed.get("dependencies") or []
        missing = [d for d in deps if not any(d.lower() in name or name in d.lower() for name in installed_names)]
        # The bulk index already stat()ed every mod folder, so the created-at field
        # and the thumbnail stamp come from it; only non-indexed dirs pay a stat.
        bucket = self._bulk_bucket(directory)
        if bucket is not None:
            mtime_ns = bucket.get("mtime_ns") or 0
            local_created = int(bucket.get("ctime") or 0)
        else:
            try:
                dir_stat = os.stat(directory)
                mtime_ns = dir_stat.st_mtime_ns
                local_created = int(dir_stat.st_ctime)
            except OSError:
                mtime_ns = 0
                local_created = 0
        thumb = self._thumbnail(directory, parsed.get("picture"), mtime_ns)
        workshop = self.workshop.summary(row.get("steamId")) if self.workshop and row.get("steamId") else None
        local_updated = int(row.get("timeUpdated") or 0)
        workshop_created = int(workshop.get("time_created") or 0) if workshop else 0
        update_available = bool(workshop and local_updated and workshop.get("time_updated")
                                and workshop["time_updated"] > local_updated + 60)
        return {
            "id": row["id"],
            "steam_id": row.get("steamId") or "",
            "name": _clip(parsed.get("name") or row.get("displayName") or row.get("name") or "未知 MOD"),
            "display_name": _clip(row.get("displayName") or row.get("name") or ""),
            "enabled": bool(entry.get("enabled")),
            "position": entry.get("position"),
            "version": parsed.get("version") or row.get("version") or "",
            "supported": supported,
            "compat": compat,
            "tags": _load_tags(row.get("tags")) or parsed.get("tags") or [],
            "dependencies": deps,
            "missing_deps": missing,
            "dir": directory,
            "source": row.get("source") or "",
            "workshop": workshop,
            "time_updated": local_updated,
            "local_created": local_created,
            "workshop_created": workshop_created,
            "update_available": update_available,
            "registry_id": row.get("gameRegistryId") or (f"mod/ugc_{row['steamId']}.mod" if row.get("steamId") else ""),
            "file_count": file_count,
            "size_bytes": size_bytes,
            "size_text": _human_bytes(size_bytes),
            "thumbnail": thumb,
            "remote_file_id": parsed.get("remote_file_id") or "",
            "conflict_paths": [],
            "conflict_paths_total": 0,
            "conflict_count": 0,
            "status": row.get("status") or "",
        }

    def _describe_available(self, row, info):
        if not self._usable(row):
            return None
        directory = self._resolve_dir(row, info)
        if not directory:
            return None
        parsed = self._read_descriptor(row, info, directory)
        workshop = self.workshop.summary(row.get("steamId")) if self.workshop and row.get("steamId") else None
        local_updated = int(row.get("timeUpdated") or 0)
        try:
            local_created = int(os.path.getctime(directory)) if directory else 0
        except OSError:
            local_created = 0
        workshop_created = int(workshop.get("time_created") or 0) if workshop else 0
        update_available = bool(workshop and local_updated and workshop.get("time_updated")
                                and workshop["time_updated"] > local_updated + 60)
        return {
            "id": row["id"],
            "steam_id": row.get("steamId") or "",
            "name": _clip(parsed.get("name") or row.get("displayName") or row.get("name") or "未知 MOD"),
            "display_name": _clip(row.get("displayName") or row.get("name") or ""),
            "tags": _load_tags(row.get("tags")) or parsed.get("tags") or [],
            "dir": directory,
            "thumbnail": self._thumbnail(directory, parsed.get("picture")),
            "remote_file_id": parsed.get("remote_file_id") or "",
            "source": row.get("source") or "",
            "version": parsed.get("version") or "",
            "supported": parsed.get("supported_version") or "",
            "workshop": workshop,
            "time_updated": local_updated,
            "local_created": local_created,
            "workshop_created": workshop_created,
            "update_available": update_available,
            "conflict_paths": [],
            "conflict_paths_total": 0,
            "conflict_count": 0,
            "file_count": 0,
            "size_bytes": 0,
            "size_text": "",
            "registry_id": row.get("gameRegistryId") or (f"mod/ugc_{row['steamId']}.mod" if row.get("steamId") else ""),
        }

    def _extra_target(self, root, parsed, mod_file):
        """Resolve the ``path=`` value of an extra-root .mod inside that root."""
        candidates = []
        raw = str(parsed.get("path") or "").strip().strip('"')
        if raw:
            part = Path(raw.replace("\\", "/"))
            parts = list(part.parts)
            if parts and parts[0].lower() == "mod":
                parts = parts[1:]
            if parts and not Path(parts[0]).anchor:
                candidates.append(root.joinpath(*parts))
            if part.is_absolute():
                candidates.append(part)
        candidates.append(root / mod_file.stem)
        candidates.append(root / f"{mod_file.stem}.zip")
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return None

    def _describe_extra_dir(self, root, info, known_dirs, side="sandbox"):
        """Local mods the launcher never registered: real drop-ins or the sandbox mirror."""
        found = []
        root = Path(root)
        try:
            mod_files = sorted(root.glob("*.mod"))
        except OSError:
            return found
        for mod_file in mod_files:
            try:
                parsed = self._parse_cached(mod_file)
            except OSError:
                continue
            if not isinstance(parsed, dict) or not parsed:
                continue
            target = self._extra_target(root, parsed, mod_file)
            if target is None:
                continue
            try:
                resolved = str(target.resolve()).lower()
            except OSError:
                continue
            if resolved in known_dirs:
                continue
            known_dirs.add(resolved)
            directory = str(target)
            file_count, size_bytes = self._measure(directory)
            supported = parsed.get("supported_version") or ""
            try:
                local_created = int(os.path.getctime(directory))
            except OSError:
                local_created = 0
            found.append({
                "id": f"{side}:{mod_file.stem.lower()}",
                "steam_id": "",
                "name": parsed.get("name") or mod_file.stem,
                "display_name": parsed.get("name") or mod_file.stem,
                "tags": parsed.get("tags") or [],
                "dir": directory,
                "thumbnail": self._thumbnail(directory, parsed.get("picture")),
                "remote_file_id": parsed.get("remote_file_id") or "",
                "source": "sandbox" if side == "sandbox" else "local",
                "version": parsed.get("version") or "",
                "supported": supported,
                "compat": descriptor.check_supported(info.get("raw_version") or info.get("version"), supported),
                "workshop": None,
                "time_updated": 0,
                "local_created": local_created,
                "workshop_created": 0,
                "update_available": False,
                "conflict_paths": [],
                "conflict_paths_total": 0,
                "conflict_count": 0,
                "file_count": file_count,
                "size_bytes": size_bytes,
                "size_text": _human_bytes(size_bytes),
                "registry_id": f"mod/{mod_file.name}",
                "side": side,
                "sandbox_only": side == "sandbox",
                "mod_file": str(mod_file),
            })
        return found

    def _measure(self, directory):
        if not directory:
            return 0, 0
        key = _cache_key(directory)
        bucket = self._bulk_bucket(directory)
        if bucket is not None:
            # Bulk-indexed dir: one shared walk already counted every file.
            self._measure_cache[key] = {"mtime_ns": bucket["mtime_ns"], "count": bucket["count"],
                                        "size": bucket["size"],
                                        "files": [item[0] for item in bucket["files"]]}
            self._index_dirty = True
            return bucket["count"], bucket["size"]
        if not os.path.isdir(directory):
            return 0, 0
        try:
            mtime = os.stat(directory).st_mtime_ns
        except OSError:
            return 0, 0
        cached = self._measure_cache.get(key)
        if cached and cached.get("mtime_ns") == mtime:
            return cached.get("count", 0), cached.get("size", 0)
        count = 0
        total = 0
        relatives = []
        for root, _dirs, files in os.walk(directory):
            for name in files:
                count += 1
                full = os.path.join(root, name)
                relatives.append(os.path.relpath(full, directory).replace("\\", "/").lower())
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
        self._measure_cache[key] = {"mtime_ns": mtime, "count": count, "size": total,
                                    "files": relatives}
        self._index_dirty = True
        return count, total

    def _relative_files(self, directory):
        """Relative (lower-cased, slash-separated) file names of one mod directory."""
        if not directory:
            return []
        key = _cache_key(directory)
        bucket = self._bulk_bucket(directory)
        if bucket is not None:
            return [item[0] for item in bucket["files"]]
        self._measure(directory)
        cached = self._measure_cache.get(key) or {}
        return cached.get("files") or []

    def _thumbnail(self, directory, picture, mtime_ns=None):
        if not directory:
            return ""
        # Thumbnail lookup used to resolve() every candidate for every mod on every
        # scan (6 realpath syscalls per mod); memoise per (dir, picture) with the
        # directory mtime so a refresh only pays one stat per mod.
        key = (_cache_key(directory), str(picture or ""))
        try:
            mtime = int(mtime_ns) if mtime_ns is not None else os.stat(directory).st_mtime_ns
        except (OSError, ValueError, TypeError):
            return ""
        cache = self.__dict__.setdefault("_thumb_cache", {})
        cached = cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        result = self._thumbnail_uncached(directory, picture, mtime, key)
        return result

    def _thumbnail_uncached(self, directory, picture, mtime, key):
        root = os.path.normpath(str(directory))
        bucket = self._bulk_bucket(root)
        known = bucket.get("files_set") if bucket else None
        candidates = [picture] if picture else []
        candidates.extend(THUMB_NAMES)
        found = ""
        for name in candidates:
            if not name:
                continue
            part = str(name).replace("\\", "/")
            if os.path.isabs(part) or part.startswith("/") or ".." in part.split("/"):
                continue
            # Lexically join: the candidate name is confined to the mod folder, so
            # no realpath() is needed. resolve() here used to cost ~12k syscalls and
            # 3.0 s on a 3000-mod library.
            candidate = os.path.join(root, part.replace("/", os.sep))
            if known is not None:
                if part.lower() not in known:
                    continue
            else:
                try:
                    if not os.path.isfile(candidate):
                        continue
                except OSError:
                    continue
            self._thumbs[candidate] = Path(candidate)
            found = candidate
            break
        self._thumb_cache[key] = (mtime, found)
        return found

    def thumbnail_for(self, directory, picture):
        return self._thumbnail(directory, picture)

    def _load_order(self, info, snap, entries):
        """Rank MODs the way the game loads them, not the way the list shows them.

        The game never reads the playset table: it reads <docs>/dlc_load.json and
        applies "enabled_mods" in array order, so the LAST entry wins every shared
        file. The launcher writes that array in playset position order, which is why
        list order usually is the truth - but a stale or hand-edited dlc_load.json
        (or loadOrder='alphabetical') breaks that assumption. Rank by the file the
        game actually reads, fall back to position order, and report the mismatch.
        """
        rows = {}
        for row in snap.get("mods") or []:
            mod_id = row.get("id")
            if mod_id:
                rows[mod_id] = row
        registry_of = {}
        for mod_id, row in rows.items():
            registry = str(row.get("gameRegistryId") or "").strip()
            if not registry and row.get("steamId"):
                registry = f"mod/ugc_{row['steamId']}.mod"
            if registry:
                registry_of[registry.lower()] = mod_id
        docs = str(info.get("docs") or "")
        dlc_path = os.path.join(docs, "dlc_load.json") if docs else ""
        ordered = []
        source = ""
        if dlc_path and os.path.isfile(dlc_path):
            data = None
            try:
                with open(dlc_path, "r", encoding="utf-8-sig") as handle:
                    data = json.load(handle)
            except (OSError, ValueError, TypeError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("enabled_mods"), list):
                for raw in data["enabled_mods"]:
                    mod_id = registry_of.get(str(raw).strip().lower())
                    if mod_id and mod_id not in ordered:
                        ordered.append(mod_id)
                if ordered:
                    source = "dlc_load"
        if not ordered:
            for entry in sorted(entries, key=lambda e: e.get("position") or 0):
                mod_id = entry.get("modId")
                if entry.get("enabled") and mod_id and mod_id not in ordered:
                    ordered.append(mod_id)
            if ordered:
                source = "position"
        if not ordered:
            ordered = sorted(rows, key=lambda mid: str(rows[mid].get("modFile")
                                                      or rows[mid].get("name") or "").lower())
            source = "filename"
        ranks = {mod_id: index for index, mod_id in enumerate(ordered)}
        mismatch = False
        if source == "dlc_load":
            listed = [e["modId"] for e in sorted(entries, key=lambda e: e.get("position") or 0)
                      if e.get("enabled")]
            seen = [mod_id for mod_id in listed if mod_id in ranks]
            actual = [mod_id for mod_id in ordered if mod_id in set(seen)]
            mismatch = bool(seen) and seen != actual
        detail = [{"mod_id": mod_id, "rank": ranks[mod_id],
                   "name": _clip(rows.get(mod_id, {}).get("name")
                                 or rows.get(mod_id, {}).get("displayName")
                                 or rows.get(mod_id, {}).get("modFile") or "", CONFLICT_NAME_CLIP)}
                  for mod_id in ordered]
        return {"ranks": ranks, "source": source, "mismatch": mismatch,
                "count": len(ordered), "entries": detail, "dlc_path": dlc_path}

    def _holder_rows(self, holders, relative, ranks, winner_id=None):
        """One row per MOD that ships this exact file: where its copy lives, its load
        rank and its size. Nothing is read here - the code preview is the separate
        ``conflict_diff`` action, so a scan stays free of file IO."""
        parts = relative.split("/")
        chosen = list(holders[:CONFLICT_HOLDERS_MAX])
        # The winner is always the last loaded holder, so with more holders than the
        # cap it would fall off the list - and "who wins" is the whole point.
        if winner_id is not None and not any(m.get("id") == winner_id for m in chosen):
            winner = next((m for m in holders if m.get("id") == winner_id), None)
            if winner is not None:
                chosen = chosen[:CONFLICT_HOLDERS_MAX - 1] + [winner]
                chosen.sort(key=_holder_sort_key(ranks))
        rows = []
        for mod in chosen:
            directory = str(mod.get("dir") or "")
            path = os.path.join(directory, *parts) if directory else ""
            size = 0
            if path:
                try:
                    size = os.stat(path).st_size
                except (OSError, ValueError):
                    size = 0
            rows.append({
                "id": mod.get("id"),
                "name": _clip(mod.get("name") or "", CONFLICT_NAME_CLIP),
                "rank": ranks.get(mod.get("id")),
                "enabled": bool(mod.get("enabled")),
                "file": _clip(path, CONFLICT_HOLDER_FILE_CLIP) if directory else "",
                "size": size,
            })
        return rows

    def _attach_conflicts(self, mods, load_order=None):
        ranks = (load_order or {}).get("ranks") or {}
        basis = (load_order or {}).get("source") or "position"
        owners = {}
        for mod in mods:
            directory = mod["dir"]
            if not directory:
                continue
            for relative in self._relative_files(directory):
                if relative in IGNORED_RELATIVE or relative in CONFLICT_SKIP_FILES:
                    continue
                if relative.startswith(CONFLICT_SKIP_DIRS):
                    continue
                owners.setdefault(relative, []).append(mod)
        conflicts = []
        for relative, holders in owners.items():
            if len(holders) < 2:
                continue
            names = sorted({_clip(m["name"], CONFLICT_NAME_CLIP) for m in holders})
            ranked = sorted(holders, key=_holder_sort_key(ranks))
            # Only ENABLED MODs are loaded by the game, so only they can win a file.
            # When every holder is disabled nobody overrides anything: say so instead
            # of crowning an arbitrary row.
            live = [mod for mod in ranked if ranks.get(mod.get("id")) is not None]
            winner = live[-1] if live else None
            winner_row = {"id": winner.get("id"), "name": _clip(winner.get("name") or "",
                                                                CONFLICT_NAME_CLIP),
                          "rank": ranks.get(winner.get("id"))} if winner else None
            # The rich row (every holder, its file, its load rank) is built for the
            # global "conflicts" list only, and only for the paths that survive the
            # payload cap. Copying it into every holder's "conflict_paths" used to
            # multiply the same data by the number of holders.
            entry = {"path": relative, "mods": names[:CONFLICT_NAMES_MAX],
                     "mod_count": len(names), "winner": winner_row,
                     "winner_basis": basis if winner else "none", "holders": [],
                     "holders_total": len(holders),
                     "holders_clipped": len(holders) > CONFLICT_HOLDERS_MAX}
            conflicts.append(entry)
            for mod in holders:
                mod["conflict_paths"].append({
                    "path": relative, "mods": entry["mods"], "mod_count": entry["mod_count"],
                    "winner_id": winner_row["id"] if winner_row else None,
                    "winner_name": winner_row["name"] if winner_row else "",
                    "winner_rank": winner_row["rank"] if winner_row else None,
                    "i_win": bool(winner_row) and mod.get("id") == winner_row["id"],
                })
        for mod in mods:
            mod["conflict_count"] = len(mod["conflict_paths"])
            # conflict_paths is clipped below; the total must stay the real number
            # so the UI can say "共 N 处，只显示 40 处".
            mod["conflict_paths_total"] = mod["conflict_count"]
            mod["conflict_paths"].sort(key=lambda item: item["path"])
            mod["conflict_paths"] = mod["conflict_paths"][:CONFLICT_PATHS_MAX]
        conflicts.sort(key=lambda item: (-item["mod_count"], item["path"]))
        for entry in conflicts[:CONFLICT_DETAIL_MAX]:
            ranked = sorted(owners[entry["path"]], key=_holder_sort_key(ranks))
            live = [mod for mod in ranked if ranks.get(mod.get("id")) is not None]
            winner_id = live[-1].get("id") if live else None
            entry["holders"] = self._holder_rows(ranked, entry["path"], ranks, winner_id)
        return conflicts


def _runtime(game_key="stellaris"):
    from .launcher_db import running_state

    return running_state(game_key)







