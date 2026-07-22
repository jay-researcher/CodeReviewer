from __future__ import annotations

import unittest
from unittest.mock import patch

from code_reviewer.jira_client import JIRA_SEARCH_FIELDS, JiraIssue
from code_reviewer.models import ReviewInput
from code_reviewer.review_service import _apply_jira_scope_responsible, _jira_scope_responsible_display
from code_reviewer.web_app import (
    _jira_issue_url,
    _review_domain_allows,
    _web_user_review_applications,
)


class ResponsibleReviewPolicyTests(unittest.TestCase):
    def test_jira_responsible_field_is_not_an_inference_input(self) -> None:
        self.assertIn("components", JIRA_SEARCH_FIELDS)
        review_input = ReviewInput(
            project="unknown/project",
            mr_id="1",
            metadata={"application": "Unknown App", "responsible": "fallback.owner"},
        )
        issue = JiraIssue(
            key="ECHNL-1", summary="Unknown", description="", assignee="", status="",
            sprint="", issue_type="", labels=[], components=["Unknown Component"],
            responsibles=["Luck Chen"],
        )
        _apply_jira_scope_responsible(review_input, issue, [])
        self.assertEqual("fallback.owner", review_input.metadata["responsible"])
        self.assertNotIn("scope_responsible", review_input.metadata)

    def test_component_driven_delivery_owner_rules(self) -> None:
        self.assertEqual(
            "Luck Chen",
            _jira_scope_responsible_display("MO Client Config", ["MO Client Config"], []),
        )
        self.assertEqual(
            "Luck Chen",
            _jira_scope_responsible_display("DPS Config", ["DPS Config"], []),
        )
        self.assertEqual(
            "Victor Xu",
            _jira_scope_responsible_display("WVAdmin", ["WVAdmin", "Lowcode Application"], []),
        )
        self.assertEqual(
            "Tran Trung Hieu",
            _jira_scope_responsible_display("WVAdmin", ["WVAdmin", "MOMD"], []),
        )
        self.assertEqual(
            "Sunny Cheng",
            _jira_scope_responsible_display("DPS", ["DPS11", "Account Opening System"], []),
        )
        self.assertEqual("Kevin Tan", _jira_scope_responsible_display("DPS", ["DPS11"], []))
        self.assertEqual("Wen Yi", _jira_scope_responsible_display("iTrade Client", ["iTrade Client 7.5.1"], []))
        self.assertEqual("Tran Trung Hieu", _jira_scope_responsible_display("Services Terminal", [], []))

    def test_unknown_component_scope_enters_config_fallback(self) -> None:
        self.assertEqual("", _jira_scope_responsible_display("Unknown App", ["Unknown Component"], []))

    def test_company_config_specific_rules_precede_regular_application_rules(self) -> None:
        resource = [{"release_gate_role": "company_config"}]
        self.assertEqual(
            "Luck Chen",
            _jira_scope_responsible_display("iTrade Client", ["MO Client Config"], resource),
        )
        self.assertEqual(
            "Luck Chen",
            _jira_scope_responsible_display("DPS", ["DPS Config"], resource),
        )

    def test_web_frontend_review_domain_is_application_scoped(self) -> None:
        policy = {
            "web_frontend": {
                "applications": ["WVAdmin", "Services Terminal"],
                "reviewers": ["wen.yi"],
            }
        }
        with (
            patch("code_reviewer.web_app.app_config_get", return_value=policy),
            patch("code_reviewer.web_app._web_user_role", return_value="auditor"),
        ):
            self.assertEqual({"wvadmin", "services terminal"}, _web_user_review_applications("wen.yi"))
            self.assertTrue(_review_domain_allows("wen.yi", ["WVAdmin"]))
            self.assertTrue(_review_domain_allows("wen.yi", ["Services Terminal"]))
            self.assertFalse(_review_domain_allows("wen.yi", ["DPS"]))

    def test_jira_link_is_safe_and_requires_configuration(self) -> None:
        with patch.dict("code_reviewer.web_app.os.environ", {"JIRA_URL": "https://jira.example.test"}, clear=False):
            self.assertEqual("https://jira.example.test/browse/ECHNL-5757", _jira_issue_url("ECHNL-5757"))
            self.assertEqual("", _jira_issue_url("../unsafe"))


if __name__ == "__main__":
    unittest.main()
