"""创意工坊预览图缓存。

把 Steam 返回的 ``preview_url`` 落到本地 ``<data-dir>/preview-cache/<id>.img``，
前端通过 ``/preview/<id>`` 读取，避免每打开一个 MOD 都去外网拉图。
只做只读缓存，失败一律返回 None，让前端退回齿轮兜底。
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import net

CACHE_DIR = "preview-cache"
MIN_BYTES = 1024
MAX_BYTES = 8 * 1024 * 1024


def guess_mime(raw: bytes) -> str:
    """按文件头猜图片类型（缓存文件本身不带扩展名）。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


class PreviewCache:
    def __init__(self, data_dir, workshop=None):
        self.dir = Path(data_dir) / CACHE_DIR
        self.workshop = workshop
        self._lock = threading.Lock()

    # ---- 对外 ----
    def path_for(self, mod_id, force=False):
        """返回本地缓存文件路径；拿不到图就返回 None。"""
        mod_id = str(mod_id or "").strip()
        if not mod_id.isdigit():
            return None
        target = self.dir / f"{mod_id}.img"
        if not force and self._usable(target):
            return target
        url = self._preview_url(mod_id, force=force)
        if not url:
            return None
        with self._lock:
            if not force and self._usable(target):
                return target
            raw = self._download(url)
            if not raw:
                return None
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".part")
                tmp.write_bytes(raw)
                tmp.replace(target)
            except OSError:
                return None
        return target if self._usable(target) else None

    def mime_of(self, path):
        try:
            with Path(path).open("rb") as handle:
                return guess_mime(handle.read(16)) or "image/jpeg"
        except OSError:
            return "image/jpeg"

    def clear(self, mod_id=None):
        try:
            if mod_id:
                (self.dir / f"{mod_id}.img").unlink(missing_ok=True)
            else:
                for item in self.dir.glob("*.img"):
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 内部 ----
    @staticmethod
    def _usable(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size >= MIN_BYTES
        except OSError:
            return False

    def _preview_url(self, mod_id, force=False):
        if self.workshop is None:
            return ""
        try:
            detail = (self.workshop.details([mod_id], force=force) or {}).get(mod_id) or {}
        except Exception:  # noqa: BLE001 - 网络/解析异常都当"没图"
            return ""
        return str(detail.get("preview_url") or "")

    @staticmethod
    def _download(url):
        if not url.lower().startswith("https://"):
            return b""
        try:
            with net.open_url(url, timeout=20) as response:
                raw = response.read(MAX_BYTES)
        except (net.NetworkError, OSError, ValueError):
            return b""
        return raw if len(raw) >= MIN_BYTES else b""
