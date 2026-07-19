from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer import config
from code_reviewer.config_store import (
    ConfigRevisionConflict,
    EffectiveConfigStore,
    InvalidConfigOverride,
    config_revision,
    deep_merge_config,
    load_web_config_overrides,
    validate_config_overrides,
)
from code_reviewer.local_workspaces import git_tools_project_entries


class EffectiveConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.overrides_path = root / "data" / "web_config_overrides.json"
        self.backup_dir = root / "data" / "config_backups"
        self.audit_path = root / "data" / "web_config_audit.jsonl"
        self.store = EffectiveConfigStore(
            overrides_path=self.overrides_path,
            backup_dir=self.backup_dir,
            audit_path=self.audit_path,
            backup_retention=5,
        )
        self.base = {
            "app": {
                "report": {"min_severity": "Medium", "history_days": 14},
                "review": {"mr_states": ["opened", "merged"]},
            },
            "project-group": {
                "type": "backend",
                "module": {"repository_url": "https://gitlab.example.test/team/module.git"},
            },
        }

    def tearDown(self) -> None:
        config.clear_config_cache()
        self.temp_dir.cleanup()

    def test_deep_merge_replaces_lists_and_supports_delete_marker(self) -> None:
        result = deep_merge_config(
            self.base,
            {
                "app": {
                    "report": {
                        "min_severity": "High",
                        "history_days": {"$delete": True},
                    },
                    "review": {"mr_states": ["opened"]},
                    "new_section": {"unused": {"$delete": True}, "enabled": True},
                }
            },
        )
        self.assertEqual("High", result["app"]["report"]["min_severity"])
        self.assertNotIn("history_days", result["app"]["report"])
        self.assertEqual(["opened"], result["app"]["review"]["mr_states"])
        self.assertEqual({"enabled": True}, result["app"]["new_section"])
        self.assertEqual(14, self.base["app"]["report"]["history_days"])

    def test_validation_rejects_non_json_sensitive_and_non_finite_values(self) -> None:
        invalid_values = (
            {"app": {"llm": {"api_token": "must-not-be-stored"}}},
            {"app": {"web": {"password": "must-not-be-stored"}}},
            {"app": {"value": ("tuple",)}},
            {"app": {"value": float("nan")}},
            {"$delete": True},
            {"app": {"bad": {"$delete": False}}},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(InvalidConfigOverride):
                    validate_config_overrides(value)
        validate_config_overrides({"app": {"llm": {"max_tokens": 3000}}})

    def test_atomic_save_creates_backup_audit_and_revision(self) -> None:
        initial_revision = self.store.revision(self.base)
        overrides = {"app": {"report": {"min_severity": "High"}}}
        result = self.store.save_overrides(
            self.base,
            overrides,
            actor="admin",
            expected_revision=initial_revision,
            request_id="request-1",
        )

        self.assertEqual(overrides, load_web_config_overrides(self.overrides_path))
        self.assertEqual("High", result["effective"]["app"]["report"]["min_severity"])
        self.assertEqual(config_revision(self.base, overrides), result["revision"])
        self.assertEqual(1, len(self.store.list_backups()))
        audit = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("admin", audit["actor"])
        self.assertEqual("save", audit["action"])
        self.assertEqual(["app.report.min_severity"], audit["changed_paths"])
        serialized = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("High", serialized)

    def test_stale_revision_is_rejected_without_mutating_store(self) -> None:
        initial_revision = self.store.revision(self.base)
        self.store.save_overrides(
            self.base,
            {"app": {"report": {"min_severity": "High"}}},
            actor="admin",
            expected_revision=initial_revision,
        )
        with self.assertRaises(ConfigRevisionConflict):
            self.store.save_overrides(
                self.base,
                {"app": {"report": {"min_severity": "Critical"}}},
                actor="admin",
                expected_revision=initial_revision,
            )
        self.assertEqual(
            "High",
            self.store.effective(self.base)["app"]["report"]["min_severity"],
        )

    def test_audit_failure_rolls_back_original_override(self) -> None:
        initial_revision = self.store.revision(self.base)
        first = self.store.save_overrides(
            self.base,
            {"app": {"report": {"min_severity": "High"}}},
            actor="admin",
            expected_revision=initial_revision,
        )
        original = self.overrides_path.read_bytes()
        with patch.object(self.store, "_append_audit", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(OSError, "audit unavailable"):
                self.store.save_overrides(
                    self.base,
                    {"app": {"report": {"min_severity": "Critical"}}},
                    actor="admin",
                    expected_revision=first["revision"],
                )
        self.assertEqual(original, self.overrides_path.read_bytes())
        self.assertEqual(
            "High",
            self.store.effective(self.base)["app"]["report"]["min_severity"],
        )

    def test_restore_backup_is_revisioned_and_audited(self) -> None:
        initial_revision = self.store.revision(self.base)
        first = self.store.save_overrides(
            self.base,
            {"app": {"report": {"min_severity": "High"}}},
            actor="admin",
            expected_revision=initial_revision,
        )
        second = self.store.save_overrides(
            self.base,
            {"app": {"report": {"min_severity": "Critical"}}},
            actor="admin",
            expected_revision=first["revision"],
        )
        backup = next(
            item for item in self.store.list_backups() if item["revision"] == first["revision"]
        )
        restored = self.store.restore_backup(
            self.base,
            str(backup["name"]),
            actor="root",
            expected_revision=second["revision"],
            request_id="restore-1",
        )
        self.assertEqual("High", restored["effective"]["app"]["report"]["min_severity"])
        events = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("restore", events[-1]["action"])
        self.assertEqual(backup["name"], events[-1]["restored_from"])

    def test_concurrent_writers_use_optimistic_revision(self) -> None:
        expected = self.store.revision(self.base)
        barrier = threading.Barrier(2)
        results: list[object] = []

        def write(value: str) -> None:
            barrier.wait()
            try:
                results.append(
                    self.store.save_overrides(
                        self.base,
                        {"app": {"report": {"min_severity": value}}},
                        actor="admin",
                        expected_revision=expected,
                    )
                )
            except Exception as exc:  # Captured for deterministic thread assertion.
                results.append(exc)

        threads = [
            threading.Thread(target=write, args=("High",)),
            threading.Thread(target=write, args=("Critical",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        self.assertEqual(
            1,
            sum(isinstance(item, ConfigRevisionConflict) for item in results),
        )

    def test_backup_retention_is_bounded(self) -> None:
        revision = self.store.revision(self.base)
        for index in range(8):
            result = self.store.save_overrides(
                self.base,
                {"app": {"report": {"history_days": index + 1}}},
                actor="admin",
                expected_revision=revision,
            )
            revision = str(result["revision"])
        self.assertLessEqual(len(self.store.list_backups()), 5)


class EffectiveConfigIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.base_path = root / "config.yml"
        self.overrides_path = root / "data" / "web_config_overrides.json"
        self.base_payload = {
            "app": {
                "report": {"min_severity": "Medium"},
                "git_tools": {"groups": []},
            },
            "base-group": {
                "type": "backend",
                "base-module": {
                    "repository_url": "https://gitlab.example.test/base/module.git",
                    "responsible": "base.owner",
                    "branch": "main",
                },
            },
        }
        self.base_path.write_text(
            """app:
  report:
    min_severity: Medium
  git_tools:
    groups: []
base-group:
  type: backend
  base-module:
    repository_url: https://gitlab.example.test/base/module.git
    responsible: base.owner
    branch: main
""",
            encoding="utf-8",
        )
        self.environment = patch.dict(
            "os.environ",
            {
                "GIT_TOOLS_CONFIG": str(self.base_path),
                "WEB_CONFIG_OVERRIDES_FILE": str(self.overrides_path),
            },
        )
        self.environment.start()
        config.clear_config_cache()

    def tearDown(self) -> None:
        config.clear_config_cache()
        self.environment.stop()
        self.temp_dir.cleanup()

    def test_app_and_project_readers_share_effective_payload(self) -> None:
        store = EffectiveConfigStore(
            overrides_path=self.overrides_path,
            backup_dir=self.overrides_path.parent / "backups",
            audit_path=self.overrides_path.parent / "audit.jsonl",
        )
        overrides = {
            "app": {"report": {"min_severity": "High"}},
            "web-group": {
                "type": "frontend",
                "web-module": {
                    "repository_url": "https://gitlab.example.test/web/module.git",
                    "responsible": "web.owner",
                    "branch": "develop",
                },
            },
        }
        store.save_overrides(
            self.base_payload,
            overrides,
            actor="admin",
            expected_revision=store.revision(self.base_payload),
        )
        config.clear_config_cache()

        self.assertEqual(
            "Medium",
            config.load_base_config_payload()["app"]["report"]["min_severity"],
        )
        self.assertEqual("High", config.load_app_config()["report"]["min_severity"])
        entries = git_tools_project_entries()
        self.assertEqual(
            {
                "base-group/base-module",
                "web-group/web-module",
            },
            {f"{entry.group}/{entry.module}" for entry in entries},
        )


if __name__ == "__main__":
    unittest.main()
