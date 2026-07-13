from __future__ import annotations

import unittest

from code_reviewer.models import ChangedFile, ReviewInput, ReviewResult
from code_reviewer.analyzer import _jira_involved_file_findings
from code_reviewer.report import render_markdown
from code_reviewer.resource_optimizer import optimize_prompt_diff
from code_reviewer.review_service import (
    _hydrate_mr_record_for_routing,
    _ignored_branch_type,
    _jira_sprint_branch_type_exclusion,
    _review_input_ignored_branch_type_exclusion,
    _release_gate_build_resource,
)


class _RepositoryClient:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def fetch_repository_file(self, _project: str, path: str, _commit: str) -> str:
        if path not in self.files:
            raise RuntimeError(f"not found: {path}")
        return self.files[path]


class _RoutingClient:
    base_url = "https://gitlab.tx-tech.com"

    def fetch_merge_request(self, _project: str, _iid: str) -> dict[str, str]:
        return {
            "source_branch": "DPS11_Config-1.4.74",
            "target_branch": "11.2.83",
            "state": "merged",
        }


class ReleaseGateTests(unittest.TestCase):
    def test_deferred_company_config_file_satisfies_jira_involved_file_list(self) -> None:
        review_input = ReviewInput(
            jira_key="ECHNL-6000",
            metadata={
                "jira_description": "Involved File Lists\ncompany/SV/config/client.yml\nAcceptance Criteria",
                "deferred_release_gate_resources": [
                    {
                        "release_gate_role": "company_config",
                        "changed_file_paths": ["company/SV/config/client.yml"],
                    }
                ],
            },
        )

        self.assertEqual(_jira_involved_file_findings(review_input), [])
        check = review_input.metadata["jira_involved_files_check"]
        self.assertEqual(check["missing"], [])
        self.assertEqual(check["deferred_actual"], ["company/SV/config/client.yml"])

    def test_deferred_file_is_identified_in_mismatch_report(self) -> None:
        review_input = ReviewInput(
            jira_key="ECHNL-6000",
            metadata={
                "jira_description": "Involved File Lists\ncompany/SV/config/expected.yml\nAcceptance Criteria",
                "deferred_release_gate_resources": [
                    {
                        "release_gate_role": "company_config",
                        "mr_url": "https://gitlab.example.com/build/repo/-/merge_requests/21",
                        "source_branch": "DPS11_Config-1.4.74",
                        "target_branch": "11.2.83",
                        "changed_file_paths": ["company/SV/config/actual.yml"],
                    }
                ],
            },
        )
        findings = _jira_involved_file_findings(review_input)
        result = ReviewResult(review_input, findings, "Has issues", [], [])

        markdown = render_markdown(result, language="en")

        self.assertIn("Changed in deferred Company Config/SCR MR but not listed in Jira", markdown)
        self.assertIn("company/SV/config/actual.yml", markdown)

    def test_branch_type_exclusion_keeps_changed_file_paths(self) -> None:
        review_input = ReviewInput(
            jira_key="ECHNL-6000",
            mr_url="https://gitlab.example.com/build/repo/-/merge_requests/21",
            source_branch="DPS11_Config-1.4.74",
            target_branch="11.2.83",
            changed_files=[ChangedFile(path="company/SV/config/client.yml")],
        )

        exclusion = _review_input_ignored_branch_type_exclusion(review_input)

        self.assertIsNotNone(exclusion)
        self.assertEqual(exclusion["changed_file_paths"], ["company/SV/config/client.yml"])

    def test_version_suffixed_build_resource_branches_are_deferred_but_git_version_is_not(self) -> None:
        self.assertEqual(_ignored_branch_type("DPS11_Config-1.4.74(11.2.83.3)"), "COMPANY_CONFIG")
        self.assertEqual(_ignored_branch_type("dps11_scr-1.4.74"), "SCR")
        self.assertEqual(_ignored_branch_type("DPS11_GIT_VERSION-1.4.74"), "")

    def test_git_version_is_excluded_only_from_jira_sprint_consolidation(self) -> None:
        exclusion = _jira_sprint_branch_type_exclusion(
            {
                "jira_key": "ECHNL-7000",
                "mr_url": "https://gitlab.tx-tech.com/web-sv-build/dps/-/merge_requests/99",
                "source_branch": "DPS11_GIT_VERSION-1.4.74",
                "target_branch": "11.2.83",
            }
        )

        self.assertIsNotNone(exclusion)
        self.assertEqual(exclusion["ignored_branch_type"], "GIT_VERSION")
        self.assertEqual(exclusion["required_review_mode"], "mr")
        self.assertIn("explicit MR mode", exclusion["reason"])

    def test_company_config_diff_is_summarized_before_prompt_budget_is_consumed(self) -> None:
        changed = ChangedFile(
            path="release/11.2.83/mas/config/client_config/mtrade.yml",
            additions=800,
            diff="\n".join(f"+option_{index}: value" for index in range(800)),
        )
        raw = f"diff --git a/{changed.path} b/{changed.path}\n{changed.diff}"

        optimized, diagnostics = optimize_prompt_diff([changed], raw, 8_000)

        self.assertIn("[Build resource summary]", optimized)
        self.assertLess(len(optimized), len(raw))
        self.assertEqual(diagnostics["resource_file_count"], 1)

    def test_remote_link_record_is_hydrated_before_release_resource_routing(self) -> None:
        record = _hydrate_mr_record_for_routing(
            _RoutingClient(),  # type: ignore[arg-type]
            {"mr_url": "https://gitlab.tx-tech.com/web-sv-build/dps/-/merge_requests/12"},
        )
        self.assertEqual(record["source_branch"], "DPS11_Config-1.4.74")
        self.assertEqual(record["target_branch"], "11.2.83")
        self.assertEqual(_ignored_branch_type(record["source_branch"]), "COMPANY_CONFIG")

    def test_code_only_build_resource_does_not_require_db_parser(self) -> None:
        client = _RepositoryClient({"company/SV/script/DPSBuild.php": "define('SCRIPT_VERSION', 'v2.2.5.6');"})
        record = _release_gate_build_resource(
            {"build_file": "release/11.2.83/build-v11.2.83.yml"},
            client,  # type: ignore[arg-type]
            "web-sv-build/dps",
            "a" * 40,
            [{"new_path": "release/11.2.83/SV/site/web/modules/example/example.php", "diff": "+<?php"}],
            {},
        )

        self.assertFalse(record["payload"]["database_files"])
        self.assertFalse(record["errors"])
        self.assertEqual([item["role"] for item in record["scripts"]], ["DPSBuild.php"])

    def test_non_dps_build_resource_does_not_require_dps_runtime_scripts(self) -> None:
        record = _release_gate_build_resource(
            {"build_file": "release/7.5.1/build-v7.5.1.yml"},
            _RepositoryClient({}),  # type: ignore[arg-type]
            "web-sv-build/webfe/itrade-client",
            "c" * 40,
            [{"new_path": "release/7.5.1/SV/site/index.html", "diff": "+<html>"}],
            {},
        )

        self.assertFalse(record["applicable"])
        self.assertEqual(record["scripts"], [])
        self.assertEqual(record["errors"], [])

    def test_ordinary_report_explains_deferred_release_resources(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-6000",
                metadata={
                    "deferred_release_gate_resources": [
                        {
                            "release_gate_role": "company_config",
                            "mr_url": "https://gitlab.tx-tech.com/web-sv-build/dps/-/merge_requests/21",
                            "source_branch": "DPS11_Config-1.4.74",
                            "target_branch": "11.2.83",
                        }
                    ]
                },
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )

        markdown = render_markdown(result, language="en")

        self.assertIn("Deferred Build Resources", markdown)
        self.assertIn("DPS11_Config-1.4.74", markdown)
        self.assertIn("GIT_VERSION release-gate review", markdown)

    def test_related_mr_report_table_includes_request_by(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-6001",
                metadata={
                    "related_merge_requests": [
                        {
                            "mr_url": "https://gitlab.tx-tech.com/team/project/-/merge_requests/77",
                            "mr_id": "77",
                            "request_by": "Jane Reviewer (@jane.reviewer)",
                            "state": "opened",
                            "project_path": "team/project",
                            "source_branch": "feature/ECHNL-6001",
                            "target_branch": "main",
                            "commit": "a" * 40,
                            "responsible": "jane.reviewer",
                            "file_count": 1,
                        }
                    ]
                },
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )

        markdown = render_markdown(result, language="en")

        self.assertIn("| MR | Request By | Status |", markdown)
        self.assertIn("Jane Reviewer (@jane.reviewer)", markdown)

    def test_database_payload_requires_parser_config_and_validates_scr_references(self) -> None:
        files = {
            "company/SV/script/DPSBuild.php": "define('SCRIPT_VERSION', 'v2.2.5.6');",
            "company/SV/script/DBChangeParser.php": "define('SCRIPT_VERSION', '1.2.2');",
            "company/SV/script/db_change.yml": "db_host: example",
            "release/11.2.83/mas/database/db_change.scr": (
                "-- MODULE: mysql, VERSION: 11.2.83.1, COMPANY: mas, ENV: uat\n"
                "mysql data/patch.sql\n"
            ),
            "release/11.2.83/mas/data/patch.sql": "UPDATE sample SET value = 1;",
        }
        record = _release_gate_build_resource(
            {"build_file": "release/11.2.83/build-v11.2.83.yml"},
            _RepositoryClient(files),  # type: ignore[arg-type]
            "web-sv-build/dps",
            "b" * 40,
            [{"new_path": "release/11.2.83/mas/database/db_change.scr", "diff": "+database change"}],
            {},
        )

        self.assertTrue(record["payload"]["database_files"])
        self.assertEqual(record["errors"], [])
        self.assertEqual(record["database_scripts"][0]["references"][0]["resolved"], "release/11.2.83/mas/data/patch.sql")


if __name__ == "__main__":
    unittest.main()
