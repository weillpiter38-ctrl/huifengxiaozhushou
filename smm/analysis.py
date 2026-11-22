"""Definition-level conflict analysis for Stellaris Clausewitz scripts.

Heuristics:
- ``common/defines``: identity is ``Category.CONSTANT``.
- ``common/on_actions``: additive hooks, skipped.
- events: blocks repeat a type name, so the identity is the inner ``id``.
- component templates: identity is the inner ``key``.
- other ``common`` files: the top-level key is the identity.
- ``.yml``: localisation keys are compared separately.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

BLOCK_START = re.compile(r"^([A-Za-z_@][A-Za-z0-9_.\-]*)\s*=\s*\{")
ID_LINE = re.compile(r'^\s*id\s*=\s*"?([A-Za-z0-9_.\-]+)"?')
KEY_LINE = re.compile(r'^\s*key\s*=\s*"([^"]+)"')
CONST_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")
YML_LINE = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*:\s*\d*\s*"')
REPLACE_PATH = re.compile(r'^\s*replace_path\s*=\s*"([^"]+)"', re.M)

IGNORED_KEYS = {
    "option", "trigger", "effect", "potential", "allow", "weight", "modifier",
    "ai_weight", "prerequisites", "limit", "hidden", "custom_tooltip",
    "desc", "picture", "icon", "sound", "fire_only_once", "abort", "abort_trigger",
    "if", "else", "else_if", "while", "random_list", "random", "weighted_random_list",
    "switch", "for_each", "count", "tooltip", "custom_description",
}
MAX_FILE_BYTES = 3 * 1024 * 1024
MAX_SNIPPET_CHARS = 1200
MAX_SNIPPET_KEYS = 120


def _strip_comment(line):
    cut = line.find("#")
    return line if cut < 0 else line[:cut]


def _blocks(text):
    depth = 0
    current = None
    body = []
    blocks = []
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if depth == 0:
            match = BLOCK_START.match(line)
            current = match.group(1) if match else None
            body = [line] if current else []
        elif current is not None:
            body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            depth = 0
            if current is not None:
                blocks.append((current, body))
            current = None
            body = []
    return blocks


def _block_identity(key, body, repeated):
    inner_key = None
    inner_id = None
    for line in body:
        if inner_key is None:
            match = KEY_LINE.match(line)
            if match:
                inner_key = match.group(1)
        if inner_id is None:
            match = ID_LINE.match(line)
            if match:
                inner_id = match.group(1)
    if inner_key:
        return inner_key
    if inner_id:
        return inner_id
    return None if repeated else key


def _iter_entries(text, kind):
    """Yield ((kind, identity), block_lines) for one Clausewitz file."""
    blocks = _blocks(text)
    if not blocks:
        return
    counts = {}
    for key, _body in blocks:
        counts[key] = counts.get(key, 0) + 1
    for key, body in blocks:
        if key.lower() in IGNORED_KEYS or key.startswith("@"):
            continue
        if kind == "defines":
            for line in body:
                match = CONST_LINE.match(line)
                if match:
                    yield ("defines", f"{key}.{match.group(1)}"), body
            continue
        name = _block_identity(key, body, counts[key] > 1)
        if name:
            yield ("script", name), body


def _parse_entry(definitions, entry, body):
    key = entry
    definitions[key] = definitions.get(key, 0) + 1


def _iter_files(directory):
    for root, _dirs, files in os.walk(directory):
        for name in files:
            lower = name.lower()
            full = Path(root) / name
            posix = full.as_posix().lower()
            if lower.endswith(".txt"):
                if "/common/" not in posix and "/events/" not in posix:
                    continue
                yield full, "defines" if "/defines/" in posix else "script"
            elif lower.endswith(".yml"):
                if "/localisation/" not in posix:
                    continue
                yield full, "loc"


def _parse_localisation(text, definitions):
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        match = YML_LINE.match(line)
        if match:
            key = ("loc", match.group(1))
            definitions[key] = definitions.get(key, 0) + 1


def _snippets(mods, wanted, ranks=None):
    """Second pass: collect block text only for conflicting identities."""
    ranks = ranks or {}
    result = {}
    for mod in mods:
        directory = mod.get("dir")
        if not directory or not os.path.isdir(directory):
            continue
        for path, kind in _iter_files(directory):
            posix = path.as_posix().lower()
            if "/on_actions/" in posix:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            relative = os.path.relpath(path, directory).replace("\\", "/")
            for entry, body in _iter_entries(text, kind):
                if entry not in wanted:
                    continue
                bucket = result.setdefault(f"{entry[0]}:{entry[1]}", [])
                existing = next((item for item in bucket if item["mod_id"] == mod["id"]), None)
                if existing is not None:
                    # A later file inside the same MOD replaces the earlier one.
                    if relative.lower() > existing["file"].lower():
                        existing["file"] = relative
                        existing["text"] = "\n".join(body)[:MAX_SNIPPET_CHARS]
                    continue
                bucket.append({
                    "mod_id": mod["id"],
                    "mod": mod["name"],
                    "file": relative,
                    "rank": ranks.get(mod["id"]),
                    "enabled": bool(mod.get("enabled")),
                    "text": "\n".join(body)[:MAX_SNIPPET_CHARS],
                })
    for bucket in result.values():
        bucket.sort(key=lambda item: (item.get("rank") is None, item.get("rank") or 0))
    return result


def _ranks_for(mods, payload):
    """Load rank per MOD, straight from what the game reads (dlc_load.json order)."""
    order = payload.get("load_order") or {}
    ranks = {}
    for row in order.get("entries") or []:
        mod_id = row.get("mod_id")
        if mod_id is not None and row.get("rank") is not None:
            ranks[mod_id] = row["rank"]
    if not ranks:
        # Older payloads (or a hand-built fixture) have no load_order block:
        # fall back to the playset order, which is what the launcher normally writes.
        ordered = sorted(mods, key=lambda item: item.get("position") or 0)
        ranks = {mod["id"]: index for index, mod in enumerate(ordered)}
    return ranks, order.get("source") or "position"


def analyze(catalog, limit=400):
    started = time.time()
    payload = catalog.scan()
    mods = payload["mods"]
    ranks, basis = _ranks_for(mods, payload)
    enabled_of = {mod["id"]: bool(mod.get("enabled")) for mod in mods}
    owners = {}
    # (kind, key) -> {mod_id: relative path of the LAST file in that MOD defining it}
    # The game loads a MOD's files in filename order, so a definition repeated inside
    # one MOD is decided by the later file, not by the MOD order.
    sources = {}
    replace_map = {}
    total_defs = 0

    for mod in mods:
        directory = mod.get("dir")
        definitions = {}
        if directory and os.path.isdir(directory):
            for path, kind in _iter_files(directory):
                posix = path.as_posix().lower()
                if "/on_actions/" in posix:
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                relative = os.path.relpath(path, directory).replace("\\", "/")
                file_defs = {}
                if kind == "loc":
                    _parse_localisation(text, file_defs)
                else:
                    for entry, body in _iter_entries(text, kind):
                        _parse_entry(file_defs, entry, body)
                for key in file_defs:
                    definitions[key] = definitions.get(key, 0) + 1
                    bucket = sources.setdefault(key, {})
                    seen = bucket.get(mod["id"])
                    if seen is None or relative.lower() > seen.lower():
                        bucket[mod["id"]] = relative
            descriptor = Path(directory) / "descriptor.mod"
            if descriptor.is_file():
                try:
                    raw = descriptor.read_text(encoding="utf-8-sig", errors="replace")
                    replace_map[mod["id"]] = REPLACE_PATH.findall(raw)
                except OSError:
                    pass
        total_defs += len(definitions)
        for kind_name, name in definitions:
            owners.setdefault((kind_name, name), set()).add(mod["id"])

    def advice_for(kind, winner, winner_file, basis_name):
        where = {"dlc_load": "dlc_load.json 加载顺序",
                 "position": "播放集排列顺序",
                 "filename": "文件名顺序"}.get(basis_name, basis_name)
        tail = f"按 {where}，最后加载的「{winner}」覆盖前面的定义。"
        if winner_file:
            tail += f"（{winner_file}）"
        if kind == "loc":
            return "本地化键被多个 MOD 覆盖，通常无害；" + tail
        if kind == "defines":
            return "数值常量被重定义，" + tail + "确认数值是否符合预期。"
        return "同一脚本实体被重定义，" + tail + "如需其他 MOD 生效，请把它移到加载顺序更靠后的位置。"

    def order_key(mod_id):
        # Only MODs the game actually loads can win; among them the last loaded one
        # wins, and inside one MOD the later filename wins.
        rank = ranks.get(mod_id)
        return (0 if rank is None else 1, rank if rank is not None else 0,
                (sources.get(key, {}).get(mod_id) or "").lower())

    names = {mod["id"]: mod["name"] for mod in mods}
    conflicts = []
    for key in owners:
        kind, name = key
        mod_ids = owners[key]
        if len(mod_ids) < 2:
            continue
        winner_id = max(mod_ids, key=order_key)
        rows = []
        for mod_id in sorted(mod_ids, key=order_key):
            rows.append({
                "mod": names.get(mod_id, ""),
                "mod_id": mod_id,
                "file": sources.get(key, {}).get(mod_id, ""),
                "rank": ranks.get(mod_id),
                "enabled": enabled_of.get(mod_id, False),
            })
        winner_file = sources.get(key, {}).get(winner_id, "")
        conflicts.append({
            "kind": kind,
            "key": name,
            "mods": [names[item] for item in mod_ids],
            "mod_ids": sorted(mod_ids),
            "winner": names.get(winner_id, ""),
            "winner_id": winner_id,
            "winner_file": winner_file,
            "winner_rank": ranks.get(winner_id),
            "basis": basis,
            "mod_rows": rows,
            "advice": advice_for(kind, names.get(winner_id, ""), winner_file, basis),
        })
    script = [item for item in conflicts if item["kind"] in ("script", "defines")]
    localisation = [item for item in conflicts if item["kind"] == "loc"]
    script.sort(key=lambda item: (-len(item["mod_ids"]), item["key"]))
    localisation.sort(key=lambda item: (-len(item["mod_ids"]), item["key"]))

    wanted = {(item["kind"], item["key"]) for item in script[:MAX_SNIPPET_KEYS]}
    snippets = _snippets(mods, wanted, ranks) if wanted else {}

    per_mod = {}
    for mod in mods:
        per_mod[mod["id"]] = sum(1 for item in script if mod["id"] in item["mod_ids"])

    replace_paths = []
    for mod in mods:
        for value in replace_map.get(mod["id"], []):
            replace_paths.append({"mod": mod["name"], "path": value})

    return {
        "ok": True,
        "analysis": {
            "definitions": total_defs,
            "conflicts": script[:limit],
            "loc_conflicts": localisation[:limit],
            "conflict_total": len(script),
            "loc_conflict_total": len(localisation),
            "per_mod": per_mod,
            "snippets": snippets,
            "replace_paths": replace_paths[:100],
            "load_order": {"source": basis,
                           "mismatch": bool((payload.get("load_order") or {}).get("mismatch"))},
            "elapsed_ms": int((time.time() - started) * 1000),
        },
    }

