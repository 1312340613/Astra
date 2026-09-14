import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageOps, features

from agent.cli.images import build_image_message_content
from agent.core.msg import ContentBlock
from agent.runtime import vision_preprocessor as vision_module
from agent.runtime.vision_policy import VisionPreprocessPolicy
from agent.runtime.vision_preprocessor import (
    VisionPreprocessError,
    VisionPreprocessor,
    VisionTileSelectionError,
    tile_origins,
)


def _write_pattern_png(path: Path, size: tuple[int, int]) -> Path:
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize(size)
    vertical = Image.linear_gradient("L").resize(size)
    image = Image.merge("RGB", (horizontal, vertical, Image.new("L", size, 127)))
    image.save(path)
    return path


def _blocks_for(path: Path):
    return build_image_message_content(str(path))


def _bare_data_block(path: Path, mime: str = "image/png") -> ContentBlock:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return ContentBlock.image_url(f"data:{mime};base64,{encoded}", detail="original")


def _pixel_bytes(image: Image.Image) -> tuple[str, tuple[int, int], bytes]:
    return image.mode, image.size, image.tobytes()


def _manifest_path(prepared) -> Path:
    assert prepared.binding is not None
    return prepared.binding.overview_path.parent / "manifest.json"


def _read_manifest(prepared) -> dict:
    return json.loads(_manifest_path(prepared).read_text(encoding="utf-8"))


def _write_manifest(prepared, manifest: dict) -> None:
    _manifest_path(prepared).write_text(json.dumps(manifest), encoding="utf-8")


def _inline_png_bytes(path: Path) -> int:
    return len("data:image/png;base64,") + 4 * ((path.stat().st_size + 2) // 3)


def test_tile_origins_anchor_far_edge_without_thin_sliver():
    assert tile_origins(1600, 768, 64) == (0, 704, 832)
    assert tile_origins(768, 768, 64) == (0,)
    assert tile_origins(300, 768, 64) == (0,)


@pytest.mark.parametrize(
    ("length", "tile_size", "overlap"),
    [(0, 768, 64), (900, 0, 0), (900, 768, -1), (900, 768, 768)],
)
def test_tile_origins_reject_invalid_geometry(length, tile_size, overlap):
    with pytest.raises(ValueError):
        tile_origins(length, tile_size, overlap)


def test_tiles_are_exact_normalized_source_crops(tmp_path):
    source = _write_pattern_png(tmp_path / "wide.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / ".astra" / "image-cache" / "tiles")
    prepared = processor.prepare_path(
        source,
        policy=VisionPreprocessPolicy(),
        session_id="session-a",
        request_id="request-a",
        source_ordinal=1,
    )
    assert prepared.binding is not None
    with Image.open(source) as original:
        normalized = ImageOps.exif_transpose(original)
        for tile in prepared.binding.tiles:
            with Image.open(tile.path) as cropped:
                assert _pixel_bytes(cropped) == _pixel_bytes(normalized.crop(tile.bounds))
                assert cropped.width <= 768 and cropped.height <= 768


def test_one_pass_bundle_contains_overview_and_all_tiles_under_global_budget(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    bundle = processor.prepare_blocks(
        _blocks_for(source), VisionPreprocessPolicy(), True, "s", "r"
    )
    images = [block for block in bundle.chat_blocks if block.type == "image_url"]
    assert len(images) == 7  # one overview plus six direct crops
    assert "original-pixel detail tiles" in bundle.status
    assert bundle.storage_blocks == tuple(_blocks_for(source))


def test_overflow_sends_only_overview_then_selects_at_most_remaining_budget(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    bundle = processor.prepare_blocks(
        _blocks_for(source), VisionPreprocessPolicy(), True, "s", "r"
    )
    images = [block for block in bundle.chat_blocks if block.type == "image_url"]
    assert len(images) == 1
    binding = processor.bindings_for("s", "r")[0]
    selected = processor.select_tiles(
        binding.tile_set_id,
        [tile.tile_id for tile in binding.tiles[:11]],
        session_id="s",
        request_id="r",
    )
    assert len(selected.data_urls) == 11


def test_selection_returns_verified_immutable_data_after_cache_mutation(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]
    tile = binding.tiles[0]
    original = tile.path.read_bytes()

    selection = processor.select_tiles(
        binding.tile_set_id,
        [tile.tile_id],
        session_id="s",
        request_id="r",
    )
    tile.path.write_bytes(b"mutated after selection")

    assert len(selection.data_urls) == 1
    assert base64.b64decode(selection.data_urls[0].split(",", 1)[1]) == original
    assert len(selection.data_urls[0].encode("ascii")) <= 41_943_040
    assert selection.remaining_inline_bytes == (
        VisionPreprocessPolicy().max_inline_body_bytes
        - VisionPreprocessor._data_url_length(binding.overview_path.stat().st_size)
        - len(selection.data_urls[0].encode("ascii"))
    )


def test_concurrent_selection_is_one_atomic_budget_and_dedup_transaction(monkeypatch, tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    probe = VisionPreprocessor(tmp_path / "probe").prepare_path(
        source, VisionPreprocessPolicy(), "probe", "probe", 1
    )
    assert probe.binding is not None
    overview_bytes = VisionPreprocessor._data_url_length(probe.binding.overview_path.stat().st_size)
    detail_budget = sum(tile.inline_bytes for tile in probe.binding.tiles[:11])
    policy = VisionPreprocessPolicy(max_inline_body_bytes=overview_bytes + detail_budget)
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), policy, True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]
    requested = [tile.tile_id for tile in binding.tiles[:11]]
    original_validate = processor._verified_tile_data_url
    arrived_threads: set[int] = set()
    arrivals_lock = threading.Lock()
    both_arrived = threading.Event()

    def synchronize_first_validation(active_binding, tile, state):
        thread_id = threading.get_ident()
        with arrivals_lock:
            first_for_thread = thread_id not in arrived_threads
            arrived_threads.add(thread_id)
            if len(arrived_threads) >= 2:
                both_arrived.set()
        if first_for_thread:
            both_arrived.wait(timeout=0.5)
        return original_validate(active_binding, tile, state)

    monkeypatch.setattr(processor, "_verified_tile_data_url", synchronize_first_validation)
    start = threading.Barrier(3)

    def select_concurrently():
        start.wait()
        try:
            return processor.select_tiles(binding.tile_set_id, requested, "s", "r"), None
        except VisionTileSelectionError as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(select_concurrently)
        second = executor.submit(select_concurrently)
        start.wait()
        outcomes = [first.result(), second.result()]

    selections = [selection for selection, _error in outcomes if selection is not None]
    errors = [error for _selection, error in outcomes if error is not None]
    selected_data_urls = [url for selection in selections for url in selection.data_urls]
    selected_ids = [
        label.split(" ", 1)[0]
        for selection in selections
        for label in selection.labels
    ]
    assert len(selected_data_urls) <= 11
    assert sum(len(url.encode("ascii")) for url in selected_data_urls) <= detail_budget
    assert len(selected_ids) == len(set(selected_ids))
    assert len(errors) == 1
    assert "already-selected" in str(errors[0])


def test_concurrent_selection_release_and_prepare_leave_no_resurrected_request(
    monkeypatch, tmp_path
):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    blocks = _blocks_for(source)
    processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]
    validation_started = threading.Event()
    allow_validation = threading.Event()
    release_started = threading.Event()
    prepare_started = threading.Event()
    original_validate = processor._verified_tile_data_url

    def pause_validation(active_binding, tile, state):
        validation_started.set()
        assert allow_validation.wait(timeout=1)
        return original_validate(active_binding, tile, state)

    monkeypatch.setattr(processor, "_verified_tile_data_url", pause_validation)

    def release_during_selection():
        release_started.set()
        processor.release_request("s", "r")

    def prepare_during_selection():
        prepare_started.set()
        try:
            return processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")
        except VisionPreprocessError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=3) as executor:
        selection = executor.submit(
            processor.select_tiles,
            binding.tile_set_id,
            [binding.tiles[0].tile_id],
            "s",
            "r",
        )
        assert validation_started.wait(timeout=1)
        release = executor.submit(release_during_selection)
        prepare = executor.submit(prepare_during_selection)
        assert release_started.wait(timeout=1)
        assert prepare_started.wait(timeout=1)
        allow_validation.set()
        assert len(selection.result().data_urls) == 1
        release.result()
        prepare_result = prepare.result()

    assert isinstance(prepare_result, (vision_module.PreparedVisionBundle, VisionPreprocessError))
    assert processor.bindings_for("s", "r") == ()
    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(
            binding.tile_set_id, [binding.tiles[0].tile_id], "s", "r"
        )
    assert processor._request_locks == {}


def test_multiple_overflow_sources_share_one_twelve_image_budget(tmp_path):
    first = _write_pattern_png(tmp_path / "one.png", (768, 9000))
    second = _write_pattern_png(tmp_path / "two.png", (768, 9000))
    with Image.open(second) as opened:
        distinct = opened.copy()
    distinct.putpixel((0, 0), (255, 0, 0))
    distinct.save(second)
    processor = VisionPreprocessor(tmp_path / "cache")
    bundle = processor.prepare_blocks(
        [*_blocks_for(first), *_blocks_for(second)], VisionPreprocessPolicy(), True, "s", "r"
    )
    assert sum(block.type == "image_url" for block in bundle.chat_blocks) == 2
    binding = processor.bindings_for("s", "r")[0]
    requested = [tile.tile_id for tile in binding.tiles[:11]]
    selection = processor.select_tiles(binding.tile_set_id, requested, "s", "r")
    assert len(selection.data_urls) == 10
    assert len(selection.unserved_ids) == 1


def test_selection_preserves_requested_order_and_cumulative_body_budget(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    probe = VisionPreprocessor(tmp_path / "probe").prepare_path(
        source, VisionPreprocessPolicy(), "probe", "probe", 1
    )
    assert probe.binding is not None
    overview_bytes = VisionPreprocessor._data_url_length(probe.binding.overview_path.stat().st_size)
    first_bytes = probe.binding.tiles[0].inline_bytes
    second_bytes = probe.binding.tiles[1].inline_bytes
    policy = VisionPreprocessPolicy(
        max_inline_body_bytes=overview_bytes + first_bytes + second_bytes - 1
    )
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), policy, True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]
    requested = [binding.tiles[1].tile_id, binding.tiles[0].tile_id]

    selection = processor.select_tiles(binding.tile_set_id, requested, "s", "r")

    assert len(selection.data_urls) == 1
    assert base64.b64decode(selection.data_urls[0].split(",", 1)[1]) == binding.tiles[1].path.read_bytes()
    assert selection.unserved_ids == (binding.tiles[0].tile_id,)
    assert selection.remaining_inline_bytes < binding.tiles[0].inline_bytes
    with pytest.raises(VisionTileSelectionError, match="already-selected"):
        processor.select_tiles(binding.tile_set_id, [binding.tiles[1].tile_id], "s", "r")


