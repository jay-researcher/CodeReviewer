from __future__ import annotations

import sys
import types
import unittest

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    # The merge function itself is pure Python; production staging provides
    # PyYAML for the command-line file adapter.
    sys.modules["yaml"] = types.SimpleNamespace()

from scripts.merge_release_scope_config import (
    LLM_POLICY_KEYS,
    REQUIRED_SCOPE_PATHS,
    merge_release_scopes,
)


def _put(payload: dict[str, object], path: tuple[str, ...], value: dict[str, object]) -> None:
    current = payload
    for segment in path[:-1]:
        current = current.setdefault(segment, {})  # type: ignore[assignment]
    current[path[-1]] = value


class DeploymentConfigMergeTests(unittest.TestCase):
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
            }
        }
        for path in REQUIRED_SCOPE_PATHS:
            _put(
                production,
                path,
                {"application": "Old", "release_line": "old", "branch": "/production/path"},
            )
            _put(
                template,
                path,
                {"application": "New", "release_line": "new", "branch": "windows-template"},
            )

        merged = merge_release_scopes(production, template)

        app = merged["app"]
        self.assertEqual(template["app"]["review_domains"], app["review_domains"])
        for key in LLM_POLICY_KEYS:
            self.assertEqual(template["app"]["llm"][key], app["llm"][key])
        self.assertEqual("http://production-cpa:8318/v1", app["llm"]["codex_http_base_url"])
        self.assertEqual("direct", app["llm"]["network_mode"])
        self.assertTrue(app["jira_prd"]["auto_fetch"])
        self.assertEqual("/var/lib/codereviewer/git-repos", merged["production_only"]["workspace"])
        for path in REQUIRED_SCOPE_PATHS:
            current: object = merged
            for segment in path:
                current = current[segment]  # type: ignore[index]
            self.assertEqual("New", current["application"])
            self.assertEqual("new", current["release_line"])
            self.assertEqual("/production/path", current["branch"])


if __name__ == "__main__":
    unittest.main()
