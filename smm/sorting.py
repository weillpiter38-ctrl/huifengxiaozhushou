"""Heuristic load-order sorting for Stellaris mods.

Rules (highest priority first):
1. ``~`` prefix goes to the top.
2. ``!`` prefix goes to the bottom; more ``!`` means further down.
3. Category tiers: framework, overhaul, UI, species/art, gameplay, localisation, patches.
4. ``dependencies`` must be loaded before the mod that lists them.
5. Original relative order is kept inside one tier.

Three modes are supported: ``community`` (full rule set), ``stable``
(keep the current order, only fix dependency direction), and ``smart``
(dependency-first topological order).
"""
from __future__ import annotations

import re

TILDE = "~"
BANG = "!"


def _has(text, words):
    for word in words:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text):
            return True
    return False


def classify(mod):
    name = (mod.get("name") or "").lower()
    tags = " ".join(str(tag) for tag in (mod.get("tags") or [])).lower()
    stripped = name.lstrip()
    tildes = len(stripped) - len(stripped.lstrip(TILDE))
    bangs = len(stripped) - len(stripped.lstrip(BANG))
    if tildes:
        return (0, "顶层（~ 前缀）")
    if bangs:
        return (90 + min(bangs, 6), f"底层（{'!' * bangs} 前缀）")
    if _has(name, ("汉化", "中文", "翻译", "localisation", "localization", "translation")) or "translation" in tags:
        return (70, "汉化 / 本地化")
    if _has(name, ("patch", "compat", "compatibility", "兼容", "补丁", "修复", "fix")):
        return (80, "兼容 / 修复补丁")
    if _has(name, ("framework", "core", "library", "lib", "基础", "前置", "依赖")) or _has(tags, ("framework", "utilities")):
        return (10, "框架 / 核心库")
    if _has(name, ("ui", "interface", "hud", "tooltip", "topbar", "outliner", "font", "icon")) or "interface" in tags:
        return (30, "UI / 界面")
    if _has(name, ("portrait", "species", "shipset", "namelist", "flag", "graphics", "gfx", "物种")):
        return (40, "物种 / 美术")
    if _has(name, ("overhaul", "gigastructural", "giga", "acot", "nsc", "real space", "planetary diversity", "unofficial")) or "overhaul" in tags:
        return (20, "大型 MOD / 重制")
    return (50, "玩法 / 内容")


def _resolve_dependency(name, by_name):
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in by_name:
        return by_name[key]
    for candidate, mod_id in by_name.items():
        if key in candidate or candidate in key:
            return mod_id
    return None


def _fix_dependencies(ordered, by_name, by_id):
    for _round in range(40):
        index = {mod["id"]: i for i, mod in enumerate(ordered)}
        moved = False
        for position, mod in enumerate(list(ordered)):
            for dependency in mod.get("dependencies") or []:
                target_id = _resolve_dependency(dependency, by_name)
                if not target_id or target_id == mod["id"]:
                    continue
                target_position = index.get(target_id)
                if target_position is None or target_position < position:
                    continue
                dependency_mod = ordered.pop(target_position)
                ordered.insert(position, dependency_mod)
                moved = True
                break
            if moved:
                break
        if not moved:
            break
    return ordered


def _smart_order(mods, by_name, by_id):
    index = {mod["id"]: i for i, mod in enumerate(mods)}
    base = sorted(mods, key=lambda mod: (classify(mod)[0], index[mod["id"]]))
    remaining = list(base)
    ordered = []
    placed = set()
    while remaining:
        chosen = None
        for mod in remaining:
            pending = [_resolve_dependency(dep, by_name) for dep in (mod.get("dependencies") or [])]
            pending = [item for item in pending if item and item in by_id and item != mod["id"]]
            if all(item in placed for item in pending):
                chosen = mod
                break
        if chosen is None:
            chosen = remaining[0]
        remaining.remove(chosen)
        ordered.append(chosen)
        placed.add(chosen["id"])
    return ordered