def test_selection_expires_modified_artifact_without_charging_shared_budget(tmp_path):
    first = _write_pattern_png(tmp_path / "one.png", (768, 9000))
    second = _write_pattern_png(tmp_path / "two.png", (768, 9000))
    with Image.open(second) as opened:
        distinct = opened.copy()
    distinct.putpixel((0, 0), (255, 0, 0))
    distinct.save(second)
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(
        [*_blocks_for(first), *_blocks_for(second)], VisionPreprocessPolicy(), True, "s", "r"
    )
    first_binding, second_binding = processor.bindings_for("s", "r")
    first_binding.tiles[0].path.write_bytes(b"not the cached tile")

    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(
            first_binding.tile_set_id, [first_binding.tiles[0].tile_id], "s", "r"
        )
    selection = processor.select_tiles(
        second_binding.tile_set_id, [second_binding.tiles[0].tile_id], "s", "r"
    )

    assert len(selection.data_urls) == 1
    assert base64.b64decode(selection.data_urls[0].split(",", 1)[1]) == second_binding.tiles[0].path.read_bytes()
    assert selection.remaining_images == 9


def test_prepare_blocks_retry_reuses_bundle_and_binding_without_double_charge(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    blocks = _blocks_for(source)

    first = processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")
    second = processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")

    assert second == first
    assert len(processor.bindings_for("s", "r")) == 1
    binding = processor.bindings_for("s", "r")[0]
    selection = processor.select_tiles(
        binding.tile_set_id, [tile.tile_id for tile in binding.tiles[:11]], "s", "r"
    )
    assert len(selection.data_urls) == 11


def test_distinct_identical_attachment_occurrences_are_charged_but_same_retry_is_idempotent(
    tmp_path,
):
    source = _write_pattern_png(tmp_path / "same-large.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    blocks = _blocks_for(source)

    first = processor.prepare_blocks(
        blocks,
        VisionPreprocessPolicy(),
        True,
        "s",
        "r",
        occurrence_id="tool:read-1",
    )
    replay = processor.prepare_blocks(
        blocks,
        VisionPreprocessPolicy(),
        True,
        "s",
        "r",
        occurrence_id="tool:read-1",
    )

    assert replay == first
    second = processor.prepare_blocks(
        blocks,
        VisionPreprocessPolicy(),
        True,
        "s",
        "r",
        occurrence_id="tool:read-2",
    )
    assert second != first
    assert sum(block.type == "image_url" for block in first.chat_blocks) == 7
    assert sum(block.type == "image_url" for block in second.chat_blocks) == 1
    state = processor._requests[("s", "r")]
    assert state.used_images == 8
    assert state.used_inline_bytes == sum(
        len(block.data["url"].encode("ascii"))
        for bundle in (first, second)
        for block in bundle.chat_blocks
        if block.type == "image_url"
    )


def test_expired_bundle_retry_rebinds_without_stale_id_or_double_charge(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    blocks = _blocks_for(source)
    first = processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")
    first_binding = processor.bindings_for("s", "r")[0]
    first_binding.tiles[0].path.write_bytes(b"changed")
    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(
            first_binding.tile_set_id, [first_binding.tiles[0].tile_id], "s", "r"
        )

    retried = processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "r")
    rebound = processor.bindings_for("s", "r")

    assert retried != first
    assert len(rebound) == 1
    assert rebound[0].tile_set_id != first_binding.tile_set_id
    assert first_binding.tile_set_id not in "\n".join(
        block.data.get("text", "") for block in retried.chat_blocks
    )
    selection = processor.select_tiles(
        rebound[0].tile_set_id, [tile.tile_id for tile in rebound[0].tiles[:11]], "s", "r"
    )
    assert len(selection.data_urls) == 11


def test_cache_pruning_preserves_active_binding_until_request_release(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]

    assert processor.prune_cache(max_bytes=0) == 0
    assert binding.tiles[0].path.is_file()

    processor.release_request("s", "r")
    assert processor.prune_cache(max_bytes=0) > 0
    assert not binding.tiles[0].path.exists()


def test_cache_pruning_counts_pinned_bytes_before_removing_inactive_candidate(tmp_path):
    inactive_source = _write_pattern_png(tmp_path / "inactive.png", (900, 900))
    active_source = _write_pattern_png(tmp_path / "active.png", (768, 9000))
    with Image.open(inactive_source) as opened:
        distinct = opened.copy()
    distinct.putpixel((0, 0), (255, 0, 0))
    distinct.save(inactive_source)
    processor = VisionPreprocessor(tmp_path / "cache")
    inactive = processor.prepare_path(
        inactive_source, VisionPreprocessPolicy(), "unused", "unused", 1
    )
    assert inactive.binding is not None
    processor.prepare_blocks(
        _blocks_for(active_source), VisionPreprocessPolicy(), True, "s", "r"
    )
    active = processor.bindings_for("s", "r")[0]
    inactive_dir = inactive.binding.overview_path.parent
    active_dir = active.overview_path.parent
    inactive_size = VisionPreprocessor._directory_size(inactive_dir)
    active_size = VisionPreprocessor._directory_size(active_dir)

    removed = processor.prune_cache(max_bytes=inactive_size + active_size - 1)

    assert removed == inactive_size
    assert not inactive_dir.exists()
    assert active_dir.is_dir()


def test_selection_rejects_duplicates_unknown_cross_request_and_expired_ids(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]
    tile_id = binding.tiles[0].tile_id
    with pytest.raises(VisionTileSelectionError, match="duplicate"):
        processor.select_tiles(binding.tile_set_id, [tile_id, tile_id], "s", "r")
    with pytest.raises(VisionTileSelectionError, match="unknown"):
        processor.select_tiles(binding.tile_set_id, ["missing"], "s", "r")
    with pytest.raises(VisionTileSelectionError, match="request"):
        processor.select_tiles(binding.tile_set_id, [tile_id], "s", "other")
    with pytest.raises(VisionTileSelectionError, match="session"):
        processor.select_tiles(binding.tile_set_id, [tile_id], "other", "r")
    processor.release_request("s", "r")
    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(binding.tile_set_id, [tile_id], "s", "r")
    with pytest.raises(VisionPreprocessError, match="expired"):
        processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "s", "r")
    assert processor.bindings_for("s", "r") == ()


def test_releasing_unknown_requests_does_not_retain_lifecycle_state(tmp_path):
    processor = VisionPreprocessor(tmp_path / "cache")

    for index in range(2_048):
        processor.release_request("missing", f"request-{index}")

    assert processor._request_locks == {}
    assert len(processor._released_requests) == 0
    assert len(processor._expired_tile_sets) == 0


def test_state_less_release_expires_an_already_registered_prepare_waiter(tmp_path):
    processor = VisionPreprocessor(tmp_path / "cache")
    key = ("race", "request")
    held_guard = processor._request_guard(key)
    held_guard.__enter__()
    record = processor._request_locks[key]
    release_started = threading.Event()
    prepare_started = threading.Event()

    def release_first():
        release_started.set()
        processor.release_request(*key)

    def prepare_second():
        prepare_started.set()
        try:
            return processor.prepare_blocks(
                [ContentBlock.image_url("https://example.com/image.png")],
                VisionPreprocessPolicy(),
                True,
                *key,
            )
        except VisionPreprocessError as exc:
            return exc

    def wait_for_users(expected):
        for _attempt in range(1_000):
            with processor._state_lock:
                if record.users == expected:
                    return
            threading.Event().wait(0.001)
        pytest.fail(f"request guard did not reach {expected} registered users")

    with ThreadPoolExecutor(max_workers=2) as executor:
        release = executor.submit(release_first)
        try:
            assert release_started.wait(timeout=1)
            wait_for_users(2)
            prepare = executor.submit(prepare_second)
            assert prepare_started.wait(timeout=1)
            wait_for_users(3)
        finally:
            held_guard.__exit__(None, None, None)
        release.result()
        prepare_result = prepare.result()

    assert isinstance(prepare_result, VisionPreprocessError)
    assert "expired" in str(prepare_result)
    assert not processor._requests
    assert processor._request_locks == {}
    assert len(processor._released_requests) == 0


def test_completed_request_lifecycle_tombstones_are_bounded_and_recent_ids_expire(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "seed", "seed")
    template = processor.bindings_for("seed", "seed")[0]
    processor.release_request("seed", "seed")
    first_key = ("s", "completed-0")
    first_tile_set_id = "tiles-" + f"{0:032x}"
    recent_key = ("s", "completed-2047")
    recent_tile_set_id = "tiles-" + f"{2_047:032x}"

    for index in range(2_048):
        key = ("s", f"completed-{index}")
        tile_set_id = "tiles-" + f"{index:032x}"
        binding = replace(
            template,
            tile_set_id=tile_set_id,
            session_id=key[0],
            request_id=key[1],
        )
        state = vision_module._RequestBudget(
            max_images=12,
            max_inline_bytes=vision_module.MAX_REQUEST_INLINE_BYTES,
            bindings=[binding],
        )
        with processor._state_lock:
            processor._requests[key] = state
            processor._bindings[tile_set_id] = binding
        processor.release_request(*key)

    assert processor._request_locks == {}
    assert len(processor._released_requests) == vision_module._MAX_RECENT_RELEASED_REQUESTS
    assert len(processor._expired_tile_sets) == vision_module._MAX_RECENT_EXPIRED_TILE_SETS
    assert next(iter(processor._released_requests)) == ("s", "completed-1024")
    assert next(iter(processor._expired_tile_sets)) == "tiles-" + f"{1_024:032x}"
    assert not processor._requests
    assert not processor._bindings
    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(recent_tile_set_id, ["image1-r1c1"], *recent_key)
    with pytest.raises(VisionPreprocessError, match="expired"):
        processor.prepare_blocks(
            [ContentBlock.image_url("https://example.com/image.png")],
            VisionPreprocessPolicy(),
            True,
            *recent_key,
        )
    with pytest.raises(VisionTileSelectionError, match="unknown"):
        processor.select_tiles(first_tile_set_id, ["image1-r1c1"], *first_key)


def test_selection_rejects_non_string_ids_instead_of_coercing_them(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_blocks(_blocks_for(source), VisionPreprocessPolicy(), True, "s", "r")
    binding = processor.bindings_for("s", "r")[0]

    with pytest.raises(VisionTileSelectionError, match="strings"):
        processor.select_tiles(binding.tile_set_id, [binding.tiles[0].tile_id, 1], "s", "r")
    with pytest.raises(VisionTileSelectionError, match="non-empty string"):
        processor.select_tiles(1, [binding.tiles[0].tile_id], "s", "r")


def test_external_url_is_unchanged_and_not_marked_protected(tmp_path):
    original = ContentBlock.image_url("https://example.com/large.png", detail="original")
    bundle = VisionPreprocessor(tmp_path / "cache").prepare_blocks(
        [original], VisionPreprocessPolicy(), True, "s", "r"
    )
    assert bundle.chat_blocks == (original,)
    assert bundle.protected is False
    assert "external URL" in bundle.status


def test_large_bare_data_url_is_tiled_without_trusting_a_caller_path(tmp_path):
    source = _write_pattern_png(tmp_path / "large-bare.png", (1600, 900))
    original = _bare_data_block(source)
    cache = tmp_path / "cache"
    processor = VisionPreprocessor(cache)

    bundle = processor.prepare_blocks(
        [original], VisionPreprocessPolicy(), True, "s", "large-data"
    )

    assert bundle.protected is True
    assert bundle.storage_blocks == (original,)
    assert sum(block.type == "image_url" for block in bundle.chat_blocks) == 7
    assert not any(
        ".staging" in str(block.data.get("source_path", ""))
        for block in bundle.chat_blocks
    )
    assert not (cache / ".staging").exists()


def test_small_bare_data_url_remains_direct_and_temporary_file_is_removed(tmp_path):
    source = _write_pattern_png(tmp_path / "small-bare.png", (64, 48))
    original = _bare_data_block(source)
    cache = tmp_path / "cache"

    bundle = VisionPreprocessor(cache).prepare_blocks(
        [original], VisionPreprocessPolicy(), True, "s", "small-data"
    )

    assert bundle.chat_blocks == (original,)
    assert bundle.storage_blocks == (original,)
    assert bundle.protected is False
    assert not (cache / ".staging").exists()


def test_bare_data_url_rejects_invalid_mime_and_base64_without_leaking_input(tmp_path):
    processor = VisionPreprocessor(tmp_path / "private-cache")
    invalid = (
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,not-valid-%%%",
    )

    for value in invalid:
        with pytest.raises(VisionPreprocessError) as caught:
            processor.prepare_blocks(
                [ContentBlock.image_url(value)],
                VisionPreprocessPolicy(),
                True,
                "s",
                "invalid-data",
            )
        message = str(caught.value)
        assert value not in message
        assert str(tmp_path) not in message
    assert not (tmp_path / "private-cache" / ".staging").exists()


def test_bare_data_url_enforces_encoded_and_decoded_size_limits(monkeypatch, tmp_path):
    source = _write_pattern_png(tmp_path / "bounded.png", (32, 32))
    block = _bare_data_block(source)

    monkeypatch.setattr(vision_module, "MAX_DATA_URL_ENCODED_BYTES", 32)
    with pytest.raises(VisionPreprocessError, match="encoded size limit"):
        VisionPreprocessor(tmp_path / "encoded-cache").prepare_blocks(
            [block], VisionPreprocessPolicy(), True, "s", "encoded"
        )

    monkeypatch.setattr(vision_module, "MAX_DATA_URL_ENCODED_BYTES", 1_000_000)
    monkeypatch.setattr(vision_module, "MAX_DATA_URL_DECODED_BYTES", 8)
    with pytest.raises(VisionPreprocessError, match="decoded size limit"):
        VisionPreprocessor(tmp_path / "decoded-cache").prepare_blocks(
            [block], VisionPreprocessPolicy(), True, "s", "decoded"
        )


def test_automatic_cache_pruning_waits_for_all_active_bindings(tmp_path):
    source = _write_pattern_png(tmp_path / "shared-large.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / "cache", max_cache_bytes=0)
    overflow_policy = VisionPreprocessPolicy(max_images_per_request=2)
    processor.prepare_blocks(
        _blocks_for(source), overflow_policy, True, "s", "first"
    )
    first = processor.bindings_for("s", "first")[0]
    processor.prepare_blocks(
        _blocks_for(source), overflow_policy, True, "s", "second"
    )
    second = processor.bindings_for("s", "second")[0]
    assert first.overview_path.parent == second.overview_path.parent
    derived_dir = first.overview_path.parent

    processor.release_request("s", "first")

    assert derived_dir.is_dir()
    processor.release_request("s", "second")
    assert not derived_dir.exists()


def test_automatic_cache_prune_failure_does_not_break_request_release(
    caplog, monkeypatch, tmp_path
):
    source = _write_pattern_png(tmp_path / "large.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / "cache", max_cache_bytes=0)
    processor.prepare_blocks(
        _blocks_for(source),
        VisionPreprocessPolicy(max_images_per_request=2),
        True,
        "s",
        "r",
    )
    binding = processor.bindings_for("s", "r")[0]
    monkeypatch.setattr(
        processor,
        "prune_cache",
        lambda **_kwargs: (_ for _ in ()).throw(
            VisionPreprocessError("private cleanup detail")
        ),
    )

    processor.release_request("s", "r")

    assert "private cleanup detail" not in caplog.text
    assert "Automatic vision cache pruning failed" in caplog.text
    assert processor.bindings_for("s", "r") == ()
    with pytest.raises(VisionTileSelectionError, match="expired"):
        processor.select_tiles(
            binding.tile_set_id,
            [binding.tiles[0].tile_id],
            "s",
            "r",
        )


def test_release_prunes_partial_artifacts_when_prepare_failed_before_state_commit(tmp_path):
    first = _write_pattern_png(tmp_path / "first-large.png", (1600, 900))
    corrupt = tmp_path / "second-corrupt.png"
    corrupt.write_bytes(b"not an image")
    processor = VisionPreprocessor(tmp_path / "cache", max_cache_bytes=0)
    blocks = [
        *_blocks_for(first),
        ContentBlock.image_url(
            "data:image/png;base64,bm90IGFuIGltYWdl",
            source_path=str(corrupt),
        ),
    ]

    with pytest.raises(VisionPreprocessError, match="Unable to decode inline image data"):
        processor.prepare_blocks(blocks, VisionPreprocessPolicy(), True, "s", "partial")

    derived = [
        path
        for path in (tmp_path / "cache").iterdir()
        if path.is_dir() and re.fullmatch(r"[0-9a-f]{64}", path.name)
    ]
    assert derived
    assert ("s", "partial") not in processor._requests

    processor.release_request("s", "partial")

    assert not any(path.exists() for path in derived)


def test_primary_prepare_error_is_not_masked_by_staging_cleanup_failure(
    caplog, monkeypatch, tmp_path
):
    source = _write_pattern_png(tmp_path / "small.png", (32, 32))
    processor = VisionPreprocessor(tmp_path / "cache")
    original_unlink = Path.unlink

    def deny_staging_unlink(path, *args, **kwargs):
        if ".staging" in path.parts and path.name.startswith("source."):
            raise PermissionError("private cleanup detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_staging_unlink)
    monkeypatch.setattr(
        processor,
        "prepare_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            VisionPreprocessError("primary decode failure")
        ),
    )

    with pytest.raises(VisionPreprocessError, match="primary decode failure"):
        processor.prepare_blocks(
            [_bare_data_block(source)], VisionPreprocessPolicy(), True, "s", "primary"
        )

    assert "private cleanup detail" not in caplog.text
    assert "Temporary inline image cleanup" in caplog.text
    assert processor._pending_staging_paths

    monkeypatch.setattr(Path, "unlink", original_unlink)
    processor.release_request("s", "primary")
    assert not (tmp_path / "cache" / ".staging").exists()
    assert not processor._pending_staging_paths


def test_processor_startup_sweeps_only_confined_stale_staging(tmp_path):
    cache = tmp_path / "cache"
    stale = cache / ".staging" / ("a" * 32)
    stale.mkdir(parents=True)
    (stale / "source.png").write_bytes(b"private inline bytes")

    processor = VisionPreprocessor(cache)

    assert processor._pending_staging_paths == set()
    assert not (cache / ".staging").exists()


def test_processor_startup_never_follows_staging_symlink(caplog, tmp_path):
    cache = tmp_path / "cache"
    staging = cache / ".staging"
    outside = tmp_path / "outside"
    staging.mkdir(parents=True)
    outside.mkdir()
    private = outside / "keep.txt"
    private.write_text("keep", encoding="utf-8")
    candidate = staging / ("b" * 32)
    try:
        candidate.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    VisionPreprocessor(cache)

    assert private.read_text(encoding="utf-8") == "keep"
    assert candidate.is_symlink()
    assert "Stale inline image staging cleanup did not complete safely" in caplog.text
    assert str(outside) not in caplog.text


def test_body_budget_forces_overflow_and_more_than_twelve_sources_is_rejected(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (1600, 900))
    tiny_budget = VisionPreprocessPolicy(max_inline_body_bytes=1000)
    processor = VisionPreprocessor(tmp_path / "cache")
    with pytest.raises(VisionPreprocessError, match="request budget"):
        processor.prepare_blocks(_blocks_for(source), tiny_budget, True, "s", "r")
    many = []
    for index in range(13):
        path = _write_pattern_png(tmp_path / f"small-{index}.png", (32, 32))
        many.extend(_blocks_for(path))
    with pytest.raises(VisionPreprocessError, match="split the request"):
        processor.prepare_blocks(many, VisionPreprocessPolicy(), True, "s", "many")


def test_overflow_manifest_exposes_only_opaque_and_logical_ids(tmp_path):
    source = _write_pattern_png(tmp_path / "long.png", (768, 9000))
    cache = tmp_path / "private-cache"
    bundle = VisionPreprocessor(cache).prepare_blocks(
        [ContentBlock.text("inspect small text"), *_blocks_for(source)],
        VisionPreprocessPolicy(),
        True,
        "s",
        "r",
    )
    text = "\n".join(
        block.data["text"] for block in bundle.chat_blocks if block.type == "text"
    )

    assert bundle.chat_blocks[0] == ContentBlock.text("inspect small text")
    assert "tile_set_id=tiles-" in text
    assert "overview label r1c1 maps to tile_id image1-r1c1" in text
    assert "image1-r1c1=[0,768)x[0,768)" in text
    assert str(cache) not in text
    assert not re.search(r"\b[0-9a-f]{64}\b", text)


def test_adjacent_tiles_overlap_by_64_pixels(tmp_path):
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        _write_pattern_png(tmp_path / "wide.png", (1600, 400)),
        VisionPreprocessPolicy(),
        "s",
        "r",
        1,
    )
    assert prepared.binding is not None
    first, second = prepared.binding.tiles[:2]
    assert first.bounds[2] - second.bounds[0] == 64


def test_tiles_have_deterministic_row_major_ids_and_half_open_bounds(tmp_path):
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        _write_pattern_png(tmp_path / "large.png", (1600, 900)),
        VisionPreprocessPolicy(),
        "s",
        "r",
        2,
    )
    assert prepared.binding is not None
    assert [tile.tile_id for tile in prepared.binding.tiles] == [
        "image2-r1c1",
        "image2-r1c2",
        "image2-r1c3",
        "image2-r2c1",
        "image2-r2c2",
        "image2-r2c3",
    ]
    assert [tile.bounds for tile in prepared.binding.tiles] == [
        (0, 0, 768, 768),
        (704, 0, 1472, 768),
        (832, 0, 1600, 768),
        (0, 132, 768, 900),
        (704, 132, 1472, 900),
        (832, 132, 1600, 900),
    ]


def test_exif_orientation_is_applied_before_coordinates(tmp_path):
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (900, 1600), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, exif=exif)
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        source, VisionPreprocessPolicy(), "s", "r", 1
    )
    assert prepared.binding is not None
    assert prepared.binding.normalized_size == (1600, 900)


