"""Astra-owned candidate, trial and activation lifecycle.

Inspired by incremental experience editing and execution-gated skill libraries;
storage, permissions and execution remain entirely within Astra.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .instance_lock import InstanceLock
from .learning_scope import learning_scope, scope_key, scoped_skill
from .learning_evidence import (
    MAX_TRIALS_PER_REQUEST, check_result, evidence_messages, fingerprint, input_fingerprint,
    normalize_checks, source_fingerprint, source_inputs, trial_status,
)

if TYPE_CHECKING:
    from .learning import LearningStore
    from .memory import MemoryStore
    from .skills import SkillStore


class LearningLifecycle:
    def __init__(self, store: LearningStore, memory: MemoryStore, skills: SkillStore):
        self.store, self.memory, self.skills = store, memory, skills
        self._active_request: tuple[str, str] | None = None
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_candidates (
                    proposal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    content_key TEXT NOT NULL, metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_candidates_content
                    ON learning_candidates(content_key);
                CREATE TABLE IF NOT EXISTS learning_versions (
                    proposal_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL, PRIMARY KEY(proposal_id, revision)
                );
                CREATE TABLE IF NOT EXISTS learning_trials (
                    id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    status TEXT NOT NULL, trial_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_trials_proposal
                    ON learning_trials(proposal_id, revision);
                CREATE TABLE IF NOT EXISTS learning_review_sources (
                    session_id TEXT PRIMARY KEY, fingerprints_json TEXT NOT NULL
                );
            """)

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        # A separate short-lived kernel lock serializes file + DB mutations
        # between Astra processes on Windows and POSIX, without holding SQLite
        # transactions across SkillStore/MemoryStore calls.
        with self.store._lock, InstanceLock(self.store.path.with_suffix(".lifecycle.lock")):
            yield

    @staticmethod
    def scope(workspace: str | Path | None = None) -> dict[str, str]:
        return learning_scope(workspace)

    def _enabled(self) -> None:
        if self.store.mode() == "off":
            raise ValueError("Learning is off; use /learn mode review to enable autonomous learning")

    def _metadata(self, proposal_id: str) -> tuple[int, dict]:
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM learning_candidates WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError("Not a managed learning candidate; inspect with /learn show")
        return row["revision"], json.loads(row["metadata_json"])

    def managed(self, proposal_id: str) -> bool:
        with self.store._connection() as db:
            return db.execute("SELECT 1 FROM learning_candidates WHERE proposal_id=?", (proposal_id,)).fetchone() is not None

    def _target(self, proposal: dict) -> dict:
        payload = proposal["payload"]
        if not proposal["kind"].startswith("skill_"):
            return {}
        name = payload["name"]
        file_path = payload.get("file_path") or "SKILL.md"
        current = self.skills.raw_file(name, file_path)
        manifest = self.skills.raw_file(name, "SKILL.md")
        header = self.skills._frontmatter(manifest) if manifest else {}
        return {
            "name": name, "file_path": file_path,
            "fingerprint": fingerprint(current), "content": current,
            "manifest_fingerprint": fingerprint(manifest),
            "manifest_exists": manifest is not None,
            "pinned": str(header.get("pinned", "")).casefold() in {"true", "yes", "1", "on"},
        }

    @staticmethod
    def _contract(raw: dict, kind: str, *, previous: dict | None = None) -> dict:
        from .learning import _SECRET

        if not isinstance(raw, dict) or set(raw) - {"claim_type", "trigger", "benefit", "verification_plan"}:
            raise ValueError("Invalid learning contract fields")
        # Omission keeps the prior plan; explicit empty values fail before a
        # revision or replacement can be written.
        for key, value in raw.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid learning {key}: provide a nonempty string")
        merged = {**(previous or {}), **raw}
        claim_type = str(merged.get("claim_type") or ("procedure" if kind.startswith("skill_") else "hypothesis"))
        if claim_type not in {"fact", "procedure", "hypothesis", "improvement"}:
            raise ValueError("claim_type must be fact, procedure, hypothesis or improvement")
        if kind.startswith("skill_") and claim_type == "fact":
            raise ValueError("A skill is a procedure, not a literal fact")
        result = {"claim_type": claim_type}
        for key in ("trigger", "benefit", "verification_plan"):
            value = str(merged.get(key) or "").strip()
            if len(value) > 1000 or _SECRET.search(value):
                raise ValueError(f"Invalid learning {key}")
            result[key] = value
        return result

    def propose(
        self, normalized: dict, session_id: str, *, contract: dict | None = None,
        messages: list[dict] | None = None, origin: str = "model",
        scope: dict | None = None, replaces: str = "", manual: bool = False, request_id: str = "",
    ) -> dict:
        if not manual:
            self._enabled()
        contract = self._contract(contract or {}, normalized["kind"])
        scope = dict(scope or self.scope())
        normalized = {**normalized, "payload": dict(normalized["payload"])}
        if normalized["kind"] == "skill_create":
            normalized["payload"]["content"] = scoped_skill(normalized["payload"]["content"], scope)
        sources = list(dict.fromkeys(source_fingerprint(item) for item in evidence_messages(messages or [])))[-16:]
        # Evidence quotes and reasons may change on a repeated review; the
        # semantic payload, host scope and contract identify this revision.
        key_payload = {key: value for key, value in normalized["payload"].items() if key not in {"evidence", "evidence_role"}}
        key = fingerprint([normalized["kind"], key_payload, scope, contract])
        target = self._target(normalized)
        with self._mutation():
            with self.store._connection() as db:
                row = db.execute(
                    "SELECT c.proposal_id FROM learning_candidates c JOIN learning_proposals p ON p.id=c.proposal_id "
                    "WHERE c.content_key=? AND p.status IN ('pending','applied') LIMIT 1", (key,),
                ).fetchone()
            if row is not None:
                return {**self.show(row["proposal_id"]), "operation": "unchanged"}
            # A generation with identical text but a different contract must
            # get its own proposal, not LearningStore's legacy pending dedup.
            proposal = self.store.stage(session_id, normalized["kind"], normalized["payload"], normalized["reason"], deduplicate=False)
            metadata = {
                "contract": contract, "scope": scope, "origin": origin,
                "sources": sources, "generation_cases": [], "replaces": replaces,
                "generation_inputs": source_inputs(messages or []),
                "generation_requests": [request_id] if request_id else [],
                "target": target,
            }
            if replaces:
                previous = self.show(replaces)
                metadata["generation_cases"] = list(dict.fromkeys([
                    *previous["learning"]["generation_cases"], *(trial["case"] for trial in previous["trials"]),
                ]))[-100:]
                metadata["generation_inputs"] = list(dict.fromkeys([
                    *metadata["generation_inputs"], *previous["learning"].get("generation_inputs", []),
                    *(input_fingerprint([check]) for trial in previous["trials"] for check in trial["checks"]),
                ]))[-400:]
            with self.store._connection() as db:
                db.execute("INSERT INTO learning_candidates VALUES (?, 1, ?, ?)", (proposal["id"], key, json.dumps(metadata, ensure_ascii=False)))
                db.execute("INSERT INTO learning_versions VALUES (?, 1, ?)", (proposal["id"], json.dumps({"proposal": proposal, "metadata": metadata}, ensure_ascii=False)))
            return {**self.show(proposal["id"]), "operation": "created"}

    def show(self, proposal_id: str) -> dict:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise ValueError("Unknown learning candidate")
        revision, metadata = self._metadata(proposal_id)
        # Full baseline is only exposed for an intentional baseline trial.
        public = {key: value for key, value in metadata.items() if key != "target"}
        return {**proposal, "revision": revision, "learning": public, "trials": self.trials(proposal_id, revision)}

    def search(self, query: str = "", limit: int = 8, *, scope: dict | None = None,
               pending_only: bool = False, timeout: float = 0.05) -> list[dict]:
        from .context_index.query import retrieval_terms

        terms = retrieval_terms(query) if query.strip() else ()
        if query.strip() and not terms:
            return []
        scope = scope or self.scope()
        rows = []
        with self.store.read_connection(timeout=timeout) as db:
            candidates = db.execute(
                "SELECT p.id, p.kind, p.status, p.payload_json, p.reason, c.revision, c.metadata_json "
                "FROM learning_candidates c JOIN learning_proposals p ON p.id=c.proposal_id "
                "WHERE p.status IN ('pending','applied') AND (?=0 OR p.status='pending') "
                "AND json_extract(c.metadata_json, '$.scope.workspace')=? "
                "AND json_extract(c.metadata_json, '$.scope.platform')=? "
                "ORDER BY p.updated_at DESC LIMIT 300", (pending_only, scope["workspace"], scope["platform"]),
            ).fetchall()
            stats = db.execute(
                "SELECT proposal_id, revision, status, count(DISTINCT json_extract(trial_json, '$.input_key')) n "
                "FROM learning_trials WHERE status IN ('passed','failed') "
                "AND json_extract(trial_json, '$.variant')='candidate' "
                "AND json_extract(trial_json, '$.phase')='validation' GROUP BY proposal_id, revision, status"
            ).fetchall()
        counts = {(row["proposal_id"], row["revision"], row["status"]): row["n"] for row in stats}
        for row in candidates:
            meta, payload = json.loads(row["metadata_json"]), json.loads(row["payload_json"])
            if meta["scope"] != scope:
                continue
            # Search semantic values, not JSON keys or category="operations".
            # Word boundaries prevent operation matching operations everywhere.
            searchable = "\n".join(str(value) for value in (
                *(payload.get(key, "") for key in ("name", "content", "old_string", "new_string", "file_path")),
                *meta["contract"].values(), row["reason"],
            )).casefold()
            matched = [term for term in terms if (
                bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", searchable))
                if term.isascii() else term in searchable)]
            identifiers = [term for term in matched if term.isascii() and any(c in term for c in "_.")]
            score = len(matched) + 2 * len(identifiers)
            if terms and len(matched) < min(2, len(terms)) and not identifiers:
                continue
            passed = counts.get((row["id"], row["revision"], "passed"), 0)
            failed = counts.get((row["id"], row["revision"], "failed"), 0)
            # Only explicitly selected, actually checked versions earn this
            # small tie-break. Viewing/retrieval and whole-task success do not.
            utility = (passed - failed) / (passed + failed + 5)
            rows.append((score, utility, {"id": row["id"], "revision": row["revision"], "kind": row["kind"],
                                 "status": row["status"], "contract": meta["contract"],
                                 "validation": {"passed": passed, "failed": failed},
                                 "matched_terms": matched,
                                 "preview": str(payload.get("name") or payload.get("content") or "")[:240]}))
        rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item for _, _, item in rows[:max(1, min(limit, 20))]]

    def revise(self, proposal_id: str, normalized: dict, *, contract: dict, session_id: str, messages: list[dict], request_id: str = "", manual: bool = False) -> dict:
        if not manual:
            self._enabled()
        original = self.show(proposal_id)
        merged_contract = self._contract(contract, normalized["kind"], previous=original["learning"]["contract"])
        if original["status"] == "applied":
            return self.propose(normalized, session_id, contract=merged_contract, messages=messages, replaces=proposal_id, request_id=request_id, manual=manual)
        if original["status"] != "pending":
            raise ValueError("Only pending or applied candidates can be revised")
        if normalized["kind"] != original["kind"]:
            raise ValueError("A revision must retain its kind; propose a separate candidate instead")
        from .learning import _now

        with self._mutation():
            revision, metadata = self._metadata(proposal_id)
            if metadata["scope"] != self.scope():
                raise ValueError("A revision must retain its verified workspace/platform; propose a separate local candidate")
            if revision != original["revision"]:
                raise ValueError("Candidate changed; read the latest version before revising")
            if (self.store.get(proposal_id) or {}).get("status") != "pending":
                raise ValueError("Candidate state changed; read its latest state before revising")
            if normalized["kind"] == "skill_create":
                normalized = {**normalized, "payload": {**normalized["payload"], "content": scoped_skill(normalized["payload"]["content"], metadata["scope"])}}
            new_contract = merged_contract
            if normalized["payload"] == original["payload"] and new_contract == metadata["contract"] and self._target(normalized) == metadata["target"]:
                return {**original, "operation": "unchanged"}
            metadata["contract"] = new_contract
            metadata["generation_cases"] = list(dict.fromkeys([
                *metadata["generation_cases"], *(trial["case"] for trial in self.trials(proposal_id)),
            ]))[-100:]
            metadata["generation_inputs"] = list(dict.fromkeys([
                *metadata.get("generation_inputs", []), *source_inputs(messages),
                *(input_fingerprint([check]) for trial in self.trials(proposal_id) for check in trial["checks"]),
            ]))[-400:]
            metadata["generation_requests"] = list(dict.fromkeys([
                *metadata.get("generation_requests", []), *([request_id] if request_id else []),
            ]))[-100:]
            metadata["sources"] = list(dict.fromkeys([
                *metadata["sources"], *(source_fingerprint(item) for item in evidence_messages(messages)),
            ]))[-32:]
            metadata["target"] = self._target(normalized)
            key_payload = {key: value for key, value in normalized["payload"].items() if key not in {"evidence", "evidence_role"}}
            key = fingerprint([normalized["kind"], key_payload, metadata["scope"], metadata["contract"]])
            with self.store._connection() as db:
                db.execute("UPDATE learning_trials SET status='inconclusive' WHERE proposal_id=? AND status='running'", (proposal_id,))
                db.execute("UPDATE learning_proposals SET payload_json=?, reason=?, updated_at=? WHERE id=? AND status='pending'",
                           (json.dumps(normalized["payload"], ensure_ascii=False), normalized["reason"][:1000], _now(), proposal_id))
                db.execute("UPDATE learning_candidates SET revision=?, content_key=?, metadata_json=? WHERE proposal_id=?",
                           (revision + 1, key, json.dumps(metadata, ensure_ascii=False), proposal_id))
                db.execute("INSERT INTO learning_versions VALUES (?, ?, ?)",
                           (proposal_id, revision + 1, json.dumps({"proposal": normalized, "metadata": metadata}, ensure_ascii=False)))
        return {**self.show(proposal_id), "operation": "revised"}

    def trials(self, proposal_id: str, revision: int | None = None) -> list[dict]:
        with self.store._connection() as db:
            rows = db.execute(
                "SELECT * FROM learning_trials WHERE proposal_id=? ORDER BY rowid DESC LIMIT 100", (proposal_id,),
            ).fetchall()
            for row in rows:
                started = json.loads(row["trial_json"]).get("started_at", "")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
                if row["status"] == "running" and started and started < cutoff:
                    db.execute("UPDATE learning_trials SET status='inconclusive' WHERE id=? AND status='running'", (row["id"],))
            rows = db.execute("SELECT * FROM learning_trials WHERE proposal_id=? ORDER BY rowid DESC LIMIT 100", (proposal_id,)).fetchall()
        return [{**json.loads(row["trial_json"]), "id": row["id"], "status": row["status"],
                 "revision": row["revision"], "session_id": row["session_id"], "request_id": row["request_id"]}
                for row in rows if revision is None or row["revision"] == revision]

    def start_trial(self, proposal_id: str, *, session_id: str, request_id: str, case: str,
                    checks: list[dict], phase: str = "validation", variant: str = "candidate", scope: dict | None = None) -> dict:
        self._enabled()
        from .learning import _SECRET

        if not request_id or not session_id:
            raise ValueError("Trials require an active task request")
        if not 3 <= len(case.strip()) <= 300 or _SECRET.search(case):
            raise ValueError("Describe the concrete task input in case (3-300 characters)")
        if phase not in {"training", "validation"} or variant not in {"candidate", "baseline"}:
            raise ValueError("Invalid trial phase or variant")
        checks = normalize_checks(checks)
        if _SECRET.search(json.dumps(checks, ensure_ascii=False)):
            raise ValueError("Do not retain credentials in learning checks")
        with self._mutation():
            proposal = self.show(proposal_id)
            revision, metadata = self._metadata(proposal_id)
            if proposal["status"] not in {"pending", "applied"} or metadata["scope"] != (scope or self.scope()):
                raise ValueError("Trial requires a pending or applied candidate in this workspace and platform")
            if proposal["status"] == "applied" and proposal["kind"].startswith("skill_") and self._target(proposal).get("fingerprint") != (proposal.get("result") or {}).get("learning_target_fingerprint"):
                raise ValueError("Applied skill changed; revise against its current version before trial")
            if not all(metadata["contract"].get(key) for key in ("trigger", "benefit", "verification_plan")):
                raise ValueError("Revise the candidate to specify trigger, benefit and verification_plan first")
            if phase == "validation" and (case in metadata["generation_cases"] or any(
                trial["case"] == case and trial["phase"] == "training" for trial in proposal["trials"]
            )):
                raise ValueError("This case was used for learning; choose a held-out validation input")
            if phase == "validation" and (request_id in metadata.get("generation_requests", []) or any(
                input_fingerprint([check]) in metadata.get("generation_inputs", []) for check in checks
            ) or any(input_fingerprint(trial["checks"]) == input_fingerprint(checks) and trial["phase"] == "training" for trial in proposal["trials"])):
                raise ValueError("This request/input was used for learning; use training or a later held-out task")
            payload = proposal["payload"]
            if variant == "baseline":
                content = metadata["target"].get("content")
            elif proposal["kind"] == "skill_patch":
                current = metadata["target"].get("content") or ""
                old = payload.get("old_string", "")
                if not old or current.count(old) != 1:
                    raise ValueError("Patch does not identify one exact baseline fragment; revise it first")
                content = current.replace(old, payload.get("new_string", ""), 1)
            else:
                content = payload.get("content")
            with self.store._connection() as db:
                count = db.execute("SELECT count(*) FROM learning_trials WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()[0]
                running = db.execute("SELECT 1 FROM learning_trials WHERE session_id=? AND request_id=? AND status='running'", (session_id, request_id)).fetchone()
                if count >= MAX_TRIALS_PER_REQUEST or running:
                    raise ValueError("At most two trials per request, one running at a time")
                trial_id = "lt_" + uuid.uuid4().hex[:12]
                body = {"case": case.strip(), "phase": phase, "variant": variant, "checks": checks,
                        "input_key": input_fingerprint(checks), "receipts": [], "started_at": datetime.now(timezone.utc).isoformat()}
                db.execute("INSERT INTO learning_trials VALUES (?, ?, ?, ?, ?, 'running', ?)",
                           (trial_id, proposal_id, revision, session_id, request_id, json.dumps(body, ensure_ascii=False)))
            self._active_request = (session_id, request_id)
            return {"trial_id": trial_id, "candidate_id": proposal_id, "revision": revision,
                    "status": "running", "scope": metadata["scope"], "contract": metadata["contract"],
                    "variant": variant, "content": content,
                    "notice": "UNVERIFIED TRIAL. Use only for this task within existing permissions/budget. Execute declared checks with normal tools, then finish. Canonical memory/skills are unchanged."}

    def observe(self, session_id: str, request_id: str, name: str, args: dict, result: dict) -> None:
        if not request_id or self._active_request != (session_id, request_id) or self.store.mode() == "off":
            return
        with self.store._lock, self.store._connection() as db:
            row = db.execute("SELECT * FROM learning_trials WHERE session_id=? AND request_id=? AND status='running'",
                             (session_id, request_id)).fetchone()
            if row is None:
                return
            trial = json.loads(row["trial_json"])
            for index, check in enumerate(trial["checks"]):
                if name == check["tool"] and args == check["args"] and not any(r["check"] == index for r in trial["receipts"]):
                    trial["receipts"].append({"check": index, **check_result(check, result)})
            db.execute("UPDATE learning_trials SET trial_json=? WHERE id=? AND status='running'", (json.dumps(trial, ensure_ascii=False), row["id"]))

    def finish_trial(self, trial_id: str, *, session_id: str, request_id: str) -> dict:
        self._enabled()
        with self._mutation(), self.store._connection() as db:
            row = db.execute("SELECT * FROM learning_trials WHERE id=?", (trial_id,)).fetchone()
            if row is None or row["session_id"] != session_id or row["request_id"] != request_id or not request_id:
                raise ValueError("Only the originating active request can finish a trial")
            trial = json.loads(row["trial_json"])
            status = trial_status(trial["receipts"], trial["checks"]) if row["status"] == "running" else row["status"]
            db.execute("UPDATE learning_trials SET status=? WHERE id=?", (status, trial_id))
            self._active_request = None
            return {"trial_id": trial_id, "status": status, "receipts": trial["receipts"],
                    "notice": "Checks establish declared postconditions only; they do not prove general usefulness or causality."}

    def end_request(self, session_id: str, request_id: str) -> None:
        if self._active_request == (session_id, request_id):
            self._active_request = None
        with self.store._lock, self.store._connection() as db:
            db.execute("UPDATE learning_trials SET status='inconclusive' WHERE session_id=? AND request_id=? AND status='running'", (session_id, request_id))

    def activation_issue(self, proposal_id: str, *, scope: dict | None = None) -> str:
        proposal = self.show(proposal_id)
        _, metadata = self._metadata(proposal_id)
        if proposal["status"] != "pending":
            return "Only pending candidates can be activated"
        if metadata["scope"] != (scope or self.scope()):
            return "Candidate has not been verified in this workspace/platform"
        if proposal["kind"] == "memory":
            return "Core Markdown remains an explicit user decision; use the memory tool or /learn approve"
        if self._target(proposal) != metadata["target"]:
            return "Target changed since proposal; revise and validate against the current version"
        if self.store.requires_approval(proposal, self.skills):
            return "Collision, deletion or major reduction requires explicit /learn approve"
        target = metadata["target"]
        if target.get("content") is not None or target.get("manifest_exists"):
            owned_file = target["file_path"] if target.get("content") is not None else "SKILL.md"
            owned_fingerprint = target["fingerprint"] if target.get("content") is not None else target["manifest_fingerprint"]
            owned = any(
                item["kind"].startswith("skill_") and item["payload"].get("name") == target["name"]
                and item["payload"].get("file_path", "SKILL.md") == owned_file
                and isinstance(item.get("result"), dict)
                and item["result"].get("learning_target_fingerprint") == owned_fingerprint
                for item in self.store.list("applied", limit=10_000)
            )
            if not owned or target.get("pinned"):
                return "Existing manual, pinned or externally changed skill requires explicit /learn approve"
        if target.get("file_path") == "SKILL.md":
            payload = proposal["payload"]
            content = str(payload.get("content") or "")
            if proposal["kind"] == "skill_patch":
                old = str(payload.get("old_string") or "")
                if not old or str(target.get("content") or "").count(old) != 1:
                    return "Patch must identify one exact baseline fragment"
                content = str(target["content"]).replace(old, str(payload.get("new_string") or ""), 1)
            if self.skills._frontmatter(content).get("astra_learning_scope") != scope_key(metadata["scope"]):
                return "Automatic skill changes must retain the verified learning scope marker"
        trials = proposal["trials"]
        if any(t["status"] == "running" for t in trials):
            return "Finish or end the running trial before activation"
        if any(t["variant"] == "candidate" and t["status"] == "failed" for t in trials):
            return "An unresolved counterexample exists; revise before another activation attempt"
        valid = [t for t in trials if t["variant"] == "candidate" and t["phase"] == "validation" and t["status"] == "passed" and t["case"] not in metadata["generation_cases"]]
        if not valid:
            return "A successful held-out validation trial is required"
        claim_type = metadata["contract"]["claim_type"]
        if proposal["kind"] == "observation":
            if claim_type != "fact":
                return "Inferred explanations remain hypotheses; revise to a literal verified fact or a testable procedure"
            content = " ".join(str(proposal["payload"].get("content", "")).split())
            literals = [" ".join(str(r.get("supports_literal") or "").split()) for trial in valid for r in trial["receipts"]]
            if content not in literals:
                return "Observation must equal an actually verified literal; a request or nearby quote does not support a broader claim"
        if claim_type in {"hypothesis", "improvement"}:
            pairs = [t for t in valid if any(
                b["variant"] == "baseline" and b["phase"] == "validation" and b["status"] == "failed"
                and b["case"] == t["case"] and b["request_id"] != t["request_id"]
                and all(r["status"] != "inconclusive" for r in b["receipts"])
                and len(b["receipts"]) == len(b["checks"])
                and [{k: v for k, v in c.items() if k != "args"} for c in b["checks"]]
                == [{k: v for k, v in c.items() if k != "args"} for c in t["checks"]]
                for b in trials
            )]
            if len({t["case"] for t in pairs}) < 2 or len({fingerprint(t["checks"]) for t in pairs}) < 2:
                return "Improvement requires paired old/new validation on two distinct held-out inputs with identical outcome assertions"
        return ""

    def activate(self, proposal_id: str, *, scope: dict | None = None) -> dict:
        self._enabled()
        with self._mutation():
            issue = self.activation_issue(proposal_id, scope=scope)
            if issue:
                raise ValueError(issue)
            revision, metadata = self._metadata(proposal_id)
            trials = self.trials(proposal_id, revision)
            proof = {"candidate_id": proposal_id, "revision": revision, "scope": metadata["scope"],
                     "claim_type": metadata["contract"]["claim_type"],
                     "trial_ids": [t["id"] for t in trials if t["status"] == "passed"],
                     "evidence_sources": metadata["sources"], "assessment": "runtime_checked_postconditions"}
            applied = self.store.approve(proposal_id, self.memory, self.skills, verification=proof)
            result = {**(applied.get("result") or {}), "learning_verification": proof,
                      "learning_target_fingerprint": self._target(applied).get("fingerprint")}
            self.store._set_status(proposal_id, "applied", result=result)
            return self.show(proposal_id)

    def discard(self, proposal_id: str) -> dict:
        self._enabled()
        with self._mutation():
            self._metadata(proposal_id)
            self.store.reject(proposal_id)
            with self.store._connection() as db:
                db.execute("UPDATE learning_trials SET status='inconclusive' WHERE proposal_id=? AND status='running'", (proposal_id,))
        return self.show(proposal_id)

    def rollback(self, proposal_id: str) -> dict:
        self._enabled()
        with self._mutation():
            proposal = self.show(proposal_id)
            proof = (proposal.get("result") or {}).get("learning_verification")
            if not proof:
                raise ValueError("Only automatically verified changes can be rolled back by the model; use /learn rollback for manual changes")
            if proposal["kind"].startswith("skill_") and self._target(proposal).get("fingerprint") != proposal["result"].get("learning_target_fingerprint"):
                raise ValueError("Skill changed after activation; rollback would overwrite another edit")
            self.store.rollback(proposal_id, self.memory, self.skills)
        return self.show(proposal_id)

    def new_review_sources(self, session_id: str, messages: list[dict]) -> list[dict]:
        with self.store._connection() as db:
            row = db.execute("SELECT fingerprints_json FROM learning_review_sources WHERE session_id=?", (session_id,)).fetchone()
        seen = set(json.loads(row[0])) if row else set()
        return [item for item in evidence_messages(messages) if source_fingerprint(item) not in seen]

    def mark_reviewed(self, session_id: str, messages: list[dict]) -> None:
        with self.store._lock, self.store._connection() as db:
            row = db.execute("SELECT fingerprints_json FROM learning_review_sources WHERE session_id=?", (session_id,)).fetchone()
            prior = json.loads(row[0]) if row else []
            hashes = list(dict.fromkeys([*prior, *(source_fingerprint(item) for item in evidence_messages(messages))]))[-2048:]
            db.execute("INSERT INTO learning_review_sources VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET fingerprints_json=excluded.fingerprints_json", (session_id, json.dumps(hashes)))
