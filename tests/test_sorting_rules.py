"""规则排序引擎：把说明里抽出的规则落到实际顺序上。"""
from __future__ import annotations

import unittest

from smm import sorting


def mods(*names):
    return [{"id": name.lower(), "name": name, "enabled": True} for name in names]


def names(ordered):
    return [mod["name"] for mod in ordered]


class ApplyRulesTest(unittest.TestCase):
    def test_top_and_bottom_move_to_edges(self):
        items = mods("A", "B", "C", "D")
        rules = [
            {"mod": "D", "kind": "top", "target": "", "confidence": 0.9},
            {"mod": "A", "kind": "bottom", "target": "", "confidence": 0.9},
        ]
        ordered, applied, warnings = sorting.apply_rules(items, rules)
        self.assertEqual(names(ordered), ["D", "B", "C", "A"])
        self.assertEqual([row["kind"] for row in applied], ["top", "bottom"])
        self.assertEqual(warnings, [])

    def test_before_and_after_are_relative(self):
        items = mods("A", "B", "C", "D")
        rules = [
            {"mod": "D", "kind": "before", "target": "B", "confidence": 0.8},
            {"mod": "A", "kind": "after", "target": "C", "confidence": 0.8},
        ]
        ordered, _applied, warnings = sorting.apply_rules(items, rules)
        order = names(ordered)
        self.assertLess(order.index("D"), order.index("B"))
        self.assertLess(order.index("C"), order.index("A"))
        self.assertEqual(warnings, [])

    def test_incompatible_and_requires_become_warnings(self):
        items = mods("Alpha", "Beta", "Gamma")
        rules = [
            {"mod": "Alpha", "kind": "incompatible", "target": "Beta", "confidence": 0.9},
            {"mod": "Gamma", "kind": "requires", "target": "Beta", "confidence": 0.9},
        ]
        _ordered, applied, warnings = sorting.apply_rules(items, rules)
        self.assertEqual(applied, [])
        self.assertEqual(len(warnings), 2)
        self.assertIn("不兼容", warnings[0])
        self.assertIn("需要", warnings[1])

    def test_unknown_mod_and_target_are_reported(self):
        items = mods("A", "B")
        rules = [
            {"mod": "NotHere", "kind": "top", "target": "", "confidence": 0.9},
            {"mod": "A", "kind": "before", "target": "Missing", "confidence": 0.9},
        ]
        _ordered, applied, warnings = sorting.apply_rules(items, rules)
        self.assertEqual(applied, [])
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("找不到" in text for text in warnings))

    def test_unknown_kind_and_low_confidence_are_skipped(self):
        items = mods("A", "B")
        rules = [
            {"mod": "B", "kind": "sideways", "target": "", "confidence": 0.9},
            {"mod": "B", "kind": "top", "target": "", "confidence": 0.2},
        ]
        ordered, applied, warnings = sorting.apply_rules(items, rules, min_confidence=0.45)
        self.assertEqual(names(ordered), ["A", "B"])
        self.assertEqual(applied, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("未知规则类型", warnings[0])

    def test_name_matching_is_case_insensitive_and_tolerates_affixes(self):
        items = [{"id": "x1", "name": "Tidy Tradition", "enabled": True},
                 {"id": "x2", "name": "More Events Mod", "enabled": True}]
        rules = [{"mod": "tidy tradition", "kind": "bottom", "target": "", "confidence": 0.9}]
        ordered, applied, _warnings = sorting.apply_rules(items, rules)
        self.assertEqual(names(ordered), ["More Events Mod", "Tidy Tradition"])
        self.assertEqual(applied[0]["mod_id"], "x1")

    def test_empty_inputs_are_safe(self):
        self.assertEqual(sorting.apply_rules([], [{"mod": "A", "kind": "top"}]), ([], [], []))
        self.assertEqual(sorting.apply_rules(mods("A"), None)[0][0]["name"], "A")


if __name__ == "__main__":
    unittest.main()
