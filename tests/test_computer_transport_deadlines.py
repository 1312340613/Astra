"""Isolated request ownership/deadline tests: no native process or desktop input."""
import asyncio
import logging
from pathlib import Path

import pytest

from agent.runtime import macos_computer as mod
from agent.runtime.computer_backend import ComputerTarget
from agent.runtime.computer_protocol import (
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerRequest,
    ComputerResponse,
)


class FakeTransport(mod.HelperTransport):
    def __init__(self, monkeypatch, *, stage='', delay=0., timeout=.06):
        monkeypatch.setattr(mod, '_validate_helper', lambda path: path)
        super().__init__(Path('/fake/helper'), timeout=timeout)
        self.stage, self.delay = stage, delay
        self.entered = asyncio.Event()
        self.stopped = 0
        self.cleanup_gate = None
        self.cleanup_entered = asyncio.Event()
        self.current_id = ''

    async def step(self, stage):
        if self.stage == stage:
            self.entered.set()
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)

    async def _ensure_process_locked(self):
        await self.step('start')
        return object()

    async def _write_locked(self, process, request):
        self.current_id = request.request_id
        await self.step('write')

    async def _read_response_locked(self, process):
        await self.step('read')
        return ComputerResponse(self.current_id, True, result={})

    async def _stop_process_locked(self):
        self.stopped += 1
        self.cleanup_entered.set()
        if self.cleanup_gate is not None:
            await self.cleanup_gate.wait()


def request():
    return ComputerRequest('private-request-id', 'status', {})


@pytest.mark.parametrize('stage', ['start', 'write', 'read'])
def test_request_budget_bounds_every_owned_stage(monkeypatch, stage):
    async def scenario():
        transport = FakeTransport(monkeypatch, stage=stage)
        with pytest.raises(mod.HelperTransportError):
            await asyncio.wait_for(transport.request(request()), .4)
        assert transport.stopped == 1
        assert not transport._lock.locked()
    asyncio.run(scenario())


def test_request_budget_is_shared_across_stages(monkeypatch):
    async def scenario():
        transport = FakeTransport(monkeypatch, delay=.025, timeout=.06)
        with pytest.raises(mod.HelperTransportError):
            await transport.request(request())
        assert transport.stopped == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_queued_timeout_or_cancel_never_stops_owner(monkeypatch, cancel):
    async def scenario():
        transport = FakeTransport(monkeypatch)
        await transport._lock.acquire()
        queued = asyncio.create_task(transport.request(request()))
        if cancel:
            await asyncio.sleep(.01)
            queued.cancel()
        try:
            with pytest.raises(asyncio.CancelledError if cancel else mod.HelperTransportError):
                await asyncio.wait_for(queued, .4)
            assert transport.stopped == 0
            assert transport._lock.locked()
        finally:
            transport._lock.release()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['start', 'write', 'read'])
def test_owned_cancellation_cleans_up_and_retains_lock_until_done(monkeypatch, stage):
    async def scenario():
        transport = FakeTransport(monkeypatch, stage=stage)
        transport.cleanup_gate = asyncio.Event()
        task = asyncio.create_task(transport.request(request()))
        await transport.entered.wait()
        task.cancel()
        await asyncio.wait_for(transport.cleanup_entered.wait(), .3)
        task.cancel()  # Repeated cancellation cannot release framing ownership early.
        await asyncio.sleep(.01)
        assert transport._lock.locked()
        transport.cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.stopped == 1
        assert not transport._lock.locked()
    asyncio.run(scenario())


def test_timing_logs_contain_only_stage_metadata(monkeypatch, caplog):
    async def scenario():
        transport = FakeTransport(monkeypatch)
        await transport.request(request())
    with caplog.at_level(logging.DEBUG, logger=mod.__name__):
        asyncio.run(scenario())
    assert 'computer_helper_request' in caplog.text
    assert 'operation=status' in caplog.text
    assert 'private-request-id' not in caplog.text
    assert all(stage in caplog.text for stage in ('queue_ms=', 'start_ms=', 'write_ms=', 'read_ms='))


