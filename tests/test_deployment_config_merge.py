from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    # The merge function itself is pure Python; production staging provides
    # PyYAML for the command-line file adapter.
    sys.modules["yaml"] = types.SimpleNamespace()
    import yaml  # type: ignore[no-redef]

from scripts.merge_release_scope_config import (
    LLM_POLICY_KEYS,
    REQUIRED_SCOPE_PATHS,
    REVIEW_POLICY_PATHS,
    merge_release_scopes,
)


def _put(payload: dict[str, object], path: tuple[str, ...], value: dict[str, object]) -> None:
    current = payload
    for segment in path[:-1]:
        current = current.setdefault(segment, {})  # type: ignore[assignment]
    current[path[-1]] = value


class DeploymentConfigMergeTests(unittest.TestCase):
    def test_wvadmin_repository_responsible_boundaries(self) -> None:
        if not hasattr(yaml, "safe_load"):
            self.skipTest("PyYAML is unavailable")
        template = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.yml").read_text(encoding="utf-8")
        )

        self.assertEqual("wen.yi", template["build-repository"]["wvadmin"]["responsible"])
        expected = {
            "momd": "hieut.tran",
            "trade_middle_office": "hieut.tran",
            "low_code_designable": "victorcz.xu",
            "low_code_renderable": "victorcz.xu",
            "low_code_application": "victorcz.xu",
            "account_middle_office": "victorcz.xu",
            "form_designable": "victorcz.xu",
            "coms": "wen.yi",
            "workflow_app": "wen.yi",
            "base": "wen.yi",
            "common": "wen.yi",
        }
        actual = template["wvadmin-repository"]
        for module, responsible in expected.items():
            self.assertEqual(responsible, actual[module]["responsible"], module)

    def test_release_template_exposes_required_review_policy(self) -> None:
        if not hasattr(yaml, "safe_load"):
            self.skipTest("PyYAML is unavailable")
        template = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.yml").read_text(encoding="utf-8")
        )

        for path in REVIEW_POLICY_PATHS:
            current = template
            for segment in path:
                self.assertIn(segment, current, ".".join(path))
                current = current[segment]

    def test_portable_policy_is_merged_without_overwriting_production_runtime(self) -> None:
        production: dict[str, object] = {
            "app": {
                "review_domains": {"legacy": {"reviewers": ["legacy.user"]}},
                "llm": {
                    "codex_http_base_url": "http://production-cpa:8318/v1",
                    "network_mode": "direct",
                    **{key: 1 for key in LLM_POLICY_KEYS},
                },
                "jira_prd": {"auto_fetch": True},
                "review": {
                    "discovery": {"require_strong_history_reference": False, "production_only": True},
                    "release_gate": {
                        "branch_prefixes": {"git_version": ["GIT_VERSION"]},
                        "production_only": True,
                    },
                },
            },
            "production_only": {"workspace": "/var/lib/codereviewer/git-repos"},
        }
        template: dict[str, object] = {
            "app": {
                "review_domains": {
                    "web_frontend": {
                        "applications": ["WVAdmin", "Services Terminal"],
                        "reviewers": ["wen.yi"],
                    }
                },
                "llm": {
                    "codex_http_base_url": "http://127.0.0.1:8318/v1",
                    "network_mode": "auto",
                    "codex_activity_timeout_seconds": 300,
                    "codex_absolute_timeout_seconds": 900,
                    "codex_progress_heartbeat_seconds": 15,
                    "dps_codex_max_retries": 2,
                    "dps_codex_retry_prompt_chars": 42000,
                },
                "jira_prd": {"auto_fetch": False},
                "review": {
                    "discovery": {"require_strong_history_reference": True},
                    "release_gate": {
                        "branch_prefixes": {
                            "git_version": ["GIT_VERSION", "COMPANY_GIT_VERSION"],
                        }
                    },
                },
            }
        }
        for path in REQUIRED_SCOPE_PATHS:
            _put(
                production,
                path,
                {
                    "application": "Old",
                    "release_line": "old",
                    "responsible": "legacy.owner",
                    "branch": "/production/path",
                },
            )
            _put(
                template,
                path,
                {
                    "application": "New",
                    "release_line": "new",
                    "responsible": "release.owner",
                    "branch": "windows-template",
                },
            )

        merged = merge_release_scopes(production, template)

        app = merged["app"]
        self.assertEqual(template["app"]["review_domains"], app["review_domains"])
        for key in LLM_POLICY_KEYS:
            self.assertEqual(template["app"]["llm"][key], app["llm"][key])
        self.assertEqual("http://production-cpa:8318/v1", app["llm"]["codex_http_base_url"])
        self.assertEqual("direct", app["llm"]["network_mode"])
        self.assertTrue(app["jira_prd"]["auto_fetch"])
        self.assertTrue(app["review"]["discovery"]["require_strong_history_reference"])
        self.assertTrue(app["review"]["discovery"]["production_only"])
        self.assertEqual(
            ["GIT_VERSION", "COMPANY_GIT_VERSION"],
            app["review"]["release_gate"]["branch_prefixes"]["git_version"],
        )
        self.assertTrue(app["review"]["release_gate"]["production_only"])
        self.assertEqual(2, len(REVIEW_POLICY_PATHS))
        self.assertEqual("/var/lib/codereviewer/git-repos", merged["production_only"]["workspace"])
        for path in REQUIRED_SCOPE_PATHS:
            current: object = merged
            for segment in path:
                current = current[segment]  # type: ignore[index]
            self.assertEqual("New", current["application"])
            self.assertEqual("new", current["release_line"])
            self.assertEqual("release.owner", current["responsible"])
            self.assertEqual("/production/path", current["branch"])


if __name__ == "__main__":
    unittest.main()
