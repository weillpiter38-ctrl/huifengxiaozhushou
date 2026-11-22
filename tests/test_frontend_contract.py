# -*- coding: utf-8 -*-
"""前后端一致性检查：JS 里用到的 DOM id 与 API 方法必须真实存在。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smm import api as A  # noqa: E402

HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))
JS_IDS = set(re.findall(r"\$\(\s*'#([A-Za-z0-9_\-]+)'", JS))
JS_IDS |= set(re.findall(r'\$\(\s*"#([A-Za-z0-9_\-]+)"', JS))
# 动态拼接的 id（如 '#list-' + side）不算
JS_IDS = {i for i in JS_IDS if not i.endswith('-') and i != 'list'}


def test_every_js_id_exists_in_html():
    missing = sorted(i for i in JS_IDS if i not in HTML_IDS)
    assert not missing, f"app.js 引用了不存在的元素 id: {missing[:20]}"


def test_workshop_ids_present():
    for needed in ("workshop-upload-dialog", "workshop-title", "workshop-description",
                   "workshop-tags", "workshop-preview", "workshop-username",
                   "workshop-file-id", "workshop-steamcmd", "workshop-result",
                   "btn-workshop-upload", "btn-workshop-read", "context-menu"):
        assert needed in HTML_IDS, needed


def test_api_methods_called_by_js_exist():
    src = (ROOT / "smm" / "api.py").read_text(encoding="utf-8")
    dispatched = set(re.findall(r'"([a-z_]+)":\s*(?:self\.|lambda)', src))
    dispatched |= set(re.findall(r'"([a-z_]+)":', src))
    called = set(re.findall(r"api\(\s*'([a-z_]+)'", JS))
    missing = sorted(c for c in called if c not in dispatched)
    assert not missing, f"app.js 调用了后端不存在的方法: {missing}"


def test_workshop_api_registered():
    src = (ROOT / "smm" / "api.py").read_text(encoding="utf-8")
    for name in ("workshop_prepare", "workshop_launch", "workshop_read_result"):
        assert f'"{name}": self.{name}' in src, name


def test_version_consistent():
    app_version = A.APP_VERSION
    assert f"v{app_version}" in HTML
    assert f"?v={app_version}" in HTML
    assert f"· {app_version}" in HTML


def test_assets_use_same_version():
    versions = set(re.findall(r"\?v=([0-9.]+)", HTML))
    assert len(versions) == 1, versions
    assert versions.pop() == A.APP_VERSION