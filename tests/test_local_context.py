from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from code_reviewer.local_workspaces import WorkspaceEntry, _git_tools_entries_from_text
from code_reviewer.analyzer import _jira_involved_file_findings
from code_reviewer.config import DEFAULT_CC_SWITCH_PROVIDER, llm_config
from code_reviewer.gitlab_client import GitLabClient
from code_reviewer.jira_client import JiraIssue
from code_reviewer.models import ChangedFile, Finding, ReviewInput, ReviewResult
from code_reviewer.project_context import build_project_context
from code_reviewer.report import render_markdown, save_report, save_reports, split_result_by_responsible
from code_reviewer.repository_sync import RepositorySyncResult, _attach_codebase_memory
from code_reviewer.resource_optimizer import optimize_prompt_diff
from code_reviewer.review_service import (
    _attach_project_context,
    _balanced_project_context,
    _chunk_review_inputs_if_needed,
    _chunk_review_reason,
    _combine_jira_issue_review_inputs,
    _ignored_branch_type,
    _missing_remote_branch_error,
    review_fingerprint_from_merge_requests,
)
from code_reviewer.llm_provider import _call_codex_cli, preview_llm_prompt_budget
from code_reviewer.local_changes import (
    _ensure_mr_commits,
    _issue_branch_candidates,
    _remote_issue_branches,
    local_merge_request_changes,
)
from code_reviewer.process_utils import run_utf8
from web import _acquire_instance_lock


class LocalContextTests(unittest.TestCase):
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

    def test_near_budget_multi_mr_review_chunks_before_hard_limit(self) -> None:
        inputs = [
            ReviewInput(
                project="group/project",
                mr_id=str(index),
                metadata={
                    "responsible": "wen.yi",
                    "git_tools_project_name": "itrade-client",
                    "gitlab_project_path": "group/project",
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
            patch("code_reviewer.llm_provider.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout=b'{"findings":[],"notes":[]}',
                stderr=b"",
            )

            output = _call_codex_cli("\ufeff中文 prompt", "gpt-5.6-sol", 30)

        self.assertIn("findings", output)
        kwargs = run_mock.call_args.kwargs
        self.assertFalse(kwargs["text"])
        self.assertNotIn("encoding", kwargs)
        self.assertTrue(kwargs["input"].startswith("\ufeff中文".encode("utf-8")))
        command = kwargs["args"] if "args" in kwargs else run_mock.call_args.args[0]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("model_provider=\"codereviewer_http\"", command)
        self.assertIn('model_providers.codereviewer_http.env_key="OPENAI_API_KEY"', command)
        self.assertIn("model_providers.codereviewer_http.requires_openai_auth=false", command)
        self.assertIn("model_providers.codereviewer_http.supports_websockets=false", command)

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

    def test_multi_owner_responsible_expands_to_individual_reports(self) -> None:
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
                            "file_prefix": "group/web!1",
                        },
                        {
                            "mr_id": "2",
                            "mr_url": "https://gitlab.example.com/group/api/-/merge_requests/2",
                            "project_path": "group/api",
                            "responsible": "kevin.tan+sunny.cheng",
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
        by_owner = {item.review_input.metadata["responsible"]: item for item in split}

        self.assertEqual(sorted(by_owner), ["kevin.tan", "sunny.cheng", "wen.yi"])
        self.assertEqual([finding.title for finding in by_owner["wen.yi"].findings], ["Web issue"])
        self.assertEqual([finding.title for finding in by_owner["kevin.tan"].findings], ["API issue"])
        self.assertEqual([finding.title for finding in by_owner["sunny.cheng"].findings], ["API issue"])

        with tempfile.TemporaryDirectory() as temp:
            saved = save_reports(result, Path(temp))
            relative_paths = sorted(path.relative_to(temp).as_posix() for _report, path in saved)

        self.assertEqual(
            relative_paths,
            [
                "kevin.tan/ECHNL-5308_has-issue-critical.md",
                "sunny.cheng/ECHNL-5308_has-issue-critical.md",
                "wen.yi/ECHNL-5308_has-issue-high.md",
            ],
        )

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
