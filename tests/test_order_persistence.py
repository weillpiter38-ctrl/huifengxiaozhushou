"""顺序永久保留（不管有没有启用）。

覆盖三件事：
1. 「停用 → 重新启用」回到原来的相对位置（前端 orderIds 语义，直接执行 web/app.js 里的真实函数）；
2. save_order 能按显式 position 落库（positions 参数，且不传时保持旧的 enumerate 行为）；
3. 顺序跨 scan 一致（停用的 MOD 也带着 position 回来，两栏都按它排）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from smm.catalog import Catalog
from smm.launcher_db import LauncherDB

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "app.js"
FORGE_JS = ROOT / "web" / "forge.js"

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

MODS = ["mod-a", "mod-b", "mod-c", "mod-d"]


def make_fixture(root: Path):
    """一个最小的 launcher-v2.sqlite + mods 目录，供 Catalog.scan() 用。"""
    docs = root / "docs"
    game = root / "game"
    for folder in (docs, game, docs / "mod"):
        folder.mkdir(parents=True, exist_ok=True)
    (game / "launcher-settings.json").write_text(
        json.dumps({"rawVersion": "v4.5.4", "version": "Cygnus v4.5.4"}), encoding="utf-8")
    db_path = docs / "launcher-v2.sqlite"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("insert into playsets (id, name, isActive, loadOrder, createdOn) "
                "values ('ps-1', '主播放集', 1, 'custom', 1)")
    for index, mod_id in enumerate(MODS):
        folder = root / "mods" / mod_id
        (folder / "common").mkdir(parents=True, exist_ok=True)
        (folder / "common" / "shared.txt").write_text("x", encoding="utf-8")
        (folder / "descriptor.mod").write_text(
            'name="%s"\nsupported_version="v4.5.*"\nversion="1.0"\n' % mod_id, encoding="utf-8")
        con.execute(
            "insert into mods (id, steamId, name, displayName, dirPath, version, requiredVersion, tags, "
            "status, source, timeUpdated, size, thumbnailPath, gameRegistryId) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mod_id, str(1000 + index), mod_id, mod_id, str(folder), "1.0", "v4.5.*",
             '["Gameplay"]', "ready_to_play", "steam", 0, 0, None, "mod/ugc_%d.mod" % (1000 + index)))
        con.execute("insert into playsets_mods values ('ps-1', ?, 1, ?)", (mod_id, index))
    con.commit()
    con.close()
    return docs, game, db_path


def read_positions(db_path: Path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select modId, enabled, position from playsets_mods where playsetId='ps-1' order by position").fetchall()
    con.close()
    return rows


class SaveOrderPositionsTest(unittest.TestCase):
    """save_order 的 positions 参数：显式顺序优先，不传时保持旧行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.docs, self.game, self.db_path = make_fixture(Path(self.tmp.name))
        self.db = LauncherDB(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_positions_win_over_entry_order(self):
        # 前端按「已启用在前、已停用在后」发 entries，但 positions 给出用户真正排的顺序。
        entries = [
            {"mod_id": "mod-a", "enabled": True},
            {"mod_id": "mod-c", "enabled": True},
            {"mod_id": "mod-b", "enabled": False},
            {"mod_id": "mod-d", "enabled": False},
        ]
        positions = {"mod-a": 0, "mod-b": 1, "mod-c": 2, "mod-d": 3}
        written = self.db.save_order("ps-1", entries, positions=positions)
        self.assertEqual(written, 4)
        self.assertEqual(read_positions(self.db_path), [
            ("mod-a", 1, 0), ("mod-b", 0, 1), ("mod-c", 1, 2), ("mod-d", 0, 3)])

    def test_positions_are_compacted_to_contiguous_range(self):
        entries = [{"mod_id": mid, "enabled": mid != "mod-b"} for mid in MODS]
        positions = {"mod-a": 10, "mod-b": 20, "mod-c": 30, "mod-d": 40}
        self.db.save_order("ps-1", entries, positions=positions)
        self.assertEqual([row[2] for row in read_positions(self.db_path)], [0, 1, 2, 3])

    def test_missing_positions_fall_back_to_entry_order(self):
        entries = [{"mod_id": "mod-c", "enabled": True}, {"mod_id": "mod-a", "enabled": False}]
        self.db.save_order("ps-1", entries, positions={"mod-c": 5})
        self.assertEqual(read_positions(self.db_path), [("mod-c", 1, 0), ("mod-a", 0, 1)])

    def test_positions_for_unknown_ids_are_ignored(self):
        entries = [{"mod_id": "mod-a", "enabled": True}]
        self.db.save_order("ps-1", entries, positions={"mod-a": 3, "ghost": 0})
        self.assertEqual(read_positions(self.db_path), [("mod-a", 1, 0)])

    def test_default_behaviour_unchanged(self):
        # 不传 positions：position 仍等于 entries 的先后（向后兼容，api.py 现在就是这么调的）。
        entries = [{"mod_id": "mod-b", "enabled": False}, {"mod_id": "mod-a", "enabled": True}]
        self.db.save_order("ps-1", entries)
        self.assertEqual(read_positions(self.db_path), [("mod-b", 0, 0), ("mod-a", 1, 1)])


class ScanOrderTest(unittest.TestCase):
    """顺序跨 scan 一致：停用的 MOD 也带 position 回来，顺序不乱。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.docs, self.game, self.db_path = make_fixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return Catalog(docs=str(self.docs), game=str(self.game), db_path=str(self.db_path)).scan()

    def test_scan_keeps_disabled_mods_in_place(self):
        LauncherDB(self.db_path).save_order(
            "ps-1",
            [{"mod_id": "mod-a", "enabled": True}, {"mod_id": "mod-c", "enabled": True},
             {"mod_id": "mod-b", "enabled": False}, {"mod_id": "mod-d", "enabled": False}],
            positions={"mod-a": 0, "mod-b": 1, "mod-c": 2, "mod-d": 3})
        payload = self.scan()
        self.assertEqual([mod["id"] for mod in payload["mods"]], MODS)
        self.assertEqual([mod["position"] for mod in payload["mods"]], [0, 1, 2, 3])
        self.assertEqual([bool(mod["enabled"]) for mod in payload["mods"]],
                         [True, False, True, False])

    def test_rescan_is_stable(self):
        # 模拟前端：orderIds 直接继承 scan 顺序（含已停用），再原样回写。
        db = LauncherDB(self.db_path)
        db.save_order(
            "ps-1",
            [{"mod_id": "mod-c", "enabled": True}, {"mod_id": "mod-a", "enabled": True},
             {"mod_id": "mod-d", "enabled": False}, {"mod_id": "mod-b", "enabled": False}],
            positions={"mod-c": 0, "mod-a": 1, "mod-d": 2, "mod-b": 3})
        first = self.scan()
        order_ids = [mod["id"] for mod in first["mods"]]
        self.assertEqual(order_ids, ["mod-c", "mod-a", "mod-d", "mod-b"])
        # 用户没动，重新打开管理器（新 Catalog 实例）再扫一遍
        second = self.scan()
        self.assertEqual([mod["id"] for mod in second["mods"]], order_ids)
        # 再按 orderIds 原样回写，顺序依旧
        db.save_order("ps-1", [{"mod_id": mod["id"], "enabled": mod["enabled"]} for mod in second["mods"]],
                      positions={mod["id"]: mod["position"] for mod in second["mods"]})
        third = self.scan()
        self.assertEqual([mod["id"] for mod in third["mods"]], order_ids)


# --------------------------------------------------------------- 前端顺序内核
def extract_function(src: str, name: str) -> str:
    """按大括号配平从 app.js 里抽出一个函数（含 async 前缀），用于离线执行真实代码。"""
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not match:
        raise AssertionError("web/app.js 里找不到函数 %s" % name)
    depth = 0
    start = src.index("{", src.index(")", match.end() - 1))
    for index in range(start, len(src)):
        char = src[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[match.start():index + 1]
    raise AssertionError("函数 %s 的大括号不配平" % name)


def extract_order_core(src: str) -> str:
    begin = src.index("/* >>> order-core begin */")
    end = src.index("/* >>> order-core end */")
    return src[begin:end]


def find_edge():
    for candidate in (
        os.environ.get("SMM_EDGE", ""),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


HARNESS_HEAD = r"""
const state = {
  data: { active_playset: 'ps-1', stats: {}, backups: [] },
  enabled: [], disabled: [], available: [], orderIds: [],
  selectedId: null, selectedIds: new Set(), analysis: null, gameFeatures: {},
  dirty: false, saving: false, autoSaveBlocked: false, changeSeq: 0, lastSavedAt: 0,
};
const results = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  results.push({ name: name, ok: ok, actual: actual, expected: expected });
}
function ids(list) { return list.map(function (m) { return m.id; }); }
function renderAll() {}
function renderBoard() {}
function renderDetail() {}
function renderStatus() {}
function markDirty() {}
function scheduleAutoSave() {}
function toast() {}
let lastPayload = null;
async function api(method, payload) { lastPayload = payload; return { stats: {}, backups: [] }; }
"""

HARNESS_TAIL = r"""
const A = { id: 'A', name: 'A', enabled: true, in_playset: true };
const B = { id: 'B', name: 'B', enabled: false, in_playset: true };
const C = { id: 'C', name: 'C', enabled: true, in_playset: true };
const D = { id: 'D', name: 'D', enabled: false, in_playset: true };
const AV = { id: 'AV', name: 'AV', enabled: false, in_playset: false };

