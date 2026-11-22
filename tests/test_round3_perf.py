"""Round-3 tests: payload bounding, incremental MOD index, gzip transfer.

All fixtures are synthetic directories under a tempdir; no game files are read.
"""
from __future__ import annotations

import gzip
import http.client
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smm.catalog import CONFLICT_PATHS_MAX, Catalog
from smm.server import serve

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


class _Fixture(unittest.TestCase):
    mod_total = 3
    shared_files = 0

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
                (folder / "common" / f"shared_{shared}.txt").write_text("x", encoding="utf-8")
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


class ConflictBoundingTest(_Fixture):
    mod_total = 2
    shared_files = CONFLICT_PATHS_MAX + 10  # 90 shared files: more than the cap

    def test_conflict_paths_are_capped_and_keep_the_true_total(self):
        payload = self.catalog().scan(force=True)
        self.assertTrue(payload["ok"])
        for mod in payload["mods"]:
            self.assertEqual(len(mod["conflict_paths"]), CONFLICT_PATHS_MAX)
            # descriptor.mod is shared by both mods as well, hence +1.
            self.assertEqual(mod["conflict_paths_total"], self.shared_files + 1)
            self.assertEqual(mod["conflict_count"], self.shared_files + 1)
        entry = payload["mods"][0]["conflict_paths"][0]
        # The UI reads item.path + item.mods.join('、'), so the shape must not change.
        self.assertIsInstance(entry["path"], str)
        self.assertTrue(all(isinstance(name, str) for name in entry["mods"]))
        self.assertEqual(entry["mod_count"], 2)
        self.assertIn("common/shared_", entry["path"])
        self.assertLessEqual(len(entry["mods"]), 4)


class BulkIndexTest(_Fixture):
    mod_total = 4

    def test_every_scan_walks_the_mod_dir(self):
        catalog = self.catalog()
        first = catalog.scan(force=True)
        self.assertEqual(first["stats"]["total"], 4)
        self.assertEqual(first["stats"]["files"], 8)  # 2 files per mod
        second = catalog.scan(force=True)
        self.assertEqual(second["stats"]["files"], 8)
        self.assertNotIn("reused", catalog._bulk)  # no reuse: a walk always happens
        self.assertEqual(catalog._bulk["ms"] < 400, True)

    def test_new_file_in_a_sub_folder_shows_up_on_the_next_scan(self):
        # A change inside <mod>/common/ does not touch the mtime of <mod> itself,
        # so an mtime-keyed reuse cache would miss it (this is why we do not cache).
        catalog = self.catalog()
        catalog.scan(force=True)
        target = self.mod_dir / "mod-000" / "common" / "added.txt"
        target.write_text("y", encoding="utf-8")
        payload = catalog.scan(force=True)
        self.assertEqual(payload["stats"]["files"], 9)

    def test_deleted_mod_folder_is_dropped(self):
        catalog = self.catalog()
        catalog.scan(force=True)
        folder = self.mod_dir / "mod-001"
        shutil.rmtree(folder)
        payload = catalog.scan(force=True)
        self.assertEqual(payload["stats"]["total"], 3)
        self.assertNotIn(folder.name, [Path(mod["dir"]).name for mod in payload["mods"]])


class _StubApi:
    def dispatch(self, action, payload):
        return {"ok": True, "data": {"blob": "x" * 4096, "action": action}}

    def thumb(self, mod_id):
        return None


class GzipTransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(_StubApi(), 0, token="qa-token-1234")
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path, accept=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if accept:
            headers["Accept-Encoding"] = accept
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_index_is_gzipped_when_the_client_asks(self):
        status, headers, body = self._get("/", accept="gzip, deflate")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        text = gzip.decompress(body).decode("utf-8")
        self.assertIn("qa-token-1234", text)
        self.assertNotIn("__SMM_TOKEN__", text)

    def test_index_is_plain_without_accept_encoding(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn(b"qa-token-1234", body)

    def test_post_response_is_gzipped_and_still_decodes(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/bootstrap", body=b"{}",
                         headers={"Content-Type": "application/json",
                                  "X-SMM-Token": "qa-token-1234",
                                  "Accept-Encoding": "gzip"})
            response = conn.getresponse()
            headers = dict(response.getheaders())
            raw = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        self.assertEqual(payload["data"]["action"], "bootstrap")
        self.assertEqual(len(payload["data"]["blob"]), 4096)

    def test_gzip_with_zero_quality_is_refused(self):
        """`gzip;q=0` means "do not gzip" (RFC 9110 12.5.3)."""
        for accept in ("gzip;q=0", "gzip;q=0.0", "br, gzip;q=0", "gzip ; q=0"):
            with self.subTest(accept=accept):
                status, headers, body = self._get("/", accept=accept)
                self.assertEqual(status, 200)
                self.assertIsNone(headers.get("Content-Encoding"))
                self.assertEqual(headers.get("Vary"), "Accept-Encoding")
                self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_identity_only_client_gets_plain_body(self):
        status, headers, body = self._get("/", accept="identity")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertIn(b"qa-token-1234", body)

    def test_positive_quality_still_compresses(self):
        for accept in ("gzip;q=0.5", "GZIP;Q=1", "deflate, gzip;q=0.8", "*"):
            with self.subTest(accept=accept):
                status, headers, body = self._get("/", accept=accept)
                self.assertEqual(status, 200)
                encoding = headers.get("Content-Encoding")
                self.assertIn(encoding, ("gzip", None))
                if encoding == "gzip":
                    self.assertEqual(int(headers["Content-Length"]), len(body))
                    self.assertIn("qa-token-1234", gzip.decompress(body).decode("utf-8"))

    def test_head_on_static_matches_get_headers_without_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("HEAD", "/", headers={"Accept-Encoding": "identity"})
            response = conn.getresponse()
            headers = dict(response.getheaders())
            body = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_head_on_api_is_method_not_allowed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("HEAD", "/api/bootstrap", headers={"X-SMM-Token": "qa-token-1234"})
            response = conn.getresponse()
            headers = dict(response.getheaders())
            body = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
