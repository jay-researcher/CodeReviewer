from __future__ import annotations

import tempfile
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from code_reviewer.adf import text_adf
from code_reviewer.workflow_store import WorkflowStore


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temp.name) / "workflow.db")
        self.report1 = Path(self.temp.name) / "wen.yi" / "ECHNL-1001_has-issue-high.md"
        self.report1.parent.mkdir()
        self.report1.write_text("run one", encoding="utf-8")
        self.finding = {"index": "1", "severity": "High", "title": "Unsafe call", "file": "src/a.py"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _register(self, report: Path, findings: list[dict[str, str]]) -> str:
        with patch("code_reviewer.workflow_store.app_config_get", return_value=["Critical", "High"]):
            return self.store.register_run(
                jira_key="ECHNL-1001", report_path=str(report), findings=findings,
                summary="Review workflow", responsible="wen.yi",
            )

    def test_fixed_blocker_requires_clean_rescan(self) -> None:
        run_id = self._register(self.report1, [self.finding])
        detail = self.store.issue_detail("ECHNL-1001")
        finding_id = detail["runs"][0]["findings"][0]["id"]
        self.store.record_handling(
            finding_id=finding_id, disposition="fixed", note="Patched", actor="gerhard.guo", actor_role="developer"
        )
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])

        report2 = self.report1.with_name("ECHNL-1001_rescan-clean.md")
        report2.write_text("run two", encoding="utf-8")
        self._register(report2, [])
        self.assertTrue(self.store.pass_readiness("ECHNL-1001")["ready"])

    def test_developer_not_issue_requires_approval(self) -> None:
        self._register(self.report1, [self.finding])
        finding_id = self.store.issue_detail("ECHNL-1001")["runs"][0]["findings"][0]["id"]
        result = self.store.record_handling(
            finding_id=finding_id, disposition="not-issue", note="False positive", actor="gerhard.guo", actor_role="developer"
        )
        self.assertEqual(result["approval_status"], "pending")
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])
        self.store.approve_handling(result["handling_id"], "wen.yi", "auditor", approved=True, reason="Verified")
        self.assertTrue(self.store.pass_readiness("ECHNL-1001")["ready"])

    def test_manager_can_override_followup_with_adf_draft(self) -> None:
        self._register(self.report1, [self.finding])
        finding_id = self.store.issue_detail("ECHNL-1001")["runs"][0]["findings"][0]["id"]
        result = self.store.record_handling(
            finding_id=finding_id, disposition="follow-up", note="Accepted risk", actor="gerhard.guo", actor_role="developer",
            jira_summary="Track safer API", jira_description_adf=text_adf("Replace the unsafe call in the next release."),
        )
        self.assertTrue(result["draft_id"])
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])
        self.store.manager_override(result["handling_id"], "admin", "Risk accepted for this release")
        readiness = self.store.pass_readiness("ECHNL-1001")
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["manager_exceptions"], 1)

    def test_cycle_membership_run_group_and_project_runs(self) -> None:
        membership = self.store.upsert_sprint_membership(
            jira_key="ECHNL-1001", sprint_id="10085", sprint_name="Sprint 1.4.75",
            sprint_state="active", source={"origin": "jira"}, responsible="wen.yi",
        )
        cycle = self.store.upsert_review_cycle(
            jira_key="ECHNL-1001", sprint_id="10085", sprint_name="Sprint 1.4.75",
            sprint_state="active", review_mode="issue", mr_scope=[{"iid": 12, "head_sha": "a" * 40}],
        )
        group = self.store.create_run_group(cycle_id=cycle["cycle_id"], stable_fingerprint="scope-1")
        frontend = self.store.register_run(
            jira_key="ECHNL-1001", report_path=str(self.report1), findings=[self.finding],
            cycle_id=cycle["cycle_id"], run_group_id=group["id"], project_type="frontend",
            mr_fingerprint="frontend-mrs", stable_fingerprint="stable-frontend",
        )
        report2 = self.report1.with_name("ECHNL-1001_backend.md")
        report2.write_text("backend", encoding="utf-8")
        backend = self.store.register_run(
            jira_key="ECHNL-1001", report_path=str(report2), findings=[], cycle_id=cycle["cycle_id"],
            run_group_id=group["id"], project_type="backend", mr_fingerprint="backend-mrs",
        )
        self.assertEqual(membership["source"], {"origin": "jira"})
        self.assertEqual(self.store.list_cycles("ECHNL-1001")[0]["mr_scope"][0]["iid"], 12)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id, run_group_id, project_type FROM review_runs WHERE id IN (?, ?) ORDER BY project_type",
                (frontend, backend),
            ).fetchall()
        self.assertEqual({row["project_type"] for row in rows}, {"frontend", "backend"})
        self.assertEqual({row["run_group_id"] for row in rows}, {group["id"]})

    def test_description_and_review_snapshots_are_versioned_immutable_and_idempotent(self) -> None:
        cycle = self.store.upsert_review_cycle(jira_key="ECHNL-1001", sprint_id="10085")
        first = self.store.create_description_snapshot(
            cycle_id=cycle["cycle_id"], source_type="description", source_id="ECHNL-1001",
            reason="cycle-start", adf_document=text_adf("Original"), idempotency_key="desc-1",
        )
        repeated = self.store.create_description_snapshot(
            cycle_id=cycle["cycle_id"], source_type="description", source_id="ECHNL-1001",
            reason="cycle-start", adf_document=text_adf("Ignored duplicate"), idempotency_key="desc-1",
        )
        second = self.store.create_description_snapshot(
            cycle_id=cycle["cycle_id"], source_type="description", source_id="ECHNL-1001",
            reason="jira-edited", adf_document=text_adf("Edited"), idempotency_key="desc-2",
        )
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(self.store.list_description_snapshots(cycle["cycle_id"])[0]["plain_text"], "Original")

        review1 = self.store.create_review_snapshot(
            cycle_id=cycle["cycle_id"], reason="handling-complete", actor="wen.yi",
            payload={"state": "handled"}, idempotency_key="snapshot-1",
        )
        review1_again = self.store.create_review_snapshot(
            cycle_id=cycle["cycle_id"], reason="handling-complete", actor="wen.yi",
            payload={"state": "different"}, idempotency_key="snapshot-1",
        )
        review2 = self.store.create_review_snapshot(
            cycle_id=cycle["cycle_id"], reason="pass", actor="wen.yi",
            payload={"state": "passed"}, idempotency_key="snapshot-2",
        )
        self.assertEqual(review1["id"], review1_again["id"])
        self.assertEqual((review1["revision"], review2["revision"]), (1, 2))
        self.assertEqual(self.store.list_review_snapshots(cycle["cycle_id"])[0]["payload"], {"state": "handled"})

    def test_deferred_resources_are_revision_scoped_and_idempotent(self) -> None:
        cycle = self.store.upsert_review_cycle(jira_key="ECHNL-1001", sprint_id="10085")
        common = {
            "cycle_id": cycle["cycle_id"], "gitlab_project": "build/dps", "mr_iid": 397,
            "resource_type": "company_config",
        }
        first = self.store.upsert_deferred_resource(
            **common, head_sha="a" * 40, idempotency_key="deferred-a",
        )
        repeated = self.store.upsert_deferred_resource(
            **common, head_sha="a" * 40, status="verified", idempotency_key="deferred-a",
        )
        verified = self.store.upsert_deferred_resource(
            **common, head_sha="a" * 40, status="verified", gate_run_id="gate-1",
            locked_build_commit="c" * 40, evidence={"ancestor": True}, verified_by="admin",
            idempotency_key="verify-a",
        )
        newer = self.store.upsert_deferred_resource(
            **common, head_sha="b" * 40, idempotency_key="deferred-b",
        )
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(verified["status"], "verified")
        self.assertNotEqual(first["id"], newer["id"])
        self.assertEqual(len(self.store.list_deferred_resources(cycle_id=cycle["cycle_id"])), 2)
        self.assertEqual(self.store.list_deferred_resources(cycle_id=cycle["cycle_id"], pending_only=True)[0]["head_sha"], "b" * 40)

    def test_concurrent_duplicate_writes_use_one_server_record(self) -> None:
        self._register(self.report1, [self.finding])
        finding_id = self.store.issue_detail("ECHNL-1001")["runs"][0]["findings"][0]["id"]

        def handling() -> dict[str, str]:
            return self.store.record_handling(
                finding_id=finding_id, disposition="not-issue", note="False positive",
                actor="wen.yi", actor_role="auditor", idempotency_key="handling-submit-1",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            handling_results = list(pool.map(lambda _: handling(), range(12)))
        self.assertEqual(len({item["handling_id"] for item in handling_results}), 1)

        def discuss() -> str:
            return self.store.add_discussion(
                "ECHNL-1001", "wen.yi", "Reviewed", idempotency_key="discussion-submit-1"
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            discussion_ids = list(pool.map(lambda _: discuss(), range(12)))
        self.assertEqual(len(set(discussion_ids)), 1)

        def passing() -> dict[str, object]:
            return self.store.manual_pass(
                "ECHNL-1001", "wen.yi", "auditor", "Accepted",
                idempotency_key="manual-pass-submit-1",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            passes = list(pool.map(lambda _: passing(), range(12)))
        self.assertEqual(len({item["pass_id"] for item in passes}), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finding_handlings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discussions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pass_records").fetchone()[0], 1)

    def test_v1_database_is_backfilled_once(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        now = "2026-07-01T00:00:00+08:00"
        db = sqlite3.connect(legacy_path)
        try:
            db.executescript(
                """
                CREATE TABLE review_issues (
                    jira_key TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', responsible TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'not-reviewed', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    latest_run_id TEXT, passed_run_id TEXT
                );
                CREATE TABLE review_runs (
                    id TEXT PRIMARY KEY, jira_key TEXT NOT NULL, report_path TEXT NOT NULL,
                    report_fingerprint TEXT NOT NULL, run_number INTEGER NOT NULL, conclusion TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed', severity_counts_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, UNIQUE(jira_key, report_fingerprint)
                );
                """
            )
            db.execute(
                "INSERT INTO review_issues VALUES('ECHNL-9', 'Legacy', 'wen.yi', 'handling', ?, ?, 'run-1', NULL)",
                (now, now),
            )
            db.execute(
                "INSERT INTO review_runs VALUES('run-1', 'ECHNL-9', 'legacy.md', 'fingerprint', 1, '', 'completed', '{}', ?)",
                (now,),
            )
            db.commit()
        finally:
            db.close()
        WorkflowStore(legacy_path)
        WorkflowStore(legacy_path)
        db = sqlite3.connect(legacy_path)
        try:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], "3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_cycles").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_run_groups").fetchone()[0], 1)
            run = db.execute("SELECT cycle_id, run_group_id FROM review_runs WHERE id='run-1'").fetchone()
            self.assertTrue(run["cycle_id"])
            self.assertTrue(run["run_group_id"])
        finally:
            db.close()

    def test_failed_migration_rolls_back_schema_changes(self) -> None:
        legacy_path = Path(self.temp.name) / "rollback.db"
        db = sqlite3.connect(legacy_path)
        try:
            db.executescript(
                """
                CREATE TABLE review_issues (
                    jira_key TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', responsible TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'not-reviewed', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    latest_run_id TEXT, passed_run_id TEXT
                );
                CREATE TABLE review_runs (
                    id TEXT PRIMARY KEY, jira_key TEXT NOT NULL, report_path TEXT NOT NULL,
                    report_fingerprint TEXT NOT NULL, run_number INTEGER NOT NULL, conclusion TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed', severity_counts_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, UNIQUE(jira_key, report_fingerprint)
                );
                """
            )
            db.commit()
        finally:
            db.close()
        with patch.object(WorkflowStore, "_backfill_cycles", side_effect=RuntimeError("migration failed")):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                WorkflowStore(legacy_path)
        db = sqlite3.connect(legacy_path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(review_runs)")}
        finally:
            db.close()
        self.assertNotIn("review_cycles", tables)
        self.assertNotIn("cycle_id", columns)


if __name__ == "__main__":
    unittest.main()
