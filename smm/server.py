"""Local HTTP server: serves the UI and the JSON API on 127.0.0.1 only."""
from __future__ import annotations

import gzip
import hmac
import json
import mimetypes
import re
import secrets
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css",
          "/app.js": "app.js", "/ai.js": "ai.js", "/ultimate.js": "ultimate.js", "/atmos.js": "atmos.js", "/forge.js": "forge.js", "/forge.css": "forge.css", "/ultimate.css": "ultimate.css", "/i18n-data.js": "i18n-data.js", "/logo.svg": "logo.svg",
          "/flags/une.svg": "flags/une.svg", "/flags/com.svg": "flags/com.svg", "/flags/une.png": "flags/une.png", "/flags/com.png": "flags/com.png",
          "/logo.png": "logo.png", "/backgrounds/com.jpg": "backgrounds/com.jpg"}
# Extension allow-list for the generic asset fallback below: new UI modules
# (crest.js, deck2.js, auspex.js, ...) are served without touching STATIC.
WEB_ASSET_EXT = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp",
                 ".gif", ".ico", ".woff", ".woff2", ".ttf"}
MAX_BODY = 64 * 1024 * 1024
TOKEN_PLACEHOLDER = b"__SMM_TOKEN__"
STATIC_CACHE = "public, max-age=3600"
# A 3000-mod library produces a ~4 MB bootstrap payload; gzip keeps the localhost
# transfer (and the browser's parse buffer) small without changing the JSON shape.
GZIP_MIN_BYTES = 1024
GZIP_TYPES = ("application/json", "text/html", "text/css", "text/javascript",
              "application/javascript", "image/svg+xml")


