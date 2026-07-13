from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.remote_codebase_memory import _credentials, _host_config_block, remote_enabled
from code_reviewer.repository_sync import _codebase_memory_executable, _run_codebase_memory_tool


class RemoteCodebaseMemoryTests(unittest.TestCase):
    def test_reads_named_host_credentials_without_putting_password_in_environment(self) -> None:
        text = """
192.168.3.78:
  user: root
  passphase: secret-value
127.0.0.1:
  user: local
"""
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yml"
            config.write_text(text, encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "CODEBASE_MEMORY_SSH_CONFIG": str(config),
                    "CODEBASE_MEMORY_SSH_USER": "",
                    "CODEBASE_MEMORY_SSH_PASSWORD": "",
                },
                clear=False,
            ):
                self.assertEqual(_credentials("192.168.3.78"), ("root", "secret-value"))

    def test_host_block_stops_at_next_top_level_key(self) -> None:
        block = _host_config_block("192.168.3.78:\n  user: root\nnext:\n  value: x\n", "192.168.3.78")
        self.assertIn("user: root", block)
        self.assertNotIn("value: x", block)

    def test_remote_mode_uses_ssh_adapter_instead_of_local_binary(self) -> None:
        with patch.dict(os.environ, {"CODEBASE_MEMORY_SSH_HOST": "192.168.3.78"}, clear=False), patch(
            "code_reviewer.repository_sync.run_remote_tool"
        ) as remote:
            remote.return_value.returncode = 0
            remote.return_value.stdout = "{}"
            remote.return_value.stderr = ""
            completed = _run_codebase_memory_tool(
                _codebase_memory_executable(),
                "list_projects",
                {},
                timeout=10,
            )
            self.assertTrue(remote_enabled())

        self.assertEqual(completed.returncode, 0)
        remote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
