"""Regression tests for 0.9.65: multi-version caches, stale MOD cleanup, save resilience."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smm import paths
from smm.api import Api
from smm.catalog import Catalog
from smm.launcher_db import LauncherDB

BASE = Path(__file__).resolve().parent.parent

SCHEMA = """
create table mods (
  id text primary key, steamId text, name text, displayName text, dirPath text,
  archivePath text, version text, requiredVersion text, tags text, status text,
  source text, timeUpdated integer, size integer, thumbnailPath text, gameRegistryId text
);
create table playsets (
  id text primary key, name text, isActive boolean, loadOrder text,
  createdOn datetime, updatedOn datetime, isRemoved boolean default 0
);
create table playsets_mods (
  playsetId text not null, modId text not null, enabled boolean, position integer,
  foreign key(playsetId) references playsets(id) on delete cascade,
  foreign key(modId) references mods(id) on delete cascade
);
"""


class DocsVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_cache(self, name):
        folder = self.home / "Documents" / "Paradox Interactive" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "launcher-v2.sqlite").write_bytes(b"")
        return folder

    def test_docs_candidates_include_beta_variants(self):
        self._make_cache("Stellaris")
        self._make_cache("Stellaris Beta")
        self._make_cache("Stellaris_test")
        (self.home / "Documents" / "Paradox Interactive" / "Hearts of Iron IV").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)}):
            names = {item.name for item in paths.docs_candidates("stellaris")}
        self.assertEqual(names, {"Stellaris", "Stellaris Beta", "Stellaris_test"})

    def test_detect_follows_game_data_path_for_beta(self):
        beta = self._make_cache("Stellaris Beta")
        game = self.home / "SteamLibrary" / "steamapps" / "common" / "Stellaris"
        game.mkdir(parents=True)
        (game / "launcher-settings.json").write_text(json.dumps({
            "gameId": "stellaris",
            "gameDataPath": "%USER_DOCUMENTS%/Paradox Interactive/Stellaris Beta",
            "version": "Cygnus v4.6.0",
            "rawVersion": "v4.6.0",
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)}):
            info = paths.detect(game_override=game)
        self.assertEqual(Path(info["docs"]), beta)
        self.assertEqual(info["docs_source"], "game_data_path")
        self.assertIn(str(beta), info["docs_candidates"])


class StaleModTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        (self.docs / "mod").mkdir(parents=True)
        self.good = self.docs / "mod" / "Good"
        self.good.mkdir()
        (self.good / "descriptor.mod").write_text(
            'name="Good Mod"\nsupported_version="v4.5.*"\n', encoding="utf-8")
        self.db_path = self.docs / "launcher-v2.sqlite"
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        con.execute("insert into mods (id,name,dirPath,status,source) values (?,?,?,?,?)",
                    ("good", "Good Mod", str(self.good), "ready_to_play", "local"))
        con.execute("insert into mods (id,name,dirPath,status,source) values (?,?,?,?,?)",
                    ("gone", "Deleted Mod", str(self.docs / "mod" / "Deleted"),
                     "ready_to_play", "local"))
        con.execute("insert into mods (id,name,dirPath,status,source) values (?,?,?,?,?)",
                    ("unsub", "Unsubscribed", str(self.good), "unsubscribed", "steam"))
        con.execute("insert into playsets (id,name,isActive,createdOn,isRemoved) values ('ps','Main',1,0,0)")
        con.execute("insert into playsets (id,name,isActive,createdOn,isRemoved) values ('old','Old',0,0,1)")
        con.execute("insert into playsets_mods values ('ps','good',1,0)")
        con.execute("insert into playsets_mods values ('ps','gone',1,1)")
        con.execute("insert into playsets_mods values ('old','good',1,0)")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_hides_missing_and_unsubscribed_mods(self):
        catalog = Catalog(docs=str(self.docs), game=None, db_path=self.db_path,
                          sandbox=True, game_key="stellaris")
        payload = catalog.scan(force=True)
        self.assertEqual([item["id"] for item in payload["mods"]], ["good"])
        self.assertEqual(payload["stats"]["hidden_invalid"], 2)
        self.assertEqual([item["id"] for item in payload["available"]], [])
        self.assertEqual([p["id"] for p in payload["playsets"]], ["ps"])
        self.assertEqual(payload["playsets"][0]["mod_count"], 1)

    def test_snapshot_skips_removed_playsets(self):
        snapshot = LauncherDB(self.db_path).snapshot()
        self.assertEqual([item["id"] for item in snapshot["playsets"]], ["ps"])

    def test_save_order_skips_unknown_mod_ids(self):
        db = LauncherDB(self.db_path)
        written = db.save_order("ps", [
            {"mod_id": "good", "enabled": True},
            {"mod_id": "ghost", "enabled": True},
        ])
        self.assertEqual(written, 1)
        self.assertEqual(db.last_skipped, 1)
        con = sqlite3.connect(self.db_path)
        rows = con.execute("select modId from playsets_mods where playsetId='ps'").fetchall()
        con.close()
        self.assertEqual(rows, [("good",)])


class SaveResilienceTest(unittest.TestCase):
    def test_backup_failure_does_not_discard_the_save(self):
        class FailingDB:
            def backup(self, folder):
                raise OSError("disk full")

        class FakeCatalog:
            def refresh(self):
                return {"ok": True, "mods": [], "available": [], "playsets": [], "stats": {}}

        api = object.__new__(Api)
        api.read_only = False
        api.game_key = "stellaris"
        api.data_dir = Path(tempfile.mkdtemp(prefix="smm-save-"))
        api.catalog = FakeCatalog()
        api.db = FailingDB()
        api.last_backup = ""
        api.last_warning = ""
        api._decorate = lambda payload: dict(payload)
        api._guard_write = lambda: None

        result = api._write_and_refresh(lambda: 7)
        self.assertEqual(result["written"], 7)
        self.assertIn("备份失败", result["warning"])

    def test_path_length_limits(self):
        api = object.__new__(Api)
        api.game_key = "stellaris"
        with self.assertRaises(ValueError):
            api.set_game_path({"path": "C:/" + "x" * 4100})
        with self.assertRaises(ValueError):
            api.set_mod_path({"path": "C:/" + "x" * 4100})


class FrontendContractTest(unittest.TestCase):
    def test_path_input_is_long_enough(self):
        html = (BASE / "web" / "index.html").read_text(encoding="utf-8")
        js = (BASE / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="text-input" maxlength="1024"', html)
        self.assertIn("input.maxLength = maxLength", js)
        self.assertIn("1024,", js)

    def test_autosave_no_longer_fails_while_saving(self):
        js = (BASE / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("if (!state.dirty || state.saving) return false;", js)
        self.assertIn("if (autoSavePromise)", js)
        self.assertIn("runAutoSave", js)
        self.assertIn("AbortController", js)


if __name__ == "__main__":
    unittest.main()
