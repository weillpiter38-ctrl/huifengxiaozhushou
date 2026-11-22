"""Application services behind a small JSON API."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import zipfile
import zlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from .catalog import Catalog, conflict_preview
from .launcher_db import GAME_EXES, LauncherDB, running_state, safe_mtime
from . import games
from . import saves as save_service
from . import updater
from . import crash
from . import sorting
from .userdata import UserData
from . import ai as ai_service
from . import paths
from . import preview
from .workshop import WorkshopService

APP_NAME = "Huifeng's Mod Manager"
# 版本号单一来源见 smm/version.py（net.py 的 User-Agent 也读它）。
from .version import APP_VERSION  # noqa: E402  (放在常量之后以便旧引用继续可读)

# Drive roots and OS-managed folders are never a sane MOD directory: pointing the
# scanner at them walks the whole Windows tree and can freeze the UI.
_SYSTEM_DIR_NAMES = frozenset({
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
})


def looks_like_system_dir(path) -> bool:
    """True for a drive root (C:\\, D:\\) or any path inside an OS-managed folder."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = Path(str(path))
    parts = [part.lower() for part in resolved.parts]
    if len(parts) <= 1:
        return True
    return any(part in _SYSTEM_DIR_NAMES for part in parts[1:])

# Every action that can write game files, the launcher DB, or manager data.
SCAN_CACHE_SCHEMA = 2  # 冲突明细字段变化时递增，旧缓存自动作废

READ_ONLY_BLOCKED = frozenset({
    "save_order", "set_active", "create_playset", "rename_playset", "delete_playset", "add_mods",
    "remove_mods", "launch_game", "enable_all_dlc", "save_game_settings",
    "save_backup", "save_restore", "snapshot_mods", "restore_mod_snapshot",
    "delete_mod_snapshot", "manager_files_delete", "cleanup_delete",
    "import_local_mod", "merge_enabled_mods", "workshop_prepare", "workshop_upload_status",
    "workshop_launch", "workshop_read_result", "import_collection",
    "import_share_code", "import_playset", "apply_save", "safe_mode_apply",
    "export_playset_file", "set_game_path", "set_mod_path", "reset_paths",
    "switch_game", "restore_backup", "download_update", "apply_update",
    "delete_merged_pack", "folder_create", "folder_rename", "folder_delete",
    "folder_assign", "folder_reorder", "save_note", "ai_save_settings", "ai_clear_key",
    "set_docs_path",
})


STEAMCMD_ERRORS = (
    ("invalid password", "密码错误", "Steam 密码不对。重新运行上传，仔细输入密码（输入时不显示字符）。"),
    ("invalid login auth code", "验证码错误", "Steam Guard 验证码不对或已过期，重新上传并输入最新验证码。"),
    ("two-factor code", "需要验证码", "该账号开了两步验证，需要输入手机/Authenticator 的验证码。"),
    ("limit exceeded", "更新太频繁", "Steam 限制同一物品的更新频率。等 15-30 分钟再传，期间改动会累积到下一次上传。"),
    ("file not found", "物品不存在", "工坊条目 ID 在这账号下不存在。清空“工坊条目 ID”改为新建，或核对 ID。"),
    ("access denied", "没有权限", "该工坊物品不属于当前登录账号，无法更新。"),
    ("no connection", "网络不通", "连不上 Steam，检查代理/VPN 后重试。"),
    ("failed to connect", "网络不通", "连不上 Steam，检查代理/VPN 后重试。"),
    ("timeout", "网络超时", "网络太慢或被拦截，换网络或关掉代理再试。"),
    ("disk full", "磁盘空间不足", "SteamCMD 所在磁盘空间不够，清理后重试。"),
    ("content folder", "内容目录无效", "MOD 目录不存在或没有可上传文件，检查 MOD 是否还在。"),
)


def classify_steamcmd_output(text):
    """把 SteamCMD 输出归类成可读状态。"""
    low = (text or "").lower()
    percent = None
    for line in reversed((text or "").splitlines()):
        match = re.search(r"\[\s*(\d{1,3})%\]", line)
        if match:
            percent = int(match.group(1))
            break
    if "committingupdate...success" in low.replace(" ", "") or "publishfileid" in low and "success" in low:
        return {"stage": "done", "percent": 100, "ok": True, "error": "", "hint": "上传成功。"}
    for needle, label, hint in STEAMCMD_ERRORS:
        if needle in low:
            return {"stage": "error", "percent": percent, "ok": False, "error": label, "hint": hint}
    if "uploading content" in low:
        return {"stage": "uploading", "percent": percent, "ok": True, "error": "", "hint": "正在上传内容…"}
    if "preparing content" in low:
        return {"stage": "preparing", "percent": percent, "ok": True, "error": "", "hint": "正在准备上传…"}
    if "logging in" in low:
        return {"stage": "login", "percent": percent, "ok": True, "error": "", "hint": "正在登录 Steam…"}
    return {"stage": "idle", "percent": percent, "ok": True, "error": "", "hint": "等待 SteamCMD 输出。"}


