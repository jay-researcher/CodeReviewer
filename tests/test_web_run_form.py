from __future__ import annotations

import unittest

from code_reviewer.web_app import render_index


class WebRunFormTests(unittest.TestCase):
    def test_run_form_has_accessible_manual_collapse_controls(self) -> None:
        page = render_index("admin")

        self.assertIn('id="runFormToggle"', page)
        self.assertIn('aria-controls="runFormBody"', page)
        self.assertIn('id="progressPanel" class="progress-panel" tabindex="-1"', page)
        self.assertIn("function setRunFormCollapsed(collapsed, options = {})", page)

    def test_run_form_tracks_the_full_review_job_lifecycle(self) -> None:
        page = render_index("admin")

        self.assertIn("reviewLifecycleActive = true;", page)
        self.assertIn("if (reviewLifecycleActive && active === 0)", page)
        self.assertIn("setRunFormCollapsed(true, { payload, focusProgress: true });", page)
        self.assertIn("setRunFormCollapsed(false);", page)

    def test_expanded_run_form_does_not_clip_action_buttons(self) -> None:
        page = render_index("admin")

        self.assertIn(".run-form-body {\n      margin-top: 12px;", page)
        self.assertIn("overflow: visible;", page)
        self.assertNotIn("max-height: 360px;", page)
        self.assertIn(".run-panel.form-collapsed .run-form-body", page)
        self.assertIn("max-height: 0;", page)

    def test_report_history_defaults_to_markdown_reports_first(self) -> None:
        page = render_index("admin")

        markdown_index = page.index('data-history-tab="reports"')
        responsible_index = page.index('data-history-tab="responsibles"')
        self.assertLess(markdown_index, responsible_index)
        self.assertIn('data-history-tab="reports" role="tab" aria-selected="true"', page)
        self.assertIn('id="reportsPane" class="history-pane" role="tabpanel"', page)
        self.assertIn('id="responsiblesPane" class="history-pane" role="tabpanel" hidden', page)

    def test_review_workflow_uses_guidance_responsive_forms_and_issue_cards(self) -> None:
        page = render_index("admin")

        self.assertIn('id="handlingGuidanceTitle">处理说明</h4>', page)
        self.assertIn('class="finding-handling-form"', page)
        self.assertIn('.followup-fields[hidden] { display: none; }', page)
        self.assertIn('class="workflow-section-title">Problem list', page)
        self.assertIn('class="issue-review-cards"', page)
        self.assertIn('class="issue-review-card${selected}"', page)
        self.assertNotIn('class="issue-review-table"', page)


if __name__ == "__main__":
    unittest.main()
