# -*- coding: utf-8 -*-
"""SteamCMD 输出归类 + 上传状态查询。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smm import api as A  # noqa: E402


@pytest.mark.parametrize("text,stage,error", [
    ("Logging in user 'x'...\nOK", "login", ""),
    ("Preparing content......", "preparing", ""),
    ("Uploading content...", "uploading", ""),
    ("Committing update...Success.", "done", ""),
    ("ERROR! Failed to update workshop item (Invalid Password)", "error", "密码错误"),
    ("ERROR! Failed to update workshop item (File Not Found)", "error", "物品不存在"),
    ("ERROR! Failed to update workshop item (Rate Limit Exceeded)", "error", "更新太频繁"),
    ("Uploading content...ERROR! Failed to update workshop item (Limit exceeded).", "error", "更新太频繁"),
    ("ERROR! Failed to update workshop item (Access Denied)", "error", "没有权限"),
    ("No Connection", "error", "网络不通"),
    ("ERROR! Failed to update workshop item (Timeout)", "error", "网络超时"),
    ("FAILED (Invalid Login Auth Code)", "error", "验证码错误"),
])
def test_classify(text, stage, error):
    state = A.classify_steamcmd_output(text)
    assert state["stage"] == stage, state
    assert state["error"] == error, state
    assert state["hint"]


def test_classify_extracts_percent():
    state = A.classify_steamcmd_output("[ 45%] Uploading content...")
    assert state["percent"] == 45
    assert state["stage"] == "uploading"


def test_status_reads_depot_log(tmp_path, monkeypatch):
    api = object.__new__(A.Api)
    api.data_dir = tmp_path
    api.game_key = "stellaris"
    sc = tmp_path / "steamcmd"
    (sc / "workshopbuilds").mkdir(parents=True)
    (sc / "steamcmd.exe").write_bytes(b"")
    (sc / "workshopbuilds" / "depot_build_281990.log").write_text(
        "Uploading content...\n[ 12%] chunk\n", encoding="utf-8")
    monkeypatch.setattr(api, "_ensure_steamcmd", lambda req="": sc / "steamcmd.exe")
    state = api.workshop_upload_status({})
    assert state["stage"] == "uploading"
    assert state["percent"] == 12
    assert state["source"].endswith("depot_build_281990.log")


def test_status_without_logs_is_idle(tmp_path, monkeypatch):
    api = object.__new__(A.Api)
    api.data_dir = tmp_path
    api.game_key = "stellaris"
    sc = tmp_path / "steamcmd"
    sc.mkdir()
    (sc / "steamcmd.exe").write_bytes(b"")
    monkeypatch.setattr(api, "_ensure_steamcmd", lambda req="": sc / "steamcmd.exe")
    state = api.workshop_upload_status({})
    assert state["stage"] == "idle"


def test_status_reports_missing_steamcmd(tmp_path, monkeypatch):
    api = object.__new__(A.Api)
    api.data_dir = tmp_path
    api.game_key = "stellaris"

    def boom(req=""):
        raise ValueError("离线")

    monkeypatch.setattr(api, "_ensure_steamcmd", boom)
    state = api.workshop_upload_status({})
    assert state["ok"] is False and state["error"] == "找不到 SteamCMD"