from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import review
from code_reviewer.review_service import parse_jira_issue_keys


class CliMultiJiraTests(unittest.TestCase):
    def test_parse_jira_issue_keys_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_jira_issue_keys(" echnl-1001, ECHNL-1002,ECHNL-1001 "),
            ["ECHNL-1001", "ECHNL-1002"],
        )

    def test_cli_routes_comma_separated_jira_keys_to_batch_review(self) -> None:
        summary = {"errors": [], "items": [], "jira_keys": ["ECHNL-1001", "ECHNL-1002"]}
        with tempfile.TemporaryDirectory() as temp, patch(
            "review.review_jira_issues_merge_requests",
            return_value=summary,
        ) as batch_review, contextlib.redirect_stdout(io.StringIO()):
            exit_code = review.main(
                [
                    "--jira",
                    "ECHNL-1001,ECHNL-1002",
                    "--jira-mr-list-only",
                    "--output-dir",
                    str(Path(temp) / "reports"),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(batch_review.call_args.kwargs["jira_keys"], ["ECHNL-1001", "ECHNL-1002"])
        self.assertTrue(batch_review.call_args.kwargs["list_only"])

    def test_cli_rejects_multi_jira_with_single_mr_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(
            io.StringIO()
        ):
            review.main(
                [
                    "--jira",
                    "ECHNL-1001,ECHNL-1002",
                    "--mr-url",
                    "https://gitlab.example.com/group/project/-/merge_requests/1",
                    "--output-dir",
                    str(Path(temp) / "reports"),
                ]
            )
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
