"""Tests for browser fallback ladder and connect-to-existing-Chrome.

Covers:
- _fallback_extract: static → CDP → screenshot escalation
- browser_open with fallback (auto-escalates tab mode)
- browser_extract with fallback
- discover_chrome_debug_ports (unit, no real Chrome)
- list_existing_targets (unit, no real Chrome)
- CdpBrowserBackend.connect_existing (unit, mock)
- browser_connect tool registration and guard
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from agent.runtime.browser_session import (
    BackendCapabilities,
    BrowserMode,
    BrowserSessionManager,
)
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers: fake backends for testing fallback
# ---------------------------------------------------------------------------

class StaticOnlyBackend:
    """Backend that only does static extraction (no interactive)."""
    name = "static-only"
    capabilities = BackendCapabilities(read=True, interactive=False, takeover=False)

    def __init__(self, content: str = ""):
        self._content = content

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        return self._content

    async def status(self):
        return True, "static-only"


class FullBackend:
    """Backend with read + interactive + takeover."""
    name = "full"
    capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)

    def __init__(
        self,
        static_content: str = "",
        cdp_text: str = "",
        screenshot_path: str = "",
    ):
        self._static_content = static_content
        self._cdp_text = cdp_text
        self._screenshot_path = screenshot_path
        self.extract_calls = []
        self.navigate_calls = []
        self.get_text_calls = []
        self.screenshot_calls = []
        # Track last navigated URL so interactive_state can return matching origin
        self._last_navigated_url = ""

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        self.extract_calls.append(url)
        return self._static_content

    async def status(self):
        return True, "full"

    async def interactive_navigate(self, url, *, tab_id="default", wait_ms=2000):
        self.navigate_calls.append((tab_id, url))
        self._last_navigated_url = url

    async def interactive_get_text(self, *, tab_id="", url=""):
        self.get_text_calls.append((tab_id, url))
        return self._cdp_text

    async def interactive_screenshot(self, *, tab_id="", url=""):
        self.screenshot_calls.append((tab_id, url))
        return self._screenshot_path

    async def close_connection(self, tab_id=""):
        pass

    async def connect_existing(self, *, port=0, host="", tab_id="default"):
        self.navigate_calls.append((tab_id, "existing"))
        return "Connected to existing Chrome on 127.0.0.1:9222"

    async def interactive_state(self, *, tab_id="default", url=""):
        # Return the last navigated URL so the origin check passes.
        # If no navigation happened yet, return the provided URL
        # or a safe default.
        resolved = self._last_navigated_url or url or "https://example.com"
        return resolved, "Test Page", self._cdp_text or "page content"


class FailingBackend:
    """Backend where everything fails."""
    name = "failing"
    capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)

    async def extract(self, url, *, max_length=12000):
        raise RuntimeError("static exploded")

    async def status(self):
        return False, "broken"

    async def interactive_navigate(self, url, *, tab_id="default", wait_ms=2000):
        raise RuntimeError("cdp exploded")

    async def interactive_get_text(self, *, tab_id="", url=""):
        raise RuntimeError("cdp text exploded")

    async def interactive_screenshot(self, *, tab_id="", url=""):
        raise RuntimeError("screenshot exploded")

    async def close_connection(self, tab_id=""):
        pass


# ---------------------------------------------------------------------------
# Fallback ladder: _fallback_extract via browser_open
# ---------------------------------------------------------------------------

class TestFallbackLadder:
    def _make_registry(self, backend, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(reg, manager=manager, backend=backend)
        return reg, manager

    def test_static_success_no_escalation(self, tmp_path):
        """Good static content → returns immediately, no CDP calls."""
        backend = FullBackend(static_content="A" * 300)
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_open", {"url": "https://example.com"}))
        out = result.get("output", "")
        assert "static rung" in out
        assert "A" * 100 in out
        # CDP should NOT have been called
        assert len(backend.navigate_calls) == 0

    def test_static_too_short_escalates_to_cdp(self, tmp_path):
        """Static returns < 200 chars → escalates to CDP."""
        backend = FullBackend(static_content="tiny", cdp_text="B" * 300)
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_open", {"url": "https://spa-app.com"}))
        out = result.get("output", "")
        assert "cdp rung" in out
        assert "B" * 100 in out
        assert len(backend.navigate_calls) == 1

    def test_static_and_cdp_fail_escalates_to_screenshot(self, tmp_path):
        """Static too short + CDP too short → screenshot."""
        backend = FullBackend(
            static_content="tiny",
            cdp_text="also tiny",
            screenshot_path="/tmp/shot.png",
        )
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_open", {"url": "https://canvas-app.com"}))
        out = result.get("output", "")
        payload = json.loads(out)
        assert payload["type"] == "image_attachment"
        assert payload["rung"] == "screenshot"
        assert payload["image_paths"] == ["/tmp/shot.png"]

    def test_all_rungs_fail(self, tmp_path):
        """Everything fails → error message."""
        backend = FailingBackend()
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_open", {"url": "https://broken.com"}))
        out = result.get("output", "")
        assert "All extraction rungs failed" in out

    def test_no_backend_no_crash(self, tmp_path):
        """No backend at all → graceful message."""
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(reg, manager=manager)
        result = run(reg.execute("browser_open", {"url": "https://example.com"}))
        out = result.get("output", "")
        assert "Opened tab" in out  # tab still opens

    def test_tab_mode_escalates_with_cdp(self, tmp_path):
        """When CDP rung is used, tab mode should escalate to HEADLESS."""
        backend = FullBackend(static_content="tiny", cdp_text="C" * 300)
        reg, manager = self._make_registry(backend, tmp_path)
        run(reg.execute("browser_open", {"url": "https://spa.com"}))
        # Find the tab
        sessions = manager.list_sessions(limit=10)
        assert len(sessions) >= 1
        session = sessions[0]
        for tab in session.tabs.values():
            if tab.url == "https://spa.com":
                assert tab.mode in (BrowserMode.HEADLESS, BrowserMode.SCREENSHOT)
                break

    def test_browser_extract_uses_fallback(self, tmp_path):
        """browser_extract also walks the fallback ladder."""
        backend = FullBackend(static_content="D" * 300)
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_extract", {"url": "https://example.com"}))
        out = result.get("output", "")
        assert "D" * 100 in out

    def test_browser_extract_fallback_to_cdp(self, tmp_path):
        """browser_extract escalates when static is too short."""
        backend = FullBackend(static_content="short", cdp_text="E" * 300)
        reg, _ = self._make_registry(backend, tmp_path)
        result = run(reg.execute("browser_extract", {"url": "https://spa.com"}))
        out = result.get("output", "")
        assert "E" * 100 in out


class TestApprovedOriginSnapshotRefresh:
    @staticmethod
    async def _approve(_request):
        return "once"

    def _registry(self, backend, tmp_path):
        reg = ToolRegistry()
        reg.set_approval_handler(self._approve)
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(reg, manager=manager, backend=backend)
        return reg

    def test_cross_origin_refresh_blocks_before_static_or_second_read(self, tmp_path):
        class RedirectBackend(FullBackend):
            async def interactive_state(self, *, tab_id="default", url=""):
                return "https://evil.example/landing", "Evil", "EVIL " + "x" * 300

            async def interactive_get_text(self, *, tab_id="", url=""):
                raise AssertionError("origin-checked state text must be used atomically")

        backend = RedirectBackend(static_content="EVIL STATIC " + "x" * 300)
        reg = self._registry(backend, tmp_path)
        opened = run(reg.execute(
            "browser_open",
            {"url": "https://safe.example/start", "extract": False},
        ))
        assert not opened["error"]

        refreshed = run(reg.execute("browser_snapshot", {"refresh": True}))
        assert "Cross-origin redirect blocked" in refreshed["error"]
        assert backend.extract_calls == []
        assert backend.get_text_calls == []

    def test_same_origin_refresh_uses_text_from_checked_state(self, tmp_path):
        class SameOriginBackend(FullBackend):
            async def interactive_state(self, *, tab_id="default", url=""):
                return (
                    "https://safe.example/final",
                    "Safe",
                    "SAFE STATE " + "s" * 300,
                )

            async def interactive_get_text(self, *, tab_id="", url=""):
                raise AssertionError("a second navigation/read would reopen the race")

        backend = SameOriginBackend(static_content="UNSAFE STATIC " + "x" * 300)
        reg = self._registry(backend, tmp_path)
        run(reg.execute(
            "browser_open",
            {"url": "https://safe.example/start", "extract": False},
        ))

        refreshed = run(reg.execute("browser_snapshot", {"refresh": True}))
        assert not refreshed["error"]
        assert "SAFE STATE" in refreshed["output"]
        assert backend.extract_calls == []

    def test_no_interactive_backend_does_not_use_static_redirect_path(self, tmp_path):
        class TrackedStaticBackend(StaticOnlyBackend):
            def __init__(self):
                super().__init__("UNSAFE STATIC " + "x" * 300)
                self.extract_calls = []

            async def extract(self, url: str, *, max_length: int = 12000) -> str:
                self.extract_calls.append(url)
                return await super().extract(url, max_length=max_length)

        backend = TrackedStaticBackend()
        reg = self._registry(backend, tmp_path)
        run(reg.execute(
            "browser_open",
            {"url": "https://safe.example/start", "extract": False},
        ))

        refreshed = run(reg.execute("browser_snapshot", {"refresh": True}))
        assert "requires an interactive backend" in refreshed["output"]
        assert backend.extract_calls == []


# ---------------------------------------------------------------------------
# discover_chrome_debug_ports / list_existing_targets (unit)
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discover_no_chrome(self):
        """No Chrome running → empty list."""
        from agent.runtime.cdp_backend import discover_chrome_debug_ports
        # Scan a port range that's very unlikely to have Chrome
        result = run(discover_chrome_debug_ports(ports=[59876, 59877]))
        assert result == []

    def test_list_targets_no_chrome(self):
        """No Chrome → empty list."""
        from agent.runtime.cdp_backend import list_existing_targets
        result = run(list_existing_targets(port=59878))
        assert result == []

    def test_discover_with_mock_server(self):
        """Mock a /json/version endpoint → discovered."""
        import http.server
        import threading
        import json

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    body = json.dumps({
                        "Browser": "Chrome/146.0",
                        "webSocketDebuggerUrl": "ws://127.0.0.1:0/devtools/browser/fake",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass  # silence

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        from agent.runtime.cdp_backend import discover_chrome_debug_ports
        result = run(discover_chrome_debug_ports(ports=[port]))
        server.server_close()

        assert len(result) == 1
        assert result[0]["port"] == port
        assert "Chrome" in result[0]["version"]


# ---------------------------------------------------------------------------
# browser_connect tool
# ---------------------------------------------------------------------------

class TestBrowserConnectTool:
    def test_connect_tool_registered(self, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = FullBackend()
        register_browser_tools(reg, manager=manager, backend=backend)
        assert "browser_connect" in reg.tool_names

    def test_connect_no_interactive_backend(self, tmp_path):
        """Without either interactive transport, connect returns a clear error."""
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(reg, manager=manager)
        result = run(reg.execute("browser_connect", {}))
        out = result.get("output", "") + result.get("error", "")
        assert "[Browser Error]" in out
        assert "interactive browser backend is required" in out

    def test_connect_no_chrome_found(self, tmp_path):
        """With CDP backend but no Chrome running → helpful message."""
        from agent.runtime.cdp_backend import CdpBrowserBackend
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = CdpBrowserBackend("/fake/chrome")
        register_browser_tools(reg, manager=manager, backend=backend)

        # Patch discover to return empty
        with patch("agent.runtime.cdp_backend.discover_chrome_debug_ports", new_callable=AsyncMock, return_value=[]):
            result = run(reg.execute("browser_connect", {}))
        out = result.get("output", "")
        assert "No running Chrome" in out
        assert "--remote-debugging-port" in out

    def test_connect_success_creates_active_tracked_tab(self, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = FullBackend()
        register_browser_tools(reg, manager=manager, backend=backend)

        connected = run(reg.execute("browser_connect", {}))
        assert "Tracked as browser tab" in connected["output"]

        snapshot = run(reg.execute("browser_snapshot", {}))
        assert "https://example.com" in snapshot["output"]
        assert "page content" in snapshot["output"]
