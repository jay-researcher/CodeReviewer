from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from code_reviewer.local_workspaces import WorkspaceEntry, _git_tools_entries_from_payload, _git_tools_entries_from_text
from code_reviewer.analyzer import _jira_involved_file_findings
from code_reviewer.config import DEFAULT_CC_SWITCH_PROVIDER, _load_windows_user_environment, llm_config
from code_reviewer.gitlab_client import GitLabClient
from code_reviewer.jira_client import JiraIssue
from code_reviewer.models import ChangedFile, Finding, ReviewInput, ReviewResult
from code_reviewer.project_context import build_project_context
from code_reviewer.report import _responsible_output_dir, render_markdown, save_report, save_reports, split_result_by_responsible
from code_reviewer.repository_sync import RepositorySyncResult, _attach_codebase_memory
from code_reviewer.resource_optimizer import optimize_prompt_diff
from code_reviewer.review_service import (
    _attach_project_context,
    _balanced_project_context,
    _chunk_review_inputs_if_needed,
    _chunk_review_reason,
    _combine_jira_issue_review_inputs,
    _gitlab_search_has_strong_issue_reference,
    _ignored_branch_type,
    _missing_remote_branch_error,
    _review_fetched_inputs_for_issue,
    review_fingerprint_from_merge_requests,
)
from code_reviewer.llm_provider import (
    _call_codex_cli,
    _compact_prompt_for_retry,
    _looks_like_dps_project,
    _run_codex_process,
    _run_auto_review,
    _run_single_review,
    preview_llm_prompt_budget,
)
from code_reviewer.local_changes import (
    _ensure_mr_commits,
    _issue_branch_candidates,
    _remote_issue_branches,
    local_merge_request_changes,
)
from code_reviewer.process_utils import run_utf8
from web import _acquire_instance_lock


