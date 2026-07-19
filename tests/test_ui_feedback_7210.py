from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

from code_reviewer import web_app


class Feedback7210AcceptanceTests(unittest.TestCase):
    opener = build_opener(ProxyHandler({}))

    def test_feedback_contract_is_present_in_rendered_application(self) -> None:
        page = web_app.render_index("admin")

        self.assertIn("review-preflight-card", page)
        self.assertIn("Checking existing Code Review reports", page)
        self.assertIn("Responsible ·", page)
        self.assertIn("Global scope", page)
        self.assertIn("release-notes-error", page)
        self.assertIn("Retry", page)
        self.assertIn("configuration-section", page)
        self.assertIn("View sprint issues", page)
        self.assertNotIn("View all Sprint Issues", page)
        self.assertIn("reportFindingPreviewText", page)
        self.assertIn("Problem ·", page)
        self.assertIn("Suggestion ·", page)
        self.assertIn("report-finding-more", page)
        self.assertIn("Deferred MR source:", page)
        self.assertIn("<strong>Reply message</strong>", page)
        self.assertIn("<strong>Follow-up</strong>", page)

    def test_public_release_notes_endpoint_does_not_depend_on_system_tzdata(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            docs = root / "7.x-docs"
            docs.mkdir()
            (docs / "public release notes.md").write_text(
                "# CodeReviewer Release Notes\n\n- Endpoint works.",
                encoding="utf-8",
            )
            users = {
                "manager.one": {
                    "username": "manager.one",
                    "role": "manager",
                    "active": True,
                    "password_hash": "unused",
                }
            }
            session = "release-notes-session"
            web_app.WEB_SESSIONS[session] = {
                "username": "manager.one",
                "expires_at": int(time.time()) + 120,
            }
            with patch.object(web_app, "ROOT_DIR", root), patch.object(
                web_app, "_load_web_users", return_value=users
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.CodeReviewerHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/public-release-notes",
                        headers={"Cookie": f"{web_app.WEB_SESSION_COOKIE}={session}"},
                    )
                    with self.opener.open(request, timeout=3) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["source_available"])
                    self.assertIn("Endpoint works", payload["markdown"])
                    self.assertRegex(payload["updated_at"], r"\+08:00$")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)
                    web_app.WEB_SESSIONS.pop(session, None)

    def test_release_notes_missing_source_returns_structured_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            users = {
                "manager.one": {
                    "username": "manager.one",
                    "role": "manager",
                    "active": True,
                    "password_hash": "unused",
                }
            }
            session = "release-notes-fallback-session"
            web_app.WEB_SESSIONS[session] = {
                "username": "manager.one",
                "expires_at": int(time.time()) + 120,
            }
            with patch.object(web_app, "ROOT_DIR", root), patch.object(
                web_app, "_load_web_users", return_value=users
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.CodeReviewerHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/public-release-notes",
                        headers={"Cookie": f"{web_app.WEB_SESSION_COOKIE}={session}"},
                    )
                    with self.opener.open(request, timeout=3) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(payload["ok"])
                    self.assertFalse(payload["source_available"])
                    self.assertIn("temporarily unavailable", payload["markdown"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)
                    web_app.WEB_SESSIONS.pop(session, None)


if __name__ == "__main__":
    unittest.main()
