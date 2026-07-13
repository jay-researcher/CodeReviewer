from __future__ import annotations

import unittest
from code_reviewer.cc_switch import _normalize_provider_selector
from code_reviewer.models import ChangedFile, Finding, ReviewInput, ReviewResult
from code_reviewer.report import _bounded_diff_text, _diff_snippet, render_markdown
from code_reviewer.storage import _compact_changed_files


class ReportSizeGuardTests(unittest.TestCase):
    def test_diff_snippet_bounds_a_large_single_line(self) -> None:
        diff = "+" + ("x" * 2_000_000)

        snippet = _diff_snippet(diff, None, max_chars=4000)

        self.assertLessEqual(len(snippet), 4000)
        self.assertIn("line abbreviated", snippet)

    def test_report_does_not_repeat_multi_megabyte_diff_for_each_finding(self) -> None:
        changed_file = ChangedFile(path="generated.json", additions=1, diff="+" + ("x" * 2_000_000))
        findings = [
            Finding("High", "generated.json", None, f"Finding {index}", "detail", "fix")
            for index in range(100)
        ]
        result = ReviewResult(
            review_input=ReviewInput(jira_key="ECHNL-1", changed_files=[changed_file]),
            findings=findings,
            conclusion="Changes required",
            risk_summary=[],
            test_suggestions=[],
        )

        markdown = render_markdown(result)

        self.assertLess(len(markdown), 1_000_000)
        self.assertIn("Diff abbreviated in this report", markdown)

    def test_history_metadata_uses_a_bounded_diff_excerpt(self) -> None:
        files = [ChangedFile(path="generated.json", diff="x" * 2_000_000)]

        compact = _compact_changed_files(files)

        self.assertLessEqual(len(compact[0]["diff"]), 20000)
        self.assertTrue(compact[0]["diff_truncated"])
        self.assertEqual(compact[0]["diff_original_chars"], 2_000_000)

    def test_claude_code_opus_selector_uses_current_claude_provider(self) -> None:
        self.assertEqual(_normalize_provider_selector("Claude code opus", "claude"), "current")


if __name__ == "__main__":
    unittest.main()
