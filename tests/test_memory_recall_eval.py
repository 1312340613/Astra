"""Memory recall evaluation suite.

Quantifies precision, recall, contradiction safety, and stale filtering
of the MemoryRecordRepository against a seeded dataset of realistic
user facts, preferences, episodes, and observations.

Run with:
    pytest tests/test_memory_recall_eval.py -v
"""

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.memory_records import MemoryRecordRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now(offset_hours: float = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(repo: MemoryRecordRepository, records: list[dict]) -> list[str]:
    """Insert records and return their IDs."""
    ids = []
    for rec in records:
        rid = rec.pop("id", f"eval-{uuid.uuid4().hex[:8]}")
        repo.add(
            record_id=rid,
            kind=rec.get("kind", "user_fact"),
            content=rec["content"],
            source_session_id=rec.get("session", "eval-session"),
            source_message_id=rec.get("message", "eval-msg"),
            confidence=rec.get("confidence", 0.9),
            salience=rec.get("salience", 0.5),
            tags=rec.get("tags", ()),
            valid_from=rec.get("valid_from", _now(-24)),
            valid_until=rec.get("valid_until", ""),
        )
        ids.append(rid)
    return ids


# ---------------------------------------------------------------------------
# Seed data: synthetic fictional facts
# ---------------------------------------------------------------------------

# Keep this dataset wholly fictional. Private Hindsight or conversation facts
# must never be copied into a source-controlled evaluation fixture.
SEED_FACTS = [
    {"id": "f1", "kind": "user_fact", "content": "测试用户在Northstar University读数据科学，2030年9月入学", "tags": ("education", "northstar"), "salience": 0.9},
    {"id": "f2", "kind": "user_fact", "content": "测试用户住在Pine Court公寓", "tags": ("housing",), "salience": 0.7},
    {"id": "f3", "kind": "preference", "content": "图像偏好极简蓝白海报与柔和自然光", "tags": ("image", "preference"), "salience": 0.8},
    {"id": "f4", "kind": "user_fact", "content": "测试用户5公里跑步成绩25分钟，下次目标24分钟", "tags": ("fitness",), "salience": 0.6},
    {"id": "f5", "kind": "preference", "content": "CanvasFlow默认使用monochrome线稿与0.8风格强度", "tags": ("canvasflow", "image"), "salience": 0.7},
    {"id": "f6", "kind": "user_fact", "content": "测试模型端口：9101=Model-A，9102=Model-B", "tags": ("infra", "models"), "salience": 0.5},
    {"id": "f7", "kind": "preference", "content": "测试用户不喜欢被称为小可爱", "tags": ("persona", "boundary"), "salience": 0.8},
    {"id": "f8", "kind": "user_fact", "content": "测试用户修读Northstar与Westbridge联合课程", "tags": ("education",), "salience": 0.6},
    {"id": "f9", "kind": "episode", "content": "2030-07-20测试CanvasFlow累计生成12张海报", "tags": ("image", "testing"), "salience": 0.4},
    {"id": "f10", "kind": "observation", "content": "CanvasFlow结构化标签比自然语言提示更稳定", "tags": ("canvasflow", "prompting"), "salience": 0.6},
    {"id": "f11", "kind": "user_fact", "content": "9月3日抵达Northstar City，9月10日完成注册", "tags": ("travel", "northstar"), "salience": 0.7},
    {"id": "f12", "kind": "preference", "content": "测试邮箱只读摘要和草稿，不自动发送", "tags": ("email", "boundary"), "salience": 0.8},
]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    db_path = str(tmp_path / "eval_memory.db")
    r = MemoryRecordRepository(db_path)
    _seed(r, [dict(rec) for rec in SEED_FACTS])
    return r


# ---------------------------------------------------------------------------
# 1. Recall precision: query returns relevant records
# ---------------------------------------------------------------------------

PRECISION_QUERIES = [
    ("Northstar University 数据科学", ["f1"], "education query should find university fact"),
    ("Pine Court 公寓", ["f2"], "housing query should find fictional residence"),
    ("极简 蓝白 海报", ["f3"], "image query should find visual preference"),
    ("5公里 25分钟", ["f4"], "fitness query should find running record"),
    ("CanvasFlow 线稿 monochrome", ["f5"], "image query should find monochrome preset"),
    ("小可爱", ["f7"], "boundary query should find nickname preference"),
    ("邮箱 摘要 草稿 自动发送", ["f12"], "email query should find email preference"),
]


class TestRecallPrecision:
    @pytest.mark.parametrize("query,expected_ids,desc", PRECISION_QUERIES)
    def test_precision(self, repo, query, expected_ids, desc):
        results = repo.recall(query, limit=5)
        result_ids = {r.record_id for r in results}
        for eid in expected_ids:
            assert eid in result_ids, (
                f"{desc}: expected {eid} in results for query '{query}', "
                f"got {result_ids}"
            )


# ---------------------------------------------------------------------------
# 2. Recall completeness: all relevant records found
# ---------------------------------------------------------------------------

class TestRecallCompleteness:
    def test_education_facts_both_found(self, repo):
        """Both fictional education facts should be recallable."""
        r1 = repo.recall("Northstar University", limit=5)
        r2 = repo.recall("Westbridge", limit=5)
        ids1 = {r.record_id for r in r1}
        ids2 = {r.record_id for r in r2}
        assert "f1" in ids1, "University fact not recalled"
        assert "f8" in ids2, "Joint-course fact not recalled"

    def test_multi_tag_query(self, repo):
        """Query matching multiple tags should return relevant records."""
        results = repo.recall("CanvasFlow image", limit=5)
        ids = {r.record_id for r in results}
        # Should find at least one comfyui-related record
        assert ids & {"f3", "f5", "f9", "f10"}, (
            f"No CanvasFlow/image records found: {ids}"
        )

    def test_kind_filter(self, repo):
        """Kind filter should restrict results."""
        prefs = repo.recall("", kinds=("preference",), limit=20)
        for r in prefs:
            assert r.kind == "preference", f"Non-preference in filtered results: {r.kind}"
        assert len(prefs) >= 3, f"Expected >=3 preferences, got {len(prefs)}"


# ---------------------------------------------------------------------------
# 3. Contradiction safety: superseded facts must not be recalled
# ---------------------------------------------------------------------------

class TestContradictionSafety:
    def test_superseded_fact_not_recalled(self, repo):
        """After superseding, old fact must not appear in recall."""
        # Supersede f4 (running time 25 -> 24 minutes)
        repo.supersede(
            "f4",
            content="测试用户5公里跑步成绩24分钟，下次目标23分钟",
            source_session_id="eval-update",
            source_message_id="eval-msg-2",
        )
        results = repo.recall("5公里 跑步", limit=5)
        ids = {r.record_id for r in results}
        assert "f4" not in ids, "Superseded fact f4 still recalled"
        # New fact should be there
        new_contents = [r.content for r in results]
        assert any("24分钟" in c for c in new_contents), (
            f"Superseding fact not found in results: {new_contents}"
        )

    def test_superseded_chain(self, repo):
        """Double supersede: only the latest fact should be active."""
        repo.supersede(
            "f2",
            content="测试用户搬到新公寓 Cedar House",
            source_session_id="s2",
            source_message_id="m2",
        )
        # Find the new record's ID
        results = repo.recall("公寓", limit=5)
        new_id = None
        for r in results:
            if "Cedar House" in r.content:
                new_id = r.record_id
                break
        assert new_id, "First superseding record not found"

        # Supersede again
        repo.supersede(
            new_id,
            content="测试用户住在 Maple Residences",
            source_session_id="s3",
            source_message_id="m3",
        )
        results2 = repo.recall("Maple Residences", limit=5)
        ids2 = {r.record_id for r in results2}
        assert "f2" not in ids2, "Original fact still active"
        assert new_id not in ids2, "First supersede still active"
        assert any("Maple Residences" in r.content for r in results2), (
            "Latest superseding fact not found"
        )


# ---------------------------------------------------------------------------
# 4. Stale filtering: expired records must not be recalled
# ---------------------------------------------------------------------------

class TestStaleFiltering:
    def test_expired_record_not_recalled(self, repo):
        """Records past valid_until should not appear."""
        # Insert a record that expired yesterday
        repo.add(
            record_id="stale1",
            kind="episode",
            content="临时约束：今天不要提CanvasFlow",
            source_session_id="eval",
            source_message_id="eval",
            valid_from=_now(-48),
            valid_until=_now(-24),  # expired 24h ago
        )
        results = repo.recall("CanvasFlow", limit=10)
        ids = {r.record_id for r in results}
        assert "stale1" not in ids, "Expired record still recalled"

    def test_future_valid_from_not_recalled(self, repo):
        """Records with valid_from in the future should not appear."""
        repo.add(
            record_id="future1",
            kind="user_fact",
            content="用户下周开始新工作",
            source_session_id="eval",
            source_message_id="eval",
            valid_from=_now(168),  # 1 week from now
        )
        results = repo.recall("新工作", limit=5)
        ids = {r.record_id for r in results}
        assert "future1" not in ids, "Future record recalled prematurely"

    def test_forget_removes_from_recall(self, repo):
        """Forgotten records must not be recalled."""
        repo.forget("f9")
        results = repo.recall("CanvasFlow 12张", limit=5)
        ids = {r.record_id for r in results}
        assert "f9" not in ids, "Forgotten record still recalled"


# ---------------------------------------------------------------------------
# 5. Provenance: every recalled record has source info
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_all_records_have_provenance(self, repo):
        results = repo.recall("", limit=20)
        for r in results:
            assert r.source_session_id, f"Missing session for {r.record_id}"
            assert r.created_at, f"Missing created_at for {r.record_id}"
            assert 0 <= r.confidence <= 1, f"Bad confidence for {r.record_id}"
            assert 0 <= r.salience <= 1, f"Bad salience for {r.record_id}"

    def test_record_to_dict_roundtrip(self, repo):
        results = repo.recall("Northstar University", limit=1)
        assert results, "No results for Northstar University"
        d = results[0].to_dict()
        assert d["id"] == results[0].record_id
        assert d["kind"] == results[0].kind
        assert isinstance(d["tags"], list)
        assert isinstance(d["metadata"], dict)


# ---------------------------------------------------------------------------
# 6. CJK recall: Chinese text should be searchable
# ---------------------------------------------------------------------------

class TestCjkRecall:
    def test_chinese_phrase_recall(self, repo):
        """Chinese phrases should match via LIKE fallback."""
        results = repo.recall("极简蓝白海报", limit=5)
        ids = {r.record_id for r in results}
        assert "f3" in ids, f"Chinese phrase not recalled: {ids}"

    def test_partial_chinese_match(self, repo):
        results = repo.recall("跑步 目标", limit=5)
        ids = {r.record_id for r in results}
        assert "f4" in ids, f"Partial Chinese match failed: {ids}"

    def test_mixed_cjk_english(self, repo):
        results = repo.recall("CanvasFlow 线稿 monochrome", limit=5)
        ids = {r.record_id for r in results}
        assert "f5" in ids, f"Mixed CJK/English recall failed: {ids}"
