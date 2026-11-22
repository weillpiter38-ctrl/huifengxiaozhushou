"""Small self-update client.

The app only *reads* a manifest from a fixed HTTPS URL. Publishing is done
outside the app (GitHub Releases or any host you control), so users cannot
push updates to other installations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import net

DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/weillpiter38-ctrl/huifengxiaozhushou/main/update.json"
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024


NetworkError = net.NetworkError


def _network_message(exc):
    return net.describe_network_error(exc)


def _version_tuple(value):
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


# Manifest problems are the publisher's fault, not the user's network. Keep them
# as constants so the UI can show the real reason instead of "网络或 DNS 失败".
MANIFEST_JSON_MESSAGE = "更新清单不是有效的 JSON。"
MANIFEST_FORMAT_MESSAGE = "更新清单格式无效。"
MANIFEST_VERSION_MESSAGE = "更新清单缺少 version 字段。"
MANIFEST_SCHEME_MESSAGE = "更新源地址无效（必须使用 HTTPS）。"
MANIFEST_MESSAGES = (MANIFEST_JSON_MESSAGE, MANIFEST_FORMAT_MESSAGE, MANIFEST_VERSION_MESSAGE,
                     MANIFEST_SCHEME_MESSAGE)


def _source_url(data_dir):
    candidates = [Path(data_dir) / "update-source.json"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "update-source.json")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "update-source.json")
    for override in candidates:
        if override.is_file():
            try:
                data = json.loads(override.read_text(encoding="utf-8-sig"))
                url = str(data.get("manifest_url") or "").strip()
                if url:
                    return url
            except (OSError, ValueError):
                pass
    return DEFAULT_MANIFEST_URL


def _fetch_json(url, timeout=12):
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(MANIFEST_SCHEME_MESSAGE)
    if url.lower().startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?/", url):
        raise ValueError(MANIFEST_SCHEME_MESSAGE)
    request = urllib.request.Request(url, headers={"User-Agent": net.USER_AGENT})
    try:
        with net.open_url(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024)
    except net.NetworkError:
        raise
    except (urllib.error.URLError, socket.gaierror, TimeoutError, OSError) as exc:
        raise NetworkError(_network_message(exc)) from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(MANIFEST_JSON_MESSAGE) from exc
    if not isinstance(data, dict):
        raise ValueError(MANIFEST_FORMAT_MESSAGE)
    return data


def _fallback_urls(source):
    """Public mirrors for users whose DNS cannot resolve raw.githubusercontent.com."""
    out = []
    match = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", source)
    if match:
        owner, repo, branch, path = match.groups()
        out.append(f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")
        out.append(f"https://fastly.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")
        out.append(f"https://gcore.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")
        out.append(f"https://ghproxy.net/{source}")
    return out


def _best_error_message(errors):
    """Pick one safe, readable message; never leak a raw exception string."""
    safe = {net.CERT_MESSAGE, net.DNS_MESSAGE, net.TIMEOUT_MESSAGE, net.GENERIC_MESSAGE}
    safe.update(MANIFEST_MESSAGES)
    http_hint = ""
    for exc in errors:
        text = str(exc or "").strip()
        if not text:
            continue
        if text in safe:
            return text
        if text.startswith("服务器返回 HTTP") and not http_hint:
            http_hint = text
    if http_hint:
        return http_hint
    return net.DNS_MESSAGE


def check(data_dir, current_version):
    source = _source_url(data_dir)
    if not source:
        return {"ok": True, "configured": False, "current": current_version,
                "message": "还没有配置更新源。发布者需要设置 update-source.json 或编译时写入清单地址。"}
    # Try every mirror. A CDN may serve a stale cached manifest for hours, so
    # pick the highest version seen instead of the first source that answers.
    candidates = []
    errors = []
    for candidate in [source] + _fallback_urls(source):
        try:
            data = _fetch_json(candidate, timeout=12 if candidate == source else 8)
            latest = str(data.get("version") or "").strip()
            if not latest:
                raise ValueError(MANIFEST_VERSION_MESSAGE)
            candidates.append((_version_tuple(latest), latest, data, candidate))
        except (ValueError, OSError) as exc:  # NetworkError is a ValueError
            errors.append(exc)
    if not candidates:
        # Offline / broken manifest is a normal state, never an error dialog.
        manifest_error = (bool(errors)
                          and all(str(exc or "").strip() in MANIFEST_MESSAGES for exc in errors)
                          and _best_error_message(errors) != MANIFEST_SCHEME_MESSAGE)
        result = {
            "ok": True,
            "configured": True,
            "offline": True,
            "available": False,
            "source": source,
            "current": current_version,
            "latest": "",
            "message": _best_error_message(errors),
            "notes": "",
            "changelog": [],
            "sha256": "",
            "download_url": "",
            "published_at": "",
        }
        if manifest_error:
            # Not a network problem: the published update.json itself is broken.
            result["manifest_error"] = True
            result["message"] = (f"{result['message']}这是更新清单（update.json）本身的问题，"
                                 "不是你的网络，程序可正常使用。")
        return result
    _, latest, data, used = max(candidates, key=lambda item: item[0])
    download = str(data.get("url") or data.get("download_url") or "").strip()
    if download and download.startswith("/"):
        download = urllib.request.urljoin(used, download)
    return {
        "ok": True,
        "configured": True,
        "offline": False,
        "source": used,
        "current": current_version,
        "latest": latest,
        "available": _version_tuple(latest) > _version_tuple(current_version),
        "notes": str(data.get("notes") or ""),
        "changelog": data.get("changelog") if isinstance(data.get("changelog"), list) else [],
        "sha256": str(data.get("sha256") or "").strip().lower(),
        "download_url": download,
        "published_at": data.get("published_at") or "",
    }


def download(data_dir, url, expected_sha, version):
    if not url:
        raise ValueError("更新清单里没有下载地址。")
    expected_sha = str(expected_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("更新清单缺少有效的 SHA-256，已取消下载。")
    if not url.lower().startswith("https://") and not re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?/", url):
        raise ValueError("下载地址必须使用 HTTPS（本机测试地址除外）。")
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", str(version))[:64] or "latest"
    folder = (Path(data_dir) / "updates").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"HuifengsModManager-{safe_version}.part"
    final = folder / f"HuifengsModManager-{safe_version}.exe"
    request = urllib.request.Request(url, headers={"User-Agent": net.USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    try:
        with net.open_url(request, timeout=60) as response, open(target, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise ValueError("更新文件超过 120MB，已取消。")
                digest.update(chunk)
                handle.write(chunk)
    except (net.NetworkError, urllib.error.URLError, socket.gaierror, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise NetworkError(_network_message(exc)) from exc
    actual = digest.hexdigest()
    if actual.lower() != expected_sha:
        target.unlink(missing_ok=True)
        raise ValueError("更新文件 SHA-256 校验失败，已删除下载。")
    os.replace(target, final)
    final.with_name(final.name + ".sha256").write_text(actual, encoding="ascii")
    return {"ok": True, "path": str(final), "size": size, "sha256": actual}


def apply(data_dir, downloaded_path, version):
    if not getattr(sys, "frozen", False):
        raise ValueError("自动替换只在打包版中可用。")
    folder = (Path(data_dir) / "updates").resolve()
    downloaded = Path(downloaded_path).resolve()
    if (downloaded.parent != folder or not downloaded.name.startswith("HuifengsModManager-")
            or not downloaded.name.endswith(".exe")):
        raise ValueError("更新文件不在更新目录内，已取消。")
    if not downloaded.is_file():
        raise ValueError("更新文件不存在，请重新下载。")
    sha_file = downloaded.with_name(downloaded.name + ".sha256")
    if not sha_file.is_file():
        raise ValueError("更新文件缺少校验信息，请重新下载。")
    expected = sha_file.read_text(encoding="ascii").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("更新文件校验信息无效，请重新下载。")
    digest = hashlib.sha256()
    with downloaded.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("更新文件 SHA-256 校验失败，已取消替换。")
    target = Path(sys.executable).resolve()
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", str(version))[:64] or "latest"
    backup = folder / f"backup-{safe_version}.exe"
    try:
        backup.write_bytes(target.read_bytes())
    except OSError:
        pass
    bat = folder / "apply-update.bat"
    script = (
        "@echo off\r\n"
        "timeout /t 3 /nobreak >nul\r\n"
        "taskkill /f /im \"Huifeng's Mod Manager.exe\" >nul 2>&1\r\n"
        f"copy /y \"{downloaded}\" \"{target}\" >nul\r\n"
        f"start \"\" \"{target}\"\r\n"
        "del \"%~f0\"\r\n"
    )
    # cmd.exe reads .bat with the ANSI code page; UTF-8 breaks Chinese paths.
    try:
        bat.write_bytes(script.encode("gbk"))
    except UnicodeEncodeError:
        bat.write_bytes(script.encode("utf-8"))
    subprocess.Popen(["cmd.exe", "/c", str(bat)], close_fds=True,
                     creationflags=0x00000008 | 0x00000200)
    return {"ok": True, "message": "更新程序已启动，管理器将在几秒后自动重启。",
            "backup": str(backup)}