def test_animated_gif_that_needs_tiling_is_rejected(tmp_path):
    source = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (900, 900), color) for color in ("red", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], loop=0)
    with pytest.raises(VisionPreprocessError, match="animated image"):
        VisionPreprocessor(tmp_path / "cache").prepare_path(
            source, VisionPreprocessPolicy(), "s", "r", 1
        )


def test_single_frame_gif_tiles_and_small_animated_gif_stays_direct(tmp_path):
    static = tmp_path / "static.gif"
    Image.new("RGB", (900, 900), "green").save(static)
    processor = VisionPreprocessor(tmp_path / "cache")
    assert processor.prepare_path(static, VisionPreprocessPolicy(), "s", "static", 1).binding
    animated = tmp_path / "small-animated.gif"
    frames = [Image.new("RGB", (64, 64), color) for color in ("red", "blue")]
    frames[0].save(animated, save_all=True, append_images=frames[1:], loop=0)
    assert processor.prepare_path(animated, VisionPreprocessPolicy(), "s", "animated", 1).direct


@pytest.mark.parametrize(("format_name", "extension", "feature_name"), [
    ("WEBP", "webp", "webp"),
    ("PNG", "png", "zlib"),
])
def test_animated_webp_and_apng_are_direct_only_when_small(
    tmp_path, format_name, extension, feature_name
):
    if not features.check(feature_name):
        pytest.skip(f"{format_name} animation support is unavailable in this Pillow build")
    processor = VisionPreprocessor(tmp_path / "cache")
    for label, size in (("small", (64, 64)), ("large", (900, 900))):
        source = tmp_path / f"{label}.{extension}"
        frames = [Image.new("RGBA", size, color) for color in ("red", "blue")]
        frames[0].save(
            source,
            format=format_name,
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
            lossless=True,
        )
        with Image.open(source) as animated:
            assert getattr(animated, "is_animated", False)
            assert animated.n_frames == 2
        if label == "small":
            assert processor.prepare_path(
                source, VisionPreprocessPolicy(), "s", f"{format_name}-small", 1
            ).direct
        else:
            with pytest.raises(VisionPreprocessError, match="animated image"):
                processor.prepare_path(
                    source, VisionPreprocessPolicy(), "s", f"{format_name}-large", 1
                )


