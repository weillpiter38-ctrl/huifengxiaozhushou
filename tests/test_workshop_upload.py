# -*- coding: utf-8 -*-
"""工坊上传链路回归测试：prepare / launch / read_result。"""
import sys
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
        {"name": "测试MOD", "thumbnail": str(thumb) if thumb else "",
         "remote_file_id": remote_id, "tags": ["Audio"]},
        Path(mod_dir),
    )
    return obj


@pytest.fixture()
def mod_dir(tmp_path):
    d = tmp_path / "mod" / "my mod"
    d.mkdir(parents=True)
    (d / "descriptor.mod").write_text('name="测试MOD"\n', encoding="utf-8")
    (d / "thumbnail.png").write_bytes(b"png")
    return d


def test_prepare_writes_ascii_vdf_name(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "初音语音包（中日双语）", "tags": "Audio"})
    assert Path(res["vdf"]).name.isascii(), res["vdf"]
    assert Path(res["vdf"]).suffix == ".vdf"


def test_prepare_escapes_newlines_and_quotes(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({
        "mod_id": "m1", "title": 'a"b', "description": "第一行\n第二行",
    })
    text = Path(res["vdf"]).read_text(encoding="utf-8")
    desc_line = [ln for ln in text.splitlines() if '"description"' in ln][0]
    assert "\n" not in desc_line
    assert "\\n" in desc_line
    assert '"a\'b"' in text or "a'b" in text


def test_prepare_uses_descriptor_remote_file_id(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png", remote_id="3841807867")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    assert res["publishedfileid"] == "3841807867"
    assert '"3841807867"' in Path(res["vdf"]).read_text(encoding="utf-8")


def test_prepare_payload_id_wins(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png", remote_id="111")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t", "publishedfileid": "222"})
    assert res["publishedfileid"] == "222"


def test_prepare_falls_back_to_thumbnail_in_dir(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=None)
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    assert Path(res["preview"]).name == "thumbnail.png"


def test_prepare_errors_without_preview(tmp_path, mod_dir):
    (mod_dir / "thumbnail.png").unlink()
    api = make_api(tmp_path, mod_dir, thumb=None)
    with pytest.raises(ValueError):
        api.workshop_prepare({"mod_id": "m1", "title": "t"})


def test_prepare_points_at_mod_dir_without_copy(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    assert Path(res["contentfolder"]) == mod_dir
    staging = api.data_dir / "workshop" / "t"
    assert not staging.exists()


def test_prepare_keeps_tags_block(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t", "tags": "Audio, Utilities"})
    text = Path(res["vdf"]).read_text(encoding="utf-8")
    assert '"0" "Audio"' in text and '"1" "Utilities"' in text


def _fake_steamcmd(tmp_path):
    api_dir = tmp_path / "data" / "steamcmd"
    api_dir.mkdir(parents=True, exist_ok=True)
    exe = api_dir / "steamcmd.exe"
    exe.write_bytes(b"")
    return exe


def test_launch_batch_is_ascii(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    _fake_steamcmd(tmp_path)
    res = api.workshop_prepare({"mod_id": "m1", "title": "中文标题", "description": "中文描述"})
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    api.workshop_launch({"vdf": res["vdf"], "steam_username": "someone"})
    batches = sorted((api.data_dir / "workshop").glob("upload_*.cmd"))
    body = batches[-1].read_text(encoding="utf-8")
    assert body.isascii(), [c for c in body if ord(c) > 127][:5]
    assert "+login" in body and "workshop_build_item" in body


def test_launch_manual_mode_prompts_username(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    _fake_steamcmd(tmp_path)
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    api.workshop_launch({"vdf": res["vdf"]})
    body = sorted((api.data_dir / "workshop").glob("upload_*.cmd"))[-1].read_text(encoding="utf-8")
    assert "set /p STEAMUSER" in body
    assert "+login %STEAMUSER%" in body


def test_launch_rejects_vdf_outside_workshop_dir(tmp_path, mod_dir, monkeypatch):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    _fake_steamcmd(tmp_path)
    outside = tmp_path / "evil.vdf"
    outside.write_text('"workshopitem"\n{\n}\n', encoding="utf-8")
    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: None)
    with pytest.raises(ValueError):
        api.workshop_launch({"vdf": str(outside), "steam_username": "x"})


def test_read_result_reads_published_id(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t", "publishedfileid": "12345"})
    out = api.workshop_read_result({"vdf": res["vdf"], "mod_id": "m1"})
    assert out["publishedfileid"] == "12345"
    state = api.data_dir / "workshop" / "m1.json"
    assert state.is_file()
    assert '"12345"' in state.read_text(encoding="utf-8")


def test_read_result_errors_on_zero(tmp_path, mod_dir):
    api = make_api(tmp_path, mod_dir, thumb=mod_dir / "thumbnail.png")
    res = api.workshop_prepare({"mod_id": "m1", "title": "t"})
    with pytest.raises(ValueError):
        api.workshop_read_result({"vdf": res["vdf"], "mod_id": "m1"})