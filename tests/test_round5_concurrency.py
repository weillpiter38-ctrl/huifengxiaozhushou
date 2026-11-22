"""round-5: single-flight scans and copy-on-write decoration (concurrency safety)."""
from __future__ import annotations

import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from smm.api import Api
from smm.catalog import Catalog


class _CountingCatalog(Catalog):
    """Catalog whose scan body is a sleep, so overlap is guaranteed."""

    def __init__(self):
        super().__init__(docs="x", game="x", db_path="x")
        self.scans = 0
        self.gate = threading.Lock()

    def _scan_once(self, force=False):
        with self.gate:
            self.scans += 1
            generation = self.scans
        time.sleep(0.4)
        payload = {"ok": True, "mods": [], "generation": generation}
        # Mirror the bookkeeping the real _scan_once does at the end of a scan.
        self._cache = payload
        self._cache_at = time.time()
        self._scan_done += 1
        return payload


class SingleFlightScanTest(unittest.TestCase):
    def test_concurrent_forced_scans_share_one_pass(self):
        catalog = _CountingCatalog()
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(catalog.scan(force=True))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        elapsed = time.perf_counter() - started

        self.assertEqual(len(results), 8)
        self.assertEqual(catalog.scans, 1, "eight concurrent refreshes must run one scan")
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len({id(item) for item in results}), 1)

    def test_sequential_forced_scans_still_rescan(self):
        catalog = _CountingCatalog()
        catalog.scan(force=True)
        catalog.scan(force=True)
        self.assertEqual(catalog.scans, 2)

    def test_cached_scan_does_not_block_or_rescan(self):
        catalog = _CountingCatalog()
        first = catalog.scan(force=True)
        second = catalog.scan()
        self.assertEqual(catalog.scans, 1)
        self.assertIs(first, second)


class ScanLockSafetyTest(unittest.TestCase):
    """A scan that calls back into scan() must fail fast instead of taking the lock twice."""

    def test_reentrant_scan_fails_fast(self):
        catalog = _CountingCatalog()
        original = catalog._scan_once
        box = {}

        def reentrant(force=False):
            box["inner"] = catalog.scan(force=True)
            return original(force)

        catalog._scan_once = reentrant
        started = time.perf_counter()
        with self.assertRaises(RuntimeError):
            catalog.scan(force=True)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertNotIn("inner", box)
        self.assertIsNone(catalog._scan_owner)

    def test_lock_is_released_after_scan_failure(self):
        catalog = _CountingCatalog()
        original = catalog._scan_once

        def boom(force=False):
            raise RuntimeError("注入的扫描异常")

        catalog._scan_once = boom
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                catalog.scan(force=True)
            self.assertIsNone(catalog._scan_owner)
        catalog._scan_once = original
        payload = catalog.scan(force=True)
        self.assertEqual(payload["generation"], 1)
        self.assertIsNone(catalog._scan_owner)

    def test_owner_marker_cleared_after_successful_scan(self):
        catalog = _CountingCatalog()
        catalog.scan()
        self.assertIsNone(catalog._scan_owner)
        catalog.scan(force=True)
        self.assertIsNone(catalog._scan_owner)


class DecorateCopiesTest(unittest.TestCase):
    """`_decorate` writes into the payload, so it must never touch the shared cache."""

    def _api(self, tmp: Path):
        api = Api.__new__(Api)
        api.game_key = "stellaris"
        api.sandbox = True
        api.read_only = False
        api.data_dir = tmp
        api.db = tmp / "launcher-v2.sqlite"
        api.catalog = types.SimpleNamespace(cache_stats=lambda: {"hits": 0, "misses": 0, "loaded": 0})
        api.userdata = types.SimpleNamespace(
            snapshot=lambda: {"notes": {"mod-1": "note"}, "folders": [], "assignments": {}})
        api._backups = lambda: []
        api.dlc_status = lambda payload=None: {"total": 0, "enabled": 0, "disabled": 0, "items": []}
        api._load_history = lambda: []
        return api

    def test_decorate_does_not_mutate_catalog_payload(self):
        with TemporaryDirectory() as raw:
            api = self._api(Path(raw))
            payload = {"ok": True, "mods": [{"id": "mod-1"}], "available": [], "game": {"version": "1"}}
            decorated = api._decorate(payload)
            self.assertIsNot(decorated, payload)
            self.assertEqual(decorated["mods"][0]["note"], "note")
            self.assertNotIn("note", payload["mods"][0])
            self.assertNotIn("app", payload)
            self.assertNotIn("app", payload.get("game") or {})

    def test_two_decorations_are_independent(self):
        with TemporaryDirectory() as raw:
            api = self._api(Path(raw))
            payload = {"ok": True, "mods": [{"id": "mod-1"}], "available": [], "game": {}}
            first = api._decorate(payload)
            second = api._decorate(payload)
            self.assertEqual(first["mods"][0]["note"], "note")
            self.assertEqual(second["mods"][0]["note"], "note")
            first["mods"][0]["note"] = "changed"
            self.assertEqual(second["mods"][0]["note"], "note")
            self.assertNotIn("note", payload["mods"][0])


class AcceptBacklogTest(unittest.TestCase):
    """The default socketserver backlog (5) refused burst connections outright."""

    def test_server_accept_queue_is_deep_enough_for_bursts(self):
        from smm.server import SMMServer

        self.assertGreaterEqual(SMMServer.request_queue_size, 128)
        self.assertTrue(SMMServer.daemon_threads)


if __name__ == "__main__":
    unittest.main()
