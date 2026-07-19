from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer import web_app


class UserManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.users_file = root / "web_users.json"
        self.audit_file = root / "web_user_audit.jsonl"
        self.patches = [
            patch.object(web_app, "WEB_USERS_FILE", self.users_file),
            patch.object(web_app, "WEB_USER_AUDIT_FILE", self.audit_file),
            patch.object(web_app, "_configured_web_user_profiles", return_value={}),
        ]
        for item in self.patches:
            item.start()
        web_app.WEB_SESSIONS.clear()
        web_app.WEB_USER_IDEMPOTENCY.clear()
        self._seed(
            {
                "manager.one": {
                    "username": "manager.one",
                    "password_hash": web_app.hash_web_password("Manager!Password24"),
                    "role": "manager",
                    "active": True,
                    "responsible": ["manager.one"],
                    "revision": 1,
                },
                "developer.one": {
                    "username": "developer.one",
                    "password_hash": web_app.hash_web_password("Developer!Pass24"),
                    "role": "developer",
                    "active": True,
                    "responsible": ["team.one"],
                    "revision": 1,
                },
            }
        )

    def tearDown(self) -> None:
        web_app.WEB_SESSIONS.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def _seed(self, users: dict[str, dict[str, object]]) -> None:
        self.users_file.write_text(json.dumps({"users": users}), encoding="utf-8")

    def _stored(self) -> dict[str, dict[str, object]]:
        return json.loads(self.users_file.read_text(encoding="utf-8"))["users"]

    def test_list_is_redacted(self) -> None:
        result = web_app.list_managed_web_users()
        self.assertTrue({"manager.one", "developer.one"}.issubset({str(item["username"]) for item in result}))
        serialized = json.dumps(result)
        self.assertNotIn("password_hash", serialized)
        self.assertNotIn("Manager!Password24", serialized)

    def test_manager_can_create_user_and_password_is_returned_once(self) -> None:
        result = web_app.save_managed_web_user(
            "manager.one",
            {
                "username": "new.developer",
                "role": "developer",
                "active": True,
                "responsibles": ["wen.yi", "kevin.tan"],
            },
        )
        temporary = str(result["temporary_password"])
        self.assertGreaterEqual(len(temporary), 14)
        record = self._stored()["new.developer"]
        self.assertNotIn("password", record)
        self.assertNotEqual(temporary, record["password_hash"])
        self.assertTrue(web_app.verify_web_password(temporary, str(record["password_hash"])))
        self.assertEqual(["wen.yi", "kevin.tan"], record["responsible"])
        self.assertTrue(record["must_change_password"])

    def test_non_manager_cannot_manage_users(self) -> None:
        with self.assertRaises(PermissionError):
            web_app.save_managed_web_user(
                "developer.one",
                {"username": "blocked.user", "role": "developer", "active": True},
            )
        with self.assertRaises(PermissionError):
            web_app.reset_managed_web_user_password("developer.one", "manager.one")

    def test_reset_password_revokes_sessions_and_never_audits_secret(self) -> None:
        token = "old-session"
        web_app.WEB_SESSIONS[token] = {
            "username": "developer.one",
            "expires_at": int(time.time()) + 300,
        }
        old_hash = str(self._stored()["developer.one"]["password_hash"])
        result = web_app.reset_managed_web_user_password("manager.one", "developer.one")
        temporary = str(result["temporary_password"])
        new_hash = str(self._stored()["developer.one"]["password_hash"])
        self.assertNotEqual(old_hash, new_hash)
        self.assertNotIn(token, web_app.WEB_SESSIONS)
        self.assertTrue(web_app.verify_web_password(temporary, new_hash))
        audit = self.audit_file.read_text(encoding="utf-8")
        self.assertNotIn(temporary, audit)
        self.assertNotIn(new_hash, audit)

    def test_manager_safety_and_optimistic_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "own Manager"):
            web_app.save_managed_web_user(
                "manager.one",
                {
                    "username": "manager.one",
                    "role": "developer",
                    "active": True,
                    "responsibles": [],
                    "revision": 1,
                },
            )
        with self.assertRaisesRegex(ValueError, "another session"):
            web_app.save_managed_web_user(
                "manager.one",
                {
                    "username": "developer.one",
                    "role": "developer",
                    "active": True,
                    "responsibles": [],
                    "revision": 99,
                },
            )

    def test_legacy_revision_is_advanced_and_stale_update_is_rejected(self) -> None:
        users = self._stored()
        users["developer.one"].pop("revision", None)
        self._seed(users)
        first = web_app.save_managed_web_user(
            "manager.one",
            {
                "username": "developer.one",
                "role": "developer",
                "active": True,
                "responsibles": ["team.one"],
                "revision": 1,
            },
        )
        self.assertEqual(2, first["user"]["revision"])
        with self.assertRaisesRegex(ValueError, "another session"):
            web_app.save_managed_web_user(
                "manager.one",
                {
                    "username": "developer.one",
                    "role": "auditor",
                    "active": True,
                    "responsibles": ["team.one"],
                    "revision": 1,
                },
            )

    def test_audit_failure_rolls_back_password_and_preserves_session(self) -> None:
        token = "session-before-failed-reset"
        web_app.WEB_SESSIONS[token] = {
            "username": "developer.one",
            "expires_at": int(time.time()) + 300,
        }
        old_hash = str(self._stored()["developer.one"]["password_hash"])
        with patch.object(web_app, "_append_web_user_audit", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(OSError, "audit unavailable"):
                web_app.reset_managed_web_user_password("manager.one", "developer.one")
        self.assertEqual(old_hash, self._stored()["developer.one"]["password_hash"])
        self.assertIn(token, web_app.WEB_SESSIONS)

    def test_non_root_manager_cannot_reset_root(self) -> None:
        users = self._stored()
        users["root"] = {
            "username": "root",
            "password_hash": web_app.hash_web_password("Protected!Root24"),
            "role": "manager",
            "active": True,
            "revision": 1,
        }
        self._seed(users)
        with self.assertRaisesRegex(PermissionError, "protected root"):
            web_app.reset_managed_web_user_password("manager.one", "root")

    def test_new_store_uses_environment_bootstrap_without_plaintext_file(self) -> None:
        root = Path(self.temp_dir.name) / "bootstrap"
        users_file = root / "web_users.json"
        with (
            patch.object(web_app, "WEB_USERS_FILE", users_file),
            patch.object(web_app, "_responsible_usernames", return_value={"admin"}),
            patch.dict(
                "os.environ",
                {
                    "WEB_BOOTSTRAP_ADMIN_USERNAME": "admin",
                    "WEB_BOOTSTRAP_ADMIN_PASSWORD": "Bootstrap!Manager24",
                },
            ),
        ):
            users = web_app.ensure_web_users()
        self.assertEqual(["admin"], list(users))
        self.assertTrue(web_app.verify_web_password("Bootstrap!Manager24", str(users["admin"]["password_hash"])))
        self.assertFalse((root / "initial_credentials_20260714.txt").exists())

    def test_disabled_user_cannot_authenticate(self) -> None:
        users = self._stored()
        users["developer.one"]["active"] = False
        self._seed(users)
        challenge = web_app.new_robot_challenge()
        answer = str(web_app.ROBOT_CHALLENGES[challenge["id"]]["answer"])
        ok, error, _ = web_app.authenticate_web_user(
            "developer.one", "Developer!Pass24", challenge["id"], answer
        )
        self.assertFalse(ok)
        self.assertEqual("Invalid username or password.", error)
        self.assertNotIn(challenge["id"], web_app.ROBOT_CHALLENGES)

    def test_corrupt_user_store_fails_closed(self) -> None:
        self.users_file.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            web_app.ensure_web_users()
        self.assertEqual("{not-json", self.users_file.read_text(encoding="utf-8"))

    def test_manager_ui_contract_and_role_scoped_entry(self) -> None:
        with patch.object(web_app, "list_gitlab_projects_for_user", return_value=[]):
            manager_page = web_app.render_index("manager.one")
            developer_page = web_app.render_index("developer.one")
        self.assertIn('id="userManagementBtn"', manager_page)
        self.assertNotIn('id="userManagementBtn"', developer_page)
        for marker in (
            'id="userAdminModal"',
            'id="userAdminSearch"',
            'id="userAdminRoleFilter"',
            'id="userAdminStatusFilter"',
            'id="managedResponsibleAdd"',
            'id="resetManagedPasswordBtn"',
            'id="temporaryPasswordModal"',
            "'Idempotency-Key': adminRequestId('user-save')",
            "/api/admin/users/reset-password",
        ):
            self.assertIn(marker, manager_page)
        self.assertIn('id="changePasswordBtn"', developer_page)
        self.assertIn("if (me.must_change_password)", developer_page)
        self.assertNotIn("localStorage", manager_page)


if __name__ == "__main__":
    unittest.main()
