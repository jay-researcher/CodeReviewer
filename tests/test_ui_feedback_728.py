from __future__ import annotations

import inspect
import json
import re
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

from code_reviewer import web_app


class _ConfigurationStoreStub:
    def revision(self, _base: dict[str, object]) -> str:
        return "revision-728"

    def list_backups(self) -> list[dict[str, object]]:
        return []


class Feedback728UiAcceptanceTests(unittest.TestCase):
    opener = build_opener(ProxyHandler({}))

    def _serve(self, snapshots: list[bool]):
        users = {
            "manager.one": {
                "username": "manager.one",
                "role": "manager",
                "active": True,
                "password_hash": "unused",
            },
            "developer.one": {
                "username": "developer.one",
                "role": "developer",
                "active": True,
                "responsible": ["wen.yi"],
                "password_hash": "unused",
            },
        }

        def health(*, details: bool = False) -> dict[str, object]:
            snapshots.append(details)
            checks = [
                {
                    "name": "External provider" if details else "Application data",
                    "ok": True,
                    "required": not details,
                    "message": "External connectivity is available." if details else "Application data storage is available.",
                }
            ]
            return {
                "ok": True,
                "status": "healthy",
                "version": "7.2.9",
                "updated_at": "2026-07-18T18:30:25",
                "checks": checks,
            }

        patchers = [
            patch.object(web_app, "_load_web_users", return_value=users),
            patch.object(web_app, "web_health_snapshot", side_effect=health),
            patch.object(
                web_app,
                "web_configuration_payload",
                return_value={
                    "ok": True,
                    "revision": "revision-728",
                    "app_fields": [],
                    "projects": [],
                    "backups": [],
                },
            ),
        ]
        for patcher in patchers:
            patcher.start()
        web_app.WEB_SESSIONS.clear()
        now = int(time.time()) + 300
        web_app.WEB_SESSIONS.update(
            {
                "manager-session": {"username": "manager.one", "expires_at": now},
                "developer-session": {"username": "developer.one", "expires_at": now},
            }
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.CodeReviewerHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, patchers

    @staticmethod
    def _get(base: str, path: str, cookie: str = "") -> tuple[int, dict[str, object]]:
        request = Request(f"{base}{path}", headers={"Cookie": cookie} if cookie else {})
        try:
            with Feedback728UiAcceptanceTests.opener.open(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_health_summary_is_public_but_details_require_authentication(self) -> None:
        calls: list[bool] = []
        server, thread, patchers = self._serve(calls)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, public = self._get(base, "/api/health")
            self.assertEqual(200, status)
            self.assertEqual("healthy", public["status"])
            self.assertNotIn("gitlab.tx-tech.com", json.dumps(public).lower())

            status, denied = self._get(base, "/api/health-details")
            self.assertEqual(401, status)
            self.assertEqual("Authentication required", denied["error"])

            status, details = self._get(
                base,
                "/api/health-details",
                f"{web_app.WEB_SESSION_COOKIE}=manager-session",
            )
            self.assertEqual(200, status)
            self.assertEqual("External provider", details["checks"][0]["name"])
            self.assertEqual([False, True], calls)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            web_app.WEB_SESSIONS.clear()
            for patcher in reversed(patchers):
                patcher.stop()

    def test_configuration_route_and_ui_are_manager_only(self) -> None:
        calls: list[bool] = []
        server, thread, patchers = self._serve(calls)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, payload = self._get(
                base,
                "/api/admin/configuration",
                f"{web_app.WEB_SESSION_COOKIE}=manager-session",
            )
            self.assertEqual(200, status)
            self.assertEqual("revision-728", payload["revision"])

            status, denied = self._get(
                base,
                "/api/admin/configuration",
                f"{web_app.WEB_SESSION_COOKIE}=developer-session",
            )
            self.assertEqual(403, status)
            self.assertIn("Manager", denied["error"])

            with patch.object(web_app, "list_gitlab_projects_for_user", return_value=[]):
                manager_page = web_app.render_index("manager.one")
                developer_page = web_app.render_index("developer.one")
            self.assertIn('id="configurationBtn"', manager_page)
            self.assertIn('id="configurationModal"', manager_page)
            self.assertNotIn('id="configurationBtn"', developer_page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            web_app.WEB_SESSIONS.clear()
            for patcher in reversed(patchers):
                patcher.stop()

    def test_configuration_supports_atomic_project_add_and_delete_contract(self) -> None:
        page = web_app.render_index("admin")
        self.assertIn('id="addConfigurationProjectBtn"', page)
        self.assertIn("async function addConfigurationProject()", page)
        self.assertIn("async function deleteConfigurationProject(id)", page)
        self.assertIn("/api/admin/configuration/project", page)
        self.assertIn("Backups &amp; restore", page)

        captured: list[dict[str, object]] = []

        class Store:
            def save_overrides(self, _base, overrides, **_kwargs):
                captured.append(overrides)
                return {
                    "revision": "after",
                    "previous_revision": "before",
                    "backup": "backup.json",
                    "changed_paths": ["dps11-repository/new-module"],
                }

        effective = {"app": {}, "dps11-repository": {}}
        response = {"ok": True, "revision": "after", "app_fields": [], "projects": [], "backups": []}
        with (
            patch.object(web_app, "_web_user_role", return_value="manager"),
            patch.object(web_app, "_web_user_record", return_value={"role": "manager", "active": True}),
            patch.object(web_app, "load_effective_config_payload", return_value=effective),
            patch.object(web_app, "load_base_config_payload", return_value=effective),
            patch.object(web_app, "load_web_config_overrides", return_value={}),
            patch.object(web_app, "EffectiveConfigStore", return_value=Store()),
            patch.object(web_app, "clear_config_cache"),
            patch.object(web_app, "web_configuration_payload", return_value=response),
        ):
            result = web_app.mutate_web_configuration_project(
                "manager.one",
                {
                    "action": "upsert",
                    "path": ["dps11-repository", "new-module"],
                    "values": {
                        "repository_url": "https://gitlab.example.test/team/new-module.git",
                        "application": "DPS",
                        "responsible": "kevin.tan",
                        "type": "backend",
                    },
                    "revision": "before",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual("DPS", captured[0]["dps11-repository"]["new-module"]["application"])

    def test_configuration_payload_filters_secrets_users_and_local_paths(self) -> None:
        base = {
            "app": {
                "report": {"history_days": 14},
                "llm": {"api_key": "base-secret", "model": "gpt-test"},
                "web": {"users": {"root": {"password": "base-password"}}},
            }
        }
        effective = {
            "app": {
                "report": {"history_days": 14},
                "llm": {"api_key": "effective-secret", "model": "gpt-test"},
                "web": {"users": {"root": {"password": "effective-password"}}},
            },
            "dps11-repository": {
                "type": "backend",
                "module": {
                    "repository_url": "https://gitlab.example.test/team/module.git",
                    "responsible": "kevin.tan",
                    "branch": "main",
                    "local_working_copy": "D:/private/source",
                    "api_token": "project-secret",
                },
            },
        }
        with (
            patch.object(web_app, "load_base_config_payload", return_value=base),
            patch.object(web_app, "load_effective_config_payload", return_value=effective),
            patch.object(web_app, "EffectiveConfigStore", return_value=_ConfigurationStoreStub()),
        ):
            payload = web_app.web_configuration_payload()

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("history_days", serialized)
        self.assertIn("gpt-test", serialized)
        self.assertIn("repository_url", serialized)
        for forbidden in (
            "base-secret",
            "effective-secret",
            "base-password",
            "effective-password",
            "project-secret",
            "api_key",
            "password",
            "local_working_copy",
            "D:/private/source",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_release_notes_exposes_and_renders_a_stable_updated_at(self) -> None:
        page = web_app.render_index("admin")
        route = inspect.getsource(web_app.CodeReviewerHandler.do_GET)
        self.assertIn('id="releaseNotesUpdated" class="meta" datetime=""', page)
        self.assertIn("data.updated_at", page)
        self.assertIn("Last updated ${updated.replace('T', ' ')} (Asia/Shanghai)", page)
        self.assertIn('"updated_at": updated_at', route)
        self.assertIn("notes_path.stat().st_mtime", route)

    def test_required_markers_remain_inline_after_their_labels(self) -> None:
        login = web_app.render_login()
        page = web_app.render_index("admin")
        self.assertRegex(
            login,
            r'Username\s+<span class="required-mark" aria-hidden="true">\*</span>',
        )
        for label in ("Current password", "New password", "Confirm new password"):
            self.assertRegex(
                page,
                rf'<span class="field-label">{re.escape(label)}\s+'
                rf'<span class="required-mark" aria-hidden="true">\*</span></span><input',
            )
        self.assertRegex(
            page,
            r"<span>Message\s+<span class=\"required-mark\" aria-hidden=\"true\">\*</span></span>\s*<textarea id=\"threadMessage\"",
        )
        self.assertRegex(
            page,
            r'<label><span class="field-label">Message\s+'
            r'<span class="required-mark" aria-hidden="true">\*</span></span>\s*'
            r'<textarea id="aiChatInput"',
        )

    def test_review_communication_has_issue_identity_and_handling_metrics(self) -> None:
        page = web_app.render_index("admin")
        for marker in (
            'id="threadContext"',
            'id="threadIssueTitle"',
            'id="threadContextMetrics"',
            "Critical/High remaining",
            "Communication records",
            "Handling",
        ):
            self.assertIn(marker, page)
        self.assertIn("$('threadIssueTitle').textContent = issueTitle;", page)
        self.assertIn("$('threadContext').hidden = !issueTitle;", page)

    def test_issue_history_overview_renders_all_applications_and_zero_state(self) -> None:
        page = web_app.render_index("admin")
        for application in ("WVAdmin", "iTrade Client", "Services Terminal", "DPS"):
            self.assertIn(f"'{application}'", page)
        for label in (
            "Reports",
            "Without report",
            "Generating",
            "Handling",
            "Ready for Pass",
            "Review Pass",
            "Failed",
            "Remaining",
        ):
            self.assertIn(label, page)
        self.assertIn("appTotal ? Math.round(reviewPass * 100 / appTotal) : null", page)
        self.assertIn("percent === null ? 'N/A'", page)
        self.assertIn("application !== 'Unmapped'", page)
        self.assertIn("issueReviewApplicationFilter = button.dataset.application", page)


class Feedback728ResponsibleAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.users_file = root / "web_users.json"
        self.audit_file = root / "web_user_audit.jsonl"
        self.users_file.write_text(
            json.dumps(
                {
                    "users": {
                        "manager.one": {
                            "username": "manager.one",
                            "password_hash": web_app.hash_web_password("Manager!Password24"),
                            "role": "manager",
                            "active": True,
                            "responsible": [],
                            "revision": 1,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.patchers = [
            patch.object(web_app, "WEB_USERS_FILE", self.users_file),
            patch.object(web_app, "WEB_USER_AUDIT_FILE", self.audit_file),
            patch.object(web_app, "_configured_web_user_profiles", return_value={}),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_manager_is_global_with_empty_mapping(self) -> None:
        result = web_app.save_managed_web_user(
            "manager.one",
            {
                "username": "global.manager",
                "role": "manager",
                "active": True,
                "responsibles": [],
            },
        )
        self.assertEqual([], result["user"]["responsibles"])
        self.assertTrue(web_app._web_user_permissions("global.manager")["view_all"])

    def test_developer_empty_responsible_mapping_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "require at least one Responsible"):
            web_app.save_managed_web_user(
                "manager.one",
                {
                    "username": "empty.developer",
                    "role": "developer",
                    "active": True,
                    "responsibles": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
