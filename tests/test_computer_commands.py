"""Public slash-command and bounded TUI-state contracts for Computer Use."""

from __future__ import annotations

import asyncio

from agent.cli import computer_commands
from agent.cli.computer_commands import (
    ComputerStateEmitter,
    computer_state,
    execute_computer_command,
    format_computer_state,
)


def run(awaitable):
    return asyncio.run(awaitable)


class FakeManager:
    def __init__(self, *, permissions=None, close_blocked=False):
        self.status_calls = 0
        self.prompt_requests = 0
        self.close_calls = 0
        self.helper_started = False
        self.session_id = "session-secret-must-not-leak"
        self.target = None
        self.handed_off = False
        self.closed = False
        self.permissions = permissions or {"accessibility": True, "screen_recording": True}
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        if not close_blocked:
            self.allow_close.set()

    def capability_status(self):
        return ("available", "available")

    async def status(self):
        self.status_calls += 1
        self.helper_started = True
        return {"supported": True, "permissions": self.permissions}

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True
        self.target = None
        self.handed_off = False


def test_status_never_opens_privacy_panes(monkeypatch):
    manager = FakeManager()
    opened = []

    output, error = run(execute_computer_command(
        ["status"], manager, platform_name="darwin", open_url=opened.append,
    ))

    assert error == ""
    assert "platform: macOS" in output
    assert "permission: available" in output
    assert manager.status_calls == 1
    assert manager.prompt_requests == 0
    assert opened == []


def test_status_reads_cached_runtime_state_without_activating_helper_or_cache(tmp_path):
    class CachedRuntime(FakeManager):
        def __init__(self):
            super().__init__()
            self.cache_root = tmp_path / "computer-cache"

        def cached_status(self):
            return None

        async def status(self):
            raise AssertionError("status must not probe or start the helper")

    manager = CachedRuntime()

    output, error = run(execute_computer_command(["status"], manager, platform_name="darwin"))

    assert error == ""
    assert "probed: no" in output
    assert manager.helper_started is False
    assert not manager.cache_root.exists()