@pytest.mark.parametrize(("extension", "format_name"), [("png", "PNG"), ("jpg", "JPEG"), ("gif", "GIF"), ("webp", "WEBP")])
def test_supported_static_formats_produce_lossless_normalized_crops(tmp_path, extension, format_name):
    if format_name == "WEBP" and not features.check("webp"):
        pytest.skip("WebP support is unavailable in this Pillow build")
    source = tmp_path / f"source.{extension}"
    Image.new("RGB", (900, 900), (23, 117, 201)).save(source, format=format_name)
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        source, VisionPreprocessPolicy(), "s", format_name, 1
    )
    assert prepared.binding is not None
    with Image.open(source) as original:
        normalized = ImageOps.exif_transpose(original)
        for tile in prepared.binding.tiles:
            with Image.open(tile.path) as cropped:
                assert _pixel_bytes(cropped) == _pixel_bytes(normalized.crop(tile.bounds))


def test_corrupt_and_decompression_bomb_inputs_fail_closed(monkeypatch, tmp_path):
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    processor = VisionPreprocessor(tmp_path / "cache")
    with pytest.raises(VisionPreprocessError, match="decode"):
        processor.prepare_path(corrupt, VisionPreprocessPolicy(), "s", "r", 1)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    bomb = _write_pattern_png(tmp_path / "bomb.png", (30, 30))
    with pytest.raises(VisionPreprocessError, match="pixel limit"):
        processor.prepare_path(bomb, VisionPreprocessPolicy(), "s", "r2", 1)


