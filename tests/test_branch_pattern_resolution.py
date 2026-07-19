from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.local_workspaces import WorkspaceEntry, resolve_workspace_for_project_path
from code_reviewer.repository_sync import (
    _version_branch_sort_key,
    resolve_branch_pattern,
    resolve_configured_branches,
)


class BranchPatternResolutionTests(unittest.TestCase):
    def _entry(self, *branches: str) -> WorkspaceEntry:
        return WorkspaceEntry(
            project_path="group/project",
            local_path=Path("D:/work/project"),
            repository_url="https://gitlab.example/group/project.git",
            branches=list(branches),
        )

    def test_exact_branch_remains_backward_compatible(self) -> None:
        entry = self._entry("7.5.1.38")
        with patch("code_reviewer.repository_sync._remote_branch_names") as remote:
            self.assertEqual(resolve_branch_pattern(entry, "7.5.1.38"), "7.5.1.38")
        remote.assert_not_called()

    def test_wildcard_selects_greatest_numeric_version(self) -> None:
        entry = self._entry("7.5.1.*")
        with patch(
            "code_reviewer.repository_sync._remote_branch_names",
            return_value=["7.5.1.9", "7.5.1.38", "7.5.1.105", "7.5.0.999"],
        ):
            self.assertEqual(resolve_branch_pattern(entry, "7.5.1.*"), "7.5.1.105")

    def test_each_product_version_pattern_selects_one_greatest_branch(self) -> None:
        entry = self._entry("9.3.*", "11.2.*", "stable")
        with patch(
            "code_reviewer.repository_sync._remote_branch_names",
            return_value=["9.3.78", "9.3.105", "11.2.9", "11.2.83"],
        ):
            self.assertEqual(
                resolve_configured_branches(entry),
                ["9.3.105", "11.2.83", "stable"],
            )

    def test_version_sort_is_numeric_not_lexical(self) -> None:
        self.assertGreater(
            _version_branch_sort_key("release/1.0.100"),
            _version_branch_sort_key("release/1.0.99"),
        )

    def test_workspace_resolution_accepts_wildcard_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = WorkspaceEntry(
                project_path="group/project",
                local_path=Path(temp) / "old",
                branches=["7.5.0.*"],
            )
            second = WorkspaceEntry(
                project_path="group/project",
                local_path=Path(temp) / "new",
                branches=["7.5.1.*"],
            )
            second.local_path.mkdir()
            with (
                patch("code_reviewer.local_workspaces.app_config_bool", return_value=True),
                patch(
                    "code_reviewer.local_workspaces.load_workspace_entries",
                    return_value=[first, second],
                ),
            ):
                selected = resolve_workspace_for_project_path(
                    "group/project",
                    branch="7.5.1.105",
                )
            self.assertIsNotNone(selected)
            self.assertEqual(selected.local_path, second.local_path)


if __name__ == "__main__":
    unittest.main()
