from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from code_reviewer.resume import ResumeTracker


class ResumeTrackerTests(unittest.TestCase):
    def test_done_entry_with_missing_report_is_stale_and_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tracker = ResumeTracker("jira-issues", Path(temp), {"jira_keys": ["ECHNL-5745"]})
            tracker.mark_done("issue", {"jira_key": "ECHNL-5745", "report": "missing.md"})

            self.assertFalse(tracker.is_done("issue"))
            self.assertTrue(tracker.is_stale_done("issue"))

    def test_done_entry_with_existing_reports_can_be_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            first = output_dir / "first.md"
            second = output_dir / "second.md"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            tracker = ResumeTracker("jira-issues", output_dir, {"jira_keys": ["ECHNL-5745"]})
            tracker.mark_done(
                "issue",
                {
                    "jira_key": "ECHNL-5745",
                    "report": str(first),
                    "reports": [{"path": str(first)}, {"path": str(second)}],
                },
            )

            self.assertTrue(tracker.is_done("issue"))
            self.assertFalse(tracker.is_stale_done("issue"))

    def test_successful_retry_clears_stale_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tracker = ResumeTracker("jira-issues", Path(temp), {"jira_keys": ["ECHNL-5651"]})

            tracker.mark_failed("issue", "temporary provider failure", {"jira_key": "ECHNL-5651"})
            tracker.mark_started("issue", {"jira_key": "ECHNL-5651"})
            tracker.mark_done("issue", {"jira_key": "ECHNL-5651", "report": "report.md"})

            entry = tracker.entry("issue")
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["summary"]["report"], "report.md")
            self.assertNotIn("error", entry)
            self.assertTrue(entry["completed_at"])

    def test_retry_start_clears_stale_completion_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tracker = ResumeTracker("jira-issues", Path(temp), {"jira_keys": ["ECHNL-5658"]})

            tracker.mark_done("issue", {"jira_key": "ECHNL-5658", "report": "old.md"})
            tracker.mark_started("issue", {"jira_key": "ECHNL-5658"})

            entry = tracker.entry("issue")
            self.assertEqual(entry["status"], "in-progress")
            self.assertNotIn("completed_at", entry)
            self.assertNotIn("error", entry)

    def test_load_normalizes_legacy_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            tracker = ResumeTracker("jira-issues", output_dir, {"jira_keys": ["ECHNL-5651"]})
            tracker.path.write_text(
                json.dumps(
                    {
                        "items": {
                            "done": {"status": "done", "error": "old failure", "completed_at": "now"},
                            "retry": {"status": "failed", "error": "current failure", "completed_at": "old"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            reloaded = ResumeTracker("jira-issues", output_dir, {"jira_keys": ["ECHNL-5651"]})

            self.assertNotIn("error", reloaded.entry("done"))
            self.assertEqual(reloaded.entry("done")["completed_at"], "now")
            self.assertEqual(reloaded.entry("retry")["error"], "current failure")
            self.assertNotIn("completed_at", reloaded.entry("retry"))


if __name__ == "__main__":
    unittest.main()
