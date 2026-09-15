import pytest

from agent.cli.session_lifecycle import ControlledRestart, RESTART_EXIT_CODE


def test_restart_waits_for_idle_and_matching_delivery_ack():
    now = [0.0]
    restart = ControlledRestart(clock=lambda: now[0])
    request = restart.request()
    assert restart.draining
    assert restart.advance(busy=True, session="work") is None
    assert not restart.acknowledge(request["request_id"])
    ready = restart.advance(busy=False, session="work")
    assert ready["type"] == "restart_ready"
    assert ready["session"] == "work"
    assert not restart.acknowledge("stale")
    assert restart.acknowledge(ready["request_id"])
    assert RESTART_EXIT_CODE != 0


def test_restart_timeout_cancels_instead_of_forcing_exit():
    now = [0.0]
    restart = ControlledRestart(clock=lambda: now[0], timeout=5)
    request = restart.request()
    now[0] = 6
    event = restart.advance(busy=True, session="work")
    assert event["state"] == "cancelled"
    assert not restart.draining
    assert not restart.acknowledge(request["request_id"])


def test_restart_ack_also_expires():
    now = [0.0]
    restart = ControlledRestart(clock=lambda: now[0], timeout=5)
    restart.request()
    event = restart.advance(busy=False, session="work")
    now[0] = 6
    assert not restart.acknowledge(event["request_id"])
    assert restart.advance(busy=False, session="work")["state"] == "cancelled"


def test_repeated_request_is_idempotent_and_cancel_resets_identity():
    restart = ControlledRestart()
    first = restart.request()
    assert restart.request()["request_id"] == first["request_id"]
    assert restart.cancel()["state"] == "cancelled"
    assert restart.request()["request_id"] != first["request_id"]


def test_restart_requires_capable_frontend():
    restart = ControlledRestart(supported=False)
    with pytest.raises(ValueError, match="TUI"):
        restart.request()
