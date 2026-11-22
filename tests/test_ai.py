# -*- coding: utf-8 -*-
"""AI 分析模块的离线回归测试（不联网、不调用真实接口）。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smm import ai


class SettingsTest(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = ai.AiSettings(tmp)
            self.assertFalse(settings.snapshot()["configured"])
            self.assertEqual(settings.base_url, ai.DEFAULT_BASE)
            self.assertEqual(settings.model, ai.DEFAULT_MODEL)
            snap = settings.save(api_key="sk-test", base_url="https://api.example.com/v1", model="m1")
            self.assertTrue(snap["configured"])
            self.assertNotIn("api_key", snap)
            again = ai.AiSettings(tmp)
            self.assertEqual(again.model, "m1")
            self.assertEqual(again.base_url, "https://api.example.com/v1")
            self.assertTrue(again.clear()["configured"] is False)

    def test_plain_http_endpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = ai.AiSettings(tmp)
            with self.assertRaises(ai.AiError):
                settings.save(base_url="http://api.example.com")

    def test_local_http_endpoint_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = ai.AiSettings(tmp)
            settings.save(base_url="http://127.0.0.1:11434/v1")
            self.assertTrue(settings.base_url.startswith("http://127.0.0.1"))


class ParseTest(unittest.TestCase):
    def test_parse_comments_extracts_bodies(self):
        page = (
            '<div class="commentthread_comment_text" id="commenttext_1">'
            '<div class="commenttext">需要放在 UI Overhaul 之前<br>否则会冲突</div></div>'
            '<div class="commentthread_comment_text" id="commenttext_2">'
            '<div class="commenttext">求更新 3.14</div></div>'
        )
        rows = ai.parse_comments(page)
        self.assertEqual(len(rows), 2)
        self.assertIn("UI Overhaul", rows[0])
        self.assertIn("3.14", rows[1])

    def test_parse_comments_on_empty_page(self):
        self.assertEqual(ai.parse_comments(""), [])
        self.assertEqual(ai.parse_comments(None), [])

    def test_extract_json_tolerates_fences(self):
        text = '```json\n{"summary":"ok","load_order":["放最底部"],"compatibility":[],"notices":["需新档"]}\n```'
        data = ai.extract_json(text)
        self.assertEqual(data["summary"], "ok")
        self.assertEqual(data["load_order"], ["放最底部"])
        self.assertEqual(data["notices"], ["需新档"])

    def test_extract_json_rejects_garbage(self):
        with self.assertRaises(ai.AiError):
            ai.extract_json("抱歉，我无法判断。")

    def test_build_messages_mentions_inputs(self):
        messages = ai.build_messages("测试MOD", "必须在核心之前", ["放底部才生效"])
        joined = "\n".join(item["content"] for item in messages)
        self.assertIn("测试MOD", joined)
        self.assertIn("必须在核心之前", joined)
        self.assertIn("放底部才生效", joined)
        self.assertEqual(messages[0]["role"], "system")


class AnalyzeTest(unittest.TestCase):
    def test_analyze_uses_model_output_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ai.AiService(tmp)
            service.settings.save(api_key="sk-test")
            calls = []

            def fake_chat(messages, timeout=90):
                calls.append(messages)
                return json.dumps({
                    "summary": "需要放在最底部",
                    "load_order": ["放在 UI Overhaul 之后（描述）"],
                    "compatibility": [],
                    "notices": ["需要新开档（评论）"],
                }), {"total_tokens": 12}

            service._chat = fake_chat
            mod = {"id": "m1", "name": "测试MOD", "steam_id": "", "workshop_description": "说明文字"}
            result = service.analyze([mod])
            self.assertTrue(result["ok"])
            item = result["items"][0]
            self.assertTrue(item["ok"])
            self.assertEqual(item["load_order"], ["放在 UI Overhaul 之后（描述）"])
            self.assertEqual(len(calls), 1)
            # 第二次命中缓存，不再调用模型
            again = service.analyze_one(mod)
            self.assertTrue(again.get("cached"))
            self.assertEqual(len(calls), 1)

    def test_analyze_without_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ai.AiService(tmp)
            with self.assertRaises(ai.AiError):
                service.analyze([{"id": "m1", "name": "x"}])

    def test_analyze_without_text_reports_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ai.AiService(tmp)
            service.settings.save(api_key="sk-test")
            service._chat = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用模型"))
            item = service.analyze_one({"id": "m1", "name": "空MOD"}, scope="description")
            self.assertFalse(item["ok"])
            self.assertIn("没有可分析的文本", item["error"])


if __name__ == "__main__":
    unittest.main()

class EmptyKeyTest(unittest.TestCase):
    """保存成功后输入框会被清空，空值回传不能把已存的 key 抹掉。"""

    def test_empty_save_keeps_key(self):
        d = tempfile.mkdtemp()
        st = ai.AiSettings(d)
        st.save(api_key="sk-keep-me")
        st.save(api_key="")
        self.assertTrue(st.snapshot()["configured"])
        self.assertEqual(st.api_key, "sk-keep-me")
        self.assertTrue(ai.AiSettings(d).snapshot()["configured"])

    def test_clear_still_works(self):
        d = tempfile.mkdtemp()
        st = ai.AiSettings(d)
        st.save(api_key="sk-keep-me")
        st.clear()
        self.assertFalse(ai.AiSettings(d).snapshot()["configured"])

    def test_test_connection_requires_key(self):
        d = tempfile.mkdtemp()
        svc = ai.AiService(d)
        with self.assertRaises(ai.AiError):
            svc.test_connection()

    def test_test_connection_uses_chat(self):
        d = tempfile.mkdtemp()
        svc = ai.AiService(d)
        svc.settings.save(api_key="sk-x")
        seen = {}

        def fake_chat(messages, timeout=90, max_tokens=900, json_mode=True):
            seen["n"] = len(messages)
            return {"choices": [{"message": {"content": "可用"}}]}

        svc._chat = fake_chat
        out = svc.test_connection()
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "可用")
        self.assertGreaterEqual(seen["n"], 1)


class HttpStatusTest(unittest.TestCase):
    """AI 接口返回 401/403 时，用户看到的是可操作的提示，而不是「服务器返回 HTTP 401」。"""

    def _svc(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ai401-")
        svc = ai.AiService(tmp)
        svc.settings.save(api_key="sk-bad", base_url="https://api.deepseek.com", model="deepseek-chat")
        return svc

    def test_401_maps_to_key_hint(self):
        import urllib.error
        svc = self._svc()
        err = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 401,
                                     "Unauthorized", {}, None)
        with mock.patch.object(ai.net, "open_url", side_effect=err):
            with self.assertRaises(ai.AiError) as ctx:
                svc.test_connection()
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertIn("密钥", msg)
        self.assertNotIn("服务器返回 HTTP", msg)

    def test_429_maps_to_rate_limit(self):
        import urllib.error
        svc = self._svc()
        err = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 429,
                                     "Too Many Requests", {}, None)
        with mock.patch.object(ai.net, "open_url", side_effect=err):
            with self.assertRaises(ai.AiError) as ctx:
                svc.test_connection()
        self.assertIn("限流", str(ctx.exception))


class FilesAndSchemaTest(unittest.TestCase):
    def test_scan_mod_files_reads_descriptor(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "common", "defines"))
            with open(os.path.join(tmp, "descriptor.mod"), "w", encoding="utf-8") as fh:
                fh.write('name="Test Mod"\nversion="1.2"\nsupported_version="4.5.*"\n'
                         'dependencies={"Alpha"}\ntags={"Gameplay"}\n')
            info = ai.scan_mod_files({"dir": tmp})
        self.assertEqual(info["descriptor"]["version"], "1.2")
        self.assertEqual(info["descriptor"]["supported_version"], "4.5.*")
        self.assertIn("Alpha", info["descriptor"]["dependencies"])
        self.assertIn("common", info["overrides"])
        self.assertEqual(info["error"], "")

    def test_scan_mod_files_missing_dir(self):
        info = ai.scan_mod_files({"dir": r"Z:\nope\missing"})
        self.assertTrue(info["error"])

    def test_build_messages_includes_files_and_order(self):
        msgs = ai.build_messages(
            "Mod", "desc", [], {"files": 12, "top": ["common"], "descriptor": {"version": "1.0"}},
            {"order": ["1: A", "2: B"], "conflicts": "3 处定义冲突"})
        body = msgs[1]["content"]
        self.assertIn("顶层目录", body)
        self.assertIn("1: A", body)
        self.assertIn("3 处定义冲突", body)

    def test_extract_json_keeps_verdict_and_position(self):
        out = ai.extract_json('{"summary":"s","verdict":"needs_patch","position":"7",'
                              '"neighbors":["前：A"],"requires":["B"]}')
        self.assertEqual(out["verdict"], "needs_patch")
        self.assertEqual(out["position"], 7)
        self.assertEqual(out["neighbors"], ["前：A"])
        self.assertEqual(out["requires"], ["B"])

    def test_extract_json_rejects_unknown_verdict(self):
        out = ai.extract_json('{"summary":"s","verdict":"maybe"}')
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["position"], 0)

class ConflictTest(unittest.TestCase):
    def _svc(self, reply):
        import tempfile
        svc = ai.AiService(tempfile.mkdtemp())
        svc.settings.save(api_key="sk-test")
        svc._chat = lambda *a, **k: reply
        return svc

    def test_parses_items_and_actions(self):
        reply = ('{"summary":"3 条冲突里只有 1 条需要处理","items":['
                 '{"path":"common/defines/x.txt","severity":"critical","verdict":"reorder","advice":"把 B 放到 A 之后","mods":["A","B"]},'
                 '{"path":"gfx/interface/y.dds","severity":"info","verdict":"ignore","advice":"纯贴图覆盖","mods":["C"]}],'
                 '"actions":[{"type":"move","mod":"B","reason":"需要晚加载"}]}')
        out = self._svc(reply).analyze_conflicts([
            {"path": "common/defines/x.txt", "kind": "file", "winner": "B",
             "mods": [{"name": "A", "rank": 1}, {"name": "B", "rank": 2}]},
            {"path": "gfx/interface/y.dds", "kind": "file", "winner": "C", "mods": [{"name": "C", "rank": 3}]},
        ], order=["A", "B", "C"])
        self.assertEqual(out["scanned"], 2)
        self.assertEqual(len(out["items"]), 2)
        self.assertEqual(out["items"][0]["severity"], "critical")
        self.assertEqual(out["items"][0]["verdict"], "reorder")
        self.assertEqual(out["items"][1]["verdict"], "ignore")
        self.assertEqual(out["actions"][0]["type"], "move")
        self.assertTrue(out["summary"])

    def test_bad_enum_falls_back(self):
        reply = '{"summary":"s","items":[{"path":"p","severity":"huge","verdict":"nope","advice":"a","mods":[]}]}'
        out = self._svc(reply).analyze_conflicts([{"path": "p", "mods": []}])
        self.assertEqual(out["items"][0]["severity"], "info")
        self.assertEqual(out["items"][0]["verdict"], "keep")

    def test_empty_conflicts_raises(self):
        with self.assertRaises(ai.AiError):
            self._svc("{}").analyze_conflicts([])

    def test_no_key_raises(self):
        import tempfile
        svc = ai.AiService(tempfile.mkdtemp())
        with self.assertRaises(ai.AiError):
            svc.analyze_conflicts([{"path": "p", "mods": []}])

class ExtractRulesTest(unittest.TestCase):
    def _service(self, tmp, reply):
        svc = ai.AiService(tmp)
        svc.settings.save(api_key="sk-test")
        calls = []

        def fake_chat(messages, timeout=90, max_tokens=900, json_mode=True):
            calls.append(messages)
            return reply

        svc._chat = fake_chat
        svc._calls = calls
        return svc

    def test_rules_are_parsed_and_cached(self):
        reply = json.dumps({
            "rules": [
                {"mod": "Tidy Tradition", "kind": "bottom", "target": "",
                 "quote": "must load last", "confidence": 0.9},
                {"mod": "More Events Mod", "kind": "incompatible", "target": "Tidy Tradition",
                 "quote": "incompatible with Tidy Tradition", "confidence": 0.8},
                {"mod": "Ghost", "kind": "top", "target": "", "quote": "x", "confidence": 0.9},
                {"mod": "Tidy Tradition", "kind": "sideways", "target": "", "quote": "x", "confidence": 0.9},
            ],
            "notes": ["ignore me"],
        })
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, reply)
            mods = [
                {"id": "a1", "name": "Tidy Tradition", "description": "must load last"},
                {"id": "a2", "name": "More Events Mod", "description": "incompatible with Tidy Tradition"},
            ]
            rules, _notes, cached = svc.extract_rules(mods)
            kinds = sorted(row["kind"] for row in rules)
            self.assertEqual(kinds, ["bottom", "incompatible"])
            self.assertEqual(cached, 0)
            self.assertTrue(Path(tmp, "rules-cache.json").exists())

            again, _notes2, cached2 = svc.extract_rules(mods)
            self.assertEqual(cached2, 2)
            self.assertEqual(len(again), 2)
            self.assertEqual(len(svc._calls), 1)  # 第二次不再调模型

    def test_changed_description_invalidates_cache(self):
        reply = json.dumps({"rules": [{"mod": "A", "kind": "top", "target": "",
                                       "quote": "load first", "confidence": 0.9}]})
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, reply)
            svc.extract_rules([{"id": "a", "name": "A", "description": "load first"}])
            svc.extract_rules([{"id": "a", "name": "A", "description": "changed text"}])
            self.assertEqual(len(svc._calls), 2)

    def test_low_confidence_is_filtered(self):
        reply = json.dumps({"rules": [{"mod": "A", "kind": "top", "target": "",
                                       "quote": "maybe", "confidence": 0.2}]})
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, reply)
            rules, _notes, _cached = svc.extract_rules([{"id": "a", "name": "A", "description": "x"}])
            self.assertEqual(rules, [])

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = ai.AiService(tmp)
            with self.assertRaises(ai.AiError):
                svc.extract_rules([{"id": "a", "name": "A", "description": "x"}])

class ExplainCrashTest(unittest.TestCase):
    def _service(self, tmp, reply):
        svc = ai.AiService(tmp)
        svc.settings.save(api_key="sk-test")
        svc._chat = lambda *a, **k: reply
        return svc

    def _report(self):
        return {
            "mapped": [
                {"mod_id": "a1", "name": "More Events Mod", "count": 12,
                 "kinds": {"unknown_trigger": 9, "duplicate": 3},
                 "samples": ["ERROR mod/ugc_727000451/common/x.txt: unknown trigger foo"]},
            ],
            "unmapped": [{"file": "error.log", "line": "ERROR something broke"}],
            "dumps": ["crash-1.dmp"],
            "total_errors": 13,
        }

    def test_result_is_normalised(self):
        reply = json.dumps({
            "verdict": "mod",
            "summary": "更可能是 More Events Mod 的触发器过期",
            "suspects": [
                {"mod": "More Events Mod", "reason": "9 条 unknown trigger", "confidence": 0.86},
                {"mod": "", "reason": "空名会被保留但截断", "confidence": "bad"},
            ],
            "fixes": ["先停用 More Events Mod", "更新到最新版本", "", "x" * 300],
        })
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, reply)
            data = svc.explain_crash(self._report())
        self.assertEqual(data["verdict"], "mod")
        self.assertEqual(len(data["suspects"]), 2)
        self.assertEqual(data["suspects"][0]["confidence"], 0.86)
        self.assertEqual(data["suspects"][1]["confidence"], 0.5)
        self.assertEqual(len(data["fixes"]), 3)
        self.assertLessEqual(len(data["fixes"][2]), 160)
        self.assertEqual(data["scanned"], 1)

    def test_unknown_verdict_falls_back(self):
        reply = json.dumps({"verdict": "nonsense", "summary": "x", "suspects": [], "fixes": []})
        with tempfile.TemporaryDirectory() as tmp:
            data = self._service(tmp, reply).explain_crash(self._report())
        self.assertEqual(data["verdict"], "unknown")

    def test_empty_report_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, "{}")
            with self.assertRaises(ai.AiError):
                svc.explain_crash({"mapped": [], "unmapped": []})

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = ai.AiService(tmp)
            with self.assertRaises(ai.AiError):
                svc.explain_crash(self._report())
