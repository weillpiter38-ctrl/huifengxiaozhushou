"""QA-T4 regression tests: hostile descriptors, system-dir guard, proc cache, token hygiene.

No real game files, launcher DB or save files are touched; everything lives in a
throw-away temp directory.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from smm import descriptor, launcher_db
from smm.api import looks_like_system_dir
from smm.catalog import _clip


class DescriptorCapsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, name, text, encoding="utf-8"):
        path = self.root / name
        path.write_bytes(text.encode(encoding, errors="replace") if isinstance(text, str) else text)
        return path

    def test_two_megabyte_name_is_clipped(self):
        huge = "A" * (2 * 1024 * 1024)
        path = self._write("descriptor.mod", f'name="{huge}"\nversion="1.0"\n')
        parsed = descriptor.parse_file(path)
        self.assertLessEqual(len(parsed["name"]), descriptor.MAX_FIELD)

    def test_unclosed_block_cannot_explode(self):
        body = "dependencies={\n" + "".join(f'    "mod_{i}"\n' for i in range(50000))
        path = self._write("descriptor.mod", 'name="big"\n' + body)
        started = time.perf_counter()
        parsed = descriptor.parse_file(path)
        elapsed = time.perf_counter() - started
        items = parsed.get("dependencies") or []
        self.assertLessEqual(len(items), descriptor.MAX_ITEMS)
        self.assertLess(elapsed, 3.0, f"parse took {elapsed:.2f}s")
        for item in items:
            self.assertLessEqual(len(item), descriptor.MAX_ITEM)

    def test_oversized_file_is_truncated(self):
        path = self._write("big.mod", 'name="first"\n' + ("#" * (3 * 1024 * 1024)))
        self.assertGreater(path.stat().st_size, descriptor.MAX_FILE_BYTES)
        parsed = descriptor.parse_file(path)  # must not slurp the whole thing
        self.assertEqual(parsed["name"], "first")

    def test_gbk_and_broken_utf8_do_not_raise(self):
        path = self._write("gbk.mod", 'name="中文名称"\ntags={\n"平衡"\n}\n', encoding="gbk")
        parsed = descriptor.parse_file(path)
        self.assertIsInstance(parsed.get("name"), str)
        raw = self.root / "raw.mod"
        raw.write_bytes(b'name="\xff\xfe\x00broken"\n')
        parsed = descriptor.parse_file(raw)
        self.assertIsInstance(parsed.get("name"), str)

    def test_empty_file(self):
        path = self._write("empty.mod", "")
        parsed = descriptor.parse_file(path)
        self.assertIsInstance(parsed, dict)


class ClipTest(unittest.TestCase):
    def test_clip_truncates_and_keeps_short(self):
        self.assertEqual(len(_clip("x" * 500)), 200)
        self.assertEqual(len(_clip("x" * 500, 120)), 120)
        self.assertEqual(_clip("短名"), "短名")
        self.assertEqual(_clip(""), "")
        self.assertEqual(_clip(None), "")


class SystemDirGuardTest(unittest.TestCase):
    def test_system_paths_are_rejected(self):
        for candidate in (
            r"C:\Windows",
            r"C:\Windows\System32\drivers\etc",
            r"C:\Program Files\Stellaris",
            r"C:\ProgramData",
            "C:\\",
        ):
            self.assertTrue(looks_like_system_dir(candidate), candidate)

    def test_ordinary_paths_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(looks_like_system_dir(tmp))
            self.assertFalse(looks_like_system_dir(str(Path(tmp) / "mod" / "更多MOD！")))

    def test_missing_path_is_not_system(self):
        self.assertFalse(looks_like_system_dir(r"D:\Games\definitely_missing_qa_t4"))


class ProcCacheTest(unittest.TestCase):
    def setUp(self):
        launcher_db._PROC_CACHE["names"] = None
        launcher_db._PROC_CACHE["at"] = 0.0

    def tearDown(self):
        launcher_db._PROC_CACHE["names"] = None
        launcher_db._PROC_CACHE["at"] = 0.0

    def test_cache_hit_and_fresh_bypass(self):
        calls = []

        def fake():
            calls.append(1)
            return ["stellaris.exe"]

        with mock.patch.object(launcher_db, "_tasklist_uncached", side_effect=fake):
            self.assertEqual(len(calls), 0)
            launcher_db._tasklist()
            self.assertEqual(len(calls), 1)
            launcher_db._tasklist()  # served from cache
            self.assertEqual(len(calls), 1)
            # The write guard reuses a list that is younger than its own window,
            # so one request does not spawn tasklist once per guarded action.
            launcher_db._tasklist(fresh=True)
            self.assertEqual(len(calls), 1)
            launcher_db._PROC_CACHE["at"] = time.monotonic() - (launcher_db._PROC_FRESH_TTL + 0.05)
            launcher_db._tasklist(fresh=True)  # older than the window: re-spawn
            self.assertEqual(len(calls), 2)

    def test_cache_expires(self):
        calls = []

        def fake():
            calls.append(1)
            return []

        with mock.patch.object(launcher_db, "_tasklist_uncached", side_effect=fake):
            launcher_db._tasklist()
            launcher_db._PROC_CACHE["at"] = time.monotonic() - (launcher_db._PROC_TTL + 1)
            launcher_db._tasklist()
            self.assertEqual(len(calls), 2)

    def test_running_state_reads_cache_but_guard_is_fresh(self):
        with mock.patch.object(launcher_db, "_tasklist", return_value=["stellaris.exe"]) as patched:
            state = launcher_db.running_state("stellaris")
            self.assertTrue(state["game_running"])
            patched.assert_called_with(fresh=False)
            state = launcher_db.running_state("stellaris", fresh=True)
            self.assertTrue(state["game_running"])
            patched.assert_called_with(fresh=True)


class LoadTokenTest(unittest.TestCase):
    def _load(self):
        import importlib

        run = importlib.import_module("run")
        return run.load_token

    def test_bad_tokens_are_replaced(self):
        load_token = self._load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for junk in ("", "!!!not a token!!!", "A" * 500, "中文令牌中文令牌中文令牌"):
                (root / "api-token").write_text(junk, encoding="utf-8", errors="replace")
                token = load_token(root)
                self.assertRegex(token, r"^[A-Za-z0-9_-]{16,128}$")
                self.assertEqual((root / "api-token").read_text(encoding="ascii").strip(), token)

    def test_good_token_is_kept(self):
        load_token = self._load()
        good = "a" * 43
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "api-token").write_text(good, encoding="ascii")
            self.assertEqual(load_token(root), good)

    def test_unwritable_token_file_still_returns_token(self):
        load_token = self._load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(Path, "write_text", side_effect=OSError("denied")):
                token = load_token(root)
            self.assertRegex(token, r"^[A-Za-z0-9_-]{16,128}$")


class LauncherWriteMessageTest(unittest.TestCase):
    def test_readonly_database_message_is_friendly(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "launcher-v2.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE playsets (id TEXT)")
            con.commit()
            con.close()
            os.chmod(db, 0o444)
            try:
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=rw", uri=True)
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    con.execute("CREATE TABLE x (y)")
                con.close()
                self.assertIn("readonly", str(ctx.exception).lower())
            finally:
                os.chmod(db, 0o666)


if __name__ == "__main__":
    unittest.main()
