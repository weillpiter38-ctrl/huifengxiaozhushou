"""round-7 guard: one minimal assertion per historical fix.

These are deliberately cheap and intent-only. They exist so a merge that
reverts a fix fails loudly instead of silently restoring the old behaviour
(11 different files have been reconciled by hand already).
"""
from __future__ import annotations

import inspect
import unittest

from smm import catalog as catalog_module
from smm import launcher_db, server
from smm.catalog import Catalog


class ProcCacheGuardTest(unittest.TestCase):
    """round-1: every refresh spawned `tasklist` (320-480 ms)."""

    def test_fresh_ttl_stays_below_normal_ttl(self):
        self.assertTrue(hasattr(launcher_db, "_PROC_FRESH_TTL"))
        self.assertGreater(launcher_db._PROC_FRESH_TTL, 0)
        self.assertLessEqual(launcher_db._PROC_FRESH_TTL, launcher_db._PROC_TTL)

    def test_fresh_bypass_exists_in_signature(self):
        self.assertIn("fresh", inspect.signature(launcher_db.running_state).parameters)


class LauncherLockGuardTest(unittest.TestCase):
    """round-2/round-3: a locked database must answer fast instead of 11.5 s."""

    def test_lock_timeout_is_short(self):
        self.assertLessEqual(launcher_db._LOCK_TIMEOUT, 2.0)

    def test_write_retry_budget_is_short(self):
        self.assertLessEqual(launcher_db._WRITE_RETRY_BUDGET, 1.0)
        self.assertGreater(launcher_db._WRITE_RETRY_BUDGET, 0)


class GzipGuardTest(unittest.TestCase):
    """round-5: `gzip;q=0` must be refused; sub-1 KB bodies stay plain."""

    def _handler(self, accept_encoding: str):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Accept-Encoding": accept_encoding}
        return handler

    def test_quality_zero_is_refused(self):
        self.assertFalse(self._handler("gzip;q=0")._accepts_gzip())
        self.assertFalse(self._handler("br, gzip;q=0")._accepts_gzip())

    def test_quality_positive_is_accepted(self):
        self.assertTrue(self._handler("gzip")._accepts_gzip())
        self.assertTrue(self._handler("gzip;q=0.5")._accepts_gzip())

    def test_min_size_is_one_kib(self):
        self.assertEqual(server.GZIP_MIN_BYTES, 1024)


class BacklogGuardTest(unittest.TestCase):
    """round-6: default backlog 5 refused burst connections (76 failures at 200)."""

    def test_accept_queue_is_deep(self):
        self.assertGreaterEqual(server.SMMServer.request_queue_size, 128)


class ScanReentryGuardTest(unittest.TestCase):
    """round-6: re-entering scan() on the same thread used to deadlock forever."""

    def test_reentry_raises_instead_of_deadlocking(self):
        import threading

        catalog = Catalog(docs="x", game="x", db_path="x")
        catalog._scan_owner = threading.get_ident()
        try:
            with self.assertRaises(RuntimeError):
                catalog.scan(force=True)
        finally:
            catalog._scan_owner = None

    def test_conflict_limits_stay_bounded(self):
        self.assertLessEqual(catalog_module.CONFLICT_PATHS_MAX, 80)
        self.assertLessEqual(catalog_module.CONFLICT_NAMES_MAX, 4)


if __name__ == "__main__":
    unittest.main()
