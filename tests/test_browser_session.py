"""Tests for Phase 3 browser session state machine.

Covers:
- Session lifecycle: create / get / list
- Tab lifecycle: open / get / set_current / record_snapshot
- Execution ladder: escalate / escalate_to / can_escalate / ordering
- Takeover/resume: request_takeover / resume_from_takeover / pre_takeover_mode
- Security invariant: snapshot sanitization (secrets never reach disk)
- Persistence: state survives manager re-instantiation
- Legacy backend adapter: read-only extract + status
"""

import asyncio
import json

import pytest

from agent.runtime.browser_session import (
    BrowserMode,
    BrowserSessionManager,
    LegacyBrowserExtractBackend,
    LegacyWslExtractBackend,
    TAKEOVER_REASONS,
    format_session,
    next_mode,
    sanitize_snapshot,
)


@pytest.fixture()
def manager(tmp_path):
    return BrowserSessionManager(path=tmp_path / "browser.db")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Execution ladder ordering
# ---------------------------------------------------------------------------

class TestLadder:
    def test_ladder_order(self):
        assert next_mode(BrowserMode.READ_ONLY) == BrowserMode.HEADLESS
        assert next_mode(BrowserMode.HEADLESS) == BrowserMode.SCREENSHOT
        assert next_mode(BrowserMode.SCREENSHOT) == BrowserMode.HEADED_TAKEOVER
        assert next_mode(BrowserMode.HEADED_TAKEOVER) is None

    def test_read_only_is_bottom(self):
        assert BrowserMode.READ_ONLY.value == "read_only"

    def test_headed_takeover_is_top(self):
        assert next_mode(BrowserMode.HEADED_TAKEOVER) is None


# ---------------------------------------------------------------------------
# Snapshot sanitization (security invariant)
# ---------------------------------------------------------------------------

class TestSanitize:
    @pytest.mark.parametrize("receipt", [False, True])
    def test_compact_form_survives_sanitization_and_persistence(self, manager, receipt):
        form = {"scope": "form", "groups": [{"name": "Question 1"}],
                "elements": [{"ref": "x", "name": "Normal option", "checked": False}],
                "text": "api_key=private-value"}
        payload = {"status": "verified", "after": form} if receipt else form
        clean = sanitize_snapshot(json.dumps(payload))
        parsed = json.loads(clean)
        assert "private-value" not in clean
        assert "[REDACTED]" in clean
        assert clean == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.org/form")
        manager.update_page_state(tab.tab_id, url=tab.url, snapshot=clean)
        assert manager.get_tab(tab.tab_id).last_snapshot == clean

    @pytest.mark.parametrize("text,should_redact", [
        ("Set-Cookie: session=abc123; Path=/", True),
        ("Cookie: token=secret_value", True),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", True),
        ("authorization: Basic dXNlcjpwYXNz", True),
        ("api_key=sk-1234567890abcdef", True),
        ("access_token=ghp_abcdefghijklmnop", True),
        ("password=hunter2", True),
        ("?sessionid=abc123def456", True),
        ('<input type="password" name="pwd" value="secret">', True),
        ("Normal page content with no secrets", False),
        ("The user prefers monochrome lineart", False),
        ("", False),
    ])
    def test_sanitize(self, text, should_redact):
        result = sanitize_snapshot(text)
        if should_redact:
            assert "[REDACTED]" in result, f"Should redact: {text}"
            # Original secret fragment must not survive
            assert "secret" not in result.lower() or "secret_value" not in result
        else:
            assert result == text

    def test_sanitize_preserves_normal_content(self):
        text = "Page title: Example\nBody: Hello world, this is a test page."
        assert sanitize_snapshot(text) == text

    def test_sanitize_multiple_secrets(self):
        text = "Cookie: a=1\nAuthorization: Bearer token123456\npassword=xyz"
        result = sanitize_snapshot(text)
        assert "token123456" not in result
        assert "Bearer" not in result or "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    def test_create_session(self, manager):
        session = manager.create_session(profile_ref="work")
        assert session.session_id
        assert session.profile_ref == "work"
        assert session.tabs == {}

    def test_create_session_default_profile(self, manager):
        session = manager.create_session()
        assert session.profile_ref == ""

    def test_get_session(self, manager):
        created = manager.create_session(profile_ref="test")
        fetched = manager.get_session(created.session_id)
        assert fetched is not None
        assert fetched.session_id == created.session_id
        assert fetched.profile_ref == "test"

    def test_get_unknown_session(self, manager):
        assert manager.get_session("nonexistent") is None

    def test_list_sessions(self, manager):
        s1 = manager.create_session(profile_ref="a")
        s2 = manager.create_session(profile_ref="b")
        sessions = manager.list_sessions()
        ids = {s.session_id for s in sessions}
        assert s1.session_id in ids
        assert s2.session_id in ids

    def test_profile_ref_never_stores_credentials(self, manager):
        """profile_ref is an opaque name, never a credential."""
        session = manager.create_session(profile_ref="default")
        # The ref is just a label
        assert session.profile_ref == "default"
        assert "cookie" not in session.profile_ref.lower()
        assert "token" not in session.profile_ref.lower()


