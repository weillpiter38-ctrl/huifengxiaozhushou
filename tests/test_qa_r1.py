"""Round-1 regression tests: rollback fidelity, recycle truth, cleanup guard,
stale-id guards and updater manifest messages.

Everything here runs against temporary folders; no real game or Paradox files.
"""
from __future__ import annotations

import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from smm import net, updater
from smm.api import Api
from smm.catalog import Catalog
from smm.launcher_db import LauncherDB

SCHEMA = """
create table mods (
  id char(36) primary key, steamId varchar(255), name varchar(255), displayName varchar(255),
  dirPath text, version varchar(255), requiredVersion varchar(255), tags json,
  status text, source text, timeUpdated integer, size integer, thumbnailPath text, gameRegistryId text
);
create table playsets (
  id char(36) primary key, name varchar(255) not null, isActive boolean,
  loadOrder varchar(255), createdOn datetime, updatedOn datetime
);
create table playsets_mods (
  playsetId char(36) not null, modId char(36) not null, enabled boolean, position integer
);
"""


class FakeCatalog:
    def __init__(self, mod_dir, mods=(), available=()):
        self._mod_dir = str(mod_dir)
        self._mods = list(mods)
        self._available = list(available)
        self.game = ""
        self.docs = ""

    def vision(self):
        return {"mod_dir": self._mod_dir, "raw_version": "v4.5.4", "game": self.game, "docs": self.docs}

    def scan(self, force=False):
        return {"mods": self._mods, "available": self._available, "active_playset": "ps-1"}

    def invalidate(self):
        return None


