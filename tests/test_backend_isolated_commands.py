"""Unit tests for isolated-mode command gating in the backend."""

import asyncio

import pytest

import agent.cli.backend as backend_module
from agent.cli.backend import (
    _isolated_mode_blocks_command,
    _session_recall_auto_logging_enabled,
    _start_channel_manager,
)


@pytest.mark.parametrize("command", ["/yolo", "/yolo on", "/yolo off", "/yolo status"])
def test_yolo_is_allowed_in_minimal_only(command: str) -> None:
    assert not _isolated_mode_blocks_command(
        command, bar_active=False, minimal_active=True, local_active=False
    )
    assert _isolated_mode_blocks_command(
        command, bar_active=True, minimal_active=False, local_active=False
    )
    assert _isolated_mode_blocks_command(
        command, bar_active=False, minimal_active=False, local_active=True
    )
    assert not _isolated_mode_blocks_command(
        command, bar_active=False, minimal_active=False, local_active=False
    )


def test_isolated_modes_still_allow_control_commands() -> None:
    for command in ("/cancel", "/reset", "/think", "/think on"):
        assert not _isolated_mode_blocks_command(
            command, bar_active=True, minimal_active=False, local_active=False
        )
        assert not _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=True, local_active=False
        )
        assert not _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=False, local_active=True
        )


def test_vision_tiles_is_allowed_in_isolated_modes() -> None:
    for command in ("/vision-tiles", "/vision-tiles status", "/vision-tiles off"):
        assert not _isolated_mode_blocks_command(
            command, bar_active=True, minimal_active=False, local_active=False
        )
        assert not _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=True, local_active=False
        )
        assert not _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=False, local_active=True
        )


def test_work_context_commands_are_blocked_in_isolated_modes() -> None:
    for command in (
        "/mode high",
        "/session work",
        "/permissions mode safe",
        "/sandbox local",
        "/persona lyra",
    ):
        assert _isolated_mode_blocks_command(
            command, bar_active=True, minimal_active=False, local_active=False
        )
        assert _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=True, local_active=False
        )
        assert _isolated_mode_blocks_command(
            command, bar_active=False, minimal_active=False, local_active=True
        )



def test_session_recall_auto_logging_is_disabled_in_isolated_modes() -> None:
    assert _session_recall_auto_logging_enabled(
        bar_active=False, minimal_active=False, local_active=False
    )
    assert not _session_recall_auto_logging_enabled(
        bar_active=True, minimal_active=False, local_active=False
    )
    assert not _session_recall_auto_logging_enabled(
        bar_active=False, minimal_active=True, local_active=False
    )
    assert not _session_recall_auto_logging_enabled(
        bar_active=False, minimal_active=False, local_active=True
    )
    assert not _session_recall_auto_logging_enabled(
        bar_active=True, minimal_active=True, local_active=True
    )



class _BusyPortChannelManager:
    async def start(self):
        raise OSError(10048, "address already in use")

    async def stop(self):
        pass


def test_start_channel_manager_suppresses_port_in_use_error(monkeypatch):
    sent = []
    monkeypatch.setattr(backend_module, "_send", lambda event: sent.append(event))

    result = asyncio.run(
        _start_channel_manager(_BusyPortChannelManager())
    )

    assert isinstance(result, OSError)
    assert sent == []
