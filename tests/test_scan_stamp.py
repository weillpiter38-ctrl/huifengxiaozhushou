import os
import tempfile
import time
import unittest
from pathlib import Path

from smm.catalog import Catalog


class ExtraRootStampTest(unittest.TestCase):
    """The 20 s scan cache must expire when a MOD appears in a sandbox/extra root."""

    def _catalog(self, root: Path) -> Catalog:
        cat = Catalog.__new__(Catalog)
        cat._mods_stamp_paths = [str(root)]
        cat._mods_stamp = cat._mods_stamp_now()
        return cat

    def test_adding_a_folder_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = self._catalog(Path(tmp))
            self.assertTrue(cat._mods_stamp_equal())
            target = Path(tmp) / "new_mod"
            target.mkdir()
            deadline = time.time() + 2
            while cat._mods_stamp_equal() and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(cat._mods_stamp_equal())

    def test_tuple_entries_are_stat_ed_by_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mirror"
            root.mkdir()
            cat = Catalog.__new__(Catalog)
            cat.extra_mod_dirs = [(root, "sandbox")]
            cat._mods_stamp_paths = [str(root)]
            cat._mods_stamp = cat._mods_stamp_now()
            self.assertEqual(cat._mods_stamp, {str(root): cat._mods_stamp[str(root)]})
            self.assertIsNotNone(cat._mods_stamp[str(root)])
            (root / "qa_vis.mod").write_text("name=\"x\"", encoding="utf-8")
            deadline = time.time() + 2
            while cat._mods_stamp_equal() and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(cat._mods_stamp_equal())

    def test_missing_paths_keep_the_cache_permissive(self):
        cat = Catalog.__new__(Catalog)
        cat._mods_stamp_paths = []
        cat._mods_stamp = None
        self.assertTrue(cat._mods_stamp_equal())


if __name__ == "__main__":
    unittest.main()