def zip_payload(name):
    """Base64 zip with a root descriptor, exactly what the import dialog sends."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("descriptor.mod", f'name="{name}"\nversion="1.0"\n')
        archive.writestr("common/thing.txt", "x")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def bare_api(mod_dir, data_dir, mods=(), available=()):
    api = object.__new__(Api)
    api.read_only = False
    api.sandbox = True
    api.data_dir = Path(data_dir)
    api.last_warning = None
    api.catalog = FakeCatalog(mod_dir, mods, available)
    return api


class RecycleTruthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_or_raise_reports_failure_instead_of_success(self):
        api = bare_api(self.root, self.root / "data")
        locked = self.root / "stray.mod"
        locked.write_text("x", encoding="utf-8")
        with mock.patch.object(Api, "_recycle", staticmethod(lambda paths: ([], [Path(p) for p in paths]))):
            with self.assertRaises(ValueError) as ctx:
                api._recycle_or_raise([locked], "项目")
        message = str(ctx.exception)
        self.assertIn("stray.mod", message)
        self.assertIn("没能移到回收站", message)
        self.assertIn("请关闭占用程序后重试", message)

    def test_recycle_of_missing_path_is_not_reported_as_moved(self):
        api = bare_api(self.root, self.root / "data")
        moved, failed = api._recycle([self.root / "gone.mod"])
        self.assertEqual(moved, [])
        self.assertEqual(failed, [])

    def test_delete_merged_pack_keeps_registry_when_delete_fails(self):
        mod_dir = self.root / "mod"
        mod_dir.mkdir()
        data_dir = self.root / "data"
        data_dir.mkdir()
        zip_path = mod_dir / "Pack.zip"
        zip_path.write_bytes(b"zip")
        mod_file = mod_dir / "Pack.mod"
        mod_file.write_text('name="Pack"', encoding="utf-8")
        registry = data_dir / "merged-packs.json"
        registry.write_text(json.dumps([{"name": "Pack", "path": str(zip_path)}], ensure_ascii=False),
                            encoding="utf-8")

        api = bare_api(mod_dir, data_dir)
        api.db = mock.Mock()
        api.db.exists.return_value = False
        with mock.patch.object(Api, "_recycle", staticmethod(lambda paths: ([], [Path(p) for p in paths]))):
            with self.assertRaises(ValueError):
                api.delete_merged_pack({"path": str(zip_path), "mod_file": str(mod_file)})

        self.assertTrue(zip_path.is_file(), "删除失败时不能留下孤儿状态")
        rows = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1, "删除失败时不能让 registry 丢掉记录")


class CleanupGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_refuses_to_delete_a_mod_still_in_the_list(self):
        live = self.mod_dir / "live-mod"
        live.mkdir()
        (live / "descriptor.mod").write_text('name="Live"', encoding="utf-8")
        api = bare_api(self.mod_dir, self.root / "data",
                       mods=[{"id": "live", "dir": str(live), "enabled": True}])
        with self.assertRaises(ValueError) as ctx:
            api.cleanup_delete({"paths": [str(live)]})
        self.assertIn("仍被 MOD 列表使用", str(ctx.exception))
        self.assertTrue(live.is_dir(), "在用 MOD 目录不能被删掉")

    def test_cleanup_deletes_only_unreferenced_items(self):
        stray = self.mod_dir / "stray.mod"
        stray.write_text("x", encoding="utf-8")
        api = bare_api(self.mod_dir, self.root / "data")
        with mock.patch.object(Api, "_recycle", staticmethod(lambda paths: ([Path(p) for p in paths], []))):
            result = api.cleanup_delete({"paths": [str(stray)]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 1)

    def test_cleanup_rejects_paths_outside_mod_dir(self):
        outside = self.root / "outside.mod"
        outside.write_text("x", encoding="utf-8")
        api = bare_api(self.mod_dir, self.root / "data")
        with self.assertRaises(ValueError):
            api.cleanup_delete({"paths": [str(outside)]})
        self.assertTrue(outside.is_file())


class SnapshotRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_purge_extra_removes_what_the_snapshot_never_had(self):
        source = self.root / "snapshot"
        (source / "common").mkdir(parents=True)
        (source / "common" / "shared.txt").write_text("clean", encoding="utf-8")

        target = self.root / "live"
        (target / "common").mkdir(parents=True)
        (target / "common" / "shared.txt").write_text("dirty", encoding="utf-8")
        (target / "common" / "added_by_user.txt").write_text("leftover", encoding="utf-8")
        (target / "junk").mkdir()

        removed = Api._purge_extra(source, target)
        self.assertGreaterEqual(removed, 2)
        self.assertFalse((target / "common" / "added_by_user.txt").exists())
        self.assertFalse((target / "junk").exists())
        self.assertTrue((target / "common" / "shared.txt").exists())


class PlaysetIdGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_playset_drops_unknown_ids_without_orphan_rows(self):
        db_path = self.root / "launcher-v2.sqlite"
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA)
        con.execute("insert into mods (id, steamId, name) values ('mod-a', '111', 'Alpha')")
        con.commit()
        con.close()

        db = LauncherDB(db_path)
        playset_id = db.create_playset("Ghost", ["mod-a", "ghost-id-1"])
        self.assertEqual(db.last_skipped, 1)
        con = sqlite3.connect(db_path)
        rows = list(con.execute("select modId from playsets_mods where playsetId = ?", (playset_id,)))
        con.close()
        self.assertEqual([row[0] for row in rows], ["mod-a"], "幽灵 id 不能留下孤儿行")


class DlcLoadSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_failure_does_not_fail_the_saved_change(self):
        api = bare_api(self.root, self.root / "data")
        with mock.patch.object(Api, "_sync_dlc_load", side_effect=OSError("disk full")):
            api._sync_dlc_load_safe("MOD 列表")  # must not raise
        self.assertIn("dlc_load.json 写入失败", api.last_warning)
        self.assertIn("MOD 列表", api.last_warning)


class UpdaterManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "update-source.json").write_text(
            json.dumps({"url": "https://example.invalid/update.json"}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_message_is_not_rewritten_as_a_network_error(self):
        picked = updater._best_error_message([ValueError(updater.MANIFEST_JSON_MESSAGE)])
        self.assertEqual(picked, updater.MANIFEST_JSON_MESSAGE)
        self.assertNotEqual(picked, net.DNS_MESSAGE)

    def test_broken_manifest_is_marked_as_a_manifest_problem(self):
        with mock.patch.object(updater, "_fetch_json",
                               side_effect=ValueError(updater.MANIFEST_JSON_MESSAGE)):
            result = updater.check(self.root, "0.9.64")
        self.assertTrue(result["ok"])
        self.assertTrue(result["offline"])
        self.assertFalse(result["available"])
        self.assertTrue(result.get("manifest_error"))
        self.assertIn("更新清单", result["message"])
        self.assertIn("不是你的网络", result["message"])

    def test_offline_stays_a_network_message(self):
        with mock.patch.object(updater, "_fetch_json", side_effect=net.NetworkError("offline")):
            result = updater.check(self.root, "0.9.64")
        self.assertTrue(result["offline"])
        self.assertFalse(result.get("manifest_error"))
        self.assertIn("无法连接", result["message"])


class PathSettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.game = self.root / "game"
        self.mod_dir = self.docs / "mod"
        for folder in (self.docs, self.game, self.mod_dir):
            folder.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_mod_path_accepts_a_real_mod_folder(self):
        api = bare_api(self.mod_dir, self.root / "data")
        api.catalog.game = str(self.game)
        api.catalog.docs = str(self.docs)
        api._save_path_settings = lambda: None
        with mock.patch.object(Api, "bootstrap", return_value={"ok": True}):
            self.assertTrue(api.set_mod_path({"path": str(self.mod_dir)})["ok"])

    def test_set_mod_path_refuses_the_game_install_folder(self):
        api = bare_api(self.mod_dir, self.root / "data")
        api.catalog.game = str(self.game)
        api.catalog.docs = str(self.docs)
        api._save_path_settings = lambda: None
        with self.assertRaises(ValueError) as ctx:
            api.set_mod_path({"path": str(self.game)})
        self.assertIn("不能当成 MOD 目录", str(ctx.exception))


class ConcurrencyAndRegistryTest(unittest.TestCase):
    """Round-3: two clicks at once must not lose a pack or hide a merge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_name_imports_get_unique_folders(self):
        api = bare_api(self.mod_dir, self.root / "data")
        payload = {"data": zip_payload("RacePack")}
        results = []

        def run():
            results.append(api.import_local_mod(payload))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(Path(item["path"]).name for item in results),
                         ["RacePack", "RacePack-2"])

    def test_merge_reports_when_the_registry_cannot_be_written(self):
        data = self.root / "data"
        data.mkdir()
        (data / "merged-packs.json").mkdir()  # a folder blocks the JSON write
        pack = self.root / "pack"
        pack.mkdir()
        (pack / "descriptor.mod").write_text('name="Pack"\n', encoding="utf-8")
        (pack / "common.txt").write_text("x", encoding="utf-8")
        api = bare_api(self.mod_dir, data, mods=[{"id": "m1", "name": "Pack",
                                                  "enabled": True, "dir": str(pack)}])
        result = api.merge_enabled_mods({"name": "Merge"})
        self.assertTrue(result["ok"])
        self.assertIn("登记表写入失败", result["warning"])
        self.assertTrue((self.mod_dir / "Merge.zip").is_file())
        self.assertTrue((self.mod_dir / "Merge.mod").is_file())


