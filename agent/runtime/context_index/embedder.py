"""Process-wide warm embedding client for the Context Index.

Design: docs/context-index-platforms.md
- MLX model ownership is shared across processes by Astra's local worker.
- Lazy backend connection (never inside factory/db-touching paths).
- ``ASTRA_CONTEXT_INDEX_EMBEDDING=off`` disables the whole channel (lexical-only).
- Backend failures are cached with a bounded backoff so a hot path never retries
  a broken model per request, but a transient failure is not permanent either.
"""
from __future__ import annotations

import logging
import atexit
import importlib
import math
import os
import sys
import threading
import time
from typing import Callable, Protocol, Sequence

ENV_SWITCH = "ASTRA_CONTEXT_INDEX_EMBEDDING"
MODEL_ID = "mlx-community/Qwen3-Embedding-4B-mxfp8"
FAILURE_BACKOFF_SECONDS = 60.0

_FALSY = {"off", "0", "false", "no", ""}  # 显式设空=关，与 Astra env 惯例一致
logger = logging.getLogger(__name__)

_lock = threading.Lock()
_instance: "Embedder | None" = None
_last_failure = float("-inf")


class EmbeddingUnavailable(RuntimeError):
    """Expected cold/busy runtime state; callers retain their lexical results."""


class _EmbeddingBackend(Protocol):
    def load(self) -> None: ...

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class Embedder:
    """Thin wrapper: backend errors degrade to None (callers keep lexical results)."""

    def __init__(self, backend: _EmbeddingBackend) -> None:
        self._backend = backend
        self._encode_lock = threading.Lock()

    def encode(self, texts: Sequence[str]) -> list[tuple[float, ...]] | None:
        if not self._encode_lock.acquire(blocking=False):
            return None
        try:
            vectors = self._backend.encode(texts)
            return [tuple(float(x) for x in vector) for vector in vectors]
        except EmbeddingUnavailable:
            return None
        except Exception:
            logger.warning("embedding encode failed; channel degraded", exc_info=True)
            return None
        finally:
            self._encode_lock.release()


class _MlxBackend:
    """Worker-owned MLX model with bounded batches and no retained scratch cache."""

    def __init__(self) -> None:
        self._model = None
        self._processor = None

    def load(self) -> None:
        mx = importlib.import_module("mlx.core")
        mx.set_cache_limit(0)
        # MLX treats this as a scheduling guideline, not a hard allocation cap.
        mx.set_memory_limit(6 * 1024 ** 3)
        embeddings = importlib.import_module("mlx_embeddings")
        try:
            self._model, self._processor = embeddings.load(MODEL_ID)
            mx.eval(self._model.parameters())
        finally:
            mx.clear_cache()

    def encode(self, texts: Sequence[str]):
        mx = importlib.import_module("mlx.core")
        embeddings = importlib.import_module("mlx_embeddings")

        result = []
        for text in texts:
            output = vectors = None
            try:
                output = embeddings.generate(self._model, self._processor, texts=[text], max_length=512)
                vectors = output.text_embeds
                mx.eval(vectors)
                # tolist materializes Python numbers without thousands of lazy
                # MLX scalar slices retaining their parent computation graphs.
                result.extend(vectors.tolist())
            finally:
                output = vectors = None
                try:
                    mx.synchronize()
                finally:
                    mx.clear_cache()
        return result

    def memory_stats(self) -> dict[str, int]:
        mx = importlib.import_module("mlx.core")
        return {"active_bytes": mx.get_active_memory(), "cache_bytes": mx.get_cache_memory(),
                "peak_active_bytes": mx.get_peak_memory()}


