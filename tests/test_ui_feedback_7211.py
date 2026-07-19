from __future__ import annotations

import re
import unittest

from code_reviewer.web_app import _configuration_app_fields, render_index


class UiFeedback7211Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_index("admin")

    def test_dialog_sizes_are_standardized_and_communication_uses_xl(self) -> None:
        page = self.page
        for token in ("--dialog-s:", "--dialog-m:", "--dialog-l:", "--dialog-xl:", "--dialog-full:"):
            self.assertIn(token, page)
        self.assertIn('class="thread-modal-dialog dialog-size-xl"', page)
        self.assertIn('class="coverage-dialog dialog-size-xl"', page)
        self.assertIn('class="release-notes-dialog" data-dialog-size="l"', page)
        self.assertRegex(page, re.compile(r"\.thread-modal-dialog\s*\{[^}]*var\(--dialog-xl\)", re.S))

    def test_sprint_review_does_not_copy_the_home_jira_field(self) -> None:
        open_coverage = self.page[
            self.page.index("function openCoverage()"):
            self.page.index("function closeCoverage()")
        ]
        self.assertIn("$('coverageJira').value = '';", open_coverage)
        self.assertNotIn("$('jira')?.value", open_coverage)

    def test_sprint_review_uses_overview_and_issue_tabs(self) -> None:
        page = self.page
        self.assertIn('data-coverage-view="overview"', page)
        self.assertIn('data-coverage-view="issues"', page)
        self.assertIn('id="coverageOverviewPanel"', page)
        self.assertIn('id="coverageIssuesPanel"', page)
        self.assertIn("function setCoverageView(view)", page)
        self.assertIn("setCoverageView('overview');", page)

    def test_issue_review_overview_uses_readable_three_column_cards(self) -> None:
        page = self.page
        self.assertIn(
            ".sprint-application-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));",
            page,
        )
        self.assertIn(".sprint-overview-metric .meta { font-size: 13px; }", page)
        self.assertIn("font-size: 22px;", page)

    def test_user_management_selects_first_user_and_has_a_wider_list(self) -> None:
        page = self.page
        self.assertIn("showManagedUserForm(managedUsers[0], false);", page)
        self.assertIn("grid-template-columns: minmax(390px, 42%) minmax(0, 1fr);", page)
        self.assertIn(".user-admin-card-head strong", page)
        self.assertIn("font-size: 15px;", page)

    def test_application_settings_preserves_llm_as_an_acronym(self) -> None:
        fields = _configuration_app_fields(
            {"app": {"llm": {"provider": "codex"}, "review_workflow": {"llm_model": "gpt"}}}
        )
        by_key = {str(item["key"]): item for item in fields}
        self.assertEqual("LLM", by_key["llm.provider"]["category"])
        self.assertEqual("LLM Model", by_key["review_workflow.llm_model"]["label"])


if __name__ == "__main__":
    unittest.main()
