"""Path discovery for Stellaris, Steam libraries and the Paradox launcher database."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import games


def _dedupe(paths):
    seen, out = set(), []
    for p in paths:
        try:
            resolved = Path(p).resolve()
            key = os.path.normcase(str(resolved))
        except (TypeError, ValueError, OSError):
            continue
        if key not in seen and Path(p).is_dir():
            seen.add(key)
            out.append(Path(p))
    return out


def _registry_steam_path():
    try:
        import winreg
    except ImportError:
        return None
    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key, name in candidates:
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                if value:
                    return Path(value)
        except OSError:
            continue
    return None


def _vdf_paths(text):
    return [m.group(1).replace("\\\\", "\\") for m in re.finditer(r'"path"\s+"([^"]+)"', text)]


def steam_roots():
    roots = []
    reg = _registry_steam_path()
    if reg:
        roots.append(reg)
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            roots.append(Path(base) / "Steam")
    roots.extend([Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")])
    return _dedupe(roots)


def library_roots():
    out = []
    for root in steam_roots():
        out.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                out.extend(Path(p) for p in _vdf_paths(vdf.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return _dedupe(out)


DOC_FOLDER_NAMES = ("Documents", "文档", "我的文档", "My Documents")
USERS_FOLDER_NAMES = ("Users", "用户")


def _registry_documents():
    """Documents as Windows itself records it (survives moved profile folders)."""
    try:
        import winreg
    except ImportError:
        return None
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "Personal")
    except OSError:
        return None
    text = os.path.expandvars(str(value or "").strip())
    return Path(text) if text else None


def _known_documents():
    """Shell's real Documents folder (handles redirected / moved folders)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        ident = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
        fields = ident.fields
        guid = GUID(fields[0], fields[1], fields[2], (ctypes.c_ubyte * 8)(*ident.bytes[8:]))
        holder = ctypes.c_wchar_p()
        code = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(holder))
        if code != 0 or not holder.value:
            return None
        value = Path(holder.value)
        try:
            ctypes.windll.ole32.CoTaskMemFree(holder)
        except (OSError, AttributeError):
            pass
        return value
    except (OSError, AttributeError, ValueError):
        return None


def _fixed_drives():
    if os.name != "nt":
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (OSError, AttributeError):
        return []
    out = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = Path(chr(ord("A") + index) + ":\\")
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(str(root)) != 3:  # DRIVE_FIXED
                continue
        except (OSError, AttributeError):
            pass
        out.append(root)
    return out


def scan_root_documents(roots):
    r"""Documents folders that live beside (not inside) a user profile.

    Some installations relocate the whole profile tree, leaving
    ``C:\文档`` next to ``C:\用户`` while ``%USERPROFILE%\Documents`` is an
    empty leftover from an old Microsoft account.
    """
    out = []
    for root in roots:
        try:
            if not Path(root).is_dir():
                continue
        except OSError:
            continue
        for name in DOC_FOLDER_NAMES:
            candidate = Path(root) / name
            try:
                if candidate.is_dir():
                    out.append(candidate)
            except OSError:
                continue
        for users_name in USERS_FOLDER_NAMES:
            users_root = Path(root) / users_name
            try:
                if not users_root.is_dir():
                    continue
                profiles = list(users_root.iterdir())
            except OSError:
                continue
            for profile in profiles:
                try:
                    if not profile.is_dir():
                        continue
                except OSError:
                    continue
                for name in DOC_FOLDER_NAMES:
                    candidate = profile / name
                    try:
                        if candidate.is_dir():
                            out.append(candidate)
                    except OSError:
                        continue
    return out


_ROOTS_CACHE = {"at": 0.0, "value": None, "key": ""}
ROOTS_TTL = 60.0


def _document_roots():
    """Cached document roots; a full re-scan only happens once a minute."""
    import time
    now = time.time()
    key = os.environ.get("USERPROFILE") or str(Path.home())
    cached = _ROOTS_CACHE["value"]
    if (cached is not None and _ROOTS_CACHE["key"] == key
            and now - _ROOTS_CACHE["at"] < ROOTS_TTL):
        return cached
    value = _scan_document_roots()
    _ROOTS_CACHE["value"] = value
    _ROOTS_CACHE["at"] = now
    _ROOTS_CACHE["key"] = key
    return value


def _scan_document_roots():
    home = Path(os.environ.get("USERPROFILE") or str(Path.home()))
    roots = []
    for candidate in (_registry_documents(), _known_documents()):
        if candidate:
            roots.append(candidate)
    roots.append(home / "Documents")
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value) / "Documents")
    try:
        roots.extend(item / "Documents" for item in home.glob("OneDrive*"))
    except OSError:
        pass
    roots.extend(scan_root_documents(_fixed_drives()))
    return _dedupe(roots)