# ---------------------------------------------------------------------------
# Tab lifecycle
# ---------------------------------------------------------------------------

class TestTabLifecycle:
    def test_open_tab(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com", title="Example")
        assert tab.tab_id
        assert tab.url == "https://example.com"
        assert tab.title == "Example"
        assert tab.mode == BrowserMode.READ_ONLY  # starts at bottom of ladder

    def test_open_tab_sets_current(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        updated = manager.get_session(session.session_id)
        assert updated.current_tab_id == tab.tab_id

    def test_open_tab_unknown_session(self, manager):
        with pytest.raises(KeyError):
            manager.open_tab("nonexistent", "https://example.com")

    def test_get_tab(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        fetched = manager.get_tab(tab.tab_id)
        assert fetched is not None
        assert fetched.url == "https://example.com"

    def test_get_unknown_tab(self, manager):
        assert manager.get_tab("nonexistent") is None

    def test_set_current_tab(self, manager):
        session = manager.create_session()
        t1 = manager.open_tab(session.session_id, "https://a.com")
        manager.open_tab(session.session_id, "https://b.com")
        manager.set_current_tab(session.session_id, t1.tab_id)
        updated = manager.get_session(session.session_id)
        assert updated.current_tab_id == t1.tab_id

    def test_set_current_tab_wrong_session(self, manager):
        s1 = manager.create_session()
        s2 = manager.create_session()
        tab = manager.open_tab(s1.session_id, "https://a.com")
        with pytest.raises(KeyError):
            manager.set_current_tab(s2.session_id, tab.tab_id)

    def test_record_snapshot_sanitizes(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        raw = "Page content\nCookie: session=abc123\nMore content"
        updated = manager.record_snapshot(tab.tab_id, raw)
        assert "abc123" not in updated.last_snapshot
        assert "[REDACTED]" in updated.last_snapshot
        assert "Page content" in updated.last_snapshot

    def test_record_snapshot_unknown_tab(self, manager):
        with pytest.raises(KeyError):
            manager.record_snapshot("nonexistent", "content")


# ---------------------------------------------------------------------------
# Execution ladder escalation
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_escalate_step_by_step(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        assert tab.mode == BrowserMode.READ_ONLY

        tab = manager.escalate(tab.tab_id)
        assert tab.mode == BrowserMode.HEADLESS

        tab = manager.escalate(tab.tab_id)
        assert tab.mode == BrowserMode.SCREENSHOT

        tab = manager.escalate(tab.tab_id)
        assert tab.mode == BrowserMode.HEADED_TAKEOVER

    def test_escalate_at_top_raises(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.escalate_to(tab.tab_id, BrowserMode.HEADED_TAKEOVER)
        with pytest.raises(ValueError, match="top of the ladder"):
            manager.escalate(tab.tab_id)

    def test_escalate_to(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        tab = manager.escalate_to(tab.tab_id, BrowserMode.SCREENSHOT)
        assert tab.mode == BrowserMode.SCREENSHOT

    def test_escalate_to_lower_raises(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.escalate_to(tab.tab_id, BrowserMode.SCREENSHOT)
        with pytest.raises(ValueError, match="not above"):
            manager.escalate_to(tab.tab_id, BrowserMode.HEADLESS)

    def test_can_escalate(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        assert manager.can_escalate(tab.tab_id) is True
        manager.escalate_to(tab.tab_id, BrowserMode.HEADED_TAKEOVER)
        assert manager.can_escalate(tab.tab_id) is False

    def test_escalate_unknown_tab(self, manager):
        with pytest.raises(KeyError):
            manager.escalate("nonexistent")


# ---------------------------------------------------------------------------
# Takeover / resume lifecycle
# ---------------------------------------------------------------------------

class TestTakeover:
    def test_request_takeover(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.escalate(tab.tab_id)  # -> HEADLESS
        tab = manager.request_takeover(tab.tab_id, reason="captcha")
        assert tab.mode == BrowserMode.HEADED_TAKEOVER
        assert tab.takeover_reason == "captcha"
        assert tab.pre_takeover_mode == BrowserMode.HEADLESS

    def test_takeover_from_read_only(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        tab = manager.request_takeover(tab.tab_id, reason="login")
        assert tab.mode == BrowserMode.HEADED_TAKEOVER
        assert tab.pre_takeover_mode == BrowserMode.READ_ONLY

    def test_resume_restores_pre_takeover_mode(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.escalate(tab.tab_id)  # -> HEADLESS
        manager.escalate(tab.tab_id)  # -> SCREENSHOT
        manager.request_takeover(tab.tab_id, reason="2fa")
        tab = manager.resume_from_takeover(tab.tab_id)
        assert tab.mode == BrowserMode.SCREENSHOT
        assert tab.takeover_reason == ""
        assert tab.pre_takeover_mode is None

    def test_resume_defaults_to_headless_on_null_pre_mode(self, manager):
        """The HEADLESS fallback only triggers for legacy/corrupted rows
        where pre_takeover_mode is NULL. Normal API always records it."""
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.request_takeover(tab.tab_id, reason="captcha")
        # Simulate a legacy row with no recorded pre-takeover mode
        with manager._lock, manager._connection() as db:
            db.execute(
                "UPDATE browser_tabs SET pre_takeover_mode=NULL WHERE tab_id=?",
                (tab.tab_id,),
            )
        tab = manager.resume_from_takeover(tab.tab_id)
        assert tab.mode == BrowserMode.HEADLESS

    def test_resume_restores_read_only_origin(self, manager):
        """A tab taken over straight from READ_ONLY restores to READ_ONLY."""
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.request_takeover(tab.tab_id, reason="captcha")
        tab = manager.resume_from_takeover(tab.tab_id)
        assert tab.mode == BrowserMode.READ_ONLY

    def test_resume_not_in_takeover_raises(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        with pytest.raises(ValueError, match="not in takeover"):
            manager.resume_from_takeover(tab.tab_id)

    def test_takeover_invalid_reason_defaults_unknown(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        tab = manager.request_takeover(tab.tab_id, reason="invalid_reason")
        assert tab.takeover_reason == "unknown"

    def test_takeover_valid_reasons(self):
        for reason in TAKEOVER_REASONS:
            assert reason in ("captcha", "2fa", "payment", "dialog", "login", "unknown")

    def test_is_in_takeover(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        assert manager.is_in_takeover(tab.tab_id) is False
        manager.request_takeover(tab.tab_id, reason="captcha")
        assert manager.is_in_takeover(tab.tab_id) is True
        manager.resume_from_takeover(tab.tab_id)
        assert manager.is_in_takeover(tab.tab_id) is False

    def test_double_takeover_refreshes_reason(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.request_takeover(tab.tab_id, reason="captcha")
        tab = manager.request_takeover(tab.tab_id, reason="2fa")
        assert tab.mode == BrowserMode.HEADED_TAKEOVER
        assert tab.takeover_reason == "2fa"

    def test_takeover_unknown_tab(self, manager):
        with pytest.raises(KeyError):
            manager.request_takeover("nonexistent", reason="captcha")


# ---------------------------------------------------------------------------
# Persistence across manager instances
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_session_survives_restart(self, tmp_path):
        db_path = tmp_path / "browser.db"
        m1 = BrowserSessionManager(path=db_path)
        session = m1.create_session(profile_ref="persist-test")
        tab = m1.open_tab(session.session_id, "https://example.com", title="Test")
        m1.escalate(tab.tab_id)
        m1.request_takeover(tab.tab_id, reason="captcha")
        m1.record_snapshot(tab.tab_id, "Page content\nCookie: secret=abc123")

        # New manager instance (simulates restart)
        m2 = BrowserSessionManager(path=db_path)
        session2 = m2.get_session(session.session_id)
        assert session2 is not None
        assert session2.profile_ref == "persist-test"
        tab2 = session2.tabs[tab.tab_id]
        assert tab2.mode == BrowserMode.HEADED_TAKEOVER
        assert tab2.takeover_reason == "captcha"
        # Escalated READ_ONLY -> HEADLESS, then took over, so pre-takeover is HEADLESS
        assert tab2.pre_takeover_mode == BrowserMode.HEADLESS
        # Snapshot was sanitized before storage
        assert "abc123" not in tab2.last_snapshot
        assert "[REDACTED]" in tab2.last_snapshot

    def test_multiple_tabs_persist(self, tmp_path):
        db_path = tmp_path / "browser.db"
        m1 = BrowserSessionManager(path=db_path)
        session = m1.create_session()
        t1 = m1.open_tab(session.session_id, "https://a.com")
        t2 = m1.open_tab(session.session_id, "https://b.com")
        m1.escalate(t1.tab_id)

        m2 = BrowserSessionManager(path=db_path)
        session2 = m2.get_session(session.session_id)
        assert len(session2.tabs) == 2
        assert session2.tabs[t1.tab_id].mode == BrowserMode.HEADLESS
        assert session2.tabs[t2.tab_id].mode == BrowserMode.READ_ONLY


# ---------------------------------------------------------------------------
# Legacy backend adapter
# ---------------------------------------------------------------------------

class TestLegacyBackend:
    def test_neutral_backend_is_the_legacy_wsl_alias(self):
        assert LegacyWslExtractBackend is LegacyBrowserExtractBackend

    def test_extract_via_backend(self, manager):
        async def fake_extract(url, max_length=12000):
            return f"Content of {url}\nCookie: session=secret123"

        async def fake_status():
            return True, "/usr/bin/extract"

        manager.backend = LegacyWslExtractBackend(fake_extract, fake_status)
        result = run(manager.extract("https://example.com"))
        assert "Content of https://example.com" in result
        assert "secret123" not in result  # sanitized
        assert "[REDACTED]" in result

    def test_extract_no_backend(self, manager):
        result = run(manager.extract("https://example.com"))
        assert "No browser backend" in result

    def test_backend_status(self, manager):
        async def fake_extract(url, max_length=12000):
            return ""

        async def fake_status():
            return True, "/usr/bin/extract"

        manager.backend = LegacyWslExtractBackend(fake_extract, fake_status)
        ok, detail = run(manager.backend_status())
        assert ok is True
        assert "extract" in detail

    def test_backend_status_no_backend(self, manager):
        ok, detail = run(manager.backend_status())
        assert ok is False

    def test_legacy_backend_capabilities(self):
        async def noop_extract(url, max_length=12000):
            return ""

        async def noop_status():
            return False, "not found"

        backend = LegacyWslExtractBackend(noop_extract, noop_status)
        assert backend.capabilities.read is True
        assert backend.capabilities.interactive is False
        assert backend.capabilities.takeover is False


# ---------------------------------------------------------------------------
# Format output
# ---------------------------------------------------------------------------

class TestFormat:
    def test_format_session(self, manager):
        session = manager.create_session(profile_ref="work")
        manager.open_tab(session.session_id, "https://example.com", title="Example")
        session = manager.get_session(session.session_id)
        text = format_session(session)
        assert "work" in text
        assert "https://example.com" in text
        assert "read_only" in text

    def test_format_session_with_takeover(self, manager):
        session = manager.create_session()
        tab = manager.open_tab(session.session_id, "https://example.com")
        manager.request_takeover(tab.tab_id, reason="captcha")
        session = manager.get_session(session.session_id)
        text = format_session(session)
        assert "captcha" in text
        assert "headed_takeover" in text

    def test_format_empty_session(self, manager):
        session = manager.create_session()
        text = format_session(session)
        assert "(none)" in text
