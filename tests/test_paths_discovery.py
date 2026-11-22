# -*- coding: utf-8 -*-
"""Documents discovery: relocated profile folders and stale account leftovers."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smm import paths


def make_docs(base, name="Stellaris", latest=1000, mods=3):
    folder = Path(base) / "Paradox Interactive" / name
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "launcher-v2.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table playsets (id integer primary key, updatedOn integer)")
    con.execute("create table mods (id text primary key)")
    con.execute("insert into playsets (updatedOn) values (?)", (latest,))
    for index in range(mods):
        con.execute("insert into mods (id) values (?)", (f"m{index}",))
    con.commit()
    con.close()
    return folder


class RootScanTest(unittest.TestCase):
    def test_finds_documents_beside_users_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "文档" / "Paradox Interactive" / "Stellaris").mkdir(parents=True)
            (base / "用户" / "someone" / "Documents").mkdir(parents=True)
            found = [str(item) for item in paths.scan_root_documents([base])]
            self.assertTrue(any(item.endswith("文档") for item in found))
            self.assertTrue(any("someone" in item and item.endswith("Documents") for item in found))

    def test_missing_roots_are_ignored(self):
        self.assertEqual(paths.scan_root_documents([Path("Z:/nope-not-here")]), [])

    def test_dedupe_resolves_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = paths._dedupe([tmp, tmp, Path(tmp)])
            self.assertEqual(len(rows), 1)


class ScoreTest(unittest.TestCase):
    def test_newer_playset_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = make_docs(Path(tmp) / "old", latest=100, mods=50)
            new = make_docs(Path(tmp) / "new", latest=900, mods=1)
            self.assertGreater(paths._db_score(new), paths._db_score(old))

    def test_missing_db_scores_lowest(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            self.assertEqual(paths._db_score(empty), (-1, -1))


class CandidatesTest(unittest.TestCase):
    def test_candidates_require_a_database(self):
        """An empty leftover folder must not masquerade as a valid cache."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_docs(base, name="Stellaris", latest=500)
            (base / "Paradox Interactive" / "Stellaris Beta").mkdir(parents=True)
            original = paths._document_roots
            paths._ROOTS_CACHE["value"] = None
            paths._document_roots = lambda: paths._dedupe([base])
            try:
                rows = [item.name for item in paths.docs_candidates("stellaris")]
            finally:
                paths._document_roots = original
                paths._ROOTS_CACHE["value"] = None
            self.assertIn("Stellaris", rows)
            self.assertNotIn("Stellaris Beta", rows)  # no launcher-v2.sqlite yet

    def test_default_picks_newest_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_docs(base, name="Stellaris", latest=100, mods=9)
            make_docs(base, name="Stellaris Beta", latest=800, mods=1)
            original = paths._document_roots
            paths._ROOTS_CACHE["value"] = None
            paths._document_roots = lambda: paths._dedupe([base])
            try:
                best = paths.default_docs_dir("stellaris")
            finally:
                paths._document_roots = original
                paths._ROOTS_CACHE["value"] = None
            self.assertEqual(best.name, "Stellaris Beta")


if __name__ == "__main__":
    unittest.main()