def test_corrupt_source_error_does_not_expose_local_paths(tmp_path):
    source = tmp_path / "private-source-name.png"
    source.write_bytes(b"not an image")
    cache = tmp_path / "private-cache-root"

    with pytest.raises(VisionPreprocessError) as caught:
        VisionPreprocessor(cache).prepare_path(
            source, VisionPreprocessPolicy(), "s", "r", 1
        )

    message = str(caught.value)
    assert message.startswith("Unable to decode image data.")
    assert str(source) not in message
    assert source.name not in message
    assert str(cache) not in message


def test_source_symlink_loop_is_sanitized_vision_error(tmp_path):
    first = tmp_path / "private-loop-a.png"
    second = tmp_path / "private-loop-b.png"
    try:
        first.symlink_to(second)
        second.symlink_to(first)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    cache = tmp_path / "private-cache-root"

    with pytest.raises(VisionPreprocessError) as caught:
        VisionPreprocessor(cache).prepare_path(
            first, VisionPreprocessPolicy(), "s", "r", 1
        )

    message = str(caught.value)
    # POSIX resolve() detects the loop and raises; Windows resolve() returns
    # the loop path so is_file() fails instead. Both are sanitized rejections;
    # accept either message prefix.
    assert message.startswith((
        "Unable to access image source",
        "Unable to decode image because the source is not a regular file",
    ))
    assert str(first) not in message
    assert first.name not in message
    assert second.name not in message
    assert str(cache) not in message


def test_cache_write_error_does_not_expose_paths_hashes_or_temp_ids(monkeypatch, tmp_path):
    source = _write_pattern_png(tmp_path / "private-source-name.png", (900, 900))
    cache = tmp_path / "private-cache-root"
    original_open = vision_module.os.open

    def fail_cache_write(path, flags, mode=0o777, *, dir_fd=None):
        candidate = Path(path)
        if candidate.is_relative_to(cache):
            raise OSError(f"forced write failure at {path}")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vision_module.os, "open", fail_cache_write)
    with pytest.raises(VisionPreprocessError) as caught:
        VisionPreprocessor(cache).prepare_path(
            source, VisionPreprocessPolicy(), "s", "r", 1
        )

    message = str(caught.value)
    derived = next(cache.iterdir())
    assert message.startswith("Unable to write the confined vision cache.")
    assert str(source) not in message
    assert str(cache) not in message
    assert derived.name not in message
    assert ".tmp" not in message
    assert re.search(r"[0-9a-f]{32,}", message) is None


