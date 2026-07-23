from __future__ import annotations

import re
import unittest

from code_reviewer.web_app import render_index


class UiAcceptance711Tests(unittest.TestCase):
    """UI contract for the 7.1.1 visual-design acceptance pass.

    These tests intentionally assert semantic hooks in addition to visible copy.
    The hooks keep the layout testable without coupling the suite to pixel values
    or a particular browser rendering engine.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_index("admin")

    def test_information_hints_use_one_accessible_icon_pattern(self) -> None:
        page = self.page
        icons = re.findall(
            r'<button[^>]*class="[^"]*information-icon[^"]*"[^>]*>',
            page,
        )
        self.assertGreaterEqual(
            len(icons),
            4,
            "Run Review and Release Gate guidance should use information icons.",
        )
        for icon in icons:
            self.assertIn('type="button"', icon)
            self.assertIn('aria-expanded="false"', icon)
            self.assertRegex(icon, r'aria-controls="[^"]+"')
            self.assertRegex(icon, r'aria-label="[^"]+"')

        controls = re.findall(r'aria-controls="([^"]+)"', "".join(icons))
        for popover_id in controls:
            self.assertRegex(
                page,
                rf'id="{re.escape(popover_id)}"[^>]*class="[^"]*information-hint-popover[^"]*"[^>]*hidden',
            )
        self.assertIn("function initInformationHints()", page)
        self.assertIn("event.key === 'Escape'", page)
        self.assertIn("event.stopImmediatePropagation();", page)

    def test_release_notes_uses_the_defined_markdown_renderer(self) -> None:
        page = self.page
        self.assertIn("function renderMarkdown(markdown, options = {})", page)
        self.assertRegex(
            page,
            r"releaseNotesContent'\)\.innerHTML\s*=\s*renderMarkdown\(",
            "Release Notes must use the renderer that is defined in this page.",
        )
        self.assertNotRegex(
            page,
            r"releaseNotesContent'\)\.innerHTML\s*=\s*markdownToHtml\(",
        )

    def test_report_history_filters_are_grouped_as_a_toolbar_card(self) -> None:
        page = self.page
        self.assertRegex(
            page,
            r'(?s)<div class="history-tools-card"[^>]*>.*?'
            r'<div class="report-filter"[^>]*>.*?id="reportSearch".*?'
            r'<div class="report-filter-row"[^>]*>.*?id="reportDays".*?'
            r'id="refreshDownloadsBtn".*?</div>.*?'
            r'<div class="history-tabs"',
            "Search, time range, Refresh, and view tabs should form one hierarchy.",
        )
        self.assertIn('aria-label="Search report history"', page)
        self.assertIn('aria-label="Report history period"', page)
        self.assertIn('.history-tools-card {', page)
        self.assertIn('grid-template-columns: minmax(0, 1fr) 76px;', page)

    def test_ai_assistant_messages_have_role_and_content_semantics(self) -> None:
        page = self.page
        self.assertIn('class="chat-message-role"', page)
        self.assertIn('class="chat-message-content"', page)
        self.assertIn("message.className = `chat-message ${role === 'assistant' ? 'assistant' : 'user'}`;", page)
        self.assertRegex(
            page,
            r"role === 'assistant'\s*\?\s*renderMarkdown\(",
            "AI replies should render structured Markdown instead of a plain <br> stream.",
        )
        self.assertIn('.chat-message.assistant', page)
        self.assertIn('.chat-message.user', page)
        self.assertIn('.chat-message-content', page)

    def test_handling_guidance_is_available_from_reply_information_hint(self) -> None:
        page = self.page
        self.assertIn('aria-controls="replyGuidancePopover"', page)
        self.assertIn('id="replyGuidancePopover" class="information-hint-popover"', page)
        self.assertNotIn('id="handlingGuidanceTitle"', page)
        self.assertNotIn('class="handling-guidance thread-guidance-wide"', page)

    def test_reply_and_followup_cards_share_actions_and_visual_weight(self) -> None:
        page = self.page
        self.assertEqual(page.count('class="thread-reply-card"'), 2)
        self.assertRegex(
            page,
            r'<button(?=[^>]*id="sendThreadMessageBtn")(?=[^>]*class="[^"]*thread-card-action[^"]*")[^>]*>',
        )
        self.assertRegex(
            page,
            r'<button(?=[^>]*id="generateFollowupsBtn")(?=[^>]*class="[^"]*thread-card-action[^"]*")[^>]*>',
        )
        self.assertIn(
            '.thread-reply-card { display: grid; grid-template-rows: auto minmax(0, 1fr) auto auto;',
            page,
        )
        self.assertIn('min-height: 140px; max-height: 320px;', page)
        self.assertIn(
            '.thread-reply-actions button { min-width: min(148px, 100%); max-width: 100%; }',
            page,
        )

    def test_followup_draft_can_be_copied_and_reply_forms_fill_the_column(self) -> None:
        page = self.page
        self.assertIn('id="copyFollowupDraftBtn"', page)
        self.assertIn('<svg viewBox="0 0 24 24"', page)
        self.assertIn("async function copyFollowupDraft()", page)
        self.assertIn("navigator.clipboard.writeText(value)", page)
        self.assertIn("button.classList.add('copied');", page)
        self.assertIn("resize: vertical;", page)
        self.assertIn("overflow-y: auto;", page)
        self.assertNotIn("<h4>Reply message</h4>", page)
        self.assertNotIn("<h4>Follow-up 整理</h4>", page)

    def test_report_history_controls_share_one_right_edge(self) -> None:
        page = self.page
        self.assertRegex(
            page,
            r"(?s)\.history-tools-card\s*\{[^}]*width:\s*100%;[^}]*box-sizing:\s*border-box;",
        )
        self.assertRegex(
            page,
            r"(?s)\.history-tabs\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);"
            r"[^}]*width:\s*100%;",
        )

    def test_problem_list_findings_are_accessibly_collapsible(self) -> None:
        page = self.page
        self.assertIn("function enhanceReportFindingCollapse(container)", page)
        self.assertIn("details.className = 'report-finding-details';", page)
        self.assertIn("summary.className = 'report-finding-summary';", page)
        self.assertIn("enhanceReportFindingCollapse(container);", page)

    def test_issue_review_header_groups_identity_actions_and_metrics(self) -> None:
        page = self.page
        self.assertRegex(
            page,
            r'(?s)<header class="issue-review-header">.*?'
            r'<div class="issue-review-primary">.*?'
            r'<div class="issue-review-identity">.*?'
            r'<div class="issue-review-controls">.*?'
            r'<div class="issue-review-actions">.*?issueRescanBtn.*?issuePassBtn.*?'
            r'<div class="issue-review-context">.*?issueReviewCycleSelect.*?'
            r'issue-readiness.*?</header>.*?metricsMarkup',
            "Issue identity, status/actions, and metrics need a stable visual hierarchy.",
        )
        self.assertIn('.issue-review-header {', page)
        self.assertIn('.issue-review-primary {', page)
        self.assertIn('.issue-review-context {', page)
        self.assertIn('.issue-review-actions {', page)
        self.assertIn('data-jump-severity="medium"', page)
        self.assertIn('class="metric-card metric-summary-card"', page)
        self.assertIn('class="cycle-empty-state"', page)
        self.assertIn('No Review Run in this Cycle', page)
        self.assertRegex(
            page,
            r'(?s)@media \(max-width: \d+px\).*?\.issue-review-primary, \.issue-review-context\s*\{[^}]*grid-template-columns:\s*1fr',
        )

    def test_release_notes_has_bounded_scrollable_accessible_dialog(self) -> None:
        page = self.page
        self.assertIn('class="release-notes-dialog"', page)
        self.assertIn('class="markdown-preview release-notes-content" tabindex="0"', page)
        self.assertIn('height: clamp(420px, 72dvh, 720px);', page)
        self.assertIn('overflow-y: auto;', page)
        self.assertIn('function closeReleaseNotes()', page)
        self.assertIn('function handleReleaseNotesKeydown(event)', page)
        self.assertIn("if (event.key === 'Escape')", page)


if __name__ == "__main__":
    unittest.main()
