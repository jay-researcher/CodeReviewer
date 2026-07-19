from __future__ import annotations

import re
import unittest

from code_reviewer.web_app import render_index


class IssueReviewProblemsUiAcceptanceTests(unittest.TestCase):
    """Acceptance contract for Issues Review History and Problems UI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_index("admin")
        cls.finding_renderer = cls._between(
            cls.page,
            "function renderWorkflowFinding(",
            "function renderDraftCard(",
        )
        cls.submit_handler = cls._between(
            cls.page,
            "async function submitWorkflowHandling(",
            "async function approveWorkflowHandling(",
        )

    @staticmethod
    def _between(value: str, start: str, end: str) -> str:
        start_at = value.index(start)
        end_at = value.index(end, start_at)
        return value[start_at:end_at]

    def test_severity_cards_show_handled_ratio_percentage_and_progress(self) -> None:
        page = self.page
        self.assertIn("const severityProgress = level =>", page)
        self.assertIn("handled / ${criticalProgress.unhandled} unhandled", page)
        self.assertIn("handled / ${highProgress.unhandled} unhandled", page)
        self.assertIn("handled / ${mediumProgress.unhandled} unhandled", page)
        self.assertIn("${criticalProgress.percent}%", page)
        self.assertIn("${highProgress.percent}%", page)
        self.assertIn("${mediumProgress.percent}%", page)
        self.assertEqual(page.count('class="metric-bar" aria-label='), 3)
        self.assertIn('.metric-bar > span {', page)

    def test_not_issue_requires_a_verifiable_reason(self) -> None:
        renderer = self.finding_renderer
        handler = self.submit_handler
        self.assertIn('<option value="not-issue">不是问题，Pass通过</option>', renderer)
        self.assertIn('处理说明 <span class="required-mark"', renderer)
        self.assertIn('textarea id="note-${finding.id}"', renderer)
        self.assertIn('required></textarea>', renderer)
        self.assertIn("disposition === 'not-issue' ? '“不是问题”必须填写可核验的理由。'", handler)
        self.assertIn("noteElement.setAttribute('aria-invalid', 'true')", handler)

    def test_problem_details_are_clamped_to_two_lines_and_expandable(self) -> None:
        page = self.page
        renderer = self.finding_renderer
        self.assertIn('class="finding-evidence-preview"', renderer)
        self.assertIn('class="finding-evidence-label">问题', renderer)
        self.assertIn('class="finding-evidence-label">建议', renderer)
        self.assertIn("details.recommendation", renderer)
        self.assertIn('data-expand-finding="${finding.id}"', renderer)
        self.assertIn('aria-expanded="false"', renderer)
        self.assertIn('View full details', renderer)
        self.assertIn('-webkit-line-clamp: 2;', page)
        self.assertIn('.finding-evidence-preview.expanded .finding-evidence-text { display: block; white-space: pre-wrap; }', page)
        self.assertIn("button.textContent = expanded ? '收起' : '更多';", page)

    def test_problem_action_uses_the_short_submit_label(self) -> None:
        renderer = self.finding_renderer
        self.assertIn('data-handle-finding="${finding.id}" type="button">Submit</button>', renderer)
        self.assertNotIn('Submit handling', renderer)

    def test_file_and_lineage_copy_is_understandable_and_spaced(self) -> None:
        renderer = self.finding_renderer
        page = self.page
        self.assertIn("finding.file_path || 'Architecture / No specific file'", renderer)
        self.assertIn("persisting:`Still present", renderer)
        self.assertIn("resolved:'Resolved after re-scan'", renderer)
        self.assertNotIn("'No file'", renderer)
        self.assertNotIn("'Persisting'", renderer)
        self.assertIn('class="meta finding-context"', renderer)
        self.assertIn('.finding-card, .timeline-card, .draft-card, .discussion-card {', page)
        self.assertRegex(page, r'(?s)\.finding-card,.*?padding:\s*14px;')

    def test_issue_summary_is_a_two_line_resizable_control(self) -> None:
        renderer = self.finding_renderer
        page = self.page
        self.assertIn('<textarea class="summary-input"', renderer)
        self.assertIn('maxlength="255" rows="2"', renderer)
        self.assertIn('.summary-input { min-height: 58px; resize: vertical; line-height: 1.45; }', page)

    def test_followup_columns_stretch_and_align_at_the_bottom(self) -> None:
        page = self.page
        self.assertIn(
            '.finding-handling-primary, .finding-handling-secondary { align-self: stretch; grid-auto-rows: min-content; }',
            page,
        )
        self.assertIn(
            '.finding-handling-form.followup-active .finding-handling-primary,',
            page,
        )
        self.assertIn('.finding-handling-form.followup-active .finding-handling-secondary { min-height: 250px; }', page)
        self.assertIn('.finding-handling-secondary { min-height: 100%;', page)

    def test_required_validation_and_duplicate_submit_protection_are_consistent(self) -> None:
        page = self.page
        renderer = self.finding_renderer
        handler = self.submit_handler
        self.assertGreaterEqual(renderer.count('class="required-mark"'), 3)
        self.assertIn('.required-mark, .required-when-active { color: var(--danger);', page)
        self.assertIn('.field-message.error, .validation-summary.error { color: var(--danger); }', page)
        self.assertIn('[aria-invalid="true"] { border-color: var(--danger) !important;', page)
        self.assertIn('class="field-message" role="alert"', renderer)
        self.assertIn("errorElement.className = 'field-message error'", handler)
        self.assertIn("await singleFlight(`handling:${findingId}`, button, async () => {", handler)
        self.assertIn("'Idempotency-Key': requestId", handler)

    def test_approval_and_manager_exception_posts_are_duplicate_safe(self) -> None:
        page = self.page
        approval = self._between(
            page,
            "async function approveWorkflowHandling(",
            "async function managerOverrideHandling(",
        )
        manager_override = self._between(
            page,
            "async function managerOverrideHandling(",
            "async function manualWorkflowPass(",
        )
        self.assertIn("singleFlight(`approval:${handlingId}`", approval)
        self.assertIn("'Idempotency-Key'", approval)
        self.assertIn("singleFlight(`manager-override:${handlingId}`", manager_override)
        self.assertIn("'Idempotency-Key'", manager_override)

    def test_issue_review_history_has_a_sprint_grouped_overview_tab(self) -> None:
        page = self.page
        self.assertIn('data-issue-review-view="overview"', page)
        self.assertIn('data-issue-review-view="issues"', page)
        self.assertIn('id="issueReviewOverviewPanel"', page)
        self.assertIn('role="tabpanel"', page)
        self.assertRegex(page, r'function renderIssueReviewOverview\([^)]*\)')
        self.assertRegex(page, r'sprint', re.I)


if __name__ == "__main__":
    unittest.main()