class DocsExplicitPassThroughTest(unittest.TestCase):
    """`docs_explicit` must survive a useless --docs path.

    Regression guard: an earlier fix derived the sandbox mirror from
    ``paths.detect()["docs_source"] == "override"``, which is always true once
    run.py passes any docs folder -- so the mirror was never created and the
    write points fell back onto the real game library.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"  # deliberately never created
        self.game = self.root / "game"
        self.data = self.root / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self):
        return Catalog(docs=str(self.docs), game=str(self.game),
                       db_path=str(self.root / "launcher-v2.sqlite"), sandbox=True)

    def test_unusable_explicit_docs_still_gets_the_sandbox_mirror(self):
        # The merged semantics: --docs only counts as "use this library" when
        # paths.detect() accepted it (it needs launcher-v2.sqlite inside).
        api = Api(self.catalog(), self.data, sandbox=True, docs_explicit=True)
        self.assertIs(api.docs_explicit, True)
        self.assertEqual(api.catalog.vision()["docs_source"], "auto_fallback")
        mirror = (self.data / "sandbox" / "mod").resolve()
        self.assertTrue(mirror.is_dir())
        self.assertEqual(Path(api.write_mod_root()).resolve(), mirror)
        self.assertIn("sandbox", str(api.write_mod_root()).lower())

    def test_accepted_explicit_docs_gets_no_mirror(self):
        self.docs.mkdir()
        (self.docs / "launcher-v2.sqlite").write_bytes(b"")
        api = Api(self.catalog(), self.data, sandbox=True, docs_explicit=True)
        self.assertEqual(api.catalog.vision()["docs_source"], "override")
        self.assertFalse((self.data / "sandbox" / "mod").exists())
        self.assertEqual(Path(api.write_mod_root()).resolve(), (self.docs / "mod").resolve())

    def test_explicit_docs_stays_true_even_when_detect_cannot_use_it(self):
        api = Api(self.catalog(), self.data, sandbox=True, docs_explicit=True)
        vision = api.catalog.vision()
        self.assertNotEqual(vision["docs"], str(self.docs))  # detect fell back
        self.assertIs(api.docs_explicit, True)

    def test_missing_docs_flag_still_creates_the_mirror(self):
        api = Api(self.catalog(), self.data, sandbox=True)
        self.assertIs(api.docs_explicit, False)
        mirror = (self.data / "sandbox" / "mod").resolve()
        self.assertTrue(mirror.is_dir())
        self.assertEqual(Path(api.write_mod_root()).resolve(), mirror)


class WriteFailureMessageTest(unittest.TestCase):
    """Raw OSErrors from a broken MOD folder must reach users as Chinese text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_reports_a_mod_folder_that_cannot_be_created(self):
        # A sandbox mirror replaced by a file used to raise FileExistsError,
        # which reached the user as "内部错误".
        data = self.root / "data"
        data.mkdir()
        broken = self.root / "mod-as-file"
        broken.write_text("not a folder", encoding="utf-8")
        pack = self.root / "pack"
        pack.mkdir()
        (pack / "descriptor.mod").write_text('name="Pack"\n', encoding="utf-8")
        (pack / "common.txt").write_text("x", encoding="utf-8")
        api = bare_api(broken, data, mods=[{"id": "m1", "name": "Pack",
                                           "enabled": True, "dir": str(pack)}])
        with self.assertRaises(ValueError) as caught:
            api.merge_enabled_mods({"name": "Merge"})
        self.assertIn("无法创建 MOD 目录", str(caught.exception))

    def test_restore_of_a_rotated_away_backup_says_so(self):
        data = self.root / "data"
        (data / "backups").mkdir(parents=True)
        api = bare_api(self.mod_dir, data)
        with self.assertRaises(ValueError) as caught:
            api.restore_backup({"name": "launcher-v2-19990101-000000.sqlite"})
        self.assertIn("备份文件不存在", str(caught.exception))
        self.assertIn("轮转", str(caught.exception))


