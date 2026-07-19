from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_reviewer.local_workspaces import git_tools_project_entries
from code_reviewer.models import Finding, ReviewInput, ReviewResult
from code_reviewer.report import save_report
from code_reviewer.review_service import (
    _attach_git_tools_project_match,
    _release_gate_branch_role,
)
from code_reviewer.web_app import _configured_web_user_profiles


class ReleaseProjectScopingTests(unittest.TestCase):
    def test_company_config_and_scr_use_release_resource_filename_contract(self) -> None:
        cases = [
            ("company_config", "dps#build", "DPS-Company Config_has-issue-high.md"),
            ("scr", "service-terminal#build", "Services Terminal-SCR_has-issue-critical.md"),
        ]
        for role, project_name, expected in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                result = ReviewResult(
                    review_input=ReviewInput(
                        project="web-sv-build/project",
                        source_branch="release-resource",
                        metadata={
                            "release_gate_role": role,
                            "project_name": project_name,
                            "responsible": "luckxh.chen",
                        },
                    ),
                    findings=[
                        Finding(
                            severity="High" if role == "company_config" else "Critical",
                            file_path="release/config.yml",
                            line=1,
                            title="Release resource issue",
                            detail="detail",
                            recommendation="fix",
                        )
                    ],
                    conclusion="Has issues",
                    risk_summary=[],
                    test_suggestions=[],
                )

                report = save_report(result, Path(temp))

                self.assertEqual(report.name, expected)

    def test_ordinary_jira_report_filename_is_unchanged(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-9000",
                metadata={"responsible": "wen.yi"},
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )
        with tempfile.TemporaryDirectory() as temp:
            report = save_report(result, Path(temp))
        self.assertEqual(report.name, "ECHNL-9000_pass.md")

    def test_project_specific_git_version_branches_are_release_gate_entries(self) -> None:
        branches = [
            "WVAdmin_GIT_VERSION-1.0.84",
            "ITRADE_CLIENT_GIT_VERSION-7.5.1.39",
            "SERVICES_TERMINAL_GIT_VERSION-5.0.63",
            "DPS_GIT_VERSION-11.2.84",
            "DPS11_GIT_VERSION-1.4.75",
        ]
        for branch in branches:
            with self.subTest(branch=branch):
                self.assertEqual(_release_gate_branch_role(branch), "git_version")

    def test_release_gate_scope_comes_from_configured_gitlab_project(self) -> None:
        projects = [
            ("web-sv-build/webfe/wvadmin", "WVAdmin_GIT_VERSION-1.0.84", "WVAdmin"),
            ("web-sv-build/webfe/itrade-client", "ITRADE_CLIENT_GIT_VERSION-7.5.1.39", "iTrade Client"),
            ("web-sv-build/webfe/services-terminal", "SERVICES_TERMINAL_GIT_VERSION-5.0.63", "Services Terminal"),
            ("web-sv-build/dps", "DPS11_GIT_VERSION-1.4.75", "DPS"),
        ]
        for project_path, branch, expected in projects:
            with self.subTest(project_path=project_path):
                review_input = ReviewInput(
                    project=project_path,
                    source_branch=branch,
                    metadata={"gitlab_project_path": project_path},
                )
                _attach_git_tools_project_match(review_input)
                self.assertEqual(review_input.metadata["release_gate_project"], expected)
                self.assertEqual(review_input.metadata["release_gate_project_path"], project_path)
                self.assertEqual(review_input.metadata["release_gate_project_match"], "matched")
                self.assertEqual(review_input.metadata["release_gate_role"], "git_version")

    def test_unconfigured_git_version_project_is_rejected(self) -> None:
        review_input = ReviewInput(
            project="other/build",
            source_branch="GIT_VERSION-1.0",
            metadata={"gitlab_project_path": "other/build"},
        )
        with self.assertRaisesRegex(ValueError, "is not defined"):
            _attach_git_tools_project_match(review_input)

    def test_release_resource_reports_do_not_overwrite_same_project_and_status(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="web-sv-build/dps",
                source_branch="DPS11_Config-1.0",
                metadata={
                    "release_gate_role": "company_config",
                    "release_gate_project": "DPS",
                    "responsible": "luckxh.chen",
                },
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )
        with tempfile.TemporaryDirectory() as temp:
            first = save_report(result, Path(temp))
            second = save_report(result, Path(temp))

            self.assertEqual(first.name, "DPS-Company Config_pass.md")
            self.assertRegex(second.name, r"^DPS-Company Config_pass_rescan-\d{14}\.md$")
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_release_resource_ownership_and_team_profiles_are_configured(self) -> None:
        by_path = {entry.project_path: entry for entry in git_tools_project_entries()}
        self.assertEqual(by_path["web-sv-build/dps"].responsible, "luckxh.chen")
        self.assertEqual(by_path["web-sv-build/webfe/itrade-client"].responsible, "luckxh.chen")

        profiles = _configured_web_user_profiles()
        self.assertEqual(profiles["kelvinh.wu"]["role"], "auditor")
        self.assertEqual(profiles["kelvinh.wu"]["responsible"], ["kelvinh.wu"])
        self.assertEqual(profiles["benyq.feng"]["role"], "developer")
        self.assertEqual(profiles["benyq.feng"]["responsible"], ["kelvinh.wu"])
        self.assertEqual(profiles["luckxh.chen"]["role"], "auditor")


if __name__ == "__main__":
    unittest.main()
