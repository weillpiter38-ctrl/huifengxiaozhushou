"""Concurrency regressions: parallel backups and orders must never surface as 500.

Found by qa-t4 round-2: `LauncherDB.backup()` picked its file name with a
`Path.exists()` loop and pruned with `key=lambda p: p.stat().st_mtime`, so two
request threads in the same second either truncated each other's backup or
crashed with `FileNotFoundError: ...backups\\launcher-v2-<stamp>.sqlite`
(reported to the browser as `{"ok": false, "error": "内部错误"}`, 11/1600 calls).
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

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


class ConcurrentWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "launcher-v2.sqlite"
        self.backups = root / "backups"
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        self.ids = []
        for index in range(24):
            mod_id = f"mod-{index:02d}"
            self.ids.append(mod_id)
            con.execute("insert into mods (id, name, displayName) values (?, ?, ?)",
                        (mod_id, f"MOD {index}", f"MOD {index}"))
        con.execute("insert into playsets (id, name, isActive) values ('ps-1', 'Play', 1)")
        con.executemany("insert into playsets_mods (playsetId, modId, enabled, position) values (?, ?, 1, ?)",
                        [("ps-1", mod_id, pos) for pos, mod_id in enumerate(self.ids)])
        con.commit()
        con.close()
        self.db = LauncherDB(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_name_race_gives_distinct_valid_copies(self):
        errors = []
        results = []
        lock = threading.Lock()

        def worker():
            try:
                path = self.db.backup(self.backups, keep=40)
                with lock:
                    results.append(path)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len({path.name for path in results}), 8)
        for path in results:
            self.assertTrue(path.is_file(), path)
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
                self.assertEqual(con.execute("select count(*) from mods").fetchone()[0], 24)
            finally:
                con.close()

    def test_backup_leaves_no_temp_or_empty_placeholder(self):
        """VACUUM 走临时名 + os.replace：备份后不能残留 .tmp-* 或 0 字节占位文件。"""
        first = self.db.backup(self.backups, keep=40)
        second = self.db.backup(self.backups, keep=40)

        self.assertNotEqual(first.name, second.name)
        leftovers = sorted(p.name for p in self.backups.iterdir() if ".tmp-" in p.name)
        self.assertEqual(leftovers, [])
        empty = sorted(p.name for p in self.backups.iterdir() if p.is_file() and p.stat().st_size == 0)
        self.assertEqual(empty, [])
        for path in (first, second):
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
            finally:
                con.close()

    def test_backup_prune_keeps_limit(self):
        errors = []

        def worker():
            try:
                self.db.backup(self.backups, keep=3)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        kept = list(self.backups.glob("launcher-v2-*.sqlite"))
        self.assertLessEqual(len(kept), 3)
        self.assertGreaterEqual(len(kept), 1)

    def test_parallel_save_order_keeps_rows_consistent(self):
        errors = []

        def worker(index):
            order = [{"mod_id": mod_id, "enabled": (pos + index) % 2 == 0}
                     for pos, mod_id in enumerate(self.ids)]
            try:
                for _ in range(10):
                    self.db.save_order("ps-1", order)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute("select modId, enabled from playsets_mods where playsetId='ps-1'").fetchall()
            self.assertEqual(len(rows), len(self.ids))
            self.assertEqual(len({row[0] for row in rows}), len(self.ids))
            self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
