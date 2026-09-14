"""Mechanical expiry/dedup boundaries; semantic maintenance is explicit."""

from datetime import datetime, timezone, timedelta

import pytest

from agent.runtime.memory_records import MemoryRecordRepository
from agent.runtime.memory_consolidation import MemoryConsolidator


@pytest.fixture()
def repo(tmp_path):
    return MemoryRecordRepository(tmp_path / "test_memory.db")


@pytest.fixture()
def consolidator(repo):
    return MemoryConsolidator(repo)


def _past_iso(days: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _future_iso(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class TestExpireStale:
    def test_expires_past_valid_until(self, repo, consolidator):
        record = repo.add(
            kind="observation",
            content="临时状态：正在准备面试",
            valid_until=_past_iso(1),
        )
        expired = consolidator.expire_stale()
        assert expired == 1
        updated = repo.get(record.record_id)
        assert updated is not None
        assert updated.status == "expired"
        assert updated.metadata["expired_at"]
        assert updated.metadata["lifecycle_events"][-1]["event"] == "expired"

    def test_does_not_expire_future_valid_until(self, repo, consolidator):
        repo.add(
            kind="observation",
            content="长期有效的事实",
            valid_until=_future_iso(30),
        )
        expired = consolidator.expire_stale()
        assert expired == 0

    def test_does_not_expire_no_valid_until(self, repo, consolidator):
        repo.add(
            kind="preference",
            content="我喜欢白透及膝袜",
            # No valid_until — permanent
        )
        expired = consolidator.expire_stale()
        assert expired == 0

    def test_does_not_expire_already_expired(self, repo, consolidator):
        repo.add(
            kind="observation",
            content="已过期的",
            valid_until=_past_iso(1),
        )
        # Manually expire first
        consolidator.expire_stale()
        # Second run should find nothing
        expired = consolidator.expire_stale()
        assert expired == 0




def test_exact_duplicate_merge_is_idempotent_without_confidence_inflation(repo, consolidator):
    metadata = {"scope": "project-A", "maturity": "provisional", "evidence_count": 1}
    records = [repo.add(kind="observation", content="Identical fact", confidence=0.7, metadata=metadata) for _ in range(3)]
    assert consolidator.merge_duplicate_observations() == 2
    assert consolidator.merge_duplicate_observations() == 0
    active = repo.list_records(status="active")
    assert len(active) == 1
    keeper = active[0]
    assert keeper.confidence == 0.7 and keeper.metadata["maturity"] == "provisional"
    assert keeper.metadata["evidence_count"] == 1
    assert keeper.last_confirmed_at == records[0].last_confirmed_at
    assert set(keeper.metadata["merged_record_ids"]) == {r.record_id for r in records[1:]}
    assert len(repo.list_records()) == 3
    assert all(repo.get(r.record_id).content == r.content for r in records)


@pytest.mark.parametrize("changed", [
    {"content": "identical fact"}, {"content": "Identical fact."},
    {"source_session_id": "another-session"}, {"source_message_id": "another-message"},
    {"valid_until": "2099-01-01T00:00:00+00:00"},
    {"valid_from": "2020-01-01T00:00:00+00:00"},
    {"metadata": {"scope": "project-B"}}, {"metadata": {"pinned": True}},
    {"metadata": {"conflict_state": "pending"}}, {"metadata": {"unknown_scope_field": "other"}},
    {"tags": ["windows"]}, {"confidence": 0.6}, {"supersedes_id": "previous-version"},
])
def test_differing_provenance_scope_or_lifetime_cannot_merge(repo, consolidator, changed):
    first = repo.add(kind="observation", content="Identical fact")
    second = repo.add(**{**{"kind": "observation", "content": "Identical fact"}, **changed})
    assert consolidator.merge_duplicate_observations() == 0
    assert repo.get(first.record_id) == first and repo.get(second.record_id) == second


@pytest.mark.parametrize("metadata", [{"pinned": True}, {"conflict_state": "pending"}])
def test_protected_records_are_never_merged(repo, consolidator, metadata):
    for _ in range(2):
        repo.add(kind="observation", content="Identical fact", metadata=metadata)
    assert consolidator.merge_duplicate_observations() == 0


def test_episodes_are_preserved_without_extraction_or_marking(repo, consolidator):
    episode = repo.add(kind="episode", content="我喜欢表格。我正在准备下周答辩", valid_until=_future_iso(1))
    with repo._connection() as db:
        db.execute("UPDATE memory_records SET created_at=? WHERE id=?", (_past_iso(30), episode.record_id))
    before = repo.get(episode.record_id)
    assert consolidator.consolidate_episodes(min_age_days=0) == 0
    assert repo.get(episode.record_id) == before
    assert len(repo.list_records()) == 1


def test_run_all_expires_and_deduplicates_without_touching_other_kinds(repo, consolidator):
    repo.add(kind="observation", content="outdated", valid_until=_past_iso(1))
    for _ in range(2):
        repo.add(kind="observation", content="same")
        repo.add(kind="preference", content="same")
    assert consolidator.run_all() == {"expired": 1, "merged_observations": 1, "extracted_observations": 0}
    assert consolidator.run_all() == {"expired": 0, "merged_observations": 0, "extracted_observations": 0}
    assert len([r for r in repo.list_records(status="active") if r.kind == "preference"]) == 2


def test_dedup_preserves_explicit_expiry_and_full_record_archive(repo, consolidator):
    deadline = _future_iso(1)
    records = [repo.add(kind="observation", content="temporary", valid_until=deadline) for _ in range(2)]
    assert consolidator.merge_duplicate_observations() == 1
    assert all(repo.get(r.record_id).valid_until == deadline for r in records)
    assert consolidator.expire_stale(now=_future_iso(2)) == 1
    assert repo.list_records(status="active") == []


def test_memory_store_wiring(tmp_path):
    from agent.runtime.memory import MemoryStore
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    for _ in range(2):
        store.add_record(kind="observation", content="same")
    assert MemoryConsolidator(store.record_store).run_all()["merged_observations"] == 1
    assert len(store.recall_records("same", kinds=("observation",))) == 1