def test_small_image_remains_direct_and_does_not_create_cache(tmp_path):
    source = _write_pattern_png(tmp_path / "small.png", (768, 768))
    cache = tmp_path / "cache"
    prepared = VisionPreprocessor(cache).prepare_path(
        source, VisionPreprocessPolicy(), "s", "r", 1
    )
    assert prepared.direct is True
    assert prepared.binding is None
    assert prepared.source_path == source.resolve()
    assert not cache.exists()


def test_overview_is_bounded_and_encoded_lengths_are_actual_data_url_lengths(tmp_path):
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        _write_pattern_png(tmp_path / "panorama.png", (5000, 900)),
        VisionPreprocessPolicy(),
        "s",
        "r",
        1,
    )
    assert prepared.binding is not None
    with Image.open(prepared.binding.overview_path) as overview:
        assert overview.width <= 800
        assert overview.height <= 800
        assert overview.width * overview.height <= 800 * 800
    for tile in prepared.binding.tiles:
        encoded = base64.b64encode(tile.path.read_bytes()).decode("ascii")
        assert tile.inline_bytes == len(f"data:image/png;base64,{encoded}")


def test_extreme_panorama_fails_before_generating_more_than_256_tiles(tmp_path):
    assert vision_module.MAX_TILES_PER_SOURCE == 256
    source = tmp_path / "extreme.png"
    Image.new("L", (200_000, 1), 127).save(source)
    cache = tmp_path / "cache"

    with pytest.raises(VisionPreprocessError, match="more than 256 detail tiles"):
        VisionPreprocessor(cache).prepare_path(
            source, VisionPreprocessPolicy(), "s", "r", 1
        )

    assert not cache.exists()


