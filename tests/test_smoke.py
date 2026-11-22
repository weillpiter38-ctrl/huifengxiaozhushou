"""Offline smoke tests: no real game files are read or written."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smm import descriptor
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


class DescriptorTest(unittest.TestCase):
    def test_parse_and_compat(self):
        text = '''
        name="Test Mod"
        path="mod/test"
        supported_version="v4.5.*"
        dependencies={
            "UI Overhaul Dynamic"
            "More Events Mod"
        }
        tags={
            "Gameplay"
            "Balance"
        }
        '''
        parsed = descriptor.parse_text(text)
        self.assertEqual(parsed["name"], "Test Mod")
        self.assertEqual(parsed["supported_version"], "v4.5.*")
        self.assertEqual(parsed["dependencies"], ["UI Overhaul Dynamic", "More Events Mod"])
        self.assertEqual(parsed["tags"], ["Gameplay", "Balance"])
        self.assertEqual(descriptor.check_supported("v4.5.4", "v4.5.*"), "ok")
        self.assertEqual(descriptor.check_supported("v4.5.4", "v4.4.*"), "mismatch")
        self.assertEqual(descriptor.check_supported("v4.5.4", "v3.*.*"), "mismatch")
        self.assertEqual(descriptor.check_supported("v4.5.4", "*"), "ok")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.docs = root / "docs"
        self.game = root / "game"
        self.mod_dir = self.docs / "mod"
        for folder in (self.docs, self.game, self.mod_dir):
            folder.mkdir(parents=True, exist_ok=True)
        (self.game / "launcher-settings.json").write_text(
            json.dumps({"rawVersion": "v4.5.4", "version": "Cygnus v4.5.4"}), encoding="utf-8")

        self.db_path = self.docs / "launcher-v2.sqlite"
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        con.execute("insert into playsets (id, name, isActive, loadOrder, createdOn) values ('ps-1', '主播放集', 1, 'custom', 1)")
        mods = [
            ("mod-a", "111", "Alpha", "Alpha", "v4.5.*"),
            ("mod-b", "222", "Beta", "Beta", "v4.4.*"),
        ]
        for index, (mod_id, steam_id, name, display, supported) in enumerate(mods):
            folder = root / "mods" / mod_id
            (folder / "common").mkdir(parents=True, exist_ok=True)
            (folder / "common" / "shared.txt").write_text("x", encoding="utf-8")
            (folder / "descriptor.mod").write_text(
                f'name="{name}"\nsupported_version="{supported}"\nversion="1.0"\n', encoding="utf-8")
            con.execute(
                "insert into mods (id, steamId, name, displayName, dirPath, version, requiredVersion, tags, "
                "status, source, timeUpdated, size, thumbnailPath, gameRegistryId) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mod_id, steam_id, name, display, str(folder), "1.0", supported, '["Gameplay"]',
                 "ready_to_play", "steam", 0, 0, None, f"mod/ugc_{steam_id}.mod"))
            con.execute("insert into playsets_mods values ('ps-1', ?, 1, ?)", (mod_id, index))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_reports_conflicts_and_compat(self):
        catalog = Catalog(docs=str(self.docs), game=str(self.game), db_path=str(self.db_path))
        payload = catalog.scan()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stats"]["total"], 2)
        self.assertEqual(payload["stats"]["conflict_paths"], 1)
        self.assertEqual(payload["mods"][1]["compat"], "mismatch")
        self.assertEqual(payload["mods"][0]["conflict_count"], 1)
        self.assertEqual(payload["mods"][1]["conflict_paths"][0]["path"], "common/shared.txt")

    def test_write_order_and_backup(self):
        db = LauncherDB(self.db_path)
        backup = db.backup(self.docs / "backups")
        self.assertTrue(backup.is_file())
        written = db.save_order("ps-1", [{"mod_id": "mod-b", "enabled": False},
                                         {"mod_id": "mod-a", "enabled": True}])
        self.assertEqual(written, 2)
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "select modId, enabled, position from playsets_mods where playsetId='ps-1' order by position").fetchall()
        con.close()
        self.assertEqual(rows, [("mod-b", 0, 0), ("mod-a", 1, 1)])

    def test_create_and_delete_playset(self):
        db = LauncherDB(self.db_path)
        new_id = db.create_playset("第二套", ["mod-a"])
        self.assertTrue(new_id)
        db.set_active(new_id)
        con = sqlite3.connect(self.db_path)
        active = con.execute("select id from playsets where isActive = 1").fetchall()
        con.close()
        self.assertEqual(active, [(new_id,)])
        db.delete_playset(new_id)
        con = sqlite3.connect(self.db_path)
        count = con.execute("select count(*) from playsets").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)


class WorkshopAndSaveTest(unittest.TestCase):
    def test_parse_collection_id(self):
        from smm.workshop import parse_collection_id
        self.assertEqual(parse_collection_id("https://steamcommunity.com/sharedfiles/filedetails/?id=2473560875"), "2473560875")
        self.assertEqual(parse_collection_id("2473560875"), "2473560875")
        self.assertEqual(parse_collection_id("https://steamcommunity.com/sharedfiles/filedetails/?id=abc"), None)
        self.assertEqual(parse_collection_id(""), None)

    def test_strip_bbcode(self):
        from smm.workshop import _strip_bbcode
        raw = "[b]Title[/b]\\n[img]https://x/y.png[/img]\\n[url=https://a]link[/url] &amp; text"
        cleaned = _strip_bbcode(raw)
        self.assertNotIn("[b]", cleaned)
        self.assertNotIn("[img]", cleaned)
        self.assertIn("link", cleaned)
        self.assertIn("&", cleaned)

    def test_save_meta(self):
        import zipfile
        from smm import saves
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            folder = docs / "save games" / "empire"
            folder.mkdir(parents=True)
            target = folder / "test.sav"
            meta = '''version="Cygnus v4.5.4"
name="Test Empire"
date="2230.01.01"
required_dlcs=
{
    "Utopia"
}
mods=
{
    "Alpha"
    "Beta"
}
'''
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("meta", meta)
                zf.writestr("gamestate", "x")
            rows = saves.list_saves(str(docs))
            self.assertEqual(len(rows), 1)
            data = saves.read_meta(str(target), str(docs))
            self.assertEqual(data["name"], "Test Empire")
            self.assertEqual(data["mods"], ["Alpha", "Beta"])
            self.assertEqual(data["dlcs"], ["Utopia"])

    def test_save_path_guard(self):
        from smm import saves
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "save games").mkdir()
            outside = docs / "outside.sav"
            outside.write_bytes(b"x")
            with self.assertRaises(ValueError):
                saves.read_meta(str(outside), str(docs))


if __name__ == "__main__":
    unittest.main()

class SortingTest(unittest.TestCase):
    def test_prefix_and_dependency_order(self):
        from smm.sorting import sort_mods
        mods = [
            {"id": "late", "name": "! Late Patch", "tags": [], "dependencies": [], "display_name": ""},
            {"id": "top", "name": "~ Top Lib", "tags": [], "dependencies": [], "display_name": ""},
            {"id": "base", "name": "Base Gameplay", "tags": [], "dependencies": [], "display_name": ""},
            {"id": "addon", "name": "Addon", "tags": [], "dependencies": ["Base Gameplay"], "display_name": ""},
        ]
        ordered, _reasons = sort_mods(mods)
        names = [mod["name"] for mod in ordered]
        self.assertEqual(names[0], "~ Top Lib")
        self.assertEqual(names[-1], "! Late Patch")
        self.assertGreater(names.index("Addon"), names.index("Base Gameplay"))

    def test_stable_mode_keeps_order(self):
        from smm.sorting import sort_mods
        mods = [
            {"id": "a", "name": "Zeta", "tags": [], "dependencies": [], "display_name": ""},
            {"id": "b", "name": "Alpha", "tags": [], "dependencies": [], "display_name": ""},
        ]
        ordered, _reasons = sort_mods(mods, "stable")
        self.assertEqual([mod["name"] for mod in ordered], ["Zeta", "Alpha"])

class UserDataTest(unittest.TestCase):
    def test_notes_and_folders(self):
        from smm.userdata import UserData
        with tempfile.TemporaryDirectory() as tmp:
            data = UserData(tmp)
            data.note("mod-1", "需要补丁")
            folder = data.folder_create("框架")
            data.folder_assign("mod-1", folder["id"])
            again = UserData(tmp)
            self.assertEqual(again.notes.get("mod-1"), "需要补丁")
            self.assertEqual(again.assignments.get("mod-1"), folder["id"])
            self.assertEqual(again.folders[0]["name"], "框架")
            data.folder_delete(folder["id"])
            self.assertEqual(data.assignments.get("mod-1"), None)

