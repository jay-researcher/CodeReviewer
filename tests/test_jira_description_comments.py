from __future__ import annotations

import unittest

from code_reviewer.jira_client import JiraClient, JiraIssue, is_description_template_comment
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


class JiraDescriptionCommentTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
