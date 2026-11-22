"""Irony-style file-conflict detail: holders, their real files, and the true winner.

The winner is decided by the order the game loads MODs, i.e. the order of
<docs>/dlc_load.json -> enabled_mods, NOT by the row order of the playset table.
These fixtures are synthetic temp directories; no game files are touched.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smm.catalog import (CONFLICT_DETAIL_MAX, CONFLICT_HOLDERS_MAX, CONFLICT_PATHS_MAX,
                         Catalog)

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

LIGHT_KEYS = {"path", "mods", "mod_count", "winner_id", "winner_name", "winner_rank", "i_win"}


class _Fixture(unittest.TestCase):
    mod_total = 2
    shared_files = 1

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
        con.execute("insert into playsets (id, name, isActive, loadOrder, createdOn) values ('ps-1', 'QA', 1, 'custom', 1)")
        for index in range(self.mod_total):
            mod_id = f"mod-{index:03d}"
            folder = self.mod_dir / mod_id
            (folder / "common").mkdir(parents=True, exist_ok=True)
            (folder / "descriptor.mod").write_text(f'name="Mod {index}"\nsupported_version="v4.5.*"\n',
                                                   encoding="utf-8")
            (folder / "common" / "unique.txt").write_text("x", encoding="utf-8")
            for shared in range(self.shared_files):
                (folder / "common" / f"shared_{shared}.txt").write_text("x" * (10 + index),
                                                                        encoding="utf-8")
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

    def write_dlc_load(self, registry_ids):
        (self.docs / "dlc_load.json").write_text(
            json.dumps({"enabled_mods": list(registry_ids), "disabled_dlcs": []}), encoding="utf-8")

    def catalog(self):
        return Catalog(docs=str(self.docs), game=str(self.game), db_path=str(self.db_path))

    def payload(self):
        return self.catalog().scan(force=True)


class LoadOrderTest(_Fixture):
    mod_total = 3
    shared_files = 1

    def test_dlc_load_order_decides_the_winner(self):
        # Playset rows say 000 < 001 < 002, the launcher file says the opposite.
        self.write_dlc_load(["mod/ugc_1002.mod", "mod/ugc_1001.mod", "mod/ugc_1000.mod"])
        payload = self.payload()
        order = payload["load_order"]
        self.assertEqual(order["source"], "dlc_load")
        self.assertTrue(order["mismatch"], "list order and load order disagree, must be reported")
        self.assertEqual([e["mod_id"] for e in order["entries"]], ["mod-002", "mod-001", "mod-000"])
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared_0.txt")
        self.assertEqual(entry["winner_basis"], "dlc_load")
        self.assertEqual(entry["winner"]["id"], "mod-000", "last loaded MOD wins the file")
        self.assertEqual(entry["winner"]["rank"], 2)

    def test_matching_order_is_not_flagged(self):
        self.write_dlc_load(["mod/ugc_1000.mod", "mod/ugc_1001.mod", "mod/ugc_1002.mod"])
        payload = self.payload()
        self.assertEqual(payload["load_order"]["source"], "dlc_load")
        self.assertFalse(payload["load_order"]["mismatch"])
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared_0.txt")
        self.assertEqual(entry["winner"]["id"], "mod-002")

    def test_missing_dlc_load_falls_back_to_position_order(self):
        payload = self.payload()
        self.assertEqual(payload["load_order"]["source"], "position")
        self.assertFalse(payload["load_order"]["mismatch"])
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared_0.txt")
        self.assertEqual(entry["winner"]["id"], "mod-002", "highest position wins")

    def test_broken_dlc_load_does_not_crash_the_scan(self):
        (self.docs / "dlc_load.json").write_text("{not json", encoding="utf-8")
        payload = self.payload()
        self.assertEqual(payload["load_order"]["source"], "position")
        self.assertTrue(payload["mods"])


class HolderDetailTest(_Fixture):
    mod_total = 3
    shared_files = 1

    def test_holders_list_every_mod_with_its_own_file_copy(self):
        payload = self.payload()
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared_0.txt")
        self.assertEqual(entry["holders_total"], 3)
        self.assertEqual(len(entry["holders"]), 3)
        self.assertFalse(entry["holders_clipped"])
        ranks = [holder["rank"] for holder in entry["holders"]]
        self.assertEqual(ranks, sorted(ranks), "holders must be listed in load order")
        for holder in entry["holders"]:
            self.assertTrue(holder["file"].endswith("shared_0.txt"))
            self.assertIn(holder["id"], holder["file"])
            self.assertGreater(holder["size"], 0)
            self.assertTrue(holder["enabled"])

    def test_per_mod_rows_stay_light_and_point_at_the_winner(self):
        payload = self.payload()
        for mod in payload["mods"]:
            for item in mod["conflict_paths"]:
                self.assertTrue(set(item) <= LIGHT_KEYS, f"per-MOD row grew: {sorted(item)}")
                self.assertNotIn("holders", item)
                self.assertEqual(item["i_win"], item["winner_id"] == mod["id"])
        mod2 = next(mod for mod in payload["mods"] if mod["id"] == "mod-002")
        self.assertTrue(mod2["conflict_paths"][0]["i_win"], "last position wins without dlc_load.json")
        mod0 = next(mod for mod in payload["mods"] if mod["id"] == "mod-000")
        self.assertFalse(mod0["conflict_paths"][0]["i_win"])


class HolderCapTest(_Fixture):
    mod_total = CONFLICT_HOLDERS_MAX + 4
    shared_files = 1

    def test_holder_list_is_capped_but_total_stays_true(self):
        payload = self.payload()
        entry = next(item for item in payload["conflicts"] if item["path"] == "common/shared_0.txt")
        self.assertEqual(entry["holders_total"], CONFLICT_HOLDERS_MAX + 4)
        self.assertEqual(len(entry["holders"]), CONFLICT_HOLDERS_MAX)
        self.assertTrue(entry["holders_clipped"])


class DetailPayloadBudgetTest(_Fixture):
    mod_total = 40
    shared_files = 30

    def test_detail_rows_do_not_explode_the_payload(self):
        payload = self.payload()
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertLess(len(blob), 6_000_000, "conflict detail must not multiply the payload")
        detailed = [item for item in payload["conflicts"][:CONFLICT_DETAIL_MAX] if item["holders"]]
        self.assertTrue(detailed)
        for entry in payload["conflicts"][:CONFLICT_DETAIL_MAX]:
            self.assertLessEqual(len(entry["holders"]), CONFLICT_HOLDERS_MAX)
        for mod in payload["mods"]:
            self.assertLessEqual(len(mod["conflict_paths"]), CONFLICT_PATHS_MAX)
            for item in mod["conflict_paths"]:
                self.assertNotIn("holders", item)


class AnalysisLoadOrderTest(unittest.TestCase):
    """Definition-level winners follow the real load order, then the filename order."""

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
        for index in range(3):
            mod_id = f"mod-{index:03d}"
            folder = self.mod_dir / mod_id
            (folder / "common" / "defines").mkdir(parents=True, exist_ok=True)
            (folder / "descriptor.mod").write_text(f'name="Mod {index}"\n', encoding="utf-8")
            (folder / "common" / "defines" / "qa.txt").write_text(
                f"NQ = {{\n\tVALUE = {index}\n}}\n", encoding="utf-8")
            con.execute(
                "insert into mods (id, steamId, name, displayName, dirPath, version, requiredVersion, tags, "
                "status, source, timeUpdated, size, thumbnailPath, gameRegistryId) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mod_id, str(1000 + index), f"Mod {index}", f"Mod {index}", str(folder), "1.0",
                 "v4.5.*", "[]", "ready_to_play", "local", 0, 0, None, None))
            con.execute("insert into playsets_mods values ('ps-1', ?, 1, ?)", (mod_id, index))
        con.commit()
        con.close()
        (self.docs / "dlc_load.json").write_text(json.dumps(
            {"enabled_mods": ["mod/ugc_1002.mod", "mod/ugc_1001.mod", "mod/ugc_1000.mod"],
             "disabled_dlcs": []}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def analyse(self):
        from smm import analysis
        return analysis.analyze(Catalog(docs=str(self.docs), game=str(self.game),
                                        db_path=str(self.db_path)))["analysis"]

    def test_definition_winner_uses_dlc_load_order_not_position(self):
        result = self.analyse()
        item = next(entry for entry in result["conflicts"] if entry["key"] == "NQ.VALUE")
        self.assertEqual(item["basis"], "dlc_load")
        self.assertEqual(item["winner_id"], "mod-000", "last MOD in dlc_load.json wins the definition")
        self.assertEqual(item["winner_rank"], 2)
        self.assertEqual(item["winner_file"], "common/defines/qa.txt")
        self.assertEqual([row["mod_id"] for row in item["mod_rows"]],
                         ["mod-002", "mod-001", "mod-000"], "rows listed in load order")

    def test_later_filename_wins_inside_one_mod(self):
        (self.mod_dir / "mod-000" / "common" / "defines" / "zz.txt").write_text(
            "NQ = {\n\tVALUE = 9\n}\n", encoding="utf-8")
        result = self.analyse()
        item = next(entry for entry in result["conflicts"] if entry["key"] == "NQ.VALUE")
        self.assertEqual(item["winner_id"], "mod-000")
        self.assertEqual(item["winner_file"], "common/defines/zz.txt",
                         "inside one MOD the later filename is the one that takes effect")
        rows = {row["mod_id"]: row for row in item["mod_rows"]}
        self.assertEqual(rows["mod-000"]["file"], "common/defines/zz.txt")

    def test_snippets_follow_load_order_and_carry_the_file(self):
        result = self.analyse()
        bucket = result["snippets"]["defines:NQ.VALUE"]
        self.assertEqual([item["mod_id"] for item in bucket], ["mod-002", "mod-001", "mod-000"])
        self.assertTrue(all(item["file"].endswith("qa.txt") for item in bucket))
        self.assertEqual(bucket[-1]["rank"], 2)
        self.assertIn("VALUE = 0", bucket[-1]["text"])

    def test_analysis_reports_which_order_it_used(self):
        result = self.analyse()
        self.assertEqual(result["load_order"]["source"], "dlc_load")
        self.assertTrue(result["load_order"]["mismatch"])


if __name__ == "__main__":
    unittest.main()
