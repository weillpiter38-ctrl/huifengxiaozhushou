"""Regression tests for 0.9.64 hardening. No real game files are touched."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from smm import updater
from smm.api import READ_ONLY_BLOCKED, Api
from smm.catalog import Catalog
from smm.server import serve
from smm.userdata import UserData


class FakeApi:
    def __init__(self):
        self.calls = []

    def dispatch(self, action, payload):
        self.calls.append((action, payload))
        return {"ok": True, "data": {"action": action}}

    def thumb(self, mod_id):
        return None


class ServerSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeApi()
        cls.server = serve(cls.fake, 0, token="test-token-123")
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_index_injects_token_and_is_not_cached(self):
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'content="test-token-123"', body)
        self.assertIn(b"Content-Security-Policy", body)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_static_assets_are_cacheable(self):
        status, headers, body = self._request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"X-SMM-Token", body)
        self.assertIn("max-age", headers.get("Cache-Control", ""))

    def test_api_requires_valid_token(self):
        status, _, _ = self._request("POST", "/api/bootstrap", b"{}")
        self.assertEqual(status, 403)
        status, _, _ = self._request("POST", "/api/bootstrap", b"{}",
                                     {"X-SMM-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _, body = self._request("POST", "/api/bootstrap", b"{}",
                                        {"X-SMM-Token": "test-token-123"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["action"], "bootstrap")

    def test_foreign_host_is_rejected(self):
        status, _, _ = self._request("POST", "/api/quit", b"{}",
                                     {"Host": "evil.example.com", "X-SMM-Token": "test-token-123"})
        self.assertEqual(status, 403)
        status, _, _ = self._request("GET", "/", None, {"Host": "evil.example.com"})
        self.assertEqual(status, 403)

    def test_invalid_payload_and_oversized_body(self):
        status, _, _ = self._request("POST", "/api/bootstrap", b"[1,2]",
                                     {"X-SMM-Token": "test-token-123"})
        self.assertEqual(status, 400)
        status, _, _ = self._request("POST", "/api/bootstrap", None,
                                     {"X-SMM-Token": "test-token-123",
                                      "Content-Length": str(200 * 1024 * 1024)})
        self.assertEqual(status, 400)

    def test_internal_errors_do_not_leak_details(self):
        def boom(action, payload):
            raise RuntimeError("C:/Users/ADMIN/secret/internal-path.txt")

        with mock.patch.object(self.fake, "dispatch", side_effect=boom):
            status, _, body = self._request("POST", "/api/bootstrap", b"{}",
                                            {"X-SMM-Token": "test-token-123"})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertIn("内部错误", body.decode("utf-8"))


class ApiHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_blocks_every_write_action(self):
        api = object.__new__(Api)
        api.read_only = True
        for action in ("save_order", "enable_all_dlc", "save_game_settings",
                       "manager_files_delete", "apply_update", "merge_enabled_mods",
                       "folder_create", "launch_game"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    api.dispatch(action, {})
        self.assertIn("save_order", READ_ONLY_BLOCKED)
        self.assertNotIn("bootstrap", READ_ONLY_BLOCKED)

    def test_settings_atomic_write_round_trips_non_utf8_bytes(self):
        raw = b'language="l_simp_chinese"\r\n# \xb6\xd3\xc4\xd0\r\n'
        text = raw.decode("utf-8", "surrogateescape")
        path = self.root / "settings.txt"
        Api._atomic_write_text(path, text)
        self.assertEqual(path.read_bytes(), raw)
        Api._atomic_write_text(path, text, bom=True)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbf" + raw)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_thumbnail_rejects_absolute_and_parent_paths(self):
        cat = object.__new__(Catalog)
        cat._thumbs = {}
        mod_dir = self.root / "mod"
        mod_dir.mkdir()
        (mod_dir / "thumb.png").write_bytes(b"PNG")
        secret = self.root / "secret.txt"
        secret.write_bytes(b"SECRET")
        self.assertEqual(cat._thumbnail(str(mod_dir), str(secret)), "")
        self.assertEqual(cat._thumbnail(str(mod_dir), "../secret.txt"), "")
        self.assertTrue(cat._thumbnail(str(mod_dir), "thumb.png").endswith("thumb.png"))

    def test_dlc_load_backup_is_bounded_and_holds_pre_change_bytes(self):
        docs = self.root / "docs"
        docs.mkdir()
        dlc = docs / "dlc_load.json"
        old_bytes = b'{"enabled_mods": ["mod/ugc_1.mod"], "disabled_dlcs": []}'
        dlc.write_bytes(old_bytes)
        backups = self.root / "data" / "backups"
        backups.mkdir(parents=True)
        for i in range(25):
            old = backups / f"dlc_load-20200101-0000{i:02d}.json"
            old.write_bytes(b"{}")
            os.utime(old, (1700000000 + i, 1700000000 + i))

        class FakeCatalog:
            def vision(self):
                return {"docs": str(docs)}

        class FakeDB:
            def snapshot(self):
                return {"mods": []}

        api = object.__new__(Api)
        api.data_dir = self.root / "data"
        api.sandbox = False
        api.catalog = FakeCatalog()
        api.db = FakeDB()
        api._write_dlc_load([])

        files = list(backups.glob("dlc_load-*.json"))
        self.assertLessEqual(len(files), 20)
        self.assertTrue(any(item.read_bytes() == old_bytes for item in files))
        self.assertEqual(json.loads(dlc.read_text(encoding="utf-8"))["enabled_mods"], [])
        self.assertEqual(list(docs.glob("*.tmp")), [])

    def test_merge_terminates_when_descriptor_file_already_exists(self):
        mod_dir = self.root / "mods"
        mod_dir.mkdir()
        source = mod_dir / "Payload"
        source.mkdir()
        (source / "common" ).mkdir()
        (source / "common" / "x.txt").write_text("hello", encoding="utf-8")
        (mod_dir / "LoopTest.mod").write_text("leftover", encoding="utf-8")

        class FakeCatalog:
            def scan(self, force=False):
                return {"mods": [{"enabled": True, "dir": str(source), "name": "Payload"}]}

            def vision(self):
                return {"mod_dir": str(mod_dir), "raw_version": "v4.5.1"}

        api = object.__new__(Api)
        api.read_only = False
        api.catalog = FakeCatalog()
        api.data_dir = self.root / "data"

        finished = threading.Event()
        box = {}

        def run():
            try:
                box["result"] = api.merge_enabled_mods({"name": "LoopTest"})
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(finished.wait(10), "merge_enabled_mods 没有结束，疑似死循环回归")
        self.assertNotIn("error", box)
        # A leftover .mod must never be overwritten: the new pack takes its own
        # name, otherwise pack #1's registration silently points at pack #2.
        packs = sorted(mod_dir.glob("LoopTest*.zip"))
        self.assertTrue(packs, "合并必须结束并生成合并包")
        self.assertEqual((mod_dir / "LoopTest.mod").read_text(encoding="utf-8"), "leftover",
                         "已存在的 .mod 不能被新合并包覆盖")
        self.assertTrue((mod_dir / (packs[0].stem + ".mod")).is_file(),
                        "合并包必须有自己的 .mod 登记文件")

    def test_userdata_concurrent_writes_leave_valid_json(self):
        service = UserData(self.root)
        errors = []

        def work(index):
            try:
                for n in range(12):
                    service.note(f"mod-{index}", f"note-{n}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertIsInstance(json.loads(service.path.read_text(encoding="utf-8")), dict)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class UpdaterHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_requires_sha256_and_sanitises_version(self):
        with self.assertRaises(ValueError):
            updater.download(self.root, "https://example.com/a.exe", "", "0.9.64")
        payload = b"fake-exe-bytes"
        sha = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            result = updater.download(self.root, "http://127.0.0.1:1/a.exe", sha, "../../evil")
        final = Path(result["path"])
        self.assertEqual(final.parent, (self.root / "updates").resolve())
        self.assertNotIn("..", final.name.replace(".._..", ""))
        self.assertTrue(final.is_file())
        self.assertTrue(Path(str(final) + ".sha256").is_file())

    def test_apply_rejects_file_outside_updates_folder(self):
        foreign = self.root / "evil.exe"
        foreign.write_bytes(b"MZ")
        with mock.patch.object(updater.sys, "frozen", True, create=True):
            with self.assertRaises(ValueError):
                updater.apply(self.root, str(foreign), "0.9.64")


if __name__ == "__main__":
    unittest.main()