class _HttpBackend:
    """OpenAI-compatible embeddings served by a separately managed llama.cpp."""

    def load(self) -> None:
        import httpx

        self.url = os.getenv("ASTRA_EMBEDDING_BASE_URL", "http://127.0.0.1:8088/v1").rstrip("/")
        self.model = os.getenv("ASTRA_EMBEDDING_MODEL", "Qwen3-Embedding-4B-Q4_K_M")
        self.timeout = max(0.05, float(os.getenv("ASTRA_EMBEDDING_TIMEOUT", "2")))
        self.client = httpx.Client(timeout=self.timeout, trust_env=False)
        atexit.register(self.client.close)

    def encode(self, texts: Sequence[str]):
        if not texts:
            return []
        key = os.getenv("ASTRA_EMBEDDING_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        response = self.client.post(
            self.url + "/embeddings", headers=headers,
            json={"model": self.model, "input": list(texts), "encoding_format": "float"},
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("Embedding response has missing or duplicate inputs")
        vectors = [[float(value) for value in row["embedding"]] for row in rows]
        if not vectors[0] or any(
            len(vector) != len(vectors[0]) or not all(math.isfinite(v) for v in vector)
            for vector in vectors
        ):
            raise ValueError("Embedding response has invalid vectors")
        return vectors


def backend_name() -> str:
    backend = os.getenv("ASTRA_EMBEDDING_BACKEND", "auto").strip().lower()
    if backend == "auto":
        backend = "mlx" if sys.platform == "darwin" else "llamacpp"
    return backend


def embedding_identity() -> str:
    backend = backend_name()
    if backend == "mlx":
        return "mlx:" + MODEL_ID
    return "http:" + os.getenv("ASTRA_EMBEDDING_MODEL", "Qwen3-Embedding-4B-Q4_K_M")


def _default_backend() -> _EmbeddingBackend:
    backend = backend_name()
    if backend == "mlx":
        from .embedding_runtime import SharedMlxBackend
        return SharedMlxBackend()
    if backend in {"llamacpp", "openai-compatible"}:
        return _HttpBackend()
    raise ValueError("Unknown embedding backend")


def _enabled() -> bool:
    # 默认 on（2026-09-05 M3 A/B 达标翻转：precision@2 +0.12、A3 位 0.48→1.40，
    # 全部超 ±0.05 噪声带）。显式 off 才关；缺库/缺依赖/加载失败自动休眠为词法。
    return os.getenv(ENV_SWITCH, "on").strip().lower() not in _FALSY


def ready_embedder() -> Embedder | None:
    """Read the warm instance without starting a model load on a query path."""
    return _instance if _enabled() else None


def get_embedder(backend_factory: Callable[[], _EmbeddingBackend] | None = None) -> Embedder | None:
    """Return the shared embedder, or None when disabled/unavailable (lexical fallback).

    实机契约（00:53 重启事故）：加载进行中**绝不等锁**——warm 线程持锁数秒期间，
    请求线程必须立即拿 None 走纯词法，否则 broker 120ms deadline 下整个 activity 源蒸发。
    """
    global _instance, _last_failure
    if not _enabled():
        return None
    if _instance is not None:  # 无锁快路径：warm 完成后热请求零竞争
        return _instance
    if not _lock.acquire(blocking=False):
        return None  # 有人正在加载：本轮降级词法，不排队
    try:
        if _instance is not None:
            return _instance
        if time.monotonic() - _last_failure < FAILURE_BACKOFF_SECONDS:
            return None
        factory = backend_factory or _default_backend
        try:
            backend = factory()
            backend.load()
        except Exception:
            _last_failure = time.monotonic()
            logger.warning("embedding backend unavailable; lexical fallback in use", exc_info=True)
            return None
        _instance = Embedder(backend)
        return _instance
    finally:
        _lock.release()


def start_warm(backend_factory: Callable[[], _EmbeddingBackend] | None = None) -> threading.Thread | None:
    """Preload off the request path (wired from broker factory at boot).

    Guarded twice: env off → nothing; no vectors DB (fresh install / CI) →
    nothing. A machine that never built the index must not pay 4B load cost.
    """
    if not _enabled():
        return None
    from .vector_index import default_vectors_db_path

    if not default_vectors_db_path().is_file():
        return None

    def _run() -> None:
        embedder = get_embedder(backend_factory)
        if embedder is not None:
            resume = getattr(embedder._backend, "resume", None)
            if callable(resume):
                resume()
            else:
                embedder.encode(["context index warm up"])

    thread = threading.Thread(target=_run, daemon=True, name="context-index-embed-warm")
    thread.start()
    return thread


def pause_shared_runtime() -> None:
    pause = getattr(getattr(_instance, "_backend", None), "pause", None)
    if callable(pause):
        pause()


def _reset_for_tests() -> None:
    global _instance, _last_failure
    with _lock:
        close = getattr(getattr(_instance, "_backend", None), "close", None)
        if callable(close):
            close()
        _instance = None
        _last_failure = float("-inf")
