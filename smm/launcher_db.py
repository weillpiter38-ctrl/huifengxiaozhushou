"""Safe read/write access to the Paradox launcher database (launcher-v2.sqlite)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import games

LAUNCHER_EXES = ("dowser.exe", "paradox launcher.exe", "bootstrapper.exe")
GAME_EXES = {profile["key"]: (profile["exe"],) for profile in games.GAMES.values()}

_FALSE_STRINGS = {"0", "false", "no", "off", "disabled", ""}


def _enabled_flag(value, default=True):
    """Parse an `enabled` field without Python truthiness surprises.

    Third-party callers send JSON strings, so `enabled: "false"` used to be
    truthy and silently *enabled* the mod.  Strings are parsed explicitly and
    a missing/None value falls back to `default`.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_STRINGS
    return bool(value)


# `tasklist` costs 150-480 ms per spawn, and one payload build asks for the
# process list more than once (catalog + decorator). Cache it for a blink of an
# eye; write guards bypass the cache so a game that just started still blocks.
_PROC_TTL = 1.2
# Write guards ask for a *fresh* list, but a payload build may ask several times
# within one request. Re-spawn only when the cached list is older than this; the
# visible cost is that a game closed inside 0.3 s may still look "running" once.
_PROC_FRESH_TTL = 0.3
_PROC_CACHE = {"at": 0.0, "names": None}

# A locked launcher database must fail fast: the UI shows a retry hint, so waiting
# 8 s per connection only makes the user stare at a spinner.
_LOCK_TIMEOUT = 1.0
_WRITE_RETRY_BUDGET = 0.4

# Backups are created from the request threads: picking a free file name, copying
# the database and pruning old copies all race with each other. One lock keeps
# the naming/hand-over/prune steps atomic inside this process.
_BACKUP_LOCK = threading.Lock()


def safe_mtime(path, default=0.0):
    """mtime for sorting, tolerant of a file another thread just pruned."""
    try:
        return path.stat().st_mtime
    except OSError:
        return default


def _drop_quiet(path, only_if_empty=False):
    """Remove a scratch file, ignoring "already gone" and locked-file errors.

    With ``only_if_empty`` we only clear the zero-byte name reservation, so a
    concurrent process that already renamed its dump into place keeps its file.
    """
    try:
        if only_if_empty and path.stat().st_size > 0:
            return
        path.unlink()
    except OSError:
        pass


def _tasklist(fresh=False):
    now = time.monotonic()
    cached = _PROC_CACHE["names"]
    limit = _PROC_FRESH_TTL if fresh else _PROC_TTL
    if cached is not None and now - _PROC_CACHE["at"] < limit:
        return list(cached)
    names = _tasklist_uncached()
    _PROC_CACHE["at"] = time.monotonic()
    _PROC_CACHE["names"] = list(names) if names is not None else None
    return names


def _tasklist_uncached():
    try:
        completed = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return None
        names = []
        for line in completed.stdout.splitlines():
            if not line.startswith('"'):
                continue
            names.append(line.split('","')[0].strip('"').lower())
        return names
    except (OSError, subprocess.SubprocessError):
        return None


def running_state(game_key="stellaris", fresh=False):
    names = _tasklist(fresh=fresh)
    profile = games.get(game_key)
    exes = GAME_EXES.get(profile["key"], (profile["exe"],))
    tokens = {profile["exe"].lower().removesuffix(".exe"), "dowser", "paradox"}
    if names is None:
        # A timed-out tasklist must not block or fake a save.
        return {"launcher_running": False, "game_running": False,
                "steam_running": False, "processes": [], "unknown": True}
    name_set = set(names)
    return {
        "launcher_running": any(name in name_set for name in LAUNCHER_EXES),
        "game_running": any(name in name_set for name in exes),
        "steam_running": "steam.exe" in name_set,
        "processes": sorted(n for n in name_set if any(token in n for token in tokens)),
        "unknown": False,
    }


