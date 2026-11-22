# -*- coding: utf-8 -*-
"""Branch-named launcher databases.

The Paradox launcher names its database after the update branch it runs on:
stable builds write ``launcher-v2.sqlite``, the open beta branch writes
``launcher-v2-openbeta.sqlite``.  Requiring the stable name made the manager
report "没有找到 launcher-v2.sqlite" for every beta user until they renamed the
file by hand, so discovery now accepts any live branch file and never picks up
a timestamped backup.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import run as run_module
from smm import paths
from smm.api import Api
from smm.catalog import Catalog


def make_db(path, latest=1000, mods=3):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("create table playsets (id integer primary key, updatedOn integer)")
        con.execute("create table mods (id text primary key)")
        con.execute("insert into playsets (updatedOn) values (?)", (latest,))
        for index in range(mods):
            con.execute("insert into mods (id) values (?)", (f"m{index}",))
        con.commit()
    finally:
        con.close()
    return path


def age(path, stamp):
    os.utime(path, (stamp, stamp))
    return path


class BranchDatabaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_branch_database_is_found(self):
        docs = self.root / "Stellaris"
        db = make_db(docs / "launcher-v2-openbeta.sqlite")
        self.assertEqual(paths.launcher_db_path(docs), db)
        self.assertEqual(paths.launcher_db_name(docs), "launcher-v2-openbeta.sqlite")
        self.assertTrue(paths.is_launcher_db(db))

    def test_underscore_variant_is_found_too(self):
        docs = self.root / "Stellaris"
        db = make_db(docs / "launcher-v2_openbeta.sqlite")
        self.assertEqual(paths.launcher_db_path(docs), db)

    def test_backups_are_never_treated_as_live_data(self):
        docs = self.root / "Stellaris"
        docs.mkdir(parents=True)
        make_db(docs / "launcher-v2-20250101-000000.sqlite")
        make_db(docs / "launcher-v2-stellaris-20250101-000000.sqlite")
        make_db(docs / "launcher-v2-stellaris-20250101-000000-2.sqlite")
        self.assertEqual(paths.launcher_db_candidates(docs), [])
        self.assertIsNone(paths.launcher_db_path(docs))

    def test_live_file_wins_over_a_newer_backup(self):
        docs = self.root / "Stellaris"
        live = age(make_db(docs / "launcher-v2-openbeta.sqlite"), 1_000_000)
        backup = age(make_db(docs / "launcher-v2-20250101-000000.sqlite"), 2_000_000)
        self.assertGreater(backup.stat().st_mtime, live.stat().st_mtime)
        self.assertEqual(paths.launcher_db_path(docs), live)

    def test_newest_branch_file_wins(self):
        docs = self.root / "Stellaris"
        stable = age(make_db(docs / "launcher-v2.sqlite", latest=100), 1_000_000)
        beta = age(make_db(docs / "launcher-v2-openbeta.sqlite", latest=900), 2_000_000)
        self.assertEqual(paths.launcher_db_path(docs), beta)
        # Switching back to the stable branch rewrites the stable file: it must win again.
        age(stable, 3_000_000)
        self.assertEqual(paths.launcher_db_path(docs), stable)

    def test_companions_and_renamed_copies_are_ignored(self):
        docs = self.root / "Stellaris"
        docs.mkdir(parents=True)
        (docs / "launcher-v2-openbeta.sqlite-wal").write_bytes(b"x" * 600)
        (docs / "launcher-v2.sqlite.bak-20250101-000000").write_bytes(b"x" * 600)
        (docs / "launcher-v2_openbeta.sqlite.tmp-123").write_bytes(b"x" * 600)
        self.assertIsNone(paths.launcher_db_path(docs))

    def test_hand_made_copies_are_ignored(self):
        """`launcher-v2_backup.sqlite` is a user copy, not a branch build."""
        docs = self.root / "Stellaris"
        live = age(make_db(docs / "launcher-v2.sqlite"), 1_000_000)
        age(make_db(docs / "launcher-v2_backup.sqlite", mods=99), 5_000_000)
        age(make_db(docs / "launcher-v2-copy.sqlite", mods=99), 6_000_000)
        age(make_db(docs / "launcher-v2-old.sqlite", mods=99), 7_000_000)
        self.assertEqual(paths.launcher_db_path(docs), live)
        self.assertEqual(len(paths.launcher_db_candidates(docs)), 1)

    def test_a_corrupt_live_file_is_still_reported_as_the_database(self):
        """A half-copied file must surface as "database broken", not "missing"."""
        docs = self.root / "Stellaris"
        docs.mkdir(parents=True)
        broken = (docs / "launcher-v2-openbeta.sqlite")
        broken.write_bytes(b"not a database" * 100)
        self.assertEqual(paths.launcher_db_path(docs), broken)

    def test_a_readable_database_outranks_a_newer_corrupt_one(self):
        docs = self.root / "Stellaris"
        good = age(make_db(docs / "launcher-v2-openbeta.sqlite"), 1_000_000)
        junk = docs / "launcher-v2.sqlite"
        junk.write_bytes(b"garbage" * 100)
        age(junk, 2_000_000)
        self.assertEqual(paths.launcher_db_path(docs), good)

    def test_missing_folder_is_not_an_error(self):
        self.assertEqual(paths.launcher_db_candidates(self.root / "nope"), [])
        self.assertIsNone(paths.launcher_db_path(self.root / "nope"))


class DetectBranchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.docs = self.root / "Paradox Interactive" / "Stellaris"
        self.game = self.root / "game"
        self.game.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_override_folder_with_branch_database(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        info = paths.detect(str(self.docs), str(self.game), game_key="stellaris")
        self.assertEqual(info["docs_source"], "override")
        self.assertEqual(Path(info["db"]), db)
        self.assertEqual(info["db_name"], "launcher-v2-openbeta.sqlite")
        self.assertIn(str(db), info["db_candidates"])

    def test_override_may_be_the_database_file_itself(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        info = paths.detect(str(db), str(self.game), game_key="stellaris")
        self.assertEqual(info["docs_source"], "override")
        self.assertEqual(Path(info["docs"]), self.docs)
        self.assertEqual(Path(info["db"]), db)

    def test_catalog_vision_reports_the_branch_file(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        catalog = Catalog(docs=str(self.docs), game=str(self.game), db_path=None,
                          sandbox=False, workshop=None, game_key="stellaris")
        vision = catalog.vision()
        self.assertEqual(Path(vision["db"]), db)
        self.assertEqual(vision["db_name"], "launcher-v2-openbeta.sqlite")

    def test_sandbox_copy_uses_the_branch_database(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        data_dir = self.root / "data"
        target = run_module.prepare_sandbox(self.docs, data_dir, True)
        self.assertEqual(Path(target), data_dir / "sandbox" / "launcher-v2.sqlite")
        con = sqlite3.connect(target)
        try:
            self.assertEqual(con.execute("select count(*) from mods").fetchone()[0], 3)
        finally:
            con.close()
        self.assertTrue(db.is_file())

    def test_sandbox_error_names_the_branch_forms(self):
        self.docs.mkdir(parents=True)
        with self.assertRaises(SystemExit) as caught:
            run_module.prepare_sandbox(self.docs, self.root / "data", True)
        message = str(caught.exception)
        self.assertIn("launcher-v2-openbeta.sqlite", message)

    def test_set_docs_path_accepts_a_branch_file(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        api = object.__new__(Api)
        api.game_key = "stellaris"
        api.data_dir = self.root / "data"
        api.data_dir.mkdir(parents=True, exist_ok=True)
        api.catalog = Catalog(docs=None, game=str(self.game), db_path=None,
                              sandbox=False, workshop=None, game_key="stellaris")
        api.bootstrap = lambda: {"ok": True}
        api.set_docs_path({"path": str(db)})
        self.assertEqual(Path(api.catalog.docs), self.docs)
        self.assertEqual(Path(api.db.path), db)

    def test_cleanup_protects_the_live_branch_database(self):
        db = make_db(self.docs / "launcher-v2-openbeta.sqlite")
        api = object.__new__(Api)
        api.data_dir = self.root / "data"
        api.data_dir.mkdir(parents=True, exist_ok=True)
        api.catalog = Catalog(docs=str(self.docs), game=str(self.game), db_path=None,
                              sandbox=False, workshop=None, game_key="stellaris")
        with self.assertRaises(ValueError) as caught:
            api.manager_files_delete({"paths": [str(db)]})
        self.assertIn("正被管理器使用", str(caught.exception))
        self.assertTrue(db.is_file())


if __name__ == "__main__":
    unittest.main()