class RoundSixTest(unittest.TestCase):
    """Round-6 findings: registry prune crash, stale scan cache, lost enabled flags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_merged_packs_prunes_a_missing_pack_without_crashing(self):
        gone = self.mod_dir / "Gone.zip"
        registry = self.data / "merged-packs.json"
        registry.write_text(json.dumps([{"name": "Gone", "path": str(gone)}], ensure_ascii=False),
                            encoding="utf-8")
        api = bare_api(self.mod_dir, self.data)
        api.db = mock.Mock()
        api.db.exists.return_value = False
        payload = api.list_merged_packs({})
        self.assertEqual(payload["packs"], [])
        self.assertEqual(json.loads(registry.read_text(encoding="utf-8")), [])

    def test_import_local_drops_the_scan_cache(self):
        api = bare_api(self.mod_dir, self.data)
        calls = []
        api.catalog.invalidate = lambda: calls.append(1)
        api.import_local_mod({"name": "Probe.zip", "data": zip_payload("Probe")})
        self.assertTrue(calls, "导入后必须让目录扫描缓存失效，否则新包 20 秒内看不见")
        self.assertTrue((self.mod_dir / "Probe.mod").is_file())

    def test_manual_drop_in_is_labelled_real_not_sandbox(self):
        catalog = Catalog(docs=self.root, game=None, db_path=self.root / "launcher-v2.sqlite", sandbox=True)
        dropin = self.mod_dir / "dropin"
        dropin.mkdir()
        (dropin / "descriptor.mod").write_text('name="DropIn"\n', encoding="utf-8")
        (self.mod_dir / "dropin.mod").write_text('name="DropIn"\npath="mod/dropin"\n', encoding="utf-8")
        catalog.extra_mod_dirs = [(self.mod_dir, "real")]
        items = catalog._describe_extra_dir(self.mod_dir, catalog.vision(), set(), side="real")
        self.assertTrue(items)
        self.assertEqual(items[0]["side"], "real")
        self.assertFalse(items[0]["sandbox_only"])
        self.assertEqual(items[0]["source"], "local")


class RecordingDb:
    """Minimal launcher-db stand-in that records what the API asked for."""

    def __init__(self):
        self.created = []
        self.orders = []
        self.active = None

    def create_playset(self, title, ids):
        self.created.append((title, list(ids)))
        return "new-playset"

    def save_order(self, playset_id, entries):
        self.orders.append((playset_id, [dict(entry) for entry in entries]))
        return {"written": len(entries)}

    def set_active(self, playset_id):
        self.active = playset_id


class MergeGuardTest(unittest.TestCase):
    """Guards for lines that pass locally but a content-anchored merge can drop.

    A merge tree once lost the `ids.append(mod_id)` line inside import_playset:
    the module still imported, the UI still worked, and every import silently
    reported "没有匹配到已安装的 MOD" because `ids` stayed empty.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _api(self, mods, available=()):
        api = bare_api(self.mod_dir, self.data, mods=mods, available=available)
        api.db = RecordingDb()
        api._sync_dlc_load_safe = lambda reason: None
        api._write_and_refresh = lambda action: {"ok": True, "created": action()}
        return api

    def test_import_playset_passes_matched_ids_to_create_playset(self):
        api = self._api([
            {"id": "m-a", "name": "QA Mod A", "steam_id": "111"},
            {"id": "m-b", "name": "QA Mod B", "steam_id": "222"},
        ])
        api.import_playset({"data": {
            "name": "导入集",
            "mods": [{"displayName": "QA Mod A", "enabled": True},
                     {"displayName": "QA Mod B", "enabled": True}],
        }})
        self.assertEqual(len(api.db.created), 1, "create_playset 必须被调用一次")
        _title, ids = api.db.created[0]
        self.assertTrue(ids, "匹配到的 mod_id 必须传给 create_playset，空列表会让任何导出都失败")
        self.assertEqual(ids, ["m-a", "m-b"])

    def test_import_playset_keeps_enabled_false(self):
        api = self._api([
            {"id": "m-a", "name": "QA Mod A", "steam_id": "111"},
            {"id": "m-b", "name": "QA Mod B", "steam_id": "222"},
        ])
        api.import_playset({"data": {
            "name": "导入集",
            "mods": [{"displayName": "QA Mod A", "enabled": True},
                     {"displayName": "QA Mod B", "enabled": False}],
        }})
        self.assertTrue(api.db.orders, "导出里的 enabled:false 必须回写，不能靠 create_playset 全启用")
        entries = api.db.orders[0][1]
        self.assertEqual({entry["mod_id"]: entry["enabled"] for entry in entries},
                         {"m-a": True, "m-b": False})

    def test_import_playset_refuses_when_nothing_matches(self):
        api = self._api([{"id": "m-a", "name": "QA Mod A"}])
        with self.assertRaises(ValueError) as ctx:
            api.import_playset({"data": {"name": "x", "mods": [{"displayName": "No Such Mod"}]}})
        self.assertIn("没有匹配到已安装的 MOD", str(ctx.exception))
        self.assertEqual(api.db.created, [], "没有匹配时不许建空播放集")

    def test_scan_labels_an_extra_dir_with_the_full_side_triplet(self):
        mirror = self.root / "mirror"
        packdir = mirror / "probe"
        packdir.mkdir(parents=True)
        (packdir / "descriptor.mod").write_text('name="Mirror Probe"\n', encoding="utf-8")
        (mirror / "probe.mod").write_text('name="Mirror Probe"\npath="mod/probe"\n', encoding="utf-8")
        catalog = Catalog(docs=self.root, game=None, db_path=self.root / "launcher-v2.sqlite",
                          sandbox=True)
        # The tuple form is what Api._register_scan_sources writes; a bare path
        # must keep defaulting to "sandbox" so old callers do not break.
        catalog.extra_mod_dirs = [(mirror, "sandbox")]
        probe = [item for item in catalog.scan(force=True)["available"]
                 if item.get("name") == "Mirror Probe"]
        self.assertTrue(probe, "附加扫描源里的包必须出现在 available 里")
        item = probe[0]
        self.assertEqual(item["side"], "sandbox")
        self.assertTrue(item["sandbox_only"])
        self.assertEqual(item["source"], "sandbox")
        self.assertTrue(item["id"].startswith("sandbox:"), item["id"])


    def test_apply_save_matches_installed_mods_and_reports_missing(self):
        """The save-rebuild path has its own `ids.append`; dropping it silently
        turns "rebuild the playset from this save" into "no mods installed"."""
        api = self._api([
            {"id": "m-a", "name": "QA Mod A"},
            {"id": "m-b", "name": "QA Mod B"},
        ])
        with mock.patch("smm.api.save_service.read_meta",
                        return_value={"name": "Ironman", "mods": ["QA Mod A", "Ghost Mod"]}):
            result = api.apply_save({"path": "slot.sav"})
        self.assertTrue(api.db.created, "命中一个 MOD 就必须建播放集")
        self.assertEqual(api.db.created[0][1], ["m-a"])
        summary = result["apply_summary"]
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["missing"], ["Ghost Mod"], "未安装的 MOD 必须进 missing 列表")

    def test_apply_save_passes_every_matched_id_and_dedupes(self):
        api = self._api([
            {"id": "m-a", "name": "QA Mod A"},
            {"id": "m-b", "name": "QA Mod B"},
        ])
        with mock.patch("smm.api.save_service.read_meta",
                        return_value={"name": "Ironman",
                                      "mods": ["QA Mod A", "QA Mod B", "qa mod b"]}):
            result = api.apply_save({"path": "slot.sav"})
        self.assertEqual(len(api.db.created), 1)
        self.assertEqual(api.db.created[0][1], ["m-a", "m-b"], "大小写重复项必须去重后原序保留")
        self.assertEqual(result["apply_summary"]["missing"], [])


