"""Real isolated Chromium fixture and independent page counters for turn replay."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.runtime.browser_session import BrowserSessionManager
from agent.runtime.cdp_backend import CdpBrowserBackend
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry


class ReplayBrowser:
    def __init__(self, root: Path):
        self.backend = CdpBrowserBackend(user_data_dir=str(root / "chrome-profile"))
        self.manager = BrowserSessionManager(root / "browser.db", backend=self.backend)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""
        self.state: dict = {}
        self.lock = threading.Lock()

    async def start(self, registry: ToolRegistry) -> None:
        if not self.backend.capabilities.interactive:
            raise RuntimeError("Replay needs installed Chrome/Edge and Astra CDP dependencies")
        html = (Path(__file__).resolve().parents[2] / "tests/fixtures/cu-form-reliability.html").read_bytes()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/form":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                if self.path != "/report" or not 0 < size < 8192:
                    self.send_error(400)
                    return
                value = json.loads(self.rfile.read(size))
                with owner.lock:
                    # Concurrent fetches can arrive out of order; counters are monotonic.
                    if value.get("selectedClicks", 0) >= owner.state.get("selectedClicks", 0):
                        owner.state = value
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/form"
        register_browser_tools(registry, manager=self.manager)

    async def oracle(self) -> dict:
        connection = next(iter(self.backend._connections.values()))
        dom = await connection.evaluate("JSON.stringify([...document.querySelectorAll('input:checked')].map(e=>e.id))")
        selected = json.loads(dom)
        state = {}
        for _ in range(100):
            with self.lock:
                state = dict(self.state)
            if state.get("selected") == selected:
                break
            await asyncio.sleep(0.02)
        if state.get("selected") != selected:
            raise AssertionError("Page report did not settle to the independently read DOM state")
        return {"selected": selected, "selected_clicks": state.get("selectedClicks"), "submits": state.get("submits")}

    async def close(self) -> None:
        try:
            await self.backend.close_connection()
        finally:
            if self.server:
                await asyncio.to_thread(self.server.shutdown)
                self.server.server_close()
            if self.thread:
                await asyncio.to_thread(self.thread.join, 2)
