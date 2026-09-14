"""Bounded image upload reuse for the official DeepSeek Files API.

Only request image bytes are uploaded. The local index contains digests, file
IDs and expiry, not source images, paths, prompts or API keys. Remote files expire
after an hour; quota/transport failures fall back to the original inline request.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .deepseek import DEEPSEEK_FLASH


def supports_file_reuse(config: Any) -> bool:
    return (os.getenv("ASTRA_DEEPSEEK_FILES", "1").lower() not in {"0", "false", "off"}
            and urlparse(str(getattr(config, "base_url", ""))).hostname == "api.deepseek.com"
            and getattr(config, "model", "") == DEEPSEEK_FLASH
            and "vision" in getattr(config, "capabilities", ()))


class DeepSeekImageFiles:
    def __init__(self, endpoint: str, api_key: str, path: Path | None = None):
        self.scope = hashlib.sha256((endpoint.rstrip("/") + "\0" + api_key).encode()).hexdigest()
        self.path = path or Path(os.getenv("ASTRA_DEEPSEEK_FILE_CACHE", "").strip()
                                 or Path.home() / ".cache" / "astra" / "deepseek-files.db")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self._lock = asyncio.Lock()
        self.disabled_until = 0.0
        self.hits = self.uploads = 0
        with closing(self._db()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS uploads (scope TEXT, digest TEXT, file_id TEXT, expires REAL, PRIMARY KEY(scope,digest))")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=0.1)

    def invalidate(self, ids: set[str]) -> None:
        with closing(self._db()) as db, db:
            db.executemany("DELETE FROM uploads WHERE scope=? AND file_id=?", [(self.scope, value) for value in ids])

    async def prepare(self, messages: list[dict], client: Any) -> tuple[list[dict], set[str]]:
        if time.monotonic() < self.disabled_until:
            return messages, set()
        prepared = []
        ids: set[str] = set()
        try:
            # The entire image batch shares this cap, including lock waits.
            # One slow upload must not delay every CU step for minutes.
            async with asyncio.timeout(3):
                for message in messages:
                    content = message.get("content")
                    if not isinstance(content, list):
                        prepared.append(message)
                        continue
                    parts = []
                    for part in content:
                        image = part.get("image_url") if isinstance(part, dict) else None
                        url = image.get("url") if isinstance(image, dict) else None
                        # Official API: high/original/auto all retain original
                        # pixels. Low alone requests server downsampling.
                        if (isinstance(part, dict) and part.get("type") == "image_url"
                                and isinstance(url, str) and url.startswith("data:image/")
                                and isinstance(image, dict)
                                and image.get("detail", "auto") in {"auto", "original", "high"}):
                            file_id = await self._resolve(url, client)
                            if file_id:
                                ids.add(file_id)
                                parts.append({"type": "file", "file_id": file_id})
                                continue
                        parts.append(part)
                    prepared.append({**message, "content": parts})
            return prepared, ids
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose SDK errors which can include request data. Avoid
            # paying the same unsupported/quota/timeout cost every iteration.
            self.disabled_until = time.monotonic() + 120
            return messages, set()

    async def _resolve(self, url: str, client: Any) -> str:
        header, separator, encoded = url.partition(",")
        mime = header[5:].removesuffix(";base64")
        if (not separator or not header.endswith(";base64")
                or mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
                or len(encoded) > 44_739_244):
            return ""
        data = base64.b64decode(encoded, validate=True)
        digest = hashlib.sha256(data).hexdigest()
        async with self._lock:
            with closing(self._db()) as db, db:
                row = db.execute("SELECT file_id, expires FROM uploads WHERE scope=? AND digest=?",
                                 (self.scope, digest)).fetchone()
            if row and row[1] > time.time() + 60:
                self.hits += 1
                return row[0]
            uploaded = await client.files.create(
                file=(f"astra-{digest[:24]}.{mime.split('/')[1]}", data, mime),
                purpose="user_data",
                extra_body={"expires_after[anchor]": "created_at", "expires_after[seconds]": "3600"},
            )
            file_id = getattr(uploaded, "id", None)
            expires = getattr(uploaded, "expires_at", None)
            if (not isinstance(file_id, str) or not file_id.startswith("file-api-")
                    or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time() + 60
                    or getattr(uploaded, "bytes", None) != len(data)):
                raise ValueError("Invalid image upload acknowledgement")
            expires = min(expires, time.time() + 3600)
            with closing(self._db()) as db, db:
                db.execute("DELETE FROM uploads WHERE expires<=?", (time.time() + 60,))
                db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?,?)", (self.scope, digest, file_id, expires))
                db.execute("DELETE FROM uploads WHERE rowid NOT IN (SELECT rowid FROM uploads ORDER BY expires DESC LIMIT 2048)")
            self.uploads += 1
            return file_id
