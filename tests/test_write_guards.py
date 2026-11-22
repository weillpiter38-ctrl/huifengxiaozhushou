"""Regression tests for QA round 2: destructive-payload guards, unique export
names, backup sidecar pruning and switch_game input validation."""
import os
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path

from smm.api import Api
from smm import games
from smm.launcher_db import LauncherDB, _enabled_flag


class _Catalog:
    def __init__(self, mods=("m1",)):
        self._mods = list(mods)

    def scan(self, force=False):
        return {"mods": [{"id": m, "enabled": True} for m in self._mods], "available": [],
                "playsets": [{"id": "p1", "name": "keep me"}],
                "active_playset": "p1"}


class _SpyDB:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"playsets": [{"id": "p1"}], "entries": []}

    def save_order(self, playset_id, entries):
        self.calls.append((playset_id, entries))
        return len(entries)


class SaveOrderGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa2-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.catalog = _Catalog()
        self.api.db = _SpyDB()
        self.api.data_dir = self.root
        self.api.last_warning = ""

    def test_all_invalid_entries_are_rejected_instead_of_wiping(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.save_order({"playset_id": "p1", "order": [1, 2, 3]})
        self.assertIn("没有可用的 MOD 条目", str(ctx.exception))
        self.assertEqual(self.api.db.calls, [])

    def test_unknown_ids_only_are_rejected_instead_of_wiping(self):
        with self.assertRaises(ValueError):
            self.api.save_order({"playset_id": "p1",
                                 "order": [{"mod_id": "ghost", "enabled": True}]})
        self.assertEqual(self.api.db.calls, [])

    def test_explicit_empty_order_still_clears_the_playset(self):
        self.api._write_and_refresh = lambda action: action()
        self.api._sync_dlc_load_safe = lambda *a, **k: None
        self.assertEqual(self.api.save_order({"playset_id": "p1", "order": []}), 0)
        self.assertEqual(self.api.db.calls, [("p1", [])])

    def test_partially_invalid_order_is_saved_with_warning(self):
        self.api._write_and_refresh = lambda action: action()
        self.api._sync_dlc_load_safe = lambda *a, **k: None
        self.api.save_order({"playset_id": "p1",
                             "order": [{"mod_id": "m1", "enabled": False}, 7]})
        self.assertEqual(len(self.api.db.calls), 1)
        self.assertEqual([e["mod_id"] for e in self.api.db.calls[0][1]], ["m1"])
        self.assertIn("忽略了", self.api.last_warning)


class SwitchGameGuardTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(Api)
        self.api.game_key = "stellaris"

    def test_unknown_game_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.switch_game({"game": "skyrim"})
        self.assertIn("不支持的游戏", str(ctx.exception))

    def test_missing_game_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.switch_game({})
        self.assertIn("缺少游戏标识", str(ctx.exception))

    def test_known_game_keys_still_pass_validation(self):
        for key in games.GAMES:
            self.assertIn(key, games.GAMES)


class ExportNamingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa2-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.data_dir = self.root
        self.api.export_playset = lambda payload: {"mods": [], "name": "x"}

    def test_three_exports_in_the_same_second_leave_three_files(self):
        names = [self.api.export_playset_file()["path"] for _ in range(3)]
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(len(list((self.root / "exports").glob("playset-*.json"))), 3)


class BackupPruneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa2-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db_path = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(str(self.db_path))
        con.execute("CREATE TABLE t (a INTEGER)")
        con.commit()
        con.close()

    def test_sidecar_metadata_is_pruned_with_its_backup(self):
        dest = self.root / "backups"
        db = LauncherDB(str(self.db_path))
        for _ in range(6):
            backup = db.backup(dest, keep=2)
            (backup.parent / (backup.name + ".json")).write_text("{}", encoding="utf-8")
        sqlite_files = list(dest.glob("launcher-v2-*.sqlite"))
        meta_files = list(dest.glob("launcher-v2-*.sqlite.json"))
        self.assertLessEqual(len(sqlite_files), 2)
        self.assertLessEqual(len(meta_files), 2)
        for meta in meta_files:
            self.assertTrue(meta.with_suffix("").exists())


class EnabledFlagTests(unittest.TestCase):
    def test_string_forms_are_parsed_not_truthy(self):
        for value in ("false", "False", "0", "no", "off", "", "  "):
            self.assertFalse(_enabled_flag(value), value)
        for value in ("true", "True", "1", "yes", "on"):
            self.assertTrue(_enabled_flag(value), value)

    def test_native_types(self):
        self.assertTrue(_enabled_flag(True))
        self.assertFalse(_enabled_flag(False))
        self.assertTrue(_enabled_flag(1))
        self.assertFalse(_enabled_flag(0))
        self.assertTrue(_enabled_flag(None))
        self.assertFalse(_enabled_flag(None, default=False))


class RemoveModsGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa2-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.catalog = _Catalog(("m1", "m2"))
        self.api.db = _SpyDB()
        self.api.data_dir = self.root
        self.api.last_warning = ""
        self.api._write_and_refresh = lambda action: action()
        self.api._sync_dlc_load_safe = lambda *a, **k: None

    def test_empty_removal_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.remove_mods({"playset_id": "p1", "mod_ids": []})
        self.assertIn("请选择要移除的 MOD", str(ctx.exception))
        self.assertEqual(self.api.db.calls, [])

    def test_non_list_removal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.api.remove_mods({"playset_id": "p1", "mod_ids": "m1"})
        self.assertEqual(self.api.db.calls, [])

    def test_unknown_ids_are_reported_as_warning(self):
        self.api.remove_mods({"playset_id": "p1", "mod_ids": ["ghost"]})
        self.assertEqual(self.api.db.calls[0][1], [{"mod_id": "m1", "enabled": True},
                                                   {"mod_id": "m2", "enabled": True}])
        self.assertIn("忽略了 1 个", self.api.last_warning)


class CorruptDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa3-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_snapshot_on_corrupt_file_raises_chinese_error(self):
        db = self.root / "launcher-v2.sqlite"
        db.write_bytes(b"not a sqlite database" * 40)
        with self.assertRaises(ValueError) as ctx:
            LauncherDB(db).snapshot()
        self.assertIn("启动器数据库无法读取", str(ctx.exception))

    def test_prepare_sandbox_on_corrupt_file_exits_with_chinese_message(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "launcher-v2.sqlite").write_bytes(b"garbage" * 100)
        import run

        with self.assertRaises(SystemExit) as ctx:
            run.prepare_sandbox(docs, self.root / "data", False)
        self.assertIn("启动器数据库无法读取", str(ctx.exception))
        self.assertFalse((self.root / "data" / "sandbox" / "launcher-v2.sqlite").exists())

    def test_snapshot_on_locked_database_reports_busy(self):
        db = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("create table mods (id text)")
        con.execute("create table playsets (id text)")
        con.execute("create table playsets_mods (playsetId text, modId text)")
        con.commit()
        con.close()
        lock = sqlite3.connect(str(db), timeout=1, isolation_level=None)
        lock.execute("PRAGMA journal_mode=DELETE")
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(ValueError) as ctx:
                LauncherDB(db).snapshot()
            self.assertIn("正被其他程序占用", str(ctx.exception))
        finally:
            lock.execute("ROLLBACK")
            lock.close()

    def test_backup_on_locked_database_reports_busy(self):
        db = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("create table t (x integer)")
        con.commit()
        con.close()
        lock = sqlite3.connect(str(db), timeout=1, isolation_level=None)
        lock.execute("PRAGMA journal_mode=DELETE")
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(ValueError) as ctx:
                LauncherDB(db).backup(self.root / "backups")
            self.assertIn("正被其他程序占用", str(ctx.exception))
        finally:
            lock.execute("ROLLBACK")
            lock.close()


class WriteFailureMappingTests(unittest.TestCase):
    """Round 4: a non-writable data dir used to surface as HTTP 500 内部错误."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa4-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.data_dir = self.root
        self.api.read_only = False
        self.api.game_key = "stellaris"
        self.api.last_warning = ""
        self.api.last_backup = ""
        self.api.catalog = _Catalog()
        self.api._guard_write = lambda: None

    def _api_with(self, db):
        self.api.db = db
        return self.api

    def test_database_error_becomes_readable_value_error(self):
        class Boom:
            def backup(self, dest):
                raise FileNotFoundError("no launcher db")

            def save_order(self, playset_id, entries):
                raise sqlite3.OperationalError("unable to open database file")

        api = self._api_with(Boom())
        with self.assertRaises(ValueError) as ctx:
            api._write_and_refresh(lambda: api.db.save_order("p1", []))
        message = str(ctx.exception)
        self.assertIn("写入失败", message)
        self.assertIn("没有保存", message)
        self.assertNotIn("unable to open", message)

    def test_permission_error_becomes_readable_value_error(self):
        class Boom:
            def backup(self, dest):
                raise FileNotFoundError("no launcher db")

            def create_playset(self, name, ids):
                raise PermissionError(13, "Permission denied")

        api = self._api_with(Boom())
        with self.assertRaises(ValueError) as ctx:
            api._write_and_refresh(lambda: api.db.create_playset("x", []))
        self.assertIn("文件夹权限", str(ctx.exception))

    def test_validation_errors_from_action_are_not_rewritten(self):
        class Boom:
            def backup(self, dest):
                raise FileNotFoundError("no launcher db")

            def save_order(self, playset_id, entries):
                raise ValueError("播放集不存在。")

        api = self._api_with(Boom())
        with self.assertRaises(ValueError) as ctx:
            api._write_and_refresh(lambda: api.db.save_order("p1", []))
        self.assertEqual(str(ctx.exception), "播放集不存在。")


class DiskFullTests(unittest.TestCase):
    """ENOSPC cannot be produced on the system volume safely, so it is injected
    at the call boundary; the HTTP mapping is the same except-branch as the
    ACL case verified live in reports\\tester1-r4.md."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa4-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.data_dir = self.root
        self.api.read_only = False
        self.api.game_key = "stellaris"
        self.api.last_warning = ""
        self.api.last_backup = ""
        self.api.catalog = _Catalog()
        self.api._guard_write = lambda: None

    def test_enospc_during_action_is_reported_in_chinese(self):
        class Boom:
            def backup(self, dest):
                raise FileNotFoundError("no launcher db")

            def save_order(self, playset_id, entries):
                raise OSError(28, "No space left on device")

        self.api.db = Boom()
        with self.assertRaises(ValueError) as ctx:
            self.api._write_and_refresh(lambda: self.api.db.save_order("p1", []))
        self.assertIn("写入失败", str(ctx.exception))
        self.assertNotIn("No space left", str(ctx.exception))

    def test_disabled_flag_written_for_mixed_payload(self):
        db_path = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("create table playsets (id text primary key, name text, isActive integer, "
                    "loadOrder text, createdOn text, updatedOn text)")
        con.execute("create table playsets_mods (playsetId text, modId text, enabled integer, position integer)")
        con.execute("insert into playsets (id, name, isActive, loadOrder) values ('p1', 'x', 1, 'custom')")
        con.commit()
        con.close()
        db = LauncherDB(str(db_path))
        db.create_playset("mix", [{"mod_id": f"m{i}", "enabled": i < 7} for i in range(19)])
        con = sqlite3.connect(str(db_path))
        on = con.execute("select count(*) from playsets_mods where enabled=1").fetchone()[0]
        off = con.execute("select count(*) from playsets_mods where enabled=0").fetchone()[0]
        con.close()
        self.assertEqual((on, off), (7, 12))


class FailedWriteKeepsUsableBackupTests(unittest.TestCase):
    """OBS-1 / chief decision: a failed write may leave the pre-write safety
    copy behind; it has to be a complete database (integrity_check = ok), never
    a truncated fragment."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa5-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db_path = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(str(self.db_path))
        con.execute("create table playsets (id text primary key, name text, isActive integer, "
                    "loadOrder text, createdOn text, updatedOn text)")
        con.execute("create table playsets_mods (playsetId text, modId text, enabled integer, position integer)")
        con.execute("insert into playsets (id, name, isActive, loadOrder) values ('p1', 'x', 1, 'custom')")
        con.execute("insert into playsets_mods values ('p1', 'm1', 1, 0)")
        con.commit()
        con.close()
        self.api = object.__new__(Api)
        self.api.data_dir = self.root
        self.api.read_only = False
        self.api.game_key = "stellaris"
        self.api.last_warning = ""
        self.api.last_backup = ""
        self.api.db = LauncherDB(str(self.db_path))
        self.api.catalog = _Catalog()
        self.api._guard_write = lambda: None

    def test_failed_write_leaves_intact_backup(self):
        backups = self.root / "backups"
        self.api._write_backup_meta = lambda path: None
        os.chmod(self.db_path, 0o444)
        try:
            with self.assertRaises(ValueError):
                self.api._write_and_refresh(
                    lambda: self.api.db.save_order("p1", [{"mod_id": "m1"}]))
        finally:
            os.chmod(self.db_path, 0o666)
        copies = sorted(backups.glob("launcher-v2-*.sqlite"))
        self.assertTrue(copies, "a pre-write safety copy should exist")
        con = sqlite3.connect(str(copies[-1]))
        try:
            self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("select count(*) from playsets_mods").fetchone()[0], 1)
        finally:
            con.close()
        con = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(con.execute("select position from playsets_mods").fetchone()[0], 0)
        finally:
            con.close()


class BrokenConfigFileTests(unittest.TestCase):
    """QA round 6: a config file holding valid JSON of the wrong type
    (null / [] / "text" / 123) used to crash the whole process on startup."""

    class _PathCatalog:
        def __init__(self):
            self.game = Path("C:/game")
            self.docs = Path("C:/docs")
            self.mod_dir_override = None
            self.invalidated = 0

        def invalidate(self):
            self.invalidated += 1

    def _api(self, payload_text):
        root = Path(tempfile.mkdtemp(prefix="smm-qa6-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "paths.json").write_text(payload_text, encoding="utf-8")
        api = object.__new__(Api)
        api.data_dir = root
        api.catalog = self._PathCatalog()
        return api

    def test_paths_json_of_wrong_type_is_ignored(self):
        for payload in ("null", "[]", '"hello"', "123", "true"):
            with self.subTest(payload=payload):
                api = self._api(payload)
                before = (api.catalog.game, api.catalog.docs, api.catalog.mod_dir_override)
                api._load_path_settings()
                self.assertEqual((api.catalog.game, api.catalog.docs,
                                  api.catalog.mod_dir_override), before)

    def test_paths_json_object_still_applies(self):
        api = self._api('{"mod_dir": "%s"}' % str(Path(tempfile.gettempdir())).replace("\\", "\\\\"))
        api._load_path_settings()
        self.assertEqual(api.catalog.mod_dir_override, str(Path(tempfile.gettempdir())))

    def test_active_game_and_launcher_settings_of_wrong_type_do_not_crash(self):
        from smm import games, paths
        root = Path(tempfile.mkdtemp(prefix="smm-qa6-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "active-game.json").write_text("null", encoding="utf-8")
        self.assertEqual(games.read_active(root), games.DEFAULT_KEY)
        game = root / "game"
        game.mkdir()
        for payload in ("[1, 2, 3]", '"text"', "null"):
            with self.subTest(payload=payload):
                (game / "launcher-settings.json").write_text(payload, encoding="utf-8")
                docs = root / "docs"
                docs.mkdir(exist_ok=True)
                info = paths.detect(docs, game, game_key="stellaris")
                self.assertEqual(info["version"], "")
                self.assertEqual(info["raw_version"], "")

    def test_userdata_json_of_wrong_type_is_ignored(self):
        from smm.userdata import UserData
        for payload in ("null", "[]", '"x"', '{"notes": [], "folders": {}, "assignments": 3}'):
            with self.subTest(payload=payload):
                root = Path(tempfile.mkdtemp(prefix="smm-qa6-"))
                self.addCleanup(shutil.rmtree, root, True)
                (root / "userdata.json").write_text(payload, encoding="utf-8")
                data = UserData(root)
                self.assertEqual(data.notes, {})
                self.assertEqual(data.folders, [])
                self.assertEqual(data.assignments, {})


if __name__ == "__main__":
    unittest.main()

