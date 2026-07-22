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
        self.assertIn("setRunFormCollapsed(true, { payload, focusProgress: options.focusProgress !== false });", page)
        self.assertIn("setRunFormCollapsed(false);", page)

    def test_expanded_run_form_does_not_clip_action_buttons(self) -> None:
        page = render_index("admin")

        self.assertIn(".run-form-body {\n      margin-top: 12px;", page)
        self.assertIn("overflow: visible;", page)
        self.assertNotIn("max-height: 360px;", page)
        self.assertIn(".run-panel.form-collapsed .run-form-body", page)
        self.assertIn("max-height: 0;", page)

    def test_run_review_sections_keep_a_clear_responsive_hierarchy(self) -> None:
        page = render_index("admin")

        self.assertIn('class="actions run-primary-actions" role="group" aria-label="Review actions"', page)
        self.assertIn('id="status" class="status run-action-status" role="status" aria-live="polite"', page)
        self.assertIn('class="release-gate-panel"', page)
        self.assertIn('class="status release-gate-status" role="status" aria-live="polite"', page)
        self.assertIn("container-name: run-review;", page)
        self.assertIn("@container run-review (max-width: 680px)", page)
        self.assertIn("@container run-review (max-width: 460px)", page)
        self.assertIn('class="release-gate-field-head"', page)
        self.assertLess(page.index('class="release-gate-field-head"'), page.index('id="releaseGateMrUrl"'))
        self.assertLess(page.index('id="releaseGateMrUrl"'), page.index('id="runReleaseGateBtn"'))
        self.assertIn('class="release-gate-footer"', page)
        self.assertIn("justify-content: space-between;", page)
        self.assertIn(".release-gate-field > textarea {", page)
        self.assertIn('id="releaseGateMrUrl" rows="1"', page)
        self.assertIn("overflow: hidden;", page)
        self.assertIn("function autoSizeReleaseGateUrl()", page)
        self.assertIn("Math.min(Math.max(field.scrollHeight, oneLineHeight), twoLineHeight)", page)
        self.assertIn(".release-gate-field { display: grid; min-width: 0; gap: 18px; }", page)
        self.assertIn(".release-gate-field-head { align-items: stretch; flex-direction: column; }", page)
        self.assertIn("overflow-x: auto;", page)
        self.assertNotIn("Run the final release gate in Web after Sprint review. Company Config and SCR resources", page)

    def test_release_gate_normalizes_wrapped_mr_url_and_uses_consistent_validation(self) -> None:
        page = render_index("admin")

        self.assertIn("replace(/\\s+/g, '').trim()", page)
        self.assertIn("^\\/.+\\/-\\/merge_requests\\/[0-9]+\\/?$", page)
        self.assertIn("$('releaseGateMrUrl').value = mrUrl;", page)
        self.assertIn("autoSizeReleaseGateUrl();", page)
        self.assertIn('id="releaseGateCandidateSelect"', page)
        self.assertIn("candidateField.hidden = candidates.length < 2;", page)

    def test_sprint_overview_filter_guidance_spans_the_toolbar(self) -> None:
        page = render_index("admin")

        self.assertIn(".coverage-filters .field-help {", page)
        self.assertIn("grid-column: 1 / -1;", page)
        self.assertIn(".coverage-filters #coverageScanBtn", page)
        self.assertIn("width: min(1480px, calc(100vw - 24px));", page)
        self.assertIn('id="coverageProgress" class="coverage-progress"', page)
        self.assertIn("/api/review-coverage-jobs", page)
        self.assertIn("COVERAGE_LONG_SCAN_SECONDS = 30", page)
        self.assertIn("Timeout countdown", page)
        self.assertIn("coverageProgress').hidden = false", page)
        self.assertIn("coverageProgress').hidden = true", page)
        self.assertIn('data-coverage-run-review=', page)
        self.assertIn("async function runCoverageIssueReview(jiraKey, button)", page)
        self.assertIn("currentPermissions.run_issue_review", page)
        self.assertIn('id="coverageRows" class="coverage-card-grid"', page)
        self.assertIn('class="coverage-issue-card"', page)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", page)
        self.assertNotIn('class="coverage-table"', page)
        self.assertIn("item.workflow_status === 'missing' && currentPermissions.run_issue_review", page)
        self.assertLess(page.index('data-coverage-run-review='), page.index('class="coverage-card-summary"'))

        self.assertIn("Issues with reports", page)
        self.assertIn("Issues without reports", page)
        self.assertIn("Generated report lifecycle", page)
        self.assertIn("Ready for Pass", page)
        self.assertIn("Review Pass", page)
        self.assertIn('id="coverageApplications" class="coverage-applications"', page)
        self.assertIn("Application release readiness", page)
        self.assertIn("100% means every scoped Issue is Review Pass", page)
        self.assertIn('class="coverage-application-card', page)
        self.assertIn("Ready for Release Gate", page)
        self.assertIn("Project mapping required", page)
        self.assertIn('class="coverage-card-applications"', page)
        self.assertIn("item.review_cycle_number", page)
        self.assertIn("No Review Cycle yet", page)
        self.assertIn("async function pollCoverageJob(jobId, options = {})", page)
        self.assertIn("Closing this window will not stop it.", page)
        self.assertNotIn("coverageController.abort(), 60000", page)
        self.assertIn("async function runCoverageIssueReview(", page)
        self.assertIn("closeCoverage();", page)
        self.assertNotIn("runReview({ keepCoverageOpen: true })", page)
        self.assertIn('class="coverage-card-state"', page)
        self.assertIn("data-coverage-run-missing=", page)

    def test_sprint_scan_panel_collapses_after_success_and_can_be_reopened(self) -> None:
        page = render_index("admin")

        self.assertIn('id="coverageScanPanel" class="coverage-scan-panel"', page)
        self.assertIn('id="coverageScanToggle"', page)
        self.assertIn('aria-controls="coverageScanBody"', page)
        self.assertIn('id="coverageScanBody" class="coverage-scan-body"', page)
        self.assertIn("function setCoverageScanCollapsed(collapsed)", page)
        self.assertIn("setCoverageScanCollapsed(true);", page)
        self.assertIn("setCoverageScanCollapsed(false);", page)
        self.assertIn("nextCollapsed ? 'Expand scan scope' : 'Collapse scan scope'", page)
        self.assertIn("$('coverageScanToggle').addEventListener('click'", page)
        self.assertIn("$('coverageScanToggle').disabled = active;", page)
        self.assertIn("Recent result' : 'Completed'", page)
        self.assertIn('.coverage-scan-body[hidden] { display: none; }', page)
        self.assertIn("classList.toggle('complete', status === 'done')", page)
        self.assertIn('.coverage-scan-panel.complete {', page)

    def test_review_progress_pauses_auto_scroll_for_sixty_seconds_after_manual_input(self) -> None:
        page = render_index("admin")

        self.assertIn("const jobAutoScrollResumeTimers = new Map();", page)
        self.assertIn("events.addEventListener('wheel', pauseAutoScroll", page)
        self.assertIn("events.addEventListener('pointerdown', pauseAutoScroll", page)
        self.assertIn("events.addEventListener('pointerup', pauseAutoScroll", page)
        self.assertIn("events.addEventListener('touchstart', pauseAutoScroll", page)
        self.assertIn("}, 60000);", page)
        self.assertIn('class="job-events" tabindex="0" aria-label="Review job event stream"', page)

    def test_issue_history_overview_and_issues_panels_respect_hidden(self) -> None:
        page = render_index("admin")

        self.assertIn('id="issueReviewOverviewPanel" class="issue-history-overview" role="tabpanel"', page)
        self.assertIn('id="issueReviewIssuesView" class="workflow-body" role="tabpanel" hidden', page)
        self.assertIn(".issue-history-overview[hidden],\n    .workflow-body[hidden] { display: none; }", page)
        self.assertIn("$('issueReviewOverviewPanel').hidden = issueReviewView !== 'overview';", page)
        self.assertIn("$('issueReviewIssuesView').hidden = issueReviewView !== 'issues';", page)

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

        self.assertIn('id="replyGuidancePopover" class="information-hint-popover"', page)
        self.assertNotIn('id="handlingGuidanceTitle"', page)
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
        self.assertIn('data-jump-severity="medium"', page)
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
        self.assertIn('id="sendThreadMessageBtn" type="button">Reply</button>', page)
        self.assertIn('id="generateFollowupsBtn" type="button">Follow-up</button>', page)

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