def test_overview_box_and_label_coordinates_stay_inside_image(monkeypatch):
    normalized = Image.new("RGB", (1, 100_000), "white")
    origins = tile_origins(normalized.height, 768, 64)
    tiles = [
        (f"r{row}c1", (0, y0, 1, min(y0 + 768, normalized.height)))
        for row, y0 in enumerate(origins, start=1)
    ]
    rectangles = []
    labels = []
    original_rectangle = ImageDraw.ImageDraw.rectangle
    original_text = ImageDraw.ImageDraw.text

    def record_rectangle(draw, xy, *args, **kwargs):
        rectangles.append(tuple(xy))
        return original_rectangle(draw, xy, *args, **kwargs)

    def record_text(draw, xy, *args, **kwargs):
        labels.append(tuple(xy))
        return original_text(draw, xy, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", record_rectangle)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    overview = VisionPreprocessor._annotated_overview(normalized, tiles)

    assert overview.size == (1, 800)
    assert rectangles and labels
    for x0, y0, x1, y1 in rectangles:
        assert 0 <= x0 <= x1 < overview.width
        assert 0 <= y0 <= y1 < overview.height
    for x, y in labels:
        assert 0 <= x < overview.width
        assert 0 <= y < overview.height


def test_cache_hit_reuses_files_and_policy_change_invalidates_key(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    mtimes = {tile.path: tile.path.stat().st_mtime_ns for tile in first.binding.tiles}
    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)
    assert second.binding is not None
    assert [tile.path for tile in second.binding.tiles] == list(mtimes)
    assert {path: path.stat().st_mtime_ns for path in mtimes} == mtimes
    assert second.binding.tile_set_id != first.binding.tile_set_id
    changed = processor.prepare_path(
        source, VisionPreprocessPolicy(overlap=32), "s", "r3", 1
    )
    assert changed.binding is not None
    assert changed.binding.tiles[0].path.parent != first.binding.tiles[0].path.parent


def test_completed_manifest_is_bounded_to_derived_metadata(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        source, VisionPreprocessPolicy(), "secret-session", "secret-request", 1
    )
    assert prepared.binding is not None
    manifest = _read_manifest(prepared)
    assert manifest["normalized_size"] == [900, 900]
    assert manifest["policy_version"] == 2
    assert manifest["overview"] == prepared.binding.overview_path.name
    assert manifest["overview_sha256"] == hashlib.sha256(prepared.binding.overview_path.read_bytes()).hexdigest()
    assert manifest["tiles"] == [
        {
            "tile_id": tile.tile_id.removeprefix("image1-"),
            "bounds": list(tile.bounds),
            "filename": tile.path.name,
            "inline_bytes": tile.inline_bytes,
            "sha256": hashlib.sha256(tile.path.read_bytes()).hexdigest(),
        }
        for tile in prepared.binding.tiles
    ]
    serialized = json.dumps(manifest)
    assert "secret-session" not in serialized
    assert "secret-request" not in serialized
    assert str(source) not in serialized


@pytest.mark.parametrize("tamper", ["tile_id", "bounds", "filename", "count"])
def test_cache_hit_regenerates_tampered_manifest_geometry(tmp_path, tamper):
    source = _write_pattern_png(tmp_path / "large.png", (1600, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    expected = _read_manifest(first)
    tampered = json.loads(json.dumps(expected))
    if tamper == "tile_id":
        tampered["tiles"][0]["tile_id"] = "r9c9"
    elif tamper == "bounds":
        tampered["tiles"][0]["bounds"] = [1, 0, 768, 768]
    elif tamper == "filename":
        overview = first.binding.overview_path
        tampered["tiles"][0]["filename"] = overview.name
        tampered["tiles"][0]["inline_bytes"] = _inline_png_bytes(overview)
        tampered["tiles"][0]["sha256"] = hashlib.sha256(overview.read_bytes()).hexdigest()
    else:
        tampered["tiles"].pop()
    _write_manifest(first, tampered)

    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert second.binding is not None
    assert _read_manifest(second) == expected
    assert [tile.bounds for tile in second.binding.tiles] == [
        tuple(record["bounds"]) for record in expected["tiles"]
    ]


def test_cache_hit_regenerates_corrupt_tile_even_with_adjusted_byte_count(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    expected_manifest = _read_manifest(first)
    tile = first.binding.tiles[0].path
    expected_bytes = tile.read_bytes()
    tile.write_bytes(b"not a png")
    tampered = _read_manifest(first)
    tampered["tiles"][0]["inline_bytes"] = _inline_png_bytes(tile)
    tampered["tiles"][0]["sha256"] = hashlib.sha256(tile.read_bytes()).hexdigest()
    _write_manifest(first, tampered)

    processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert tile.read_bytes() == expected_bytes
    assert _read_manifest(first) == expected_manifest


def test_cache_hit_regenerates_tile_with_wrong_png_dimensions(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    tile = first.binding.tiles[0].path
    Image.new("RGB", (1, 1), "red").save(tile)
    tampered = _read_manifest(first)
    tampered["tiles"][0]["inline_bytes"] = _inline_png_bytes(tile)
    tampered["tiles"][0]["sha256"] = hashlib.sha256(tile.read_bytes()).hexdigest()
    _write_manifest(first, tampered)

    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert second.binding is not None
    with Image.open(second.binding.tiles[0].path) as regenerated:
        assert regenerated.format == "PNG"
        assert regenerated.size == (768, 768)


def test_cache_hit_regenerates_artifact_hash_mismatch(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    expected = _read_manifest(first)
    tampered = json.loads(json.dumps(expected))
    tampered["tiles"][0]["sha256"] = "0" * 64
    _write_manifest(first, tampered)

    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert second.binding is not None
    assert _read_manifest(second) == expected


def test_cache_hit_regenerates_same_size_wrong_tile_with_coherent_hash(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    tile = first.binding.tiles[0].path
    expected_bytes = tile.read_bytes()
    with Image.open(tile) as original_tile:
        Image.new(original_tile.mode, original_tile.size, "magenta").save(tile)
    tampered = _read_manifest(first)
    tampered["tiles"][0]["inline_bytes"] = _inline_png_bytes(tile)
    tampered["tiles"][0]["sha256"] = hashlib.sha256(tile.read_bytes()).hexdigest()
    _write_manifest(first, tampered)

    processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert tile.read_bytes() == expected_bytes


def test_cache_hit_regenerates_same_size_wrong_overview_with_coherent_hash(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    overview = first.binding.overview_path
    expected_bytes = overview.read_bytes()
    with Image.open(overview) as original_overview:
        Image.new(original_overview.mode, original_overview.size, "magenta").save(overview)
    tampered = _read_manifest(first)
    tampered["overview_sha256"] = hashlib.sha256(overview.read_bytes()).hexdigest()
    _write_manifest(first, tampered)

    processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert overview.read_bytes() == expected_bytes


@pytest.mark.parametrize(("size", "promote_warning"), [
    ((2000, 2000), False),
    ((1500, 1000), True),
])
def test_cache_artifact_decompression_bomb_is_invalid_not_public_error(
    monkeypatch, tmp_path, size, promote_warning
):
    artifact = tmp_path / "oversized-dimensions.png"
    Image.new("L", size, 127).save(artifact)
    encoded = artifact.read_bytes()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    if promote_warning:
        warnings.simplefilter("error", Image.DecompressionBombWarning)

    result = VisionPreprocessor._validated_png_bytes(
        artifact,
        expected_size=size,
        expected_png_bytes=encoded,
        expected_sha256=hashlib.sha256(encoded).hexdigest(),
        max_inline_bytes=VisionPreprocessPolicy().max_inline_body_bytes,
    )

    assert result is None


def test_cache_hit_rejects_oversized_artifact_before_reading_it(monkeypatch, tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    policy = VisionPreprocessPolicy()
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, policy, "s", "r1", 1)
    assert first.binding is not None
    tile = first.binding.tiles[0].path
    with tile.open("wb") as stream:
        stream.truncate(policy.max_inline_body_bytes)
    tampered = _read_manifest(first)
    tampered["tiles"][0]["inline_bytes"] = _inline_png_bytes(tile)
    _write_manifest(first, tampered)
    original_read_bytes = Path.read_bytes

    def reject_oversized_read(path):
        if path == tile and path.stat().st_size >= policy.max_inline_body_bytes:
            raise AssertionError("oversized cache artifact was read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_oversized_read)
    second = processor.prepare_path(source, policy, "s", "r2", 1)

    assert second.binding is not None
    assert original_read_bytes(tile) != b""
    assert tile.stat().st_size < policy.max_inline_body_bytes


def test_small_request_budget_does_not_make_bounded_cache_artifacts_invalid(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    policy = VisionPreprocessPolicy(max_inline_body_bytes=1000)

    prepared = VisionPreprocessor(tmp_path / "cache").prepare_path(
        source, policy, "s", "r", 1
    )

    assert prepared.binding is not None
    assert prepared.binding.tiles


@pytest.mark.parametrize("damage", ["corrupt", "dimensions"])
def test_cache_hit_regenerates_invalid_overview_png(tmp_path, damage):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    overview = first.binding.overview_path
    if damage == "corrupt":
        overview.write_bytes(b"not a png")
    else:
        Image.new("RGB", (1, 1), "red").save(overview)
    tampered = _read_manifest(first)
    tampered["overview_sha256"] = hashlib.sha256(overview.read_bytes()).hexdigest()
    _write_manifest(first, tampered)

    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)

    assert second.binding is not None
    with Image.open(second.binding.overview_path) as regenerated:
        assert regenerated.format == "PNG"
        assert regenerated.size == (800, 800)


def test_cache_hit_regenerates_missing_referenced_file(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert first.binding is not None
    missing = first.binding.tiles[0].path
    missing.unlink()
    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)
    assert second.binding is not None
    assert missing.is_file()


def test_cache_pruning_never_removes_source_or_outside_file(tmp_path):
    source = _write_pattern_png(tmp_path / "source.png", (900, 900))
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")
    processor = VisionPreprocessor(tmp_path / "cache")
    processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r", 1)
    assert processor.prune_cache(max_bytes=1) > 0
    assert source.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cache_prune_filesystem_failure_is_sanitized(monkeypatch, tmp_path):
    source = _write_pattern_png(tmp_path / "private-source.png", (900, 900))
    cache = tmp_path / "private-cache-root"
    processor = VisionPreprocessor(cache)
    prepared = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r", 1)
    assert prepared.binding is not None
    root = cache.resolve()
    derived_hash = prepared.binding.tiles[0].path.parent.name
    original_iterdir = Path.iterdir

    def fail_root_scan(path):
        if path == root:
            raise OSError(f"forced prune failure at {root}/{derived_hash}/.artifact.{'a' * 32}.tmp")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_root_scan)
    with pytest.raises(VisionPreprocessError) as caught:
        processor.prune_cache(max_bytes=0)

    message = str(caught.value)
    assert message.startswith("Unable to prune the vision cache safely.")
    assert str(source) not in message
    assert str(cache) not in message
    assert derived_hash not in message
    assert ".tmp" not in message
    assert re.search(r"[0-9a-f]{32,}", message) is None


def test_symlinked_cache_directory_cannot_escape_root(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    prepared = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r1", 1)
    assert prepared.binding is not None
    derived_dir = prepared.binding.tiles[0].path.parent
    shutil.rmtree(derived_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        derived_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(VisionPreprocessError, match="cache root"):
        processor.prepare_path(source, VisionPreprocessPolicy(), "s", "r2", 1)
    assert list(outside.iterdir()) == []


def test_data_url_source_path_mismatch_never_substitutes_local_pixels(tmp_path):
    supplied = _write_pattern_png(tmp_path / "supplied.png", (900, 900))
    unrelated = tmp_path / "unrelated.png"
    Image.new("RGB", (900, 900), "blue").save(unrelated)
    block = _bare_data_block(supplied)
    block.data["source_path"] = str(unrelated)

    bundle = VisionPreprocessor(tmp_path / "cache").prepare_blocks(
        [block], VisionPreprocessPolicy(), True, "s", "mismatch"
    )

    images = [item for item in bundle.chat_blocks if item.type == "image_url"]
    first_detail = base64.b64decode(images[1].data["url"].split(",", 1)[1])
    with Image.open(supplied) as expected, Image.open(io.BytesIO(first_detail)) as actual:
        assert _pixel_bytes(actual) == _pixel_bytes(expected.crop((0, 0, 768, 768)))


def test_legitimate_build_image_block_uses_matching_local_source_without_staging(
    monkeypatch,
    tmp_path,
):
    source = _write_pattern_png(tmp_path / "legitimate.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    monkeypatch.setattr(
        processor,
        "_materialize_data_url",
        lambda *_args, **_kwargs: pytest.fail("matching local source must not be staged"),
    )

    bundle = processor.prepare_blocks(
        _blocks_for(source), VisionPreprocessPolicy(), True, "s", "legitimate"
    )

    assert bundle.protected is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_cache_and_staging_permissions_ignore_permissive_umask(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    cache = tmp_path / "cache"
    processor = VisionPreprocessor(cache)
    previous_umask = os.umask(0)
    try:
        staged = processor._materialize_data_url(_bare_data_block(source).data["url"])
        prepared = processor.prepare_path(
            source, VisionPreprocessPolicy(), "s", "permissions", 1
        )
    finally:
        os.umask(previous_umask)

    assert prepared.binding is not None
    private_directories = (cache, staged.parent.parent, staged.parent, prepared.binding.overview_path.parent)
    private_files = (
        staged,
        prepared.binding.overview_path,
        *[tile.path for tile in prepared.binding.tiles],
        prepared.binding.overview_path.parent / "manifest.json",
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in private_directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)
    processor._cleanup_materialized_path(staged, fail_closed=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_cache_hit_repairs_overly_broad_permissions(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (900, 900))
    processor = VisionPreprocessor(tmp_path / "cache")
    first = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "first", 1)
    assert first.binding is not None
    derived = first.binding.overview_path.parent
    artifacts = [first.binding.overview_path, *[tile.path for tile in first.binding.tiles], derived / "manifest.json"]
    processor.cache_root.chmod(0o755)
    derived.chmod(0o755)
    for artifact in artifacts:
        artifact.chmod(0o644)

    second = processor.prepare_path(source, VisionPreprocessPolicy(), "s", "second", 1)

    assert second.binding is not None
    assert stat.S_IMODE(processor.cache_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(derived.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in artifacts)


def test_repeated_staging_cleanup_failure_blocks_new_materialization_and_recovers(
    monkeypatch,
    tmp_path,
):
    source = _write_pattern_png(tmp_path / "small.png", (32, 32))
    processor = VisionPreprocessor(tmp_path / "cache")
    original_unlink = Path.unlink
    deny = True

    def maybe_deny(path, *args, **kwargs):
        if deny and ".staging" in path.parts and path.name.startswith("source."):
            raise PermissionError("private cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", maybe_deny)
    with pytest.raises(VisionPreprocessError, match="temporary inline image"):
        processor.prepare_blocks(
            [_bare_data_block(source)], VisionPreprocessPolicy(), True, "s", "first"
        )
    staging_root = tmp_path / "cache" / ".staging"
    first_entries = tuple(staging_root.iterdir())
    assert len(first_entries) == 1
    assert len(processor._pending_staging_paths) == 1

    with pytest.raises(VisionPreprocessError, match="cleanup is unhealthy"):
        processor.prepare_blocks(
            [_bare_data_block(source)], VisionPreprocessPolicy(), True, "s", "second"
        )
    assert tuple(staging_root.iterdir()) == first_entries
    assert len(processor._pending_staging_paths) == 1

    deny = False
    recovered = processor.prepare_blocks(
        [_bare_data_block(source)], VisionPreprocessPolicy(), True, "s", "third"
    )
    assert recovered.chat_blocks == (_bare_data_block(source),)
    assert not processor._pending_staging_paths
    assert not staging_root.exists()


def test_overview_must_fit_effective_policy_budget_and_exact_boundary(tmp_path):
    source = _write_pattern_png(tmp_path / "large.png", (1600, 900))
    probe = VisionPreprocessor(tmp_path / "probe").prepare_path(
        source, VisionPreprocessPolicy(), "probe", "probe", 1
    )
    assert probe.binding is not None
    overview_bytes = VisionPreprocessor._data_url_length(
        probe.binding.overview_path.stat().st_size
    )

    with pytest.raises(VisionPreprocessError, match="request budget"):
        VisionPreprocessor(tmp_path / "too-small").prepare_blocks(
            _blocks_for(source),
            VisionPreprocessPolicy(max_inline_body_bytes=1_000),
            True,
            "s",
            "small",
        )

    exact = VisionPreprocessor(tmp_path / "exact").prepare_blocks(
        _blocks_for(source),
        VisionPreprocessPolicy(max_inline_body_bytes=overview_bytes),
        True,
        "s",
        "exact",
    )
    assert sum(block.type == "image_url" for block in exact.chat_blocks) == 1


def test_overflow_overview_uses_atomically_captured_bytes_at_exact_budget(
    monkeypatch,
    tmp_path,
):
    source = _write_pattern_png(tmp_path / "overview-source.png", (1600, 900))
    probe = VisionPreprocessor(tmp_path / "probe").prepare_blocks(
        _blocks_for(source), VisionPreprocessPolicy(), True, "probe", "probe"
    )
    probe_urls = [block.data["url"] for block in probe.chat_blocks if block.type == "image_url"]
    overview_budget = len(probe_urls[0].encode("ascii"))

    processor = VisionPreprocessor(tmp_path / "cache")
    original_capture = getattr(processor, "_capture_artifact_data_url", None)
    captured = {}

    def capture_then_swap(path, *args, **kwargs):
        data_url = original_capture(path, *args, **kwargs)
        if path.name == "overview.png":
            captured["url"] = data_url
            with Image.open(path) as opened:
                replacement = Image.new("RGB", opened.size, "blue")
            replacement.save(path)
            captured["replacement"] = path.read_bytes()
        return data_url

    monkeypatch.setattr(
        processor,
        "_capture_artifact_data_url",
        capture_then_swap,
        raising=False,
    )
    bundle = processor.prepare_blocks(
        _blocks_for(source),
        VisionPreprocessPolicy(max_inline_body_bytes=overview_budget),
        True,
        "s",
        "overview-swap",
    )

    urls = [block.data["url"] for block in bundle.chat_blocks if block.type == "image_url"]
    assert captured
    assert urls == [captured["url"]]
    assert sum(len(url.encode("ascii")) for url in urls) == overview_budget
    assert base64.b64decode(urls[0].split(",", 1)[1]) != captured["replacement"]


def test_one_pass_tiles_use_atomically_captured_bytes_at_exact_budget(
    monkeypatch,
    tmp_path,
):
    source = _write_pattern_png(tmp_path / "one-pass-source.png", (1600, 900))
    probe = VisionPreprocessor(tmp_path / "probe-one-pass").prepare_blocks(
        _blocks_for(source), VisionPreprocessPolicy(), True, "probe", "one-pass"
    )
    probe_urls = [block.data["url"] for block in probe.chat_blocks if block.type == "image_url"]
    exact_budget = sum(len(url.encode("ascii")) for url in probe_urls)

    processor = VisionPreprocessor(tmp_path / "cache-one-pass")
    original_capture = getattr(processor, "_capture_artifact_data_url", None)
    captured = {}

    def capture_then_swap(path, *args, **kwargs):
        data_url = original_capture(path, *args, **kwargs)
        if path.name.startswith("r") and path.suffix == ".png" and not captured:
            captured["url"] = data_url
            with Image.open(path) as opened:
                replacement = Image.new("RGB", opened.size, "blue")
            replacement.save(path)
            captured["replacement"] = path.read_bytes()
        return data_url

    monkeypatch.setattr(
        processor,
        "_capture_artifact_data_url",
        capture_then_swap,
        raising=False,
    )
    bundle = processor.prepare_blocks(
        _blocks_for(source),
        VisionPreprocessPolicy(max_inline_body_bytes=exact_budget),
        True,
        "s",
        "one-pass-swap",
    )

    urls = [block.data["url"] for block in bundle.chat_blocks if block.type == "image_url"]
    assert captured
    assert len(urls) == len(probe_urls)
    assert captured["url"] in urls
    assert sum(len(url.encode("ascii")) for url in urls) == exact_budget
    assert base64.b64decode(captured["url"].split(",", 1)[1]) != captured["replacement"]


def test_mixed_bundle_reports_protected_local_and_unprotected_external_counts(tmp_path):
    source = _write_pattern_png(tmp_path / "local.png", (1600, 900))
    external = ContentBlock.image_url("https://example.test/external.png", detail="original")

    bundle = VisionPreprocessor(tmp_path / "cache").prepare_blocks(
        [*_blocks_for(source), external], VisionPreprocessPolicy(), True, "s", "mixed"
    )

    assert bundle.protected is False
    assert bundle.protected_local_images == 1
    assert bundle.unprotected_external_images == 1
    assert "1 protected local image" in bundle.status
    assert "1 unprotected external image" in bundle.status