class RoundNineTest(unittest.TestCase):
    """Round-9 findings: pack delete left registry files / wiped unrelated rows,
    and one data dir held every game's backups without telling them apart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod_dir = self.root / "mod"
        self.mod_dir.mkdir()
        self.data = self.root / "data"
        (self.data / "backups").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _fake_recycle(paths):
        moved = []
        for item in paths:
            item = Path(item)
            if item.exists():
                item.unlink()
                moved.append(item)
        return moved, []

    def _pack(self, name="Pack", pointer=None):
        archive = self.mod_dir / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("descriptor.mod", f'name="{name}"\n')
        registry = self.mod_dir / f"{name}.mod"
        target = pointer or f"mod/{name}.zip"
        registry.write_text(f'name="{name}"\npath="{target}"\n', encoding="utf-8")
        return archive, registry

    def test_delete_without_mod_file_removes_its_registry_file(self):
        # the .mod points somewhere else on purpose: only the registry row can
        # tell us this file belongs to the pack being deleted.
        archive, registry = self._pack(pointer="mod/Other.zip")
        (self.data / "merged-packs.json").write_text(
            json.dumps([{"name": "Pack", "path": str(archive), "mod_file": str(registry)}]),
            encoding="utf-8")
        api = bare_api(self.mod_dir, self.data)
        api.db = mock.Mock()
        api.db.exists.return_value = False
        with mock.patch.object(Api, "_recycle", staticmethod(self._fake_recycle)):
            api.delete_merged_pack({"path": str(archive)})
        self.assertFalse(archive.exists())
        self.assertFalse(registry.exists(), "只传 path 时也必须删掉合并包自己的 .mod")
        self.assertEqual(json.loads((self.data / "merged-packs.json").read_text(encoding="utf-8")), [])

    def test_delete_without_mod_file_falls_back_to_a_matching_neighbour(self):
        archive, registry = self._pack("Neighbour")
        (self.data / "merged-packs.json").write_text("[]", encoding="utf-8")
        api = bare_api(self.mod_dir, self.data)
        api.db = mock.Mock()
        api.db.exists.return_value = False
        with mock.patch.object(Api, "_recycle", staticmethod(self._fake_recycle)):
            api.delete_merged_pack({"path": str(archive)})
        self.assertFalse(registry.exists(), ".mod 内容指向该 zip 时也应一起清掉")

    def test_delete_does_not_match_rows_without_a_registry_id(self):
        archive, _ = self._pack()
        api = bare_api(self.mod_dir, self.data)
        api.db = mock.Mock()
        api.db.exists.return_value = True
        api.db.snapshot.return_value = {"mods": [
            {"id": "m1", "gameRegistryId": "", "dirPath": str(self.root / "elsewhere")},
            {"id": "m2", "gameRegistryId": None, "dirPath": ""},
        ]}
        self.assertEqual(api._merged_db_ids(archive, None), [],
                         "空 gameRegistryId 不能被 Path('') 拼成空名而全量命中")
        with mock.patch.object(Api, "_recycle", staticmethod(self._fake_recycle)):
            api.delete_merged_pack({"path": str(archive)})
        api.db.delete_mods.assert_not_called()

    def test_restore_refuses_a_backup_from_another_game(self):
        stellaris = self.data / "backups" / "launcher-v2-stellaris-20250101-000000.sqlite"
        stellaris.write_bytes(b"")
        api = bare_api(self.mod_dir, self.data)
        api.db = mock.Mock()
        api.game_key = "hoi4"
        with self.assertRaises(ValueError) as ctx:
            api._restore_backup({"name": stellaris.name})
        message = str(ctx.exception)
        self.assertIn("这个备份属于 群星", message)
        self.assertIn("不能在 钢铁雄心4 下恢复", message)
        api.db.backup.assert_not_called()

    def test_backup_name_carries_the_game_tag(self):
        api = bare_api(self.mod_dir, self.data)
        api.game_key = "stellaris"
        source = self.data / "launcher-v2.sqlite"
        connection = sqlite3.connect(str(source))
        try:
            connection.execute("create table mods (id text)")
            connection.execute("create table playsets (id text)")
            connection.commit()
        finally:
            connection.close()
        api.db = LauncherDB(source)
        created = api._db_backup()
        self.assertIn("-stellaris-", created.name)
        rows = api._backups()
        self.assertEqual(rows[0]["game"], "stellaris")
        self.assertEqual(api._backup_game("launcher-v2-20240101-000000.sqlite"), None)
        self.assertEqual(api._backup_game("launcher-v2-hoi4-20240101-000000.sqlite"), "hoi4")


if __name__ == "__main__":
    unittest.main()

