from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from code_reviewer import web_app


class ReviewCommunicationPendingJiraUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = web_app.render_index("admin")

    def test_discussion_follows_history_draft_reply_visual_and_keyboard_order(self) -> None:
        page = self.page
        discussion = page[page.index('id="discussionPane"'):page.index('id="chatPane"')]
        history_at = discussion.index('id="threadMessages"')
        draft_at = discussion.index('id="followupDraft"')
        reply_at = discussion.index('class="thread-column thread-reply-column"')
        self.assertLess(history_at, draft_at)
        self.assertLess(draft_at, reply_at)
        self.assertNotIn('class="handling-guidance thread-guidance-wide"', discussion)
        self.assertIn('id="replyGuidancePopover"', discussion)
        self.assertIn(
            ".thread-layout > .thread-followup-column { grid-column: 2; grid-row: 1; }",
            page,
        )
        self.assertIn(
            ".thread-layout > .thread-reply-column { grid-column: 3; grid-row: 1; }",
            page,
        )
        self.assertIn("grid-template-rows: minmax(0, 1fr);", page)

    def test_reply_cards_share_width_and_actions_cannot_overflow(self) -> None:
        page = self.page
        self.assertEqual(page.count('class="thread-reply-card"'), 2)
        self.assertIn(
            ".thread-reply-actions button { min-width: min(148px, 100%); max-width: 100%; }",
            page,
        )
        self.assertIn('class="thread-card-action" id="sendThreadMessageBtn"', page)
        self.assertIn('class="thread-card-action" id="generateFollowupsBtn"', page)
        self.assertIn("resize: vertical;", page)
        self.assertIn("overflow-y: auto;", page)

    def test_ai_assist_and_open_issue_review_use_consistent_shell_actions(self) -> None:
        page = self.page
        self.assertIn('id="chatPane" class="thread-pane tools"', page)
        self.assertIn('class="chat-messages"', page)
        self.assertIn('class="thread-form ai-ask-card"', page)
        self.assertIn('class="thread-card-action" id="sendAiChatBtn"', page)
        self.assertIn(
            'id="openIssueReviewFromReportBtn" class="thread-tab thread-open-review"',
            page,
        )
        self.assertIn(".thread-open-review { margin-left: auto;", page)

    def test_pending_jira_describes_compatibility_without_claiming_native_atlaskit(self) -> None:
        page = self.page
        self.assertIn("ADF-compatible Issue Description draft", page)
        self.assertIn("Structured Jira description", page)
        self.assertIn("optional progressive enhancement", page)
        self.assertIn('id="adfEditorEngine" class="adf-editor-engine">Built-in ADF editor</span>', page)
        self.assertNotIn("ADF-native Issue Description", page)

    def test_pending_jira_uses_atlaskit_only_when_optional_bundle_is_available(self) -> None:
        page = self.page
        source = inspect.getsource(web_app.render_index)
        self.assertIn(
            """adf_asset = '<script src="/assets/adf-editor.js"></script>' if (WEB_STATIC_DIR / "adf-editor.js").is_file() else """"",
            source,
        )
        self.assertIn("if (mountAtlaskitAdf('edit')) {", page)
        self.assertIn("if (!window.CodeReviewerADF || !host) return false;", page)
        self.assertIn("Atlaskit enhanced", page)
        self.assertIn("Built-in ADF editor", page)
        self.assertIn("function unmountAtlaskitAdf()", page)
        self.assertIn("using the built-in editor", page)

    def test_optional_island_uses_official_atlaskit_editor_and_renderer_packages(self) -> None:
        root = Path(web_app.ROOT_DIR)
        source = (root / "frontend" / "adf-editor" / "src" / "main.tsx").read_text(encoding="utf-8")
        readme = (root / "frontend" / "adf-editor" / "README.md").read_text(encoding="utf-8")
        self.assertIn("from '@atlaskit/editor-core'", source)
        self.assertIn("from '@atlaskit/renderer'", source)
        self.assertIn("allowTables={{ advanced: true }}", source)
        self.assertIn("allowExpand={{ allowInsertion: true }}", source)
        self.assertIn("optional build, not a runtime requirement", readme)
        self.assertIn("built-in schema-native ADF editor/preview remains available", readme)

    def test_pending_jira_keeps_validation_and_duplicate_submit_protection(self) -> None:
        page = self.page
        self.assertIn("Issue Summary is required.", page)
        self.assertIn("Issue Description is required. Add at least one text block.", page)
        self.assertIn("singleFlight('save-draft', $('saveDraftBtn'), saveCurrentDraft)", page)
        self.assertIn('aria-required="true"', page)


if __name__ == "__main__":
    unittest.main()
