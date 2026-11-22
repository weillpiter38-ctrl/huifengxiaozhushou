"""Parsing of Stellaris .mod / descriptor.mod files and version compatibility."""
from __future__ import annotations

import re

# A quoted value never spans lines: an unterminated quote in a damaged file must
# not swallow the following fields into the mod name.
_FIELD = r'^\s*{name}\s*=\s*"([^"\r\n]*)"'
_BLOCK = r"{name}\s*=\s*\{{(.*?)\}}"
# Paradox .mod files are UTF-8, but hand-edited ones are often saved as GBK/GB18030
# or (for JP/KR authors) CP932/CP949.  Decoding everything as UTF-8 with
# errors="replace" turned those names into U+FFFD mojibake, so try the plausible
# encodings in order and keep the first one that decodes without control junk.
_ENCODINGS = ("utf-8-sig", "gb18030", "cp932", "cp949", "cp1252")
_C1 = re.compile(r"[\u0080-\u009f\ufffd]")

# A real descriptor is a few hundred bytes. A damaged or hand-made one can hold a
# 2 MB "name" line or 50k dependencies; every byte of that used to travel into the
# bootstrap payload once per mod and once per conflict entry (a 9-mod library
# answered with 65 MB). Cap the file, the scalar fields and the lists here so the
# rest of the app can assume sane sizes.
MAX_FILE_BYTES = 1 << 20
MAX_FIELD = 200
MAX_ITEMS = 256
MAX_ITEM = 200


def _strip_comments(text):
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _field(text, name):
    match = re.search(_FIELD.format(name=re.escape(name)), text, re.M | re.I)
    return match.group(1)[:MAX_FIELD] if match else ""


def _block_items(text, name):
    match = re.search(_BLOCK.format(name=re.escape(name)), text, re.S | re.I)
    if not match:
        return []
    items = re.findall(r'"([^"]+)"', match.group(1))
    return [item[:MAX_ITEM] for item in items[:MAX_ITEMS]]


def parse_text(text):
    text = _strip_comments(text)
    return {
        "name": _field(text, "name"),
        "path": _field(text, "path"),
        "picture": _field(text, "picture"),
        "remote_file_id": _field(text, "remote_file_id"),
        "version": _field(text, "version"),
        "supported_version": _field(text, "supported_version"),
        "dependencies": _block_items(text, "dependencies"),
        "tags": _block_items(text, "tags"),
    }


def decode_bytes(raw):
    """Decode a .mod file body, preferring a clean non-UTF-8 decode over mojibake."""
    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if _C1.search(text):
            continue
        return text
    return raw.decode("utf-8", errors="replace")


def parse_file(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES)
    except OSError:
        return {}
    return parse_text(decode_bytes(raw))


def _version_tuple(value):
    value = (value or "").strip().lstrip("vV")
    if not value:
        return None
    parts = value.split(".")
    out = []
    for part in parts[:3]:
        if part in ("*", "x", "X", ""):
            out.append(None)
        else:
            digits = re.match(r"\d+", part)
            out.append(int(digits.group(0)) if digits else None)
    while len(out) < 3:
        out.append(None)
    return tuple(out)


def check_supported(game_version, supported):
    """Return ok | warn | mismatch | unknown for a mod's supported_version."""
    game = _version_tuple(game_version)
    mod = _version_tuple(supported)
    if not game or not mod:
        return "unknown"
    if mod[0] is None:
        return "ok"
    if mod[0] != game[0]:
        return "mismatch"
    if mod[1] is not None and game[1] is not None and mod[1] != game[1]:
        return "mismatch"
    if mod[2] is not None and game[2] is not None and mod[2] > game[2]:
        return "warn"
    return "ok"
