"""Advisory routing never grants capabilities or claims fresh app acceptance."""
from agent.runtime import computer_routing as routing
from agent.runtime.macos_computer_compatibility import CompatibilityRegistryError, MacDispatchCompatibility


def test_advice_uses_exact_identity_and_distinguishes_backends(monkeypatch):
    calls = []
    def lookup(bundle, version, backend):
        calls.append((bundle, version, backend))
        if backend == 'pid_pointer':
            return MacDispatchCompatibility(backend, frozenset({'click', 'scroll'}), requires_active=True)
        if backend == 'foreground_keyboard':
            return MacDispatchCompatibility(backend, frozenset({'text'}), allow_text_entry=True)
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', lookup)
    advice = routing.computer_route_advice('example.application', '1.2.3')
    assert {b for b, _, _ in calls} == {'example.application'}
    assert {v for _, v, _ in calls} == {'1.2.3'}
    assert advice['advisory_only'] is True
    assert advice['evidence'] == 'configured'
    assert advice['real_app_acceptance'] == 'unknown'
    assert {r['backend'] for r in advice['routes']} == {'pid_pointer', 'foreground_keyboard'}
    assert all(r['suggested_mode'] == 'foreground_takeover' for r in advice['routes'])
    assert all(r['exact_plan_required'] is True for r in advice['routes'])
    assert advice['routes'][0]['enabled_actions'] == ['click', 'scroll']


def test_unknown_version_and_registry_failure_do_not_infer_routes(monkeypatch):
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', lambda *args: None)
    assert routing.computer_route_advice('example.app', 'other')['routes'] == []
    def broken(*args):
        raise CompatibilityRegistryError('private path detail')
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', broken)
    advice = routing.computer_route_advice('example.app', '1')
    assert advice['evidence'] == 'unknown'
    assert advice['routes'] == []
    assert 'private path' not in str(advice)


def test_background_cell_is_not_promoted_to_takeover(monkeypatch):
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', lambda b, v, backend:
        MacDispatchCompatibility(backend, frozenset({'click'})) if backend == 'pid_pointer' else None)
    advice = routing.computer_route_advice('example.app', '1')
    assert advice['routes'][0]['suggested_mode'] == 'background'


def test_browser_advice_requires_explicit_matching_session(monkeypatch):
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', lambda *args: None)
    advice = routing.computer_route_advice('com.microsoft.edgemac', '1')
    browser = advice['browser_advice']
    assert browser['session_binding'] == 'explicit_matching_session_required'
    assert browser['authentication_transfer'] is False
    assert browser['cdp_availability'] == 'unknown'
    assert advice['routes'] == []
    assert 'browser_advice' not in routing.computer_route_advice('example.app', '1')


def test_generic_foreground_advice_for_unlisted_app_is_not_acceptance(monkeypatch):
    monkeypatch.setattr(routing, 'exact_dispatch_compatibility', lambda *args: None)
    advice = routing.computer_route_advice('test.unlisted', '99')
    assert advice['generic_foreground']['exact_plan_required'] is True
    assert advice['generic_foreground']['uses_real_pointer'] is True
    assert advice['real_app_acceptance'] == 'unknown'
    assert advice['routes'] == []
