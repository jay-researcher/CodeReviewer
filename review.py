from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_reviewer.config import (
    app_config_int,
    app_config_str,
    apply_speed_profile,
    ensure_directories,
    git_tools_config_path,
    normalize_severity,
    report_output_dir,
    set_app_runtime_override,
)
from code_reviewer.network import check_network
from code_reviewer.llm_provider import _brief_error, _call_codex_cli, _resolve_codex_cli
from code_reviewer.report import render_markdown, save_report
from code_reviewer.review_service import (
    post_gitlab_comment,
    review_from_diff_text,
    review_from_git_repo,
    review_jira_issue_merge_requests,
    review_jira_issues_merge_requests,
    review_jira_filter_merge_requests,
    review_from_mr_url,
    review_issue_branches,
    list_reviewer_merge_requests,
    review_reviewer_merge_requests,
    review_sprint_merge_requests,
    parse_jira_issue_keys,
)
from code_reviewer.storage import append_review_history, save_to_gitnexus
from code_reviewer.repository_sync import sync_all_workspaces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a GitLab MR code review Markdown report.")
    parser.add_argument("--mr-url", help="GitLab merge request URL.")
    parser.add_argument("--repo", help="Local git repository path.")
    parser.add_argument("--context-repo", default="", help="Local full project path used as extra review context for MR/diff review.")
    parser.add_argument("--diff-file", help="Unified diff file path.")
    parser.add_argument("--project", default="", help="Project key/name.")
    parser.add_argument("--jira", default="", help="One Jira issue key or an English-comma-separated list, for example ECHNL-8888,ECHNL-8889.")
    parser.add_argument("--jira-filter", default="", help="Jira saved filter ID. Reviews MRs linked to all reviewable issues returned by the filter.")
    parser.add_argument("--sprint", default="", help="Sprint name/ID. Without another input source, reviews MRs linked to all Jira issues in the sprint.")
    parser.add_argument("--jira-project", default="", help="Jira project key for sprint mode. Defaults to config.yml app.jira.project_key.")
    parser.add_argument("--git-tools-config", default=str(git_tools_config_path()), help="Project config.yml path for sprint branch discovery.")
    parser.add_argument("--git-tools-groups", default="", help="Runtime override for config.yml app.git_tools.groups.")
    parser.add_argument("--workspace-config", default="", help="Runtime override for LOCAL_WORKSPACE_CONFIG.")
    parser.add_argument("--workspace-roots", default="", help="Runtime override for config.yml app.local_context.workspace_roots.")
    parser.add_argument("--workspace-projects", default="", help="Optional comma-separated GitLab project paths to include for local issue-branch review.")
    parser.add_argument("--auto-context-repo", default="", choices=["on", "off"], help="Auto attach local project context by GitLab project path when --context-repo is omitted.")
    parser.add_argument("--review-framework", default="", help="Runtime override for config.yml app.review.framework.")
    parser.add_argument("--review-template", default="", help="Runtime override for config.yml app.review.template_path.")
    parser.add_argument("--jira-prd-data", default=os.getenv("JIRA_PRD_DATA_DIR", ""), help="Local jira-prd data directory used as requirement context during review.")
    parser.add_argument("--jira-prd-context", default=os.getenv("JIRA_PRD_CONTEXT", ""), choices=["auto", "on", "off"], help="Attach local Jira/PRD issue context to review prompts.")
    parser.add_argument("--source-branch", default="", help="Source branch for local repo mode.")
    parser.add_argument("--target-branch", default="", help="Target branch for local repo mode.")
    parser.add_argument("--output", default="", help="Output Markdown report path.")
    parser.add_argument("--output-dir", default=os.getenv("REPORT_OUTPUT_DIR", ""), help="Directory for generated Markdown reports.")
    parser.add_argument("--report-language", default="", help="Runtime override for config.yml app.report.language, for example zh-CN or en.")
    parser.add_argument("--report-min-severity", default="", help="Runtime override for config.yml app.report.min_severity.")
    parser.add_argument("--speed", default="", choices=["standard", "fast"], help="LLM speed tier. Use fast for GPT-5.5 Fast / priority service tier.")
    parser.add_argument("--post-gitlab-comment", action="store_true", help="Post report summary to the MR.")
    parser.add_argument("--yes", action="store_true", help="Confirm writeback actions such as GitLab comments.")
    parser.add_argument("--network-check", action="store_true", help="Check GitLab/Codex/DeepSeek network posture.")
    parser.add_argument("--codex-check", action="store_true", help="Run a real tiny Codex CLI execution check.")
    parser.add_argument("--codex-check-timeout", type=int, default=int(os.getenv("CODEX_CHECK_TIMEOUT_SECONDS", "180")), help="Timeout for --codex-check.")
    parser.add_argument("--reviewer-mrs", action="store_true", help="Query and review MRs assigned to a reviewer.")
    parser.add_argument("--reviewer", default=os.getenv("REVIEWER_EMAIL", "jay.wince@tx-tech.com"), help="Reviewer email or username.")
    parser.add_argument("--reviewer-days", type=int, default=int(os.getenv("REVIEWER_LOOKBACK_DAYS", "7")), help="Lookback window in days.")
    parser.add_argument("--reviewer-state", default=os.getenv("REVIEWER_MR_STATE", "opened,merged"), help="GitLab MR state filter. Supports comma-separated values, for example opened,merged, or all.")
    parser.add_argument("--reviewer-limit", type=int, default=int(os.getenv("REVIEWER_MR_LIMIT", "100")), help="Maximum MRs to process.")
    parser.add_argument("--reviewer-list-only", action="store_true", help="Only list reviewer MRs; do not fetch diffs or run review.")
    parser.add_argument("--jira-mr-list-only", action="store_true", help="Only list MRs related to --jira; do not fetch diffs or run review.")
    parser.add_argument("--jira-filter-list-only", action="store_true", help="Only list MRs related to --jira-filter issues; do not fetch diffs or run review.")
    parser.add_argument("--jira-review-statuses", default="", help="Runtime override for config.yml app.review.jira_allowed_statuses. Use all to disable status filtering.")
    parser.add_argument("--sprint-state", default="", help="Runtime override for config.yml app.review.mr_states.")
    parser.add_argument("--sprint-limit", type=int, default=0, help="Runtime override for config.yml app.review.mr_limit.")
    parser.add_argument("--sprint-list-only", action="store_true", help="Only list sprint-linked MRs; do not fetch diffs or run review.")
    parser.add_argument("--sprint-fast-list", action="store_true", help="Fast sprint listing: skip Jira remote-link/dev-panel and branch scans.")
    parser.add_argument("--issue-branches", action="store_true", help="Review local Git branches matching --jira across configured local working copies.")
    parser.add_argument("--issue-branch-list-only", action="store_true", help="Only list matching local issue branches; do not run review.")
    parser.add_argument("--issue-branch-limit", type=int, default=0, help="Runtime override for config.yml app.review.issue_branch_limit.")
    parser.add_argument("--no-resume", action="store_true", help="Disable batch review resume checkpoints for this run.")
    parser.add_argument("--reset-resume", action="store_true", help="Clear the matching batch resume checkpoint before running.")
    parser.add_argument("--sync-repositories", action="store_true", help="Clone/fetch all repositories and configured branches from config.yml.")
    parser.add_argument("--sync-no-index", action="store_true", help="Skip codebase-memory-mcp indexing during --sync-repositories.")
    parser.add_argument("--sync-force", action="store_true", help="Ignore the in-process repository sync cache.")
    args = parser.parse_args(argv)

    if args.report_language:
        set_app_runtime_override("REPORT_LANGUAGE", args.report_language)
    if args.output_dir:
        os.environ["REPORT_OUTPUT_DIR"] = args.output_dir
    if args.report_min_severity:
        set_app_runtime_override("REPORT_MIN_SEVERITY", normalize_severity(args.report_min_severity))
    if args.speed:
        apply_speed_profile(args.speed, force=True)
    if args.git_tools_config:
        os.environ["GIT_TOOLS_CONFIG"] = args.git_tools_config
    if args.git_tools_groups:
        set_app_runtime_override("GIT_TOOLS_GROUPS", args.git_tools_groups)
    if args.workspace_config:
        os.environ["LOCAL_WORKSPACE_CONFIG"] = args.workspace_config
    if args.workspace_roots:
        set_app_runtime_override("LOCAL_WORKSPACE_ROOTS", args.workspace_roots)
    if args.auto_context_repo:
        os.environ["LOCAL_CONTEXT_AUTO"] = "0" if args.auto_context_repo == "off" else "1"
    if args.review_framework:
        set_app_runtime_override("REVIEW_FRAMEWORK", args.review_framework)
    if args.review_template:
        set_app_runtime_override("REVIEW_TEMPLATE_PATH", args.review_template)
    if args.jira_prd_data:
        os.environ["JIRA_PRD_DATA_DIR"] = args.jira_prd_data
    if args.jira_prd_context:
        os.environ["JIRA_PRD_CONTEXT"] = args.jira_prd_context
    if args.jira_review_statuses:
        set_app_runtime_override("JIRA_REVIEW_ALLOWED_STATUSES", args.jira_review_statuses)
    if args.no_resume:
        os.environ["REVIEW_RESUME"] = "0"
    if args.reset_resume:
        os.environ["REVIEW_RESET_RESUME"] = "1"
    if args.sprint_fast_list:
        set_app_runtime_override("SPRINT_JIRA_REMOTE_LINK_DISCOVERY", "0")
        set_app_runtime_override("SPRINT_JIRA_DEV_PANEL_DISCOVERY", "0")
        set_app_runtime_override("SPRINT_BRANCH_DISCOVERY", "0")

    ensure_directories()
    configured_jira_project = app_config_str("jira.project_key", "JIRA_PROJECT_KEY", "ECHNL")
    configured_mr_state = app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    configured_mr_limit = app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200)
    configured_issue_branch_limit = app_config_int("review.issue_branch_limit", "ISSUE_BRANCH_REVIEW_LIMIT", 200)
    if args.sync_repositories:
        results = sync_all_workspaces(
            groups=args.git_tools_groups,
            index=not args.sync_no_index,
            force=args.sync_force,
        )
        payload = [item.to_dict() for item in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2 if any(item.error for item in results) else 0
    if args.network_check:
        status = check_network()
        print(f"GitLab host: {status.gitlab_host}")
        print(f"GitLab port 443: {'open' if status.gitlab_port_open else 'closed'}")
        print(f"GitLab: {status.gitlab_hint}")
        print(f"Codex: {status.codex_hint}")
        print(f"DeepSeek: {status.deepseek_hint}")
        print(f"Recommended: {status.recommended_action}")
        return 0

    if args.codex_check:
        return _run_codex_check(args.codex_check_timeout)

    jira_keys: list[str] = []
    if args.jira:
        try:
            jira_keys = parse_jira_issue_keys(args.jira)
        except ValueError as exc:
            parser.error(str(exc))
    if len(jira_keys) > 1 and any(
        [args.mr_url, args.repo, args.diff_file, args.sprint, args.jira_filter, args.issue_branches]
    ):
        parser.error("Comma-separated --jira keys are only supported as a standalone Jira MR batch review input.")

    if args.jira_mr_list_only and not args.jira:
        parser.error("--jira-mr-list-only requires --jira.")
    if args.jira_filter_list_only and not args.jira_filter:
        parser.error("--jira-filter-list-only requires --jira-filter.")

    if args.reviewer_mrs:
        try:
            if args.reviewer_list_only:
                summary = list_reviewer_merge_requests(
                    reviewer=args.reviewer,
                    days=args.reviewer_days,
                    state=args.reviewer_state,
                    limit=args.reviewer_limit,
                )
            else:
                summary = review_reviewer_merge_requests(
                    reviewer=args.reviewer,
                    days=args.reviewer_days,
                    state=args.reviewer_state,
                    limit=args.reviewer_limit,
                    output_dir=report_output_dir(),
                )
        except Exception as exc:
            print(f"Reviewer MR query failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("errors"):
            return 2
        return 0

    if args.issue_branches:
        if not args.jira:
            parser.error("--issue-branches requires --jira.")
        try:
            summary = review_issue_branches(
                jira_key=args.jira,
                target_branch=args.target_branch,
                groups=args.git_tools_groups,
                projects=args.workspace_projects,
                limit=args.issue_branch_limit or configured_issue_branch_limit,
                list_only=args.issue_branch_list_only,
                output_dir=report_output_dir(),
            )
        except Exception as exc:
            print(f"Issue branch review failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("errors"):
            return 2
        return 0

    if args.jira_filter and not any([args.mr_url, args.repo, args.diff_file]):
        try:
            summary = review_jira_filter_merge_requests(
                filter_id=args.jira_filter,
                state=args.sprint_state or configured_mr_state,
                limit=args.sprint_limit or configured_mr_limit,
                list_only=args.jira_filter_list_only,
                output_dir=report_output_dir(),
                context_repo=args.context_repo or None,
            )
        except Exception as exc:
            print(f"Jira filter MR review failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("errors"):
            return 2
        return 0

    if args.sprint and not any([args.mr_url, args.repo, args.diff_file]):
        try:
            summary = review_sprint_merge_requests(
                sprint=args.sprint,
                jira_project_key=args.jira_project or configured_jira_project,
                state=args.sprint_state or configured_mr_state,
                limit=args.sprint_limit or configured_mr_limit,
                list_only=args.sprint_list_only,
                output_dir=report_output_dir(),
                context_repo=args.context_repo or None,
            )
        except Exception as exc:
            print(f"Sprint MR review failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("errors"):
            return 2
        return 0

    if args.jira and not any([args.mr_url, args.repo, args.diff_file]):
        try:
            if len(jira_keys) > 1:
                summary = review_jira_issues_merge_requests(
                    jira_keys=jira_keys,
                    state=args.sprint_state or configured_mr_state,
                    limit=args.sprint_limit or configured_mr_limit,
                    list_only=args.jira_mr_list_only,
                    output_dir=report_output_dir(),
                    context_repo=args.context_repo or None,
                )
            else:
                summary = review_jira_issue_merge_requests(
                    jira_key=jira_keys[0],
                    state=args.sprint_state or configured_mr_state,
                    limit=args.sprint_limit or configured_mr_limit,
                    list_only=args.jira_mr_list_only,
                    output_dir=report_output_dir(),
                    context_repo=args.context_repo or None,
                )
        except Exception as exc:
            print(f"Jira issue MR review failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary.get("errors"):
            return 2
        return 0

    try:
        if args.mr_url:
            result = review_from_mr_url(
                args.mr_url,
                jira_key=args.jira,
                sprint=args.sprint,
                context_repo=args.context_repo or None,
            )
        elif args.repo:
            result = review_from_git_repo(
                repo=Path(args.repo),
                source_branch=args.source_branch,
                target_branch=args.target_branch,
                project=args.project,
                jira_key=args.jira,
                sprint=args.sprint,
            )
        elif args.diff_file:
            diff_text = Path(args.diff_file).read_text(encoding="utf-8", errors="ignore")
            result = review_from_diff_text(
                diff_text=diff_text,
                project=args.project,
                jira_key=args.jira,
                sprint=args.sprint,
                source_branch=args.source_branch,
                target_branch=args.target_branch,
                context_repo=args.context_repo or None,
            )
        else:
            parser.error("Provide one input: --mr-url, --repo, --diff-file, --reviewer-mrs, --issue-branches, --jira, --jira-filter, or --sprint.")
    except Exception as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output) if args.output else None
    output_dir, output_name = _resolve_output(output, args.output_dir)
    report_path = save_report(
        result,
        output_dir,
        output_name,
        language=args.report_language,
    )
    gitnexus = save_to_gitnexus(result, report_path)
    append_review_history(result, report_path)
    markdown = render_markdown(result, language=args.report_language)

    if args.post_gitlab_comment:
        comment_mr_url = args.mr_url
        if not comment_mr_url:
            print("--post-gitlab-comment requires --mr-url.", file=sys.stderr)
            return 1
        if not args.yes:
            print("Writeback requires explicit confirmation. Re-run with --yes after reviewing the report.", file=sys.stderr)
            return 1
        try:
            post_gitlab_comment(comment_mr_url, markdown, confirmed=True)
        except Exception as exc:
            print(f"Report saved, but GitLab comment failed: {exc}", file=sys.stderr)
            return 2

    counts = result.severity_counts
    print(f"Report: {report_path}")
    print(f"GitNexus: {gitnexus['report_path']}")
    print(result.conclusion)
    print(
        "Findings: "
        f"{counts.get('Critical', 0)} Critical, "
        f"{counts.get('High', 0)} High, "
        f"{counts.get('Medium', 0)} Medium, "
        f"{counts.get('Low', 0)} Low, "
        f"{counts.get('Warning', 0)} Warning"
    )
    return 0

def _resolve_output(output: Path | None, output_dir: str) -> tuple[Path, str | None]:
    if output_dir:
        return Path(output_dir).expanduser(), output.name if output else None
    if output and _looks_like_directory(output):
        return output.expanduser(), None
    if output and str(output.parent) not in {"", "."}:
        return output.parent.expanduser(), output.name
    if output:
        return report_output_dir(), output.name
    return report_output_dir(), None


def _run_codex_check(timeout: int) -> int:
    codex = _resolve_codex_cli()
    print(f"Codex CLI: {codex or 'not found'}")
    if not codex:
        return 1
    try:
        text = _call_codex_cli(
            'Return strict JSON only: {"findings":[],"notes":["codex-check-ok"]}',
            app_config_str("llm.codex_model", "LLM_CODEX_MODEL", "gpt-5.6-sol"),
            timeout,
            reasoning_effort=app_config_str("llm.reasoning_effort", "LLM_REASONING_EFFORT", "High"),
            speed=app_config_str("llm.speed", "LLM_SPEED", "standard"),
            service_tier=app_config_str("llm.codex_service_tier", "LLM_CODEX_SERVICE_TIER", ""),
        )
    except Exception as exc:
        print(f"Codex check failed: {_brief_error(exc)}", file=sys.stderr)
        return 1
    print("Codex check passed.")
    print(text.strip()[:500])
    return 0


def _looks_like_directory(path: Path) -> bool:
    return path.exists() and path.is_dir() or (not path.suffix and str(path).rstrip("\\/") == str(path))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted. Re-run the same review.py command to continue from the last completed checkpoint.", file=sys.stderr)
        raise SystemExit(130)
