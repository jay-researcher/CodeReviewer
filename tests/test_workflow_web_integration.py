from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.web_app import (
    _snapshot_completed_handling,
    _sync_workflow_history,
    _workflow_cycle_from_history_entry,
)
from code_reviewer.workflow_store import WorkflowStore


REPORT = """# ECHNL-9001 Code Review Report

## Findings

### 1. [High] Unsafe authorization fallback

- Problem: fallback accepts an unverified identity
"""


class WorkflowWebIntegrationTests(unittest.TestCase):
    def test_current_delivery_report_never_falls_back_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = WorkflowStore(Path(temp) / "workflow.sqlite3")
            expected = store.upsert_review_cycle(
                jira_key="ECHNL-9100",
                sprint_id="10100",
                sprint_name="e-Channel Sprint 1.4.78",
                sprint_state="active",
            )
            cycle_id, sprint_id = _workflow_cycle_from_history_entry(
                store,
                {"reviewed_at": "2026-07-24T10:00:00"},
                {"workflow_cycle_required": True},
                "ECHNL-9100",
                "Current delivery",
                "wen.yi",
            )
            self.assertEqual(expected["cycle_id"], cycle_id)
            self.assertEqual("10100", sprint_id)
            self.assertNotEqual("legacy", sprint_id)

    def test_ambiguous_current_delivery_report_is_rejected_not_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = WorkflowStore(Path(temp) / "workflow.sqlite3")
            with self.assertRaisesRegex(ValueError, "refusing to register it as Legacy"):
                _workflow_cycle_from_history_entry(
                    store,
                    {"reviewed_at": "2026-07-24T10:00:00"},
                    {"workflow_cycle_required": True},
                    "ECHNL-9101",
                    "Current delivery",
                    "wen.yi",
                )

    def test_history_sync_persists_cycle_group_description_and_deferred_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "wen.yi" / "ECHNL-9001_has-issue-high.md"
            report.parent.mkdir(parents=True)
            report.write_text(REPORT, encoding="utf-8")
            store = WorkflowStore(root / "workflow.sqlite3")
            entry = {
                "reviewed_at": "2026-07-17T09:30:00",
                "report_path": str(report),
                "jira_key": "ECHNL-9001",
                "sprint": "e-Channel Sprint 1.4.75",
                "conclusion": "Blocking",
                "metadata": {
                    "jira_summary": "Cross-Sprint follow-up",
                    "jira_status": "Development Done",
                    "jira_description": "Original description\n\nCurrent formal follow-up comment",
                    "responsible": "wen.yi",
                    "run_group_id": "rg-integration-1",
                    "project_type": "frontend",
                    "review_fingerprint": "revision-fingerprint",
                    "review_stable_fingerprint": "stable-fingerprint",
                    "jira_current_sprint_id": "10085",
                    "jira_current_sprint_state": "active",
                    "jira_sprint_memberships": [
                        {"id": "10001", "name": "Old Sprint", "state": "complete"},
                        {"id": "10085", "name": "e-Channel Sprint 1.4.75", "state": "active"},
                    ],
                    "current_review_scope": {
                        "sprint_id": "10085",
                        "sprint": "e-Channel Sprint 1.4.75",
                        "sprint_state": "active",
                    },
                    "related_merge_requests": [
                        {"project_path": "web/app", "mr_id": "12", "head_sha": "a" * 40}
                    ],
                    "deferred_release_gate_resources": [
                        {
                            "release_gate_role": "company_config",
                            "project_path": "release/company-config",
                            "mr_id": "7",
                            "head_sha": "b" * 40,
                            "mr_url": "https://gitlab.example/release/company-config/-/merge_requests/7",
                        }
                    ],
                },
            }
            with patch("code_reviewer.web_app.workflow_store", return_value=store), patch(
                "code_reviewer.web_app.load_review_history", return_value=[entry]
            ), patch("code_reviewer.web_app.WEB_THREADS_DIR", root / "threads"):
                _sync_workflow_history()
                _sync_workflow_history()

            detail = store.issue_detail("ECHNL-9001")
            self.assertIsNotNone(detail)
            self.assertEqual([item["sprint_id"] for item in detail["cycles"]], ["10085"])
            self.assertEqual(len(detail["sprint_memberships"]), 2)
            self.assertEqual(len(detail["run_groups"]), 1)
            self.assertEqual(detail["runs"][0]["project_type"], "frontend")
            self.assertEqual(len(detail["description_snapshots"]), 1)
            self.assertEqual(len(detail["deferred_resources"]), 1)
            self.assertEqual(detail["deferred_resources"][0]["head_sha"], "b" * 40)

    def test_snapshot_is_created_only_after_every_run_group_finding_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = WorkflowStore(Path(temp) / "workflow.sqlite3")
            cycle = store.upsert_review_cycle(jira_key="ECHNL-9002", sprint_id="10085")
            group = store.create_run_group(cycle_id=cycle["cycle_id"], run_group_id="rg-two-projects")
            first_report = Path(temp) / "frontend.md"
            second_report = Path(temp) / "backend.md"
            first_id = store.register_run(
                jira_key="ECHNL-9002",
                report_path=str(first_report),
                findings=[{"index": "1", "severity": "Medium", "title": "Frontend finding"}],
                cycle_id=cycle["cycle_id"],
                run_group_id=group["id"],
                project_type="frontend",
            )
            second_id = store.register_run(
                jira_key="ECHNL-9002",
                report_path=str(second_report),
                findings=[{"index": "1", "severity": "Low", "title": "Backend finding"}],
                cycle_id=cycle["cycle_id"],
                run_group_id=group["id"],
                project_type="backend",
            )
            detail = store.issue_detail("ECHNL-9002")
            finding_by_run = {run["id"]: run["findings"][0]["id"] for run in detail["runs"]}
            store.record_handling(
                finding_id=finding_by_run[first_id], disposition="fixed", note="Fixed frontend",
                actor="dev", actor_role="developer",
            )
            with patch("code_reviewer.web_app.workflow_store", return_value=store):
                self.assertIsNone(_snapshot_completed_handling("ECHNL-9002", "dev"))
                store.record_handling(
                    finding_id=finding_by_run[second_id], disposition="not-issue", note="Verified false positive",
                    actor="auditor", actor_role="auditor",
                )
                snapshot = _snapshot_completed_handling("ECHNL-9002", "auditor")
                repeated = _snapshot_completed_handling("ECHNL-9002", "auditor")

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["id"], repeated["id"])
            self.assertEqual(len(store.list_review_snapshots(cycle["cycle_id"])), 1)


if __name__ == "__main__":
    unittest.main()
