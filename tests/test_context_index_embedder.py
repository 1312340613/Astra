"""M1 tests for the embedding singleton (no real model in unit tests)."""
import time

import pytest

from agent.runtime.context_index import embedder as mod


class FakeBackend:
    def __init__(self, fail_load=False):
        self.fail_load = fail_load
        self.loaded = 0
        self.calls = 0

    def load(self):
        if self.fail_load:
            raise RuntimeError("model missing")
        self.loaded += 1

    def encode(self, texts):
        self.calls += 1
        return [(float(len(t)), 1.0) for t in texts]


@pytest.fixture(autouse=True)
def _reset_singleton():
    mod._reset_for_tests()
    yield
    mod._reset_for_tests()


def test_env_switch_gates_channel(monkeypatch) -> None:
    for value in ("off", "0", "false", "no", ""):
        monkeypatch.setenv(mod.ENV_SWITCH, value)
        assert mod.get_embedder(lambda: FakeBackend()) is None
    monkeypatch.setenv(mod.ENV_SWITCH, "anything-else")
    assert mod.get_embedder(lambda: FakeBackend()) is not None  # opt-out：只有显式 falsy 才关
    monkeypatch.delenv(mod.ENV_SWITCH, raising=False)
    assert mod.get_embedder(lambda: FakeBackend()) is not None  # M3 A/B 达标后默认 on


def test_backend_loads_once_and_encodes_delegate(monkeypatch) -> None:
    monkeypatch.setenv(mod.ENV_SWITCH, "on")
    backend = FakeBackend()
    factory = lambda: backend  # noqa: E731
    first = mod.get_embedder(factory)
    second = mod.get_embedder(factory)
    assert first is second
    assert backend.loaded == 1
    assert first.encode(["hello"]) == [(5.0, 1.0)]
    assert backend.calls == 1


def test_load_failure_returns_none_and_backs_off(monkeypatch) -> None:
    monkeypatch.setenv(mod.ENV_SWITCH, "on")
    attempts = []

    def factory():
        attempts.append(1)
        return FakeBackend(fail_load=True)

    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    assert mod.get_embedder(factory) is None
    # 退避窗口内不再撞后端（失败不每请求重试）
    assert mod.get_embedder(factory) is None
    assert len(attempts) == 1
    # 退避过期后允许再试（瞬时故障不永久化）
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0 + mod.FAILURE_BACKOFF_SECONDS + 1)
    assert mod.get_embedder(factory) is None
    assert len(attempts) == 2


def test_loading_never_blocks_requesters(monkeypatch) -> None:
    # 实机复盘（00:53 重启轮）：warm 线程持锁加载 4B 时，请求线程在粗粒度锁上
    # 排队 → activity 源超 120ms deadline 整节消失。契约：加载进行中必须
    # 立即返回 None（本轮纯词法），绝不等锁。
    import threading

    release = threading.Event()

    class SlowBackend:
        def load(self):
            release.wait(5.0)

        def encode(self, texts):
            return [(1.0,) for _ in texts]

    monkeypatch.setenv(mod.ENV_SWITCH, "on")
    holder = threading.Thread(target=lambda: mod.get_embedder(SlowBackend), daemon=True)
    holder.start()
    time.sleep(0.1)  # 让 holder 真正进入 load
    t0 = time.monotonic()
    assert mod.get_embedder(SlowBackend) is None
    assert time.monotonic() - t0 < 0.5  # 非阻塞降级，不排队等锁
    release.set()
    holder.join(6.0)


def test_start_warm_requires_vectors_db(tmp_path, monkeypatch) -> None:
    # 守卫：没建过向量库的机器（CI/新用户）绝不为预热加载 4B。
    missing = tmp_path / "no-vectors.db"
    monkeypatch.setenv(mod.ENV_SWITCH, "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(missing))
    assert mod.start_warm() is None

    loaded = []

    class Spy:
        def load(self):
            loaded.append(1)

        def encode(self, texts):
            return [(0.0,) for _ in texts]

    missing.touch()  # 库存在后 warm 才上岗（线程真加载）
    thread = mod.start_warm(backend_factory=lambda: Spy())
    assert thread is not None
    thread.join(5.0)
    assert loaded == [1]
