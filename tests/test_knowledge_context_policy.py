from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from code_reviewer.knowledge_context import (
    KnowledgeProviderPolicy,
    _validate_policy,
    attach_knowledge_context,
)
from code_reviewer.models import ReviewInput


class KnowledgeContextPolicyTests(unittest.TestCase):
    def test_rovo_is_read_only_and_jira_rest_remains_authoritative(self) -> None:
        _validate_policy(
            KnowledgeProviderPolicy(
                authoritative_issue_provider="jira_rest",
                primary_context_provider="rovo_mcp",
                jira_write_provider="jira_rest",
                rovo_read_only=True,
                local_jira_prd_enabled=False,
            )
        )
        with self.assertRaisesRegex(RuntimeError, "retrieval-only"):
            _validate_policy(
                KnowledgeProviderPolicy(
                    authoritative_issue_provider="jira_rest",
                    primary_context_provider="rovo_mcp",
                    jira_write_provider="jira_rest",
                    rovo_read_only=False,
                    local_jira_prd_enabled=False,
                )
            )

    def test_missing_rovo_credentials_is_non_blocking_and_does_not_scan_local_prd(self) -> None:
        review_input = ReviewInput(jira_key="ECHNL-1001", title="Review context")
        policy = KnowledgeProviderPolicy(
            authoritative_issue_provider="jira_rest",
            primary_context_provider="rovo_mcp",
            jira_write_provider="jira_rest",
            rovo_read_only=True,
            local_jira_prd_enabled=False,
        )
        with (
            patch(
                "code_reviewer.knowledge_context.knowledge_provider_policy",
                return_value=policy,
            ),
            patch.dict(
                os.environ,
                {"JIRA_TOKEN": "", "ATLASSIAN_ROVO_MCP_TOKEN": ""},
                clear=False,
            ),
            patch("code_reviewer.knowledge_context.attach_jira_prd_context") as local,
        ):
            attach_knowledge_context(review_input)
        local.assert_not_called()
        self.assertEqual(
            review_input.metadata["rovo_knowledge_status"],
            "credential-missing",
        )

    def test_rovo_results_are_bounded_candidate_context(self) -> None:
        review_input = ReviewInput(jira_key="ECHNL-1001", title="Review context")
        policy = KnowledgeProviderPolicy(
            authoritative_issue_provider="jira_rest",
            primary_context_provider="rovo_mcp",
            jira_write_provider="jira_rest",
            rovo_read_only=True,
            local_jira_prd_enabled=False,
        )
        provider = SimpleNamespace(
            rovo_search_knowledge=lambda config, query, limit: (
                [
                    SimpleNamespace(
                        source="Confluence",
                        title="Design",
                        url="https://example/wiki/design",
                        excerpt="Candidate design context",
                        key="",
                    )
                ],
                SimpleNamespace(status="ok-search"),
            )
        )
        with (
            patch(
                "code_reviewer.knowledge_context.knowledge_provider_policy",
                return_value=policy,
            ),
            patch.dict(os.environ, {"JIRA_TOKEN": "test-token"}, clear=False),
            patch("code_reviewer.knowledge_context.Path.is_file", return_value=True),
            patch(
                "code_reviewer.knowledge_context._load_jira_reviewer_provider",
                return_value=provider,
            ),
        ):
            attach_knowledge_context(review_input)
        self.assertEqual(review_input.metadata["rovo_knowledge_status"], "ok-search")
        self.assertIn("Rovo retrieval-only", review_input.metadata["jira_prd_context"])
        self.assertIn("Candidate design context", review_input.metadata["jira_prd_context"])


if __name__ == "__main__":
    unittest.main()
