"""On-demand conflict preview: the scan must never read file bodies.

A scan ships only where each MOD's copy of a conflicting file lives. The code
preview is the separate ``conflict_diff`` action, asked for one path at a time.
These fixtures are synthetic temp directories; no game files are touched.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from smm import catalog as catalog_mod
from smm.api import Api
from smm.catalog import Catalog, conflict_preview

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

HOLDER_KEYS = {"id", "name", "rank", "enabled", "file", "size"}


class _Fixture(unittest.TestCase):
    mod_total = 3
    bodies = ("alpha = 1\nbeta = 1\n",            # mod-000: one changed line
              "alpha = 1\nbeta = 1\ngamma = 1\n",  # mod-001: one extra line
              "alpha = 1\nbeta = 2\n")            # mod-002: winner

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
        con.execute("insert into playsets (id, name, isActive, loadOrder, createdOn) "
                    "values ('ps-1', 'QA', 1, 'custom', 1)")
        for index in range(self.mod_total):
            mod_id = f"mod-{index:03d}"
            folder = self.mod_dir / mod_id
            (folder / "common").mkdir(parents=True, exist_ok=True)
            (folder / "descriptor.mod").write_text(f'name="Mod {index}"\nsupported_version="v4.5.*"\n',
                                                   encoding="utf-8")
            (folder / "common" / "unique.txt").write_text("x", encoding="utf-8")
            (folder / "common" / "shared.txt").write_text(self.bodies[index], encoding="utf-8")
            con.execute(
                "insert into mods (id, steamId, name, displayName, dirPath, version, requiredVersion, tags, "
                "status, source, timeUpdated, size, thumbnailPath, gameRegistryId) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mod_id, str(1000 + index), f"Mod {index}", f"Mod {index}", str(folder), "1.0", "v4.5.*",
                 "[]", "ready_to_play", "local", 0, 0, None, None))
            con.execute("insert into playsets_mods values ('ps-1', ?, 1, ?)", (mod_id, index))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self):
        return Catalog(docs=str(self.docs), game=str(self.game), db_path=str(self.db_path))


class ScanStaysIoFreeTest(_Fixture):
    def test_holder_rows_ship_no_file_content(self):
        payload = self.catalog().scan(force=True)
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared.txt")
        self.assertEqual(len(entry["holders"]), 3)
        for holder in entry["holders"]:
            self.assertEqual(set(holder), HOLDER_KEYS, f"scan shipped extra holder keys: {sorted(holder)}")
        self.assertEqual(entry["winner"]["id"], "mod-002")

    def test_scan_never_opens_a_mod_file(self):
        original = catalog_mod._read_lines

        def boom(path):
            raise AssertionError(f"scan must not read MOD files: {path}")

        catalog_mod._read_lines = boom
        try:
            payload = self.catalog().scan(force=True)
        finally:
            catalog_mod._read_lines = original
        self.assertTrue(payload["conflicts"], "fixture must produce a conflict")


class ConflictPreviewTest(_Fixture):
    def test_changed_lines_are_reported_per_holder(self):
        catalog = self.catalog()
        result = conflict_preview(catalog, "common/shared.txt")
        rows = {holder["id"]: holder for holder in result["holders"]}
        self.assertEqual(result["winner"]["id"], "mod-002")

        loser = rows["mod-000"]
        self.assertFalse(loser["identical"])
        self.assertEqual([row["op"] for row in loser["diff"]], ["change"])
        self.assertEqual(loser["diff"][0]["line"], 2)
        self.assertIn("beta = 1", loser["diff"][0]["text"])

        extra = rows["mod-001"]
        self.assertEqual([row["op"] for row in extra["diff"]], ["change", "change"])
        self.assertEqual([row["line"] for row in extra["diff"]], [2, 3])
        self.assertIn("gamma", extra["diff"][1]["text"])

        self.assertTrue(rows["mod-002"]["is_winner"])
        self.assertEqual(rows["mod-002"]["diff"], [], "the winner needs no diff against itself")
        self.assertEqual(result["identical_count"], 0)

    def test_identical_copies_are_flagged_instead_of_diffed(self):
        for mod_id in ("mod-000", "mod-001", "mod-002"):
            (self.mod_dir / mod_id / "common" / "shared.txt").write_text("same = 1\n", encoding="utf-8")
        catalog = self.catalog()
        result = conflict_preview(catalog, "common/shared.txt")
        self.assertEqual(result["identical_count"], 2, "both non-winners ship a byte-identical copy")
        for holder in result["holders"]:
            self.assertEqual(holder["diff"], [])
            if not holder["is_winner"]:
                self.assertTrue(holder["identical"])

    def test_preview_rows_stay_bounded(self):
        big = "\n".join(f"line{i} = {i}" for i in range(400)) + "\n"
        (self.mod_dir / "mod-000" / "common" / "shared.txt").write_text(big, encoding="utf-8")
        result = conflict_preview(self.catalog(), "common/shared.txt")
        holder = next(item for item in result["holders"] if item["id"] == "mod-000")
        self.assertLessEqual(len(holder["diff"]), catalog_mod.CONFLICT_DIFF_LINES)

    def test_mod_ids_narrows_the_answer(self):
        result = conflict_preview(self.catalog(), "common/shared.txt", ["mod-000", "mod-002"])
        self.assertEqual([holder["id"] for holder in result["holders"]], ["mod-000", "mod-002"])

    def test_preview_is_fast_enough_for_a_click(self):
        catalog = self.catalog()
        catalog.scan(force=True)
        started = time.perf_counter()
        conflict_preview(catalog, "common/shared.txt")
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertLess(elapsed_ms, 200, f"one preview took {elapsed_ms:.1f} ms")


class ConflictPreviewGuardTest(_Fixture):
    def test_empty_path_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            conflict_preview(self.catalog(), "   ")
        self.assertIn("缺少文件路径", str(caught.exception))

    def test_path_traversal_is_refused(self):
        for bad in ("../secrets.txt", "common/../../x", "C:/Windows/win.ini", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=bad) as caught:
                conflict_preview(self.catalog(), bad)
            self.assertIn("不合法", str(caught.exception), bad)

    def test_unknown_path_is_reported_not_crashed(self):
        with self.assertRaises(ValueError) as caught:
            conflict_preview(self.catalog(), "common/nope.txt")
        self.assertIn("已经不存在", str(caught.exception))


class ConflictDiffActionTest(_Fixture):
    def test_dispatch_routes_to_the_preview(self):
        catalog = self.catalog()
        api = object.__new__(Api)
        api.catalog = catalog
        api.read_only = False
        result = api.dispatch("conflict_diff", {"path": "common/shared.txt"})
        self.assertEqual(result["winner"]["id"], "mod-002")
        self.assertEqual(len(result["holders"]), 3)

    def test_dispatch_rejects_a_non_list_mod_ids(self):
        api = object.__new__(Api)
        api.catalog = self.catalog()
        api.read_only = False
        with self.assertRaises(ValueError) as caught:
            api.dispatch("conflict_diff", {"path": "common/shared.txt", "mod_ids": "mod-000"})
        self.assertIn("mod_ids", str(caught.exception))


class RealLibraryPreviewLatencyTest(unittest.TestCase):
    """Manual check: QA_REAL_LIB=1 runs the hottest path of the real library."""

    @unittest.skipUnless(__import__("os").environ.get("QA_REAL_LIB"),
                         "set QA_REAL_LIB=1 to time the real Stellaris library")
    def test_hottest_real_path_answers_under_200ms(self):
        import os
        docs = Path(os.path.expanduser("~")) / "Documents" / "Paradox Interactive" / "Stellaris"
        catalog = Catalog(docs=str(docs), game=str(docs), db_path=str(docs / "launcher-v2.sqlite"))
        payload = catalog.scan(force=True)
        hottest = max(payload["conflicts"], key=lambda item: item["mod_count"])
        started = time.perf_counter()
        result = conflict_preview(catalog, hottest["path"])
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertTrue(result["holders"])
        self.assertLess(elapsed_ms, 200, f"{hottest['path']} took {elapsed_ms:.1f} ms")


if __name__ == "__main__":
    unittest.main()
