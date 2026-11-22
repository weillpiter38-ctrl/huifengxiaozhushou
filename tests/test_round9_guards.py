"""QA round 9 guards: stale scan cache across instances, credential clearing and
the legacy-backup warning.

The scan cache used to be a bare 20 s timer, so a second instance (or the Paradox
launcher itself) could change launcher-v2.sqlite and the first instance kept
serving the old MOD list for up to 20 s (QA OBS-9).  It now also compares the DB
file stamp (mtime_ns + size).
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from pathlib import Path
from types import SimpleNamespace

from smm import games
from smm.ai import AiSettings
from smm.catalog import Catalog
from smm.launcher_db import LauncherDB
from smm.api import Api


SCHEMA = """
CREATE TABLE mods (id text, steamId text, name text, displayName text, dirPath text,
                   version text, requiredVersion text, tags text, status text,
                   source text, timeUpdated integer, size integer,
                   thumbnailPath text, gameRegistryId text);
CREATE TABLE playsets (id text, name text, isActive integer, loadOrder text,
                       createdOn text, updatedOn text);
CREATE TABLE playsets_mods (playsetId text, modId text, enabled integer, position integer);
"""


def _make_mod(docs, index, enabled, position):
    folder = docs / "mod" / f"m{index}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "descriptor.mod").write_text(
        f'name="测试 MOD {index}"\nversion="1.0"\nsupported_version="v4.5.*"\n',
        encoding="utf-8")
    return {
        "id": f"m{index}",
        "steamId": str(1000 + index),
        "name": f"测试 MOD {index}",
        "displayName": f"测试 MOD {index}",
        "dirPath": str(folder),
        "version": "1.0",
        "requiredVersion": "*",
        "tags": "",
        "status": "enabled",
        "source": "steam",
        "timeUpdated": 0,
        "size": 0,
        "thumbnailPath": "",
        "gameRegistryId": "",
    }


def _insert_mod(db_path, row, playset_id, enabled, position):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO mods VALUES (:id,:steamId,:name,:displayName,:dirPath,:version,"
            ":requiredVersion,:tags,:status,:source,:timeUpdated,:size,:thumbnailPath,"
            ":gameRegistryId)", row)
        conn.execute("INSERT INTO playsets_mods VALUES (?,?,?,?)",
                     (playset_id, row["id"], 1 if enabled else 0, position))
        conn.commit()
    finally:
        conn.close()


def _build_docs(root, mod_count=1, enabled=True):
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    db_path = docs / "launcher-v2.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO playsets VALUES ('p1','测试播放集',1,'','','')")
        conn.commit()
    finally:
        conn.close()
    for index in range(1, mod_count + 1):
        _insert_mod(db_path, _make_mod(docs, index, enabled, index - 1), "p1", enabled, index - 1)
    return docs, db_path


class CatalogCacheStampTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa9-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.docs, self.db_path = _build_docs(self.root, mod_count=1)
        self.catalog = Catalog(docs=str(self.docs), game=str(self.root / "game"),
                               game_key="stellaris")

    def test_untouched_database_keeps_serving_the_cache(self):
        first = self.catalog.scan()
        second = self.catalog.scan()
        self.assertIs(first, second)
        self.assertEqual(len(second["mods"]), 1)

    def test_external_database_change_is_visible_without_waiting_for_the_ttl(self):
        first = self.catalog.scan()
        self.assertEqual(len(first["mods"]), 1)

        # Another instance / the Paradox launcher writes the same DB.
        _insert_mod(self.db_path, _make_mod(self.docs, 2, True, 1), "p1", True, 1)

        second = self.catalog.scan()
        self.assertEqual(len(second["mods"]), 2)
        self.assertTrue(second["scanned_at"] >= first["scanned_at"])

    def test_in_place_edit_of_the_same_size_is_detected(self):
        first = self.catalog.scan()
        self.assertEqual(len(first["mods"]), 1)

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("UPDATE playsets_mods SET enabled = 0 WHERE modId = 'm1'")
            conn.commit()
        finally:
            conn.close()
        # Make sure the stamp really moved on this filesystem.
        os.utime(self.db_path, None)

        second = self.catalog.scan()
        self.assertFalse(second["mods"][0]["enabled"])

    def test_missing_database_falls_back_to_the_plain_ttl(self):
        self.catalog.scan()
        self.db_path.unlink()
        # No exception, cache is still usable (the DB path is gone, nothing to compare).
        self.assertIsInstance(self.catalog.scan(), dict)


class AiClearTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa9-ai-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.settings = AiSettings(self.root)
        self.settings.save(api_key="sk-test", steam_cookie="sessionid=abc")

    def test_clear_removes_api_key_and_steam_cookie(self):
        payload = self.settings.clear()
        self.assertEqual(payload.get("message"), "已清除 API Key 与 Steam Cookie")
        raw = json.loads((self.root / "ai-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(raw.get("api_key"), "")
        self.assertEqual(raw.get("steam_cookie"), "")
        reloaded = AiSettings(self.root)
        self.assertEqual(reloaded.api_key, "")
        self.assertEqual(reloaded.steam_cookie, "")


class RestoreLegacyBackupWarningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa9-bk-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.live = self.root / "launcher-v2.sqlite"
        conn = sqlite3.connect(str(self.live))
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

        self.api = object.__new__(Api)
        self.api.data_dir = self.root
        self.api.game_key = "stellaris"
        self.api.read_only = False
        self.api.last_warning = ""
        self.api.db = SimpleNamespace(path=str(self.live))

        def _write(action):
            # Mirrors Api._write_and_refresh: clear the warning first, run the
            # action, surface whatever the action left in last_warning.
            self.api.last_warning = ""
            result = action()
            payload = {"written": result}
            if self.api.last_warning:
                payload["warning"] = self.api.last_warning
            return payload

        self.api._write_and_refresh = _write
        # The merged restore re-syncs dlc_load.json after rolling back (t4's
        # decision); this fixture has no catalog, so stub the sync out.
        self.api._sync_dlc_load_safe = lambda *a, **k: None

    def _backup(self, name, meta=None):
        source = self.backups / name
        shutil.copy2(self.live, source)
        if meta is not None:
            (self.backups / (name + ".json")).write_text(
                json.dumps(meta), encoding="utf-8")
        return source

    def test_legacy_backup_without_game_tag_warns_but_restores(self):
        self._backup("launcher-v2-20250101-000000.sqlite")
        result = self.api.restore_backup({"name": "launcher-v2-20250101-000000.sqlite"})
        self.assertEqual(result["written"]["restored"], "launcher-v2-20250101-000000.sqlite")
        self.assertEqual(
            result.get("warning"),
            "这是旧格式备份（未标游戏），已按当前游戏恢复，请确认 MOD 列表是否正确。")

    def test_tagged_backup_restores_without_warning(self):
        self._backup("launcher-v2-20250101-000001.sqlite", {"game": "stellaris", "version": "2.0.5"})
        result = self.api.restore_backup({"name": "launcher-v2-20250101-000001.sqlite"})
        self.assertNotIn("warning", result)

    def test_other_game_backup_is_still_rejected(self):
        self._backup("launcher-v2-20250101-000002.sqlite", {"game": "hoi4"})
        with self.assertRaises(ValueError) as ctx:
            self.api.restore_backup({"name": "launcher-v2-20250101-000002.sqlite"})
        self.assertIn(games.get("stellaris")["label"], str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