class Api:
    def __init__(self, catalog: Catalog, data_dir, read_only=False, sandbox=False, workshop=None,
                 game_key="stellaris", root_data=None, docs_explicit=False):
        self.catalog = catalog
        self.game_key = games.normalize(game_key)
        self.root_data = Path(root_data) if root_data else Path(data_dir)
        self.data_dir = Path(data_dir)
        self._load_path_settings()
        try:
            self.catalog.set_cache_path(self.data_dir / "cache" / "mod-index.json")
        except Exception:
            pass
        self.read_only = read_only
        self.sandbox = sandbox
        # Set by run.py when the user passed --docs. Sandbox isolation must key off
        # this flag, not off paths "docs_source": run.py always hands Catalog the
        # detected docs folder, so docs_source reads "override" even on a normal
        # launch and the old check disabled isolation everywhere.
        self.docs_explicit = bool(docs_explicit)
        self.write_mod_root_override = None
        # Merge picks a free pack name and then writes; without a lock two clicks
        # arriving together both see the same name free and one pack overwrites the
        # other while merged-packs.json keeps two rows for a single zip.
        self._merge_lock = threading.Lock()
        # Every launcher-v2.sqlite write (save_order / restore_backup / play set
        # edits) goes through _write_and_refresh; without a lock, 8 browsers doing
        # 50 restores each made 44% of the calls fail with "备份文件读取失败".
        self._db_lock = threading.RLock()
        self._isolate_sandbox_mod_dir()
        self._register_scan_sources()
        self.db = LauncherDB(catalog.vision()["db"])
        self.workshop = workshop or WorkshopService(self.data_dir)
        self.userdata = UserData(self.data_dir)
        self.last_backup = ""
        self.last_warning = ""

    # ------------------------------------------------------------- helpers
    def _isolate_sandbox_mod_dir(self):
        """Pick the folder sandbox mode is allowed to write merged/imported MODs into.

        ``--sandbox`` used to redirect only launcher-v2.sqlite, so merge/import/
        cleanup still wrote and deleted files in the real ``Documents\\...\\mod``.
        Reads must keep using the real mod dir (the user still wants to see their
        library), so this only decides the *write* root: ``<data-dir>\\sandbox\\mod``
        whenever the docs root was not chosen explicitly with ``--docs``.
        """
        if not getattr(self, "sandbox", False):
            return
        # ``--docs`` only means "use this library" when paths.detect() actually
        # accepted it. A typo'd path falls back to the auto-detected real docs
        # folder, and honouring docs_explicit alone would then write the real
        # game library -- exactly what --sandbox must prevent.
        if getattr(self, "docs_explicit", False):
            try:
                accepted = self.catalog.vision().get("docs_source") == "override"
            except Exception:  # noqa: BLE001 - detection problems must not disable isolation
                accepted = False
            if accepted:
                return
        try:
            mirror = Path(self.data_dir) / "sandbox" / "mod"
            mirror.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        self.write_mod_root_override = mirror

    def _register_scan_sources(self):
        """Tell the catalogue where to look for local packs the launcher ignores.

        Two kinds of pack have no launcher DB row: the ones imported here (they are
        only registered after the Paradox launcher starts once) and the ones written
        into the sandbox mirror (they are never registered at all). Both must still
        show up in the mod list, otherwise the user cannot see what they just
        imported and 一键清理 treats the pack as an orphan.
        """
        sources = []
        try:
            mod_dir = Path(self.catalog.vision()["mod_dir"])
        except Exception:  # noqa: BLE001 - path detection must never break start-up
            mod_dir = None
        if mod_dir is not None:
            # Packs the user dropped into the real mod folder by hand have no
            # launcher row either; they are still `real`, not sandbox mirror packs.
            sources.append((mod_dir, "real"))
        write_root = getattr(self, "write_mod_root_override", None)
        if write_root is not None:
            try:
                if mod_dir is None or Path(write_root).resolve() != mod_dir.resolve():
                    sources.append((Path(write_root), "sandbox"))
            except OSError:
                sources.append((Path(write_root), "sandbox"))
        try:
            self.catalog.extra_mod_dirs = sources
        except AttributeError:
            pass

    def write_mod_root(self):
        """Folder every filesystem write (merge/import/cleanup/delete) must use."""
        override = getattr(self, "write_mod_root_override", None)
        if override:
            return Path(override)
        return Path(self.catalog.vision()["mod_dir"])

    def _sandbox_write_root(self, root):
        """Redirect a would-be real-library directory into the sandbox mirror."""
        target = Path(root)
        if not getattr(self, "sandbox", False):
            return target
        return self.write_mod_root()

    def _path_settings_file(self):
        return self.data_dir / "paths.json"

    def _load_path_settings(self):
        path = self._path_settings_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            # paths.json 里可能是 null / [] / "xxx" 这类合法 JSON 但类型不对，忽略即可，不能让启动崩掉。
            return
        game = str(data.get("game") or "").strip()
        mod_dir = str(data.get("mod_dir") or "").strip()
        if game and Path(game).exists():
            self.catalog.game = game
        if mod_dir and Path(mod_dir).is_dir():
            self.catalog.mod_dir_override = mod_dir
        docs = str(data.get("docs") or "").strip()
        if docs and paths.launcher_db_path(Path(docs)) is not None:
            self.catalog.docs = Path(docs)
        self.catalog.invalidate()

    def _save_path_settings(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = {"game": self.catalog.game or "",
                       "mod_dir": str(self.catalog.mod_dir_override or ""),
                       "docs": str(self.catalog.docs or "")}
            self._path_settings_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            # A silent failure would leave the UI showing a path that is gone after restart.
            raise ValueError(
                "路径设置无法写入，paths.json 可能被占用或本身是目录，本次修改不会保存。") from exc

    def set_game_path(self, payload):
        raw = str(payload.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise ValueError("路径不能为空。")
        if len(raw) > 4096:
            raise ValueError("路径过长（最多 4096 个字符）。")
        profile = games.get(self.game_key)
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if candidate.is_file() and candidate.name.lower() == profile["exe"]:
            candidate = candidate.parent
        exe = candidate / profile["exe"]
        if not exe.is_file():
            raise ValueError(f"这个目录里没有 {profile['exe']}，请选择游戏安装目录或 {profile['exe']}。")
        self.catalog.game = str(candidate)
        self.catalog.invalidate()
        self._save_path_settings()
        return self.bootstrap()

    def set_mod_path(self, payload):
        raw = str(payload.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise ValueError("路径不能为空。")
        if len(raw) > 4096:
            raise ValueError("路径过长（最多 4096 个字符）。")
        profile = games.get(getattr(self, "game_key", "stellaris"))
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not candidate.is_absolute():
            raise ValueError("请填写完整的绝对路径，例如 "
                             r"D:\SteamLibrary\steamapps\common\Stellaris\mod。")
        if not candidate.is_dir():
            raise ValueError("MOD 目录不存在。请选择游戏 Documents\\Paradox Interactive\\"
                             f"{profile['docs_folder']}\\mod 目录。")
        if looks_like_system_dir(candidate):
            raise ValueError("这是系统目录或磁盘根目录，不能作为 MOD 目录。")
        # Pointing the mod folder at the game install or the Paradox documents root
        # would let import/merge/cleanup write into game files, so refuse it.
        for label, known in (("游戏安装目录", self.catalog.game),
                             ("Paradox 存档/配置目录", self.catalog.docs)):
            if not known:
                continue
            try:
                if candidate.resolve() == Path(known).resolve():
                    raise ValueError(f"这是{label}，不能当成 MOD 目录，请选择 ...\\{profile['docs_folder']}\\mod。")
            except OSError:
                continue
        try:
            if (candidate / profile["exe"]).is_file():
                raise ValueError(f"这是{profile['label']}游戏安装目录，不能当成 MOD 目录，"
                                 f"请选择 ...\\{profile['docs_folder']}\\mod。")
        except OSError:
            pass
        self.catalog.mod_dir_override = str(candidate)
        self.catalog.invalidate()
        self._save_path_settings()
        return self.bootstrap()

    def reset_paths(self, payload):
        self.catalog.game = None
        self.catalog.mod_dir_override = None
        self.catalog.docs = None
        self.catalog.invalidate()
        self._save_path_settings()
        return self.bootstrap()

    def set_docs_path(self, payload):
        r"""Let the user point at the right Documents\Paradox Interactive folder."""
        raw = str(payload.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise ValueError("路径不能为空。")
        if len(raw) > 4096:
            raise ValueError("路径过长（最多 4096 个字符）。")
        profile = games.get(self.game_key)
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if candidate.is_file() and paths.is_launcher_db(candidate):
            candidate = candidate.parent
        docs = None
        for probe in (candidate,
                      candidate / "Paradox Interactive" / profile["docs_folder"],
                      candidate / profile["docs_folder"]):
            try:
                if paths.launcher_db_path(probe) is not None:
                    docs = probe
                    break
            except OSError:
                continue
        if docs is None:
            raise ValueError("这个目录里没有 launcher-v2.sqlite 或 launcher-v2-<分支>.sqlite"
                             "（例如 launcher-v2-openbeta.sqlite），请选择 ...\\Paradox Interactive\\"
                             + profile["docs_folder"] + " 这样的目录。")
        self.catalog.docs = docs
        self.catalog.invalidate()
        self._save_path_settings()
        self.db = LauncherDB(self.catalog.vision()["db"])
        return self.bootstrap()

    def _decorate(self, payload):
        # The catalog payload can be shared by concurrent requests (20 s cache and
        # single-flight scans), so every write below must land on a private copy.
        payload = dict(payload)
        payload["mods"] = [dict(mod) for mod in payload.get("mods") or []]
        payload["available"] = [dict(mod) for mod in payload.get("available") or []]
        profile = games.get(self.game_key)
        payload["app"] = {"name": APP_NAME, "version": APP_VERSION, "sandbox": self.sandbox,
                          "game_key": profile["key"], "game_name": profile["name"], "game_label": profile["label"]}
        game_payload = dict(payload.get("game") or {}) if isinstance(payload.get("game"), dict) else {}
        game_payload.update({"key": profile["key"], "name": profile["name"], "label": profile["label"],
                             "appid": profile["appid"], "features": profile["features"]})
        payload["game"] = game_payload
        payload["games"] = [{"key": item["key"], "name": item["name"], "label": item["label"]}
                            for item in games.GAMES.values()]
        payload["read_only"] = self.read_only
        payload["data_dir"] = str(self.data_dir)
        payload["backups"] = self._backups()
        payload["index_cache"] = self.catalog.cache_stats()
        payload["db_exists"] = self.db.exists()
        try:
            payload["dlc"] = self.dlc_status({})
        except Exception:
            payload["dlc"] = {"total": 0, "enabled": 0, "disabled": 0, "items": []}
        payload["history"] = self._load_history()[:8]
        user = self.userdata.snapshot()
        payload["notes"] = user["notes"]
        payload["folders"] = user["folders"]
        payload["assignments"] = user["assignments"]
        for group in ("mods", "available"):
            for mod in payload.get(group, []):
                mod["note"] = user["notes"].get(mod["id"], "")
                mod["folder_id"] = user["assignments"].get(mod["id"], "")
        return payload

    # ------------------------------------------------------------- AI
    def ai(self):
        service = getattr(self, "_ai_service", None)
        if service is None or service.settings.path.parent != self.data_dir:
            service = ai_service.AiService(self.data_dir, self.workshop, self.game_key)
            self._ai_service = service
        service.workshop = self.workshop
        service.game_key = self.game_key
        return service

    def ai_save_settings(self, payload):
        service = self.ai()
        key = payload.get("api_key")
        # 空字符串等于「不动已存的 key」：前端保存成功后会把输入框清空，
        # 之后点「测试连接」如果照原样回传 ""，旧 key 就会被抹掉。
        if isinstance(key, str) and not key.strip():
            key = None
        cookie = payload.get("steam_cookie")
        snapshot = service.settings.save(
            api_key=None if key is None else str(key),
            base_url=payload.get("base_url"),
            model=payload.get("model"),
            steam_cookie=None if cookie is None else str(cookie),
        )
        return snapshot

    def ai_analyze(self, payload):
        service = self.ai()
        scope = str(payload.get("scope") or "both")
        if scope not in ("both", "description", "comments"):
            scope = "both"
        try:
            limit = int(payload.get("comment_limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        ids = payload.get("mod_ids")
        if ids is None and payload.get("mod_id"):
            ids = [payload.get("mod_id")]
        if not isinstance(ids, list) or not ids:
            raise ValueError("没有选择要分析的 MOD。")
        snapshot = getattr(self, "_last_payload", None) or self.bootstrap()
        mods_by_id = {}
        for key in ("mods", "available"):
            for mod in (snapshot.get(key) or []):
                if isinstance(mod, dict) and mod.get("id"):
                    mods_by_id[str(mod["id"])] = mod
        selected = []
        for mod_id in ids:
            mod = mods_by_id.get(str(mod_id))
            if mod:
                selected.append(self._with_workshop_text(mod))
        if not selected:
            raise ValueError("没有找到可分析的 MOD（可能已被删除或未安装）。")
        order = []
        for index, mod in enumerate(snapshot.get("mods") or [], start=1):
            if not isinstance(mod, dict) or not mod.get("enabled"):
                continue
            order.append("%d: %s" % (index, mod.get("name") or mod.get("id")))
        analysis = snapshot.get("analysis") or {}
        conflicts = ""
        if analysis:
            conflicts = "%s 处定义冲突，%s 处本地化冲突" % (
                analysis.get("conflict_total") or 0, analysis.get("loc_conflict_total") or 0)
        context = {
            "order": order[:40],
            "conflicts": conflicts,
            "game_version": str((snapshot.get("game") or {}).get("version") or ""),
        }
        for mod in selected:
            mod["_context"] = context
        return service.analyze(selected, scope=scope, comment_limit=limit)

    def _with_workshop_text(self, mod):
        """Attach workshop title/description/appid so the AI prompt has context."""
        enriched = dict(mod)
        profile = games.get(self.game_key)
        enriched.setdefault("appid", str(profile["appid"]))
        steam_id = str(mod.get("steam_id") or "")
        workshop = mod.get("workshop") or {}
        enriched.setdefault("workshop_title", workshop.get("title") or "")
        if steam_id.isdigit() and self.workshop:
            try:
                item = (self.workshop.details([steam_id]) or {}).get(steam_id) or {}
            except (OSError, ValueError):
                item = {}
            if item:
                enriched["workshop_title"] = item.get("title") or enriched.get("workshop_title") or ""
                enriched["workshop_description"] = item.get("description") or ""
        return enriched

    def switch_game(self, payload):
        wanted = str(payload.get("game") or payload.get("game_key") or "").strip().lower()
        if not wanted:
            raise ValueError("缺少游戏标识。")
        if wanted not in games.GAMES:
            raise ValueError("不支持的游戏：" + wanted)
        key = games.normalize(wanted)
        if key == self.game_key:
            return self.bootstrap()
        if self.read_only:
            raise ValueError("当前是只读模式，不能切换游戏。")
        state = running_state(self.game_key, fresh=True)
        if state["game_running"] or state["launcher_running"]:
            raise ValueError("游戏或 Paradox 启动器正在运行，请先退出再切换。")
        games.write_active(self.root_data, key)
        self._rebind_game(key)
        data = self.bootstrap()
        data["switched"] = True
        data["switch_message"] = "已切换到 " + games.get(key)["label"]
        return data

    def _rebind_game(self, key):
        """Re-point every service at the other game without restarting the app."""
        key = games.normalize(key)
        info = paths.detect(None, None, game_key=key)
        # Switching in place must land on the exact same paths a restart with
        # --game-key <key> would use, otherwise notes/userdata/backups/exports
        # of both games share one directory and start overwriting each other.
        self.data_dir = games.game_data_dir(self.root_data, key)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.game_key = key
        db_override = None
        if self.sandbox:
            db_override = self._sandbox_db_for(key, info.get("db"))
        workshop = WorkshopService(self.data_dir)
        self.catalog = Catalog(docs=info["docs"], game=info.get("game") or None,
                               db_path=db_override, sandbox=self.sandbox,
                               workshop=workshop, game_key=key)
        try:
            self.catalog.set_cache_path(self.data_dir / "cache" / "mod-index.json")
        except Exception:
            pass
        self.workshop = workshop
        self.userdata = UserData(self.data_dir)
        self._load_path_settings()
        self.db = LauncherDB(self.catalog.vision()["db"])
        self._ai_service = None
        self._preview_cache_obj = None

    def _sandbox_db_for(self, key, db_path):
        # One sandbox copy per game (each game owns its own data_dir).  An
        # existing copy is reused as-is: re-copying the real DB on every switch
        # would silently wipe every playset/order saved in the sandbox.
        folder = self.data_dir / "sandbox"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        target = folder / "launcher-v2.sqlite"
        if target.is_file():
            return str(target)
        if not db_path or not Path(db_path).is_file():
            return None
        try:
            shutil.copy2(db_path, target)
        except OSError:
            return None
        return str(target)

    def _restart_app(self, key):
        env = os.environ.copy()
        env["SMM_GAME"] = key
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--game-key", key]
            cwd = Path(sys.executable).parent
        else:
            run_py = Path(__file__).resolve().parent.parent / "run.py"
            command = [sys.executable, str(run_py), "--window", "--game-key", key]
            cwd = run_py.parent
        flags = 0x00000008 | 0x00000200
        subprocess.Popen(command, cwd=str(cwd), env=env, close_fds=True, creationflags=flags)
        threading.Timer(1.0, lambda: os._exit(0)).start()

    # ------------------------------------------------------------------
    # 扫描结果持久化：上一次扫描（含深度分析）存盘，重启后直接复用，
    # 直到下一次扫描、或 MOD 集合变化（取消订阅 / 删除）才失效。
    # ------------------------------------------------------------------
    def _scan_cache_path(self):
        return self.data_dir / "scan-cache.json"

    def _scan_stamp(self, payload):
        # 指纹必须覆盖所有 MOD（播放集内 + 未入集的订阅/本地包），
        # 否则导入或删除一个未入集的包时缓存仍然有效，会拿旧结论糊弄用户。
        seen = {}
        for key in ("mods", "available", "enabled", "disabled"):
            for mod in payload.get(key) or []:
                if not isinstance(mod, dict):
                    continue
                mid = str(mod.get("id") or "")
                if not mid:
                    continue
                try:
                    seen[mid] = int(os.path.getmtime(mod.get("dir")))
                except (OSError, TypeError, ValueError):
                    seen.setdefault(mid, 0)
        ids = sorted(seen)
        return {"ids": ids, "stamps": seen, "count": len(ids)}

    def _save_scan_cache(self, payload, analysis):
        data = {
            "schema": SCAN_CACHE_SCHEMA,
            "saved_at": int(time.time() * 1000),
            "playset": payload.get("active_playset") or "",
            "stamp": self._scan_stamp(payload),
            "analysis": analysis,
        }
        try:
            path = self._scan_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            pass

    def _load_scan_cache(self, payload):
        path = self._scan_cache_path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # 旧版本写的缓存没有新字段（胜出者 / 持有者），一律作废，避免展示空明细。
        if int(data.get("schema") or 0) != SCAN_CACHE_SCHEMA:
            return None
        stamp = data.get("stamp") or {}
        current = self._scan_stamp(payload)
        if stamp.get("ids") != current["ids"]:
            return None
        # 目录 mtime 变了说明 MOD 内容更新过，旧结论不再可信。
        if stamp.get("stamps") != current["stamps"]:
            return None
        if (data.get("playset") or "") != (payload.get("active_playset") or ""):
            return None
        return data

    def _apply_scan_cache(self, payload):
        cached = self._load_scan_cache(payload)
        if not cached:
            payload["scan_cached"] = False
            payload["scan_saved_at"] = 0
            key = frozenset(str(mod.get("id")) for mod in payload.get("mods") or [])
            if self.catalog._analysis is not None and self.catalog._analysis_key != key:
                # MOD 被取消订阅 / 删除 / 新增后，旧分析结论不再可信。
                self.catalog._analysis = None
                self.catalog._analysis_key = key
                payload["analysis"] = None
            return payload
        analysis = cached.get("analysis")
        if analysis and not payload.get("analysis"):
            payload["analysis"] = analysis
            if self.catalog._analysis is None:
                self.catalog._analysis = analysis
        payload["scan_cached"] = True
        payload["scan_saved_at"] = int(cached.get("saved_at") or 0)
        return payload

    def bootstrap(self):
        payload = self._decorate(self.catalog.scan(force=False))
        payload = self._apply_scan_cache(payload)
        self._last_payload = payload
        return payload

    def refresh(self, payload=None):
        self.catalog.invalidate()
        self.db = LauncherDB(self.catalog.vision()["db"])
        data = self._decorate(self.catalog.refresh())
        self._last_payload = data
        return data

    def _backups(self):
        folder = self.data_dir / "backups"
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob("launcher-v2-*.sqlite"), key=safe_mtime, reverse=True)[:40]:
            try:
                size = path.stat().st_size
                mtime = path.stat().st_mtime
            except OSError:
                # Another request pruned the file between glob and stat.
                continue
            rows.append({
                "name": path.name,
                "size": size,
                "time": int(mtime * 1000),
                "game": self._backup_game(path.name),
            })
        return rows

    _BACKUP_NAME = re.compile(
        r"^launcher-v2-(?:(?P<tag>[a-z][a-z0-9_]*)-)?(?P<stamp>\d{8}-\d{6})(?:-\d+)?\.sqlite$")

    def _backup_tag(self):
        """Per-game marker embedded in the backup file name."""
        return str(getattr(self, "game_key", None) or games.DEFAULT_KEY)

    def _backup_game(self, name):
        """Game key a backup belongs to; None when it cannot be determined.

        Two sources: the tag embedded in the file name (round-9) and the
        `<backup>.sqlite.json` sidecar older builds wrote. The name wins.
        """
        path = Path(str(name))
        match = self._BACKUP_NAME.match(path.name)
        if match:
            tag = match.group("tag")
            if tag and tag in games.GAMES:
                return tag
        try:
            data = json.loads(self._backup_meta_path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("game"):
            return None
        return games.normalize(data.get("game")) or None

    def _backup_owner_name(self, key):
        return games.get(key).get("name", key) if key else "未知游戏"

    def _db_backup(self):
        """Snapshot the launcher DB, tagged with the game it belongs to."""
        folder = self.data_dir / "backups"
        try:
            return self.db.backup(folder, tag=self._backup_tag())
        except TypeError as exc:
            # Test doubles / older LauncherDB objects do not take the tag kwarg.
            if "tag" not in str(exc):
                raise
            return self.db.backup(folder)

    def _backup_meta_path(self, path):
        path = Path(path)
        return path.with_name(path.name + ".json")

    def _write_backup_meta(self, path):
        """Tag each launcher DB backup with the game it came from."""
        try:
            self._backup_meta_path(path).write_text(
                json.dumps({"game": self.game_key, "version": APP_VERSION}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass


    def _guard_write(self):
        if self.read_only:
            raise ValueError("当前是只读模式，未写入任何文件。")
        state = running_state(self.game_key, fresh=True)
        if state["launcher_running"]:
            raise ValueError("Paradox 启动器正在运行，请先关闭启动器再保存。")
        if state["game_running"]:
            raise ValueError("游戏正在运行，请先退出游戏再保存。")

    def _guard_user_write(self):
        """Notes/folders only touch userdata.json; still must honour read-only."""
        if self.read_only:
            raise ValueError("当前是只读模式，未写入任何文件。")

    def _write_and_refresh(self, action):
        # Serialise DB mutations: two browser tabs saving/rolling back at the same
        # time used to collide on the sqlite handle ("备份文件读取失败" / 内部错误).
        lock = getattr(self, "_db_lock", None)
        if lock is None:  # bare instances built via object.__new__ in tests
            lock = self._db_lock = threading.RLock()
        with lock:
            return self._write_and_refresh_locked(action)

    def _write_and_refresh_locked(self, action):
        self._guard_write()
        self.last_warning = ""
        backup = None
        try:
            backup = self._db_backup()
            self.last_backup = str(backup)
            self._write_backup_meta(backup)
        except (OSError, sqlite3.Error) as exc:
            # A failed safety copy must not throw away an otherwise valid save.
            self.last_warning = f"数据库备份失败，本次修改已直接写入：{exc}"
        try:
            result = action()
        except (sqlite3.Error, OSError) as exc:
            # A read-only folder, a locked launcher DB or a half-copied file used to
            # escape as a bare 500 "内部错误"; say what failed and that nothing saved.
            raise ValueError(
                "写入失败：数据目录不可写或启动器数据库被占用，本次修改没有保存。"
                "请检查文件夹权限，或关闭 Paradox 启动器/游戏后重试。") from exc
        payload = self._decorate(self.catalog.refresh())
        if backup is not None:
            payload["backup_created"] = backup.name
        if self.last_warning:
            payload["warning"] = self.last_warning
        payload["written"] = result
        return payload

    @staticmethod
    def _discard_partial(path):
        """Remove a half-written artifact so a failed merge never leaves a 0-byte zip."""
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _recycle(paths):
        """Send paths to the Windows recycle bin and report what really happened.

        Returns (moved, failed). The old code assumed success and told the user
        "已移到回收站" even when the file was locked and still there.
        """
        targets = []
        for item in paths:
            candidate = Path(item)
            if candidate.exists():
                targets.append(candidate)
        if not targets:
            # Nothing left to recycle: already-gone files are not "moved" and not
            # failures, otherwise the caller reports a bogus success/failure.
            return [], []
        quote = lambda value: str(value).replace("'", "''")
        commands = []
        for target in targets:
            method = "DeleteDirectory" if target.is_dir() else "DeleteFile"
            commands.append(f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}("
                            f"'{quote(target)}','OnlyErrorDialogs','SendToRecycleBin')")
        script = "Add-Type -AssemblyName Microsoft.VisualBasic; " + "; ".join(commands)
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False,
                           creationflags=0x08000000, timeout=180)
        except (OSError, subprocess.SubprocessError):
            pass
        moved, failed = [], []
        for target in targets:
            try:
                (failed if target.exists() else moved).append(target)
            except OSError:
                failed.append(target)
        return moved, failed

    def _recycle_or_raise(self, targets, what="项目"):
        moved, failed = self._recycle(targets)
        if failed:
            names = "、".join(item.name for item in failed[:5])
            more = f" 等 {len(failed)} 项" if len(failed) > 5 else ""
            raise ValueError(f"{names}{more}没能移到回收站（文件可能正被占用），"
                             f"已清理 {len(moved)} 项，请关闭占用程序后重试。")
        return moved

    def _merged_registry_paths(self):
        """Dirs/.mod files owned by merged packs.

        A merged pack has no launcher DB row until the user launches the launcher
        once, so anything built from DB rows alone treats its ``.mod`` as an
        orphan and lets 一键清理 delete it.
        """
        dirs, files = set(), set()
        for item in self._merged_rows():
            for key in ("path", "mod_file"):
                raw = str(item.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    target = Path(raw).resolve()
                except OSError:
                    continue
                if target.suffix.lower() == ".mod":
                    files.add(target.name.lower())
                dirs.add(str(target.parent).lower())
        return dirs, files

    def _referenced_paths(self):
        """Dirs/.mod files that the mod list still uses (cleanup must never touch them)."""
        dirs, files = set(), set()
        merged_dirs, merged_files = self._merged_registry_paths()
        files |= merged_files
        try:
            snapshot = self.catalog.scan()
        except Exception:  # noqa: BLE001 - a broken catalog must not bypass the guard
            return dirs | merged_dirs, files
        for row in snapshot.get("mods", []) + snapshot.get("available", []):
            directory = Path(row.get("dir") or "")
            if row.get("dir"):
                try:
                    dirs.add(str(directory.resolve()).lower())
                except OSError:
                    pass
                files.add(directory.name.lower() + ".mod")
            registry = str(row.get("gameRegistryId") or row.get("registry_id") or "")
            if registry:
                files.add(Path(registry).name.lower())
            # Packs listed from an extra scan root (sandbox mirror) carry their own
            # .mod path; that file is a real, user-created pack, never an orphan.
            mod_file = str(row.get("mod_file") or "")
            if mod_file:
                files.add(Path(mod_file).name.lower())
        return dirs, files

    def _sync_dlc_load_safe(self, action_label="设置"):
        """Derived file: a write failure must never fail the committed DB change."""
        try:
            self._sync_dlc_load()
        except Exception as exc:  # noqa: BLE001
            self.last_warning = f"{action_label}已保存，但 dlc_load.json 写入失败：{exc}"

    def _active_playset(self, playset_id=None):
        snapshot = self.catalog.scan()
        if playset_id:
            return playset_id
        if not snapshot.get("active_playset"):
            raise ValueError("没有找到播放集，请先创建一个。")
        return snapshot["active_playset"]

    def _ensure_playset(self, playset_id):
        playsets = self.db.snapshot().get("playsets", [])
        if not any(item["id"] == playset_id for item in playsets):
            raise ValueError("播放集不存在。")

    # ------------------------------------------------------------- actions
    def _dlc_load_path(self):
        if self.sandbox:
            return self.data_dir / "sandbox" / "dlc_load.json"
        return Path(self.catalog.vision()["docs"]) / "dlc_load.json"

    def _write_dlc_load(self, entries, snapshot=None):
        """Write the enabled mod list consumed when launching the game directly."""
        path = self._dlc_load_path()
        snapshot = snapshot or self.db.snapshot()
        registry = {}
        for row in snapshot["mods"]:
            directory = row.get("dirPath") or ""
            if not directory:
                continue
            try:
                if not Path(directory).exists():
                    continue
            except OSError:
                continue
            registry[row["id"]] = (row.get("gameRegistryId")
                                   or (f"mod/ugc_{row['steamId']}.mod" if row.get("steamId") else ""))
        enabled = []
        for entry in entries:
            if not entry.get("enabled"):
                continue
            registry_id = registry.get(entry["mod_id"])
            if registry_id:
                enabled.append(registry_id)
        existing = {"enabled_mods": [], "disabled_dlcs": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    existing.update(loaded)
            except (OSError, ValueError, TypeError):
                pass
        if existing.get("enabled_mods") == enabled and path.is_file():
            return enabled
        previous = path.read_bytes() if path.is_file() else None
        existing["enabled_mods"] = enabled
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)
        if not self.sandbox:
            try:
                self._backup_dlc_load(path, previous)
            except OSError:
                pass
        return enabled

    def _backup_dlc_load(self, path, previous=None):
        """Keep a bounded history of the pre-change dlc_load.json."""
        folder = self.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = folder / f"dlc_load-{stamp}.json"
        if previous is not None:
            backup.write_bytes(previous)
        elif path.is_file():
            shutil.copy2(path, backup)
        for old in sorted(folder.glob("dlc_load-*.json"),
                          key=safe_mtime, reverse=True)[20:]:
            try:
                old.unlink()
            except OSError:
                pass
        return backup

    def _sync_dlc_load(self):
        snapshot = self.db.snapshot()
        playsets = snapshot["playsets"]
        active = next((p for p in playsets if p.get("isActive")), playsets[0] if playsets else None)
        if not active:
            return []
        entries = sorted(
            (entry for entry in snapshot["entries"] if entry["playsetId"] == active["id"]),
            key=lambda entry: entry.get("position") or 0,
        )
        return self._write_dlc_load(
            [{"mod_id": entry["modId"], "enabled": entry["enabled"]} for entry in entries],
            snapshot,
        )

    def save_order(self, payload):
        playset_id = self._active_playset(payload.get("playset_id"))
        self._ensure_playset(playset_id)
        # A missing/None order used to fall back to "[]" and silently wipe the
        # whole playset; only an explicit list may clear it.
        raw = payload.get("order")
        if not isinstance(raw, list):
            raise ValueError("顺序数据格式不正确。")
        snapshot = self.catalog.scan()
        valid_ids = {mod["id"] for mod in snapshot["mods"]} | {mod["id"] for mod in snapshot["available"]}
        entries = [entry for entry in raw
                   if isinstance(entry, dict) and str(entry.get("mod_id") or "") in valid_ids]
        ignored = len(raw) - len(entries)
        # A payload whose entries are all invalid (wrong shape / unknown ids)
        # must not be treated as "clear the playset": the user still sees the
        # old list on screen, so wiping it loses data with no way back.
        if raw and not entries:
            raise ValueError("顺序数据里没有可用的 MOD 条目，已取消保存（原播放集未变动）。")

        # 半量提交保护：前端只发了一部分（例如某个视图只含已启用项）时，
        # 不能把没提到的 MOD 当成「用户要删掉」，否则播放集成员会静默丢失。
        # 缺的条目按「未启用」补回到末尾，保持成员集合不变。
        try:
            snap = self.db.snapshot()
            current_ids = [str(row.get("modId") or row.get("mod_id") or "")
                           for row in snap.get("entries", [])
                           if str(row.get("playsetId") or row.get("playset_id") or "") == str(playset_id)]
        except Exception:
            current_ids = []
        positions = payload.get("positions")
        positions = positions if isinstance(positions, dict) else None
        seen = {str(entry.get("mod_id")) for entry in entries}
        missing = [mid for mid in current_ids if mid and str(mid) not in seen]
        if missing:
            for mid in missing:
                entries.append({"mod_id": mid, "enabled": False})
            if positions is None:
                positions = {}

        def mutate():
            if positions:
                try:
                    written = self.db.save_order(playset_id, entries, positions=positions)
                except TypeError:  # 兼容不接受 positions 的旧实现 / 测试替身
                    written = self.db.save_order(playset_id, entries)
            else:
                written = self.db.save_order(playset_id, entries)
            skipped = ignored + int(getattr(self.db, "last_skipped", 0) or 0)
            if skipped:
                self.last_warning = f"顺序已保存，但忽略了 {skipped} 个无效条目（MOD 不存在或数据格式不对）。"
            self._sync_dlc_load_safe("顺序")
            return written

        return self._write_and_refresh(mutate)

    def set_active(self, payload):
        playset_id = str(payload.get("playset_id") or "")
        if not playset_id:
            raise ValueError("缺少播放集 ID。")
        self._ensure_playset(playset_id)

        def mutate():
            result = self.db.set_active(playset_id)
            self._sync_dlc_load_safe("切换播放集")
            return result

        return self._write_and_refresh(mutate)

    @staticmethod
    def _clean_playset_name(value):
        """Play set names end up in the launcher DB and in JSON round-trips, so
        strip control characters, collapse blanks and cap the length instead of
        storing whatever the client sent."""
        if value is not None and not isinstance(value, str):
            raise ValueError("播放集名称格式不正确（应为文本）。")
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:64]

    def create_playset(self, payload):
        name = self._clean_playset_name(payload.get("name"))
        if not name:
            raise ValueError("播放集名称不能为空。")
        raw = payload.get("mod_ids") or []
        if not isinstance(raw, list):
            raise ValueError("MOD 数据格式不正确。")
        valid_ids = self._known_mod_ids()
        wanted = []
        ignored = 0
        for item in raw:
            if isinstance(item, dict):
                mod_id = str(item.get("mod_id") or item.get("id") or "").strip()
                enabled = bool(item.get("enabled", True))
            else:
                mod_id, enabled = str(item or "").strip(), True
            if not mod_id:
                continue
            if mod_id in valid_ids:
                wanted.append({"mod_id": mod_id, "enabled": enabled})
            else:
                ignored += 1

        def mutate():
            new_id = self.db.create_playset(name, wanted)
            self.db.set_active(new_id)
            skipped = ignored + int(getattr(self.db, "last_skipped", 0) or 0)
            if skipped:
                self.last_warning = f"播放集已创建，但忽略了 {skipped} 个不存在的 MOD。"
            self._sync_dlc_load_safe("播放集")
            return new_id

        return self._write_and_refresh(mutate)

    def rename_playset(self, payload):
        playset_id = str(payload.get("playset_id") or "")
        if not playset_id:
            raise ValueError("缺少播放集 ID。")
        self._ensure_playset(playset_id)
        name = self._clean_playset_name(payload.get("name"))
        if not name:
            raise ValueError("播放集名称不能为空。")
        lock = getattr(self, "_db_lock", None)
        if lock is None:  # bare instances built via object.__new__ in tests
            lock = self._db_lock = threading.RLock()
        with lock:
            # qa4-c P3-11：Windows 用户看不出大小写差别，判重必须 casefold
            current = {p["id"]: p.get("name") or "" for p in self.catalog.scan().get("playsets", [])}
            folded = name.casefold()
            if any(pid != playset_id and (nm or "").strip().casefold() == folded
                   for pid, nm in current.items()):
                raise ValueError("已经有同名播放集了，请换一个名字。")

            def mutate():
                result = self.db.rename_playset(playset_id, name)
                self._sync_dlc_load_safe("重命名播放集")
                return {"renamed": result, "playset_id": playset_id}

            # 查重与写入放在同一把锁里，避免两个播放集同时改成同名（P3-12 TOCTOU）
            return self._write_and_refresh(mutate)

    def delete_playset(self, payload):
        playset_id = str(payload.get("playset_id") or "")
        if not playset_id:
            raise ValueError("缺少播放集 ID。")
        self._ensure_playset(playset_id)
        remaining = [p for p in self.catalog.scan().get("playsets", []) if p["id"] != playset_id]
        if not remaining:
            raise ValueError("至少保留一个播放集。")

        def mutate():
            result = self.db.delete_playset(playset_id)
            self._sync_dlc_load_safe("删除播放集")
            return result

        return self._write_and_refresh(mutate)

    def add_mods(self, payload):
        playset_id = self._active_playset(payload.get("playset_id"))
        self._ensure_playset(playset_id)
        raw = payload.get("mod_ids") or []
        if not isinstance(raw, list):
            raise ValueError("MOD 数据格式不正确。")
        wanted = [str(x) for x in raw]
        if not wanted:
            raise ValueError("请选择要加入的 MOD。")
        valid_ids = self._known_mod_ids()
        snapshot = self.catalog.scan()
        known = {mod["id"] for mod in snapshot["mods"]} | {mod["id"] for mod in snapshot["available"]}
        unknown = [mod_id for mod_id in wanted if mod_id not in known]
        if unknown and not any(mod_id in known for mod_id in wanted):
            raise ValueError("选中的 MOD 不存在或已卸载，请刷新列表后重试。")
        current = [{"mod_id": m["id"], "enabled": m["enabled"]} for m in snapshot["mods"]]
        existing = {entry["mod_id"] for entry in current}
        ignored = 0
        for mod_id in wanted:
            if mod_id not in valid_ids:
                ignored += 1
                continue
            if mod_id not in existing:
                existing.add(mod_id)
                current.append({"mod_id": mod_id, "enabled": True})

        def mutate():
            result = self.db.save_order(playset_id, current)
            if unknown:
                self.last_warning = f"有 {len(unknown)} 个 MOD 不存在或已卸载，已跳过。"
            self._sync_dlc_load_safe("MOD 列表")
            return result

        return self._write_and_refresh(mutate)

    def remove_mods(self, payload):
        playset_id = self._active_playset(payload.get("playset_id"))
        self._ensure_playset(playset_id)
        raw = payload.get("mod_ids") or []
        if not isinstance(raw, list):
            raise ValueError("MOD 数据格式不正确。")
        drop = {str(x) for x in raw}
        if not drop:
            raise ValueError("请选择要移除的 MOD。")
        snapshot = self.catalog.scan()
        present = {m["id"] for m in snapshot["mods"]}
        missing = len(drop - present)
        current = [{"mod_id": m["id"], "enabled": m["enabled"]} for m in snapshot["mods"]
                   if m["id"] not in drop]

        def mutate():
            result = self.db.save_order(playset_id, current)
            if missing:
                self.last_warning = f"已移除所选 MOD，但忽略了 {missing} 个不在当前播放集中的 MOD。"
            self._sync_dlc_load_safe("MOD 列表")
            return result

        return self._write_and_refresh(mutate)

    def _known_mod_ids(self):
        snapshot = self.catalog.scan()
        return {mod["id"] for mod in snapshot["mods"]} | {mod["id"] for mod in snapshot["available"]}

    def launch_game(self, payload):
        mode = (payload.get("mode") or "exe").lower()
        info = self.catalog.vision()
        if self.sandbox:
            # qa4-c P1-2：沙盒实例点「启动游戏」真的把游戏拉起来了。
            # 沙盒只用于验证界面与接口，绝不允许影响真实游戏/存档。
            self._record_launch(mode)
            return {"ok": True, "mode": "sandbox",
                    "message": "沙盒模式：已模拟启动，没有真正运行游戏。"}
        if mode == "steam":
            if not self.sandbox and not self.read_only:
                self._sync_dlc_load()
            self._record_launch("steam")
            os.startfile(f"steam://rungameid/{games.get(self.game_key)['appid']}")  # type: ignore[attr-defined]
            return {"ok": True, "mode": "steam",
                    "message": "已通过 Steam 启动：Steam 和官方启动器会读取管理器当前播放集的顺序。"}

        save_note = ""
        record_mode = "exe"
        if mode in ("continue", "save"):
            docs = info["docs"]
            if mode == "save":
                meta = save_service.read_meta(payload.get("save_path") or "", docs)
                save_note = f"存档「{meta['name']}」（{meta['date']}）"
                record_mode = "save"
            else:
                saves = save_service.list_saves(docs)
                if not saves:
                    raise ValueError("还没有存档，无法继续游戏。")
                save_note = f"最新存档「{saves[0]['name']}」"
                record_mode = "continue"

        exe = info.get("game_exe")
        if not exe or not Path(exe).is_file():
            raise ValueError(f"没有找到 {games.get(self.game_key)['exe']}，请检查游戏路径。")
        state = running_state(self.game_key)
        if state["game_running"]:
            return {"ok": True, "mode": "already-running", "message": "游戏已经在运行。"}
        if not state["steam_running"]:
            if save_note:
                raise ValueError("Steam 没有运行，无法启动游戏。请先启动 Steam。")
            self._record_launch("steam")
            os.startfile(f"steam://rungameid/{games.get(self.game_key)['appid']}")  # type: ignore[attr-defined]
            return {"ok": True, "mode": "steam-fallback",
                    "message": "Steam 未运行，已改用 Steam 启动，会经过 Paradox 启动器。"}
        if not self.sandbox and not self.read_only:
            self._sync_dlc_load()
        self._record_launch(record_mode)
        profile = games.get(self.game_key)
        env = os.environ.copy()
        # Steam sets these when it launches a game.  A direct exe launch from
        # the manager inherits a bare environment, which can make the Steam
        # API unable to validate owned DLC (the game then shows "not owned").
        env.setdefault("SteamAppId", profile["appid"])
        env.setdefault("SteamGameId", profile["appid"])
        env.setdefault("SteamOverlayGameId", profile["appid"])
        subprocess.Popen(
            [exe],
            cwd=str(Path(exe).parent),
            env=env,
            close_fds=True,
            creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if running_state(self.game_key, fresh=True)["game_running"]:
                suffix = f"；进入主菜单后选择{save_note}" if save_note else ""
                return {"ok": True, "mode": "exe",
                        "message": f"已启动群星（跳过启动器）{suffix}"}
            time.sleep(0.4)
        suffix = f"；请进入主菜单选择{save_note}" if save_note else ""
        return {"ok": True, "mode": "exe-pending",
                "message": f"已启动 {games.get(self.game_key)['exe']}，但暂时没有检测到进程。{suffix}"}

    def low_power(self, payload):
        enabled = bool(payload.get("enabled"))
        if enabled:
            try:
                import ctypes
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                ctypes.windll.psapi.EmptyWorkingSet(handle)
            except Exception:
                pass
        return {"ok": True, "enabled": enabled}

    def runtime(self, payload):
        state = running_state(self.game_key)
        state["ok"] = True
        state["steam_running"] = state.get("steam_running", False)
        return state

    def _analysis_history_path(self):
        return self.data_dir / "analysis-history.json"

    def analyze(self, payload):
        busy = getattr(self, "_analyze_busy", None)
        if busy is None:
            busy = threading.Lock()
            self._analyze_busy = busy
        if not busy.acquire(False):
            raise ValueError("正在分析中，请稍候再试。")
        try:
            result = self.catalog.analyse(force=False, deep=bool(payload.get("deep")))
            snapshot = self.catalog.scan()
            history = {
                "analysis": result.get("analysis"),
                "saved_at": int(time.time() * 1000),
                "playset": snapshot.get("active_playset") or "",
                "mod_ids": [mod["id"] for mod in snapshot.get("mods", [])],
                "mod_count": len(snapshot.get("mods", [])),
            }
            try:
                if self.read_only:
                    raise OSError("read-only")
                path = self._analysis_history_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, path)
            except OSError:
                pass
            result["saved_at"] = history["saved_at"]
            self._save_scan_cache(snapshot, result.get("analysis"))
            return result
        finally:
            busy.release()

    def analysis_history(self, payload):
        path = self._analysis_history_path()
        if not path.is_file():
            return {"ok": True, "analysis": None, "saved_at": 0, "stale": True}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("历史分析缓存已损坏，请重新分析。") from exc
        if not isinstance(data, dict):
            raise ValueError("历史分析缓存已损坏，请重新分析。")
        snapshot = self.catalog.scan()
        current_ids = {mod["id"] for mod in snapshot.get("mods", [])}
        saved_ids = set(data.get("mod_ids") or [])
        stale = bool(data.get("playset") != (snapshot.get("active_playset") or "")
                     or current_ids != saved_ids)
        return {"ok": True, "analysis": data.get("analysis"), "saved_at": data.get("saved_at") or 0,
                "stale": stale, "mod_count": data.get("mod_count") or len(saved_ids),
                "playset": data.get("playset") or ""}

    def workshop_refresh(self, payload):
        snapshot = self.catalog.scan()
        mod_id = str(payload.get("mod_id") or "")
        pool = snapshot["mods"] + snapshot["available"]
        if mod_id:
            mod = next((item for item in pool if item["id"] == mod_id), None)
            if not mod:
                raise ValueError("找不到该 MOD。")
            if not mod.get("steam_id"):
                raise ValueError("这个 MOD 不是 Steam 创意工坊项目。")
            ids = [mod["steam_id"]]
        elif payload.get("all"):
            pool = snapshot["mods"] + snapshot["available"]
            ids = sorted({mod["steam_id"] for mod in pool if mod.get("steam_id")})
            if not ids:
                raise ValueError("没有找到已安装的 Steam 创意工坊 MOD。")
        else:
            ids = [mod["steam_id"] for mod in snapshot["mods"] if mod.get("steam_id")]
            if not ids:
                raise ValueError("当前播放集里没有 Steam 创意工坊 MOD。")
        details = self.workshop.details(ids, force=True)
        if not details:
            raise ValueError("Steam 创意工坊接口暂时不可用，未更新任何 MOD，请稍后重试。")
        creators = self.workshop.creators(ids)
        authors = self.workshop.resolve_authors(creators, force=True)
        self.catalog.refresh()
        return {"ok": True, "updated": len(details), "authors": len(authors),
                "failed": max(0, len(ids) - len(details)), "mod_id": mod_id}

    def workshop_collection(self, payload):
        data = self.workshop.collection(payload.get("input") or "")
        snapshot = self.catalog.scan()
        installed = {}
        for mod in snapshot["mods"] + snapshot["available"]:
            steam_id = mod.get("steam_id")
            if steam_id:
                installed[str(steam_id)] = mod
        in_playset = {str(mod.get("steam_id")) for mod in snapshot["mods"]}
        matched, missing = [], []
        for steam_id in data["children"]:
            mod = installed.get(steam_id)
            detail = data["details"].get(steam_id) or {}
            if mod:
                matched.append({
                    "steam_id": steam_id,
                    "mod_id": mod["id"],
                    "name": mod["name"],
                    "in_playset": steam_id in in_playset,
                })
            else:
                missing.append({
                    "steam_id": steam_id,
                    "title": detail.get("title") or f"工坊项目 {steam_id}",
                })
        return {
            "ok": True,
            "collection": {"id": data["id"], "title": data["title"], "total": data["total"]},
            "matched": matched,
            "missing": missing,
        }

    def import_collection(self, payload):
        mod_ids = [str(item) for item in (payload.get("mod_ids") or [])]
        if not mod_ids:
            raise ValueError("合集里没有可导入的已安装 MOD。")
        name = (payload.get("title") or "工坊合集").strip()[:60]

        def mutate():
            new_id = self.db.create_playset(name, mod_ids)
            self.db.set_active(new_id)
            self._sync_dlc_load_safe("合集播放集")
            return new_id

        return self._write_and_refresh(mutate)

    def check_update(self, payload):
        return updater.check(self.data_dir, APP_VERSION)

    def download_update(self, payload):
        return updater.download(self.data_dir, str(payload.get("url") or ""),
                                str(payload.get("sha256") or ""), str(payload.get("version") or ""))

    def apply_update(self, payload):
        return updater.apply(self.data_dir, str(payload.get("path") or ""), str(payload.get("version") or ""))

    def crash_report(self, payload):
        """把 error.log 里的报错按 MOD 归类（确定性，不花 token）。"""
        info = self.catalog.vision()
        snapshot = self.catalog.scan()
        return crash.collect(info["docs"], snapshot["mods"])

    def crash_explain(self, payload):
        """让模型根据报错统计给出归因与处理顺序。"""
        info = self.catalog.vision()
        snapshot = self.catalog.scan()
        report = crash.collect(info["docs"], snapshot["mods"])
        result = self.ai().explain_crash(report)
        result["ok"] = True
        result["report"] = {
            "total_errors": report.get("total_errors"),
            "mapped": len(report.get("mapped") or []),
            "unmapped": len(report.get("unmapped") or []),
        }
        return result

    def diagnostics(self, payload):
        info = self.catalog.vision()
        docs = Path(info["docs"])
        snapshot = self.catalog.scan()
        files, errors = [], []
        pattern = re.compile(r"(error|exception|failed|crash|traceback|assert)", re.I)
        for folder, label in ((docs / "logs", "日志"), (docs / "crashes", "崩溃")):
            if not folder.is_dir():
                continue
            candidates = sorted(folder.glob("*"), key=lambda item: item.stat().st_mtime if item.is_file() else 0, reverse=True)
            for path in candidates[:12]:
                if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                    continue
                try:
                    lines = path.read_text("utf-8", errors="replace").splitlines()[-200:]
                except OSError:
                    continue
                files.append({"name": path.name, "folder": label, "size": path.stat().st_size,
                              "mtime": int(path.stat().st_mtime * 1000), "lines": len(lines)})
                for line in lines[-80:]:
                    if pattern.search(line):
                        errors.append({"file": path.name, "line": line.strip()[:300]})
        return {"ok": True, "docs": str(docs), "logs_dir": str(docs / "logs"),
                "crashes_dir": str(docs / "crashes"), "files": files, "errors": errors[:80],
                "enabled": sum(1 for mod in snapshot["mods"] if mod["enabled"]),
                "total": len(snapshot["mods"]), "game": info.get("raw_version") or ""}

    def save_backup(self, payload):
        docs = self.catalog.vision()["docs"]
        meta = save_service.read_meta(payload.get("path") or "", docs)
        root = self.data_dir / "backups" / "saves"
        root.mkdir(parents=True, exist_ok=True)
        source = Path(meta["path"])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = root / f"{source.stem}-{stamp}.sav"
        shutil.copy2(source, dest)
        return {"ok": True, "name": dest.name, "size": dest.stat().st_size, "path": str(dest)}

    def save_backups(self, payload):
        root = self.data_dir / "backups" / "saves"
        rows = []
        if root.is_dir():
            for path in sorted(root.glob("*.sav"), key=lambda item: item.stat().st_mtime, reverse=True)[:50]:
                rows.append({"name": path.name, "size": path.stat().st_size, "mtime": int(path.stat().st_mtime * 1000)})
        return {"ok": True, "backups": rows}

    def save_restore(self, payload):
        if getattr(self, "sandbox", False):
            try:
                docs_source = self.catalog.vision().get("docs_source")
            except Exception:  # noqa: BLE001 - path detection must not break the call
                docs_source = None
            if docs_source != "override":
                # Sandbox must never overwrite the real save folder.
                raise ValueError("沙箱模式下不会写入真实存档目录；请用 --docs 指定测试目录后再试。")
        docs = self.catalog.vision()["docs"]
        target = save_service.read_meta(payload.get("path") or "", docs)
        name = Path(str(payload.get("name") or "")).name
        root = (self.data_dir / "backups" / "saves").resolve()
        backup = (root / name).resolve()
        if root not in backup.parents or not backup.is_file() or backup.suffix.lower() != ".sav":
            raise ValueError("备份文件不存在。")
        safety = Path(target["path"])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(safety, root / f"{safety.stem}-before-restore-{stamp}.sav")
        shutil.copy2(backup, safety)
        return {"ok": True, "restored": name}

    def _merged_db_ids(self, target, mod_file=None):
        target = Path(target)
        names = {target.name.lower(), (target.stem + ".mod").lower()}
        raw_mod = str(mod_file or "").strip()
        if raw_mod:
            # Path("") is "." and its .name is "" -- adding it matched every row
            # with an empty gameRegistryId, wiping unrelated local mods.
            names.add(Path(raw_mod).name.lower())
        names.discard("")
        ids = []
        try:
            if not self.db.exists():
                return ids
            rows = self.db.snapshot().get("mods", [])
        except (OSError, sqlite3.Error):
            return ids
        for row in rows:
            registry = Path(str(row.get("gameRegistryId") or "")).name.lower()
            directory = str(row.get("dirPath") or "")
            if registry in names or (directory and Path(directory).resolve() == target.resolve()):
                ids.append(row["id"])
        return ids

    def _merged_registry_file(self):
        return self.data_dir / "merged-packs.json"

    def _merged_rows(self):
        """Read merged-packs.json defensively.

        A hand-edited or truncated registry can hold nulls, strings or a bare
        object instead of a list of pack dicts. Only dict rows with a path are
        usable; anything else must be dropped instead of crashing every caller
        with AttributeError.
        """
        registry = self._merged_registry_file()
        try:
            rows = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else []
        except (OSError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        return [item for item in rows
                if isinstance(item, dict) and str(item.get("path") or "").strip()]

    def list_merged_packs(self, payload):
        rows = self._merged_rows()
        missing = [item for item in rows if not Path(item.get("path", "")).is_file()]
        removed = 0
        stale_ids = []
        for item in missing:
            stale_ids.extend(self._merged_db_ids(item.get("path", ""), item.get("mod_file")))
        # Older merged packs may have been deleted before their registry entry
        # existed (or were deleted outside the manager). Clean their stale local
        # DB rows by the merge-name prefix, but only when the target is gone.
        if self.db.exists():
            for row in self.db.snapshot().get("mods", []):
                name = str(row.get("displayName") or "")
                directory = str(row.get("dirPath") or "")
                if name.startswith("HuifengMerged-") and directory and not Path(directory).exists():
                    stale_ids.append(row["id"])
        if stale_ids and self.db.exists():
            try:
                self._db_backup()
                removed = self.db.delete_mods(set(stale_ids))
                self.catalog.invalidate()
            except (OSError, sqlite3.Error):
                removed = 0
        if missing:
            rows = [item for item in rows if Path(item.get("path", "")).is_file()]
            try:
                self._merged_registry_file().write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        return {"ok": True, "packs": sorted(rows, key=lambda item: item.get("created_at", 0), reverse=True),
                "removed": removed}

    def delete_merged_pack(self, payload):
        target = Path(str(payload.get("path") or ""))
        mod_dir = self.write_mod_root()
        try:
            inside = target.resolve().parent == mod_dir.resolve()
        except OSError:
            inside = False
        if not target.is_file() or not inside:
            raise ValueError("只能删除群星 MOD 目录里的合并包。")
        mod_file = Path(str(payload.get("mod_file") or "").strip()) if str(payload.get("mod_file") or "").strip() else None
        if mod_file is None:
            # A bare delete (API client, stale page) used to leave the pack's .mod
            # behind: the zip vanished but the registry file stayed and came back
            # as a cleanup orphan.
            for row in self._merged_rows():
                if Path(str(row.get("path") or "")) == target and row.get("mod_file"):
                    mod_file = Path(str(row["mod_file"]))
                    break
        if mod_file is None:
            neighbour = target.with_suffix(".mod")
            try:
                text = neighbour.read_text(encoding="utf-8", errors="replace") if neighbour.is_file() else ""
            except OSError:
                text = ""
            if neighbour.is_file() and target.name.lower() in text.lower():
                mod_file = neighbour
        ids = self._merged_db_ids(target, mod_file)
        paths = [target]
        try:
            same_dir = bool(mod_file) and mod_file.is_file() and mod_file.resolve().parent == target.resolve().parent
        except OSError:
            same_dir = False
        if same_dir:
            paths.append(mod_file)
        # Delete first, and only drop the registry rows / DB rows when the files
        # really left the folder. Otherwise the user is left with an orphan zip.
        self._recycle_or_raise(paths, "合并包")
        registry = self._merged_registry_file()
        try:
            rows = [item for item in self._merged_rows() if Path(item.get("path", "")) != target]
            registry.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        removed = 0
        if ids and self.db.exists():
            try:
                self._db_backup()
                removed = self.db.delete_mods(set(ids))
                self.catalog.invalidate()
            except (OSError, sqlite3.Error):
                removed = 0
        return {"ok": True, "removed": removed,
                "message": f"合并包已移到回收站，并从 MOD 列表移除 {removed} 条记录。"}

    def manager_files_scan(self, payload):
        data = self.data_dir
        items = []
        now = time.time()

        def add(path, feature, purpose, checked=True, extra=None, auto=None, kind=None):
            try:
                target = Path(path)
                if not target.exists():
                    return
                stat = target.stat()
                size = stat.st_size if target.is_file() else sum(
                    item.stat().st_size for item in target.rglob("*") if item.is_file()
                )
                age_days = max(0, int((now - stat.st_mtime) / 86400))
                items.append({
                    "path": str(target), "feature": feature, "purpose": purpose,
                    "size": size, "checked": bool(checked),
                    "auto": bool(checked if auto is None else auto),
                    "kind": kind or feature,
                    "mtime": int(stat.st_mtime * 1000),
                    "age_days": age_days,
                    "extra": extra or [],
                })
            except OSError:
                pass

        for path in sorted((data / "backups").glob("*.sqlite"), reverse=True)[:80]:
            add(path, "自动保存备份", "launcher-v2 数据库快照，可用于恢复", False,
                auto=False, kind="历史备份")
        # Keep the five newest database backups; older history is safe to clean.
        backups = sorted([item for item in items if item["kind"] == "历史备份"],
                         key=lambda item: item["mtime"], reverse=True)
        for index, item in enumerate(backups):
            if index >= 5:
                item["auto"] = True
                item["checked"] = True
        for path in sorted((data / "backups" / "saves").glob("*.sav"), reverse=True)[:80]:
            add(path, "存档备份", "手动备份的群星存档，可用于恢复", False,
                auto=False, kind="存档备份")
        for path in sorted((data / "backups" / "settings").glob("*"), reverse=True)[:80]:
            add(path, "游戏设置备份", "修改游戏设置前的 settings 文件备份", False,
                auto=False, kind="设置备份")
        for path in sorted((data / "backups").glob("dlc_load-*.json"), reverse=True)[:80]:
            add(path, "DLC 加载备份", "调整 MOD 顺序前的 dlc_load.json 快照，自动保留最近 20 份", False,
                auto=False, kind="DLC 加载备份")
        for path in sorted((data / "exports").glob("*"), reverse=True)[:40]:
            add(path, "播放集导出", "导出的播放集文件，删除不影响游戏", False,
                auto=False, kind="导出文件")
        for path in sorted((data / "backups" / "mod-snapshots").glob("*"), reverse=True)[:40]:
            if path.is_dir():
                add(path, "更新前快照", "完整 MOD 文件快照，可用于回滚", False,
                    auto=False, kind="MOD 快照")
        for path in sorted((data / "updates").glob("*"), reverse=True)[:40]:
            add(path, "在线更新", "已下载的更新安装包或替换脚本；删除不影响当前版本",
                auto=True, kind="更新残留")
        for path in sorted((data / "workshop").glob("*"), reverse=True)[:40]:
            add(path, "工坊上传辅助", "上传 Steam 工坊前生成的临时配置/副本",
                auto=True, kind="临时文件")
        for path in sorted((data / "cache").glob("*"), reverse=True)[:20]:
            add(path, "MOD 索引缓存", "加速管理器扫描，可随时重建",
                auto=True, kind="可重建缓存")
        add(data / "analysis-history.json", "深度分析", "上次冲突分析结果，可重新分析生成",
            auto=True, kind="历史记录")
        add(data / "history.json", "启动记录", "管理器启动游戏的历史记录",
            auto=True, kind="历史记录")
        add(data / "merged-packs.json", "合并包管理", "合并包登记表；删除后合并包仍存在但不再列出",
            False, auto=False, kind="合并包")
        add(data / "userdata.json", "备注 / 文件夹", "保存 MOD 备注和文件夹归类，删除会丢失这些信息",
            False, auto=False, kind="用户数据")
        for path in sorted(data.glob("*.log"), reverse=True)[:20]:
            add(path, "运行日志", "管理器运行日志，用于排查问题",
                auto=True, kind="历史日志")
        add(data / "paths.json", "路径设置", "手动添加的游戏/MOD 路径，删除后需要重新设置",
            False, auto=False, kind="用户数据")
        add(data / "window-profile", "窗口配置", "WebView 浏览器配置/缓存，删除会重置窗口状态",
            auto=True, kind="窗口缓存")
        registry = data / "merged-packs.json"
        if registry.is_file():
            try:
                rows = json.loads(registry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows = []
            for item in rows[:80]:
                add(item.get("path"), "合并MOD", "合并生成的 MOD 压缩包；删除前确认不再使用",
                    False, auto=False, kind="合并包")
                add(item.get("mod_file"), "合并MOD", "合并包的 .mod 描述文件",
                    False, auto=False, kind="合并包")
        auto_count = sum(1 for item in items if item["auto"])
        return {"ok": True, "items": items,
                "total": sum(item["size"] for item in items),
                "auto_count": auto_count,
                "data_dir": str(data)}

    def manager_files_delete(self, payload):
        paths = [str(item) for item in (payload.get("paths") or []) if str(item)]
        mode = "recycle" if payload.get("mode") != "permanent" else "permanent"
        if not paths:
            raise ValueError("没有选择要清理的文件。")
        data = self.data_dir.resolve()
        mod_dir = Path(self.catalog.vision()["mod_dir"]).resolve()
        try:
            mod_roots = {self.write_mod_root().resolve()}
        except OSError:
            mod_roots = {mod_dir}
        # 正在使用的启动器数据库 / API 令牌 / 沙盒副本不能被"清理文件"顺手删掉，
        # 否则管理器当场失去自己的数据。
        protected = set()
        for name in ("launcher-v2.sqlite", "api-token", "scan-cache.json", "ai-settings.json",
                     "ai-cache.json", "userdata.json", "analysis-history.json",
                     # qa4-c P2-3：删掉 instance.lock 就等于解除单实例保护
                     "instance.lock"):
            protected.add((self.data_dir / name).resolve())
        try:
            protected.add(self._scan_cache_path().resolve())
        except OSError:
            pass
        # The live database may be a branch file (launcher-v2-openbeta.sqlite),
        # so protect whatever this session actually opened, not just the
        # canonical name.
        try:
            active_db = str(self.catalog.vision().get("db") or "")
            if active_db:
                protected.add(Path(active_db).resolve())
        except OSError:
            pass
        sandbox_dir = (self.data_dir / "sandbox").resolve()
        targets = []
        for raw in paths:
            target = Path(raw).resolve()
            if target in protected or sandbox_dir == target or sandbox_dir in target.parents:
                raise ValueError("这个文件正被管理器使用（数据库 / 令牌 / 沙盒副本），不能清理。")
            if target == data or target in mod_roots:
                raise ValueError("不能清理整个数据目录或 MOD 目录。")
            if data not in target.parents and not any(root in target.parents for root in mod_roots):
                raise ValueError("只能清理管理器数据目录或群星 MOD 目录里的文件。")
            if target.exists():
                targets.append(target)
        if not targets:
            return {"ok": True, "deleted": 0, "message": "没有可清理的文件。"}
        if mode == "recycle":
            moved = self._recycle_or_raise(targets)
            return {"ok": True, "deleted": len(moved), "mode": mode,
                    "message": f"已清理 {len(moved)} 项（回收站）。"}
        failed = []
        for target in targets:
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError:
                failed.append(target)
        done = [target for target in targets if target not in failed]
        if failed:
            names = "、".join(item.name for item in failed[:5])
            raise ValueError(f"{names} 删除失败（文件可能正被占用），已清理 {len(done)} 项。")
        return {"ok": True, "deleted": len(done), "mode": mode,
                "message": f"已清理 {len(done)} 项（永久删除）。"}

    def cleanup_scan(self, payload):
        info = self.catalog.vision()
        mod_dir = Path(info["mod_dir"])
        if not mod_dir.is_dir():
            raise ValueError("MOD 目录不存在。")
        # In sandbox mode merge/import write into <data-dir>\sandbox\mod, so the
        # scan has to cover that folder too or those packs become invisible here.
        roots = [mod_dir]
        write_root = self.write_mod_root()
        try:
            if write_root.resolve() != mod_dir.resolve():
                roots.append(write_root)
        except OSError:
            pass
        snapshot = self.catalog.scan()
        rows = snapshot["mods"] + snapshot["available"]
        # Merged packs count as referenced even though they have no launcher DB row
        # yet, otherwise 一键清理 offers to delete their registration file.
        referenced_dirs, referenced_files = self._referenced_paths()
        for row in rows:
            directory = Path(row.get("dir") or "")
            if row.get("dir"):
                referenced_dirs.add(str(directory.resolve()).lower())
                referenced_files.add(directory.name.lower() + ".mod")
            registry = str(row.get("gameRegistryId") or row.get("registry_id") or "")
            if registry:
                referenced_files.add(Path(registry).name.lower())
        issues = []

        def add(path, kind, reason, name="", checked=False):
            try:
                target = Path(path).resolve()
                if not target.exists():
                    return
                relative = None
                for root in roots:
                    try:
                        relative = target.relative_to(root.resolve())
                        break
                    except ValueError:
                        continue
                if relative is None:
                    return
                size = target.stat().st_size if target.is_file() else sum(
                    item.stat().st_size for item in target.rglob("*") if item.is_file()
                )
                side = "real"
                try:
                    if write_root.resolve() != mod_dir.resolve() and write_root.resolve() in target.parents:
                        side = "sandbox"
                except OSError:
                    pass
                issues.append({"path": str(target), "kind": kind, "reason": reason,
                               "name": name or target.name, "size": size, "checked": checked,
                               "relative": str(relative), "side": side})
            except (OSError, ValueError):
                pass

        groups = {}
        for row in rows:
            key = str(row.get("steam_id") or "").strip() or str(row.get("name") or "").lower()
            if key:
                groups.setdefault(key, []).append(row)
        seen_paths = set()
        for key, group in groups.items():
            if len(group) < 2:
                continue
            for row in group:
                path = row.get("dir") or ""
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    add(path, "duplicate", f"同一 MOD 有 {len(group)} 条记录（重复）", row.get("name"))
        for row in rows:
            directory = Path(row.get("dir") or "")
            if row.get("dir") and not directory.is_dir():
                continue
            if directory.is_dir() and not (directory / "descriptor.mod").is_file():
                add(directory, "invalid", "缺少 descriptor.mod", row.get("name"))
        for root in roots:
            for mod_file in root.glob("*.mod"):
                if mod_file.name.lower() not in referenced_files:
                    add(mod_file, "orphan", "没有任何播放集/记录引用这个 .mod", mod_file.stem)
        for root in roots:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if str(child.resolve()).lower() in referenced_dirs:
                    continue
                if not any(child.iterdir()):
                    add(child, "orphan", "空目录，未被任何 MOD 记录引用", child.name)
                elif not (child / "descriptor.mod").is_file():
                    add(child, "orphan", "没有 descriptor.mod 且未被记录引用", child.name)
        return {"ok": True, "issues": issues, "mod_dir": str(mod_dir),
                "write_mod_dir": str(write_root)}

    def cleanup_delete(self, payload):
        info = self.catalog.vision()
        mod_dir = Path(info["mod_dir"]).resolve()
        # Only the write root may be deleted from: in sandbox mode that is the
        # mirror under the data dir, so a stale page can never remove a file from
        # the real game library.
        try:
            roots = {self.write_mod_root().resolve()}
        except OSError:
            roots = {mod_dir}
        raw = [str(item) for item in (payload.get("paths") or []) if str(item)]
        if not raw:
            raise ValueError("没有选择要清理的项目。")
        # The scan list may be minutes old: re-check what the mod list still uses
        # so a stale page can never delete a mod that is in use right now.
        used_dirs, used_files = self._referenced_paths()
        targets = []
        in_use = []
        for item in raw:
            target = Path(item).resolve()
            if target in roots or not any(root in target.parents for root in roots):
                raise ValueError("只能清理群星 MOD 目录内的项目。")
            if not target.exists():
                continue
            try:
                key = str(target).lower()
            except OSError:
                key = str(target)
            if key in used_dirs or (target.is_file() and target.name.lower() in used_files):
                in_use.append(target.name)
                continue
            targets.append(target)
        if in_use:
            raise ValueError("、".join(in_use[:5]) + " 仍被 MOD 列表使用，已跳过。请先在列表里停用或删除对应 MOD。")
        if not targets:
            return {"ok": True, "deleted": 0, "message": "没有需要清理的项目。"}
        moved = self._recycle_or_raise(targets)
        return {"ok": True, "deleted": len(moved), "message": f"已把 {len(moved)} 项移到回收站。"}

    def _snapshot_root(self):
        root = self.data_dir / "backups" / "mod-snapshots"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _snapshot_path(self, snapshot_id):
        root = self._snapshot_root().resolve()
        target = (root / str(snapshot_id)).resolve()
        if root not in target.parents or not target.is_dir():
            raise ValueError("快照不存在。")
        return target

    def snapshot_mods(self, payload):
        ids = {str(item) for item in (payload.get("mod_ids") or []) if str(item)}
        if not ids:
            raise ValueError("请选择要快照的 MOD。")
        if running_state(self.game_key, fresh=True)["game_running"]:
            raise ValueError("游戏正在运行，请先退出再创建快照。")
        snapshot = self.catalog.scan()
        mods = [mod for mod in snapshot["mods"] + snapshot["available"] if mod["id"] in ids]
        if not mods:
            raise ValueError("选中的 MOD 不存在。")
        total = sum(int(mod.get("size_bytes") or 0) for mod in mods)
        free = shutil.disk_usage(self.data_dir).free
        if total and total > free * 0.95:
            raise ValueError("磁盘剩余空间不足，无法创建快照。")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = self._snapshot_root() / f"{stamp}-{uuid.uuid4().hex[:6]}"
        folder.mkdir(parents=True, exist_ok=False)
        entries = []
        copied = 0
        for mod in mods:
            source = Path(mod.get("dir") or "")
            if not source.is_dir():
                continue
            dest = folder / "mods" / mod["id"]
            shutil.copytree(source, dest, dirs_exist_ok=True)
            copied += 1
            entries.append({"id": mod["id"], "name": mod.get("name") or mod["id"],
                            "steam_id": mod.get("steam_id") or "", "source": str(source),
                            "backup": str(dest), "size": int(mod.get("size_bytes") or 0)})
        if not entries:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("没有可复制的 MOD 文件。")
        manifest = {"id": folder.name, "created_at": int(time.time() * 1000),
                    "entries": entries, "size": sum(item["size"] for item in entries)}
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "snapshot": {k: manifest[k] for k in ("id", "created_at", "size")},
                "mods": copied, "path": str(folder),
                "message": f"快照完成：{copied} 个 MOD，约 {manifest['size'] / 1024 / 1024:.1f} MB。"}

    def list_mod_snapshots(self, payload):
        rows = []
        free = shutil.disk_usage(self.data_dir).free
        for folder in sorted(self._snapshot_root().iterdir(), reverse=True):
            manifest = folder / "manifest.json"
            if not folder.is_dir() or not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"id": data.get("id") or folder.name,
                         "created_at": data.get("created_at") or 0,
                         "size": data.get("size") or 0,
                         "mods": [{"id": item.get("id"), "name": item.get("name"),
                                   "steam_id": item.get("steam_id"), "size": item.get("size") or 0}
                                  for item in data.get("entries") or []]})
        return {"ok": True, "snapshots": rows, "free": free}

    @staticmethod
    def _purge_extra(source, target):
        """Drop files/dirs in target that the snapshot does not contain."""
        keep_files = set()
        keep_dirs = set()
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if path.is_dir():
                keep_dirs.add(relative.as_posix().lower())
            else:
                keep_files.add(relative.as_posix().lower())
        removed = 0
        for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            relative = path.relative_to(target).as_posix().lower()
            if path.is_dir():
                if relative not in keep_dirs:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            elif relative not in keep_files:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def restore_mod_snapshot(self, payload):
        if running_state(self.game_key, fresh=True)["game_running"]:
            raise ValueError("游戏正在运行，请先退出再回滚。")
        folder = self._snapshot_path(payload.get("id"))
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        wanted = {str(item) for item in (payload.get("mod_ids") or []) if str(item)}
        entries = [item for item in manifest.get("entries") or [] if not wanted or item["id"] in wanted]
        if not entries:
            raise ValueError("没有选择要回滚的 MOD。")
        restored = 0
        purged = 0
        for item in entries:
            source = Path(item.get("backup") or "")
            target = Path(item.get("source") or "")
            if not source.is_dir() or not target:
                continue
            if target.is_dir():
                # A rollback must reproduce the snapshot exactly, so files added or
                # left over after the snapshot have to go as well.
                purged += self._purge_extra(source, target)
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            restored += 1
        detail = f"，同时清理了 {purged} 个快照外多出来的文件" if purged else ""
        return {"ok": True, "restored": restored, "purged": purged,
                "message": f"已回滚 {restored} 个 MOD{detail}。Steam 下次检查更新时可能再次覆盖，请按需暂停 Steam 自动更新或使用离线模式。"}

    def delete_mod_snapshot(self, payload):
        folder = self._snapshot_path(payload.get("id"))
        self._recycle_or_raise([folder], "快照")
        return {"ok": True, "message": "快照已移到回收站。"}

    def safe_mode_apply(self, payload):
        ids = [str(item) for item in (payload.get("mod_ids") or []) if str(item)]
        if not ids:
            raise ValueError("没有可启用的 MOD。")
        name = str(payload.get("name") or "安全模式排查")
        def mutate():
            new_id = self.db.create_playset(name, ids)
            self.db.set_active(new_id)
            if getattr(self.db, "last_skipped", 0):
                self.last_warning = (f"已跳过 {self.db.last_skipped} 个不存在或未安装的 MOD。")
            self._sync_dlc_load_safe("安全模式播放集")
            return new_id
        return self._write_and_refresh(mutate)

    def share_playset(self, payload):
        snapshot = self.catalog.scan()
        enabled = [mod for mod in snapshot["mods"] if mod["enabled"]]
        if not enabled:
            raise ValueError("当前播放集没有启用的 MOD。")
        playset = next((item for item in snapshot["playsets"] if item["id"] == snapshot["active_playset"]), None)
        payload_data = {
            "v": 1,
            "name": (playset or {}).get("name") or "联机播放集",
            "game": self.game_key,
            "game_version": (snapshot.get("game") or {}).get("raw_version") or "",
            "mods": [
                {"n": mod["name"], "w": mod.get("steam_id") or "", "l": not bool(mod.get("steam_id"))}
                for mod in enabled
            ],
        }
        raw = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        code = "HFMM1-" + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")
        return {"ok": True, "code": code, "count": len(enabled), "name": payload_data["name"]}

    def import_share_code(self, payload):
        code = str(payload.get("code") or "").strip()
        if not code.startswith("HFMM1-"):
            raise ValueError("分享码格式不正确，应以 HFMM1- 开头。")
        try:
            raw = zlib.decompress(base64.urlsafe_b64decode(code[6:]))
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("分享码损坏或不是本管理器的分享码。") from exc
        if not isinstance(data, dict) or not isinstance(data.get("mods"), list):
            raise ValueError("分享码内容不完整。")
        if data.get("game") and data["game"] != self.game_key:
            raise ValueError("这是 " + games.get(data["game"])["label"] + " 的分享码，当前管理器是 " + games.get(self.game_key)["label"] + "。")
        snapshot = self.catalog.scan()
        installed = snapshot["mods"] + snapshot["available"]
        by_steam = {str(mod.get("steam_id")): mod for mod in installed if mod.get("steam_id")}
        by_name = {str(mod.get("name") or "").lower(): mod for mod in installed if mod.get("name")}
        matched, missing = [], []
        for entry in data["mods"]:
            steam_id = str(entry.get("w") or "")
            name = str(entry.get("n") or "")
            mod = by_steam.get(steam_id) if steam_id else by_name.get(name.lower())
            if mod:
                matched.append(mod["id"])
            else:
                missing.append({"name": name, "steam_id": steam_id,
                                "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam_id}" if steam_id else ""})
        if not matched:
            raise ValueError("分享码里的 MOD 一个都没有安装，无法创建播放集。")
        name = str(data.get("name") or "联机播放集") + "（联机导入）"
        def mutate():
            new_id = self.db.create_playset(name, matched)
            self.db.set_active(new_id)
            self._sync_dlc_load_safe("联机播放集")
            return new_id
        result = self._write_and_refresh(mutate)
        result["share_import"] = {
            "name": name,
            "matched": len(matched),
            "missing": missing,
            "total": len(data["mods"]),
        }
        return result

    def _dlc_root(self):
        game = Path(self.catalog.vision().get("game") or "")
        # ``set_game_path`` normalises an executable to its folder, but a CLI
        # ``--game ...\stellaris.exe`` reached us verbatim and turned the DLC
        # root into ``<exe>\dlc``, which silently reported "no DLC installed".
        if game.suffix.lower() == ".exe" or game.is_file():
            game = game.parent
        return game / "dlc"

    @staticmethod
    def _dlc_name(folder):
        for file in sorted(folder.rglob("*.dlc")):
            try:
                text = file.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            match = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M)
            if match:
                return match.group(1).replace("_", " ")
        clean = re.sub(r"^dlc\d+_", "", folder.name, flags=re.I)
        return clean.replace("_", " ").strip() or folder.name

    def dlc_status(self, payload=None):
        root = self._dlc_root()
        load_path = self._dlc_load_path()
        disabled = set()
        if load_path.is_file():
            try:
                data = json.loads(load_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    disabled = {Path(str(item)).stem.lower() for item in (data.get("disabled_dlcs") or [])}
            except (OSError, ValueError, TypeError):
                pass
        items = []
        if root.is_dir():
            for folder in sorted(root.iterdir(), key=lambda item: item.name.lower()):
                if not folder.is_dir() or not folder.name.lower().startswith("dlc"):
                    continue
                item_id = folder.name
                items.append({"id": item_id, "name": self._dlc_name(folder),
                              "enabled": item_id not in disabled,
                              "installed": True})
        enabled = sum(1 for item in items if item["enabled"])
        return {"ok": True, "game": self.game_key, "items": items,
                "total": len(items), "enabled": enabled, "disabled": len(items) - enabled,
                "dlc_load_path": str(load_path),
                "load_exists": load_path.is_file()}

    def enable_all_dlc(self, payload=None):
        if running_state(self.game_key, fresh=True)["game_running"]:
            raise ValueError("游戏正在运行，请先退出再修改 DLC 状态。")
        path = self._dlc_load_path()
        data = {"enabled_mods": [], "disabled_dlcs": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                data.update(loaded)
        data["disabled_dlcs"] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.is_file() else None
        self._backup_dlc_load(path, previous)
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        self._replace_temp(temp, path, "DLC 配置文件")
        result = self.dlc_status({})
        result["message"] = f"已启用全部 {result['total']} 个已安装 DLC。"
        return result

    def _settings_paths(self):
        docs = Path(self.catalog.vision()["docs"])
        if not self.sandbox:
            return docs / "settings.txt", docs / "pdx_settings.txt"
        # qa4-c P1-1：沙盒必须隔离 —— 真实 settings.txt 一行都不能改。
        # 在 data_dir 下维护一份影子副本：首次（或源文件更新时）复制，之后读写的都是副本。
        shadow = Path(self.data_dir) / "sandbox-docs"
        out = []
        for name in ("settings.txt", "pdx_settings.txt"):
            src = docs / name
            dst = shadow / name
            if src.is_file():
                try:
                    if (not dst.is_file()
                            or src.stat().st_mtime_ns > dst.stat().st_mtime_ns):
                        shadow.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                except OSError:
                    pass
            out.append(dst)
        return out[0], out[1]

    @staticmethod
    def _replace_temp(temp, path, label="文件"):
        """os.replace, but a directory / locked target becomes a readable error."""
        try:
            os.replace(temp, path)
        except OSError as exc:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError(
                f"{label}无法写入，目标被占用或本身是目录：{Path(path).name}。" ) from exc

    @staticmethod
    def _atomic_write_text(path, text, bom=False):
        # surrogateescape round-trips non-UTF-8 bytes instead of corrupting them.
        data = text.encode("utf-8", "surrogateescape")
        if bom:
            data = b"\xef\xbb\xbf" + data
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
        Api._replace_temp(temp, path, "设置文件")

    @staticmethod
    def _read_setting(text, pattern, default=""):
        match = re.search(pattern, text, re.M | re.S)
        return match.group(1) if match else default

    def get_game_settings(self, payload):
        profile = games.get(self.game_key)
        settings, pdx = self._settings_paths()
        if not settings.is_file():
            raise ValueError(f"找不到{profile['label']} settings.txt，请先启动一次游戏。")
        text = settings.read_text(encoding="utf-8-sig", errors="replace")
        pdx_text = pdx.read_text(encoding="utf-8-sig", errors="replace") if pdx.is_file() else ""
        mode = "fullscreen" if self._read_setting(text, r"^\s*fullScreen=(yes|no)", "yes") == "yes" else (
            "borderless" if self._read_setting(text, r"^\s*borderless=(yes|no)", "no") == "yes" else "windowed")
        size = re.search(r"size\s*=\s*\{[^}]*?x=(\d+)[^}]*?y=(\d+)", text, re.S)
        language = self._read_setting(text, r'^language="([^"]*)"', "")
        if not language:
            language = self._read_setting(pdx_text, r'"language"\s*=\s*\{[^}]*?value="([^"]*)"', "l_english")
        values = {
            "language": language or "l_english",
            "display_mode": mode,
            "resolution": f"{size.group(1)}x{size.group(2)}" if size else "1920x1080",
            "vsync": self._read_setting(text, r"^\s*vsync=(yes|no)", "yes") == "yes",
            "gui_scale": float(self._read_setting(text, r"^\s*gui_scale=([\d.]+)", "1.0") or 1.0),
            "master_volume": int(float(self._read_setting(text, r"^master_volume=([\d.]+)", "50") or 50)),
            "music_volume": int(float(self._read_setting(text, r"^music_volume=([\d.]+)", "50") or 50)),
            "sound_fx_volume": int(float(self._read_setting(text, r"^sound_fx_volume=([\d.]+)", "50") or 50)),
            "gfx_quality": int(self._read_setting(text, r"^gfx_quality=(\d+)", "2") or 2) if profile.get("settings_quality", True) else 2,
            "quality_supported": bool(profile.get("settings_quality", True)),
            "game_key": self.game_key,
            "game_label": profile["label"],
            "settings_path": str(settings),
            "pdx_settings_path": str(pdx) if pdx.is_file() else "",
        }
        return {"ok": True, "settings": values}

    def save_game_settings(self, payload):
        if running_state(self.game_key, fresh=True)["game_running"]:
            raise ValueError("游戏正在运行，请先退出游戏再修改设置。")
        profile = games.get(self.game_key)
        settings, pdx = self._settings_paths()
        if not settings.is_file():
            raise ValueError(f"找不到{profile['label']} settings.txt，请先启动一次游戏。")
        current = self.get_game_settings({})["settings"]
        data = {**current}
        for key in ("language", "display_mode", "resolution", "vsync", "gui_scale",
                    "master_volume", "music_volume", "sound_fx_volume", "gfx_quality"):
            if key in payload:
                data[key] = payload[key]
        if data["language"] not in ("l_english", "l_simp_chinese", "l_korean", "l_japanese", "l_russian"):
            raise ValueError("不支持的游戏语言。")
        if not re.match(r"^\d{3,5}x\d{3,5}$", str(data["resolution"])):
            raise ValueError("分辨率格式应为 宽x高，例如 1920x1080。")
        data["gui_scale"] = max(0.5, min(2.0, float(data["gui_scale"])))
        for key in ("master_volume", "music_volume", "sound_fx_volume"):
            data[key] = max(0, min(100, int(float(data[key]))))
        if profile.get("settings_quality", True):
            data["gfx_quality"] = max(0, min(3, int(data["gfx_quality"])))
        data["display_mode"] = data["display_mode"] if data["display_mode"] in ("fullscreen", "borderless", "windowed") else "fullscreen"

        raw_settings = settings.read_bytes()
        settings_bom = raw_settings.startswith(b"\xef\xbb\xbf")
        text = raw_settings.decode("utf-8-sig", errors="surrogateescape")
        text = re.sub(r'^language="[^"]*"', f'language="{data["language"]}"', text, count=1, flags=re.M)
        for key in ("master_volume", "music_volume", "sound_fx_volume"):
            text = re.sub(rf"^{key}=[\d.]+", f"{key}={float(data[key]):.6f}", text, count=1, flags=re.M)
        text = re.sub(r"^gfx_quality=\d+", f"gfx_quality={data['gfx_quality']}", text, count=1, flags=re.M)
        width, height = data["resolution"].split("x")
        text = re.sub(r"size\s*=\s*\{[^}]*?x=\d+[^}]*?y=\d+[^}]*?\}",
                      lambda m: f"size=\n\t{{\n\t\tx={width}\n\t\ty={height}\n\t}}", text, count=1, flags=re.S)
        text = re.sub(r"^\s*gui_scale=[\d.]+", f"\tgui_scale={float(data['gui_scale']):.6f}", text, count=1, flags=re.M)
        text = re.sub(r"^\s*vsync=(yes|no)", "\tvsync=" + ("yes" if data["vsync"] else "no"), text, count=1, flags=re.M)
        if data["display_mode"] == "fullscreen":
            text = re.sub(r"^\s*fullScreen=(yes|no)", "\tfullScreen=yes", text, count=1, flags=re.M)
            text = re.sub(r"^\s*borderless=(yes|no)", "\tborderless=no", text, count=1, flags=re.M)
        elif data["display_mode"] == "borderless":
            text = re.sub(r"^\s*fullScreen=(yes|no)", "\tfullScreen=no", text, count=1, flags=re.M)
            text = re.sub(r"^\s*borderless=(yes|no)", "\tborderless=yes", text, count=1, flags=re.M)
        else:
            text = re.sub(r"^\s*fullScreen=(yes|no)", "\tfullScreen=no", text, count=1, flags=re.M)
            text = re.sub(r"^\s*borderless=(yes|no)", "\tborderless=no", text, count=1, flags=re.M)

        pdx_text = ""
        pdx_bom = False
        if pdx.is_file():
            raw_pdx = pdx.read_bytes()
            pdx_bom = raw_pdx.startswith(b"\xef\xbb\xbf")
            pdx_text = raw_pdx.decode("utf-8-sig", errors="surrogateescape")
        if pdx_text:
            pdx_text = re.sub(r'("language"\s*=\s*\{[^}]*?value=")[^"]*(")', rf'\g<1>{data["language"]}\g<2>', pdx_text, count=1, flags=re.S)
            pdx_mode = data["display_mode"]
            if self.game_key == "hoi4":
                pdx_mode = "borderless_fullscreen" if pdx_mode == "borderless" else pdx_mode
            pdx_text = re.sub(r'("display_mode"\s*=\s*\{[^}]*?value=")[^"]*(")', rf'\g<1>{pdx_mode}\g<2>', pdx_text, count=1, flags=re.S)
            pdx_text = re.sub(r'("fullscreen_resolution"\s*=\s*\{[^}]*?value=")[^"]*(")', rf'\g<1>{data["resolution"]}\g<2>', pdx_text, count=1, flags=re.S)
            pdx_text = re.sub(r'("vsync"\s*=\s*\{[^}]*?enabled=)(yes|no)', rf'\g<1>{"yes" if data["vsync"] else "no"}', pdx_text, count=1, flags=re.S)

        root = self.data_dir / "backups" / "settings"
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # Back up the ORIGINAL bytes so the save can actually be rolled back.
        if settings.is_file():
            shutil.copy2(settings, root / f"settings-{stamp}.txt")
        if pdx.is_file():
            shutil.copy2(pdx, root / f"pdx_settings-{stamp}.txt")
        for old in sorted(root.glob("settings-*.txt"),
                          key=safe_mtime, reverse=True)[40:]:
            try:
                old.unlink()
            except OSError:
                pass
        self._atomic_write_text(settings, text, settings_bom)
        if pdx_text:
            self._atomic_write_text(pdx, pdx_text, pdx_bom)
        return {"ok": True, "settings": data, "message": f"游戏设置已保存，重启{profile['label']}后生效。"}

    def _local_mod(self, mod_id):
        snapshot = self.catalog.scan()
        mod = next((item for item in (snapshot["mods"] + snapshot["available"]) if item["id"] == mod_id), None)
        if not mod:
            raise ValueError("找不到这个 MOD。")
        if mod.get("steam_id"):
            raise ValueError("这是 Steam 创意工坊 MOD，不能从管理器再次上传。")
        directory = Path(mod.get("dir") or "")
        if not directory.is_dir():
            raise ValueError("找不到本地 MOD 目录。")
        return mod, directory

    def _ensure_steamcmd(self, requested=""):
        candidates = []
        if requested:
            candidates.append(Path(requested))
        candidates += [
            self.data_dir / "steamcmd" / "steamcmd.exe",
            Path.home() / "Documents" / "StellarisModManager" / "workshop-upload" / "steamcmd" / "steamcmd.exe",
            Path(r"C:\steamcmd\steamcmd.exe"),
            Path(r"C:\Program Files (x86)\Steam\steamcmd.exe"),
            Path(r"C:\Program Files\Steam\steamcmd.exe"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                if candidate.name.lower() != "steamcmd.exe":
                    continue
                return candidate
        target = self.data_dir / "steamcmd"
        target.mkdir(parents=True, exist_ok=True)
        archive = target / "steamcmd.zip"
        try:
            with urllib.request.urlopen("https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip", timeout=120) as response, open(archive, "wb") as handle:
                shutil.copyfileobj(response, handle)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(target)
        except Exception as exc:
            raise ValueError("自动下载 SteamCMD 失败，请检查网络，或在“SteamCMD 路径”里手动指定 steamcmd.exe。") from exc
        exe = target / "steamcmd.exe"
        if not exe.is_file():
            raise ValueError("SteamCMD 解压后没有找到 steamcmd.exe。")
        return exe

    def workshop_prepare(self, payload):
        mod, directory = self._local_mod(str(payload.get("mod_id") or ""))
        mod_id = str(payload.get("mod_id") or "")
        published = str(payload.get("publishedfileid") or "").strip()
        if not published:
            state = self.data_dir / "workshop" / f"{mod_id}.json"
            try:
                published = str(json.loads(state.read_text(encoding="utf-8")).get("publishedfileid") or "")
            except (OSError, ValueError):
                published = ""
        if not published:
            published = str(mod.get("remote_file_id") or "").strip()
        published = published if published.isdigit() else "0"

        title = str(payload.get("title") or mod.get("name") or "Untitled MOD").strip()
        description = str(payload.get("description") or "").strip()
        changenote = str(payload.get("changenote") or "").strip() or "Update from Huifeng Mod Manager"
        tags = [item.strip() for item in str(payload.get("tags") or "").split(",") if item.strip()]

        preview_raw = str(payload.get("preview") or mod.get("thumbnail") or "").strip()
        preview = Path(preview_raw) if preview_raw else None
        if not preview or not preview.is_file():
            for name in ("thumbnail.png", "thumbnail.jpg", "thumbnail.webp"):
                candidate = Path(directory) / name
                if candidate.is_file():
                    preview = candidate
                    break
        if not preview or not preview.is_file():
            raise ValueError("找不到预览图。请在 MOD 目录放一张 thumbnail.png（512×512 以上），或在“预览图路径”里指定图片。")

        folder = self.data_dir / "workshop"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = published if published != "0" else mod_id.replace("-", "")[:12]
        safe = f"workshop_{stamp}"

        def esc(value):
            text = str(value).replace("\\", "\\\\").replace('"', "'")
            return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")

        tag_block = "\n".join(f'        "{index}" "{esc(tag)}"' for index, tag in enumerate(tags))
        if tag_block:
            tag_block = '    "tags"\n    {\n' + tag_block + '\n    }\n'
        vdf = folder / f"{safe}.vdf"
        vdf.write_text(
            '"workshopitem"\n{\n'
            f'    "appid" "{games.get(self.game_key)["appid"]}"\n'
            f'    "publishedfileid" "{published}"\n'
            f'    "contentfolder" "{directory}"\n'
            f'    "previewfile" "{preview}"\n'
            '    "visibility" "0"\n'
            f'    "title" "{esc(title)}"\n'
            f'    "description" "{esc(description)}"\n'
            f'    "changenote" "{esc(changenote)}"\n'
            + tag_block +
            '}\n',
            encoding="utf-8",
        )
        action = "更新已有条目" if published != "0" else "新建条目"
        return {"ok": True, "vdf": str(vdf), "contentfolder": str(directory),
                "preview": str(preview), "publishedfileid": published,
                "message": f"已生成上传配置（{action}，内容目录 {directory}）。接下来输入 Steam 密码即可上传。"}

    def workshop_read_result(self, payload):
        vdf = Path(str(payload.get("vdf") or ""))
        if not vdf.is_file():
            raise ValueError("上传配置文件不存在。")
        text = vdf.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'"publishedfileid"\s+"(\d+)"', text)
        published = match.group(1) if match else "0"
        if published == "0":
            raise ValueError("还没有读到工坊 ID。请等 SteamCMD 上传完成后重试；若仍失败，请把 SteamCMD 输出里的 PublishedFileId 填到“工坊条目 ID”。")
        mod_id = str(payload.get("mod_id") or "")
        if mod_id:
            state = self.data_dir / "workshop" / f"{mod_id}.json"
            state.write_text(json.dumps({"publishedfileid": published, "vdf": str(vdf)}, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "publishedfileid": published, "message": f"已保存工坊 ID：{published}"}

    def workshop_launch(self, payload):
        folder = (self.data_dir / "workshop").resolve()
        vdf = Path(str(payload.get("vdf") or "")).resolve()
        if folder not in vdf.parents or vdf.suffix.lower() != ".vdf" or not vdf.is_file():
            raise ValueError("上传配置不存在，请先生成。")
        requested = str(payload.get("steamcmd") or "").strip().strip('"')
        steamcmd = self._ensure_steamcmd(requested)
        username = str(payload.get("steam_username") or "").strip()
        if username and not re.match(r"^[A-Za-z0-9_.@+\-]{1,64}$", username):
            raise ValueError("Steam 用户名格式不正确。")

        batch = self.data_dir / "workshop" / f"upload_{time.time_ns()}_{uuid.uuid4().hex[:6]}.cmd"
        batch.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "title SteamCMD Workshop Upload\r\n"
            f'cd /d "{steamcmd.parent}"\r\n'
            "echo ============================================\r\n"
        )
        footer = (
            "echo.\r\n"
            "echo Done. Go back to the manager and click Read upload result.\r\n"
            "pause\r\n"
        )
        if username:
            body = header + (
                f"echo Logging in as: {username}\r\n"
                "echo Password and Steam Guard are hidden (no asterisks). Type and press Enter.\r\n"
                "echo Paste with right-click or Ctrl+Shift+V. Ctrl+C aborts, not copies.\r\n"
                "echo ============================================\r\n"
                f'"{steamcmd}" +login "{username}" +workshop_build_item "{vdf}" +quit\r\n'
            ) + footer
            message = "已打开 SteamCMD。输入密码和 Steam Guard 后会自动上传，完成后回管理器点“读取上传结果”。"
        else:
            body = header + (
                "echo Enter your Steam account name (the login name, not nickname)\r\n"
                "echo ============================================\r\n"
                "set /p STEAMUSER=Steam username: \r\n"
                "echo.\r\n"
                "echo Logging in. Password and Steam Guard are hidden - type and press Enter.\r\n"
                f'"{steamcmd}" +login %STEAMUSER% +workshop_build_item "{vdf}" +quit\r\n'
            ) + footer
            message = "已打开 SteamCMD：先输入 Steam 用户名，再输入密码，会自动完成上传。"

        batch.write_text(body, encoding="utf-8", newline="")
        subprocess.Popen(["cmd.exe", "/k", "call", str(batch)], creationflags=0x00000010)
        return {"ok": True, "message": message, "steamcmd": str(steamcmd), "batch": str(batch)}

    def workshop_upload_status(self, payload):
        requested = str(payload.get("steamcmd") or "").strip().strip('"')
        try:
            steamcmd = self._ensure_steamcmd(requested)
        except ValueError as exc:
            return {"ok": False, "stage": "error", "error": "找不到 SteamCMD", "hint": str(exc), "percent": None}
        appid = str(games.get(self.game_key)["appid"])
        candidates = [
            steamcmd.parent / "workshopbuilds" / f"depot_build_{appid}.log",
            steamcmd.parent / "logs" / "content_log.txt",
        ]
        chunks = []
        for path in candidates:
            if path.is_file():
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace")[-6000:])
                except OSError:
                    pass
        text = "\n".join(chunks)
        state = classify_steamcmd_output(text)
        state["source"] = str(candidates[0]) if candidates[0].is_file() else ""
        return state

    def merge_enabled_mods(self, payload):
        # Two clicks arriving together used to pick the same free pack name and one
        # merge silently overwrote the other; serialise the whole pick+write step.
        lock = getattr(self, "_merge_lock", None)
        if lock is None:  # bare instances built via object.__new__ in tests
            lock = self._merge_lock = threading.Lock()
        with lock:
            return self._merge_enabled_mods(payload)

    def _merge_enabled_mods(self, payload):
        snapshot = self.catalog.scan(force=True)
        enabled = [mod for mod in snapshot["mods"] if mod.get("enabled")]
        if not enabled:
            raise ValueError("当前播放集没有启用的 MOD。")
        info = self.catalog.vision()
        game_version = info.get("raw_version") or info.get("version") or ""
        match = re.search(r"(\d+)\.(\d+)", str(game_version))
        supported = f"v{match.group(1)}.{match.group(2)}.*" if match else "v4.5.*"
        requested = str(payload.get("name") or "").strip()
        name = requested or f"HuifengMerged-{time.strftime('%Y%m%d-%H%M')}"
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:60] or "HuifengMerged"
        mod_root = self.write_mod_root()
        try:
            mod_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # A read-only MOD folder, or a file sitting where the folder should
            # be (a sandbox mirror replaced by hand), used to escape as a raw
            # FileExistsError and reach the user as "内部错误".
            raise ValueError(f"无法创建 MOD 目录 {mod_root}（路径只读或不可用）：{exc}") from exc
        output = mod_root / f"{safe}.zip"
        counter = 2
        while output.exists() or (mod_root / f"{output.stem}.mod").exists():
            if counter > 1000:
                raise ValueError("同名合并包过多，请先清理 MOD 目录。")
            output = mod_root / f"{safe}-{counter}.zip"
            counter += 1
        # The .mod name must follow the de-duplicated zip name: two packs sharing
        # one .mod means the second merge overwrites the first registration and
        # deleting either pack breaks the other.
        pack_name = output.stem
        entries = {}
        overwritten = 0
        skipped = []
        for mod in enabled:
            directory = Path(mod["dir"]) if mod.get("dir") else None
            if not directory or not directory.is_dir():
                skipped.append(mod.get("name") or mod.get("id") or "未知 MOD")
                continue
            for root, _dirs, files in os.walk(directory):
                for filename in files:
                    source = Path(root) / filename
                    rel = source.relative_to(directory).as_posix()
                    if rel.lower() == "descriptor.mod":
                        continue
                    if rel in entries:
                        overwritten += 1
                    entries[rel] = source
        if not entries:
            raise ValueError("没有找到可合并的 MOD 文件。")
        descriptor = (f'name="{pack_name}"\nversion="1.0"\nsupported_version="{supported}"\n'
                      f'tags={{ "Merged" }}\n')
        external = (f'name="{pack_name}"\npath="mod/{output.name}"\n'
                    f'supported_version="{supported}"\ntags={{ "Merged" }}\n')
        compression = zipfile.ZIP_STORED if payload.get("store") else zipfile.ZIP_DEFLATED
        compresslevel = None if payload.get("store") else 1
        try:
            with zipfile.ZipFile(output, "w", compression, compresslevel=compresslevel) as archive:
                archive.writestr("descriptor.mod", descriptor)
                for rel in sorted(entries):
                    archive.write(entries[rel], rel)
            mod_file = mod_root / f"{pack_name}.mod"
            mod_file.write_text(external, encoding="utf-8")
        except OSError as exc:
            # A read-only or locked MOD folder must not surface as 内部错误.
            self._discard_partial(output)
            raise ValueError(f"无法写入 MOD 目录 {mod_root}（目录只读或被占用）：{exc}") from exc
        record = {
            "name": pack_name,
            "path": str(output),
            "mod_file": str(mod_file),
            "mods": len(enabled) - len(skipped),
            "files": len(entries),
            "size": output.stat().st_size,
            "created_at": int(time.time() * 1000),
        }
        warning = ""
        try:
            registry = self._merged_registry_file()
            rows = [item for item in self._merged_rows() if Path(item.get("path", "")).exists()]
            rows.append(record)
            registry.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            # The pack itself is fine; losing the registry row only hides it from
            # 合并包 page, so report that instead of pretending everything worked.
            warning = (f"合并包已生成（{output.name}），但合并包登记表写入失败，"
                       f"刷新后可能看不到它：{exc}")
            self.last_warning = warning
        result = {
            "ok": True,
            "name": pack_name,
            "path": str(output),
            "mod_file": str(mod_file),
            "mods": record["mods"],
            "files": len(entries),
            "overwritten": overwritten,
            "skipped": skipped,
            "size": record["size"],
            "compressed": not payload.get("store"),
            "message": "合并完成。请启动一次 Paradox 启动器注册新 MOD，然后回到管理器刷新；测试前请保留原播放集备份。",
        }
        if warning:
            result["warning"] = warning
        return result

    def import_local_mod(self, payload):
        # Two imports of the same pack name did the free-name scan at the same time,
        # so both picked "RacePack" and the loser died on mkdir with a 400.
        lock = getattr(self, "_import_lock", None)
        if lock is None:  # bare instances built via object.__new__ in tests
            lock = self._import_lock = threading.Lock()
        with lock:
            return self._import_local_mod(payload)

    def _import_local_mod(self, payload):
        info = self.catalog.vision()
        raw = str(payload.get("data") or "")
        if not raw.strip():
            raise ValueError("缺少 ZIP 文件数据，请重新选择要导入的压缩包。")
        try:
            blob = base64.b64decode(raw.split(",", 1)[-1], validate=True)
        except Exception as exc:
            raise ValueError("ZIP 数据无效。") from exc
        if len(blob) > 30 * 1024 * 1024:
            raise ValueError("ZIP 不能超过 30MB。")
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as exc:
            raise ValueError("不是有效的 ZIP 文件。") from exc
        names = [name for name in archive.namelist() if name and not name.endswith("/")]
        descriptors = [name for name in names if name.lower().endswith("descriptor.mod")]
        if not descriptors:
            raise ValueError("ZIP 里没有 descriptor.mod。")
        descriptor = min(descriptors, key=lambda name: name.count("/"))
        root_prefix = descriptor[:-len("descriptor.mod")]
        text = archive.read(descriptor).decode("utf-8-sig", "replace")
        match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
        mod_name = (match.group(1) if match else Path(str(payload.get("name") or "local_mod")).stem).strip()
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", mod_name).strip(" .")[:60] or "local_mod"
        base = self.write_mod_root()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"无法创建 MOD 目录 {base}（路径只读或不可用）：{exc}") from exc
        dest = base / safe
        counter = 2
        while dest.exists():
            dest = base / f"{safe}-{counter}"
            counter += 1
        included = [name for name in names if not root_prefix or name.startswith(root_prefix)]
        if sum(archive.getinfo(name).file_size for name in included) > 100 * 1024 * 1024:
            raise ValueError("解压内容超过 100MB，已取消导入。")
        try:
            dest.mkdir(parents=True)
            for name in names:
                if root_prefix and not name.startswith(root_prefix):
                    continue
                rel = name[len(root_prefix):] if root_prefix else name
                if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                    continue
                out = dest / rel
                if not str(out.resolve()).startswith(str(dest.resolve())):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(archive.read(name))
            mod_file = base / f"{dest.name}.mod"
            version = str(info.get("raw_version") or info.get("version") or "")
            match = re.search(r"(\d+)\.(\d+)", version)
            supported = f"v{match.group(1)}.{match.group(2)}.*" if match else "v4.5.*"
            mod_file.write_text(f'name="{mod_name}"\npath="mod/{dest.name}"\nsupported_version="{supported}"\n',
                                encoding="utf-8")
        except OSError as exc:
            # Read-only or locked MOD folder: drop the half-unpacked folder and say
            # something usable instead of letting a raw OSError become 内部错误.
            try:
                shutil.rmtree(dest, ignore_errors=True)
            except OSError:
                pass
            raise ValueError(f"无法写入 MOD 目录 {base}（目录只读或被占用）：{exc}") from exc
        # The file is on disk but the launcher has no row for it yet: without a
        # cache drop the 20 s scan cache keeps the pack invisible right after the
        # import, which looks like a failed import.
        self.catalog.invalidate()
        return {"ok": True, "name": mod_name, "path": str(dest), "mod_file": str(mod_file),
                "write_mod_dir": str(base),
                "message": "已导入到 MOD 目录。请启动一次 Paradox 启动器完成注册，然后回到管理器刷新。"}

    def list_saves(self, payload):
        docs = self.catalog.vision()["docs"]
        return {"ok": True, "saves": save_service.list_saves(docs)}

    def save_detail(self, payload):
        docs = self.catalog.vision()["docs"]
        meta = save_service.read_meta(payload.get("path") or "", docs)
        snapshot = self.catalog.scan()
        current = {mod["name"].lower() for mod in snapshot["mods"]}
        enabled = {mod["name"].lower() for mod in snapshot["mods"] if mod["enabled"]}
        save_names = {name.lower() for name in meta["mods"]}
        meta["missing"] = [name for name in meta["mods"] if name.lower() not in current]
        meta["not_enabled"] = [name for name in meta["mods"]
                               if name.lower() in current and name.lower() not in enabled]
        meta["extra"] = [mod["name"] for mod in snapshot["mods"]
                         if mod["enabled"] and mod["name"].lower() not in save_names]
        meta["save_mod_count"] = len(meta["mods"])
        return {"ok": True, "save": meta}

    def import_playset(self, payload):
        data = payload.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as exc:
                raise ValueError("导入文件不是有效的 JSON。") from exc
        if not isinstance(data, dict):
            raise ValueError("导入文件格式不正确。")
        entries = data.get("mods") or []
        if not isinstance(entries, list):
            raise ValueError("导入文件里没有 MOD 列表。")
        snapshot = self.catalog.scan()
        # 已安装（mods 表）优先，虚拟的 available 行只在没冲突时补位：
        # 否则同名 MOD 会被 available 的 real:* id 顶掉，导入时静默丢条目。
        by_id, by_steam, by_name = {}, {}, {}
        for mod in snapshot["mods"]:
            by_id[mod["id"]] = mod["id"]
            if mod.get("steam_id"):
                by_steam.setdefault(str(mod["steam_id"]), mod["id"])
            by_name.setdefault(mod["name"].lower(), mod["id"])
            if mod.get("display_name"):
                by_name.setdefault(mod["display_name"].lower(), mod["id"])
        for mod in snapshot["available"]:
            by_id.setdefault(mod["id"], mod["id"])
            if mod.get("steam_id"):
                by_steam.setdefault(str(mod["steam_id"]), mod["id"])
            by_name.setdefault(mod["name"].lower(), mod["id"])
            if mod.get("display_name"):
                by_name.setdefault(mod["display_name"].lower(), mod["id"])
        ids, missing, seen, flags = [], [], set(), {}
        dropped, duplicates = [], 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_id = str(entry.get("id") or "").strip()
            steam_id = str(entry.get("steamId") or entry.get("remote_file_id") or "")
            name = (entry.get("displayName") or entry.get("name") or "").strip()
            mod_id = by_id.get(raw_id) if raw_id else None
            if not mod_id and steam_id:
                mod_id = by_steam.get(steam_id)
            if not mod_id and name:
                mod_id = by_name.get(name.lower())
            if mod_id and str(mod_id).startswith("real:"):
                # 解析到了虚拟条目（未安装的 real:* 占位），按缺失处理，不要静默丢
                mod_id = None
            if mod_id:
                if mod_id in seen:
                    duplicates += 1
                    continue
                seen.add(mod_id)
                ids.append(mod_id)
                flags[mod_id] = bool(entry.get("enabled", True))
            else:
                label = name or steam_id or raw_id
                missing.append(label)
                dropped.append(label)
        if not ids:
            raise ValueError("没有匹配到已安装的 MOD，未创建播放集。")
        title = (data.get("name") or "导入的播放集").strip()[:60] or "导入的播放集"

        def mutate():
            new_id = self.db.create_playset(title, ids)
            if any(not flag for flag in flags.values()):
                # create_playset enables everything; an export that carried
                # `enabled: false` must survive the round trip.
                self.db.save_order(new_id, [
                    {"mod_id": mod_id, "enabled": flags.get(mod_id, True)} for mod_id in ids
                ])
            self.db.set_active(new_id)
            self._sync_dlc_load_safe("导入的播放集")
            return new_id

        result = self._write_and_refresh(mutate)
        result["import_summary"] = {
            "matched": len(ids),
            "missing": missing[:50],
            "missing_count": len(missing),
            "duplicates": duplicates,
            "dropped_count": len(dropped),
            "name": title,
        }
        return result

    def apply_save(self, payload):
        docs = self.catalog.vision()["docs"]
        meta = save_service.read_meta(payload.get("path") or "", docs)
        snapshot = self.catalog.scan()
        by_name = {}
        for mod in snapshot["mods"] + snapshot["available"]:
            by_name[mod["name"].lower()] = mod["id"]
            if mod.get("display_name"):
                by_name.setdefault(mod["display_name"].lower(), mod["id"])
        ids, missing, seen = [], [], set()
        for name in meta["mods"]:
            mod_id = by_name.get(name.lower())
            if mod_id:
                if mod_id not in seen:
                    seen.add(mod_id)
                    ids.append(mod_id)
            else:
                missing.append(name)
        if not ids:
            raise ValueError("这个存档用到的 MOD 都没有安装，无法重建。")
        title = (payload.get("name") or f"存档 {meta['name']}")[:60]
        existing = next((item for item in snapshot.get("playsets", []) if item.get("name") == title), None)
        def mutate():
            if existing:
                playset_id = existing["id"]
                self.db.save_order(playset_id, [{"mod_id": mod_id, "enabled": True} for mod_id in ids])
            else:
                playset_id = self.db.create_playset(title, ids)
            self.db.set_active(playset_id)
            self._sync_dlc_load_safe("存档播放集")
            return playset_id
        result = self._write_and_refresh(mutate)
        result["apply_summary"] = {
            "matched": len(ids),
            "missing": missing[:50],
            "name": title,
            "reused": bool(existing),
            "playset_id": result.get("written"),
        }
        return result

    def save_note(self, payload):
        self._guard_user_write()
        note = self.userdata.note(payload.get("mod_id") or "", payload.get("note") or "")
        return {"ok": True, "note": note, "user": self.userdata.snapshot()}

    def folder_create(self, payload):
        self._guard_user_write()
        folder = self.userdata.folder_create(payload.get("name") or "")
        return {"ok": True, "folder": folder, "user": self.userdata.snapshot()}

    def folder_rename(self, payload):
        self._guard_user_write()
        folder = self.userdata.folder_rename(payload.get("folder_id") or "", payload.get("name") or "")
        return {"ok": True, "folder": folder, "user": self.userdata.snapshot()}

    def folder_delete(self, payload):
        self._guard_user_write()
        self.userdata.folder_delete(payload.get("folder_id") or "")
        return {"ok": True, "user": self.userdata.snapshot()}

    def folder_reorder(self, payload):
        folders = self.userdata.folder_reorder(payload.get("ids") or [])
        return {"ok": True, "folders": folders, "user": self.userdata.snapshot()}

    def folder_assign(self, payload):
        self._guard_user_write()
        folder_id = self.userdata.folder_assign(payload.get("mod_id") or "", payload.get("folder_id") or "")
        return {"ok": True, "folder_id": folder_id, "user": self.userdata.snapshot()}

    def steam_action(self, payload):
        mod_id = str(payload.get("mod_id") or "")
        action = str(payload.get("action") or "")
        snapshot = self.catalog.scan()
        mod = next((item for item in snapshot["mods"] + snapshot["available"] if item["id"] == mod_id), None)
        if not mod:
            raise ValueError("找不到该 MOD。")
        steam_id = str(mod.get("steam_id") or "")
        if not steam_id:
            raise ValueError("这个 MOD 不是 Steam 创意工坊项目。")
        page = f"https://steamcommunity.com/sharedfiles/filedetails/?id={steam_id}"
        client = f"steam://url/CommunityFilePage/{steam_id}"
        if action == "web":
            os.startfile(page)  # type: ignore[attr-defined]
            return {"ok": True, "message": "已在浏览器打开创意工坊页面。"}
        if action == "client":
            os.startfile(client)  # type: ignore[attr-defined]
            return {"ok": True, "message": "已在 Steam 客户端打开创意工坊页面。"}
        if action == "unsubscribe":
            os.startfile(client)  # type: ignore[attr-defined]
            return {"ok": True, "message": "已打开 Steam 工坊页面：点“取消订阅”即可。Steam 不提供第三方取消订阅接口。"}
        if action == "force_update":
            os.startfile(client)  # type: ignore[attr-defined]
            return {"ok": True, "message": "已打开 Steam 页面：先取消订阅，再重新订阅，即可强制更新。"}
        raise ValueError("未知的 Steam 操作。")

    def auto_sort(self, payload):
        snapshot = self.catalog.scan()
        mods = snapshot["mods"]
        if not mods:
            raise ValueError("当前播放集没有 MOD。")
        mode = payload.get("mode") if payload.get("mode") in ("community", "stable", "smart") else "community"
        ordered, reasons = sorting.sort_mods(mods, mode)
        old = {mod["id"]: index + 1 for index, mod in enumerate(mods)}
        changes = []
        preview = []
        for index, mod in enumerate(ordered):
            tier, category = sorting.classify(mod) if mode in ("community", "smart") else (0, "保持原序")
            preview.append({
                "mod_id": mod["id"],
                "name": mod["name"],
                "old": old[mod["id"]],
                "new": index + 1,
                "reason": reasons.get(mod["id"], category),
                "category": category,
            })
            if old[mod["id"]] != index + 1:
                changes.append(preview[-1])
        return {
            "ok": True,
            "mode": mode,
            "order": [{"mod_id": mod["id"], "enabled": mod["enabled"]} for mod in ordered],
            "changes": changes[:200],
            "moved": len(changes),
            "preview": preview[:300],
        }


    def ai_conflicts(self, payload):
        """把扫描出的文件覆盖冲突交给 AI 解读。"""
        snapshot = self.catalog.scan()
        analysis = snapshot.get("analysis") or {}
        conflicts = analysis.get("conflicts") or []
        order = [m.get("name") for m in snapshot.get("enabled") or [] if m.get("name")]
        result = self.ai().analyze_conflicts(conflicts, order=order)
        result["ok"] = True
        result["conflict_total"] = analysis.get("conflict_total") or len(conflicts)
        return result

    def rules_sort(self, payload):
        """读 MOD 说明里的排序要求，按抽出的规则重排；返回与 auto_sort 同形状。"""
        snapshot = self.catalog.scan()
        mods = snapshot["mods"]
        if not mods:
            raise ValueError("当前播放集没有 MOD。")
        force = bool(payload.get("force"))
        enriched = []
        for mod in mods:
            try:
                enriched.append(self._with_workshop_text(mod))
            except Exception:
                enriched.append(dict(mod))
        rules, notes, cached = self.ai().extract_rules(enriched, force=force)
        ordered, applied, warnings = sorting.apply_rules(mods, rules)
        reason_by_id = {}
        for row in applied:
            label = "AI 规则：%s" % row.get("kind")
            if row.get("target"):
                label += " " + str(row.get("target"))
            reason_by_id[row.get("mod_id")] = label
        old = {mod["id"]: index + 1 for index, mod in enumerate(mods)}
        changes = []
        preview = []
        for index, mod in enumerate(ordered):
            item = {
                "mod_id": mod["id"],
                "name": mod["name"],
                "old": old[mod["id"]],
                "new": index + 1,
                "reason": reason_by_id.get(mod["id"], "保持原序"),
                "category": reason_by_id.get(mod["id"], "保持原序"),
            }
            preview.append(item)
            if old[mod["id"]] != index + 1:
                changes.append(item)
        return {
            "ok": True,
            "mode": "rules",
            "order": [{"mod_id": mod["id"], "enabled": mod["enabled"]} for mod in ordered],
            "changes": changes[:200],
            "moved": len(changes),
            "preview": preview[:300],
            "rules": applied[:60],
            "warnings": warnings[:40],
            "notes": notes,
            "cached": cached,
            "rules_total": len(rules),
        }

    def ai_sort(self, payload):
        """AI 排序：把启用的 MOD 交给模型排一遍，返回与 auto_sort 同形状的预览。"""
        snapshot = self.catalog.scan()
        mods = snapshot["mods"]
        enabled = [m for m in mods if m.get("enabled")]
        if not enabled:
            raise ValueError("当前播放集没有启用的 MOD。")
        ordered, notes = self.ai().sort_mods(enabled)
        rank = {str(m["id"]): index + 1 for index, m in enumerate(ordered)}
        old = {str(m["id"]): index + 1 for index, m in enumerate(mods)}
        preview = []
        changes = []
        for mod in ordered:
            item = {
                "mod_id": mod["id"],
                "name": mod.get("name"),
                "old": old.get(str(mod["id"]), 0),
                "new": rank[str(mod["id"])],
                "reason": "AI 建议",
                "category": "AI 排序",
            }
            preview.append(item)
            if item["old"] != item["new"]:
                changes.append(item)
        ordered_ids = [str(m["id"]) for m in ordered]
        rest = [m for m in mods if str(m["id"]) not in set(ordered_ids)]
        final = ordered + rest
        return {
            "ok": True,
            "mode": "ai",
            "order": [{"mod_id": m["id"], "enabled": bool(m.get("enabled"))} for m in final],
            "changes": changes[:200],
            "moved": len(changes),
            "preview": preview[:300],
            "notes": notes,
        }

    def quit_app(self, payload):
        threading.Timer(0.6, lambda: os._exit(0)).start()
        return {"ok": True, "message": "正在退出管理器。"}

    def history(self, payload):
        return {"ok": True, "history": self._load_history()}

    def open_path(self, payload):
        kind = payload.get("kind")
        info = self.catalog.vision()
        mapping = {
            "mod": info.get("mod_dir"),
            "workshop": info.get("workshop"),
            "game": info.get("game"),
            "db": info.get("db"),
            "logs": str(Path(info.get("docs") or "") / "logs"),
            "crashes": str(Path(info.get("docs") or "") / "crashes"),
        }
        target = mapping.get(kind)
        if not target or not Path(target).exists():
            raise ValueError("目标路径不存在。")
        if kind == "db":
            subprocess.Popen(["explorer", "/select,", str(Path(target))])
        else:
            os.startfile(str(Path(target)))  # type: ignore[attr-defined]
        return {"ok": True}

    def open_mod_dir(self, payload):
        mod_id = str(payload.get("mod_id") or "")
        for mod in self.catalog.scan().get("mods", []):
            if mod["id"] == mod_id:
                if mod.get("dir") and Path(mod["dir"]).is_dir():
                    os.startfile(mod["dir"])  # type: ignore[attr-defined]
                    return {"ok": True}
                raise ValueError("该 MOD 没有本地目录。")
        raise ValueError("找不到该 MOD。")

    def _validate_backup_file(self, source):
        """Refuse a damaged backup before it is copied over the live database."""
        try:
            probe = sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro", uri=True)
            try:
                tables = {row[0] for row in probe.execute(
                    "select name from sqlite_master where type='table'")}
            finally:
                probe.close()
        except (sqlite3.Error, OSError) as exc:
            raise ValueError(f"备份文件 {Path(source).name} 已损坏，不是可用的启动器数据库，"
                             "数据库未做任何改动。") from exc
        missing = {"mods", "playsets", "playsets_mods"} - tables
        if missing:
            raise ValueError(f"备份文件 {Path(source).name} 不是启动器数据库（缺少 "
                             f"{'/'.join(sorted(missing))} 表），数据库未做任何改动。")
        # A table can exist with the wrong columns; copying such a file over the
        # live DB only fails afterwards, leaving the manager unusable.
        probes = (
            ("mods", "select id, name, dirPath from mods limit 1"),
            ("playsets", "select id, name from playsets limit 1"),
            ("playsets_mods", "select playsetId, modId, enabled, position from playsets_mods limit 1"),
        )
        try:
            probe = sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro", uri=True)
            try:
                for label, sql in probes:
                    try:
                        probe.execute(sql).fetchall()
                    except sqlite3.Error as exc:
                        raise ValueError(
                            f"备份文件 {Path(source).name} 的表 {label} 结构不完整"
                            f"（{exc}），数据库未做任何改动。") from exc
            finally:
                probe.close()
        except ValueError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise ValueError(f"备份文件 {Path(source).name} 已损坏，数据库未做任何改动。") from exc
        return True

    def restore_backup(self, payload):
        # Checking the file and copying it must happen under the same lock as the
        # writers: a concurrent save_order rotates old backups away, so the source
        # file could disappear between the check and the copy (stress runs showed
        # a 45% failure rate with "备份文件读取失败" before this).
        lock = getattr(self, "_db_lock", None)
        if lock is None:  # bare instances built via object.__new__ in tests
            return self._restore_backup(payload)
        with lock:
            return self._restore_backup(payload)

    def _restore_backup(self, payload):
        name = Path(payload.get("name") or "").name
        if not name:
            raise ValueError("缺少备份文件名。")
        source = self.data_dir / "backups" / name
        if not source.is_file():
            raise ValueError("备份文件不存在（可能已被备份轮转清理），请刷新备份列表后重试。")
        # Ownership is metadata we wrote ourselves, so check it before the slower
        # sqlite header validation: a foreign backup is a user mistake, not corruption.
        owner = self._backup_game(source)
        if owner and owner != self.game_key:
            raise ValueError(
                f"这个备份属于 {games.get(owner)['label']}，不能在 {games.get(self.game_key)['label']} 下恢复。")
        self._validate_backup_file(source)
        self._guard_write()

        def mutate():
            try:
                src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
                try:
                    dst = sqlite3.connect(str(self.db.path))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
            except sqlite3.Error as exc:
                raise ValueError("备份文件读取失败，数据库未做任何改动。") from exc
            # Second line of defence: the copy itself may have produced a DB that
            # cannot be read back. Never leave the live file in that state.
            try:
                chk = sqlite3.connect(str(self.db.path))
                try:
                    bad = chk.execute("pragma quick_check").fetchone()
                    if not bad or str(bad[0]).lower() != "ok":
                        raise sqlite3.DatabaseError("quick_check failed")
                    chk.execute("select id, name, dirPath from mods limit 1").fetchall()
                    chk.execute("select id, name from playsets limit 1").fetchall()
                    chk.execute(
                        "select playsetId, modId, enabled, position from playsets_mods limit 1").fetchall()
                finally:
                    chk.close()
            except sqlite3.Error as exc:
                raise ValueError(
                    "备份文件内容不完整，已放弃恢复（数据库保持原样）。") from exc
            # dlc_load.json is a copy of the playset state; leaving the pre-rollback
            # list behind would launch the game with mods the DB no longer enables.
            self._sync_dlc_load_safe("数据库回滚")
            result = {"restored": name}
            if not owner:
                # Legacy backups carry no game marker, so nothing stopped the user
                # from rolling one game's DB over another. Do not block the restore
                # (that would strand old files), but say it out loud.
                self.last_warning = (
                    "这是旧格式备份（未标游戏），已按当前游戏恢复，请确认 MOD 列表是否正确。")
            return result

        return self._write_and_refresh(mutate)

    def export_playset(self, payload):
        snapshot = self.catalog.scan()
        active = snapshot.get("active_playset")
        playset = next((p for p in snapshot["playsets"] if p["id"] == active), None)
        if not playset:
            raise ValueError("没有可导出的播放集。")
        return {
            "format": "stellaris-mod-manager/playset",
            "game": self.game_key,
            "version": APP_VERSION,
            "name": playset["name"],
            "mods": [
                {
                    "id": m["id"],
                    "displayName": m["name"],
                    "enabled": m["enabled"],
                    "position": index,
                    "steamId": m["steam_id"],
                }
                for index, m in enumerate(snapshot["mods"])
            ],
        }

    def export_playset_file(self):
        payload = self.export_playset({})
        folder = self.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / f"playset-{stamp}.json"
        counter = 1
        # Two exports inside one second used to overwrite each other, so the
        # second file the user asked for silently never existed.
        while path.exists():
            counter += 1
            path = folder / f"playset-{stamp}-{counter}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "count": len(payload["mods"])}

    def thumb(self, mod_id):
        snapshot = self.catalog.scan()
        for mod in snapshot.get("mods", []):
            if mod["id"] == mod_id and mod.get("thumbnail"):
                return Path(mod["thumbnail"])
        for mod in snapshot.get("available", []):
            if mod["id"] == mod_id and mod.get("thumbnail"):
                return Path(mod["thumbnail"])
        return None

    def preview(self, mod_id):
        """创意工坊预览图（本地缓存），拿不到就返回 None，前端退回齿轮兜底。"""
        cache = self._preview_cache()
        if cache is None:
            return None
        return cache.path_for(mod_id)

    def _preview_cache(self):
        cache = getattr(self, "_preview_cache_obj", None)
        if cache is None:
            cache = preview.PreviewCache(self.data_dir, self.workshop)
            self._preview_cache_obj = cache
        return cache

    def _history_path(self):
        return self.data_dir / "history.json"

    def _load_history(self):
        path = self._history_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _record_launch(self, mode):
        snapshot = self.catalog.scan()
        playset = next((p for p in snapshot["playsets"] if p["id"] == snapshot["active_playset"]), None)
        entry = {
            "time": int(time.time() * 1000),
            "mode": mode,
            "playset": playset["name"] if playset else "",
            "mods": snapshot["stats"]["enabled"],
            "version": (snapshot.get("game") or {}).get("raw_version") or (snapshot.get("game") or {}).get("version") or "",
        }
        history = self._load_history()
        history.insert(0, entry)
        path = self._history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history[:50], ensure_ascii=False, indent=1), encoding="utf-8")
        return entry

    def dispatch(self, action, payload):
        if self.read_only and action in READ_ONLY_BLOCKED:
            raise ValueError("当前是只读模式，未写入任何文件。")
        handlers = {
            "bootstrap": lambda p: self.bootstrap(),
            "switch_game": self.switch_game,
            "refresh": self.refresh,
            "save_order": self.save_order,
            "set_active": self.set_active,
            "create_playset": self.create_playset,
            "rename_playset": self.rename_playset,
            "delete_playset": self.delete_playset,
            "add_mods": self.add_mods,
            "remove_mods": self.remove_mods,
            "launch_game": self.launch_game,
            "runtime": self.runtime,
            "low_power": self.low_power,
            "analyze": self.analyze,
            "analysis_history": self.analysis_history,
            "workshop_refresh": self.workshop_refresh,
            "workshop_collection": self.workshop_collection,
            "import_collection": self.import_collection,
            "saves": self.list_saves,
            "save_backup": self.save_backup,
            "save_backups": self.save_backups,
            "save_restore": self.save_restore,
            "diagnostics": self.diagnostics,
            "safe_mode_apply": self.safe_mode_apply,
            "manager_files_scan": self.manager_files_scan,
            "manager_files_delete": self.manager_files_delete,
            "cleanup_scan": self.cleanup_scan,
            "cleanup_delete": self.cleanup_delete,
            "snapshot_mods": self.snapshot_mods,
            "list_mod_snapshots": self.list_mod_snapshots,
            "restore_mod_snapshot": self.restore_mod_snapshot,
            "delete_mod_snapshot": self.delete_mod_snapshot,
            "list_merged_packs": self.list_merged_packs,
            "delete_merged_pack": self.delete_merged_pack,
            "share_playset": self.share_playset,
            "import_share_code": self.import_share_code,
            "dlc_status": self.dlc_status,
            "enable_all_dlc": self.enable_all_dlc,
            "game_settings": self.get_game_settings,
            "save_game_settings": self.save_game_settings,
            "set_game_path": self.set_game_path,
            "set_mod_path": self.set_mod_path,
            "reset_paths": self.reset_paths,
            "check_update": self.check_update,
            "download_update": self.download_update,
            "apply_update": self.apply_update,
            "import_local_mod": self.import_local_mod,
            "merge_enabled_mods": self.merge_enabled_mods,
            "workshop_prepare": self.workshop_prepare,
            "workshop_read_result": self.workshop_read_result,
            "workshop_upload_status": self.workshop_upload_status,
            "workshop_launch": self.workshop_launch,
            "save_detail": self.save_detail,
            "import_playset": self.import_playset,
            "apply_save": self.apply_save,
            "auto_sort": self.auto_sort,
        "ai_sort": self.ai_sort,
        "rules_sort": self.rules_sort,
        "crash_report": self.crash_report,
        "crash_explain": self.crash_explain,
        "ai_conflicts": self.ai_conflicts,
            "save_note": self.save_note,
            "folder_create": self.folder_create,
            "folder_rename": self.folder_rename,
            "folder_delete": self.folder_delete,
            "folder_assign": self.folder_assign,
            "folder_reorder": self.folder_reorder,
            "ai_settings": lambda p: self.ai().settings.snapshot(),
            "ai_save_settings": self.ai_save_settings,
        "ai_test": lambda p: self.ai().test_connection(),
            "ai_clear_key": lambda p: self.ai().settings.clear(),
            "ai_analyze": self.ai_analyze,
            "set_docs_path": self.set_docs_path,
            "steam_action": self.steam_action,
            "quit": self.quit_app,
            "history": self.history,
            "open_path": self.open_path,
            "open_mod_dir": self.open_mod_dir,
            "backups": lambda p: {"backups": self._backups()},
            "restore_backup": self.restore_backup,
            "export_playset": self.export_playset,
            "export_playset_file": lambda p: self.export_playset_file(),
            "conflict_diff": self.conflict_diff,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"未知操作：{action}")
        return handler(payload or {})

    def conflict_diff(self, payload):
        """Code preview for ONE conflicting file; the scan never reads file bodies."""
        path = (payload or {}).get("path")
        mod_ids = (payload or {}).get("mod_ids") or (payload or {}).get("mods") or []
        if not isinstance(mod_ids, (list, tuple)):
            raise ValueError("mod_ids 必须是数组。")
        return conflict_preview(self.catalog, path, mod_ids)


















