def sort_mods(mods, mode="community"):
    """Return (ordered mods, {mod_id: reason})."""
    by_id = {mod["id"]: mod for mod in mods}
    by_name = {}
    for mod in mods:
        by_name[(mod.get("name") or "").strip().lower()] = mod["id"]
        if mod.get("display_name"):
            by_name.setdefault(mod["display_name"].strip().lower(), mod["id"])

    reasons = {mod["id"]: "保持原序" for mod in mods}
    if mode == "stable":
        ordered = list(mods)
    elif mode == "smart":
        ordered = _smart_order(mods, by_name, by_id)
        for mod in ordered:
            reasons[mod["id"]] = "依赖优先 / 按分类整理"
    else:
        keyed = []
        for index, mod in enumerate(mods):
            tier, reason = classify(mod)
            reasons[mod["id"]] = reason
            keyed.append((tier, index, mod))
        keyed.sort(key=lambda item: (item[0], item[1]))
        ordered = [item[2] for item in keyed]

    return _fix_dependencies(ordered, by_name, by_id), reasons


_RULE_KINDS = ("top", "bottom", "before", "after", "incompatible", "requires")


def _norm_ref(text):
    return (text or "").strip().casefold()


def _rule_ref_index(mods):
    by_id = {mod["id"]: mod for mod in mods}
    by_name = {}
    for mod in mods:
        by_name[_norm_ref(mod.get("name"))] = mod["id"]
        if mod.get("display_name"):
            by_name.setdefault(_norm_ref(mod["display_name"]), mod["id"])
    return by_id, by_name


def _match_ref(ref, mods, by_id, by_name):
    """Resolve a MOD name written in a description to a MOD in this playset."""
    key = _norm_ref(ref)
    if not key:
        return None
    if ref in by_id:
        return by_id[ref]
    if key in by_name:
        return by_id[by_name[key]]
    hits = [mod for mod in mods if key and key in _norm_ref(mod.get("name"))]
    if len(hits) == 1:
        return hits[0]
    hits = [mod for mod in mods if key and _norm_ref(mod.get("name")) in key]
    if len(hits) == 1:
        return hits[0]
    return None


def apply_rules(mods, rules, min_confidence=0.0):
    """Order MODs by rules extracted from their descriptions.

    Returns (ordered mods, applied rules, warnings).  The incoming order is the
    stable baseline: a rule only ever moves the MOD it names.
    """
    mods = list(mods)
    if not mods:
        return [], [], []
    by_id, by_name = _rule_ref_index(mods)
    ordered = list(mods)
    applied = []
    warnings = []

    def move_to_edge(mod, to_top):
        if mod not in ordered:
            return
        ordered.remove(mod)
        ordered.insert(0 if to_top else len(ordered), mod)

    def move_next_to(mod, target, after):
        if mod not in ordered or target not in ordered or mod is target:
            return
        ordered.remove(mod)
        position = ordered.index(target)
        ordered.insert(position + 1 if after else position, mod)

    parsed = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("kind") or "").strip().lower()
        if kind not in _RULE_KINDS:
            warnings.append(f"忽略未知规则类型：{rule.get('kind')}")
            continue
        try:
            confidence = float(rule.get("confidence", 1))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        if confidence < min_confidence:
            continue
        parsed.append((kind, rule, confidence))

    # top/bottom first, then relative placement, so a "before X" rule still
    # lands next to X after X itself was moved to the top.
    for phase in (("top", "bottom"), ("before", "after"), ("incompatible", "requires")):
        for kind, rule, confidence in parsed:
            if kind not in phase:
                continue
            mod = _match_ref(rule.get("mod"), mods, by_id, by_name)
            if mod is None:
                warnings.append(f"找不到规则里提到的 MOD：{rule.get('mod')}")
                continue
            target = None
            if kind in ("before", "after", "incompatible", "requires"):
                target = _match_ref(rule.get("target"), mods, by_id, by_name)
                if target is None:
                    warnings.append(
                        f"找不到 {mod.get('name')} 提到的 MOD：{rule.get('target')}"
                    )
                    continue
            if kind == "top":
                move_to_edge(mod, True)
            elif kind == "bottom":
                move_to_edge(mod, False)
            elif kind == "before":
                move_next_to(mod, target, False)
            elif kind == "after":
                move_next_to(mod, target, True)
            elif kind == "incompatible":
                warnings.append(
                    f"{mod.get('name')} 与 {target.get('name')} 声明不兼容，建议只启用其中一个"
                )
            elif kind == "requires":
                warnings.append(
                    f"{mod.get('name')} 需要 {target.get('name')} 一起启用"
                )
            if kind in ("top", "bottom", "before", "after"):
                applied.append({
                    "mod_id": mod["id"],
                    "mod": mod.get("name"),
                    "kind": kind,
                    "target": (target or {}).get("name", ""),
                    "quote": str(rule.get("quote") or "")[:80],
                    "confidence": confidence,
                })
    return ordered, applied, warnings
