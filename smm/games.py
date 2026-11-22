"""Game profiles for the multi-game manager."""
from __future__ import annotations

import json
import os
from pathlib import Path

GAMES = {
    "stellaris": {
        "key": "stellaris",
        "name": "Stellaris",
        "label": "群星",
        "appid": "281990",
        "folder": "Stellaris",
        "exe": "stellaris.exe",
        "docs_folder": "Stellaris",
        "data_subdir": "",
        "features": {"analysis": True, "saves": True, "settings": True, "merge": True, "workshop": True},
        "sort_modes": ["community", "smart", "stable"],
    },
    "hoi4": {
        "key": "hoi4",
        "name": "Hearts of Iron IV",
        "label": "钢铁雄心4",
        "appid": "394360",
        "folder": "Hearts of Iron IV",
        "exe": "hoi4.exe",
        "docs_folder": "Hearts of Iron IV",
        "data_subdir": "hoi4",
        "features": {"analysis": False, "saves": False, "settings": True, "merge": True, "workshop": True},
        "sort_modes": ["smart", "stable"],
        "settings_quality": False,
    },
}

DEFAULT_KEY = "stellaris"
ACTIVE_FILE = "active-game.json"


def normalize(key: str | None) -> str:
    value = str(key or "").strip().lower()
    return value if value in GAMES else DEFAULT_KEY


def get(key: str | None = None) -> dict:
    return GAMES[normalize(key)]


def active_path(root_data) -> Path:
    return Path(root_data) / ACTIVE_FILE


def read_active(root_data) -> str:
    env = os.environ.get("SMM_GAME")
    if env and env.lower() in GAMES:
        return env.lower()
    path = active_path(root_data)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_KEY
    if not isinstance(payload, dict):
        return DEFAULT_KEY
    return normalize(payload.get("game"))


def write_active(root_data, key: str) -> str:
    key = normalize(key)
    path = active_path(root_data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"game": key}, ensure_ascii=False, indent=2), encoding="utf-8")
    return key


def game_data_dir(root_data, key: str) -> Path:
    profile = get(key)
    sub = profile.get("data_subdir") or ""
    return Path(root_data) if not sub else Path(root_data) / "games" / sub
