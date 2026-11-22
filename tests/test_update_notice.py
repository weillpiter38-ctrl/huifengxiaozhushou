# -*- coding: utf-8 -*-
"""0.9.66 regression tests: update notice + no false error + stable token."""
import tempfile
import unittest
from pathlib import Path

from smm import server, updater
from smm.api import APP_VERSION
from run import load_token

BASE = Path(__file__).resolve().parent.parent
WEB = BASE / "web"


class UpdateCheckTest(unittest.TestCase):
    def setUp(self):
        self.orig = updater._fetch_json
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        updater._fetch_json = self.orig
        self.tmp.cleanup()

    def test_broken_primary_manifest_uses_mirror(self):
        calls = []

        def fake(url, timeout=12):
            calls.append(url)
            if "raw.githubusercontent.com" in url:
                raise ValueError("更新清单不是有效的 JSON。")
            return {"version": "0.9.70", "notes": "ok", "changelog": ["x"]}

        updater._fetch_json = fake
        result = updater.check(self.tmp.name, "0.9.66")
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "0.9.70")
        self.assertGreater(len(calls), 1)

    def test_picks_highest_version_not_first_mirror(self):
        """A stale CDN cache must not hide a newer release on the primary URL."""
        def fake(url, timeout=12):
            if "raw.githubusercontent.com" in url:
                return {"version": "0.9.70", "changelog": ["new"]}
            return {"version": "0.9.59", "changelog": ["stale"]}

        updater._fetch_json = fake
        result = updater.check(self.tmp.name, "0.9.66")
        self.assertEqual(result["latest"], "0.9.70")
        self.assertEqual(result["changelog"], ["new"])
        self.assertTrue(result["available"])

    def test_all_sources_fail_returns_offline_instead_of_raising(self):
        def fake(url, timeout=12):
            raise updater.NetworkError("offline")

        updater._fetch_json = fake
        result = updater.check(self.tmp.name, "0.9.66")
        self.assertTrue(result["ok"])
        self.assertTrue(result["offline"])
        self.assertFalse(result["available"])
        self.assertIn("无法连接", result["message"])

    def test_all_sources_invalid_json_is_not_an_error(self):
        def fake(url, timeout=12):
            raise ValueError("更新清单不是有效的 JSON。")

        updater._fetch_json = fake
        result = updater.check(self.tmp.name, "0.9.66")
        self.assertTrue(result["offline"])
        self.assertFalse(result["available"])


class TokenAndCacheTest(unittest.TestCase):
    def test_token_is_stable_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = load_token(Path(tmp))
            second = load_token(Path(tmp))
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 16)
            self.assertTrue((Path(tmp) / "api-token").is_file())

    def test_query_string_still_serves_static_files(self):
        self.assertEqual(urllib_parse("/app.js?v=0.9.66"), "/app.js")
        self.assertIn(urllib_parse("/?v=1"), server.STATIC)

    def test_index_uses_versioned_assets(self):
        html = (WEB / "index.html").read_text(encoding="utf-8-sig")
        self.assertIn(f"/app.js?v={APP_VERSION}", html)
        self.assertIn(f"/i18n-data.js?v={APP_VERSION}", html)
        self.assertIn(f"/forge.js?v={APP_VERSION}", html)
        self.assertIn(f"/forge.css?v={APP_VERSION}", html)
        # 1.6 起 40K 外观层接管样式：style.css 不再作为独立 <link>，而是由 forge.css 分层引入
        self.assertNotIn('<link rel="stylesheet" href="/style.css', html)
        forge = (WEB / "forge.css").read_text(encoding="utf-8-sig")
        self.assertIn(f"/style.css?v={APP_VERSION}", forge)


def urllib_parse(url):
    import urllib.parse
    return urllib.parse.urlsplit(url).path


class FrontendUpdateNoticeTest(unittest.TestCase):
    def test_prominent_dialog_exists(self):
        html = (WEB / "index.html").read_text(encoding="utf-8-sig")
        self.assertIn('id="new-version-dialog"', html)
        self.assertIn('class="dialog wide update-spotlight"', html)
        self.assertIn('id="btn-new-version-install"', html)
        self.assertIn('id="btn-update-badge"', html)

    def test_check_update_no_longer_uses_red_error_toast(self):
        js = (WEB / "app.js").read_text(encoding="utf-8-sig")
        block = js[js.index("async function checkAppUpdate"):js.index("async function installAppUpdate")]
        self.assertNotIn("toast(err.message, 'err')", block)
        self.assertNotIn("toast(`发现新版本", block)
        self.assertIn("showUpdateDialog(data)", block)
        self.assertIn("data.offline", block)
        self.assertIn("showNotice('暂时无法检查更新'", block)

    def test_notice_never_downgrades_to_toast(self):
        js = (WEB / "app.js").read_text(encoding="utf-8-sig")
        block = js[js.index("function showUpdateDialog"):js.index("async function checkAppUpdate")]
        self.assertIn("dialog.showModal()", block)
        self.assertNotIn("toast(", block)

    def test_stale_token_self_heals(self):
        js = (WEB / "app.js").read_text(encoding="utf-8-sig")
        self.assertIn("smm.tokenReloaded", js)
        self.assertIn("location.reload()", js)

    def test_version_is_consistent(self):
        """index.html 显示的版本必须与后端 APP_VERSION 一致（以后改版本不用改测试）。"""
        self.assertRegex(APP_VERSION, r"^\d+\.\d+\.\d+$")
        html = (WEB / "index.html").read_text(encoding="utf-8-sig")
        self.assertIn(f"版本 {APP_VERSION}", html)


if __name__ == "__main__":
    unittest.main()
