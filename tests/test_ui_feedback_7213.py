from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer import web_app
from code_reviewer import review_service


class UiFeedback7213Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = web_app.render_index("admin")

    @staticmethod
    def _between(value: str, start: str, end: str) -> str:
        start_at = value.index(start)
        return value[start_at : value.index(end, start_at)]

    def test_crystal_brand_is_used_on_login_and_workspace(self) -> None:
        login = web_app.render_login()
        self.assertIn('/assets/ttl-jay-crystal-logo.png', login)
        self.assertIn('/assets/ttl-jay-crystal-logo.png', self.page)
        logo = web_app.WEB_STATIC_DIR / "ttl-jay-crystal-logo.png"
        self.assertTrue(logo.is_file())
        self.assertGreater(logo.stat().st_size, 10_000)

    def test_sprint_overview_prevents_horizontal_overflow_and_separates_heading_copy(self) -> None:
        self.assertIn("overflow-x: hidden;", self.page)
        self.assertIn(".coverage-lifecycle-head > div:first-child {", self.page)
        self.assertIn("display: grid;", self.page)
        self.assertIn("gap: 3px;", self.page)

    def test_single_issue_review_leaves_sprint_overview_and_runs_only_the_issue(self) -> None:
        handler = self._between(
            self.page,
            "async function runCoverageIssueReview(",
            "async function runCoverageMissingReviews(",
        )
        self.assertIn("if ($('jira')) $('jira').value = key;", handler)
        self.assertIn("if ($('sprint')) $('sprint').value = '';", handler)
        self.assertIn("if ($('jiraFilter')) $('jiraFilter').value = '';", handler)
        self.assertIn("closeCoverage();", handler)
        self.assertIn("runReview()", handler)
        self.assertNotIn("keepCoverageOpen", handler)
        self.assertNotIn("scanCoverage()", handler)

    def test_manager_can_start_all_missing_issue_reviews_from_overview(self) -> None:
        self.assertIn("data-coverage-run-missing=", self.page)
        self.assertIn(">Run remaining</button>", self.page)
        self.assertIn("currentPermissions.run_sprint_review", self.page)
        self.assertIn("Start Code Review for ${keys.length} Issue(s) without reports?", self.page)

    def test_web_jira_batch_routes_all_missing_keys_to_batch_review(self) -> None:
        summary = {"errors": [], "items": []}
        with patch.object(
            review_service,
            "review_jira_issues_merge_requests",
            return_value=summary,
        ) as batch_review:
            result = review_service.run_review_from_payload(
                {"mode": "jira", "jira_key": "ECHNL-1001, ECHNL-1002"}
            )

        batch_review.assert_called_once()
        self.assertEqual(
            batch_review.call_args.kwargs["jira_keys"],
            ["ECHNL-1001", "ECHNL-1002"],
        )
        self.assertTrue(result["ok"])

    def test_report_problem_and_suggestion_are_extracted_for_two_line_preview(self) -> None:
        markdown = """\
### 1. [High] Unsafe fallback

- 类型：Correctness
- 位置：`src/example.ts`
- 问题：Empty arrays are truthy, so the fallback does not run.
- 建议：Choose the first non-empty array and add regression coverage.
"""
        findings = web_app._extract_report_findings(markdown)
        self.assertEqual(findings[0]["problem"], "Empty arrays are truthy, so the fallback does not run.")
        self.assertEqual(
            findings[0]["recommendation"],
            "Choose the first non-empty array and add regression coverage.",
        )
        self.assertIn("问题详情", self.page)
        self.assertIn("处理建议", self.page)
        self.assertIn("-webkit-line-clamp: 2;", self.page)

    def test_legacy_workflow_finding_is_enriched_from_its_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "ECHNL-1001_has-issue-high.md"
            report.write_text(
                "### 1. [High] Example\n\n- 问题：Evidence text\n- 建议：Action text\n",
                encoding="utf-8",
            )
            finding = {"report_index": "1", "details": {"index": "1"}}
            detail = {
                "runs": [{"id": "run-1", "report_path": str(report), "findings": [finding]}],
                "latest_run_group": {
                    "findings": [{"run_id": "run-1", "report_index": "1", "details": {"index": "1"}}]
                },
            }
            web_app._enrich_issue_review_finding_details(detail)

        self.assertEqual(finding["details"]["problem"], "Evidence text")
        self.assertEqual(
            detail["latest_run_group"]["findings"][0]["details"]["suggestion"],
            "Action text",
        )


if __name__ == "__main__":
    unittest.main()
