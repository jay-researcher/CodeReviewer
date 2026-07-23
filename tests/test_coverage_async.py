from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

from code_reviewer import web_app


class CoverageAsyncJobTests(unittest.TestCase):
    def setUp(self) -> None:
        with web_app.WEB_COVERAGE_JOBS_LOCK:
            web_app.WEB_COVERAGE_JOBS.clear()

    def tearDown(self) -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            with web_app.WEB_COVERAGE_JOBS_LOCK:
                active = [
                    job
                    for job in web_app.WEB_COVERAGE_JOBS.values()
                    if job.get("status") in {"queued", "running"}
                ]
            if not active:
                break
            time.sleep(0.01)
        with web_app.WEB_COVERAGE_JOBS_LOCK:
            web_app.WEB_COVERAGE_JOBS.clear()

    @staticmethod
    def _wait_for_status(job_id: str, expected: set[str], timeout: float = 2) -> dict[str, object]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = web_app.coverage_job_snapshot(job_id, "manager.user")
            if snapshot and snapshot.get("status") in expected:
                return snapshot
            time.sleep(0.01)
        raise AssertionError(f"Coverage job {job_id} did not reach {expected}.")

    def test_background_job_completes_and_preserves_result(self) -> None:
        def build(**kwargs):
            kwargs["progress"](
                {
                    "event": "discovery-issue",
                    "message": "Discovering ECHNL-1001",
                    "index": 1,
                    "total": 1,
                    "jira_key": "ECHNL-1001",
                }
            )
            return {"issues": [{"jira_key": "ECHNL-1001"}], "counts": {}}

        with (
            patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": True}),
            patch.object(web_app, "build_review_coverage", side_effect=build),
        ):
            job, reused = web_app.create_coverage_job(
                {"sprint": "e-Channel Sprint 1.4.76"},
                "manager.user",
            )
            snapshot = self._wait_for_status(str(job["id"]), {"done"})

        self.assertFalse(reused)
        self.assertEqual(snapshot["progress"]["percent"], 100)
        self.assertEqual(snapshot["result"]["issues"][0]["jira_key"], "ECHNL-1001")
        self.assertEqual(
            web_app.latest_coverage_job_snapshot("manager.user")["id"],
            snapshot["id"],
        )
        self.assertIsNone(web_app.coverage_job_snapshot(str(job["id"]), "another.user"))

    def test_duplicate_active_scope_reuses_single_job(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def build(**kwargs):
            started.set()
            release.wait(timeout=1)
            return {"issues": [], "counts": {}}

        with (
            patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": True}),
            patch.object(web_app, "build_review_coverage", side_effect=build),
        ):
            first, first_reused = web_app.create_coverage_job(
                {"sprint": "e-Channel Sprint 1.4.76"},
                "manager.user",
            )
            self.assertTrue(started.wait(timeout=1))
            second, second_reused = web_app.create_coverage_job(
                {"sprint": " e-Channel Sprint 1.4.76 "},
                "manager.user",
            )
            release.set()
            self._wait_for_status(str(first["id"]), {"done"})

        self.assertFalse(first_reused)
        self.assertTrue(second_reused)
        self.assertEqual(first["id"], second["id"])

    def test_recent_completed_scope_is_cached(self) -> None:
        with (
            patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": True}),
            patch.object(web_app, "build_review_coverage", return_value={"issues": [], "counts": {}}),
        ):
            first, _ = web_app.create_coverage_job({"jira": "ECHNL-1001"}, "manager.user")
            self._wait_for_status(str(first["id"]), {"done"})
            second, reused = web_app.create_coverage_job({"jira": "ECHNL-1001"}, "manager.user")

        self.assertTrue(reused)
        self.assertEqual(first["id"], second["id"])

    def test_explicit_scan_bypasses_recent_completed_cache(self) -> None:
        with (
            patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": True}),
            patch.object(web_app, "build_review_coverage", return_value={"issues": [], "counts": {}}),
        ):
            first, _ = web_app.create_coverage_job({"jira": "ECHNL-1001"}, "manager.user")
            self._wait_for_status(str(first["id"]), {"done"})
            second, reused = web_app.create_coverage_job(
                {"jira": "ECHNL-1001", "force_refresh": True}, "manager.user"
            )
            self._wait_for_status(str(second["id"]), {"done"})

        self.assertFalse(reused)
        self.assertNotEqual(first["id"], second["id"])

    def test_sprint_scope_requires_coverage_permission(self) -> None:
        with patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": False}):
            with self.assertRaises(PermissionError):
                web_app.create_coverage_job({"sprint": "10068"}, "developer.user")

    def test_http_api_returns_job_immediately_and_supports_polling(self) -> None:
        users = {
            "manager.user": {
                "username": "manager.user",
                "role": "manager",
                "active": True,
                "password_hash": "unused",
            }
        }
        web_app.WEB_SESSIONS["coverage-test-session"] = {
            "username": "manager.user",
            "expires_at": int(time.time()) + 300,
        }
        opener = build_opener(ProxyHandler({}))
        with (
            patch.object(web_app, "_load_web_users", return_value=users),
            patch.object(web_app, "_web_user_permissions", return_value={"scan_coverage": True}),
            patch.object(web_app, "build_review_coverage", return_value={"issues": [], "counts": {}}),
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.CodeReviewerHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                request = Request(
                    f"{base}/api/review-coverage-jobs",
                    data=json.dumps({"sprint": "10068"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Cookie": f"{web_app.WEB_SESSION_COOKIE}=coverage-test-session",
                    },
                    method="POST",
                )
                started = time.perf_counter()
                with opener.open(request, timeout=3) as response:
                    created = json.loads(response.read().decode("utf-8"))
                    self.assertIn(response.status, {200, 202})
                self.assertLess(time.perf_counter() - started, 1)
                job_id = created["job"]["id"]
                poll = Request(
                    f"{base}/api/review-coverage-jobs/{job_id}",
                    headers={"Cookie": f"{web_app.WEB_SESSION_COOKIE}=coverage-test-session"},
                )
                with opener.open(poll, timeout=3) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertTrue(snapshot["ok"])
                self.assertEqual(job_id, snapshot["job"]["id"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                web_app.WEB_SESSIONS.pop("coverage-test-session", None)

    def test_rendered_ui_uses_background_job_and_long_scan_countdown(self) -> None:
        page = web_app.render_index("manager.user")
        self.assertIn("/api/review-coverage-jobs", page)
        self.assertIn("force_refresh: true", page)
        self.assertIn("COVERAGE_LONG_SCAN_SECONDS = 30", page)
        self.assertIn("Timeout countdown", page)
        self.assertIn("Closing this window will not stop it.", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("Coverage scan timed out after 60 seconds", page)
        self.assertNotIn("coverageController.abort()", page)


if __name__ == "__main__":
    unittest.main()
