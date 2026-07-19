from __future__ import annotations

import threading
import time
import unittest
from email.message import Message
from unittest.mock import patch

from code_reviewer import web_app


class WebEpicUiTests(unittest.TestCase):
    def tearDown(self) -> None:
        with web_app.WEB_REVIEW_JOBS_LOCK:
            web_app.WEB_REVIEW_JOBS.clear()

    def test_report_preview_has_no_formal_handling_or_teams_delivery(self) -> None:
        page = web_app.render_index("admin")
        self.assertIn("Open Issue Review", page)
        self.assertIn("AI Assist", page)
        self.assertNotIn('data-thread-tab="handling"', page)
        self.assertNotIn('data-thread-tab="teams"', page)
        self.assertNotIn("Teams Delivery", page)
        self.assertNotIn("/api/report-thread/handling", page)
        self.assertNotIn("/api/report-thread/teams-send", page)

    def test_epic_controls_and_accessibility_are_rendered(self) -> None:
        page = web_app.render_index("admin")
        for expected in (
            "Sprint Overview", "Release Notes", "Jump latest", "Maximize",
            "View full details", "metric-bar", "Idempotency-Key", "singleFlight",
            'id="sprintOptions"', "/api/sprint-preflight",
        ):
            self.assertIn(expected, page)
        self.assertIn('aria-invalid', page)
        self.assertIn('class="required-mark"', page)

    def test_explicit_cross_origin_post_is_rejected(self) -> None:
        handler = object.__new__(web_app.CodeReviewerHandler)
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Origin"] = "https://evil.example"
        self.assertFalse(handler._request_origin_allowed())
        handler.headers.replace_header("Origin", "http://127.0.0.1:8765")
        self.assertTrue(handler._request_origin_allowed())

    def test_release_gate_mr_url_validation_accepts_wrapped_gitlab_links(self) -> None:
        value = "  https://gitlab.tx-tech.com/web-sv-build/webfe/itrade-client/\n-/merge_requests/6  "
        self.assertEqual(
            web_app._normalize_merge_request_url(value),
            "https://gitlab.tx-tech.com/web-sv-build/webfe/itrade-client/-/merge_requests/6",
        )
        self.assertTrue(web_app._is_valid_merge_request_url(value))
        self.assertTrue(web_app._is_valid_merge_request_url(value + "?diff_id=12"))
        self.assertFalse(web_app._is_valid_merge_request_url("https://gitlab.tx-tech.com/group/project"))
        self.assertFalse(web_app._is_valid_merge_request_url("javascript:alert(1)"))

    def test_review_jobs_execute_serially_and_queued_job_can_stop(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def fake_review(payload, progress):
            calls.append(str(payload.get("jira_key")))
            first_started.set()
            release_first.wait(3)
            return {"conclusion": "ok"}

        with patch("code_reviewer.web_app.run_review_from_payload", side_effect=fake_review):
            first = web_app.create_review_job({"jira_key": "ECHNL-1"}, "admin")
            self.assertTrue(first_started.wait(2))
            second = web_app.create_review_job({"jira_key": "ECHNL-2"}, "admin")
            time.sleep(0.15)
            self.assertEqual(web_app.review_job_snapshot(second["id"], "admin")["status"], "queued")
            self.assertTrue(web_app.stop_review_job(second["id"], "admin")["ok"])
            release_first.set()
            deadline = time.time() + 3
            while time.time() < deadline:
                if web_app.review_job_snapshot(second["id"], "admin")["status"] == "canceled":
                    break
                time.sleep(0.05)
            self.assertEqual(calls, ["ECHNL-1"])
            self.assertEqual(web_app.review_job_snapshot(second["id"], "admin")["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
