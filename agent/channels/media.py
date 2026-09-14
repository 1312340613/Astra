"""Bounded, SSRF-aware media loading for messaging channels."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx


SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}


async def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(
                None,
                socket.getaddrinfo,
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except OSError:
            return False
        addresses = []
        for item in info:
            try:
                addresses.append(ipaddress.ip_address(item[4][0]))
            except ValueError:
                continue
    return bool(addresses) and all(address.is_global for address in addresses)


async def download_image_data_url(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 20.0,
) -> tuple[str, str]:
    """Download one public image and return ``(data_url, content_type)``."""

    current = str(url or "").strip()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(4):
            if not await _is_public_http_url(current):
                raise ValueError("image URL must resolve to a public HTTP(S) address")
            async with client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("image redirect is missing a location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in SUPPORTED_IMAGE_TYPES:
                    raise ValueError(f"unsupported image content type: {content_type or 'unknown'}")
                try:
                    declared = int(response.headers.get("content-length", "0") or 0)
                except ValueError:
                    declared = 0
                if declared > max_bytes:
                    raise ValueError(f"image is too large: {declared} bytes > {max_bytes} bytes")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ValueError(
                            f"image is too large: more than {max_bytes} bytes"
                        )
                if not payload:
                    raise ValueError("image download returned no data")
                encoded = base64.b64encode(payload).decode("ascii")
                return f"data:{content_type};base64,{encoded}", content_type
    raise ValueError("too many image redirects")
