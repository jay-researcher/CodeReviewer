from __future__ import annotations

import re
import inspect
import unittest
from pathlib import Path

from code_reviewer import web_app
from code_reviewer.web_app import render_login


class LoginRequiredMarkerUiTests(unittest.TestCase):
    def test_required_markers_stay_inline_with_all_field_labels(self) -> None:
        page = render_login()

        self.assertEqual(page.count('class="field-label"'), 3)
        for label in ("Username", "Password", "Robot Check"):
            self.assertRegex(
                page,
                rf'<span class="field-label">{re.escape(label)} '
                r'<span class="required-mark" aria-hidden="true">\*</span></span>',
            )
        self.assertIn("display: inline-flex;", page)
        self.assertIn("align-items: baseline;", page)

    def test_robot_challenge_keeps_prompt_and_reset_in_one_row(self) -> None:
        page = render_login()

        self.assertIn('class="challenge-line"', page)
        self.assertIn('class="challenge-prompt"', page)
        self.assertIn('id="refreshChallengeBtn"', page)
        self.assertIn("flex-wrap: wrap;", page)

    def test_login_uses_the_code_review_background_and_blue_glass_card(self) -> None:
        page = render_login()

        self.assertIn('url("/assets/login-code-review-bg.png")', page)
        self.assertIn('class="brand-mark"', page)
        self.assertIn('class="login-kicker"', page)
        self.assertIn("backdrop-filter: blur(18px)", page)
        self.assertIn("--accent-soft: #eaf4ff;", page)
        self.assertIn("width: calc(100vw - 32px); max-width: 448px;", page)
        self.assertTrue((Path(web_app.WEB_STATIC_DIR) / "login-code-review-bg.png").is_file())

    def test_login_background_is_available_before_authentication(self) -> None:
        route = inspect.getsource(web_app.CodeReviewerHandler.do_GET)
        asset_position = route.index('parsed.path == "/assets/login-code-review-bg.png"')
        auth_position = route.index("user = self._current_user()")

        self.assertLess(asset_position, auth_position)
        self.assertIn('self.send_header("Content-Type", "image/png")', route)


if __name__ == "__main__":
    unittest.main()
