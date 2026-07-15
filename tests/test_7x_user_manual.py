from __future__ import annotations

import unittest
from pathlib import Path

from code_reviewer import __version__


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "7.x-docs" / "CodeReviewer User Mannual.md"


class UserManual7xTests(unittest.TestCase):
    def test_manual_tracks_current_7x_version(self) -> None:
        self.assertTrue(__version__.startswith("7."), "This guard applies to the 7.x release line")
        text = MANUAL.read_text(encoding="utf-8")

        self.assertIn(f"当前适用版本：{__version__}", text)
        self.assertIn(f"### {__version__} —", text)

    def test_manual_keeps_required_user_guidance(self) -> None:
        text = MANUAL.read_text(encoding="utf-8")
        required_topics = (
            "角色与权限",
            "Finding 处理",
            "Re-scan",
            "Manual Pass",
            "Pending Jira",
            "ADF",
            "7.x 版本更新记录",
        )

        for topic in required_topics:
            with self.subTest(topic=topic):
                self.assertIn(topic, text)


if __name__ == "__main__":
    unittest.main()
