# -*- coding: utf-8 -*-
"""订阅更新检测（update_available）与工坊元数据合并。"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smm.catalog import Catalog  # noqa: E402

SCHEMA = """
create table mods (
  id char(36) primary key, steamId varchar(255), name varchar(255), displayName varchar(255),
  dirPath text, version varchar(255), requiredVersion varchar(255), tags json,
  status text, source text, timeUpdated integer, size integer, thumbnailPath text, gameRegistryId text
);
create table playsets (
  id char(36) primary key, name varchar(255) not null, isActive boolean,
  loadOrder varchar(255), createdOn datetime, updatedOn datetime
);
create table playsets_mods (
  playsetId char(36) not null, modId char(36) not null, enabled boolean, position integer
);
"""


class FakeWorkshop:
    def __init__(self, data):
        self.data = data

    def summary(self, steam_id):
        return self.data.get(str(steam_id))

    def details(self, ids):
        return {i: self.data[i] for i in ids if i in self.data}


@pytest.fixture()
def env(tmp_path):
    docs = tmp_path / "docs"
    game = tmp_path / "game"
    moddir = docs / "mod" / "ugc_555"
    moddir.mkdir(parents=True)
    (moddir / "descriptor.mod").write_text('name="UGC Mod"\n', encoding="utf-8")
    game.mkdir(parents=True, exist_ok=True)
    (game / "launcher-settings.json").write_text(json.dumps({"rawVersion": "v4.5.4"}), encoding="utf-8")
    db = docs / "launcher-v2.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("insert into playsets (id,name,isActive) values ('ps-1','主',1)")
    con.execute(
        "insert into mods (id,steamId,name,displayName,dirPath,status,source,timeUpdated,gameRegistryId)"
        " values ('ugc-1','555','UGC Mod','UGC Mod',?,'ready_to_play','steam',1000,'mod/ugc_555.mod')",
        (str(moddir),))
    con.execute("insert into playsets_mods values ('ps-1','ugc-1',1,0)")
    con.commit()
    con.close()
    return docs, game


def build(env, workshop_data):
    docs, game = env
    return Catalog(docs, game, workshop=FakeWorkshop(workshop_data), game_key="stellaris")


def test_update_available_when_workshop_newer(env):
    cat = build(env, {"555": {"time_updated": 2000, "time_created": 900, "title": "UGC Mod"}})
    snap = cat.scan(force=True)
    mod = [m for m in snap["mods"] if m["id"] == "ugc-1"][0]
    assert mod["update_available"] is True


def test_no_update_when_workshop_older(env):
    cat = build(env, {"555": {"time_updated": 500, "time_created": 100, "title": "UGC Mod"}})
    snap = cat.scan(force=True)
    mod = [m for m in snap["mods"] if m["id"] == "ugc-1"][0]
    assert mod["update_available"] is False


def test_no_update_within_grace_window(env):
    cat = build(env, {"555": {"time_updated": 1030, "time_created": 100, "title": "UGC Mod"}})
    snap = cat.scan(force=True)
    mod = [m for m in snap["mods"] if m["id"] == "ugc-1"][0]
    assert mod["update_available"] is False


def test_missing_workshop_meta_is_safe(env):
    cat = build(env, {})
    snap = cat.scan(force=True)
    mod = [m for m in snap["mods"] if m["id"] == "ugc-1"][0]
    assert mod["update_available"] is False
    assert mod["workshop"] is None


def test_remote_file_id_exposed(env):
    docs, game = env
    descriptor = docs / "mod" / "ugc_555" / "descriptor.mod"
    descriptor.write_text('name="UGC Mod"\nremote_file_id="555"\n', encoding="utf-8")
    cat = build(env, {"555": {"time_updated": 500, "time_created": 100, "title": "UGC Mod"}})
    snap = cat.scan(force=True)
    mod = [m for m in snap["mods"] if m["id"] == "ugc-1"][0]
    assert mod["remote_file_id"] == "555"