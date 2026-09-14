"""Shared network routing helpers.

Proxy environment variables describe an available route, not a guarantee that
the proxy process is still running.  Keep environment proxy handling explicit
so a stopped local proxy cannot strand long-lived Astra processes.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse


_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "::1"}


def _env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def configured_proxy_for_url(url: str) -> str | None:
    """Return the environment-configured proxy for *url*, without probing it."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme == "https":
        value = _env_value(
            "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"
        )
    elif scheme == "http":
        value = _env_value("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    else:
        value = _env_value("ALL_PROXY", "all_proxy")
    return value or None


def _is_local_host(host: str) -> bool:
    lowered = host.strip("[]").rstrip(".").lower()
    if lowered in _LOCAL_HOSTS or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def _matches_no_proxy(host: str, port: int | None) -> bool:
    raw = _env_value("NO_PROXY", "no_proxy")
    if not raw:
        return False
    host = host.strip("[]").rstrip(".").lower()
    for item in raw.split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token == "*":
            return True
        if "://" in token:
            token = urllib.parse.urlparse(token).netloc
        token_host, separator, token_port = token.rpartition(":")
        if not separator or not token_port.isdigit():
            token_host, token_port = token, ""
        token_host = token_host.strip("[]").lstrip(".").rstrip(".")
        if token_port and port != int(token_port):
            continue
        if host == token_host or host.endswith("." + token_host):
            return True
    return False


def proxy_endpoint_available(proxy_url: str, timeout: float | None = None) -> bool:
    """Check whether the configured proxy endpoint is accepting TCP connections."""
    parsed = urllib.parse.urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    host = parsed.hostname
    if not host:
        return False
    default_port = 1080 if parsed.scheme.startswith("socks") else 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return False
    if timeout is None:
        try:
            timeout = max(0.05, float(os.getenv("ASTRA_PROXY_PROBE_TIMEOUT", "0.25")))
        except ValueError:
            timeout = 0.25
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    connection.close()
    return True


def active_proxy_for_url(url: str) -> str | None:
    """Return a usable proxy route, or ``None`` for a direct connection.

    Local/private destinations and NO_PROXY matches always stay direct.  For
    other destinations the proxy is used only while its endpoint is reachable.
    Set ``ASTRA_PROXY_MODE=off`` to force direct or ``always`` to skip probing.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host or _is_local_host(host) or _matches_no_proxy(host, parsed.port):
        return None

    mode = os.getenv("ASTRA_PROXY_MODE", "off").strip().lower()
    if mode in {"off", "direct", "disabled", "0", "false"}:
        return None
    proxy_url = configured_proxy_for_url(url)
    if not proxy_url:
        return None
    if mode in {"always", "on", "1", "true"}:
        return proxy_url
    return proxy_url if proxy_endpoint_available(proxy_url) else None