class LocalContextTests(unittest.TestCase):
    def test_scope_responsible_takes_precedence_over_web_runner_folder(self) -> None:
        target = _responsible_output_dir(
            Path("reports"),
            {"responsible": "hieut.tran", "responsible_people": ["hieut.tran"], "web_report_owner": "wen.yi"},
        )
        self.assertEqual(target, Path("reports") / "hieut.tran")

    def test_issue_review_creates_wvadmin_and_deferred_dps11_reports(self) -> None:
        issue = JiraIssue(
            key="ECHNL-5757",
            summary="MO Client Config and MOMD change",
            description=(
                "Involved File Lists\n"
                "dps/release/11.2.84/mas/config/client_config/wvadmin_web.yml\n"
                "modules/subsystem/momd/src/constants/configuration.ts\n"
                "Acceptance Criteria"
            ),
            assignee="owner",
            status="Development Done",
            sprint="10085",
            issue_type="Story",
            labels=[],
            components=["MO Client Config", "MOMD", "WVAdmin"],
            responsibles=["Luck Chen", "Tran Trung Hieu"],
        )
        normal = ReviewInput(
            project="wvp-sv/wvadm/sub/momd",
            mr_id="105",
            jira_key=issue.key,
            source_branch=f"feature/{issue.key}",
            target_branch="master",
            changed_files=[ChangedFile(path="src/constants/configuration.ts", diff="+export const value = 1")],
            raw_diff="+export const value = 1",
            metadata={
                "application": "WVAdmin",
                "release_line": "1.0",
                "responsible": "developer.one",
                "gitlab_project_path": "wvp-sv/wvadm/sub/momd",
                "git_tools_project_match": "matched",
            },
        )
        deferred = [
            {
                "application": "DPS",
                "release_line": "DPS11",
                "project_path": "web-sv-build/dps",
                "mr_id": "2184",
                "mr_url": "https://gitlab.example.com/web-sv-build/dps/-/merge_requests/2184",
                "source_branch": "DPS11_Config-1.4.77",
                "release_gate_role": "company_config",
                "changed_file_paths": ["release/11.2.84/mas/config/client_config/wvadmin_web.yml"],
            },
            {
                "application": "DPS",
                "release_line": "DPS11",
                "project_path": "web-sv-build/dps",
                "mr_id": "2185",
                "source_branch": "DPS11_SCR-1.4.77",
                "release_gate_role": "scr",
                "changed_file_paths": ["release/11.2.84/mas/db_change.scr"],
            },
        ]
        saved_inputs: list[ReviewInput] = []

        def fake_analyze(review_input: ReviewInput, progress: object = None) -> ReviewResult:
            return ReviewResult(review_input, _jira_involved_file_findings(review_input), "Pass", [], [])

        def fake_save(result: ReviewResult, *_args: object, **_kwargs: object) -> dict[str, object]:
            saved_inputs.append(result.review_input)
            scope = f"{result.review_input.metadata['application']}:{result.review_input.metadata['release_line']}"
            return {
                "report": f"{scope}.md",
                "reports": [{"path": f"{scope}.md", "name": f"{scope}.md"}],
                "gitnexus_report": "",
            }

        with (
            patch("code_reviewer.review_service.analyze", side_effect=fake_analyze),
            patch("code_reviewer.review_service._save_review_reports", side_effect=fake_save),
            patch(
                "code_reviewer.review_service._preview_prompt_budget_no_raise",
                return_value={"original_chars": 100, "max_chars": 160000},
            ),
        ):
            result = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=[normal],
                discovered_items=[],
                configured_project_paths=[],
                output_dir=Path("."),
                progress=None,
                deferred_release_gate_resources=deferred,
            )

        self.assertEqual(result["scope_count"], 2)
        self.assertEqual(len(saved_inputs), 2)
        by_scope = {
            (item.metadata["application"], item.metadata["release_line"]): item
            for item in saved_inputs
        }
        self.assertEqual(by_scope[("WVAdmin", "1.0")].metadata["responsible"], "hieut.tran")
        self.assertEqual(by_scope[("DPS", "DPS11")].metadata["responsible"], "luckxh.chen")
        self.assertTrue(by_scope[("DPS", "DPS11")].metadata["deferred_scope_report"])
        self.assertEqual(len(by_scope[("DPS", "DPS11")].metadata["related_merge_requests"]), 2)
        self.assertEqual(_jira_involved_file_findings(by_scope[("WVAdmin", "1.0")]), [])
        self.assertEqual(_jira_involved_file_findings(by_scope[("DPS", "DPS11")]), [])

    def test_deferred_only_issue_generates_a_real_report(self) -> None:
        issue = JiraIssue(
            key="ECHNL-5751",
            summary="Company Config only",
            description="Involved File Lists\nrelease/11.2.84/config.yml\nAcceptance Criteria",
            assignee="owner",
            status="Development Done",
            sprint="10085",
            issue_type="Story",
            labels=[],
            components=["DPS Config"],
        )
        deferred = [
            {
                "application": "DPS",
                "release_line": "DPS11",
                "project_path": "web-sv-build/dps",
                "mr_id": "2184",
                "mr_url": "https://gitlab.example.com/web-sv-build/dps/-/merge_requests/2184",
                "source_branch": "DPS11_Config-1.4.77",
                "release_gate_role": "company_config",
                "changed_file_paths": ["release/11.2.84/config.yml"],
            }
        ]
        saved_inputs: list[ReviewInput] = []

        def fake_save(result: ReviewResult, *_args: object, **_kwargs: object) -> dict[str, object]:
            saved_inputs.append(result.review_input)
            return {
                "report": "ECHNL-5751.md",
                "reports": [{"path": "ECHNL-5751.md", "name": "ECHNL-5751.md"}],
                "gitnexus_report": "",
            }

        with patch("code_reviewer.review_service._save_review_reports", side_effect=fake_save):
            result = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=[],
                discovered_items=[],
                configured_project_paths=[],
                output_dir=Path("."),
                progress=None,
                deferred_release_gate_resources=deferred,
            )

        self.assertEqual(result["review_mode"], "deferred-release-scope")
        self.assertEqual(result["deferred_resource_count"], 1)
        self.assertEqual(result["issue_review_status"], "report-generated")
        self.assertEqual(len(saved_inputs), 1)
        self.assertTrue(saved_inputs[0].metadata["deferred_scope_report"])

    def test_save_reports_splits_deferred_resources_without_name_error(self) -> None:
        review_input = ReviewInput(
            project="web-sv-build/dps",
            mr_id="multi-mr-1",
            jira_key="ECHNL-5757",
            changed_files=[ChangedFile(path="web-sv-build/dps!2184/release/config.yml", diff="+enabled: true")],
            raw_diff="+enabled: true",
            metadata={
                "related_merge_requests": [
                    {
                        "project_path": "web-sv-build/dps",
                        "mr_id": "2184",
                        "application": "DPS",
                        "release_line": "DPS11",
                        "responsible": "luckxh.chen",
                    }
                ],
                "deferred_release_gate_resources": [
                    {
                        "project_path": "web-sv-build/dps",
                        "mr_id": "2184",
                        "application": "DPS",
                        "release_line": "DPS11",
                        "release_gate_role": "company_config",
                    }
                ],
            },
        )
        result = ReviewResult(review_input, [], "Pass", [], [])
        with tempfile.TemporaryDirectory() as directory:
            saved = save_reports(result, Path(directory))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0].review_input.metadata["application"], "DPS")
        self.assertEqual(len(saved[0][0].review_input.changed_files), 1)

    def test_deferred_scope_report_keeps_unprefixed_file_evidence(self) -> None:
        review_input = ReviewInput(
            project="jira-issue",
            mr_id="deferred-DPS11",
            jira_key="ECHNL-5751",
            changed_files=[ChangedFile(path="release/11.2.84/config.yml")],
            metadata={
                "deferred_scope_report": True,
                "application": "DPS",
                "release_line": "DPS11",
                "related_merge_requests": [
                    {
                        "project_path": "web-sv-build/dps",
                        "mr_id": "2184",
                        "application": "DPS",
                        "release_line": "DPS11",
                    }
                ],
            },
        )
        result = ReviewResult(review_input, [], "Pass", [], [])
        split = split_result_by_responsible(result)
        self.assertEqual(len(split), 1)
        self.assertEqual(split[0].review_input.changed_files[0].path, "release/11.2.84/config.yml")

    def test_codex_retry_uses_compacted_prompt(self) -> None:
        prompt = "instructions\n" + ("context\n" * 9000)
        retry_prompt = _compact_prompt_for_retry(prompt, 24000)
        self.assertLessEqual(len(retry_prompt), 24000)
        with patch(
            "code_reviewer.llm_provider._call_codex_cli",
            side_effect=[RuntimeError("timed out after 300 seconds"), '{"findings": [], "notes": []}'],
        ) as call:
            output = _run_single_review(
                "codex-cli",
                "gpt-test",
                prompt,
                300,
                "high",
                "standard",
                "default",
                max_retries=2,
                require_success=True,
                retry_prompt=retry_prompt,
            )
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0].args[0], prompt)
        self.assertEqual(call.call_args_list[1].args[0], retry_prompt)
        self.assertIn("attempt 2/2", " ".join(output.notes))

    def test_issue_review_partitions_llm_input_by_application_release_line(self) -> None:
        issue = JiraIssue(
            key="ECHNL-7213",
            summary="Cross application delivery",
            description="Requirement",
            assignee="owner",
            status="Development Done",
            sprint="10085",
            issue_type="Story",
            labels=[],
        )
        scopes = [
            ("1", "iTrade Client", "7.5.0", "src/750.ts"),
            ("2", "iTrade Client", "7.5.1", "src/751.ts"),
            ("3", "DPS", "DPS11", "src/dps.php"),
        ]
        inputs = [
            ReviewInput(
                project=f"group/project-{mr_id}",
                mr_id=mr_id,
                jira_key=issue.key,
                source_branch=f"feature/{issue.key}",
                target_branch=release_line,
                changed_files=[ChangedFile(path=path, diff=f"+{path}")],
                raw_diff=f"+{path}",
                metadata={
                    "application": application,
                    "release_line": release_line,
                    "responsible": "owner",
                    "gitlab_project_path": f"group/project-{mr_id}",
                    "git_tools_project_match": "matched",
                },
            )
            for mr_id, application, release_line, path in scopes
        ]
        deferred = [
            {
                "application": "DPS",
                "release_line": "DPS11",
                "project_path": "web-sv-build/dps",
                "source_branch": "DPS11_Config-1.0",
            }
        ]
        analyzed: list[ReviewInput] = []

        def fake_analyze(review_input: ReviewInput, progress: object = None) -> ReviewResult:
            analyzed.append(review_input)
            return ReviewResult(
                review_input=review_input,
                findings=[],
                conclusion="Pass",
                risk_summary=[],
                test_suggestions=[],
            )

        def fake_save(result: ReviewResult, *_args: object, **_kwargs: object) -> dict[str, object]:
            scope = f"{result.review_input.metadata['application']}:{result.review_input.metadata['release_line']}"
            return {
                "report": f"{scope}.md",
                "reports": [{"path": f"{scope}.md", "name": f"{scope}.md"}],
                "gitnexus_report": "",
            }

        with (
            patch("code_reviewer.review_service.analyze", side_effect=fake_analyze),
            patch("code_reviewer.review_service._save_review_reports", side_effect=fake_save),
            patch(
                "code_reviewer.review_service._preview_prompt_budget_no_raise",
                return_value={"original_chars": 100, "max_chars": 160000},
            ),
        ):
            result = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=inputs,
                discovered_items=[],
                configured_project_paths=[],
                output_dir=Path("."),
                progress=None,
                deferred_release_gate_resources=deferred,
            )

        self.assertEqual(3, len(analyzed))
        self.assertEqual(3, result["scope_count"])
        self.assertEqual(
            {
                ("iTrade Client", "7.5.0"),
                ("iTrade Client", "7.5.1"),
                ("DPS", "DPS11"),
            },
            {
                (
                    str(item.metadata.get("application")),
                    str(item.metadata.get("release_line")),
                )
                for item in analyzed
            },
        )
        self.assertEqual(1, len({str(item.metadata["run_group_id"]) for item in analyzed}))
        for item in analyzed:
            related = item.metadata.get("related_merge_requests") or []
            self.assertEqual(1, len(related))
            deferred_items = item.metadata.get("deferred_release_gate_resources") or []
            if item.metadata.get("application") == "DPS":
                self.assertEqual(1, len(deferred_items))
            else:
                self.assertEqual([], deferred_items)

    def test_windows_user_openai_key_is_loaded_for_an_existing_shell(self) -> None:
        closed: list[object] = []
        registry_key = object()
        fake_winreg = SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            KEY_READ=1,
            OpenKey=lambda *_args: registry_key,
            QueryValueEx=lambda key, name: ("user-scoped-key", 1),
            CloseKey=closed.append,
        )
        with (
            patch.dict(sys.modules, {"winreg": fake_winreg}),
            patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
        ):
            loaded = _load_windows_user_environment(windows=True)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "user-scoped-key")

        self.assertEqual(loaded, ["OPENAI_API_KEY"])
        self.assertEqual(closed, [registry_key])

    def test_dps_root_project_requires_codex(self) -> None:
        self.assertTrue(_looks_like_dps_project(ReviewInput(project="web-sv-build/dps")))
        self.assertTrue(_looks_like_dps_project(ReviewInput(project="web-sv-build/dps11")))
        self.assertFalse(_looks_like_dps_project(ReviewInput(project="web-sv-build/wvadmin")))

    def test_deleted_target_branch_uses_exact_mr_base_context(self) -> None:
        workspace = WorkspaceEntry(
            project_path="group/project",
            local_path=Path(r"D:\repos\project"),
            repository_url="https://gitlab.example.com/group/project.git",
        )
        sync_result = RepositorySyncResult(
            project_path="group/project",
            local_path=str(workspace.local_path),
            branch="ECHNL-5658",
            action="failed",
            error="git fetch failed: fatal: couldn't find remote ref refs/heads/ECHNL-5658",
        )
        review_input = ReviewInput(
            project="group/project",
            target_branch="ECHNL-5658",
            changed_files=[ChangedFile(path="src/value.php")],
            metadata={
                "gitlab_project_path": "group/project",
                "diff_base_sha": "a" * 40,
            },
        )

        with patch("code_reviewer.review_service.resolve_workspace_for_project_path", return_value=workspace), patch(
            "code_reviewer.review_service.sync_workspace", return_value=sync_result
        ), patch("code_reviewer.review_service.attach_project_context") as attach_context:
            _attach_project_context(review_input, None)

        attach_context.assert_called_once_with(review_input, workspace.local_path, ref="a" * 40)
        self.assertEqual(review_input.metadata["project_context_source"], "local-workspace-exact-mr-base")
        self.assertEqual(review_input.metadata["repository_sync_fallback_ref"], "a" * 40)
        self.assertIn("target branch unavailable", review_input.metadata["codebase_memory_status"])
        self.assertNotIn("project_context_error", review_input.metadata)

    def test_only_missing_remote_branch_errors_enable_exact_base_fallback(self) -> None:
        self.assertTrue(_missing_remote_branch_error("fatal: couldn't find remote ref refs/heads/ECHNL-5658"))
        self.assertTrue(_missing_remote_branch_error("remote ref does not exist"))
        self.assertFalse(_missing_remote_branch_error("authentication failed"))

    def test_default_cc_switch_provider_is_claude_code_opus(self) -> None:
        self.assertEqual(DEFAULT_CC_SWITCH_PROVIDER, "Claude code opus")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODE_REVIEW_OVERRIDE_LLM_CC_SWITCH_PROVIDER", None)
            self.assertEqual(llm_config()["cc_switch_provider"], "Claude code opus")

    def test_default_codex_model_is_gpt_56_sol(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_CODEX_MODEL", None)
            os.environ.pop("CODEX_MODEL", None)
            self.assertEqual(llm_config()["codex_model"], "gpt-5.6-sol")

    def test_default_provider_uses_cpa_codex_without_cc_switch_fallback(self) -> None:
        config = llm_config()
        self.assertEqual(config["provider"], "codex-cli")
        self.assertFalse(config["fallback_to_cc_switch"])
        with (
            patch("code_reviewer.llm_provider._call_codex_cli", side_effect=RuntimeError("CPA unavailable")),
            patch("code_reviewer.llm_provider._call_cc_switch_api") as cc_switch,
            patch("code_reviewer.llm_provider._llm_require_success", return_value=False),
        ):
            output = _run_auto_review(
                "review prompt",
                10,
                {
                    "codex_model": "gpt-5.6-sol",
                    "codex_timeout_seconds": 10,
                    "fallback_to_cc_switch": False,
                },
                "high",
                "standard",
                1,
            )

        cc_switch.assert_not_called()
        self.assertIn("Automatic CC Switch fallback is disabled", " ".join(output.notes))

    def test_near_budget_multi_mr_review_chunks_before_hard_limit(self) -> None:
        inputs = [
            ReviewInput(
                project="group/project",
                mr_id=str(index),
                metadata={
                    "responsible": "wen.yi",
                    "git_tools_project_name": "itrade-client",
                    "gitlab_project_path": "group/project",
                    "application": "iTrade Client",
                    "release_line": "7.5.1",
                },
            )
            for index in range(1, 5)
        ]
        budget = {"original_chars": 146196, "final_chars": 146196, "max_chars": 160000}

        reason = _chunk_review_reason(inputs, budget)
        chunks = _chunk_review_inputs_if_needed(inputs[0], inputs, budget, reason=reason)

        self.assertIn("near", reason)
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2])

    def test_prompt_budget_trims_to_soft_target_before_hard_limit(self) -> None:
        review_input = ReviewInput(
            project="project",
            jira_key="ECHNL-1",
            mr_id="multi-mr-1",
            changed_files=[ChangedFile(path="src/a.php", diff="+change\n")],
            raw_diff="+change\n",
            metadata={"project_context": "A" * 150000},
        )

        budget = preview_llm_prompt_budget(review_input)

        self.assertEqual(budget["max_chars"], 160000)
        self.assertEqual(budget["target_chars"], 90000)
        self.assertLessEqual(budget["final_chars"], 90000)
        self.assertGreater(budget["trimmed_chars"], 0)

    def test_git_version_review_uses_focused_release_gate_budget(self) -> None:
        changed = ChangedFile(
            path="release/11.2.83/git_version-v11.2.83.yml",
            additions=4000,
            diff="\n".join(f"+module_{index}: {'a' * 40}" for index in range(4000)),
        )
        review_input = ReviewInput(
            project="web-sv-build/dps",
            source_branch="DPS11_GIT_VERSION-11.2.83",
            changed_files=[changed],
            raw_diff=changed.diff,
            metadata={
                "mr_type": "GIT_VERSION",
                "project_context": "P" * 50000,
                "jira_prd_context": "J" * 30000,
                "git_version_review_context": "G" * 50000,
            },
        )

        budget = preview_llm_prompt_budget(review_input)

        self.assertEqual(review_input.metadata["llm_review_profile"], "git-version-release-gate")
        self.assertEqual(review_input.metadata["llm_diff_optimization"]["max_chars"], 20000)
        self.assertEqual(budget["max_chars"], 60000)
        self.assertEqual(budget["target_chars"], 45000)
        self.assertLessEqual(budget["final_chars"], 45000)

    def test_git_version_profile_can_be_detected_from_branch_before_metadata(self) -> None:
        review_input = ReviewInput(
            source_branch="GIT_VERSION-7.5.1",
            raw_diff="+version: 7.5.1\n",
        )

        budget = preview_llm_prompt_budget(review_input)

        self.assertEqual(review_input.metadata["llm_review_profile"], "git-version-release-gate")
        self.assertEqual(budget["max_chars"], 60000)

    def test_web_resource_diff_is_summarized_before_prompt_truncation(self) -> None:
        css_diff = "\n".join([f"+.company-{index} {{ color: #fff; }}" for index in range(800)])
        logic_diff = "@@ -1 +1 @@\n-export const enabled = false\n+export const enabled = true\n"
        changed_files = [
            ChangedFile(path="src/components/feature.ts", additions=1, deletions=1, diff=logic_diff),
            ChangedFile(path="company/css/company.css", additions=800, deletions=0, diff=css_diff),
        ]
        raw_diff = "\n\n".join(
            f"diff --git a/{item.path} b/{item.path}\n--- a/{item.path}\n+++ b/{item.path}\n{item.diff}"
            for item in changed_files
        )

        diff, diagnostics = optimize_prompt_diff(changed_files, raw_diff, 12000)

        self.assertIn("src/components/feature.ts", diff)
        self.assertIn("enabled = true", diff)
        self.assertIn("[Web resource summary]", diff)
        self.assertLess(len(diff), len(raw_diff))
        self.assertEqual(diagnostics["resource_file_count"], 1)
        self.assertTrue(diagnostics["optimized_files"])

    def test_codex_cli_stdin_uses_utf8_for_bom_and_chinese_prompt(self) -> None:
        with (
            patch.dict(os.environ, {"LLM_CODEX_HTTP_API_KEY_ENV": "OPENAI_API_KEY", "OPENAI_API_KEY": "test-key"}),
            patch("code_reviewer.llm_provider._resolve_codex_cli", return_value="codex"),
            patch("code_reviewer.llm_provider._run_codex_process") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout=b'{"findings":[],"notes":[]}',
                stderr=b"",
            )

            output = _call_codex_cli("\ufeff中文 prompt", "gpt-5.6-sol", 30)

        self.assertIn("findings", output)
        self.assertTrue(run_mock.call_args.args[1].startswith("\ufeff中文".encode("utf-8")))
        command = run_mock.call_args.args[0]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--json", command)
        self.assertIn("model_provider=\"codereviewer_http\"", command)
        self.assertIn('model_providers.codereviewer_http.env_key="OPENAI_API_KEY"', command)
        self.assertIn("model_providers.codereviewer_http.requires_openai_auth=false", command)
        self.assertIn("model_providers.codereviewer_http.supports_websockets=false", command)

    def test_codex_process_emits_heartbeats_while_stream_is_active(self) -> None:
        events: list[dict[str, object]] = []
        command = [
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); print('{\"type\":\"started\"}', flush=True); time.sleep(.2); print('{\"type\":\"done\"}', flush=True)",
        ]
        completed = _run_codex_process(
            command,
            "中文".encode("utf-8"),
            os.environ.copy(),
            activity_timeout=1,
            absolute_timeout=3,
            heartbeat_seconds=1,
            progress=events.append,
            jira_key="ECHNL-5655",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn(b'"started"', completed.stdout)
        self.assertIn("llm-start", [str(item.get("event")) for item in events])
        self.assertIn("llm-heartbeat", [str(item.get("event")) for item in events])

    def test_codex_process_times_out_only_after_no_real_activity(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(2)"]
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "no Codex provider activity"):
            _run_codex_process(
                command,
                b"prompt",
                os.environ.copy(),
                activity_timeout=0.2,
                absolute_timeout=3,
                heartbeat_seconds=1,
            )
        self.assertLess(time.monotonic() - started, 1.5)

    def test_web_port_instance_lock_rejects_a_second_server(self) -> None:
        port = 49000 + (os.getpid() % 1000)
        first = _acquire_instance_lock(port)
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                _acquire_instance_lock(port)
        finally:
            first.close()

    def test_subprocess_timeout_returns_promptly_and_kills_process_group(self) -> None:
        script = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        completed = run_utf8([sys.executable, "-c", script], timeout=1)

        self.assertEqual(completed.returncode, 124)
        self.assertIn("timed out after 1 seconds", completed.stderr)
        self.assertLess(time.monotonic() - started, 8)

    def test_codebase_memory_skips_build_repositories(self) -> None:
        entry = WorkspaceEntry(
            project_path="web-sv-build/dps",
            local_path=Path("build-repository/dps"),
        )
        result = RepositorySyncResult(
            project_path=entry.project_path,
            local_path=str(entry.local_path),
            branch="develop",
            commit="a" * 40,
        )
        with patch("code_reviewer.repository_sync._prepare_codebase_memory_source") as prepare:
            _attach_codebase_memory(result, entry, "develop")

        prepare.assert_not_called()
        self.assertEqual(result.index_status, "skipped: project matches web-sv-build")

    def test_project_context_limits_web_resource_file_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            resource_dir = repo / "company" / "css"
            resource_dir.mkdir(parents=True)
            resource = resource_dir / "company.css"
            resource.write_text(".company { color: red; }\n" * 1000, encoding="utf-8")

            context = build_project_context(repo, ["company/css/company.css"])

            text = str(context["text"])
            self.assertIn("[Web resource context optimized]", text)
            self.assertIn("company/css/company.css", text)
            self.assertNotIn(".company { color: red; }\n" * 200, text)

    def test_project_context_prioritizes_dependencies_and_skips_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "Service.php").write_text(
                "<?php\nuse App\\Support\\Dependency;\nfinal class Service {}\n",
                encoding="utf-8",
            )
            (source / "Dependency.php").write_text("<?php\nfinal class Dependency {}\n", encoding="utf-8")
            (repo / "README.md").write_text("large documentation", encoding="utf-8")
            (repo / ".gitlab-ci.yml").write_text("test: true", encoding="utf-8")
            (repo / "phpcs-report.xml").write_text("<report>noise</report>", encoding="utf-8")

            context = build_project_context(repo, ["src/Service.php"])
            included = context["included_files"]

            self.assertIn("src/Service.php", included)
            self.assertIn("src/Dependency.php", included)
            self.assertNotIn("README.md", included)
            self.assertNotIn(".gitlab-ci.yml", included)
            self.assertNotIn("phpcs-report.xml", included)

    def test_consolidated_context_balances_each_merge_request(self) -> None:
        parts = [
            "## project-a !1\n" + "A" * 4000,
            "## project-b !2\n" + "B" * 4000,
            "## project-c !3\n" + "C" * 4000,
        ]

        context = _balanced_project_context(parts, 3000)

        self.assertLessEqual(len(context), 3000)
        self.assertIn("project-a !1", context)
        self.assertIn("project-b !2", context)
        self.assertIn("project-c !3", context)

    def test_config_text_parser_reads_branch_lists_and_local_working_copy(self) -> None:
        entries = _git_tools_entries_from_text(
            """
build-repository:
  client:
    repository_url: https://gitlab.example.com/group/client.git
    branch:
      - 7.5.0
      - 7.5.1
    local_working_copy: D:/repos/client
""",
            set(),
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].branches, ["7.5.0", "7.5.1"])
        expected_path = Path("D:/repos/client")
        if not expected_path.is_absolute():
            expected_path = Path.cwd() / expected_path
        self.assertEqual(entries[0].local_path, expected_path)

    def test_git_tools_config_inherits_frontend_backend_type(self) -> None:
        entries = _git_tools_entries_from_payload(
            {
                "middle-office": {
                    "responsible": ["kevin.tan"],
                    "wvadmin-projects": {
                        "type": "frontend",
                        "dev_repository_url": {
                            "form": {"repository_url": "https://gitlab.example.com/group/form.git"}
                        },
                    },
                    "dps11-projects": {
                        "type": "backend",
                        "dev_repository": {
                            "user": {"repository_url": "https://gitlab.example.com/group/user.git"}
                        },
                    },
                }
            },
            set(),
        )

        by_path = {entry.project_path: entry for entry in entries}
        self.assertEqual(by_path["group/form"].project_type, "frontend")
        self.assertEqual(by_path["group/user"].project_type, "backend")
        self.assertEqual(by_path["group/user"].responsible, "kevin.tan")

    def test_text_config_group_type_is_inherited_by_projects(self) -> None:
        entries = _git_tools_entries_from_text(
            """
backend-projects:
  type: backend
  api:
    repository_url: https://gitlab.example.com/group/api.git
""",
            set(),
        )

        self.assertEqual(entries[0].project_type, "backend")

    def test_project_context_reads_the_requested_git_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "reviewer@example.com")
            _git(repo, "config", "user.name", "CodeReviewer Test")
            source = repo / "src"
            source.mkdir()
            target = source / "value.py"
            target.write_text("VALUE = 'main'\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "main")
            _git(repo, "checkout", "-b", "release")
            target.write_text("VALUE = 'release'\n", encoding="utf-8")
            _git(repo, "commit", "-am", "release")
            _git(repo, "checkout", "-")

            context = build_project_context(repo, ["src/value.py"], ref="release")

            self.assertEqual(context["ref"], "release")
            self.assertIn("VALUE = 'release'", str(context["text"]))
            self.assertNotIn("VALUE = 'main'", str(context["text"]))

    def test_involved_file_check_uses_union_of_historical_formal_mrs(self) -> None:
        issue = JiraIssue(
            key="ECHNL-1",
            summary="History review",
            description=(
                "Involved File Lists\n"
                "group/project!src/earlier.js\n"
                "Testing Notes\n"
                "first delivery\n"
                "Involved File Lists\n"
                "group/project!src/latest.js\n"
                "Acceptance Criteria\n"
            ),
            assignee="owner",
            status="Development Done",
            sprint="",
            issue_type="Bug",
            labels=[],
        )
        inputs = [
            _mr_input("1", "src/earlier.js"),
            _mr_input("2", "src/latest.js"),
        ]
        combined = _combine_jira_issue_review_inputs(issue, inputs, [], ["group/project"])

        self.assertEqual(_jira_involved_file_findings(combined), [])

    def test_gitlab_diff_fetch_falls_back_when_changes_payload_is_null(self) -> None:
        client = _FakeGitLabClient()

        review_input = client.review_input_from_mr(
            "https://gitlab.example.com/group/project/-/merge_requests/55",
            jira_key="ECHNL-5657",
        )

        self.assertEqual(review_input.mr_id, "55")
        self.assertEqual([item.path for item in review_input.changed_files], ["src/value.php"])
        self.assertEqual(client.requested_paths[1], "/api/v4/projects/group%2Fproject/merge_requests/55/changes")
        self.assertEqual(client.requested_paths[2], "/api/v4/projects/group%2Fproject/merge_requests/55/diffs")

    def test_codebase_memory_failure_does_not_block_repository_sync_result(self) -> None:
        entry = _git_tools_entries_from_text(
            """
group:
  project:
    repository_url: https://gitlab.example.com/group/project.git
    branch: release
    local_working_copy: D:/repos/project
""",
            set(),
        )[0]
        result = RepositorySyncResult(
            project_path=entry.project_path,
            local_path=str(entry.local_path),
            branch="release",
            commit="abc123",
            action="fetched",
        )

        with (
            patch("code_reviewer.repository_sync._persistent_index_matches", return_value=False),
            patch(
                "code_reviewer.repository_sync._prepare_codebase_memory_source",
                side_effect=RuntimeError("Codebase Memory source has unexpected local changes"),
            ),
        ):
            _attach_codebase_memory(result, entry, "release")

        self.assertEqual(result.error, "")
        self.assertFalse(result.indexed)
        self.assertIn("skipped:", result.index_status)

    def test_local_mr_diff_uses_immutable_sha_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "reviewer@example.com")
            _git(repo, "config", "user.name", "Reviewer")
            source = repo / "src"
            source.mkdir()
            target = source / "value.py"
            target.write_text("VALUE = 'before'\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base_sha = _git_output(repo, "rev-parse", "HEAD")
            target.write_text("VALUE = 'after'\n", encoding="utf-8")
            _git(repo, "commit", "-am", "head")
            head_sha = _git_output(repo, "rev-parse", "HEAD")
            mr = {
                "diff_refs": {"base_sha": base_sha, "head_sha": head_sha},
                "source_branch": "feature/ECHNL-1",
                "target_branch": "main",
            }
            cache = root / "gitnexus"
            with patch("code_reviewer.local_changes.gitnexus_config", return_value={"storage_path": str(cache)}):
                first = local_merge_request_changes(repo, "group/project", "1", mr)
                second = local_merge_request_changes(repo, "group/project", "1", mr)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual([item.path for item in second.changed_files], ["src/value.py"])
            self.assertIn("VALUE = 'after'", second.raw_diff)

    def test_issue_branch_candidates_use_jira_key_and_cover_dps_layers(self) -> None:
        candidates = _issue_branch_candidates("ECHNL-5658")

        self.assertIn("improvement/ECHNL-5658", candidates)
        self.assertIn("feature/API#ECHNL-5658", candidates)
        self.assertIn("bug/DAO#ECHNL-5658", candidates)
        self.assertIn("API#ECHNL-5658", candidates)
        self.assertNotIn("feature/1255", candidates)

    def test_empty_source_branch_fetches_exact_mr_ref_without_branch_guessing(self) -> None:
        base_sha = "a" * 40
        head_sha = "b" * 40
        available: set[str] = set()
        fetched: list[str] = []

        def fake_fetch(_repo: Path, refspec: str, _errors: list[str]) -> bool:
            fetched.append(refspec)
            if "refs/heads/main" in refspec:
                available.add(base_sha)
            if "refs/merge-requests/1255/head" in refspec:
                available.add(head_sha)
            return True

        with (
            patch("code_reviewer.local_changes._has_commit", side_effect=lambda _repo, sha: sha in available),
            patch("code_reviewer.local_changes._fetch_refspec", side_effect=fake_fetch),
            patch("code_reviewer.local_changes._fetch_object") as fetch_object,
            patch("code_reviewer.local_changes._remote_issue_branches") as remote_branches,
        ):
            _ensure_mr_commits(
                Path("repo"),
                "1255",
                {"target_branch": "main", "source_branch": ""},
                base_sha,
                head_sha,
                jira_key="ECHNL-5658",
            )

        self.assertTrue(any("refs/heads/main" in item for item in fetched))
        self.assertTrue(any("refs/merge-requests/1255/head" in item for item in fetched))
        self.assertFalse(any("ECHNL-5658" in item for item in fetched))
        fetch_object.assert_not_called()
        remote_branches.assert_not_called()

    def test_branch_fallback_uses_jira_key_only_after_exact_refs_and_sha_fail(self) -> None:
        base_sha = "c" * 40
        head_sha = "d" * 40
        available: set[str] = set()
        fetched: list[str] = []

        def fake_fetch(_repo: Path, refspec: str, _errors: list[str]) -> bool:
            fetched.append(refspec)
            if "improvement/ECHNL-5658" in refspec:
                available.update({base_sha, head_sha})
                return True
            return False

        with (
            patch("code_reviewer.local_changes._has_commit", side_effect=lambda _repo, sha: sha in available),
            patch("code_reviewer.local_changes._fetch_refspec", side_effect=fake_fetch),
            patch("code_reviewer.local_changes._fetch_object", return_value=False),
            patch(
                "code_reviewer.local_changes._remote_issue_branches",
                return_value=["improvement/ECHNL-5658"],
            ) as remote_branches,
        ):
            _ensure_mr_commits(
                Path("repo"),
                "1255",
                {"target_branch": "main", "source_branch": ""},
                base_sha,
                head_sha,
                jira_key="ECHNL-5658",
            )

        remote_branches.assert_called_once_with(Path("repo"), "ECHNL-5658", ANY)
        self.assertTrue(any("improvement/ECHNL-5658" in item for item in fetched))
        self.assertFalse(any("improvement/1255" in item for item in fetched))

    def test_remote_branch_resolution_fetches_only_existing_candidate(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="f" * 40 + "\trefs/heads/improvement/ECHNL-5658\n",
            stderr="",
        )
        with patch("code_reviewer.local_changes.run_utf8", return_value=completed) as run:
            branches = _remote_issue_branches(Path("repo"), "ECHNL-5658", [])

        self.assertEqual(branches, ["improvement/ECHNL-5658"])
        command = run.call_args.args[0]
        self.assertIn("refs/heads/feature/API#ECHNL-5658", command)

    def test_ignored_branch_type_matches_source_branch_prefix_case_insensitive(self) -> None:
        self.assertEqual(_ignored_branch_type("Company_Config/ECHNL-1"), "COMPANY_CONFIG")
        self.assertEqual(_ignored_branch_type("company_config/ECHNL-1"), "COMPANY_CONFIG")
        # GIT_VERSION is the release-gate entry point. It must be reviewed,
        # unlike Company Config and SCR branches that are deferred to it.
        self.assertEqual(_ignored_branch_type("GIT_VERSION/ECHNL-1"), "")
        self.assertEqual(_ignored_branch_type("git_version\\ECHNL-1"), "")
        self.assertEqual(_ignored_branch_type("feature/Git_Version-ECHNL-1"), "")
        self.assertEqual(_ignored_branch_type("gitversion/ECHNL-1"), "")

    def test_gitlab_history_fallback_requires_title_or_branch_jira_reference(self) -> None:
        self.assertTrue(_gitlab_search_has_strong_issue_reference(
            "ECHNL-5655",
            {"title": "Fix ECHNL-5655 audit log", "source_branch": "11.2.83"},
        ))
        self.assertTrue(_gitlab_search_has_strong_issue_reference(
            "ECHNL-5655",
            {"title": "Fix audit log", "source_branch": "bug/ECHNL-5655"},
        ))
        self.assertFalse(_gitlab_search_has_strong_issue_reference(
            "ECHNL-5655",
            {
                "title": "Merged From Company_GIT_VERSION into 11.2.83",
                "source_branch": "Company_GIT_VERSION-1.4.76(11.2.83)",
                "description": "Generated release notes mention ECHNL-5655",
            },
        ))

    def test_same_application_release_line_merges_mrs_and_responsible_scope(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-5308",
                mr_id="multi-mr-2",
                changed_files=[
                    ChangedFile(path="group/web!1/src/a.js", additions=1, diff="+web"),
                    ChangedFile(path="group/api!2/src/b.js", additions=1, diff="+api"),
                ],
                metadata={
                    "related_merge_requests": [
                        {
                            "mr_id": "1",
                            "mr_url": "https://gitlab.example.com/group/web/-/merge_requests/1",
                            "project_path": "group/web",
                            "responsible": "wen.yi",
                            "application": "iTrade Client",
                            "project_type": "frontend",
                            "target_branch": "7.5.1.38",
                            "file_prefix": "group/web!1",
                        },
                        {
                            "mr_id": "2",
                            "mr_url": "https://gitlab.example.com/group/api/-/merge_requests/2",
                            "project_path": "group/api",
                            "responsible": "kevin.tan+sunny.cheng",
                            "application": "iTrade Client",
                            "project_type": "backend",
                            "target_branch": "ITRADE_CLIENT_7.5.1",
                            "file_prefix": "group/api!2",
                        },
                    ]
                },
            ),
            findings=[
                Finding(
                    severity="High",
                    file_path="group/web!1/src/a.js",
                    line=1,
                    title="Web issue",
                    detail="web detail",
                    recommendation="fix web",
                ),
                Finding(
                    severity="Critical",
                    file_path="group/api!2/src/b.js",
                    line=1,
                    title="API issue",
                    detail="api detail",
                    recommendation="fix api",
                ),
            ],
            conclusion="Review result",
            risk_summary=[],
            test_suggestions=[],
        )

        split = split_result_by_responsible(result)
        self.assertEqual(1, len(split))
        report = split[0]
        self.assertEqual("iTrade Client", report.review_input.metadata["application"])
        self.assertEqual("7.5.1", report.review_input.metadata["release_line"])
        self.assertEqual("mixed", report.review_input.metadata["project_type"])
        self.assertEqual(
            ["kevin.tan", "sunny.cheng", "wen.yi"],
            report.review_input.metadata["responsible_scope"],
        )
        self.assertEqual(
            ["Web issue", "API issue"],
            [finding.title for finding in report.findings],
        )
        markdown = render_markdown(report, language="en")
        self.assertIn("- Application: iTrade Client", markdown)
        self.assertIn("- Release Line: 7.5.1", markdown)

        with tempfile.TemporaryDirectory() as temp:
            saved = save_reports(result, Path(temp))
            relative_paths = sorted(path.relative_to(temp).as_posix() for _report, path in saved)

        self.assertEqual(
            relative_paths,
            [
                "kevin.tan+sunny.cheng+wen.yi/ECHNL-5308_iTrade-Client-7.5.1_has-issue-critical.md",
            ],
        )

    def test_reports_split_by_application_instead_of_project_type(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-8888",
                mr_id="multi-mr-2",
                changed_files=[
                    ChangedFile(path="group/web!1/src/a.ts", additions=1, diff="+web"),
                    ChangedFile(path="group/api!2/src/a.php", additions=1, diff="+api"),
                ],
                metadata={
                    "related_merge_requests": [
                        {
                            "mr_id": "1",
                            "project_path": "group/web",
                            "responsible": "wen.yi",
                            "project_type": "frontend",
                            "application": "iTrade Client",
                            "target_branch": "7.5.1.39",
                            "file_prefix": "group/web!1",
                        },
                        {
                            "mr_id": "2",
                            "project_path": "group/dps9/api",
                            "responsible": "wen.yi",
                            "project_type": "backend",
                            "application": "DPS",
                            "target_branch": "9.3.80",
                            "file_prefix": "group/api!2",
                        },
                    ]
                },
            ),
            findings=[
                Finding("High", "group/web!1/src/a.ts", 1, "Web issue", "detail", "fix"),
                Finding("Critical", "group/api!2/src/a.php", 1, "API issue", "detail", "fix"),
            ],
            conclusion="Review result",
            risk_summary=[],
            test_suggestions=[],
        )

        split = split_result_by_responsible(result)
        by_application = {
            (item.review_input.metadata["application"], item.review_input.metadata["release_line"]): item
            for item in split
        }
        self.assertEqual(
            ["Web issue"],
            [finding.title for finding in by_application[("iTrade Client", "7.5.1")].findings],
        )
        self.assertEqual(
            ["API issue"],
            [finding.title for finding in by_application[("DPS", "DPS9")].findings],
        )

        with tempfile.TemporaryDirectory() as temp:
            saved = save_reports(result, Path(temp))
            names = sorted(path.name for _report, path in saved)
        self.assertEqual(
            names,
            ["ECHNL-8888_DPS9_has-issue-critical.md", "ECHNL-8888_iTrade-Client-7.5.1_has-issue-high.md"],
        )

    def test_release_lines_applications_and_unmapped_scopes_stay_isolated(self) -> None:
        scope_specs = [
            ("itrade!1", "iTrade Client", "7.5.0.38", "group/itrade-client", "frontend"),
            ("itrade!2", "iTrade Client", "7.5.1.39", "group/itrade-client", "frontend"),
            ("dps9!3", "DPS", "9.3.80", "group/dps9/api", "backend"),
            ("dps11!4", "DPS", "11.2.84", "group/dps11/api", "backend"),
            ("wvadmin!5", "WVAdmin", "1.0.84", "group/wvadmin", "frontend"),
            ("terminal!6", "Services Terminal", "5.0.63", "group/services-terminal", "frontend"),
            ("alpha!7", "", "main", "group/alpha", "frontend"),
            ("beta!8", "", "main", "group/beta", "backend"),
        ]
        changed_files = [
            ChangedFile(path=f"{prefix}/src/change.txt", additions=1, diff=f"+{prefix}")
            for prefix, _application, _branch, _project, _type in scope_specs
        ]
        related_mrs = [
            {
                "mr_id": prefix.rsplit("!", 1)[-1],
                "project_path": project,
                "responsible": "wen.yi",
                "project_type": project_type,
                "application": application,
                "target_branch": branch,
                "file_prefix": prefix,
            }
            for prefix, application, branch, project, project_type in scope_specs
        ]
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-9999",
                mr_id="multi-mr-8",
                changed_files=changed_files,
                metadata={"related_merge_requests": related_mrs},
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )

        split = split_result_by_responsible(result)
        self.assertEqual(8, len(split))
        scopes = {
            (
                item.review_input.metadata["application"],
                item.review_input.metadata["release_line"],
                item.review_input.metadata["split_report_scope_component"],
            )
            for item in split
        }
        self.assertIn(("iTrade Client", "7.5.0", "iTrade-Client-7.5.0"), scopes)
        self.assertIn(("iTrade Client", "7.5.1", "iTrade-Client-7.5.1"), scopes)
        self.assertIn(("DPS", "DPS9", "DPS9"), scopes)
        self.assertIn(("DPS", "DPS11", "DPS11"), scopes)
        self.assertIn(("WVAdmin", "1.0", "WVAdmin"), scopes)
        self.assertIn(("Services Terminal", "5.0", "Services-Terminal"), scopes)
        unmapped = [scope for scope in scopes if scope[0] == "Unmapped"]
        self.assertEqual(2, len(unmapped))
        self.assertEqual(2, len({scope[2] for scope in unmapped}))

    def test_mr_review_fingerprint_tracks_commit_changes_not_updated_time_only(self) -> None:
        base = {
            "mr_url": "https://gitlab.example.com/group/web/-/merge_requests/1247",
            "state": "merged",
            "project_path": "group/web",
            "source_branch": "bug/ECHNL-5673",
            "target_branch": "7.5.1",
            "commit": "abc123",
            "updated_at": "2026-07-10T10:00:00Z",
        }
        same_commit_later_update = {**base, "updated_at": "2026-07-10T11:00:00Z"}
        different_commit = {**base, "commit": "def456"}

        original = review_fingerprint_from_merge_requests([base])
        later = review_fingerprint_from_merge_requests([same_commit_later_update])
        changed = review_fingerprint_from_merge_requests([different_commit])

        self.assertEqual(original["stable_fingerprint"], later["stable_fingerprint"])
        self.assertNotEqual(original["fingerprint"], later["fingerprint"])
        self.assertNotEqual(original["stable_fingerprint"], changed["stable_fingerprint"])

    def test_rendered_report_contains_hidden_reuse_metadata(self) -> None:
        result = ReviewResult(
            review_input=ReviewInput(
                project="jira-issue",
                jira_key="ECHNL-1",
                mr_id="multi-mr-1",
                metadata={
                    "responsible": "wen.yi",
                    "web_report_owner": "wen.yi",
                    "review_fingerprint": "full-hash",
                    "review_stable_fingerprint": "stable-hash",
                    "review_fingerprint_items": [{"mr_url": "https://gitlab.example.com/group/web/-/merge_requests/1"}],
                    "review_stable_fingerprint_items": [{"mr_url": "https://gitlab.example.com/group/web/-/merge_requests/1"}],
                },
            ),
            findings=[],
            conclusion="Pass",
            risk_summary=[],
            test_suggestions=[],
        )

        markdown = render_markdown(result)

        self.assertIn("<!-- code_reviewer_metadata:", markdown)
        self.assertIn('"review_stable_fingerprint":"stable-hash"', markdown)
        self.assertIn('"web_report_owner":"wen.yi"', markdown)

        with tempfile.TemporaryDirectory() as temp:
            first = save_report(result, Path(temp))
            second = save_report(result, Path(temp))

        self.assertEqual(first.parent.name, "wen.yi")
        self.assertEqual(first.name, "ECHNL-1_pass.md")
        self.assertTrue(second.name.startswith("ECHNL-1_pass_rescan-"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _mr_input(iid: str, path: str) -> ReviewInput:
    return ReviewInput(
        project="project",
        mr_url=f"https://gitlab.example.com/group/project/-/merge_requests/{iid}",
        mr_id=iid,
        source_branch="bug/ECHNL-1",
        target_branch="release",
        changed_files=[ChangedFile(path=path, additions=1, diff="+change")],
        metadata={"gitlab_project_path": "group/project", "git_tools_project_match": "matched"},
    )


class _FakeGitLabClient(GitLabClient):
    def __init__(self) -> None:
        super().__init__("https://gitlab.example.com", token="test-token")
        self.requested_paths: list[str] = []

    def _request_json(self, path: str, method: str = "GET", payload: dict | None = None) -> object:
        self.requested_paths.append(path)
        if path == "/api/v4/projects/group%2Fproject/merge_requests/55":
            return {
                "iid": 55,
                "title": "ECHNL-5657 test MR",
                "description": "",
                "source_branch": "bug/ECHNL-5657",
                "target_branch": "11.2.83",
                "sha": "abc123",
                "author": {"name": "Tester"},
                "state": "merged",
            }
        if path == "/api/v4/projects/group%2Fproject/merge_requests/55/changes":
            return None
        if path == "/api/v4/projects/group%2Fproject/merge_requests/55/diffs":
            return [
                {
                    "old_path": "src/value.php",
                    "new_path": "src/value.php",
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ]
        raise AssertionError(f"Unexpected path: {path}")


if __name__ == "__main__":
    unittest.main()
