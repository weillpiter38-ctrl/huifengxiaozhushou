"""崩溃归因的离线测试：日志 → MOD 的映射。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smm import crash


def make_mods():
    return [
        {"id": "a1", "name": "More Events Mod", "dir": r"G:\SteamLibrary\steamapps\workshop\content\281990\727000451",
         "steam_id": "727000451"},
        {"id": "b2", "name": "Tidy Tradition", "dir": r"C:\Users\x\Documents\Paradox Interactive\Stellaris\mod\tidy_tradition"},
        {"id": "c3", "name": "UI Overhaul", "dir": r"C:\mods\ui_overhaul"},
    ]


class CollectTest(unittest.TestCase):
    def _docs(self, tmp, error_text, game_text="", dumps=()):
        docs = Path(tmp)
        (docs / "logs").mkdir(parents=True)
        (docs / "logs" / "error.log").write_text(error_text, encoding="utf-8")
        if game_text:
            (docs / "logs" / "game.log").write_text(game_text, encoding="utf-8")
        if dumps:
            (docs / "crashes").mkdir()
            for name in dumps:
                (docs / "crashes" / name).write_text("dump", encoding="utf-8")
        return docs

    def test_maps_errors_to_mods_by_path(self):
        text = "\n".join([
            "[12:00:00][game.cpp:1]: ERROR mod/ugc_727000451/common/x.txt: unknown trigger foo",
            "[12:00:01][game.cpp:2]: ERROR mod/ugc_727000451/events/y.txt: duplicate definition",
            "[12:00:02][game.cpp:3]: ERROR mod/tidy_tradition/common/z.txt: parse error",
            "[12:00:03][game.cpp:4]: INFO everything fine",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            report = crash.collect(self._docs(tmp, text, dumps=["crash-1.dmp"]), make_mods())
        self.assertEqual(report["total_errors"], 3)
        names = [row["name"] for row in report["mapped"]]
        self.assertEqual(names[0], "More Events Mod")
        self.assertEqual(report["mapped"][0]["count"], 2)
        self.assertEqual(report["mapped"][0]["kinds"].get("duplicate"), 1)
        self.assertIn("Tidy Tradition", names)
        self.assertEqual(report["dumps"], ["crash-1.dmp"])
        self.assertEqual(report["suspects"][0]["name"], "More Events Mod")

    def test_unmapped_lines_are_kept(self):
        text = "[x][game.cpp:9]: ERROR something broke without a mod path\n"
        with tempfile.TemporaryDirectory() as tmp:
            report = crash.collect(self._docs(tmp, text), make_mods())
        self.assertEqual(report["mapped"], [])
        self.assertEqual(len(report["unmapped"]), 1)
        self.assertIn("without a mod path", report["unmapped"][0]["line"])

    def test_game_log_is_scanned_too(self):
        error = "[x][a]: ERROR mod/ui_overhaul/common/a.txt: missing localisation key\n"
        game = "[x][b]: ERROR mod/ugc_727000451/common/b.txt: unknown effect bar\n"
        with tempfile.TemporaryDirectory() as tmp:
            report = crash.collect(self._docs(tmp, error, game), make_mods())
        counts = {row["name"]: row["count"] for row in report["mapped"]}
        self.assertEqual(counts.get("UI Overhaul"), 1)
        self.assertEqual(counts.get("More Events Mod"), 1)
        self.assertEqual(len(report["files"]), 2)

    def test_missing_logs_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = crash.collect(Path(tmp), make_mods())
        self.assertTrue(report["ok"])
        self.assertEqual(report["files"], [])
        self.assertEqual(report["total_errors"], 0)
        self.assertEqual(report["suspects"], [])

    def test_mod_without_dir_does_not_crash(self):
        text = "[x][a]: ERROR mod/whatever/common/a.txt: parse error\n"
        with tempfile.TemporaryDirectory() as tmp:
            report = crash.collect(self._docs(tmp, text), [{"id": "z", "name": "No Dir"}])
        self.assertEqual(report["mapped"], [])
        self.assertEqual(len(report["unmapped"]), 1)

    def test_missing_file_is_attributed_to_the_mod_that_has_it(self):
        text = "[x][virtualfilesystem_physfs.cpp:1]: Could not open file: sound/aar_sfx.asset, error: not found\n"
        with tempfile.TemporaryDirectory() as tmp:
            docs = self._docs(tmp, text)
            mod_a = Path(tmp) / "mods" / "mod_a"
            mod_b = Path(tmp) / "mods" / "mod_b"
            (mod_a / "sound").mkdir(parents=True)
            (mod_b / "sound").mkdir(parents=True)
            (mod_a / "sound" / "aar_sfx.asset").write_text("x", encoding="utf-8")
            mods = [
                {"id": "a", "name": "Mod A", "dir": str(mod_a)},
                {"id": "b", "name": "Mod B", "dir": str(mod_b)},
            ]
            report = crash.collect(docs, mods)
        self.assertEqual(len(report["mapped"]), 1)
        self.assertEqual(report["mapped"][0]["name"], "Mod A")
        self.assertEqual(report["mapped"][0]["kinds"], {"missing_file": 1})

    def test_shared_missing_file_stays_unmapped(self):
        text = "[x][vfs.cpp:1]: Could not open file: sound/shared.asset, error: not found\n"
        with tempfile.TemporaryDirectory() as tmp:
            docs = self._docs(tmp, text)
            for name in ("m1", "m2"):
                folder = Path(tmp) / "mods" / name / "sound"
                folder.mkdir(parents=True)
                (folder / "shared.asset").write_text("x", encoding="utf-8")
            mods = [
                {"id": "1", "name": "One", "dir": str(Path(tmp) / "mods" / "m1")},
                {"id": "2", "name": "Two", "dir": str(Path(tmp) / "mods" / "m2")},
            ]
            report = crash.collect(docs, mods)
        self.assertEqual(report["mapped"], [])
        self.assertEqual(len(report["unmapped"]), 1)


if __name__ == "__main__":
    unittest.main()