def test_over_budget_wait_batch_rejected_before_plan_rpc():
    class NoRPC:
        async def request(self, request):
            pytest.fail('over-budget plan must not reach helper')
    async def scenario():
        backend = mod.MacComputerBackend(NoRPC())
        with pytest.raises(mod.HelperApplicationError) as raised:
            await backend.plan_actions(ComputerTarget('app', 'window'), 'snapshot',
                [{'type': 'wait', 'duration_ms': 10000}, {'type': 'drag', 'x': 1., 'y': 1., 'end_x': 2., 'end_y': 2., 'target_element_ref': 'target'}],
                ComputerInteractionMode.BACKGROUND)
        assert raised.value.error.code is ComputerErrorCode.ACTION_TIMEOUT
    asyncio.run(scenario())


def test_failed_cleanup_is_bounded_and_prevents_reuse(monkeypatch):
    async def scenario():
        monkeypatch.setattr(mod, '_REQUEST_CLEANUP_TIMEOUT', .03)
        transport = FakeTransport(monkeypatch, stage='read', timeout=.03)
        transport.cleanup_gate = asyncio.Event()
        with pytest.raises(mod.HelperTransportError):
            await asyncio.wait_for(transport.request(request()), .4)
        assert not transport._lock.locked()
        with pytest.raises(mod.HelperTransportError):
            await transport.request(request())
        assert transport.stopped == 1
        transport.cleanup_gate.set()
        await transport.close()
        assert transport.stopped == 2
        assert transport._closed
    asyncio.run(scenario())


def test_over_budget_act_rejected_without_dispatch():
    class NoRPC:
        async def request(self, request):
            pytest.fail('over-budget act must not dispatch')
    async def scenario():
        backend = mod.MacComputerBackend(NoRPC())
        result = await backend.act(ComputerTarget('app', 'window'), 'snapshot',
            [{'type': 'wait', 'duration_ms': 10000}, {'type': 'wait', 'duration_ms': 1}],
            interaction_mode=ComputerInteractionMode.BACKGROUND, plan_ref='plan')
        assert result.error.code is ComputerErrorCode.ACTION_TIMEOUT
    asyncio.run(scenario())


def test_planned_delay_boundary_preserves_ordinary_actions():
    mod.MacComputerBackend._validate_planned_delay([
        {'type': 'wait', 'duration_ms': 9700}, {'type': 'drag'}, {'type': 'keypress'},
    ])
    with pytest.raises(mod.HelperApplicationError):
        mod.MacComputerBackend._validate_planned_delay([
            {'type': 'wait', 'duration_ms': 9701}, {'type': 'drag'},
        ])


@pytest.mark.parametrize('cleanup_failure', ['timeout', 'error'])
def test_owned_cancellation_survives_failed_cleanup(monkeypatch, caplog, cleanup_failure):
    async def scenario():
        monkeypatch.setattr(mod, '_REQUEST_CLEANUP_TIMEOUT', .02)
        transport = FakeTransport(monkeypatch, stage='read', timeout=.3)
        if cleanup_failure == 'timeout':
            transport.cleanup_gate = asyncio.Event()
        else:
            async def fail_cleanup():
                transport.stopped += 1
                raise mod.HelperTransportError('private cleanup payload')
            transport._stop_process_locked = fail_cleanup
        task = asyncio.create_task(transport.request(request()))
        await transport.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .4)
        assert task.cancelled()
        assert transport._request_cleanup_failed
        assert not transport._lock.locked()
        with pytest.raises(mod.HelperTransportError):
            await transport.request(request())
        assert transport.stopped == 1
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        asyncio.run(scenario())
    assert 'computer_helper_cancel_cleanup_failed' in caplog.text
    assert 'private cleanup payload' not in caplog.text
    assert 'private-request-id' not in caplog.text
