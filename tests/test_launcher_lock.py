"""QA-T4 r3: a locked launcher DB must fail fast instead of stalling the request."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from smm import launcher_db


class LockedDatabaseTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "launcher-v2.sqlite"
        con = sqlite3.connect(str(self.db_path))
        con.execute("create table mods (id text primary key, name text)")
        con.execute("create table playsets (id text primary key, name text, isActive integer, "
                    "loadOrder text, createdOn integer, updatedOn integer)")
        con.execute("create table playsets_mods (playsetId text, modId text, enabled integer, position integer)")
        con.execute("insert into mods values ('m1', 'one')")
        con.commit()
        con.close()

    def tearDown(self):
        self._dir.cleanup()

    def test_locked_write_fails_within_budget(self):
        holder = sqlite3.connect(str(self.db_path), timeout=0.1, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            db = launcher_db.LauncherDB(self.db_path)
            started = time.perf_counter()
            with self.assertRaises(ValueError) as caught:
                db.save_order("ps1", [{"mod_id": "m1", "enabled": True}])
            elapsed = time.perf_counter() - started
            self.assertIn("正被占用", str(caught.exception))
            self.assertLess(elapsed, 3.5, f"locked write took {elapsed:.2f}s")
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_locked_snapshot_fails_fast(self):
        holder = sqlite3.connect(str(self.db_path), timeout=0.1, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            db = launcher_db.LauncherDB(self.db_path)
            started = time.perf_counter()
            try:
                db.snapshot()
            except ValueError as exc:
                self.assertIn("占用", str(exc))
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 3.5, f"locked snapshot took {elapsed:.2f}s")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
