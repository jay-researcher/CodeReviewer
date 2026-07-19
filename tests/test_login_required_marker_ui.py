from __future__ import annotations

import re
import unittest

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


if __name__ == "__main__":
    unittest.main()