def _expand_path(raw):
    """Expand %USER_DOCUMENTS% / %USERPROFILE% / ~ used by launcher-settings.json."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return None
    home = Path(os.environ.get("USERPROFILE") or str(Path.home()))
    text = text.replace("%USER_DOCUMENTS%", str(home / "Documents"))
    text = text.replace("%USERPROFILE%", str(home))
    if text.startswith("~"):
        text = str(home) + text[1:]
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _game_data_path(game, profile):
    """The game's own launcher-settings.json decides which cache a branch uses.

    Public and beta builds can point gameDataPath at different folders
    (Stellaris / Stellaris Beta / Stellaris_test ...).  Reading this file is
    the only reliable way to keep those caches isolated.
    """
    if not game:
        return None
    settings = Path(game) / "launcher-settings.json"
    if not settings.is_file():
        return None
    try:
        payload = json.loads(settings.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    game_id = str(payload.get("gameId") or "").strip().lower()
    if game_id and game_id != profile["key"]:
        return None
    return _expand_path(payload.get("gameDataPath"))


# ---------------------------------------------------------------- launcher DB
# The Paradox launcher names its database after the update branch it runs on:
# the stable build writes "launcher-v2.sqlite", the open beta branch writes
# "launcher-v2-openbeta.sqlite" (older builds used "launcher-v2_openbeta.sqlite").
# Requiring the stable name made the manager report "没有找到 launcher-v2.sqlite"
# for every beta user until they renamed the file by hand.
LAUNCHER_DB_DEFAULT = "launcher-v2.sqlite"
_LAUNCHER_DB_RE = re.compile(
    r"^launcher-v2(?:[-_](?P<tag>[A-Za-z][A-Za-z0-9_]{0,31}))?\.sqlite$", re.IGNORECASE)
# Timestamped snapshots -- ours (`launcher-v2-<tag>-<stamp>.sqlite`) and the
# launcher's own (`launcher-v2-<stamp>.sqlite`) -- are backups, never live data.
_LAUNCHER_DB_STAMP_RE = re.compile(
    r"^launcher-v2-(?:[A-Za-z][A-Za-z0-9_]*-)?\d{8}-\d{6}(?:-\d+)?\.sqlite$", re.IGNORECASE)
# Hand-made copies users keep next to the real file ("launcher-v2_backup.sqlite",
# "launcher-v2-copy.sqlite").  A branch build never uses these names, so they can
# be skipped without losing the openbeta case.
_LAUNCHER_DB_COPY_RE = re.compile(
    r"^launcher-v2[-_](?:backup|bak|copy|old)(?:[-_].*)?\.sqlite$", re.IGNORECASE)
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sqlite_header(path):
    """Cheap "is this really a database" test so leftovers cannot win."""
    try:
        if not path.is_file() or path.stat().st_size < 512:
            return False
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def is_launcher_db(path):
    """True for a live launcher database name of any branch (never a backup).

    The name decides, not the sqlite header: a truncated or half-copied
    ``launcher-v2.sqlite`` is still the file the launcher would open, so the
    manager has to report "数据库已损坏" instead of "找不到数据库".
    """
    path = Path(path)
    name = path.name
    if _LAUNCHER_DB_COPY_RE.match(name) or _LAUNCHER_DB_STAMP_RE.match(name):
        return False
    return bool(_LAUNCHER_DB_RE.match(name))


def launcher_db_candidates(folder):
    """Live launcher databases inside `folder`, best candidate first.

    Both branch files are live data, so the newest one wins: after switching
    branches the other file is the stale leftover, and its playset list would
    silently hide every change the user made since.  A readable sqlite header
    outranks a corrupt leftover with a newer timestamp.
    """
    try:
        children = list(Path(folder).iterdir())
    except OSError:
        return []
    rows = []
    for child in children:
        if not is_launcher_db(child):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        rows.append((0 if _sqlite_header(child) else 1, -mtime,
                     child.name.lower() != LAUNCHER_DB_DEFAULT, child.name.lower(), child))
    rows.sort()
    return [row[4] for row in rows]


def launcher_db_path(folder):
    """The launcher database the game itself would read, or None."""
    found = launcher_db_candidates(folder)
    return found[0] if found else None


def launcher_db_name(folder):
    found = launcher_db_path(folder)
    return found.name if found else ""


def docs_candidates(game_key="stellaris"):
    """Every Stellaris* cache folder, including beta branch variants.

    Beta builds do not necessarily reuse "Stellaris": the launcher creates
    folders such as "Stellaris Beta" or "Stellaris_test".  Match by prefix
    and require a launcher database (any branch name) so empty leftovers are ignored.
    """
    profile = games.get(game_key)
    prefix = profile["docs_folder"].lower()
    out = []
    seen = set()
    for root in _document_roots():
        paradox = root / "Paradox Interactive"
        if not paradox.is_dir():
            continue
        try:
            children = list(paradox.iterdir())
        except OSError:
            continue
        for candidate in children:
            try:
                if not candidate.is_dir():
                    continue
                if not candidate.name.lower().startswith(prefix):
                    continue
                if launcher_db_path(candidate) is None:
                    continue
            except OSError:
                continue
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    # Keep the exact legacy folder even when its DB is temporarily missing.
    for root in _document_roots():
        candidate = root / "Paradox Interactive" / profile["docs_folder"]
        if candidate.is_dir():
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def _db_score(docs):
    db = launcher_db_path(docs)
    if db is None:
        return (-1, -1)
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute("select max(updatedOn) from playsets").fetchone()
            latest = int(row[0] or 0) if row else 0
            if latest <= 0:
                latest = int(db.stat().st_mtime * 1000)
            mods = con.execute("select count(*) from mods").fetchone()[0]
            return (latest, mods)
        finally:
            con.close()
    except Exception:
        return (0, 0)


def default_docs_dir(game_key="stellaris"):
    candidates = docs_candidates(game_key)
    if not candidates:
        profile = games.get(game_key)
        home = Path(os.environ.get("USERPROFILE") or str(Path.home()))
        return home / "Documents" / "Paradox Interactive" / profile["docs_folder"]
    # The official launcher may have been moved to OneDrive.  The local
    # Documents DB often keeps old playsets after the user switches to a new
    # collection, so the newest playset wins -- not the DB with the most rows.
    best = max(candidates, key=_db_score)
    return best


def detect(docs_override=None, game_override=None, game_key="stellaris"):
    profile = games.get(game_key)
    game = Path(game_override) if game_override else None
    if game is None:
        for lib in library_roots():
            candidate = lib / "steamapps" / "common" / profile["folder"]
            if candidate.is_dir():
                game = candidate
                break

    docs_source = "auto"
    docs = None
    if docs_override:
        candidate = Path(docs_override)
        # Users paste the database file itself as often as the folder.
        if candidate.is_file() and is_launcher_db(candidate):
            candidate = candidate.parent
        if launcher_db_path(candidate) is not None:
            docs = candidate
            docs_source = "override"
        else:
            docs_source = "auto_fallback"
    if docs is None:
        from_game = _game_data_path(game, profile)
        if from_game and launcher_db_path(from_game) is not None:
            docs = from_game
            docs_source = "game_data_path"
    if docs is None:
        docs = default_docs_dir(profile["key"])
    workshop = None
    if game is not None:
        lib = game.parent.parent.parent
        candidate = lib / "steamapps" / "workshop" / "content" / profile["appid"]
        if candidate.is_dir():
            workshop = candidate
    if workshop is None:
        for lib in library_roots():
            candidate = lib / "steamapps" / "workshop" / "content" / profile["appid"]
            if candidate.is_dir():
                workshop = candidate
                break

    version = ""
    raw_version = ""
    if game is not None:
        settings = game / "launcher-settings.json"
        if settings.is_file():
            try:
                payload = json.loads(settings.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                payload = None
            if isinstance(payload, dict):
                version = str(payload.get("version") or "")
                raw_version = str(payload.get("rawVersion") or "")

    db_file = launcher_db_path(docs)
    return {
        "key": profile["key"],
        "name": profile["name"],
        "label": profile["label"],
        "appid": profile["appid"],
        "docs": str(docs),
        "docs_source": docs_source,
        "docs_candidates": [str(item) for item in docs_candidates(profile["key"])],
        "game": str(game) if game else "",
        "workshop": str(workshop) if workshop else "",
        "db": str(db_file or (Path(docs) / LAUNCHER_DB_DEFAULT)),
        "db_name": db_file.name if db_file else "",
        "db_candidates": [str(item) for item in launcher_db_candidates(docs)],
        "mod_dir": str(docs / "mod"),
        "game_exe": str(game / profile["exe"]) if game else "",
        "version": version,
        "raw_version": raw_version,
    }
