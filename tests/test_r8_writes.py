"""Round-8: a data file replaced by a directory must produce a readable 400-class error.

`os.replace` onto a directory raises PermissionError on Windows; before this fix the
Api turned that into `500 内部错误` plus a traceback in the log, and the settings
writer swallowed OSError entirely (UI said ok, change was lost on restart).
"""
from __future__ import annotations

import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from smm.api import Api
from smm.userdata import UserData


def _api(tmp: Path) -> Api:
    api = Api.__new__(Api)
    api.game_key = "stellaris"
    api.sandbox = True
    api.read_only = False
    api.data_dir = tmp
    api.db = tmp / "launcher-v2.sqlite"
    api.catalog = types.SimpleNamespace(game="", mod_dir_override="", docs="")
    return api


class DirectoryTargetTest(unittest.TestCase):
    def test_dlc_load_directory_gives_readable_error(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "dlc_load.json"
            target.mkdir()
            api = _api(tmp)
            api._dlc_load_path = lambda: target
            api._backup_dlc_load = lambda *a, **k: None
            api.dlc_status = lambda payload=None: {"total": 0, "enabled": 0, "disabled": 0, "items": []}
            with self.assertRaises(ValueError) as ctx:
                api.enable_all_dlc({})
            self.assertIn("无法写入", str(ctx.exception))
            self.assertIn("dlc_load.json", str(ctx.exception))
            self.assertTrue(target.is_dir(), "受伤的目录本身不应被替换")
            leftovers = [p.name for p in tmp.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], "临时文件必须清理")

    def test_path_settings_directory_gives_readable_error(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "paths.json").mkdir()
            api = _api(tmp)
            with self.assertRaises(ValueError) as ctx:
                api._save_path_settings()
            self.assertIn("paths.json", str(ctx.exception))

    def test_userdata_directory_gives_readable_error(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "userdata.json").mkdir()
            store = UserData(tmp)
            with self.assertRaises(ValueError) as ctx:
                store.folder_create("QA")
            self.assertIn("userdata.json", str(ctx.exception))

    def test_atomic_write_text_directory_target(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "settings.txt"
            target.mkdir()
            with self.assertRaises(ValueError) as ctx:
                Api._atomic_write_text(target, "fullScreen=no\n")
            self.assertIn("设置文件", str(ctx.exception))
            self.assertEqual([p.name for p in tmp.iterdir() if p.name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
