import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.runtime.cdp_backend import CdpBrowserBackend, CdpConnection
from agent.runtime.process_env import browser_child_environment


def test_child_environment_excludes_credentials_and_preserves_platform_settings():
    source = {"PATH": "/bin", "HOME": "/home/user", "SystemRoot": r"C:\Windows",
              "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=fixture",
              "LC_CTYPE": "UTF-8", "HTTPS_PROXY": "http://proxy:8080",
              "NO_PROXY": "127.0.0.1,localhost", "WSL_INTEROP": "/run/fixture",
              "WSLENV": "PATH/p:DEEPSEEK_API_KEY:HOME/p:PRIVATE_TOKEN/u",
              "DEEPSEEK_API_KEY": "dummy", "OPENAI_API_KEY": "dummy",
              "MAIL_PASSWORD": "dummy", "PRIVATE_TOKEN": "dummy",
              "NODE_OPTIONS": "--require injected.js", "PYTHONPATH": "/injected"}
    result = browser_child_environment(source)
    for key in ("PATH", "HOME", "SystemRoot", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                "LC_CTYPE", "HTTPS_PROXY", "NO_PROXY", "WSL_INTEROP"):
        assert result[key] == source[key]
    assert result["WSLENV"] == "PATH/p:HOME/p"
    assert not any(value == "dummy" for value in result.values())
    assert "NODE_OPTIONS" not in result and "PYTHONPATH" not in result
    assert "DEEPSEEK_API_KEY" in source  # Parent is not mutated.


def test_browser_version_probe_uses_scrubbed_environment(monkeypatch):
    captured = []

    async def spawn(*args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"Chrome fixture", b"")))

    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-do-not-forward")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    ok, _ = asyncio.run(CdpBrowserBackend("fixture-browser").status())
    assert ok and "DEEPSEEK_API_KEY" not in captured[0]["env"]
    assert captured[0]["env"]["NO_PROXY"] == "localhost,127.0.0.1"


def test_cdp_launch_and_headed_handoff_scrub_credentials(monkeypatch, tmp_path):
    import subprocess
    from agent.runtime import cdp_backend
    captured = []

    async def spawn(*args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

    def popen(*args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(cdp_backend, "_to_windows_path", lambda value: value)
    monkeypatch.setattr(CdpConnection, "_wait_for_debugger", AsyncMock(return_value="ws://localhost/browser/fixture"))
    monkeypatch.setattr(CdpConnection, "_start_wsl_relay", AsyncMock())
    monkeypatch.setattr(CdpConnection, "_find_page_websocket", AsyncMock(return_value="ws://localhost/page/fixture"))
    monkeypatch.setattr(CdpConnection, "_connect_page", AsyncMock())

    async def scenario():
        conn = CdpConnection("fixture-browser", user_data_dir=str(tmp_path))
        await conn.start()
        await conn.close()
        CdpBrowserBackend("fixture-browser").launch_headed("https://example.com")

    asyncio.run(scenario())
    assert len(captured) == 2
    assert all("OPENAI_API_KEY" not in call["env"] for call in captured)
