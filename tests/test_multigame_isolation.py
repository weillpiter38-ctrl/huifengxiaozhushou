"""Regression tests for QA round 1: sandbox copy reuse, backup game tagging,
and non-destructive save_order validation."""
import shutil
import tempfile
import unittest
from pathlib import Path

from smm.api import Api


class _FakeCatalog:
    def scan(self, force=False):
        return {"mods": [], "available": [],
                "playsets": [{"id": "p1", "name": "keep me", "mods": 0}],
                "active_playset": "p1"}


class _SpyDB:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"playsets": [{"id": "p1"}], "entries": []}

    def save_order(self, playset_id, entries):
        self.calls.append((playset_id, entries))
        return len(entries)


class SandboxCopyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _api(self):
        api = object.__new__(Api)
        api.sandbox = True
        api.data_dir = self.root / "data"
        api.game_key = "stellaris"
        return api

    def test_sandbox_copy_is_created_once_then_reused(self):
        real = self.root / "real" / "launcher-v2.sqlite"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"REAL-DB")
        api = self._api()

        first = api._sandbox_db_for("stellaris", str(real))
        self.assertEqual(Path(first), api.data_dir / "sandbox" / "launcher-v2.sqlite")
        self.assertEqual(Path(first).read_bytes(), b"REAL-DB")

        # A later switch must never re-copy the real DB over the sandbox file,
        # otherwise every playset saved in the sandbox disappears.
        Path(first).write_bytes(b"SANDBOX-WITH-EDITS")
        real.write_bytes(b"REAL-DB-CHANGED")
        again = api._sandbox_db_for("stellaris", str(real))
        self.assertEqual(again, first)
        self.assertEqual(Path(again).read_bytes(), b"SANDBOX-WITH-EDITS")

    def test_sandbox_copy_falls_back_to_none_without_real_db(self):
        api = self._api()
        self.assertIsNone(api._sandbox_db_for("stellaris", str(self.root / "missing.sqlite")))

    def test_backup_is_tagged_with_its_game_and_mismatch_is_refused(self):
        api = self._api()
        backup = api.data_dir / "backups" / "launcher-v2-20260101-000000.sqlite"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"x")
        api._write_backup_meta(backup)
        self.assertEqual(api._backup_game(backup), "stellaris")

        api.game_key = "hoi4"
        with self.assertRaises(ValueError) as ctx:
            api.restore_backup({"name": backup.name})
        self.assertIn("不能在", str(ctx.exception))
        self.assertEqual(backup.read_bytes(), b"x")

    def test_untagged_backup_stays_restorable(self):
        api = self._api()
        backup = api.data_dir / "backups" / "launcher-v2-20260101-000001.sqlite"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"x")
        self.assertIsNone(api._backup_game(backup))


class _ModCatalog:
    """Catalog whose active playset really holds mods (for sanitising tests)."""

    def __init__(self, mods):
        self._mods = list(mods)

    def scan(self, force=False):
        return {"mods": [{"id": m} for m in self._mods], "available": [],
                "playsets": [{"id": "p1", "name": "keep me"}],
                "active_playset": "p1"}


class SaveOrderValidationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="smm-qa-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = object.__new__(Api)
        self.api.catalog = _FakeCatalog()
        self.api.db = _SpyDB()
        self.api.data_dir = self.root

    def test_missing_order_key_is_rejected_without_writing(self):
        with self.assertRaises(ValueError) as ctx:
            self.api.save_order({})
        self.assertEqual(str(ctx.exception), "顺序数据格式不正确。")
        self.assertEqual(self.api.db.calls, [])

    def test_none_order_is_rejected_without_writing(self):
        with self.assertRaises(ValueError):
            self.api.save_order({"playset_id": "p1", "order": None})
        self.assertEqual(self.api.db.calls, [])

    def test_string_order_is_rejected_without_writing(self):
        with self.assertRaises(ValueError):
            self.api.save_order({"playset_id": "p1", "order": "1,2,3"})
        self.assertEqual(self.api.db.calls, [])

    def test_string_array_of_real_ids_does_not_wipe_the_playset(self):
        """Old frontends send ["<mod_id>", ...]; that must be a 400, not a wipe."""
        mods = [f"m{i}" for i in range(19)]
        self.api.catalog = _ModCatalog(mods)
        with self.assertRaises(ValueError) as ctx:
            self.api.save_order({"playset_id": "p1", "order": mods})
        self.assertIn("没有可用的 MOD 条目", str(ctx.exception))
        self.assertEqual(self.api.db.calls, [])


if __name__ == "__main__":
    unittest.main()