(async function () {
  // 1) 扫描结果按 position 升序 → orderIds 继承，两栏各自按它排
  apply({ mods: [A, B, C, D], available: [AV], stats: {}, backups: [] });
  check('apply.orderIds', state.orderIds, ['A', 'B', 'C', 'D']);
  check('apply.enabled', ids(state.enabled), ['A', 'C']);
  check('apply.disabled.by-position', ids(state.disabled), ['B', 'D']);

  // 2) 停用 C → orderIds 不动；再启用 → 回到原来的相对位置（A 后面，不是末尾）
  disableMod(C);
  check('disable.orderIds', state.orderIds, ['A', 'B', 'C', 'D']);
  check('disable.disabled.by-position', ids(state.disabled), ['B', 'C', 'D']);
  enableMod(C);
  check('re-enable.enabled', ids(state.enabled), ['A', 'C']);
  check('re-enable.orderIds', state.orderIds, ['A', 'B', 'C', 'D']);

  // 3) 自动保存按 orderIds 发，停用的 MOD 不会被挤到末尾
  await runAutoSave();
  check('autosave.order', lastPayload.order.map(function (e) { return [e.mod_id, e.enabled]; }),
    [['A', true], ['B', false], ['C', true], ['D', false]]);

  // 4) 停用首项再启用：回到第一位，不跑到末尾
  disableMod(A);
  check('disable-first.enabled', ids(state.enabled), ['C']);
  enableMod(A);
  check('re-enable-first.enabled', ids(state.enabled), ['A', 'C']);

  // 5) 置底只重排已启用项，已停用的 B/D 保持槽位
  moveModEdge(A, true);
  check('move-edge.enabled', ids(state.enabled), ['C', 'A']);
  check('move-edge.orderIds', state.orderIds, ['C', 'B', 'A', 'D']);
  await runAutoSave();
  check('move-edge.autosave', lastPayload.order.map(function (e) { return [e.mod_id, e.enabled]; }),
    [['C', true], ['B', false], ['A', true], ['D', false]]);

  // 6) 启用夹在中间的 B：落在 C 与 A 之间
  enableMod(B);
  check('re-enable-middle.enabled', ids(state.enabled), ['C', 'B', 'A']);
  check('re-enable-middle.orderIds', state.orderIds, ['C', 'B', 'A', 'D']);

  // 7) 全新 MOD 才追加到末尾
  enableMod(AV);
  check('new-mod.orderIds', state.orderIds, ['C', 'B', 'A', 'D', 'AV']);
  check('new-mod.enabled', ids(state.enabled), ['C', 'B', 'A', 'AV']);

  // 8) 全关再全开：顺序按 orderIds 恢复
  setAllEnabled(false);
  check('all-off.orderIds', state.orderIds, ['C', 'B', 'A', 'D', 'AV']);
  check('all-off.enabled', ids(state.enabled), []);
  setAllEnabled(true);
  check('all-on.enabled', ids(state.enabled), ['C', 'B', 'A', 'D', 'AV']);

  // 9) 显式落点（拖拽）仍按给定 index
  disableMod(D);
  enableMod(D, 0);
  check('explicit-index.enabled', ids(state.enabled), ['D', 'C', 'B', 'A', 'AV']);

  // 10) 批量停用/启用同样回原位
  state.selectedIds = new Set(['C', 'A']);
  batchSetEnabled(false);
  check('batch-off.enabled', ids(state.enabled), ['D', 'B', 'AV']);
  state.selectedIds = new Set(['C', 'A']);
  batchSetEnabled(true);
  check('batch-on.enabled', ids(state.enabled), ['D', 'C', 'B', 'A', 'AV']);
  check('batch-on.orderIds', state.orderIds, ['D', 'C', 'B', 'A', 'AV']);

  const fails = results.filter(function (r) { return !r.ok; });
  document.title = JSON.stringify({ total: results.length, fails: fails });
})();
"""


class OrderCoreRuntimeTest(unittest.TestCase):
    """把 app.js 里的真实顺序函数抽出来，在 Edge 无头里跑一遍。"""

    def test_order_functions_keep_positions(self):
        edge = find_edge()
        if not edge:
            self.skipTest("找不到 msedge.exe，跳过真实 JS 执行")
        src = APP_JS.read_text(encoding="utf-8")
        names = ["allMods", "modById", "apply", "runAutoSave", "enableMod", "disableMod",
                 "moveMod", "moveModEdge", "setAllEnabled", "batchSetEnabled", "setFolderEnabled"]
        body = "\n\n".join([extract_order_core(src)] + [extract_function(src, name) for name in names])
        harness = ("<!doctype html><meta charset=\"utf-8\"><title>pending</title><script>\n"
                   + HARNESS_HEAD + "\n" + body + "\n" + HARNESS_TAIL + "\n</script>\n")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "order-harness.html"
            page.write_text(harness, encoding="utf-8")
            profile = Path(tmp) / "edge-profile"
            out = subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--window-size=1200,800", "--virtual-time-budget=15000",
                 "--user-data-dir=" + str(profile), "--dump-dom", page.as_uri()],
                capture_output=True, text=True, timeout=180, errors="replace")
        match = re.search(r"<title>(.*?)</title>", out.stdout, re.S)
        self.assertTrue(match, "抓不到 <title>，dump 前 800 字：%s" % out.stdout[:800])
        raw = match.group(1).replace("&quot;", '"').replace("&amp;", "&")
        self.assertNotEqual(raw, "pending", "harness 没跑完（可能是 JS 语法错误）：%s" % out.stderr[-800:])
        report = json.loads(raw)
        self.assertTrue(report["total"] >= 20, report)
        self.assertEqual(report["fails"], [], "顺序断言失败：%s" % json.dumps(
            report["fails"], ensure_ascii=False, indent=2))


class WiringTest(unittest.TestCase):
    """静态契约：没装 Edge 也要能拦住「又把顺序写回末尾」的改动。"""

    def setUp(self):
        self.app = APP_JS.read_text(encoding="utf-8")
        self.forge = FORGE_JS.read_text(encoding="utf-8")

    def test_run_auto_save_uses_order_ids(self):
        body = extract_function(self.app, "runAutoSave")
        self.assertIn("state.orderIds", body)
        self.assertNotIn("[...state.enabled, ...state.disabled]", body)

    def test_disable_mod_does_not_push_to_disabled(self):
        body = extract_function(self.app, "disableMod")
        self.assertNotIn("state.disabled.push", body)
        self.assertIn("syncPanes", body)

    def test_enable_mod_has_no_forced_index(self):
        for name in ("toggleMod", "moveTo"):
            match = re.search(r"function\s+" + name + r"\s*\(", self.forge)
            self.assertTrue(match, name)
            block = self.forge[match.start():match.start() + 1400]
            self.assertNotIn("enableMod(mod, st.enabled.length)", block)

    def test_forge_drag_commits_order(self):
        self.assertIn("commitOrder()", self.forge)

    def test_order_core_block_present(self):
        core = extract_order_core(self.app)
        for name in ("syncPanes", "commitOrder", "placeEnabled", "reorderEnabledInOrder"):
            self.assertIn("function " + name, core)


if __name__ == "__main__":
    unittest.main()