class LauncherDB:
    def __init__(self, path):
        self.path = Path(path)
        self.last_skipped = 0

    def exists(self):
        return self.path.is_file()

    def snapshot(self):
        if not self.exists():
            return {"mods": [], "playsets": [], "entries": []}
        con = None
        try:
            con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=_LOCK_TIMEOUT)
            con.row_factory = sqlite3.Row
            mods = [dict(r) for r in con.execute(
                "select id, steamId, name, displayName, dirPath, version, requiredVersion, "
                "tags, status, source, timeUpdated, size, thumbnailPath, gameRegistryId from mods")]
            columns = {row[1] for row in con.execute("pragma table_info(playsets)")}
            removed_filter = " where coalesce(isRemoved, 0) = 0" if "isRemoved" in columns else ""
            playsets = [dict(r) for r in con.execute(
                "select id, name, isActive, loadOrder, createdOn, updatedOn from playsets"
                + removed_filter + " order by isActive desc, createdOn asc")]
            entries = [dict(r) for r in con.execute(
                "select playsetId, modId, enabled, position from playsets_mods order by position asc")]
        except sqlite3.Error as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                # t1 measured an 11.5 s hang when sqlite retried silently: fail fast
                # with a message the user can act on instead.
                raise ValueError(
                    "启动器数据库正被其他程序占用，请关闭 Paradox 启动器或游戏后重试。") from exc
            if "readonly" in message or "read-only" in message:
                raise ValueError("启动器数据库是只读的，未读取到任何内容。") from exc
            # A truncated/corrupt launcher database must surface as a readable
            # message instead of a raw sqlite3 traceback in the console.
            raise ValueError(
                f"启动器数据库无法读取，文件可能已损坏：{self.path}。"
                "请在 Paradox 启动器里重新生成，或用备份恢复。") from exc
        finally:
            if con is not None:
                con.close()
        return {"mods": mods, "playsets": playsets, "entries": entries}
    def delete_mods(self, mod_ids):
        clean = [str(item) for item in mod_ids if str(item)]
        if not clean:
            return 0
        def mutate(con):
            total = 0
            for mod_id in clean:
                con.execute("delete from playsets_mods where modId = ?", (mod_id,))
                try:
                    con.execute("delete from mods_dependencies where modId = ?", (mod_id,))
                except sqlite3.Error:
                    pass
                total += con.execute("delete from mods where id = ?", (mod_id,)).rowcount
            return total
        return self._write(mutate)

    def backup(self, dest_dir, keep=40, tag=""):
        if not self.exists():
            raise FileNotFoundError(str(self.path))
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with _BACKUP_LOCK:
            dest = self._claim_backup_name(dest_dir, stamp, tag)
            # `VACUUM INTO` refuses to write over an existing file, and writing
            # straight onto `dest` would let a second process (the thread lock is
            # process-local) beat us and leave a truncated backup behind. Dump to a
            # per-process temp file first, then `os.replace` it into place: the
            # rename is atomic, so a reader either sees no file or a complete one.
            tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}-{threading.get_ident()}")
            try:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                src = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=3)
                try:
                    # A locked launcher database must fail fast with a readable message:
                    # the old sqlite3 backup API retried SQLITE_BUSY forever.
                    src.execute("PRAGMA busy_timeout = 3000")
                    src.execute("VACUUM INTO ?", (str(tmp),))
                finally:
                    src.close()
                os.replace(str(tmp), str(dest))
            except sqlite3.DatabaseError as exc:
                _drop_quiet(tmp)
                _drop_quiet(dest, only_if_empty=True)
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    raise ValueError(
                        "启动器数据库正被其他程序占用，请关闭 Paradox 启动器或游戏后重试。") from exc
                raise ValueError(
                    f"备份启动器数据库失败：{exc}。文件可能已损坏，请在 Paradox 启动器里重新生成。"
                ) from exc
            except OSError as exc:
                _drop_quiet(tmp)
                _drop_quiet(dest, only_if_empty=True)
                raise ValueError(f"无法写入备份目录：{exc}") from exc
            backups = sorted(dest_dir.glob("launcher-v2-*.sqlite"), key=safe_mtime, reverse=True)
            for old in backups[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            # Sidecar metadata (<backup>.sqlite.json) used to outlive its sqlite file
            # forever, so backups/ grew without bound even at keep=40.
            for meta in dest_dir.glob("launcher-v2-*.sqlite.json"):
                if not meta.with_suffix("").exists():
                    try:
                        meta.unlink()
                    except OSError:
                        pass
        return dest
    @staticmethod
    def _claim_backup_name(dest_dir, stamp, tag=""):
        """Reserve the next free `launcher-v2-[<tag>-]<stamp>[-n].sqlite` atomically."""
        prefix = f"launcher-v2-{tag}-" if tag else "launcher-v2-"
        counter = 0
        while True:
            suffix = "" if counter == 0 else f"-{counter + 1}"
            candidate = dest_dir / f"{prefix}{stamp}{suffix}.sqlite"
            try:
                # `os.O_EXCL` makes the reservation atomic between threads, so two
                # backups taken in the same second cannot overwrite each other.
                handle = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                counter += 1
                if counter > 999:
                    raise ValueError("备份文件过多，请先清理 backups 目录。")
                continue
            os.close(handle)
            return candidate
    def _write(self, mutate):
        last_error = None
        deadline = time.monotonic() + _WRITE_RETRY_BUDGET
        attempt = 0
        while True:
            con = None
            try:
                con = sqlite3.connect(str(self.path), timeout=_LOCK_TIMEOUT, isolation_level=None)
                con.execute("PRAGMA foreign_keys = ON")
                con.execute("BEGIN IMMEDIATE")
                try:
                    result = mutate(con)
                    con.execute("COMMIT")
                    return result
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    if "readonly" in message or "read-only" in message:
                        raise ValueError("启动器数据库是只读的，未写入任何内容。") from exc
                    if "unable to open" in message or "permission" in message:
                        raise ValueError("没有写入启动器数据库的权限，未写入任何内容。") from exc
                    raise
                last_error = exc
                attempt += 1
                if time.monotonic() >= deadline or attempt >= 3:
                    break
                time.sleep(0.15)
            finally:
                if con is not None:
                    con.close()
        raise ValueError("启动器数据库正被占用，请稍后重试。") from last_error

    def save_order(self, playset_id, entries, positions=None):
        """Replace the playset contents. entries: [{"mod_id", "enabled"}]. Returns rows written.

        positions: 可选的 {mod_id: position} 显式顺序。前端把「用户排的顺序」（含已停用的
        MOD）一起传进来，position 就不再单纯依赖 entries 的先后。不传时退回原来的
        enumerate 行为，保持向后兼容。
        """
        clean = []
        seen = set()
        for entry in entries:
            mod_id = str(entry.get("mod_id") or "").strip()
            if not mod_id or mod_id in seen:
                continue
            seen.add(mod_id)
            clean.append((mod_id, 1 if _enabled_flag(entry.get("enabled")) else 0))

        explicit = {}
        if positions:
            for key, value in dict(positions).items():
                try:
                    explicit[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

        def mutate(con):
            # Stale rows (deleted/unsubscribed mods) must not break the foreign key.
            valid = {row[0] for row in con.execute("select id from mods")}
            rows = [(mod_id, enabled) for mod_id, enabled in clean if mod_id in valid]
            self.last_skipped = len(clean) - len(rows)
            if explicit:
                # 显式 position：先按给定位置排序，缺位置的按出现次序补齐，再压平成 0..n-1，
                # 保证 playsets_mods.position 连续且没有并列。
                indexed = []
                for order, (mod_id, enabled) in enumerate(rows):
                    indexed.append((explicit.get(mod_id, 10 ** 6 + order), order, mod_id, enabled))
                indexed.sort(key=lambda item: (item[0], item[1]))
                rows = [(mod_id, enabled) for _, _, mod_id, enabled in indexed]
            con.execute("delete from playsets_mods where playsetId = ?", (playset_id,))
            for position, (mod_id, enabled) in enumerate(rows):
                con.execute(
                    "insert into playsets_mods (playsetId, modId, enabled, position) values (?, ?, ?, ?)",
                    (playset_id, mod_id, enabled, position),
                )
            con.execute("update playsets set updatedOn = ? where id = ?", (int(time.time() * 1000), playset_id))
            return con.execute(
                "select count(*) from playsets_mods where playsetId = ?", (playset_id,)).fetchone()[0]

        return self._write(mutate)

    def set_active(self, playset_id):
        def mutate(con):
            con.execute("update playsets set isActive = 0")
            con.execute("update playsets set isActive = 1 where id = ?", (playset_id,))
            con.execute("update playsets set updatedOn = ? where id = ?", (int(time.time() * 1000), playset_id))
            return con.execute(
                "select count(*) from playsets where isActive = 1 and id = ?", (playset_id,)).fetchone()[0]

        return self._write(mutate)

    def create_playset(self, name, mod_ids=()):
        """Create a playset. mod_ids may hold ids, {"mod_id","enabled"} dicts, or (id, enabled) pairs."""
        playset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        rows = []
        for item in mod_ids:
            if isinstance(item, dict):
                mod_id = str(item.get("mod_id") or item.get("id") or "").strip()
                enabled = 1 if _enabled_flag(item.get("enabled")) else 0
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                mod_id, enabled = str(item[0] or "").strip(), (1 if _enabled_flag(item[1]) else 0)
            else:
                mod_id, enabled = str(item or "").strip(), 1
            if mod_id:
                rows.append((mod_id, enabled))

        def mutate(con):
            con.execute(
                "insert into playsets (id, name, isActive, loadOrder, createdOn, updatedOn) "
                "values (?, ?, 0, 'custom', ?, ?)",
                (playset_id, name, now, now),
            )
            # Same guard as save_order: a stale/uninstalled id must not leave an
            # orphan row behind (and must not trip the launcher's foreign key).
            # ``rows`` above already carries the per-entry enabled flag, so only
            # filter - never rebuild it with a hard-coded True.
            try:
                valid = {str(row[0]) for row in con.execute("select id from mods")}
            except sqlite3.OperationalError:
                # Minimal fixtures may have no mods table; trust the caller then.
                valid = None
            kept = [(mid, flag) for mid, flag in rows if valid is None or mid in valid]
            self.last_skipped = len(rows) - len(kept)
            for position, (mod_id, enabled) in enumerate(kept):
                con.execute(
                    "insert into playsets_mods (playsetId, modId, enabled, position) values (?, ?, ?, ?)",
                    (playset_id, mod_id, enabled, position),
                )
            return playset_id

        return self._write(mutate)

    def rename_playset(self, playset_id, name):
        """Rename one playset (qa4-a P2-3: the UI had no rename path at all)."""
        def mutate(con):
            cur = con.execute("select count(*) from playsets where id = ?", (playset_id,)).fetchone()[0]
            if not cur:
                raise ValueError("找不到这个播放集。")
            con.execute("update playsets set name = ?, updatedOn = ? where id = ?",
                        (name, int(time.time() * 1000), playset_id))
            return name

        return self._write(mutate)

    def delete_playset(self, playset_id):
        def mutate(con):
            con.execute("delete from playsets_mods where playsetId = ?", (playset_id,))
            con.execute("delete from playsets where id = ?", (playset_id,))
            remaining = con.execute("select count(*) from playsets").fetchone()[0]
            if remaining:
                con.execute(
                    "update playsets set isActive = 1 where id = "
                    "(select id from playsets order by isActive desc, createdOn asc limit 1)")
            return remaining

        return self._write(mutate)
