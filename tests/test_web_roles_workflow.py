from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from code_reviewer.web_app import (
    _can_access_report,
    _application_review_progress,
    _aggregate_coverage_report_status,
    _coverage_report_summary,
    _coverage_scope_summary,
    _jira_key_from_report_name,
    _jira_keys_from_text,
    _review_application_from_discovery,
    build_review_coverage,
    _manual_pass_readiness,
    _record_finding_handling,
    _read_report_metadata,
    _web_user_responsibles,
    _web_user_permissions,
    _web_user_role,
    render_index,
)


REPORT = """# ECHNL-1001 Code Review Report

## Findings

### 1. [High] Blocking behavior

- Problem: broken

### 2. [Medium] Improvement

- Problem: improve
"""


class WebRolesWorkflowTests(unittest.TestCase):
    def test_report_scope_metadata_grants_auditor_access_without_directory_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            report = base / "admin" / "ECHNL-1001_WVAdmin_has-issue-high.md"
            report.parent.mkdir()
            report.write_text(
                '<!-- code_reviewer_metadata: {"responsible_scope":["wen.yi"],'
                '"application":"WVAdmin","release_line":"1.0"} -->\n'
                + ("x" * 200000),
                encoding="utf-8",
            )
            metadata = _read_report_metadata(report)
            self.assertEqual(["wen.yi"], metadata["responsible_scope"])
            with patch(
                "code_reviewer.web_app._web_user_responsibles",
                return_value=["wen.yi"],
            ), patch("code_reviewer.web_app._web_user_role", return_value="auditor"):
                self.assertTrue(_can_access_report(base, report, "auditor", metadata=metadata))

    def test_default_roles_and_permissions(self) -> None:
        with patch("code_reviewer.web_app._load_web_users", return_value={}), patch(
            "code_reviewer.web_app._configured_web_user_profiles",
            return_value={},
        ):
            self.assertEqual(_web_user_role("admin"), "manager")
            self.assertEqual(_web_user_role("wen.yi"), "auditor")
            self.assertTrue(_web_user_permissions("admin")["run_sprint_review"])
            self.assertTrue(_web_user_permissions("admin")["run_release_gate"])
            self.assertFalse(_web_user_permissions("wen.yi")["run_sprint_review"])
            self.assertFalse(_web_user_permissions("wen.yi")["run_release_gate"])

    def test_release_gate_workspace_is_manager_only(self) -> None:
        with patch("code_reviewer.web_app.list_gitlab_projects_for_user", return_value=[]), patch(
            "code_reviewer.web_app._load_web_users", return_value={}
        ), patch("code_reviewer.web_app._configured_web_user_profiles", return_value={}):
            manager_html = render_index("admin")
            auditor_html = render_index("wen.yi")

        self.assertIn('id="releaseGatePanel"', manager_html)
        self.assertIn('id="runReleaseGateBtn"', manager_html)
        self.assertNotIn("__RELEASE_GATE_PANEL__", manager_html)
        self.assertNotIn('id="releaseGatePanel"', auditor_html)

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
            "kelvinh.wu": {"role": "auditor", "responsible": ["kelvinh.wu"]},
            "benyq.feng": {"role": "developer", "responsible": ["kelvinh.wu"]},
            "luckxh.chen": {"role": "auditor", "responsible": ["luckxh.chen"]},
        }
        with patch("code_reviewer.web_app._load_web_users", return_value={}), patch(
            "code_reviewer.web_app._configured_web_user_profiles", return_value=profiles,
        ):
            self.assertEqual(_web_user_responsibles("gerhard.guo"), ["wen.yi"])
            self.assertEqual(_web_user_responsibles("bryan.tan"), ["wen.yi"])
            self.assertEqual(_web_user_responsibles("vincentgr.wang"), ["kevin.tan"])
            self.assertEqual(_web_user_responsibles("kelvinh.wu"), ["kelvinh.wu"])
            self.assertEqual(_web_user_responsibles("benyq.feng"), ["kelvinh.wu"])
            self.assertEqual(_web_user_responsibles("luckxh.chen"), ["luckxh.chen"])

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

    def test_coverage_compares_reported_issues_and_three_review_lifecycle_states(self) -> None:
        summary = _coverage_report_summary(
            [
                {"report_count": 2, "report_review_status": "pending"},
                {"report_count": 1, "report_review_status": "ready"},
                {"report_count": 1, "report_review_status": "passed"},
                {"report_count": 0, "report_review_status": ""},
            ],
            {"running": 1, "failed": 2},
        )
        self.assertEqual(summary["issues_with_reports"], 3)
        self.assertEqual(summary["issues_without_reports"], 1)
        self.assertEqual(summary["generated_breakdown"], {"handling": 1, "ready": 1, "passed": 1})
        self.assertEqual(summary["generating"], 1)
        self.assertEqual(summary["failed"], 2)

    def test_report_filename_jira_key_allows_generated_underscore_suffix(self) -> None:
        name = "wen.yi/ECHNL-5747_iTrade-Client-7.5.1_has-issue-critical.md"
        self.assertEqual(_jira_key_from_report_name(name), "ECHNL-5747")
        self.assertEqual(_jira_keys_from_text(name), ["ECHNL-5747", "CLIENT-7"])

    def test_scope_coverage_distinguishes_required_scopes_from_report_history(self) -> None:
        summary = _coverage_scope_summary(
            [{"report_count": 3}],
            [
                {"application": "iTrade Client", "release_line": "7.5.0", "issue_count": 1, "issues_with_reports": 1},
                {"application": "iTrade Client", "release_line": "7.5.1", "issue_count": 1, "issues_with_reports": 1},
            ],
        )
        self.assertEqual(summary["application_scope_count"], 2)
        self.assertEqual(summary["application_scopes_with_reports"], 2)
        self.assertEqual(summary["application_scopes_without_reports"], 0)
        self.assertEqual(summary["generated_report_files"], 3)

    def test_coverage_matches_generated_underscore_report_name_to_issue(self) -> None:
        discovered = {
            "jira_key": "ECHNL-5747",
            "jira_summary": "Korean market restoration",
            "jira_status": "Development Done",
            "items": [
                {
                    "jira_key": "ECHNL-5747",
                    "jira_summary": "Korean market restoration",
                    "jira_status": "Development Done",
                    "responsible": "wen.yi",
                    "git_tools_group": "itrade-client",
                    "git_tools_module": "itrade-client",
                    "gitlab_project": "itrade-sv/client/web",
                }
            ],
            "issues_without_mrs": [],
            "errors": [],
        }
        report = {
            "name": "ECHNL-5747_iTrade-Client-7.5.1_has-issue-critical.md",
            "relative_path": "wen.yi/ECHNL-5747_iTrade-Client-7.5.1_has-issue-critical.md",
            "output_dir": "/reports/e-channel-sprint20260724",
        }
        report_state = {
            "status": "pending",
            "finding_count": 1,
            "handled_count": 0,
            "blocking_pending": 1,
            "application": "iTrade Client",
            "release_line": "7.5.1",
        }
        store = Mock()
        store.list_issues.return_value = []
        with (
            patch("code_reviewer.web_app._web_user_permissions", return_value={"scan_coverage": True}),
            patch("code_reviewer.web_app._web_user_role", return_value="manager"),
            patch("code_reviewer.web_app.review_jira_issue_merge_requests", return_value=discovered),
            patch("code_reviewer.web_app.list_reports", return_value=[report]),
            patch("code_reviewer.web_app._coverage_report_state", return_value=report_state),
            patch("code_reviewer.web_app.list_review_job_snapshots", return_value=[]),
            patch("code_reviewer.web_app.workflow_store", return_value=store),
        ):
            coverage = build_review_coverage("admin", jira_keys="ECHNL-5747")

        self.assertEqual(coverage["report_coverage"]["issues_with_reports"], 1)
        self.assertEqual(coverage["report_coverage"]["application_scopes_with_reports"], 1)
        self.assertEqual(coverage["issues"][0]["latest_report"], report["relative_path"])

    def test_review_application_mapping_uses_configured_project_group_and_module(self) -> None:
        self.assertEqual(
            _review_application_from_discovery(
                {"git_tools_group": "itrade-client", "git_tools_module": "itrade-client"}
            ),
            "iTrade Client",
        )
        self.assertEqual(
            _review_application_from_discovery(
                {"git_tools_group": "itrade-client", "git_tools_module": "services-terminal"}
            ),
            "Services Terminal",
        )
        self.assertEqual(
            _review_application_from_discovery({"git_tools_group": "wvadmin-repository"}),
            "WVAdmin",
        )
        self.assertEqual(
            _review_application_from_discovery({"git_tools_group": "dps11-repository"}),
            "DPS",
        )
        self.assertEqual(_review_application_from_discovery({}), "Unmapped")

    def test_application_progress_counts_cross_application_issue_once_per_application(self) -> None:
        progress = _application_review_progress(
            [
                {
                    "jira_key": "ECHNL-1001",
                    "applications": ["iTrade Client", "Services Terminal"],
                    "workflow_status": "passed",
                    "report_count": 2,
                },
                {
                    "jira_key": "ECHNL-1002",
                    "applications": ["iTrade Client"],
                    "workflow_status": "pending",
                    "report_count": 1,
                },
                {
                    "jira_key": "ECHNL-1003",
                    "applications": [],
                    "workflow_status": "missing",
                    "report_count": 0,
                },
            ]
        )
        by_application = {item["application"]: item for item in progress}
        itrade = by_application["iTrade Client"]
        self.assertEqual(itrade["issue_count"], 2)
        self.assertEqual(itrade["readiness_percent"], 50)
        self.assertEqual(itrade["remaining"], 1)
        self.assertFalse(itrade["gate_ready"])
        self.assertEqual(itrade["counts"]["pending"], 1)
        terminal = by_application["Services Terminal"]
        self.assertEqual(terminal["readiness_percent"], 100)
        self.assertTrue(terminal["gate_ready"])
        self.assertFalse(by_application["Unmapped"]["gate_ready"])

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
                    "git_tools_group": "dps11-repository",
                    "gitlab_project": "wvp-sv/dps11/microsrvs/momd",
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
        self.assertEqual(row["applications"], ["DPS"])
        self.assertEqual(coverage["application_progress"][0]["application"], "DPS")
        self.assertEqual(coverage["application_progress"][0]["readiness_percent"], 0)
        self.assertEqual(coverage["report_coverage"]["issues_with_reports"], 0)
        self.assertEqual(coverage["report_coverage"]["issues_without_reports"], 1)
        self.assertEqual(
            coverage["report_coverage"]["generated_breakdown"],
            {"handling": 0, "ready": 0, "passed": 0},
        )


if __name__ == "__main__":
    unittest.main()
