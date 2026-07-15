from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.web_app import (
    _aggregate_coverage_report_status,
    build_review_coverage,
    _manual_pass_readiness,
    _record_finding_handling,
    _web_user_responsibles,
    _web_user_permissions,
    _web_user_role,
)


REPORT = """# ECHNL-1001 Code Review Report

## Findings

### 1. [High] Blocking behavior

- Problem: broken

### 2. [Medium] Improvement

- Problem: improve
"""


class WebRolesWorkflowTests(unittest.TestCase):
    def test_default_roles_and_permissions(self) -> None:
        with patch("code_reviewer.web_app._load_web_users", return_value={}), patch(
            "code_reviewer.web_app._configured_web_user_profiles",
            return_value={},
        ):
            self.assertEqual(_web_user_role("admin"), "manager")
            self.assertEqual(_web_user_role("wen.yi"), "auditor")
            self.assertTrue(_web_user_permissions("admin")["run_sprint_review"])
            self.assertFalse(_web_user_permissions("wen.yi")["run_sprint_review"])

    def test_configured_developer_can_handle_but_not_run_review(self) -> None:
        profiles = {"dev.user": {"role": "developer", "responsible": ["wen.yi"]}}
        with patch("code_reviewer.web_app._load_web_users", return_value={}), patch(
            "code_reviewer.web_app._configured_web_user_profiles",
            return_value=profiles,
        ):
            permissions = _web_user_permissions("dev.user")
            self.assertTrue(permissions["submit_handling"])
            self.assertFalse(permissions["run_issue_review"])
            self.assertFalse(permissions["manual_pass"])

    def test_trial_developer_responsible_mapping(self) -> None:
        profiles = {
            "gerhard.guo": {"role": "developer", "responsible": ["wen.yi"]},
            "bryan.tan": {"role": "developer", "responsible": ["wen.yi"]},
            "vincentgr.wang": {"role": "developer", "responsible": ["kevin.tan"]},
            "kelvinh.wu": {"role": "developer", "responsible": ["kevin.tan"]},
        }
        with patch("code_reviewer.web_app._load_web_users", return_value={}), patch(
            "code_reviewer.web_app._configured_web_user_profiles", return_value=profiles,
        ):
            self.assertEqual(_web_user_responsibles("gerhard.guo"), ["wen.yi"])
            self.assertEqual(_web_user_responsibles("bryan.tan"), ["wen.yi"])
            self.assertEqual(_web_user_responsibles("vincentgr.wang"), ["kevin.tan"])
            self.assertEqual(_web_user_responsibles("kelvinh.wu"), ["kevin.tan"])

    def test_high_finding_requires_clean_rescan_or_leader_not_issue_before_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            report = base / "wen.yi" / "ECHNL-1001_has-issue-high.md"
            report.parent.mkdir()
            report.write_text(REPORT, encoding="utf-8")
            with patch("code_reviewer.web_app.WEB_THREADS_DIR", base / "threads"):
                thread = _record_finding_handling(base, report, "dev.user", "1", "follow-up", "Track later")
                self.assertFalse(_manual_pass_readiness(report, thread)["ready"])

                thread = _record_finding_handling(base, report, "dev.user", "1", "fixed", "Fixed and verified")
                readiness = _manual_pass_readiness(report, thread)
                self.assertFalse(readiness["ready"])

                with patch("code_reviewer.web_app._web_user_role", return_value="auditor"):
                    thread = _record_finding_handling(base, report, "wen.yi", "1", "not-issue", "Verified false positive")
                    readiness = _manual_pass_readiness(report, thread)
                self.assertTrue(readiness["ready"])
                self.assertEqual(readiness["blocking_pending"], 0)

    def test_coverage_requires_all_reports_to_pass(self) -> None:
        self.assertEqual(_aggregate_coverage_report_status([{"status": "passed"}, {"status": "pending"}]), "pending")
        self.assertEqual(_aggregate_coverage_report_status([{"status": "passed"}, {"status": "passed"}]), "passed")

    def test_direct_jira_coverage_populates_summary_and_responsible(self) -> None:
        discovered = {
            "jira_key": "ECHNL-1001",
            "jira_summary": "LDAP login support",
            "jira_status": "Development Done",
            "items": [
                {
                    "jira_key": "ECHNL-1001",
                    "jira_summary": "LDAP login support",
                    "jira_status": "Development Done",
                    "responsible": "kevin.tan",
                }
            ],
            "issues_without_mrs": [],
            "errors": [],
        }
        with (
            patch("code_reviewer.web_app._web_user_permissions", return_value={"scan_coverage": True}),
            patch("code_reviewer.web_app._web_user_role", return_value="manager"),
            patch("code_reviewer.web_app.review_jira_issue_merge_requests", return_value=discovered),
            patch("code_reviewer.web_app.list_reports", return_value=[]),
            patch("code_reviewer.web_app.list_review_job_snapshots", return_value=[]),
        ):
            coverage = build_review_coverage("admin", jira_keys="ECHNL-1001")

        row = coverage["issues"][0]
        self.assertEqual(row["summary"], "LDAP login support")
        self.assertEqual(row["responsible"], "kevin.tan")
        self.assertEqual(row["mr_count"], 1)


if __name__ == "__main__":
    unittest.main()
