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

    def test_jira_followup_has_wide_summary_and_compact_description_preview(self) -> None:
        page = render_index("admin")

        self.assertIn('maxlength="255" size="50"', page)
        self.assertIn('class="followup-adf-preview"', page)
        self.assertIn('type="button">Edit issue</button>', page)
        self.assertIn('class="finding-head-action" data-handle-finding=', page)
        self.assertIn('.finding-handling-form:not(.followup-active) .finding-handling-secondary { display: none; }', page)
        self.assertNotIn('class="finding-submit-row"', page)
        self.assertIn('function adfTextPreview(document, maxLength = 180)', page)
        self.assertNotIn('Edit Issue Description (ADF)', page)

    def test_issue_metrics_jump_to_the_first_matching_problem(self) -> None:
        page = render_index("admin")

        self.assertIn('data-jump-severity="critical"', page)
        self.assertIn('data-jump-severity="high"', page)
        self.assertIn('data-jump-blocker="true"', page)
        self.assertIn("target.scrollIntoView({behavior: 'smooth', block: 'center'});", page)
        self.assertIn("target.classList.add('finding-flash');", page)

    def test_adf_description_uses_drag_and_drop_blocks_instead_of_json_editor(self) -> None:
        page = render_index("admin")

        self.assertIn('id="draftAdfSource" class="adf-source" spellcheck="false" hidden', page)
        self.assertIn('id="draftBlockEditor" class="adf-block-editor"', page)
        self.assertIn('class="adf-block" draggable="true"', page)
        self.assertIn('title="Drag to reorder"', page)
        self.assertIn('function renderAdfBlockEditor()', page)
        self.assertIn('function showAdfEditMode()', page)
        self.assertIn("textContent = 'Apply description';", page)
        self.assertIn('id="closeDraftEditorBtn" class="secondary small-action" type="button">Cancel</button>', page)

    def test_discuss_reply_matches_issue_review_card_layout(self) -> None:
        page = render_index("admin")

        self.assertIn('class="thread-column thread-reply-column"', page)
        self.assertIn('class="thread-section-heading"', page)
        self.assertEqual(page.count('class="thread-reply-card"'), 2)
        self.assertIn('id="sendThreadMessageBtn" type="button">Send reply</button>', page)
        self.assertIn('id="generateFollowupsBtn" type="button">Generate follow-ups</button>', page)

    def test_issue_review_detail_tabs_switch_their_panels(self) -> None:
        page = render_index("admin")

        for name in ("problems", "discuss", "history", "pending"):
            self.assertIn(f'data-workflow-tab="{name}"', page)
            self.assertIn(f'data-workflow-panel="{name}"', page)
        self.assertIn("const activateWorkflowTab = name => {", page)
        self.assertIn("panel.hidden = panel.dataset.workflowPanel !== name;", page)
        self.assertIn("activateWorkflowTab('problems');", page)


if __name__ == "__main__":
    unittest.main()