class Handler(BaseHTTPRequestHandler):
    api = None
    token = ""
    server_version = "StellarisModManager/0.2"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _accepts_gzip(self):
        """True only when the client allows gzip with a non-zero q-value.

        `gzip;q=0` means "do not use gzip" (RFC 9110 12.5.3), and some proxies send
        `br, gzip;q=0`; treating any mention of gzip as consent would break them.
        """
        best = None
        for part in (self.headers.get("Accept-Encoding") or "").split(","):
            token, _, params = part.strip().partition(";")
            token = token.strip().lower()
            if token not in ("gzip", "x-gzip", "*"):
                continue
            quality = 1.0
            for param in params.split(";"):
                name, _, value = param.partition("=")
                if name.strip().lower() != "q":
                    continue
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
            if best is None or quality > best:
                best = quality
        return best is not None and best > 0

    def _send(self, status, body, content_type, cache="no-store", head_only=False,
              extra_headers=None):
        encoding = None
        if (len(body) >= GZIP_MIN_BYTES and self._accepts_gzip()
                and any(content_type.startswith(kind) for kind in GZIP_TYPES)):
            # Big payloads use the fast level: on 4 MB the ratio barely differs
            # (25x vs 27x) but the CPU cost drops from ~200 ms to ~25 ms.
            body = gzip.compress(body, 1 if len(body) > 1 << 20 else 6)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        if any(content_type.startswith(kind) for kind in GZIP_TYPES):
            self.send_header("Vary", "Accept-Encoding")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _host_ok(self):
        # Blocks DNS-rebinding: a foreign page must not be able to reach the API.
        return bool(re.fullmatch(r"(127\.0\.0\.1|localhost)(:\d+)?",
                                 self.headers.get("Host", "").strip()))

    def do_GET(self):  # noqa: N802
        self._serve_get(head_only=False)

    def do_HEAD(self):  # noqa: N802
        # HEAD is answered for static files (so proxies/health checks work) and
        # rejected with 405 + Allow for the POST-only API.
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "text/plain", head_only=True,
                       extra_headers={"Allow": "POST"})
            return
        self._serve_get(head_only=True)

    def _serve_get(self, head_only=False):
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain", head_only=head_only)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in STATIC:
            target = WEB_DIR / STATIC[path]
            if not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain", head_only=head_only)
                return
            body = target.read_bytes()
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if path in ("/", "/index.html"):
                # Token lives in a meta tag (never in a script), so a strict CSP works.
                body = body.replace(TOKEN_PLACEHOLDER, self.token.encode("ascii"))
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", cache="no-store",
                           head_only=head_only)
                return
            self._send(HTTPStatus.OK, body, mime, cache=STATIC_CACHE, head_only=head_only)
            return
        # Generic web asset fallback: any allow-listed extension inside web/.
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if "." + ext in WEB_ASSET_EXT and path != "/":
            try:
                root = WEB_DIR.resolve()
                candidate = (root / urllib.parse.unquote(path).lstrip("/")).resolve()
                candidate.relative_to(root)
            except (ValueError, OSError):
                candidate = None
            if candidate is not None and candidate.is_file() and candidate.is_relative_to(WEB_DIR.resolve()):
                mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, candidate.read_bytes(), mime, cache=STATIC_CACHE, head_only=head_only)
                return
        match = re.fullmatch(r"/preview/([0-9]{8,20})", path)
        if match:
            target = self.api.preview(match.group(1))
            if target and Path(target).is_file():
                cache = self.api._preview_cache()
                raw = Path(target).read_bytes()
                mime = cache.mime_of(target) if cache else "image/jpeg"
                self._send(HTTPStatus.OK, raw, mime, cache=STATIC_CACHE, head_only=head_only)
                return
        # 本地 MOD 的 id 不一定是十六进制（如 hf_miku_ethics），放宽到任意非斜杠片段再反解
        match = re.fullmatch(r"/thumb/([^/]{2,200})", path)
        if match:
            thumb = self.api.thumb(urllib.parse.unquote(match.group(1)))
            if thumb and Path(thumb).is_file():
                mime = mimetypes.guess_type(str(thumb))[0] or "image/jpeg"
                self._send(HTTPStatus.OK, Path(thumb).read_bytes(), mime, cache=STATIC_CACHE,
                           head_only=head_only)
                return
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain", head_only=head_only)

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "非法请求来源"})
            return
        provided = self.headers.get("X-SMM-Token", "")
        if not self.token or not hmac.compare_digest(provided, self.token):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "请求未授权，请刷新页面后重试"})
            return
        match = re.fullmatch(r"/api/([a-z_]+)", self.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求内容过大"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                # json.JSONDecodeError is a ValueError, so the generic handler
                # below would echo the English parser message to the browser.
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求数据格式不正确。"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "参数格式不正确"})
                return
            result = self.api.dispatch(match.group(1), payload)
            if isinstance(result, dict) and "ok" in result:
                self._json(HTTPStatus.OK, result)
            else:
                self._json(HTTPStatus.OK, {"ok": True, "data": result})
        except ValueError as exc:
            # Keep the message readable in the UI; some are echoes of user input
            # (e.g. an unknown action name) and can be kilobytes long.
            message = str(exc)
            if len(message) > 240:
                message = message[:240] + "…"
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": message})
        except OSError:
            # PermissionError / ENOSPC / antivirus locks used to surface as a
            # bare 500 "内部错误"; report the class of failure without leaking
            # absolute paths.
            self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "文件读写失败：数据目录不可写或磁盘空间不足，请检查文件夹权限后重试。",
            })
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            # Never echo internal paths / exception details back to the browser,
            # but keep the traceback in the console log or the 500 is undebuggable.
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "内部错误"})


class SMMServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a deep accept queue.

    socketserver.TCPServer.request_queue_size defaults to 5, which is smaller
    than the 40-200 parallel refreshes the front end can fire; the extra
    connections were refused (WinError 10061) or reset mid-handshake (10054).
    """

    daemon_threads = True
    request_queue_size = 256


def serve(api, port=8720, token=None):
    Handler.api = api
    Handler.token = token or secrets.token_urlsafe(24)
    server = SMMServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.smm_token = Handler.token
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
