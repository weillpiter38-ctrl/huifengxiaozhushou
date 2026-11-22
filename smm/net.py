"""Shared HTTPS access for the manager.

Certificate verification is never disabled. When the OS trust store is missing
(e.g. inside a frozen build) we rebuild it from the Windows certificate stores
and retry. All failures are converted to short, readable Chinese messages so a
raw ``urlopen error [SSL: ...]`` is never shown as an "internal error".
"""
from __future__ import annotations

import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request

# 与 smm/api.py 的 APP_VERSION 同步（tests/test_net_ua.py 校验，避免再次落后于版本号）
from .version import APP_VERSION

USER_AGENT = "HuifengsModManager/" + APP_VERSION

CERT_MESSAGE = (
    "无法验证服务器证书（证书链不受信任）。常见原因：代理或安全软件拦截 HTTPS、"
    "系统时间不正确、网络被劫持或服务器证书链不完整。请检查系统时间与网络环境后重试。"
)
DNS_MESSAGE = "无法连接服务器（网络或 DNS 失败）。请检查网络、代理/VPN 后重试。"
TIMEOUT_MESSAGE = "连接服务器超时，请稍后重试。"
GENERIC_MESSAGE = "网络请求失败，请稍后重试。"


class NetworkError(ValueError):
    """A network failure whose message is safe to show to the user."""


def describe_network_error(exc):
    """Pure function: map a network exception to one fixed, readable message."""
    if exc is None:
        return DNS_MESSAGE
    if isinstance(exc, NetworkError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"服务器返回 HTTP {exc.code}，请稍后重试或联系发布者。"
    reason = getattr(exc, "reason", exc)
    text = f"{type(reason).__name__} {reason}".lower()
    if (isinstance(reason, ssl.SSLCertVerificationError)
            or "certificate_verify_failed" in text
            or "certificate verify failed" in text
            or "unable to get issuer certificate" in text):
        return CERT_MESSAGE
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text:
        return TIMEOUT_MESSAGE
    if (isinstance(reason, socket.gaierror) or "getaddrinfo" in text
            or "name or service not known" in text):
        return DNS_MESSAGE
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return DNS_MESSAGE
    return GENERIC_MESSAGE


def _is_cert_error(exc):
    return describe_network_error(exc) == CERT_MESSAGE


_full_context = None
_context_lock = threading.Lock()


def _windows_context():
    """Rebuild the trust store from Windows ROOT/CA stores (cached, once)."""
    global _full_context
    with _context_lock:
        if _full_context is not None:
            return _full_context
        context = ssl.create_default_context()
        if sys.platform == "win32" and hasattr(ssl, "enum_certificates"):
            try:
                for store in ("ROOT", "CA"):
                    for cert, encoding, _trust in ssl.enum_certificates(store):
                        if encoding != "x509_asn1":
                            continue
                        try:
                            context.load_verify_locations(
                                cadata=ssl.DER_cert_to_PEM_cert(cert))
                        except (ssl.SSLError, ValueError, TypeError):
                            continue
            except (OSError, ValueError):
                pass
        _full_context = context
        return context


def open_url(target, timeout=12, headers=None, raise_http=False):
    """Open an http(s) URL and return the response (usable as a context manager).

    Raises NetworkError with a readable message; never leaks an exception text.
    """
    if isinstance(target, urllib.request.Request):
        request = target
    else:
        request = urllib.request.Request(
            str(target), headers=headers or {"User-Agent": USER_AGENT})
    https = str(request.full_url).lower().startswith("https://")
    errors = (urllib.error.URLError, ssl.SSLError, socket.gaierror,
              TimeoutError, OSError)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 调用方（如 AI 接口）需要按状态码给出针对性提示时，原样上抛。
        if raise_http:
            raise
        raise NetworkError(describe_network_error(exc)) from exc
    except errors as exc:
        if https and _is_cert_error(exc):
            try:
                return urllib.request.urlopen(
                    request, timeout=timeout, context=_windows_context())
            except errors as retry:
                raise NetworkError(describe_network_error(retry)) from retry
        raise NetworkError(describe_network_error(exc)) from exc
