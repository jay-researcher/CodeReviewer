from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.web_app import ensure_web_users, verify_web_password


class WebAuthHashingTests(unittest.TestCase):
    def test_legacy_plaintext_password_is_migrated_to_pbkdf2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            users_file = Path(temp) / "web_users.json"
            users_file.write_text(
                json.dumps({"users": {"dev.user": {"username": "dev.user", "password": "Abcd1234!", "role": "developer"}}}),
                encoding="utf-8",
            )
            profiles = {"dev.user": {"role": "developer", "responsible": ["wen.yi"]}}
            with patch("code_reviewer.web_app.WEB_USERS_FILE", users_file), patch(
                "code_reviewer.web_app._responsible_usernames", return_value={"dev.user"},
            ), patch("code_reviewer.web_app._configured_web_user_profiles", return_value=profiles):
                users = ensure_web_users()
            record = users["dev.user"]
            self.assertNotIn("password", record)
            self.assertTrue(verify_web_password("Abcd1234!", str(record["password_hash"])))


if __name__ == "__main__":
    unittest.main()
