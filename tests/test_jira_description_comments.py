from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import patch

from code_reviewer.jira_client import JiraClient, JiraIssue, _jira_issue_from_item, is_description_template_comment
from code_reviewer.llm_provider import _review_prompt
from code_reviewer.models import ReviewInput


def _table_body(*labels: str) -> dict:
    return {
        'type': 'doc',
        'version': 1,
        'content': [
            {
                'type': 'table',
                'content': [
                    {
                        'type': 'tableRow',
                        'content': [
                            {
                                'type': 'tableCell',
                                'content': [
                                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': label}]}
                                ],
                            }
                            for label in labels
                        ],
                    }
                ],
            }
        ],
    }


class _FakeJiraClient(JiraClient):
    def __init__(self, responses: list[dict]) -> None:
        self.base_url = 'https://jira.example.com'
        self.username = 'reviewer'
        self.api_token = 'token'
        self.responses = responses
        self.paths: list[str] = []

    def _request_json(self, path: str, method: str = 'GET', payload: dict | None = None):
        self.paths.append(path)
        return self.responses.pop(0)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


class JiraDescriptionCommentTests(unittest.TestCase):
    def test_issue_parser_keeps_components_and_responsibles(self) -> None:
        issue = _jira_issue_from_item(
            {
                "key": "ECHNL-5757",
                "fields": {
                    "summary": "Scope extraction",
                    "components": [{"name": "MO Client Config"}, {"name": "MOMD"}, {"name": "WVAdmin"}],
                    "customfield_10036": [
                        {"displayName": "Luck Chen"},
                        {"displayName": "Tran Trung Hieu"},
                    ],
                },
            }
        )

        self.assertEqual(issue.components, ["MO Client Config", "MOMD", "WVAdmin"])
        self.assertEqual(issue.responsibles, ["Luck Chen", "Tran Trung Hieu"])

    def test_requirement_template_requires_table_and_all_defined_rows(self) -> None:
        labels = (
            'Screenshot', 'Description', 'Additional remarks',
            'Requirement Description', 'Requirement Analysis', 'Proposed Solution', 'Expected Result',
            'Affected Project or Functional Scope', 'Involved File Lists',
        )
        body = _table_body(*labels)
        text = ' '.join(labels)

        self.assertTrue(is_description_template_comment(body, text, 'Story'))
        self.assertFalse(is_description_template_comment({'type': 'doc'}, text, 'Story'))
        self.assertFalse(is_description_template_comment(body, text.replace('Expected Result', ''), 'Story'))

    def test_bug_template_does_not_require_expected_result(self) -> None:
        labels = (
            'Screenshot', 'Description', 'Additional remarks',
            'Bug Description', 'Bug Analysis', 'Workaround',
            'Affected Project or Functional Scope', 'Involved File Lists',
        )
        body = _table_body(*labels)
        self.assertTrue(is_description_template_comment(body, ' '.join(labels), 'Bug'))

    def test_fetch_keeps_only_template_comments_in_chronological_order(self) -> None:
        first = _table_body(
            'Screenshot', 'Description', 'Additional remarks',
            'Requirement Description', 'Requirement Analysis', 'Proposed Solution', 'Expected Result',
            'Affected Project or Functional Scope', 'Involved File Lists', 'follow-up one',
        )
        second = _table_body(
            'Screenshot', 'Description', 'Additional remarks',
            'Requirement Description', 'Requirement Analysis', 'Proposed Solution', 'Expected Result',
            'Affected Project or Functional Scope', 'Involved File Lists', 'follow-up two',
        )
        test_comment = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Tests: passed'}]}]}
        client = _FakeJiraClient([{'comments': [{'body': first}, {'body': test_comment}, {'body': second}], 'total': 3}])

        comments = client.fetch_description_template_comments('ECHNL-1', 'Story')

        self.assertEqual(len(comments), 2)
        self.assertIn('follow-up one', comments[0])
        self.assertIn('follow-up two', comments[1])
        self.assertIn('orderBy=created', client.paths[0])

    def test_final_description_appends_each_formal_comment(self) -> None:
        issue = JiraIssue('ECHNL-1', 'Summary', 'Original', 'Owner', 'Open', 'Sprint', 'Story', [])
        issue.description_comments = ['Follow-up one', 'Follow-up two']
        self.assertEqual(issue.final_description, 'Original\n\nFollow-up one\n\nFollow-up two')

    def test_final_description_is_included_in_llm_review_context(self) -> None:
        review_input = ReviewInput(jira_key='ECHNL-1')
        review_input.metadata['jira_description'] = 'Original\n\nFollow-up requirement'
        prompt = _review_prompt(review_input)
        self.assertIn('Final Jira issue description', prompt)
        self.assertIn('Follow-up requirement', prompt)

    def test_get_request_retries_once_with_endpoint_and_attempt_context(self) -> None:
        client = JiraClient("https://jira.example.com", "reviewer", "token")
        with (
            patch(
                "code_reviewer.jira_client.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError(TimeoutError("read operation timed out")),
                    _FakeResponse({"ok": True}),
                ],
            ) as urlopen,
            patch("code_reviewer.jira_client.time.sleep") as sleep,
        ):
            result = client._request_json("/rest/api/3/issue/ECHNL-1/comment")

        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once()

    def test_get_request_reports_final_attempt_and_endpoint(self) -> None:
        client = JiraClient("https://jira.example.com", "reviewer", "token")
        with (
            patch(
                "code_reviewer.jira_client.urllib.request.urlopen",
                side_effect=urllib.error.URLError(TimeoutError("read operation timed out")),
            ),
            patch("code_reviewer.jira_client.time.sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"GET /rest/api/3/issue/ECHNL-1/comment failed at request attempt 2/2",
            ):
                client._request_json("/rest/api/3/issue/ECHNL-1/comment")

    def test_comment_failure_is_a_warning_and_does_not_drop_sprint_issue(self) -> None:
        client = JiraClient("https://jira.example.com", "reviewer", "token")
        issues = [
            JiraIssue("ECHNL-1", "One", "Description", "Owner", "Done", "Sprint", "Story", []),
            JiraIssue("ECHNL-2", "Two", "Description", "Owner", "Done", "Sprint", "Story", []),
        ]
        events: list[dict] = []

        def fetch(issue_key: str, _issue_type: str = "") -> list[str]:
            if issue_key == "ECHNL-2":
                raise RuntimeError("Jira API GET comment failed at request attempt 2/2")
            return ["Formal requirement update"]

        with patch.object(client, "fetch_description_template_comments", side_effect=fetch):
            warnings = client._load_description_comments(issues, progress=events.append)

        self.assertEqual(["Formal requirement update"], issues[0].description_comments)
        self.assertEqual([], issues[1].description_comments)
        self.assertEqual("ECHNL-2", warnings[0]["jira_key"])
        self.assertEqual("jira-comments", warnings[0]["stage"])
        self.assertIn("/rest/api/3/issue/ECHNL-2/comment", warnings[0]["endpoint"])
        self.assertEqual(2, events[-1]["current"])
        self.assertEqual(2, events[-1]["total"])
        self.assertTrue(any(event["event"] == "jira-comments-warning" for event in events))


if __name__ == '__main__':
    unittest.main()
