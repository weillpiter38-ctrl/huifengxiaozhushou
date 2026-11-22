# -*- coding: utf-8 -*-
"""工坊上传：边界条件与故障注入。"""
import sys
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smm import api as A  # noqa: E402


def make_api(tmp_path, mod_dir, thumb=None, remote_id=""):
    obj = object.__new__(A.Api)
    obj.data_dir = tmp_path / "data"
    obj.data_dir.mkdir(parents=True, exist_ok=True)
    obj.game_key = "stellaris"
    obj._local_mod = lambda _mid: (
        {"name": "m", "thumbnail": str(thumb) if thumb else "",
         "remote_file_id": remote_id, "tags": []},
        Path(mod_dir),
    )
    return obj


@pytest.fixture()
def mod_dir(tmp_path):
    d = tmp_path / "mod with space" / "我的 MOD"
    d.mkdir(parents=True)
    (d / "descriptor.mod").write_text('name="m"\n', encoding="utf-8")
    (d / "thumbnail.png").write_bytes(b"png")
    return d


def fake_steamcmd(tmp_path):
    d = tmp_path / "data" / "steamcmd"
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "steamcmd.exe"
    exe.write_bytes(b"")
    return exe


def test_prepare_handles_space_and_cjk_paths(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    text = Path(res["vdf"]).read_text(encoding="utf-8")
    assert str(mod_dir) in text
    assert '"contentfolder"' in text


def test_prepare_strips_control_chars_from_title(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    api.workshop_prepare({"mod_id": "m1", "title": "bad\x00title", "publishedfileid": "9"})
    vd = list((api.data_dir / "workshop").glob("*.vdf"))
    assert vd and all(v.name.isascii() for v in vd)


def test_launch_requires_existing_vdf(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    fake_steamcmd(tmp_path)
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    with pytest.raises(ValueError):
        api.workshop_launch({"vdf": str(api.data_dir / "workshop" / "nope.vdf")})


def test_launch_rejects_bad_username(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    fake_steamcmd(tmp_path)
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    with pytest.raises(ValueError):
        api.workshop_launch({"vdf": res["vdf"], "steam_username": "bad user; rm -rf"})


def test_launch_unique_batch_names(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    fake_steamcmd(tmp_path)
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    a = api.workshop_launch({"vdf": res["vdf"], "steam_username": "u"})
    b = api.workshop_launch({"vdf": res["vdf"], "steam_username": "u"})
    assert a["batch"] != b["batch"], "同一秒内两次上传会覆盖批处理"


def test_ensure_steamcmd_offline_reports_clearly(tmp_path, monkeypatch):
    api = make_api(tmp_path, tmp_path, thumb=None)
    monkeypatch.setattr(A.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

    def boom(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(A.urllib.request, "urlopen", boom)
    with pytest.raises(ValueError) as err:
        api._ensure_steamcmd("")
    assert "SteamCMD" in str(err.value)


def test_prepare_survives_unwritable_state_json(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    state_dir = api.data_dir / "workshop"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "m1.json").write_text("{ not json", encoding="utf-8")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    assert Path(res["vdf"]).is_file()


def test_prepare_overwrites_stale_vdf_for_same_id(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    r1 = api.workshop_prepare({"mod_id": "m1", "title": "old", "publishedfileid": "7"})
    r2 = api.workshop_prepare({"mod_id": "m1", "title": "new", "publishedfileid": "7"})
    assert r1["vdf"] == r2["vdf"]
    assert '"new"' in Path(r2["vdf"]).read_text(encoding="utf-8")


def test_read_result_ignores_broken_state(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t", "publishedfileid": "55"})
    out = api.workshop_read_result({"vdf": res["vdf"], "mod_id": "m1"})
    assert out["publishedfileid"] == "55"