def test_setup_opens_only_missing_permission_panes():
    manager = FakeManager(permissions={"accessibility": False, "screen_recording": True})
    opened = []

    output, error = run(execute_computer_command(
        ["setup"], manager, platform_name="darwin", open_url=opened.append,
    ))

    assert error == ""
    assert "Accessibility" in output
    assert opened == ["x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]


def test_setup_rejects_unknown_permission_payload_without_opening_settings():
    manager = FakeManager()
    manager.permissions = {"accessibility": "yes", "screen_recording": None}
    opened = []

    output, error = run(execute_computer_command(
        ["setup"], manager, platform_name="darwin", open_url=opened.append,
    ))

    assert output == ""
    assert "permission status is unavailable" in error
    assert opened == []


def test_status_degrades_for_unknown_permission_payload():
    manager = FakeManager(permissions={"accessibility": True, "screen_recording": True})
    manager.permissions = {"accessibility": "unknown", "screen_recording": True}

    output, error = run(execute_computer_command(["status"], manager, platform_name="darwin"))

    assert error == ""
    assert "permission: degraded" in output


def test_setup_reports_open_failure_instead_of_claiming_success():
    manager = FakeManager(permissions={"accessibility": False, "screen_recording": True})

    def failed_open(_url):
        raise OSError("open failed")

    output, error = run(execute_computer_command(
        ["setup"], manager, platform_name="darwin", open_url=failed_open,
    ))

    assert output == ""
    assert "Unable to open macOS privacy settings" in error


def test_setup_is_unsupported_off_macos():
    manager = FakeManager()
    opened = []

    output, error = run(execute_computer_command(
        ["setup"], manager, platform_name="linux", open_url=opened.append,
    ))

    assert output == ""
    assert "only available on macOS" in error
    assert manager.status_calls == 0
    assert opened == []


def test_status_reports_unsupported_without_starting_a_runtime():
    manager = FakeManager()

    output, error = run(execute_computer_command(["status"], manager, platform_name="linux"))

    assert error == ""
    assert "platform: unsupported" in output
    assert "permission: unsupported" in output
    assert manager.status_calls == 0


def test_stop_is_idempotent_and_waits_for_one_teardown():
    manager = FakeManager(close_blocked=True)

    async def scenario():
        first = asyncio.create_task(execute_computer_command(["stop"], manager, platform_name="darwin"))
        await manager.close_started.wait()
        second = asyncio.create_task(execute_computer_command(["stop"], manager, platform_name="darwin"))
        await asyncio.sleep(0)
        assert not second.done()
        manager.allow_close.set()
        return await first, await second

    first, second = run(scenario())

    assert first == ("Computer Use stopped.", "")
    assert second == ("Computer Use stopped.", "")
    assert manager.close_calls == 1


def test_state_events_are_bounded_and_close_has_no_active_ghost():
    manager = FakeManager()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")

    emitter.observe_catalog({
        "apps": [{"app_ref": "opaque-app", "name": "WPS Office /private/secret.docx"}],
    })
    manager.target = type("Target", (), {"app_ref": "opaque-app", "window_ref": "window-secret"})()
    emitter.emit()
    manager.handed_off = True
    emitter.emit()
    manager.target = None
    manager.handed_off = False
    manager.closed = True
    emitter.emit()

    assert [event["active"] for event in events] == [True, True, False]
    assert [event["handed_off"] for event in events] == [False, True, False]
    assert [event["control"] for event in events] == ["background", "user_control", "inactive"]
    assert events[0]["application"] == "WPS Office"
    encoded = repr(events)
    assert "window-secret" not in encoded
    assert "private" not in encoded
    assert "session-secret" not in encoded
    assert "screenshot" not in encoded


def test_application_labels_remove_terminal_and_bidi_controls_but_keep_cjk_and_emoji():
    manager = FakeManager()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")
    emitter.observe_catalog({
        "apps": [{
            "app_ref": "opaque-app",
            "name": "WPS\x1b[2J\x1b]8;;https://bad.example\x07文档\u202e\U0001f469\u200d\U0001f4bb/private/secret",
        }],
    })
    manager.target = type("Target", (), {"app_ref": "opaque-app"})()

    event = emitter.emit()
    rendered = format_computer_state({**event, "platform": "macOS", "configured": True, "probed": True})

    assert event["application"] == "WPS文档👩‍💻"
    assert "\x1b" not in rendered and "\x07" not in rendered and "\u202e" not in rendered


def test_application_labels_remove_default_ignorables_and_truncate_by_grapheme():
    manager = FakeManager()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")
    long_label = "界" * 63 + "👩🏽‍💻" + "尾"
    emitter.observe_catalog({
        "apps": [
            {"app_ref": "controls", "name": "WPS\u200b\u2060文档👩🏽‍💻❤️"},
            {"app_ref": "long", "name": long_label},
        ],
    })

    manager.target = type("Target", (), {"app_ref": "controls"})()
    controlled = emitter.emit()
    manager.target = type("Target", (), {"app_ref": "long"})()
    truncated = emitter.emit()

    assert controlled["application"] == "WPS文档👩🏽‍💻❤️"
    assert truncated["application"] == "界" * 63 + "👩🏽‍💻"


def test_state_emitter_clears_active_state_on_session_rotation_and_helper_error():
    manager = FakeManager()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")
    emitter.observe_catalog({"apps": [{"app_ref": "opaque-app", "name": "Finder"}]})
    manager.target = type("Target", (), {"app_ref": "opaque-app"})()
    emitter.emit()
    manager.target = None
    manager.closed = True
    emitter.observe_session_end("session-secret-must-not-leak", "reset")
    emitter.observe_tool_error("computer_focus", {}, "helper secret text", object())

    assert [event["active"] for event in events] == [True, False, False]
    assert all(set(event) <= {"type", "permission", "active", "handed_off", "control", "application"} for event in events)


def test_state_projection_and_runtime_events_surface_cooperative_control_immediately():
    manager = FakeManager()
    manager.target = type("Target", (), {"app_ref": "opaque-app"})()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")
    emitter.observe_catalog({"apps": [{"app_ref": "opaque-app", "name": "WPS Office"}]})

    assert computer_state(manager, platform_name="darwin")["control"] == "background"
    emitter.emit()
    emitter.observe_runtime_event({
        "type": "foreground_takeover_begin",
        "application": "WPS Office",
        "action_classes": ["scroll"],
        "window_title": "private window",
        "takeover_ref": "private-takeover-ref",
    })
    emitter.observe_runtime_event({"type": "foreground_takeover_user_activity_paused"})

    assert [event["control"] for event in events] == [
        "background",
        "foreground_takeover",
        "paused",
    ]
    assert events[1]["application"] == "WPS Office"
    assert all(set(event) <= {
        "type", "permission", "active", "handed_off", "control", "application",
    } for event in events)
    assert "private window" not in repr(events)
    assert "private-takeover-ref" not in repr(events)


def test_user_control_has_priority_and_takeover_end_returns_to_background():
    manager = FakeManager()
    manager.target = type("Target", (), {"app_ref": "opaque-app"})()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")

    emitter.observe_runtime_event({"type": "foreground_takeover_begin", "application": "Finder"})
    manager.handed_off = True
    emitter.emit()
    manager.handed_off = False
    emitter.observe_runtime_event({"type": "foreground_takeover_end", "application": "Finder"})

    assert [event["control"] for event in events] == [
        "foreground_takeover",
        "user_control",
        "background",
    ]


def test_stop_clears_stale_runtime_control_idempotently():
    manager = FakeManager()
    manager.target = type("Target", (), {"app_ref": "opaque-app"})()
    events = []
    emitter = ComputerStateEmitter(manager, events.append, platform_name="darwin")
    emitter.observe_runtime_event({"type": "foreground_takeover_begin", "application": "Finder"})
    emitter.observe_runtime_event({"type": "foreground_takeover_user_activity_paused"})

    first = run(execute_computer_command(
        ["stop"], manager, platform_name="darwin", emit_state=emitter.emit,
    ))
    second = run(execute_computer_command(
        ["stop"], manager, platform_name="darwin", emit_state=emitter.emit,
    ))

    assert first == second == ("Computer Use stopped.", "")
    assert manager.close_calls == 2
    assert events[-2]["control"] == events[-1]["control"] == "inactive"


def test_takeover_approval_choices_follow_the_backend_scope():
    assert computer_commands.approval_choices({
        "kind": "computer_foreground_takeover",
        "choices": ["once", "session", "deny"],
    }) == ["once", "session", "deny"]
    assert computer_commands.approval_choices({"kind": "computer_foreground_takeover"}) == ["once", "deny"]
    assert computer_commands.approval_choices({"kind": "computer_foreground_takeover", "choices": ["once", "deny"]}) == ["once", "deny"]
    assert computer_commands.approval_choices({"kind": "tool_policy"}) == ["once", "session", "deny"]


def test_format_state_is_bounded_and_never_includes_target_or_session():
    rendered = format_computer_state({
        "platform": "macOS", "configured": True, "probed": True,
        "permission": "degraded", "active": True, "handed_off": False,
        "application": "WPS Office", "target": "private", "session_id": "secret",
    })

    assert "WPS Office" in rendered
    assert "private" not in rendered
    assert "secret" not in rendered
