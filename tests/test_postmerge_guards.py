"""合并层补丁的回归：无效 --docs 不得回落到真实库写根。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smm.api import Api  # noqa: E402
from smm.catalog import Catalog  # noqa: E402


def make_api(tmp: Path, docs: Path, docs_explicit: bool) -> Api:
    catalog = Catalog(docs=str(docs), game=None, db_path=None, sandbox=True, workshop=None,
                      game_key="stellaris")
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Api(catalog, data_dir, read_only=False, sandbox=True, workshop=None,
               game_key="stellaris", root_data=tmp, docs_explicit=docs_explicit)


class SandboxWriteRootTests(unittest.TestCase):
    def test_unhonored_explicit_docs_still_isolates_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            api = make_api(tmp, tmp / "missing-docs", docs_explicit=True)
            real = Path(api.catalog.vision()["mod_dir"])
            self.assertNotEqual(Path(api.write_mod_root()).resolve(), real.resolve())
            self.assertIn("sandbox", str(api.write_mod_root()).lower())

    def test_dlc_root_accepts_executable_path(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            api = make_api(tmp, tmp / "docs", docs_explicit=False)
            original = api.catalog.vision
            api.catalog.vision = lambda: {"game": str(tmp / "Stellaris" / "stellaris.exe")}
            try:
                self.assertEqual(api._dlc_root(), tmp / "Stellaris" / "dlc")
            finally:
                api.catalog.vision = original


class ImportPlaysetMatchTests(unittest.TestCase):
    """导出文件里的 MOD 必须真的被收进播放集。

    合并 v2 曾在这一块丢了 `ids.append(mod_id)`：`if not ids:` 永远成立，
    任何导出文件都报「没有匹配到已安装的 MOD，未创建播放集。」
    """

    def _api(self, tmp: Path, api: Api) -> Api:
        api.catalog.scan = lambda: {
            "mods": [],
            "available": [{"id": "m1", "name": "Alpha", "display_name": "Alpha", "steam_id": "111"}],
            "playsets": [],
            "active_playset": None,
        }
        created = {}

        class FakeDb:
            def create_playset(self, title, ids):
                created["title"] = title
                created["ids"] = list(ids)
                return "new-id"

            def set_active(self, pid):
                created["active"] = pid

            def save_order(self, pid, rows):
                created["rows"] = rows

        api.db = FakeDb()
        api._sync_dlc_load_safe = lambda *a, **k: None
        api._write_and_refresh = lambda action: {"new_id": action()}
        api._created = created
        return api

    def test_exported_mods_are_collected_and_flags_kept(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            docs = tmp / "docs"
            docs.mkdir()
            api = self._api(tmp, make_api(tmp, docs, docs_explicit=False))
            result = api.import_playset({"data": {"name": "往返", "mods": [
                {"displayName": "Alpha", "enabled": False},
                {"displayName": "Missing Ones", "enabled": True},
            ]}})
            self.assertEqual(api._created["ids"], ["m1"])
            self.assertEqual(api._created["rows"], [{"mod_id": "m1", "enabled": False}])
            self.assertEqual(result["import_summary"]["matched"], 1)
            self.assertEqual(result["import_summary"]["missing"], ["Missing Ones"])


if __name__ == "__main__":
    unittest.main()
