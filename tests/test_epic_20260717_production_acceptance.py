from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from code_reviewer.review_service import run_review_from_payload
from code_reviewer.web_app import recent_workflow_sprints, render_index
from code_reviewer.workflow_store import WorkflowStore


class Epic20260717ProductionAcceptanceTests(unittest.TestCase):
    def test_new_sprint_cycle_closes_previous_cycle_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "workflow.sqlite3")
            first = store.upsert_review_cycle(jira_key="ECHNL-9001", sprint_id="100", sprint_name="Sprint A")
            second = store.upsert_review_cycle(jira_key="ECHNL-9001", sprint_id="101", sprint_name="Sprint B")
            cycles = store.list_cycles("ECHNL-9001")
            by_id = {item["cycle_id"]: item for item in cycles}
            self.assertTrue(by_id[first["cycle_id"]]["cycle_closed_at"])
            self.assertFalse(by_id[second["cycle_id"]]["cycle_closed_at"])
            self.assertEqual(len(cycles), 2)

    def test_recent_sprint_suggestions_exclude_older_than_one_month(self) -> None:
        now = datetime.now(timezone.utc)

        class FakeStore:
            def list_issues(self, **_kwargs):
                return [{"jira_key": "ECHNL-1"}, {"jira_key": "ECHNL-2"}]

            def issue_detail(self, jira_key):
                recent = jira_key == "ECHNL-1"
                return {"cycles": [{
                    "sprint_id": "101" if recent else "99",
                    "sprint_name": "Recent Sprint" if recent else "Old Sprint",
                    "updated_at": (now - timedelta(days=2 if recent else 60)).isoformat(),
                }]}

        with patch("code_reviewer.web_app.workflow_store", return_value=FakeStore()):
            rows = recent_workflow_sprints()
        self.assertEqual([row["id"] for row in rows], ["101"])

    def test_server_enforces_batch_confirmation_and_persists_final_mode(self) -> None:
        batch = {
            "valid": True, "accessible": True, "empty": False,
            "review_mode": "batch-preview", "requires_confirmation": True,
        }
        with patch("code_reviewer.review_service.sprint_review_preflight", return_value=batch):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                run_review_from_payload({"mode": "sprint", "sprint": "101", "review_mode": "batch-preview"})

        final = {"valid": True, "accessible": True, "empty": False, "review_mode": "final-sprint"}
        summary = {"items": [], "errors": []}
        with patch("code_reviewer.review_service.sprint_review_preflight", return_value=final), patch(
            "code_reviewer.review_service.review_sprint_merge_requests", return_value=summary
        ) as review:
            result = run_review_from_payload({"mode": "sprint", "sprint": "102", "review_mode": "final-sprint"})
        self.assertEqual(result["review_mode"], "final-sprint")
        self.assertEqual(review.call_args.kwargs["workflow_review_mode"], "final-sprint")

    def test_issue_history_overview_and_unique_maximize_are_rendered(self) -> None:
        page = render_index("admin")
        for token in (
            'data-issue-review-view="overview"',
            'id="issueReviewOverviewPanel"',
            "function renderIssueReviewOverview()",
            "maximizedJobs.clear();",
            "singleFlight('run-review'",
            "singleFlight('save-draft'",
        ):
            self.assertIn(token, page)


if __name__ == "__main__":
    unittest.main()
