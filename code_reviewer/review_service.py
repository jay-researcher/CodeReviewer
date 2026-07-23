from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from .analyzer import _jira_involved_file_findings, analyze
from .association import association_to_metadata, parse_issue_association
from .config import (
    app_config_bool,
    app_config_int,
    app_config_list,
    app_config_str,
    apply_speed_profile,
    git_tools_config_path,
    load_projects,
    normalize_severity,
    report_output_dir,
    set_app_runtime_override,
)
from .diff_parser import parse_unified_diff
from .git_version_review import extract_git_version_lock_context, parse_build_summary, parse_git_version_repository_entries
from .gitlab_client import GitLabClient, detect_jira_key, parse_mr_url, parse_repository_url
from .jira_client import JiraClient, JiraIssue, select_current_sprint
from .local_workspaces import (
    WorkspaceEntry,
    git_tools_project_entries,
    normalize_project_path,
    resolve_workspace_for_project_path,
    workspace_entries_for_issue_review,
)
from .models import ChangedFile
from .models import ReviewInput, ReviewResult
from .project_context import attach_project_context
from .resource_optimizer import is_optimizable_build_resource
from .review_scope import ReviewScope, review_scope_for_merge_request
from .repository_sync import codebase_memory_change_context, sync_workspace
from .report import render_markdown, save_report, save_reports
from .resume import ResumeTracker, stable_resume_key
from .storage import append_review_history, save_to_gitnexus
from .llm_provider import preview_llm_prompt_budget


class ReviewCancelled(Exception):
    """Raised by Web/job progress callbacks when a review is stopped cooperatively."""


DB_CHANGE_HEADER_RE = re.compile(  # Header format consumed by locked DBChangeParser.php.
    r"^--\s*MODULE:\s*(?P<module>\w+),\s*VERSION:\s*(?P<version>[\da-z.\-+{}$_]+),\s*"
    r"COMPANY:\s*(?P<company>\w+),\s*ENV:\s*(?P<environment>\w+)\s*$",
    re.I,
)
DB_CHANGE_REFERENCE_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_./-]+\.(?:sql|js|php|sh))(?![\w.-])", re.I)


def _attach_project_context(review_input: ReviewInput, context_repo: Path | str | None) -> None:
    if context_repo:
        attach_project_context(review_input, context_repo, ref=review_input.target_branch)
        review_input.metadata["project_context_source"] = "explicit"
        return

    project_path = str(review_input.metadata.get("gitlab_project_path") or review_input.project or "")
    workspace = resolve_workspace_for_project_path(project_path, branch=review_input.target_branch)
    if not workspace:
        return
    try:
        sync_result = sync_workspace(workspace, branch=review_input.target_branch)
        review_input.metadata["repository_sync"] = sync_result.to_dict()
        if sync_result.error:
            base_sha = str(review_input.metadata.get("diff_base_sha") or "").strip()
            if base_sha and _missing_remote_branch_error(sync_result.error):
                try:
                    attach_project_context(review_input, workspace.local_path, ref=base_sha)
                except Exception as fallback_exc:
                    review_input.metadata["project_context_error"] = (
                        f"{sync_result.error}; exact MR base context fallback failed: {fallback_exc}"
                    )
                    if app_config_bool("local_context.repository_sync_required", "REPOSITORY_SYNC_REQUIRED", True):
                        raise RuntimeError(review_input.metadata["project_context_error"]) from fallback_exc
                    return
                review_input.metadata["project_context_sync_warning"] = sync_result.error
                review_input.metadata["project_context_source"] = "local-workspace-exact-mr-base"
                review_input.metadata["project_context_branch"] = review_input.target_branch
                review_input.metadata["codebase_memory_status"] = (
                    "skipped: target branch unavailable; exact MR base commit context used"
                )
                review_input.metadata["repository_sync_fallback_ref"] = base_sha
                return
            review_input.metadata["project_context_error"] = sync_result.error
            if app_config_bool("local_context.repository_sync_required", "REPOSITORY_SYNC_REQUIRED", True):
                raise RuntimeError(sync_result.error)
        attach_project_context(review_input, workspace.local_path, ref=review_input.target_branch)
        if sync_result.codebase_memory_context:
            existing = str(review_input.metadata.get("project_context") or "")
            changed_context = codebase_memory_change_context(
                sync_result.codebase_memory_project,
                [item.path for item in review_input.changed_files],
            )
            review_input.metadata["project_context"] = (
                f"{existing}\n\nCodebase Memory persistent architecture context "
                f"({sync_result.codebase_memory_project}):\n{sync_result.codebase_memory_context}"
                f"\n\nCodebase Memory changed-file dependency context:\n{changed_context or '-'}"
            ).strip()
        review_input.metadata["project_context_source"] = "local-workspace-auto"
        review_input.metadata["project_context_branch"] = review_input.target_branch
        review_input.metadata["codebase_memory_status"] = sync_result.index_status
    except Exception as exc:
        review_input.metadata["project_context_error"] = str(exc)
        if app_config_bool("local_context.repository_sync_required", "REPOSITORY_SYNC_REQUIRED", True):
            raise


def _missing_remote_branch_error(error: str) -> bool:
    text = (error or "").lower()
    return "couldn't find remote ref" in text or "remote ref does not exist" in text


def _attach_git_tools_project_match(review_input: ReviewInput) -> None:
    project_path = str(review_input.metadata.get("gitlab_project_path") or "")
    match = _git_tools_project_match(
        project_path,
        target_branch=review_input.target_branch,
        source_branch=review_input.source_branch,
    )
    review_input.metadata.update(
        {
            "git_tools_project_match": match["status"],
            "git_tools_project_path": match["project_path"],
            "git_tools_config": match["config"],
            "git_tools_config_project_count": match["configured_count"],
        }
    )
    if match.get("group"):
        review_input.metadata["git_tools_group"] = match["group"]
    if match.get("module"):
        review_input.metadata["git_tools_module"] = match["module"]
    if match.get("repository_url"):
        review_input.metadata["git_tools_repository_url"] = match["repository_url"]
    if match.get("responsible"):
        review_input.metadata["responsible"] = match["responsible"]
        review_input.metadata["git_tools_responsible"] = match["responsible"]
    if match.get("project_name"):
        review_input.metadata["project_name"] = match["project_name"]
        review_input.metadata["git_tools_project_name"] = match["project_name"]
    if match.get("project_type"):
        review_input.metadata["project_type"] = match["project_type"]
        review_input.metadata["git_tools_project_type"] = match["project_type"]
    if match.get("llm_model"):
        review_input.metadata["llm_model_config"] = match["llm_model"]
    if match.get("application"):
        review_input.metadata["application"] = match["application"]
    if match.get("release_line"):
        review_input.metadata["release_line"] = match["release_line"]
    if match.get("release_lines"):
        review_input.metadata["release_lines"] = match["release_lines"]
    if match.get("dev_branch"):
        review_input.metadata["dev_branch"] = match["dev_branch"]

    _attach_release_gate_project_scope(review_input)

    require_match = app_config_bool("git_tools.require_mr_match", "GIT_TOOLS_REQUIRE_MR_MATCH", False)
    release_role = str(review_input.metadata.get("release_gate_role") or "").strip().lower()
    require_release_match = (
        release_role == "git_version"
        and app_config_bool(
            "review.release_gate.require_project_match",
            "RELEASE_GATE_REQUIRE_PROJECT_MATCH",
            True,
        )
    )
    if (require_match or require_release_match) and match["status"] != "matched":
        raise ValueError(
            f"MR project {project_path} is not defined in {match['config']} "
            f"({match['configured_count']} configured GitLab project(s))."
        )


def _git_tools_project_match(
    project_path: str,
    *,
    target_branch: str = "",
    source_branch: str = "",
) -> dict[str, Any]:
    normalized = normalize_project_path(project_path)
    config_path = str(git_tools_config_path())
    entries = git_tools_project_entries()
    result: dict[str, Any] = {
        "status": "not-configured",
        "project_path": normalized,
        "config": config_path,
        "configured_count": len(entries),
        "group": "",
        "module": "",
        "repository_url": "",
        "responsible": "",
        "project_name": "",
        "project_type": "",
        "llm_model": "",
        "application": "",
        "release_line": "",
        "release_lines": [],
        "dev_branch": [],
    }
    if not normalized:
        result["status"] = "unknown"
        return result
    if not entries:
        return result

    matches = [entry for entry in entries if normalize_project_path(entry.project_path) == normalized]
    branch_candidates = [item.strip() for item in (target_branch, source_branch) if item.strip()]
    if len(matches) > 1 and branch_candidates:
        branch_matches = [
            entry
            for entry in matches
            if entry.branches
            and any(
                fnmatchcase(branch.lower(), pattern.lower())
                for branch in branch_candidates
                for pattern in entry.branches
            )
        ]
        if branch_matches:
            matches = branch_matches

    if len(matches) > 1:
        # The iTrade source repository intentionally appears once per parallel
        # release line. If no configured branch glob selects one entry, retain
        # only shared metadata and let ReviewScope infer the line from the real
        # source/target branch. Never silently inherit the first configured line.
        shared_fields = (
            "group",
            "module",
            "repository_url",
            "responsible",
            "project_name",
            "project_type",
            "llm_model",
            "application",
        )
        result["status"] = "matched"
        for field in shared_fields:
            values = {str(getattr(entry, field) or "").strip() for entry in matches}
            if len(values) == 1:
                result[field] = values.pop()
        result["release_lines"] = sorted(
            {
                line
                for entry in matches
                for line in ([entry.release_line] if entry.release_line else entry.release_lines)
                if line
            }
        )
        result["dev_branch"] = list(
            dict.fromkeys(branch for entry in matches for branch in entry.dev_branch)
        )
        return result

    for entry in matches:
        result.update(
            {
                "status": "matched",
                "group": entry.group,
                "module": entry.module,
                "repository_url": entry.repository_url,
                "responsible": entry.responsible,
                "project_name": entry.project_name,
                "project_type": entry.project_type,
                "llm_model": entry.llm_model,
                "application": entry.application,
                "release_line": entry.release_line,
                "release_lines": entry.release_lines,
                "dev_branch": entry.dev_branch,
            }
        )
        return result

    result["status"] = "unmatched"
    return result


def _attach_release_gate_project_scope(review_input: ReviewInput) -> None:
    """Classify each release-resource MR by its configured GitLab project."""
    role = _release_gate_branch_role(review_input.source_branch)
    if not role and str(review_input.metadata.get("mr_type") or "").strip().upper() == "GIT_VERSION":
        role = "git_version"
    if not role:
        return

    metadata = review_input.metadata
    candidate = str(
        metadata.get("project_name")
        or metadata.get("git_tools_project_name")
        or metadata.get("git_tools_module")
        or metadata.get("git_tools_project_path")
        or review_input.project
        or "Project"
    ).strip()
    normalized = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
    aliases = {
        "wvadmin": "WVAdmin",
        "wvadmin-build": "WVAdmin",
        "itrade-client": "iTrade Client",
        "itrade-client-build": "iTrade Client",
        "service-terminal": "Services Terminal",
        "service-terminal-build": "Services Terminal",
        "services-terminal": "Services Terminal",
        "services-terminal-build": "Services Terminal",
        "dps": "DPS",
        "dps-build": "DPS",
    }
    metadata.update(
        {
            "release_gate_role": role,
            "release_gate_project": aliases.get(normalized, candidate),
            "release_gate_project_path": metadata.get("git_tools_project_path") or metadata.get("gitlab_project_path") or "",
            "release_gate_project_match": metadata.get("git_tools_project_match") or "unknown",
        }
    )


def _configured_dev_branches(project_match: dict[str, Any]) -> list[str]:
    configured = project_match.get("dev_branch") or []
    if isinstance(configured, str):
        values = [item.strip() for item in re.split(r"[,;]+", configured) if item.strip()]
    elif isinstance(configured, list):
        values = [str(item).strip() for item in configured if str(item).strip()]
    else:
        values = []
    if values:
        return values
    module = str(project_match.get("module") or "").strip()
    if module:
        return [module.upper()]
    project_path = str(project_match.get("project_path") or "").strip("/")
    project_key = project_path.rsplit("/", 1)[-1]
    return [project_key.upper()] if project_key else []


def _is_dev_version_branch(target_branch: str, project_match: dict[str, Any]) -> bool:
    branch = (target_branch or "").strip()
    if not branch:
        return False
    return branch.upper() in {item.upper() for item in _configured_dev_branches(project_match)}


def _dev_branch_exclusion(item: dict[str, Any], project_match: dict[str, Any]) -> dict[str, Any]:
    return {
        "jira_key": item.get("jira_key", ""),
        "mr_url": item.get("mr_url", ""),
        "source_branch": item.get("source_branch", ""),
        "target_branch": item.get("target_branch", ""),
        "git_tools_group": project_match.get("group", ""),
        "git_tools_module": project_match.get("module", ""),
        "dev_branch": _configured_dev_branches(project_match),
        "reason": "target branch is configured as development-version branch",
    }


def _mr_state_exclusion(item: dict[str, Any], actual_state: str, configured_state: str) -> dict[str, Any]:
    return {
        "jira_key": item.get("jira_key", ""),
        "mr_url": item.get("mr_url", ""),
        "source": item.get("source", ""),
        "project_path": item.get("project_path", ""),
        "source_branch": item.get("source_branch", ""),
        "target_branch": item.get("target_branch", ""),
        "actual_state": actual_state,
        "configured_state": configured_state,
        "reason": "merge request state does not match configured review state",
    }


def _configured_ignored_branch_types() -> list[str]:
    return app_config_list("review.ignored_branch_types", "REVIEW_IGNORED_BRANCH_TYPES", ["Company_Config", "Git_Version"])


def _release_gate_branch_prefixes(role: str) -> list[str]:
    return app_config_list(
        f"review.release_gate.branch_prefixes.{role}",
        "",
        [],
    )


def _release_gate_branch_role(source_branch: str) -> str:
    """Classify release-resource branches without treating version suffixes as a new type."""
    if not app_config_bool("review.release_gate.enabled", "RELEASE_GATE_ENABLED", True):
        return ""
    token = _normalize_branch_type(_branch_type_token(source_branch))
    if not token:
        return ""
    for role in ("company_config", "scr", "git_version"):
        for prefix in _release_gate_branch_prefixes(role):
            normalized_prefix = _normalize_branch_type(prefix)
            if token == normalized_prefix or token.startswith(f"{normalized_prefix}_"):
                return role
    return ""


def _release_gate_deferred_roles() -> set[str]:
    return {
        _normalize_branch_type(value)
        for value in app_config_list(
            "review.release_gate.deferred_roles",
            "RELEASE_GATE_DEFERRED_ROLES",
            ["company_config", "scr"],
        )
    }


def _release_gate_git_version_review_mode() -> str:
    return app_config_str(
        "review.release_gate.git_version_review_mode",
        "RELEASE_GATE_GIT_VERSION_REVIEW_MODE",
        "mr",
    ).strip().lower() or "mr"


def _branch_type_token(branch: str) -> str:
    value = (branch or "").strip().strip("/")
    if not value:
        return ""
    return re.split(r"[\\/]", value, maxsplit=1)[0]


def _normalize_branch_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def _ignored_branch_type(source_branch: str) -> str:
    release_role = _release_gate_branch_role(source_branch)
    if release_role:
        # GIT_VERSION is the release-gate review entry point, never a branch to skip.
        if release_role == "git_version":
            return ""
        if _normalize_branch_type(release_role) in _release_gate_deferred_roles():
            return release_role.upper()

    token = _branch_type_token(source_branch)
    if not token:
        return ""
    normalized = _normalize_branch_type(token)
    for configured in _configured_ignored_branch_types():
        if normalized == _normalize_branch_type(configured):
            return configured
    return ""


def _branch_type_exclusion(item: dict[str, Any], branch_type: str) -> dict[str, Any]:
    release_role = _release_gate_branch_role(str(item.get("source_branch") or ""))
    if release_role == "git_version":
        reason = "GIT_VERSION merge requests must be reviewed using explicit MR mode"
    elif release_role:
        reason = "build resource is deferred to GIT_VERSION release-gate review"
    else:
        reason = "source branch type is configured to be ignored"
    return {
        "jira_key": item.get("jira_key", ""),
        "mr_url": item.get("mr_url", ""),
        "source": item.get("source", ""),
        "project_path": item.get("project_path", ""),
        "source_branch": item.get("source_branch", ""),
        "target_branch": item.get("target_branch", ""),
        "mr_id": item.get("mr_id") or item.get("iid", ""),
        "head_sha": item.get("head_sha") or item.get("commit") or item.get("sha", ""),
        "base_sha": item.get("base_sha", ""),
        "merge_commit_sha": item.get("merge_commit_sha", ""),
        "squash_commit_sha": item.get("squash_commit_sha", ""),
        "ignored_branch_type": branch_type,
        "release_gate_role": release_role,
        "required_review_mode": "mr" if release_role == "git_version" else "",
        "configured_ignored_branch_types": _configured_ignored_branch_types(),
        "reason": reason,
    }


def _jira_sprint_branch_type_exclusion(item: dict[str, Any]) -> dict[str, Any] | None:
    """Apply branch routing for Jira/Sprint/Filter consolidated review only."""
    source_branch = str(item.get("source_branch") or "")
    release_role = _release_gate_branch_role(source_branch)
    if release_role == "git_version" and _release_gate_git_version_review_mode() == "mr":
        return _branch_type_exclusion(item, "GIT_VERSION")
    branch_type = _ignored_branch_type(source_branch)
    return _branch_type_exclusion(item, branch_type) if branch_type else None


def _review_input_ignored_branch_type_exclusion(review_input: ReviewInput, jira_key: str = "") -> dict[str, Any] | None:
    exclusion = _jira_sprint_branch_type_exclusion(
        {
            "jira_key": jira_key or review_input.jira_key,
            "mr_url": review_input.mr_url,
            "source_branch": review_input.source_branch,
            "target_branch": review_input.target_branch,
            "project_path": review_input.metadata.get("gitlab_project_path", ""),
        }
    )
    if exclusion:
        exclusion["changed_file_paths"] = [item.path for item in review_input.changed_files if item.path]
    return exclusion


def _hydrate_deferred_release_gate_resources(
    resources: list[dict[str, Any]],
    jira_key: str,
    sprint: str = "",
    cycle_id: str = "",
) -> list[dict[str, Any]]:
    """Fetch file paths for discovery-time Company Config/SCR exclusions."""
    hydrated: list[dict[str, Any]] = []
    for resource in resources:
        item = {
            **resource,
            **deferred_release_resource_identity(
                {
                    **resource,
                    "jira_key": resource.get("jira_key") or jira_key,
                    "sprint_id": resource.get("sprint_id") or sprint,
                    "cycle_id": resource.get("cycle_id") or cycle_id,
                }
            ),
        }
        if _normalize_branch_type(str(item.get("release_gate_role") or "")) not in {"company_config", "scr"}:
            hydrated.append(item)
            continue
        if item.get("changed_file_paths"):
            hydrated.append(item)
            continue
        mr_url = str(item.get("mr_url") or "")
        if not mr_url:
            hydrated.append(item)
            continue
        try:
            review_input = _review_input_from_mr_url(
                mr_url,
                jira_key=jira_key,
                sprint=sprint,
                attach_context=False,
                attach_jira_metadata=False,
            )
            item["changed_file_paths"] = [changed.path for changed in review_input.changed_files if changed.path]
            item.update(
                deferred_release_resource_identity(
                    {
                        **item,
                        "jira_key": jira_key,
                        "sprint_id": item.get("sprint_id") or sprint,
                        "cycle_id": item.get("cycle_id") or cycle_id,
                        "project_path": review_input.metadata.get("gitlab_project_path", ""),
                        "mr_id": review_input.mr_id,
                        "head_sha": review_input.commit,
                        "base_sha": review_input.metadata.get("diff_base_sha", ""),
                    }
                )
            )
        except Exception as exc:
            item["changed_files_error"] = str(exc)[:500]
        hydrated.append(item)
    return hydrated


def _review_input_dev_branch_exclusion(review_input: ReviewInput, jira_key: str = "") -> dict[str, Any] | None:
    project_path = str(review_input.metadata.get("gitlab_project_path") or review_input.project or "")
    project_match = _git_tools_project_match(
        project_path,
        target_branch=review_input.target_branch,
        source_branch=review_input.source_branch,
    )
    if not _is_dev_version_branch(review_input.target_branch, project_match):
        return None
    return _dev_branch_exclusion(
        {
            "jira_key": jira_key or review_input.jira_key,
            "mr_url": review_input.mr_url,
            "source_branch": review_input.source_branch,
            "target_branch": review_input.target_branch,
        },
        project_match,
    )


def _git_tools_project_match_for_mr_url(
    mr_url: str,
    *,
    target_branch: str = "",
    source_branch: str = "",
) -> dict[str, Any]:
    try:
        ref = parse_mr_url(mr_url)
    except Exception:
        return _git_tools_project_match("")
    return _git_tools_project_match(
        ref.project_path,
        target_branch=target_branch,
        source_branch=source_branch,
    )


def _batch_output_dir(output_dir: Path | None) -> Path:
    return output_dir or report_output_dir()


def _resume_tracker(scope: str, output_dir: Path | None, identity: dict[str, Any]) -> ResumeTracker:
    return ResumeTracker(scope, _batch_output_dir(output_dir), identity)


def _resume_path(tracker: ResumeTracker) -> str:
    return str(tracker.path) if tracker.enabled else ""


def _print_batch_start(name: str, total: int, output_dir: Path, tracker: ResumeTracker) -> None:
    print(f"Batch: {name}; total={total}", file=sys.stderr, flush=True)
    print(f"Output dir: {output_dir}", file=sys.stderr, flush=True)
    if tracker.enabled:
        print(f"Resume state: {tracker.path}", file=sys.stderr, flush=True)
    else:
        print("Resume state: disabled", file=sys.stderr, flush=True)


def _print_done(index: int, total: int, label: str, item: dict[str, Any], *, status: str = "DONE") -> None:
    report = str(item.get("report") or "-")
    reports = item.get("reports") if isinstance(item.get("reports"), list) else []
    if len(reports) > 1:
        report = f"{report} (+{len(reports) - 1} more)"
    counts = _format_severity_counts(item.get("severity_counts"))
    findings = item.get("finding_count", "-")
    print(
        f"[{index}/{total}] {status} {label} -> report: {report} "
        f"({counts}; findings={findings})",
        file=sys.stderr,
        flush=True,
    )


def _print_skipped(index: int, total: int, label: str, item: dict[str, Any]) -> None:
    report = str(item.get("report") or "-")
    reports = item.get("reports") if isinstance(item.get("reports"), list) else []
    if len(reports) > 1:
        report = f"{report} (+{len(reports) - 1} more)"
    counts = _format_severity_counts(item.get("severity_counts"))
    findings = item.get("finding_count", "-")
    print(
        f"[{index}/{total}] SKIP DONE {label} -> report: {report} "
        f"({counts}; findings={findings})",
        file=sys.stderr,
        flush=True,
    )


def _print_failed(index: int, total: int, label: str, error: str) -> None:
    print(f"[{index}/{total}] FAILED {label}: {error}", file=sys.stderr, flush=True)


def _print_dev_branch_skip(index: int, total: int, label: str, target_branch: str, dev_branches: list[str]) -> None:
    branches = ", ".join(dev_branches) or "-"
    print(
        f"[{index}/{total}] SKIP DEV-BRANCH {label}: target={target_branch or '-'} configured={branches}",
        file=sys.stderr,
        flush=True,
    )


def _print_mr_state_skip(index: int, total: int, label: str, actual_state: str, configured_state: str) -> None:
    print(
        f"[{index}/{total}] SKIP MR-STATE {label}: state={actual_state or '-'} configured={configured_state or '-'}",
        file=sys.stderr,
        flush=True,
    )


def _print_branch_type_skip(index: int, total: int, label: str, source_branch: str, branch_type: str) -> None:
    print(
        f"[{index}/{total}] SKIP BRANCH-TYPE {label}: source={source_branch or '-'} ignored={branch_type or '-'}",
        file=sys.stderr,
        flush=True,
    )


def _progress(progress: Any, event: str, message: str, **data: Any) -> None:
    if not progress:
        return
    try:
        progress({"event": event, "message": message, **data})
    except ReviewCancelled:
        raise
    except Exception:
        pass


def _emit_discovery_progress(progress: Any, label: str, discovered: dict[str, Any], **data: Any) -> None:
    mr_count = len(discovered.get("mrs") or [])
    state_skipped = list(discovered.get("excluded_state_mrs") or [])
    dev_skipped = list(discovered.get("excluded_dev_branch_mrs") or [])
    branch_type_skipped = list(discovered.get("excluded_branch_type_mrs") or [])
    suffix_parts = []
    if state_skipped:
        suffix_parts.append(f"{len(state_skipped)} state skipped")
    if dev_skipped:
        suffix_parts.append(f"{len(dev_skipped)} dev-branch skipped")
    if branch_type_skipped:
        suffix_parts.append(f"{len(branch_type_skipped)} branch-type skipped")
    suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
    _progress(
        progress,
        "discover",
        f"Discovered {mr_count} MR(s) for {label}{suffix}",
        mr_count=mr_count,
        state_skipped=len(state_skipped),
        dev_branch_skipped=len(dev_skipped),
        branch_type_skipped=len(branch_type_skipped),
        **data,
    )
    for item in state_skipped[:20]:
        _progress(
            progress,
            "skip-state",
            (
                f"SKIP MR-STATE {item.get('jira_key', '')}: {item.get('mr_url', '')} "
                f"(state={item.get('actual_state') or '-'}; allowed={item.get('configured_state') or '-'})"
            ),
            jira_key=item.get("jira_key", ""),
            mr_url=item.get("mr_url", ""),
            actual_state=item.get("actual_state", ""),
            configured_state=item.get("configured_state", ""),
        )
    for item in branch_type_skipped[:20]:
        _progress(
            progress,
            "skip-branch-type",
            (
                f"SKIP BRANCH-TYPE {item.get('jira_key', '')}: {item.get('mr_url', '')} "
                f"(source={item.get('source_branch') or '-'}; ignored={item.get('ignored_branch_type') or '-'})"
            ),
            jira_key=item.get("jira_key", ""),
            mr_url=item.get("mr_url", ""),
            source_branch=item.get("source_branch", ""),
            ignored_branch_type=item.get("ignored_branch_type", ""),
        )


def _save_review_reports(
    result: ReviewResult,
    output_dir: Path,
    filename: str | None = None,
    language: str | None = None,
    report_owner: str = "",
) -> dict[str, Any]:
    if report_owner:
        result.review_input.metadata["web_report_owner"] = report_owner
    saved = save_reports(result, output_dir, filename=filename, language=language)
    reports: list[dict[str, Any]] = []
    for saved_result, report_path in saved:
        gitnexus = save_to_gitnexus(saved_result, report_path)
        append_review_history(saved_result, report_path)
        reports.append(
            {
                "path": str(report_path),
                "name": report_path.name,
                "gitnexus_report": gitnexus["report_path"],
                "responsible": saved_result.review_input.metadata.get("responsible", ""),
                "severity_counts": saved_result.severity_counts,
                "finding_count": len(saved_result.findings),
                "mr_count": len(saved_result.review_input.metadata.get("related_merge_requests") or []),
            }
        )
    primary = reports[0] if reports else {}
    return {
        "saved": saved,
        "reports": reports,
        "report": str(primary.get("path") or ""),
        "report_name": str(primary.get("name") or ""),
        "gitnexus_report": str(primary.get("gitnexus_report") or ""),
    }


def _deferred_resource_in_review_scope(item: dict[str, Any], scope: object) -> bool:
    resource_scope = review_scope_for_merge_request(item)
    application = str(getattr(scope, "application", "") or "")
    release_line = str(getattr(scope, "release_line", "") or "")
    if resource_scope.application != application:
        return False
    if resource_scope.release_line in {"", "Unmapped release line"}:
        return application == "Unmapped" or release_line in {"", "Unmapped release line"}
    return resource_scope.release_line == release_line


def _review_scope_file_paths(
    inputs: list[ReviewInput], resources: list[dict[str, Any]]
) -> list[str]:
    paths = [changed.path for review_input in inputs for changed in review_input.changed_files if changed.path]
    paths.extend(
        str(path)
        for resource in resources
        for path in (resource.get("changed_file_paths") or [])
        if str(path).strip()
    )
    return list(dict.fromkeys(path.replace("\\", "/") for path in paths))


def _review_deferred_scope_for_issue(
    *,
    issue: JiraIssue,
    scope: ReviewScope,
    resources: list[dict[str, Any]],
    other_scope_file_paths: list[str],
    output_dir: Path,
    report_owner: str,
    sprint: str,
    run_group_id: str,
) -> dict[str, Any]:
    related_mrs: list[dict[str, Any]] = []
    for resource in resources:
        related_mrs.append(
            {
                "mr_url": resource.get("mr_url", ""),
                "mr_id": resource.get("mr_id") or resource.get("iid", ""),
                "state": resource.get("state") or resource.get("status", ""),
                "project": resource.get("project") or resource.get("project_path", ""),
                "project_path": resource.get("project_path") or resource.get("project", ""),
                "source_branch": resource.get("source_branch", ""),
                "target_branch": resource.get("target_branch", ""),
                "commit": resource.get("head_sha") or resource.get("commit", ""),
                "head_sha": resource.get("head_sha") or resource.get("commit", ""),
                "base_sha": resource.get("base_sha", ""),
                "file_count": len(resource.get("changed_file_paths") or []),
                "application": scope.application,
                "release_line": scope.release_line,
                "release_gate_role": resource.get("release_gate_role", ""),
                "responsible": resource.get("responsible", ""),
            }
        )
    paths = _review_scope_file_paths([], resources)
    review_input = ReviewInput(
        project="jira-issue",
        mr_id=f"deferred-{scope.filename_component}",
        jira_key=issue.key,
        sprint=sprint or issue.sprint,
        source_branch="deferred-release-resources",
        target_branch=scope.release_line,
        commit="deferred",
        title=issue.summary,
        changed_files=[ChangedFile(path=path) for path in paths],
        metadata={
            "review_input_mode": "jira-deferred-release-scope",
            "network_stage": "jira-issue-deferred-release-scope",
            "mr_type": "DEFERRED_RELEASE_RESOURCES",
            "related_merge_requests": related_mrs,
            "deferred_release_gate_resources": resources,
            "deferred_scope_report": True,
            "other_review_scope_file_paths": other_scope_file_paths,
            "jira_summary": issue.summary,
            "jira_description": issue.final_description,
            "jira_status": issue.status,
            "jira_issue_type": issue.issue_type,
            "jira_components": issue.components,
            "jira_responsibles": issue.responsibles,
            "application": scope.application,
            "applications": [scope.application],
            "release_line": scope.release_line,
            "release_lines": [scope.release_line],
            "run_group_id": run_group_id,
        },
    )
    _attach_review_scope_metadata(review_input, issue)
    _apply_jira_scope_responsible(review_input, issue, resources)
    findings = _jira_involved_file_findings(review_input)
    result = ReviewResult(
        review_input=review_input,
        findings=findings,
        conclusion="Blocking traceability mismatch." if findings else "Deferred to GIT_VERSION release gate.",
        risk_summary=[
            "Company Config/SCR resources are recorded as a separate application scope and remain subject to GIT_VERSION release-gate review."
        ],
        test_suggestions=["Verify the final GIT_VERSION lock includes these deferred resource commits before release."],
    )
    saved_reports = _save_review_reports(result, output_dir, report_owner=report_owner)
    reviewed_item = _reviewed_item_from_result(
        issue,
        result,
        saved_reports,
        "deferred-release-scope",
        len(resources),
    )
    reviewed_item.update(
        {
            "code_mr_count": 0,
            "deferred_resource_count": len(resources),
            "deferred_release_gate_resources": resources,
            "issue_review_status": "report-generated",
            "code_review_status": "complete",
            "release_gate_status": "pending",
        }
    )
    return reviewed_item


def _review_fetched_inputs_for_issue(
    *,
    issue: JiraIssue,
    fetched_inputs: list[ReviewInput],
    discovered_items: list[dict[str, Any]],
    configured_project_paths: list[str],
    output_dir: Path,
    progress: Any,
    context_repo: Path | str | None = None,
    sprint: str = "",
    index: int = 1,
    total: int = 1,
    report_owner: str = "",
    deferred_release_gate_resources: list[dict[str, Any]] | None = None,
    _scope_partitioned: bool = False,
    _run_group_id: str = "",
    _other_scope_file_paths: list[str] | None = None,
) -> dict[str, Any]:
    run_group_seed = "|".join(
        [
            issue.key,
            sprint or issue.sprint,
            min((item.generated_at.isoformat() for item in fetched_inputs), default=""),
        ]
        + sorted(f"{item.metadata.get('gitlab_project_path', item.project)}!{item.mr_id}@{item.commit}" for item in fetched_inputs)
    )
    run_group_id = _run_group_id or f"rg-{hashlib.sha256(run_group_seed.encode('utf-8')).hexdigest()[:20]}"
    if not _scope_partitioned:
        scoped_inputs: dict[ReviewScope, list[ReviewInput]] = {}
        for review_input in fetched_inputs:
            scope = review_scope_for_merge_request(
                {
                    **review_input.metadata,
                    "project": review_input.project,
                    "project_path": review_input.metadata.get("gitlab_project_path") or review_input.project,
                    "mr_id": review_input.mr_id,
                    "source_branch": review_input.source_branch,
                    "target_branch": review_input.target_branch,
                }
            )
            scoped_inputs.setdefault(scope, []).append(review_input)
        scoped_deferred: dict[ReviewScope, list[dict[str, Any]]] = {}
        for item in deferred_release_gate_resources or []:
            if not isinstance(item, dict):
                continue
            scoped_deferred.setdefault(review_scope_for_merge_request(item), []).append(item)
        all_scopes = list(dict.fromkeys([*scoped_inputs, *scoped_deferred]))
        if not scoped_inputs and all_scopes:
            scope = all_scopes[0]
            _progress(
                progress,
                "scope-reviewing",
                f"Generating deferred release-scope report for {issue.key}: {scope.filename_component}",
                index=index,
                total=total,
                jira_key=issue.key,
                application=scope.application,
                release_line=scope.release_line,
                mr_count=0,
                deferred_resource_count=len(scoped_deferred.get(scope, [])),
            )
            return _review_deferred_scope_for_issue(
                issue=issue,
                scope=scope,
                resources=scoped_deferred.get(scope, []),
                other_scope_file_paths=[],
                output_dir=output_dir,
                report_owner=report_owner,
                sprint=sprint,
                run_group_id=run_group_id,
            )
        if len(all_scopes) > 1:
            all_reports: list[dict[str, Any]] = []
            aggregate_counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
            aggregate_findings = 0
            aggregate_mrs: list[dict[str, Any]] = []
            gitnexus_report = ""
            scope_paths = {
                scope: _review_scope_file_paths(scoped_inputs.get(scope, []), scoped_deferred.get(scope, []))
                for scope in all_scopes
            }
            for scope in all_scopes:
                scope_inputs = scoped_inputs.get(scope, [])
                scoped_deferred_resources = scoped_deferred.get(scope, [])
                other_scope_paths = [
                    path
                    for other_scope, paths in scope_paths.items()
                    if other_scope != scope
                    for path in paths
                ]
                _progress(
                    progress,
                    "scope-reviewing",
                    f"Reviewing {issue.key} for {scope.filename_component}",
                    index=index,
                    total=total,
                    jira_key=issue.key,
                    application=scope.application,
                    release_line=scope.release_line,
                    mr_count=len(scope_inputs),
                )
                if scope_inputs:
                    scoped_result = _review_fetched_inputs_for_issue(
                        issue=issue,
                        fetched_inputs=scope_inputs,
                        discovered_items=discovered_items,
                        configured_project_paths=configured_project_paths,
                        output_dir=output_dir,
                        progress=progress,
                        context_repo=context_repo,
                        sprint=sprint,
                        index=index,
                        total=total,
                        report_owner=report_owner,
                        deferred_release_gate_resources=scoped_deferred_resources,
                        _scope_partitioned=True,
                        _run_group_id=run_group_id,
                        _other_scope_file_paths=other_scope_paths,
                    )
                else:
                    scoped_result = _review_deferred_scope_for_issue(
                        issue=issue,
                        scope=scope,
                        resources=scoped_deferred_resources,
                        other_scope_file_paths=other_scope_paths,
                        output_dir=output_dir,
                        report_owner=report_owner,
                        sprint=sprint,
                        run_group_id=run_group_id,
                    )
                all_reports.extend(scoped_result.get("reports") or [])
                for severity, count in (scoped_result.get("severity_counts") or {}).items():
                    aggregate_counts[severity] = aggregate_counts.get(severity, 0) + int(count or 0)
                aggregate_findings += int(scoped_result.get("finding_count") or 0)
                aggregate_mrs.extend(scoped_result.get("mrs") or [])
                gitnexus_report = gitnexus_report or str(scoped_result.get("gitnexus_report") or "")
            primary = all_reports[0] if all_reports else {}
            return {
                "jira_key": issue.key,
                "jira_summary": issue.summary,
                "jira_status": issue.status,
                "review_mode": "application-release-line-scoped",
                "report": str(primary.get("path") or ""),
                "reports": all_reports,
                "gitnexus_report": gitnexus_report,
                "conclusion": _aggregate_conclusion(aggregate_counts),
                "severity_counts": aggregate_counts,
                "finding_count": aggregate_findings,
                "mr_count": len(fetched_inputs),
                "scope_count": len(all_scopes),
                "mrs": aggregate_mrs,
            }
    combined_input = _combine_jira_issue_review_inputs(
        issue=issue,
        mr_inputs=fetched_inputs,
        discovered_items=discovered_items,
        configured_project_paths=configured_project_paths,
        run_group_id=run_group_id,
    )
    if deferred_release_gate_resources:
        combined_input.metadata["deferred_release_gate_resources"] = deferred_release_gate_resources
    if _other_scope_file_paths:
        combined_input.metadata["other_review_scope_file_paths"] = _other_scope_file_paths
    if report_owner:
        combined_input.metadata["web_report_owner"] = report_owner
    combined_input.sprint = sprint or combined_input.sprint
    _attach_review_scope_metadata(combined_input, issue)
    _apply_jira_scope_responsible(combined_input, issue, deferred_release_gate_resources or [])
    if context_repo:
        _attach_project_context(combined_input, context_repo)
    _ensure_detailed_jira_review_runtime()

    budget = _preview_prompt_budget_no_raise(combined_input)
    _progress_prompt_budget(progress, issue.key, budget, index=index, total=total, stage="context-preflight")
    chunk_reason = _chunk_review_reason(fetched_inputs, budget)
    chunks = _chunk_review_inputs_if_needed(combined_input, fetched_inputs, budget, reason=chunk_reason)
    if len(chunks) <= 1:
        print(f"  Reviewing {issue.key} as one consolidated review across {len(fetched_inputs)} MR(s)", file=sys.stderr, flush=True)
        _progress(
            progress,
            "reviewing",
            f"Reviewing {issue.key} as one consolidated review across {len(fetched_inputs)} MR(s)",
            index=index,
            total=total,
            jira_key=issue.key,
            mr_count=len(fetched_inputs),
        )
        result = analyze(combined_input, progress=progress)
        _progress(progress, "saving", f"Saving report for {issue.key}", index=index, total=total, jira_key=issue.key)
        saved_reports = _save_review_reports(result, output_dir, report_owner=report_owner)
        return _reviewed_item_from_result(issue, result, saved_reports, "consolidated-mrs", len(fetched_inputs))

    _progress(
        progress,
        "chunk-start",
        f"Splitting {issue.key} into {len(chunks)} review chunk(s) because {chunk_reason or 'prompt context budget'}",
        index=index,
        total=total,
        jira_key=issue.key,
        chunk_count=len(chunks),
        mr_count=len(fetched_inputs),
        original_chars=budget.get("original_chars"),
        max_chars=budget.get("max_chars"),
    )
    print(
        f"  Splitting {issue.key} into {len(chunks)} review chunk(s) "
        f"({len(fetched_inputs)} MR(s); {chunk_reason or 'prompt context budget'}; "
        f"context {budget.get('original_chars', '-')}/{budget.get('max_chars', '-')} chars)",
        file=sys.stderr,
        flush=True,
    )

    all_reports: list[dict[str, Any]] = []
    aggregate_counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
    aggregate_findings = 0
    aggregate_mrs: list[dict[str, Any]] = []
    gitnexus_report = ""
    for chunk_index, chunk_inputs in enumerate(chunks, 1):
        chunk_input = _combine_jira_issue_review_inputs(
            issue=issue,
            mr_inputs=chunk_inputs,
            discovered_items=discovered_items,
            configured_project_paths=configured_project_paths,
            run_group_id=run_group_id,
        )
        if deferred_release_gate_resources:
            chunk_input.metadata["deferred_release_gate_resources"] = deferred_release_gate_resources
        if _other_scope_file_paths:
            chunk_input.metadata["other_review_scope_file_paths"] = _other_scope_file_paths
        if report_owner:
            chunk_input.metadata["web_report_owner"] = report_owner
        chunk_input.sprint = sprint or chunk_input.sprint
        _attach_review_scope_metadata(chunk_input, issue)
        _apply_jira_scope_responsible(chunk_input, issue, deferred_release_gate_resources or [])
        chunk_input.mr_id = f"multi-mr-{len(chunk_inputs)}-chunk-{chunk_index}"
        chunk_input.metadata["chunked_review"] = True
        chunk_input.metadata["chunk_index"] = chunk_index
        chunk_input.metadata["chunk_total"] = len(chunks)
        chunk_input.metadata["chunk_reason"] = chunk_reason or "prompt context budget"
        chunk_input.metadata["parent_context_budget"] = budget
        if context_repo:
            _attach_project_context(chunk_input, context_repo)
        chunk_budget = _preview_prompt_budget_no_raise(chunk_input)
        _progress_prompt_budget(progress, issue.key, chunk_budget, index=index, total=total, stage="chunk-context", chunk_index=chunk_index, chunk_total=len(chunks))
        _progress(
            progress,
            "chunk-reviewing",
            f"Reviewing {issue.key} chunk {chunk_index}/{len(chunks)} across {len(chunk_inputs)} MR(s)",
            index=index,
            total=total,
            jira_key=issue.key,
            chunk_index=chunk_index,
            chunk_total=len(chunks),
            mr_count=len(chunk_inputs),
        )
        result = analyze(chunk_input, progress=progress)
        _progress(
            progress,
            "chunk-saving",
            f"Saving {issue.key} chunk {chunk_index}/{len(chunks)} report",
            index=index,
            total=total,
            jira_key=issue.key,
            chunk_index=chunk_index,
            chunk_total=len(chunks),
        )
        saved_reports = _save_review_reports(result, output_dir, report_owner=report_owner)
        all_reports.extend(saved_reports["reports"])
        if not gitnexus_report:
            gitnexus_report = saved_reports["gitnexus_report"]
        for severity, count in result.severity_counts.items():
            aggregate_counts[severity] = aggregate_counts.get(severity, 0) + count
        aggregate_findings += len(result.findings)
        related = result.review_input.metadata.get("related_merge_requests") or []
        if isinstance(related, list):
            aggregate_mrs.extend(item for item in related if isinstance(item, dict))

    primary = all_reports[0] if all_reports else {}
    return {
        "jira_key": issue.key,
        "jira_summary": issue.summary,
        "jira_status": issue.status,
        "review_mode": "chunked-consolidated-mrs",
        "report": str(primary.get("path") or ""),
        "reports": all_reports,
        "gitnexus_report": gitnexus_report,
        "conclusion": _aggregate_conclusion(aggregate_counts),
        "severity_counts": aggregate_counts,
        "finding_count": aggregate_findings,
        "mr_count": len(fetched_inputs),
        "chunk_count": len(chunks),
        "chunk_strategy": "application+release-line",
        "context_budget": budget,
        "mrs": aggregate_mrs,
    }


def _reviewed_item_from_result(
    issue: JiraIssue,
    result: ReviewResult,
    saved_reports: dict[str, Any],
    review_mode: str,
    mr_count: int,
) -> dict[str, Any]:
    return {
        "jira_key": issue.key,
        "jira_summary": issue.summary,
        "jira_status": issue.status,
        "review_mode": review_mode,
        "report": saved_reports["report"],
        "reports": saved_reports["reports"],
        "gitnexus_report": saved_reports["gitnexus_report"],
        "conclusion": result.conclusion,
        "severity_counts": result.severity_counts,
        "finding_count": len(result.findings),
        "mr_count": mr_count,
        "mrs": result.review_input.metadata.get("related_merge_requests", []),
        "context_budget": result.review_input.metadata.get("llm_context_budget", {}),
    }


def _preview_prompt_budget_no_raise(review_input: ReviewInput) -> dict[str, Any]:
    try:
        return preview_llm_prompt_budget(review_input)
    except RuntimeError as exc:
        budget = review_input.metadata.get("llm_context_budget")
        if isinstance(budget, dict):
            budget["error"] = str(exc)
            return budget
        return {
            "error": str(exc),
            "original_chars": len(review_input.raw_diff or ""),
            "max_chars": app_config_int("llm.prompt_max_chars", "LLM_PROMPT_MAX_CHARS", 160000),
        }


def _progress_prompt_budget(
    progress: Any,
    jira_key: str,
    budget: dict[str, Any],
    *,
    index: int = 1,
    total: int = 1,
    stage: str = "context-preflight",
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> None:
    if not isinstance(budget, dict) or not budget:
        return
    final_chars = budget.get("final_chars", budget.get("original_chars", "-"))
    max_chars = budget.get("max_chars", "-")
    message = f"LLM context preflight {final_chars}/{max_chars} chars"
    if chunk_index and chunk_total:
        message = f"LLM context chunk {chunk_index}/{chunk_total}: {final_chars}/{max_chars} chars"
    _progress(
        progress,
        stage,
        message,
        index=index,
        total=total,
        jira_key=jira_key,
        original_chars=budget.get("original_chars"),
        final_chars=budget.get("final_chars"),
        max_chars=budget.get("max_chars"),
        target_chars=budget.get("target_chars"),
        trimmed_chars=budget.get("trimmed_chars"),
        hard_truncated=budget.get("hard_truncated", False),
        sections=budget.get("sections") if isinstance(budget.get("sections"), dict) else {},
        error=budget.get("error", ""),
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )


def _chunk_review_inputs_if_needed(
    combined_input: ReviewInput,
    mr_inputs: list[ReviewInput],
    budget: dict[str, Any],
    reason: str = "",
) -> list[list[ReviewInput]]:
    if len(mr_inputs) <= 1:
        return [mr_inputs]
    if not app_config_bool("review.auto_chunk", "REVIEW_AUTO_CHUNK", True):
        return [mr_inputs]
    reason = reason or _chunk_review_reason(mr_inputs, budget)
    if not reason:
        return [mr_inputs]

    max_per_chunk = _chunk_max_mrs_per_chunk(reason)
    groups: dict[str, list[ReviewInput]] = {}
    order: list[str] = []
    for review_input in mr_inputs:
        key = _review_chunk_key(review_input)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(review_input)

    chunks: list[list[ReviewInput]] = []
    for key in order:
        items = groups[key]
        for offset in range(0, len(items), max_per_chunk):
            chunk = items[offset : offset + max_per_chunk]
            if chunk:
                chunks.append(chunk)
    return chunks if len(chunks) > 1 else [mr_inputs]


def _chunk_review_reason(mr_inputs: list[ReviewInput], budget: dict[str, Any]) -> str:
    if len(mr_inputs) <= 1:
        return ""
    if not app_config_bool("review.auto_chunk", "REVIEW_AUTO_CHUNK", True):
        return ""
    force_limit = app_config_int("review.chunk_mr_limit", "REVIEW_CHUNK_MR_LIMIT", 0)
    if force_limit > 0 and len(mr_inputs) > force_limit:
        return f"MR count {len(mr_inputs)} exceeds configured chunk limit {force_limit}"
    if _budget_original_over_limit(budget):
        return "prompt context is over hard budget"
    if _budget_near_limit(budget):
        return "prompt context is near the safe Codex budget"
    return ""


def _chunk_max_mrs_per_chunk(reason: str) -> int:
    if "near" in reason.lower():
        configured = app_config_int("review.chunk_near_budget_max_mrs_per_chunk", "REVIEW_CHUNK_NEAR_BUDGET_MAX_MRS_PER_CHUNK", 2)
        if configured > 0:
            return configured
    return max(1, app_config_int("review.chunk_max_mrs_per_chunk", "REVIEW_CHUNK_MAX_MRS_PER_CHUNK", 3))


def _budget_original_over_limit(budget: dict[str, Any]) -> bool:
    try:
        original = int(budget.get("original_chars") or 0)
        maximum = int(budget.get("max_chars") or 0)
    except (TypeError, ValueError):
        return False
    return maximum > 0 and original > maximum


def _budget_near_limit(budget: dict[str, Any]) -> bool:
    try:
        original = int(budget.get("original_chars") or 0)
        maximum = int(budget.get("max_chars") or 0)
    except (TypeError, ValueError):
        return False
    if original <= 0:
        return False
    threshold_chars = app_config_int("review.chunk_prompt_threshold_chars", "REVIEW_CHUNK_PROMPT_THRESHOLD_CHARS", 120000)
    ratio_text = app_config_str("review.chunk_prompt_threshold_ratio", "REVIEW_CHUNK_PROMPT_THRESHOLD_RATIO", "0.75")
    try:
        threshold_ratio = float(ratio_text)
    except (TypeError, ValueError):
        threshold_ratio = 0.75
    near_by_chars = threshold_chars > 0 and original >= threshold_chars
    near_by_ratio = maximum > 0 and threshold_ratio > 0 and original >= int(maximum * threshold_ratio)
    return near_by_chars or near_by_ratio


def _review_chunk_key(review_input: ReviewInput) -> str:
    scope = review_scope_for_merge_request(
        {
            **review_input.metadata,
            "project": review_input.project,
            "project_path": review_input.metadata.get("gitlab_project_path") or review_input.project,
            "mr_id": review_input.mr_id,
            "source_branch": review_input.source_branch,
            "target_branch": review_input.target_branch,
        }
    )
    return f"{scope.application.lower()}|{scope.release_line.lower()}|{scope.isolation_key.lower()}"


def _aggregate_conclusion(counts: dict[str, int]) -> str:
    if counts.get("Critical", 0):
        return "阻塞：存在 Critical 风险，建议修复后再合并。"
    if counts.get("High", 0):
        return "需修改：存在 High 风险，建议修复或给出明确豁免说明。"
    if sum(counts.values()):
        return "需复核：未发现阻塞问题，但仍有中低风险需要 reviewer 确认。"
    return "通过：未发现明显风险。"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


def _format_severity_counts(value: Any) -> str:
    counts = value if isinstance(value, dict) else {}
    return (
        f"Critical={counts.get('Critical', 0)} "
        f"High={counts.get('High', 0)} "
        f"Medium={counts.get('Medium', 0)} "
        f"Low={counts.get('Low', 0)} "
        f"Warning={counts.get('Warning', 0)}"
    )


def _mr_resume_key(mr_url: str) -> str:
    return stable_resume_key("mr-url", mr_url)


def _jira_group_resume_key(issue_key: str, items: list[dict[str, Any]]) -> str:
    revisions = sorted(
        mr_revision_identity(item)["revision_key"] or str(item.get("mr_url") or "")
        for item in items
        if item.get("mr_url") or item.get("project_path")
    )
    return stable_resume_key("jira-consolidated-mrs", issue_key.upper(), revisions)


def _issue_branch_resume_key(item: dict[str, Any], jira_key: str) -> str:
    return stable_resume_key(
        "issue-branch",
        jira_key.upper(),
        item.get("project_path", ""),
        item.get("repo", ""),
        item.get("source_branch", ""),
        item.get("target_branch", ""),
    )


def review_from_mr_url(
    mr_url: str,
    jira_key: str = "",
    sprint: str = "",
    context_repo: Path | str | None = None,
) -> ReviewResult:
    review_input = _review_input_from_mr_url(
        mr_url,
        jira_key=jira_key,
        sprint=sprint,
        context_repo=context_repo,
    )
    return analyze(review_input)


def review_release_gate_from_mr_url(
    mr_url: str,
    jira_key: str = "",
    sprint: str = "",
    context_repo: Path | str | None = None,
    progress: Any = None,
) -> ReviewResult:
    """Run the explicit GIT_VERSION release gate used by the Web workflow."""
    _progress(progress, "release-gate-fetch", f"Loading GIT_VERSION MR {mr_url}", mr_url=mr_url, sprint=sprint)
    review_input = _review_input_from_mr_url(
        mr_url,
        jira_key=jira_key,
        sprint=sprint,
        context_repo=context_repo,
    )
    if str(review_input.metadata.get("mr_type") or "").strip().upper() != "GIT_VERSION":
        raise ValueError(
            "Release Gate requires a GIT_VERSION MR containing versioned git_version.yml/build.yml resources."
        )
    gate = review_input.metadata.get("release_gate") or {}
    _progress(
        progress,
        "release-gate-preflight",
        f"Release Gate preflight {str(gate.get('status') or 'unknown').upper()}",
        release_gate_status=str(gate.get("status") or "unknown"),
        source_repository_count=int(gate.get("source_repository_count") or 0),
        build_resource_count=int(gate.get("build_resource_count") or 0),
        blocker_count=len(gate.get("errors") or []),
    )
    result = analyze(review_input)
    configured_blockers = {
        normalize_severity(value)
        for value in app_config_list(
            "review_workflow.blocking_severities",
            "REVIEW_BLOCKING_SEVERITIES",
            ["Critical", "High"],
        )
    }
    finding_blockers = [
        finding
        for finding in result.findings
        if normalize_severity(finding.severity) in configured_blockers
    ]
    gate["deterministic_status"] = str(gate.get("status") or "unknown").lower()
    gate["finding_blocker_count"] = len(finding_blockers)
    gate["status"] = "blocked" if gate.get("errors") or finding_blockers else "ready"
    review_input.metadata["release_gate"] = gate
    _progress(
        progress,
        "release-gate-analyzed",
        f"Release Gate analysis completed: {str(gate.get('status') or 'unknown').upper()}",
        release_gate_status=str(gate.get("status") or "unknown"),
        finding_count=len(result.findings),
        severity_counts=result.severity_counts,
    )
    return result


def _review_input_from_mr_url(
    mr_url: str,
    jira_key: str = "",
    sprint: str = "",
    context_repo: Path | str | None = None,
    attach_context: bool = True,
    attach_jira_metadata: bool = True,
) -> ReviewInput:
    client, _ = GitLabClient.from_mr_url(mr_url)
    review_input = client.review_input_from_mr(mr_url, jira_key=jira_key, sprint=sprint)
    review_input.metadata["network_stage"] = "gitlab-fetch-and-review"
    _attach_git_tools_project_match(review_input)
    if attach_jira_metadata:
        _attach_jira_issue_metadata(review_input)
    if attach_context:
        _attach_project_context(review_input, context_repo)
        target_context = review_input.metadata.get("current_target_context")
        if isinstance(target_context, dict):
            target_context["project_context_source"] = review_input.metadata.get("project_context_source", "")
            target_context["project_context_ref"] = review_input.metadata.get("project_context_ref", review_input.target_branch)
    attach_git_version_locked_repository_reviews(review_input, client)
    return review_input


def _attach_jira_issue_metadata(review_input: ReviewInput) -> None:
    if review_input.metadata.get("jira_description"):
        return
    jira_key = (review_input.jira_key or "").strip().upper()
    if not jira_key:
        return
    try:
        issue = JiraClient().fetch_issue(jira_key)
    except Exception as exc:
        review_input.metadata["jira_metadata_error"] = str(exc)
        return
    review_input.metadata.update(
        {
            "jira_summary": issue.summary,
            'jira_description': issue.final_description,
            "jira_status": issue.status,
            "jira_issue_type": issue.issue_type,
            "jira_components": issue.components,
            "jira_responsibles": issue.responsibles,
            "jira_sprint_memberships": [item.to_dict() for item in issue.sprint_memberships],
            "jira_current_sprint_id": issue.current_sprint_id,
            "jira_current_sprint_state": issue.current_sprint_state,
        }
    )
    _attach_review_scope_metadata(review_input, issue)


def _attach_review_scope_metadata(review_input: ReviewInput, issue: JiraIssue) -> None:
    """Separate actionable revision scope from target and historical context."""
    comments = [str(item).strip() for item in (issue.description_comments or []) if str(item).strip()]
    current_follow_up = comments[-1] if comments else ""
    historical_parts = [issue.description.strip()]
    historical_parts.extend(comments[:-1])
    related = review_input.metadata.get("related_merge_requests") or []
    if not related and review_input.mr_url:
        related = [
            {
                "project_path": review_input.metadata.get("gitlab_project_path") or review_input.project,
                "mr_id": review_input.mr_id,
                "head_sha": review_input.commit,
                "base_sha": review_input.metadata.get("diff_base_sha", ""),
                "source_branch": review_input.source_branch,
                "target_branch": review_input.target_branch,
            }
        ]
    cycle_id = str(review_input.metadata.get("cycle_id") or "").strip()
    selected_sprint = select_current_sprint(
        issue.sprint_memberships,
        preferred_id=review_input.sprint,
        preferred_name=review_input.sprint,
    )
    review_input.metadata.update(
        {
            "current_review_scope": {
                "jira_key": issue.key,
                "sprint": (selected_sprint.name if selected_sprint else "") or review_input.sprint or issue.sprint,
                "sprint_id": (selected_sprint.id if selected_sprint else "") or issue.current_sprint_id,
                "sprint_state": (selected_sprint.state if selected_sprint else "") or issue.current_sprint_state,
                "cycle_id": cycle_id,
                "current_follow_up_comment": current_follow_up,
                "merge_requests": related,
                "diff_policy": "Only base_sha to head_sha incremental diffs in this run are review targets.",
            },
            "current_target_context": {
                "policy": "Target-branch latest related code is context only, not a review finding source by itself.",
                "target_branches": sorted(
                    {
                        str(item.get("target_branch") or "").strip()
                        for item in related
                        if isinstance(item, dict) and str(item.get("target_branch") or "").strip()
                    }
                ),
                "project_context_source": review_input.metadata.get("project_context_source", ""),
            },
            "historical_requirement_context": {
                "original_description": issue.description.strip(),
                "previous_formal_comments": comments[:-1],
                "summary": "\n\n".join(part for part in historical_parts if part),
                "excludes_previous_cycle_diffs": True,
            },
        }
    )


def review_reviewer_merge_requests(
    reviewer: str,
    days: int = 7,
    state: str = "opened,merged",
    limit: int = 100,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    client = GitLabClient(base_url=_gitlab_base_url())
    mrs = client.list_merge_requests_for_reviewer(reviewer=reviewer, days=days, state=state, limit=limit)
    target_output_dir = _batch_output_dir(output_dir)
    tracker = _resume_tracker(
        "reviewer-mrs",
        target_output_dir,
        {"reviewer": reviewer, "days": days, "state": state, "limit": limit},
    )
    reviewed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped_completed = 0
    _print_batch_start("reviewer-mrs", len(mrs), target_output_dir, tracker)

    for index, mr in enumerate(mrs, 1):
        mr_url = str(mr.get("web_url", ""))
        resume_key = _mr_resume_key(mr_url)
        if tracker.is_done(resume_key):
            item = tracker.done_summary(resume_key)
            _print_skipped(index, len(mrs), mr_url, item)
            reviewed.append(item)
            skipped_completed += 1
            continue
        print(f"[{index}/{len(mrs)}] Processing {mr_url}", file=sys.stderr, flush=True)
        tracker.mark_started(resume_key, {"mr_url": mr_url})
        try:
            review_input = client.mr_web_url_to_review_input(mr)
            review_input.metadata["network_stage"] = "gitlab-reviewer-query"
            _attach_git_tools_project_match(review_input)
            item: dict[str, Any] = {
                "mr_url": review_input.mr_url or mr_url,
                "project": review_input.project,
                "mr_id": review_input.mr_id,
                "title": review_input.title,
                "jira_key": review_input.jira_key,
                "git_tools_project_match": review_input.metadata.get("git_tools_project_match", ""),
                "git_tools_group": review_input.metadata.get("git_tools_group", ""),
                "git_tools_module": review_input.metadata.get("git_tools_module", ""),
                "responsible": review_input.metadata.get("responsible", ""),
                "project_name": review_input.metadata.get("project_name", ""),
                "project_type": review_input.metadata.get("project_type", ""),
                "application": review_input.metadata.get("application", ""),
                "release_line": review_input.metadata.get("release_line", ""),
                "release_lines": review_input.metadata.get("release_lines", []),
                "llm_model_config": review_input.metadata.get("llm_model_config", ""),
            }
            _attach_project_context(review_input, None)
            attach_git_version_locked_repository_reviews(review_input, client)
            result = analyze(review_input)
            report_path = save_report(result, target_output_dir)
            gitnexus = save_to_gitnexus(result, report_path)
            append_review_history(result, report_path)
            item.update(
                {
                    "report": str(report_path),
                    "gitnexus_report": gitnexus["report_path"],
                    "conclusion": result.conclusion,
                    "severity_counts": result.severity_counts,
                    "finding_count": len(result.findings),
                }
            )
            reviewed.append(item)
            tracker.mark_done(resume_key, item)
            _print_done(index, len(mrs), review_input.mr_url or mr_url, item)
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, {"mr_url": mr_url})
            raise
        except Exception as exc:
            errors.append({"mr_url": mr_url, "error": str(exc)})
            tracker.mark_failed(resume_key, str(exc), {"mr_url": mr_url})
            _print_failed(index, len(mrs), mr_url, str(exc))

    return {
        "reviewer": reviewer,
        "days": days,
        "state": state,
        "found": len(mrs),
        "processed": len(reviewed),
        "skipped_completed": skipped_completed,
        "resume_state": _resume_path(tracker),
        "errors": errors,
        "items": reviewed,
    }


def _configured_jira_review_statuses() -> list[str]:
    values = app_config_list("review.jira_allowed_statuses", "JIRA_REVIEW_ALLOWED_STATUSES", ["Development Done"])
    if len(values) == 1 and values[0].lower() in {"", "*", "all", "any"}:
        return []
    by_key: dict[str, str] = {}
    for value in values:
        by_key.setdefault(value.lower(), value)
    return [by_key[key] for key in sorted(by_key)]


def _jira_issue_status_allowed(issue: JiraIssue) -> bool:
    allowed = _configured_jira_review_statuses()
    if not allowed:
        return True
    return issue.status.strip().lower() in {item.lower() for item in allowed}


def _filter_reviewable_jira_issues(issues: list[JiraIssue]) -> tuple[list[JiraIssue], list[dict[str, str]]]:
    allowed = _configured_jira_review_statuses()
    if not allowed:
        return issues, []
    allowed_text = ", ".join(allowed)
    reviewable: list[JiraIssue] = []
    skipped: list[dict[str, str]] = []
    for issue in issues:
        if _jira_issue_status_allowed(issue):
            reviewable.append(issue)
        else:
            skipped.append(
                {
                    "jira_key": issue.key,
                    "summary": issue.summary,
                    "status": issue.status,
                    "reason": f"Jira issue status is not in allowed review statuses: {allowed_text}",
                }
            )
    return reviewable, skipped


def review_sprint_merge_requests(
    sprint: str,
    jira_project_key: str = "ECHNL",
    state: str = "opened,merged",
    limit: int = 200,
    list_only: bool = False,
    output_dir: Path | None = None,
    context_repo: Path | str | None = None,
    progress: Any = None,
    report_owner: str = "",
    force_rerun: bool = False,
    workflow_review_mode: str = "issue",
) -> dict[str, Any]:
    jira_project_key = jira_project_key or app_config_str("jira.project_key", "JIRA_PROJECT_KEY", "ECHNL")
    state = state or app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    limit = int(limit or app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200))
    _progress(progress, "start", f"Loading Jira sprint {sprint}", sprint=sprint)
    jira = JiraClient()
    issues = jira.search_issues_by_sprint(sprint, project_key=jira_project_key)
    issues, skipped_issues = _filter_reviewable_jira_issues(issues)
    _progress(
        progress,
        "jira",
        f"Loaded {len(issues)} reviewable Jira issue(s) from sprint {sprint}",
        sprint=sprint,
        issue_count=len(issues),
        skipped_issue_count=len(skipped_issues),
    )
    return _review_issue_collection_merge_requests(
        issues=issues,
        source_kind="sprint",
        source_label=f"sprint {sprint}",
        resume_namespace="sprint-mrs",
        batch_title="sprint consolidated Jira reviews",
        state=state,
        limit=limit,
        list_only=list_only,
        output_dir=output_dir,
        context_repo=context_repo,
        progress=progress,
        report_owner=report_owner,
        force_rerun=force_rerun,
        source_metadata={
            "sprint": sprint,
            "jira_project_key": jira_project_key,
            "skipped_status_issues": skipped_issues,
            "workflow_review_mode": workflow_review_mode,
        },
        sprint=sprint,
    )


def review_jira_filter_merge_requests(
    filter_id: str,
    state: str = "opened,merged",
    limit: int = 200,
    list_only: bool = False,
    output_dir: Path | None = None,
    context_repo: Path | str | None = None,
    progress: Any = None,
    report_owner: str = "",
    force_rerun: bool = False,
) -> dict[str, Any]:
    value = (filter_id or "").strip()
    if not value:
        raise ValueError("--jira-filter is required for Jira filter review.")
    _progress(progress, "start", f"Loading Jira filter {value}", jira_filter=value)
    jira = JiraClient()
    state = state or app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    limit = int(limit or app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200))
    max_issues = app_config_int("jira.filter_max_issues", "JIRA_FILTER_MAX_ISSUES", 500)
    issues = jira.search_issues_by_filter_id(value, max_issues=max_issues)
    issues, skipped_issues = _filter_reviewable_jira_issues(issues)
    _progress(
        progress,
        "jira",
        f"Loaded {len(issues)} reviewable Jira issue(s) from filter {value}",
        jira_filter=value,
        issue_count=len(issues),
        skipped_issue_count=len(skipped_issues),
    )
    return _review_issue_collection_merge_requests(
        issues=issues,
        source_kind="jira-filter",
        source_label=f"Jira filter {value}",
        resume_namespace="jira-filter-mrs",
        batch_title="Jira filter consolidated Jira reviews",
        state=state,
        limit=limit,
        list_only=list_only,
        output_dir=output_dir,
        context_repo=context_repo,
        progress=progress,
        report_owner=report_owner,
        force_rerun=force_rerun,
        source_metadata={
            "jira_filter": value,
            "skipped_status_issues": skipped_issues,
        },
    )


def _review_issue_collection_merge_requests(
    *,
    issues: list[JiraIssue],
    source_kind: str,
    source_label: str,
    resume_namespace: str,
    batch_title: str,
    state: str,
    limit: int,
    list_only: bool,
    output_dir: Path | None,
    context_repo: Path | str | None,
    progress: Any,
    source_metadata: dict[str, Any],
    report_owner: str = "",
    force_rerun: bool = False,
    sprint: str = "",
) -> dict[str, Any]:
    jira = JiraClient()
    gitlab = GitLabClient(base_url=_gitlab_base_url())
    discovered = _discover_sprint_merge_requests(
        jira,
        gitlab,
        issues,
        state=state,
        limit=limit,
        progress=progress,
        source_label=source_label,
    )
    _emit_discovery_progress(progress, source_label, discovered)
    if list_only:
        return {
            **source_metadata,
            "source_kind": source_kind,
            "issue_count": len(issues),
            "mr_count": len(discovered["mrs"]),
            "issues_without_mrs": discovered["issues_without_mrs"],
            "excluded_dev_branch_mrs": discovered.get("excluded_dev_branch_mrs", []),
            "excluded_branch_type_mrs": discovered.get("excluded_branch_type_mrs", []),
            "excluded_state_mrs": discovered.get("excluded_state_mrs", []),
            "discovery_errors": discovered["errors"],
            "items": discovered["mrs"],
            "discovery_truncated": len(discovered["mrs"]) >= limit,
            "scope_issues": [
                {
                    "jira_key": issue.key,
                    "summary": issue.summary,
                    "jira_status": issue.status,
                    "sprint_name": issue.sprint,
                    "current_sprint_id": issue.current_sprint_id,
                    "current_sprint_state": issue.current_sprint_state,
                    "sprint_memberships": [membership.to_dict() for membership in issue.sprint_memberships],
                    "components": list(issue.components),
                }
                for issue in issues
            ],
        }

    target_output_dir = _batch_output_dir(output_dir)
    tracker = _resume_tracker(
        resume_namespace,
        target_output_dir,
        {**source_metadata, "state": state, "limit": limit},
    )
    reviewed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = list(discovered["errors"])
    excluded_dev_branch_mrs: list[dict[str, Any]] = list(discovered.get("excluded_dev_branch_mrs", []))
    excluded_branch_type_mrs: list[dict[str, Any]] = list(discovered.get("excluded_branch_type_mrs", []))
    excluded_state_mrs: list[dict[str, Any]] = list(discovered.get("excluded_state_mrs", []))
    excluded_cycle_revision_mrs: list[dict[str, Any]] = []
    skipped_completed = 0
    project_paths = _sprint_branch_project_paths(gitlab)
    issues_by_key = {issue.key.upper(): issue for issue in issues}
    grouped_items = _group_discovered_mrs_by_jira(discovered["mrs"])
    for item in excluded_branch_type_mrs:
        issue_key = str(item.get("jira_key") or "").upper()
        release_role = _normalize_branch_type(str(item.get("release_gate_role") or ""))
        if issue_key in issues_by_key and release_role in {"company_config", "scr"}:
            grouped_items.setdefault(issue_key, [])
    _print_batch_start(batch_title, len(grouped_items), target_output_dir, tracker)
    _progress(progress, "batch-start", f"Preparing consolidated review for {len(grouped_items)} Jira issue(s)", total=len(grouped_items), output_dir=str(target_output_dir), resume_state=_resume_path(tracker))
    for issue_index, (issue_key, issue_items) in enumerate(grouped_items.items(), 1):
        issue = issues_by_key.get(issue_key.upper())
        if not issue:
            error = "Jira issue was not found in the loaded issue collection."
            errors.append({"jira_key": issue_key, "stage": "sprint-issue-lookup", "error": error})
            _print_failed(issue_index, len(grouped_items), issue_key, error)
            _progress(progress, "failed", f"{issue_key}: {error}", index=issue_index, total=len(grouped_items), jira_key=issue_key, error=error)
            continue
        resume_key = _jira_group_resume_key(issue.key, issue_items)
        resume_item = {
            "jira_key": issue.key,
            "jira_summary": issue.summary,
            "mr_urls": [item.get("mr_url", "") for item in issue_items],
        }
        if not force_rerun and tracker.is_done(resume_key):
            item = tracker.done_summary(resume_key)
            _print_skipped(issue_index, len(grouped_items), f"{issue.key} ({len(issue_items)} MR(s))", item)
            _progress(progress, "skip-done", f"SKIP DONE {issue.key} ({len(issue_items)} MR(s))", index=issue_index, total=len(grouped_items), jira_key=issue.key, report=item.get("report", ""))
            reviewed.append(item)
            skipped_completed += 1
            continue
        print(
            f"[{issue_index}/{len(grouped_items)}] Preparing consolidated review for {issue.key}: "
            f"{len(issue_items)} MR(s)",
            file=sys.stderr,
            flush=True,
        )
        _progress(progress, "issue-start", f"Preparing consolidated review for {issue.key}: {len(issue_items)} MR(s)", index=issue_index, total=len(grouped_items), jira_key=issue.key, mr_count=len(issue_items))
        tracker.mark_started(resume_key, resume_item)
        fetched_inputs: list[ReviewInput] = []
        issue_excluded_count = 0
        try:
            for mr_index, item in enumerate(issue_items, 1):
                mr_url = item["mr_url"]
                print(f"  Fetching MR diff [{mr_index}/{len(issue_items)}]: {mr_url}", file=sys.stderr, flush=True)
                _progress(progress, "fetch-mr", f"Fetching MR diff [{mr_index}/{len(issue_items)}]: {mr_url}", index=issue_index, total=len(grouped_items), jira_key=issue.key, mr_index=mr_index, mr_total=len(issue_items), mr_url=mr_url)
                try:
                    fetched = _review_input_from_mr_url(
                        mr_url,
                        jira_key=issue.key,
                        sprint=sprint,
                        attach_context=context_repo is None,
                        attach_jira_metadata=False,
                    )
                    branch_type_exclusion = _review_input_ignored_branch_type_exclusion(fetched, issue.key)
                    if branch_type_exclusion:
                        excluded_branch_type_mrs.append(branch_type_exclusion)
                        issue_excluded_count += 1
                        _progress(
                            progress,
                            "skip-branch-type",
                            f"SKIP BRANCH-TYPE {issue.key}: {mr_url}",
                            index=issue_index,
                            total=len(grouped_items),
                            jira_key=issue.key,
                            mr_url=mr_url,
                            source_branch=fetched.source_branch,
                            ignored_branch_type=branch_type_exclusion.get("ignored_branch_type", ""),
                        )
                        continue
                    exclusion = _review_input_dev_branch_exclusion(fetched, issue.key)
                    if exclusion:
                        excluded_dev_branch_mrs.append(exclusion)
                        issue_excluded_count += 1
                        _print_dev_branch_skip(
                            issue_index,
                            len(grouped_items),
                            f"{issue.key} {mr_url}",
                            fetched.target_branch,
                            exclusion.get("dev_branch", []),
                        )
                        _progress(progress, "skip-dev-branch", f"SKIP DEV-BRANCH {issue.key}: {mr_url}", index=issue_index, total=len(grouped_items), jira_key=issue.key, mr_url=mr_url, target_branch=fetched.target_branch, dev_branch=exclusion.get("dev_branch", []))
                        continue
                    fetched_inputs.append(fetched)
                except Exception as exc:
                    errors.append({"jira_key": issue.key, "mr_url": mr_url, "stage": "fetch-mr", "error": str(exc)})
                    _print_failed(issue_index, len(grouped_items), f"{issue.key} fetch {mr_url}", str(exc))
                    _progress(progress, "failed", f"FAILED {issue.key} fetch {mr_url}: {exc}", index=issue_index, total=len(grouped_items), jira_key=issue.key, mr_url=mr_url, error=str(exc))
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, resume_item)
            raise
        fetched_inputs, cycle_exclusions = _select_fetched_cycle_revisions(issue.key, fetched_inputs)
        if cycle_exclusions:
            excluded_cycle_revision_mrs.extend(cycle_exclusions)
            issue_excluded_count += len(cycle_exclusions)
            _progress(
                progress,
                "skip-cycle-revision",
                f"SKIP {len(cycle_exclusions)} unchanged MR revision(s) already reviewed for {issue.key}",
                index=issue_index,
                total=len(grouped_items),
                jira_key=issue.key,
                excluded_revisions=cycle_exclusions,
            )
        deferred_resources = _hydrate_deferred_release_gate_resources(
            [
                item
                for item in excluded_branch_type_mrs
                if str(item.get("jira_key") or "").upper() == issue.key.upper()
                and _normalize_branch_type(str(item.get("release_gate_role") or ""))
                in {"company_config", "scr"}
            ],
            issue.key,
            sprint,
        )
        if not fetched_inputs:
            if deferred_resources:
                try:
                    reviewed_item = _review_fetched_inputs_for_issue(
                        issue=issue,
                        fetched_inputs=[],
                        discovered_items=issue_items,
                        configured_project_paths=project_paths,
                        output_dir=target_output_dir,
                        progress=progress,
                        context_repo=context_repo,
                        sprint=sprint,
                        index=issue_index,
                        total=len(grouped_items),
                        report_owner=report_owner,
                        deferred_release_gate_resources=deferred_resources,
                    )
                    reviewed.append(reviewed_item)
                    tracker.mark_done(resume_key, reviewed_item)
                    _print_done(
                        issue_index,
                        len(grouped_items),
                        f"{issue.key} ({len(deferred_resources)} deferred resource(s))",
                        reviewed_item,
                    )
                    _progress(
                        progress,
                        "done",
                        f"DONE {issue.key} ({len(deferred_resources)} deferred release resource(s))",
                        index=issue_index,
                        total=len(grouped_items),
                        jira_key=issue.key,
                        report=reviewed_item.get("report", ""),
                        reports=reviewed_item.get("reports", []),
                        deferred_resource_count=len(deferred_resources),
                    )
                except Exception as exc:
                    errors.append({"jira_key": issue.key, "stage": "deferred-scope-report", "error": str(exc)})
                    tracker.mark_failed(resume_key, str(exc), resume_item)
                    _print_failed(issue_index, len(grouped_items), issue.key, str(exc))
                    _progress(
                        progress,
                        "failed",
                        f"FAILED {issue.key} deferred report: {exc}",
                        index=issue_index,
                        total=len(grouped_items),
                        jira_key=issue.key,
                        error=str(exc),
                    )
                continue
            if issue_excluded_count:
                cycle_only = bool(cycle_exclusions) and len(cycle_exclusions) == issue_excluded_count
                excluded_item = {
                    "jira_key": issue.key,
                    "jira_summary": issue.summary,
                    "jira_status": issue.status,
                    "review_mode": "no-new-mr-revisions" if cycle_only else "excluded-mrs",
                    "mr_count": len(issue_items),
                    "excluded_dev_branch_count": issue_excluded_count,
                    "excluded_cycle_revision_count": len(cycle_exclusions),
                    "resume_status": "no-new-mr-revisions" if cycle_only else "excluded-mrs",
                    "conclusion": "No new MR revisions to review." if cycle_only else "All MR revisions were routed or excluded.",
                }
                reviewed.append(excluded_item)
                tracker.mark_done(resume_key, excluded_item)
                continue
            error = "No MR diffs were fetched for this Jira issue."
            tracker.mark_failed(resume_key, error, resume_item)
            _print_failed(issue_index, len(grouped_items), issue.key, error)
            _progress(progress, "failed", f"FAILED {issue.key}: {error}", index=issue_index, total=len(grouped_items), jira_key=issue.key, error=error)
            continue

        try:
            for fetched in fetched_inputs:
                fetched.metadata["workflow_review_mode"] = str(source_metadata.get("workflow_review_mode") or "issue")
            reviewed_item = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=fetched_inputs,
                discovered_items=issue_items,
                configured_project_paths=project_paths,
                output_dir=target_output_dir,
                progress=progress,
                context_repo=context_repo,
                sprint=sprint,
                index=issue_index,
                total=len(grouped_items),
                report_owner=report_owner,
                deferred_release_gate_resources=deferred_resources,
            )
            reviewed.append(reviewed_item)
            if len(fetched_inputs) + issue_excluded_count == len(issue_items):
                tracker.mark_done(resume_key, reviewed_item)
                _print_done(issue_index, len(grouped_items), f"{issue.key} ({len(issue_items)} MR(s))", reviewed_item)
                _progress(progress, "done", f"DONE {issue.key} ({len(issue_items)} MR(s))", index=issue_index, total=len(grouped_items), jira_key=issue.key, report=reviewed_item.get("report", ""), reports=reviewed_item.get("reports", []), severity_counts=reviewed_item.get("severity_counts", {}), finding_count=reviewed_item.get("finding_count", 0))
            else:
                error = (
                    f"Only handled {len(fetched_inputs) + issue_excluded_count}/{len(issue_items)} MR(s) "
                    f"({len(fetched_inputs)} reviewed, {issue_excluded_count} dev-branch excluded); rerun will retry this Jira issue."
                )
                tracker.mark_failed(
                    resume_key,
                    error,
                    resume_item,
                )
                _print_done(issue_index, len(grouped_items), f"{issue.key} ({len(fetched_inputs)}/{len(issue_items)} MR(s))", reviewed_item, status="PARTIAL")
                _print_failed(issue_index, len(grouped_items), issue.key, error)
                _progress(progress, "partial", f"PARTIAL {issue.key}: {error}", index=issue_index, total=len(grouped_items), jira_key=issue.key, report=reviewed_item.get("report", ""), reports=reviewed_item.get("reports", []), error=error)
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, resume_item)
            raise
        except Exception as exc:
            errors.append({"jira_key": issue.key, "stage": "consolidated-review", "error": str(exc)})
            tracker.mark_failed(resume_key, str(exc), resume_item)
            _print_failed(issue_index, len(grouped_items), issue.key, str(exc))
            _progress(progress, "failed", f"FAILED {issue.key}: {exc}", index=issue_index, total=len(grouped_items), jira_key=issue.key, error=str(exc))

    reviewed_keys = {str(item.get("jira_key") or "").upper() for item in reviewed}
    for deferred_issue in issues:
        if deferred_issue.key.upper() in reviewed_keys:
            continue
        deferred = [
            item
            for item in excluded_branch_type_mrs
            if str(item.get("jira_key") or "").upper() == deferred_issue.key.upper()
            and _normalize_branch_type(str(item.get("release_gate_role") or "")) in {"company_config", "scr"}
        ]
        if not deferred:
            continue
        hydrated = _hydrate_deferred_release_gate_resources(
            deferred,
            deferred_issue.key,
            sprint or deferred_issue.sprint,
        )
        reviewed.append(
            {
                "jira_key": deferred_issue.key,
                "jira_summary": deferred_issue.summary,
                "jira_status": deferred_issue.status,
                "review_mode": "deferred-only",
                "code_mr_count": 0,
                "mr_count": 0,
                "deferred_resource_count": len(hydrated),
                "deferred_release_gate_resources": hydrated,
                "issue_review_status": "no-code-changes-to-review",
                "code_review_status": "complete",
                "release_gate_status": "pending",
                "conclusion": "No code changes to review; deferred resources await GIT_VERSION Release Gate.",
                "severity_counts": {},
                "finding_count": 0,
            }
        )

    return {
        **source_metadata,
        "source_kind": source_kind,
        "issue_count": len(issues),
        "mr_count": len(discovered["mrs"]),
        "processed": len(reviewed),
        "skipped_completed": skipped_completed,
        "review_mode": "consolidated-mrs-by-jira-issue",
        "output_dir": str(target_output_dir),
        "resume_state": _resume_path(tracker),
        "issues_without_mrs": discovered["issues_without_mrs"],
        "excluded_dev_branch_mrs": excluded_dev_branch_mrs,
        "excluded_branch_type_mrs": excluded_branch_type_mrs,
        "excluded_state_mrs": excluded_state_mrs,
        "excluded_cycle_revision_mrs": excluded_cycle_revision_mrs,
        "errors": errors,
        "items": reviewed,
        "discovered_mrs": discovered["mrs"],
    }


def _group_discovered_mrs_by_jira(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        issue_key = str(item.get("jira_key") or "").strip().upper()
        if not issue_key:
            continue
        grouped.setdefault(issue_key, []).append(item)
    return grouped


def _responsible_people_from_related_mrs(items: list[dict[str, Any]]) -> list[str]:
    by_key: dict[str, str] = {}
    for item in items:
        responsible = str(item.get("responsible") or "").strip()
        if not responsible:
            continue
        for value in re.split(r"[+,;]+", responsible):
            person = value.strip()
            key = person.lower()
            if key and key not in by_key:
                by_key[key] = person
    return [by_key[key] for key in sorted(by_key)]


JIRA_REVIEWER_RESPONSIBLE_USERNAMES = {
    "Luck Chen": "luckxh.chen",
    "Tran Trung Hieu": "hieut.tran",
    "Wen Yi": "wen.yi",
    "Kevin Tan": "kevin.tan",
    "Sunny Cheng": "sunny.cheng",
    "Victor Xu": "victorcz.xu",
}


def _apply_jira_scope_responsible(
    review_input: ReviewInput,
    issue: JiraIssue,
    deferred_resources: list[dict[str, Any]],
) -> None:
    display_name = _jira_scope_responsible_display(
        str(review_input.metadata.get("application") or ""),
        issue.components,
        deferred_resources,
    )
    username = JIRA_REVIEWER_RESPONSIBLE_USERNAMES.get(display_name, "")
    review_input.metadata["jira_components"] = issue.components
    review_input.metadata["jira_responsibles"] = issue.responsibles
    if not username:
        return
    previous = str(review_input.metadata.get("responsible") or "").strip()
    if previous and not review_input.metadata.get("git_tools_responsible"):
        review_input.metadata["git_tools_responsible"] = previous
    review_input.metadata.update(
        {
            "scope_responsible_display": display_name,
            "scope_responsible": username,
            "responsible": username,
            "responsible_people": [username],
            "responsible_scope": [username],
        }
    )


def _jira_scope_responsible_display(
    application: str,
    components: list[str],
    deferred_resources: list[dict[str, Any]],
) -> str:
    component_keys = {
        re.sub(r"[^a-z0-9]+", " ", component.casefold()).strip()
        for component in components
    }
    is_aop_or_lca = bool(component_keys & {"account opening system", "lowcode application"})
    roles = {
        _normalize_branch_type(str(item.get("release_gate_role") or ""))
        for item in deferred_resources
    }
    if application in {"MO Client Config", "DPS Config"}:
        return "Luck Chen"
    if "company_config" in roles:
        if application == "iTrade Client" and "mo client config" in component_keys:
            return "Luck Chen"
        if application == "DPS" and component_keys & {"dps config", "mo client config"}:
            return "Luck Chen"
    if application == "iTrade Client":
        return "Wen Yi"
    if application in {"WVAdmin", "Services Terminal"}:
        return "Victor Xu" if application == "WVAdmin" and is_aop_or_lca else "Tran Trung Hieu"
    if application == "DPS":
        if is_aop_or_lca:
            return "Sunny Cheng"
        return "Kevin Tan"
    return ""


def review_fingerprint_from_merge_requests(items: list[dict[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mr_url = str(item.get("mr_url") or item.get("web_url") or "").strip()
        if not mr_url:
            continue
        project_path = str(item.get("project_path") or item.get("gitlab_project") or item.get("project") or "").strip()
        iid = str(item.get("mr_id") or item.get("iid") or "").strip()
        if not project_path or not iid:
            try:
                ref = parse_mr_url(mr_url)
                project_path = project_path or ref.project_path
                iid = iid or ref.iid
            except Exception:
                pass
        normalized.append(
            {
                "mr_url": mr_url,
                "project_path": project_path,
                "iid": iid,
                "state": str(item.get("state") or item.get("mr_state") or item.get("status") or "").strip(),
                "source_branch": str(item.get("source_branch") or "").strip(),
                "target_branch": str(item.get("target_branch") or "").strip(),
                "commit": str(item.get("head_sha") or item.get("commit") or item.get("sha") or "").strip(),
                "updated_at": str(item.get("updated_at") or item.get("mr_updated_at") or "").strip(),
                "merged_at": str(item.get("merged_at") or item.get("mr_merged_at") or "").strip(),
            }
        )
    normalized.sort(key=lambda entry: (entry["project_path"].lower(), _safe_int(entry["iid"]), entry["mr_url"].lower()))
    stable_items = [
        {key: item.get(key, "") for key in ("mr_url", "project_path", "iid", "state", "source_branch", "target_branch", "commit")}
        for item in normalized
    ]
    return {
        "schema": "code_reviewer_mr_fingerprint_v1",
        "fingerprint": _hash_fingerprint_items(normalized),
        "stable_fingerprint": _hash_fingerprint_items(stable_items),
        "items": normalized,
        "stable_items": stable_items,
        "mr_count": len(normalized),
    }


def mr_revision_identity(item: dict[str, Any]) -> dict[str, str]:
    """Return the cycle-safe GitLab project + IID + head SHA identity."""
    mr_url = str(item.get("mr_url") or item.get("web_url") or "").strip()
    project_path = str(item.get("project_path") or item.get("gitlab_project") or item.get("project") or "").strip("/")
    iid = str(item.get("mr_id") or item.get("iid") or "").strip()
    if mr_url and (not project_path or not iid):
        try:
            ref = parse_mr_url(mr_url)
            project_path = project_path or ref.project_path
            iid = iid or ref.iid
        except Exception:
            pass
    head_sha = str(item.get("head_sha") or item.get("commit") or item.get("sha") or "").strip().lower()
    stable_key = f"{project_path.casefold()}!{iid}"
    revision_key = f"{stable_key}@{head_sha}" if head_sha else stable_key
    return {
        "project_path": project_path,
        "mr_iid": iid,
        "head_sha": head_sha,
        "stable_key": stable_key,
        "revision_key": revision_key,
        "revision_fingerprint": hashlib.sha256(revision_key.encode("utf-8")).hexdigest(),
    }


def select_cycle_mr_revisions(
    candidates: list[dict[str, Any]],
    previously_reviewed: list[dict[str, Any]] | None = None,
    decisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Select only new MR revisions while retaining auditable include/exclude rows.

    Explicit decisions may be keyed by revision key, revision fingerprint, or
    stable project!IID key. A changed head SHA is always a distinct revision.
    Missing SHA candidates stay included because they cannot be safely proven old.
    """
    previous = {
        mr_revision_identity(item)["revision_key"]
        for item in (previously_reviewed or [])
        if mr_revision_identity(item)["head_sha"]
    }
    normalized_decisions = {str(key): str(value).strip().casefold() for key, value in (decisions or {}).items()}
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        identity = mr_revision_identity(candidate)
        decision = next(
            (
                normalized_decisions[key]
                for key in (identity["revision_key"], identity["revision_fingerprint"], identity["stable_key"])
                if key in normalized_decisions
            ),
            "",
        )
        unchanged = bool(identity["head_sha"] and identity["revision_key"] in previous)
        selected = decision in {"include", "included", "yes", "true", "1"} or (
            decision not in {"exclude", "excluded", "no", "false", "0"} and not unchanged
        )
        reason = "explicit-include" if selected and decision else "new-revision"
        if not selected:
            reason = "explicit-exclude" if decision else "reviewed-unchanged-revision"
        row = {**candidate, **identity, "selected": selected, "selection_reason": reason}
        rows.append(row)
        (included if selected else excluded).append(row)
    return {
        "schema": "code_reviewer_cycle_mr_selection_v1",
        "candidate_count": len(rows),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "included": included,
        "excluded": excluded,
        "items": rows,
    }


def previously_reviewed_cycle_revisions(jira_key: str) -> list[dict[str, Any]]:
    """Load immutable MR revision identities already persisted for older/current Cycles.

    The import stays local so the CLI review service remains usable without
    initializing workflow storage until cycle-aware selection is required.
    """
    try:
        from .workflow_store import workflow_store

        cycles = workflow_store().list_cycles((jira_key or "").strip().upper())
    except Exception:
        return []
    revisions: list[dict[str, Any]] = []
    for cycle in cycles:
        scope = cycle.get("mr_scope_json") or cycle.get("mr_scope") or []
        if not isinstance(scope, list):
            continue
        for item in scope:
            if not isinstance(item, dict):
                continue
            identity = mr_revision_identity(item)
            if identity["project_path"] and identity["mr_iid"] and identity["head_sha"]:
                revisions.append({**item, **identity, "cycle_id": cycle.get("cycle_id", "")})
    return revisions


def _select_fetched_cycle_revisions(
    jira_key: str,
    fetched_inputs: list[ReviewInput],
    decisions: dict[str, str] | None = None,
) -> tuple[list[ReviewInput], list[dict[str, Any]]]:
    previous = previously_reviewed_cycle_revisions(jira_key)
    candidates: list[dict[str, Any]] = []
    by_key: dict[str, ReviewInput] = {}
    for review_input in fetched_inputs:
        row = {
            "jira_key": jira_key,
            "project_path": review_input.metadata.get("gitlab_project_path") or review_input.project,
            "mr_id": review_input.mr_id,
            "mr_url": review_input.mr_url,
            "head_sha": review_input.commit,
            "base_sha": review_input.metadata.get("diff_base_sha", ""),
            "source_branch": review_input.source_branch,
            "target_branch": review_input.target_branch,
        }
        identity = mr_revision_identity(row)
        candidates.append({**row, **identity})
        by_key[identity["revision_key"]] = review_input
    selection = select_cycle_mr_revisions(
        candidates,
        previously_reviewed=previous,
        decisions=decisions,
    )
    included = [by_key[item["revision_key"]] for item in selection["included"] if item["revision_key"] in by_key]
    return included, selection["excluded"]


def deferred_release_resource_identity(item: dict[str, Any]) -> dict[str, str]:
    revision = mr_revision_identity(item)
    jira_key = str(item.get("jira_key") or "").strip().upper()
    sprint_id = str(item.get("sprint_id") or item.get("sprint") or "").strip()
    cycle_id = str(item.get("cycle_id") or "").strip()
    identity_key = "|".join(
        [jira_key, sprint_id, cycle_id, revision["project_path"].casefold(), revision["mr_iid"], revision["head_sha"]]
    )
    return {
        **revision,
        "jira_key": jira_key,
        "sprint_id": sprint_id,
        "cycle_id": cycle_id,
        "resource_key": identity_key,
        "resource_fingerprint": hashlib.sha256(identity_key.encode("utf-8")).hexdigest(),
    }


def reconcile_deferred_release_resources(
    resources: list[dict[str, Any]],
    *,
    sprint_id: str = "",
    cycle_ids: list[str] | None = None,
    verified_revisions: list[dict[str, Any]] | None = None,
    contained_commit_shas: list[str] | None = None,
) -> dict[str, Any]:
    """Prepare deterministic deferred/GIT_VERSION reconciliation for persistence.

    Commit ancestry/content verification remains the caller's GitLab/build-lock
    responsibility; this helper consumes those verified/contained revision facts
    and ensures only the current release scope and unseen head SHAs are pending.
    """
    cycle_set = {str(value) for value in (cycle_ids or []) if str(value)}
    verified_keys = {deferred_release_resource_identity(item)["resource_key"] for item in (verified_revisions or [])}
    contained = {str(value).strip().lower() for value in (contained_commit_shas or []) if str(value).strip()}
    pending: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    for raw in resources:
        identity = deferred_release_resource_identity(raw)
        row = {**raw, **identity}
        in_scope = (not sprint_id or identity["sprint_id"] == str(sprint_id)) and (
            not cycle_set or identity["cycle_id"] in cycle_set
        )
        if not in_scope:
            row.update({"reconciliation_status": "out-of-scope", "release_gate_pending": False})
            out_of_scope.append(row)
        elif identity["resource_key"] in verified_keys or identity["head_sha"] in contained:
            row.update({"reconciliation_status": "verified", "release_gate_pending": False})
            verified.append(row)
        else:
            row.update({"reconciliation_status": "pending", "release_gate_pending": True})
            pending.append(row)
    return {
        "schema": "code_reviewer_deferred_reconciliation_v1",
        "resource_count": len(resources),
        "pending_count": len(pending),
        "verified_count": len(verified),
        "out_of_scope_count": len(out_of_scope),
        "release_gate_status": "pending" if pending else "verified",
        "pending": pending,
        "verified": verified,
        "out_of_scope": out_of_scope,
    }


def sprint_review_preflight(sprint: str, jira_project_key: str = "ECHNL") -> dict[str, Any]:
    """Public service boundary for Web Sprint validation and mode selection."""
    project = jira_project_key or app_config_str("jira.project_key", "JIRA_PROJECT_KEY", "ECHNL")
    try:
        return JiraClient().sprint_preflight(sprint, project_key=project)
    except Exception as exc:
        return {
            "valid": False,
            "accessible": False,
            "sprint": str(sprint or "").strip(),
            "project_key": project,
            "issue_count": 0,
            "all_development_done": False,
            "review_mode": "batch-preview",
            "requires_confirmation": False,
            "empty": True,
            "issues": [],
            "not_development_done_issues": [],
            "error": str(exc),
        }


def parse_jira_issue_keys(value: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in (value or "").split(","):
        key = raw.strip().upper()
        if not key:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
            invalid.append(raw.strip() or "<empty>")
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if invalid:
        raise ValueError(f"Invalid Jira issue key(s): {', '.join(invalid)}")
    if not keys:
        raise ValueError("At least one Jira issue key is required.")
    return keys


def review_jira_issues_merge_requests(
    jira_keys: list[str] | str,
    state: str = "opened,merged",
    limit: int = 200,
    list_only: bool = False,
    output_dir: Path | None = None,
    context_repo: Path | str | None = None,
    progress: Any = None,
    report_owner: str = "",
    force_rerun: bool = False,
) -> dict[str, Any]:
    keys = parse_jira_issue_keys(jira_keys) if isinstance(jira_keys, str) else parse_jira_issue_keys(",".join(jira_keys))
    state = state or app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    limit = int(limit or app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200))
    _progress(progress, "start", f"Loading {len(keys)} Jira issues", jira_keys=keys, issue_count=len(keys))
    jira = JiraClient()
    issues: list[JiraIssue] = []
    load_errors: list[dict[str, str]] = []
    for key in keys:
        try:
            issues.append(jira.fetch_issue(key))
        except Exception as exc:
            load_errors.append({"jira_key": key, "stage": "jira-load", "error": str(exc)})
    reviewable, skipped_issues = _filter_reviewable_jira_issues(issues)
    _progress(
        progress,
        "jira",
        f"Loaded {len(reviewable)}/{len(keys)} reviewable Jira issue(s)",
        jira_keys=keys,
        issue_count=len(reviewable),
        skipped_issue_count=len(skipped_issues),
        load_error_count=len(load_errors),
    )
    summary = _review_issue_collection_merge_requests(
        issues=reviewable,
        source_kind="jira-issue-list",
        source_label=f"Jira issues {','.join(keys)}",
        resume_namespace="jira-issue-list-mrs",
        batch_title="Jira issue-list consolidated reviews",
        state=state,
        limit=limit,
        list_only=list_only,
        output_dir=output_dir,
        context_repo=context_repo,
        progress=progress,
        report_owner=report_owner,
        force_rerun=force_rerun,
        source_metadata={
            "jira_keys": keys,
            "requested_issue_count": len(keys),
            "skipped_status_issues": skipped_issues,
            "jira_load_errors": load_errors,
        },
    )
    existing_errors = summary.get("errors") or summary.get("discovery_errors") or []
    summary["errors"] = [*load_errors, *existing_errors]
    return summary


def jira_issue_review_fingerprint(jira_key: str, state: str = "", limit: int = 0) -> dict[str, Any]:
    issue_key = (jira_key or "").strip().upper()
    if not issue_key:
        raise ValueError("Jira issue key is required.")
    configured_state = state or app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    configured_limit = int(limit or app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200))
    jira = JiraClient()
    gitlab = GitLabClient(base_url=_gitlab_base_url())
    issue = jira.fetch_issue(issue_key)
    discovered = _discover_sprint_merge_requests(jira, gitlab, [issue], state=configured_state, limit=configured_limit)
    enriched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = list(discovered.get("errors") or [])
    for item in discovered.get("mrs", []) or []:
        if not isinstance(item, dict):
            continue
        mr_url = str(item.get("mr_url") or "").strip()
        record = dict(item)
        if mr_url:
            try:
                ref = parse_mr_url(mr_url)
                mr = gitlab.fetch_merge_request(ref.project_path, ref.iid)
                if isinstance(mr, dict):
                    record.update(
                        {
                            "project_path": ref.project_path,
                            "mr_id": str(mr.get("iid") or ref.iid),
                            "state": str(mr.get("state") or record.get("state") or ""),
                            "source_branch": str(mr.get("source_branch") or record.get("source_branch") or ""),
                            "target_branch": str(mr.get("target_branch") or record.get("target_branch") or ""),
                            "commit": str(mr.get("sha") or record.get("commit") or ""),
                            "updated_at": str(mr.get("updated_at") or ""),
                            "merged_at": str(mr.get("merged_at") or ""),
                            "closed_at": str(mr.get("closed_at") or ""),
                        }
                    )
            except Exception as exc:
                errors.append({"jira_key": issue.key, "mr_url": mr_url, "stage": "fingerprint", "error": str(exc)})
        enriched.append(record)
    fingerprint = review_fingerprint_from_merge_requests(enriched)
    return {
        "jira_key": issue.key,
        "jira_summary": issue.summary,
        "jira_status": issue.status,
        "state": configured_state,
        "limit": configured_limit,
        "fingerprint": fingerprint,
        "errors": errors,
        "excluded_dev_branch_mrs": discovered.get("excluded_dev_branch_mrs", []),
        "excluded_branch_type_mrs": discovered.get("excluded_branch_type_mrs", []),
        "excluded_state_mrs": discovered.get("excluded_state_mrs", []),
        "issues_without_mrs": discovered.get("issues_without_mrs", []),
    }


def _hash_fingerprint_items(items: list[dict[str, str]]) -> str:
    payload = json.dumps(items, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _distinct_values_from_related_mrs(items: list[dict[str, Any]], key_name: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item.get(key_name) or "").strip()
        if not value:
            continue
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(value)
    return values


def review_jira_issue_merge_requests(
    jira_key: str,
    state: str = "opened,merged",
    limit: int = 50,
    list_only: bool = False,
    output_dir: Path | None = None,
    context_repo: Path | str | None = None,
    progress: Any = None,
    report_owner: str = "",
    force_rerun: bool = False,
) -> dict[str, Any]:
    issue_key = (jira_key or "").strip().upper()
    if not issue_key:
        raise ValueError("--jira is required for Jira issue MR review.")
    state = state or app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    limit = int(limit or app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200))
    _progress(progress, "start", f"Loading Jira issue {issue_key}", jira_key=issue_key)
    jira = JiraClient()
    gitlab = GitLabClient(base_url=_gitlab_base_url())
    issue = jira.fetch_issue(issue_key)
    _progress(progress, "jira", f"Loaded Jira issue {issue.key}: {issue.summary}", jira_key=issue.key, jira_status=issue.status)
    project_paths = _sprint_branch_project_paths(gitlab)
    if not _jira_issue_status_allowed(issue):
        skipped = _filter_reviewable_jira_issues([issue])[1]
        _progress(progress, "skip-status", f"SKIP {issue.key}: Jira status {issue.status} is not reviewable", jira_key=issue.key, jira_status=issue.status)
        return {
            "jira_key": issue.key,
            "jira_summary": issue.summary,
            "jira_status": issue.status,
            "configured_gitlab_project_count": len(project_paths),
            "configured_gitlab_projects": project_paths,
            "mr_count": 0,
            "processed": 0,
            "skipped_completed": 0,
            "review_mode": "consolidated-mrs",
            "output_dir": str(_batch_output_dir(output_dir)),
            "resume_state": "",
            "issues_without_mrs": [],
            "skipped_status_issues": skipped,
            "excluded_dev_branch_mrs": [],
            "excluded_branch_type_mrs": [],
            "excluded_state_mrs": [],
            "errors": [],
            "items": [],
            "discovered_mrs": [],
        }
    discovered = _discover_sprint_merge_requests(
        jira,
        gitlab,
        [issue],
        state=state,
        limit=limit,
        progress=progress,
        source_label=issue.key,
    )
    _emit_discovery_progress(progress, issue.key, discovered, jira_key=issue.key)
    if list_only:
        return {
            "jira_key": issue.key,
            "jira_summary": issue.summary,
            "jira_status": issue.status,
            "configured_gitlab_project_count": len(project_paths),
            "configured_gitlab_projects": project_paths,
            "mr_count": len(discovered["mrs"]),
            "items": discovered["mrs"],
            "issues_without_mrs": discovered["issues_without_mrs"],
            "excluded_dev_branch_mrs": discovered.get("excluded_dev_branch_mrs", []),
            "excluded_branch_type_mrs": discovered.get("excluded_branch_type_mrs", []),
            "excluded_state_mrs": discovered.get("excluded_state_mrs", []),
            "errors": discovered["errors"],
        }

    fetched_inputs: list[ReviewInput] = []
    errors: list[dict[str, str]] = list(discovered["errors"])
    excluded_dev_branch_mrs: list[dict[str, Any]] = list(discovered.get("excluded_dev_branch_mrs", []))
    excluded_branch_type_mrs: list[dict[str, Any]] = list(discovered.get("excluded_branch_type_mrs", []))
    excluded_state_mrs: list[dict[str, Any]] = list(discovered.get("excluded_state_mrs", []))
    excluded_cycle_revision_mrs: list[dict[str, Any]] = []
    target_output_dir = _batch_output_dir(output_dir)
    tracker = _resume_tracker(
        "jira-issue-mrs",
        target_output_dir,
        {"jira_key": issue.key, "state": state, "limit": limit},
    )
    resume_key = _jira_group_resume_key(issue.key, discovered["mrs"])
    resume_item = {
        "jira_key": issue.key,
        "jira_summary": issue.summary,
        "mr_urls": [item.get("mr_url", "") for item in discovered["mrs"]],
    }
    skipped_completed = 0
    issue_excluded_count = 0
    _print_batch_start("Jira issue consolidated review", 1 if discovered["mrs"] else 0, target_output_dir, tracker)
    _progress(progress, "batch-start", f"Preparing consolidated review for {issue.key}: {len(discovered['mrs'])} MR(s)", total=1 if discovered["mrs"] else 0, output_dir=str(target_output_dir), resume_state=_resume_path(tracker))

    reviewed: list[dict[str, Any]] = []
    if discovered["mrs"] and not force_rerun and tracker.is_done(resume_key):
        item = tracker.done_summary(resume_key)
        _print_skipped(1, 1, f"{issue.key} ({len(discovered['mrs'])} MR(s))", item)
        _progress(progress, "skip-done", f"SKIP DONE {issue.key} ({len(discovered['mrs'])} MR(s))", jira_key=issue.key, report=item.get("report", ""))
        reviewed.append(item)
        skipped_completed = 1
    elif discovered["mrs"]:
        tracker.mark_started(resume_key, resume_item)
        try:
            for index, item in enumerate(discovered["mrs"], 1):
                mr_url = item["mr_url"]
                print(f"[{index}/{len(discovered['mrs'])}] Fetching {issue.key}: {mr_url}", file=sys.stderr, flush=True)
                _progress(progress, "fetch-mr", f"Fetching {issue.key} [{index}/{len(discovered['mrs'])}]: {mr_url}", jira_key=issue.key, mr_index=index, mr_total=len(discovered["mrs"]), mr_url=mr_url)
                try:
                    fetched = _review_input_from_mr_url(
                        mr_url,
                        jira_key=issue.key,
                        attach_context=context_repo is None,
                        attach_jira_metadata=False,
                    )
                    branch_type_exclusion = _review_input_ignored_branch_type_exclusion(fetched, issue.key)
                    if branch_type_exclusion:
                        excluded_branch_type_mrs.append(branch_type_exclusion)
                        issue_excluded_count += 1
                        _progress(
                            progress,
                            "skip-branch-type",
                            f"SKIP BRANCH-TYPE {issue.key}: {mr_url}",
                            jira_key=issue.key,
                            mr_url=mr_url,
                            source_branch=fetched.source_branch,
                            ignored_branch_type=branch_type_exclusion.get("ignored_branch_type", ""),
                        )
                        continue
                    exclusion = _review_input_dev_branch_exclusion(fetched, issue.key)
                    if exclusion:
                        excluded_dev_branch_mrs.append(exclusion)
                        issue_excluded_count += 1
                        _print_dev_branch_skip(1, 1, f"{issue.key} {mr_url}", fetched.target_branch, exclusion.get("dev_branch", []))
                        _progress(progress, "skip-dev-branch", f"SKIP DEV-BRANCH {issue.key}: {mr_url}", jira_key=issue.key, mr_url=mr_url, target_branch=fetched.target_branch, dev_branch=exclusion.get("dev_branch", []))
                        continue
                    fetched_inputs.append(fetched)
                except Exception as exc:
                    errors.append({"jira_key": issue.key, "mr_url": mr_url, "error": str(exc)})
                    _print_failed(1, 1, f"{issue.key} fetch {mr_url}", str(exc))
                    _progress(progress, "failed", f"FAILED {issue.key} fetch {mr_url}: {exc}", jira_key=issue.key, mr_url=mr_url, error=str(exc))
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, resume_item)
            raise

    if not reviewed and fetched_inputs:
        fetched_inputs, excluded_cycle_revision_mrs = _select_fetched_cycle_revisions(issue.key, fetched_inputs)
        if excluded_cycle_revision_mrs:
            issue_excluded_count += len(excluded_cycle_revision_mrs)
            _progress(
                progress,
                "skip-cycle-revision",
                f"SKIP {len(excluded_cycle_revision_mrs)} unchanged MR revision(s) already reviewed for {issue.key}",
                jira_key=issue.key,
                excluded_revisions=excluded_cycle_revision_mrs,
            )

    deferred_resources = _hydrate_deferred_release_gate_resources(
        [
            item
            for item in excluded_branch_type_mrs
            if str(item.get("jira_key") or "").upper() == issue.key.upper()
            and _normalize_branch_type(str(item.get("release_gate_role") or ""))
            in {"company_config", "scr"}
        ],
        issue.key,
        issue.sprint,
    )

    if not reviewed and not fetched_inputs and deferred_resources:
        try:
            reviewed_item = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=[],
                discovered_items=discovered["mrs"],
                configured_project_paths=project_paths,
                output_dir=target_output_dir,
                progress=progress,
                context_repo=context_repo,
                sprint=issue.sprint,
                index=1,
                total=1,
                report_owner=report_owner,
                deferred_release_gate_resources=deferred_resources,
            )
            reviewed.append(reviewed_item)
            tracker.mark_done(resume_key, reviewed_item)
            _print_done(
                1,
                1,
                f"{issue.key} ({len(deferred_resources)} deferred resource(s))",
                reviewed_item,
            )
            _progress(
                progress,
                "done",
                f"DONE {issue.key} ({len(deferred_resources)} deferred release resource(s))",
                jira_key=issue.key,
                report=reviewed_item.get("report", ""),
                reports=reviewed_item.get("reports", []),
                deferred_resource_count=len(deferred_resources),
            )
        except Exception as exc:
            errors.append({"jira_key": issue.key, "stage": "deferred-scope-report", "error": str(exc)})
            tracker.mark_failed(resume_key, str(exc), resume_item)
            _print_failed(1, 1, issue.key, str(exc))
            _progress(
                progress,
                "failed",
                f"FAILED {issue.key} deferred report: {exc}",
                jira_key=issue.key,
                error=str(exc),
            )

    if not reviewed and not fetched_inputs and discovered["mrs"]:
        if issue_excluded_count:
            cycle_only = bool(excluded_cycle_revision_mrs) and len(excluded_cycle_revision_mrs) == issue_excluded_count
            excluded_item = {
                "jira_key": issue.key,
                "jira_summary": issue.summary,
                "jira_status": issue.status,
                "review_mode": "no-new-mr-revisions" if cycle_only else "excluded-mrs",
                "mr_count": len(discovered["mrs"]),
                "excluded_dev_branch_count": issue_excluded_count,
                "excluded_cycle_revision_count": len(excluded_cycle_revision_mrs),
                "resume_status": "no-new-mr-revisions" if cycle_only else "excluded-mrs",
                "conclusion": "No new MR revisions to review." if cycle_only else "All MR revisions were routed or excluded.",
            }
            reviewed.append(excluded_item)
            tracker.mark_done(resume_key, excluded_item)
        else:
            error = "No MR diffs were fetched for this Jira issue."
            tracker.mark_failed(resume_key, error, resume_item)
            _print_failed(1, 1, issue.key, error)
            _progress(progress, "failed", f"FAILED {issue.key}: {error}", jira_key=issue.key, error=error)

    if not reviewed and fetched_inputs:
        try:
            reviewed_item = _review_fetched_inputs_for_issue(
                issue=issue,
                fetched_inputs=fetched_inputs,
                discovered_items=discovered["mrs"],
                configured_project_paths=project_paths,
                output_dir=target_output_dir,
                progress=progress,
                context_repo=context_repo,
                sprint=issue.sprint,
                index=1,
                total=1,
                report_owner=report_owner,
                deferred_release_gate_resources=deferred_resources,
            )
            reviewed.append(reviewed_item)
            if len(fetched_inputs) + issue_excluded_count == len(discovered["mrs"]):
                tracker.mark_done(resume_key, reviewed_item)
                _print_done(1, 1, f"{issue.key} ({len(discovered['mrs'])} MR(s))", reviewed_item)
                _progress(progress, "done", f"DONE {issue.key} ({len(discovered['mrs'])} MR(s))", jira_key=issue.key, report=reviewed_item.get("report", ""), reports=reviewed_item.get("reports", []), severity_counts=reviewed_item.get("severity_counts", {}), finding_count=reviewed_item.get("finding_count", 0))
            else:
                error = (
                    f"Only handled {len(fetched_inputs) + issue_excluded_count}/{len(discovered['mrs'])} MR(s) "
                    f"({len(fetched_inputs)} reviewed, {issue_excluded_count} dev-branch excluded); rerun will retry this Jira issue."
                )
                tracker.mark_failed(
                    resume_key,
                    error,
                    resume_item,
                )
                _print_done(1, 1, f"{issue.key} ({len(fetched_inputs)}/{len(discovered['mrs'])} MR(s))", reviewed_item, status="PARTIAL")
                _print_failed(1, 1, issue.key, error)
                _progress(progress, "partial", f"PARTIAL {issue.key}: {error}", jira_key=issue.key, report=reviewed_item.get("report", ""), reports=reviewed_item.get("reports", []), error=error)
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, resume_item)
            raise
        except Exception as exc:
            errors.append({"jira_key": issue.key, "stage": "consolidated-review", "error": str(exc)})
            tracker.mark_failed(resume_key, str(exc), resume_item)
            _print_failed(1, 1, issue.key, str(exc))
            _progress(progress, "failed", f"FAILED {issue.key}: {exc}", jira_key=issue.key, error=str(exc))

    return {
        "jira_key": issue.key,
        "jira_summary": issue.summary,
        "jira_status": issue.status,
        "configured_gitlab_project_count": len(project_paths),
        "configured_gitlab_projects": project_paths,
        "mr_count": len(discovered["mrs"]),
        "processed": len(reviewed),
        "skipped_completed": skipped_completed,
        "review_mode": "consolidated-mrs",
        "output_dir": str(target_output_dir),
        "resume_state": _resume_path(tracker),
        "issues_without_mrs": discovered["issues_without_mrs"],
        "excluded_dev_branch_mrs": excluded_dev_branch_mrs,
        "excluded_branch_type_mrs": excluded_branch_type_mrs,
        "excluded_state_mrs": excluded_state_mrs,
        "excluded_cycle_revision_mrs": excluded_cycle_revision_mrs,
        "errors": errors,
        "items": reviewed,
        "discovered_mrs": discovered["mrs"],
    }


def _combine_jira_issue_review_inputs(
    issue: JiraIssue,
    mr_inputs: list[ReviewInput],
    discovered_items: list[dict[str, Any]],
    configured_project_paths: list[str],
    run_group_id: str = "",
) -> ReviewInput:
    changed_files: list[ChangedFile] = []
    raw_diff_parts: list[str] = []
    related_mrs: list[dict[str, Any]] = []
    file_links: dict[str, dict[str, str]] = {}
    project_context_parts: list[str] = []
    cross_mr_contract_parts: list[str] = []
    git_version_context_parts: list[str] = []
    issue_links: list[dict[str, str]] = []
    seen_issue_links: set[str] = set()
    seen_changed_content: set[tuple[str, str, str]] = set()
    deduplicated_changed_files: list[dict[str, str]] = []
    matched_count = 0

    discovered_by_url = {str(item.get("mr_url") or ""): item for item in discovered_items}
    for review_input in mr_inputs:
        project_path = str(review_input.metadata.get("gitlab_project_path") or review_input.project)
        prefix = _multi_mr_file_prefix(review_input)
        match_status = str(review_input.metadata.get("git_tools_project_match") or "")
        if match_status == "matched":
            matched_count += 1
        related = {
            "mr_url": review_input.mr_url,
            "mr_id": review_input.mr_id,
            "state": review_input.metadata.get("mr_state") or review_input.metadata.get("mr_status", ""),
            "status": review_input.metadata.get("mr_status") or review_input.metadata.get("mr_state", ""),
            "project": review_input.project,
            "project_path": project_path,
            "file_prefix": prefix,
            "title": review_input.title,
            "request_by": str(
                review_input.metadata.get("mr_request_by") or review_input.author or ""
            ).strip(),
            "source_branch": review_input.source_branch,
            "target_branch": review_input.target_branch,
            "commit": review_input.commit,
            "head_sha": review_input.commit,
            "base_sha": review_input.metadata.get("diff_base_sha", ""),
            "revision_key": mr_revision_identity(
                {
                    "project_path": project_path,
                    "mr_id": review_input.mr_id,
                    "head_sha": review_input.commit,
                }
            )["revision_key"],
            "updated_at": review_input.metadata.get("mr_updated_at", ""),
            "merged_at": review_input.metadata.get("mr_merged_at", ""),
            "created_at": review_input.metadata.get("mr_created_at", ""),
            "closed_at": review_input.metadata.get("mr_closed_at", ""),
            "file_count": len(review_input.changed_files),
            "git_tools_project_match": match_status,
            "git_tools_group": review_input.metadata.get("git_tools_group", ""),
            "git_tools_module": review_input.metadata.get("git_tools_module", ""),
            "responsible": review_input.metadata.get("responsible") or review_input.metadata.get("git_tools_responsible", ""),
            "project_name": review_input.metadata.get("project_name") or review_input.metadata.get("git_tools_project_name", ""),
            "project_type": review_input.metadata.get("project_type") or review_input.metadata.get("git_tools_project_type", ""),
            "application": review_input.metadata.get("application", ""),
            "release_line": review_input.metadata.get("release_line", ""),
            "release_lines": review_input.metadata.get("release_lines", []),
            "llm_model_config": review_input.metadata.get("llm_model_config", ""),
            "discovery_source": discovered_by_url.get(review_input.mr_url, {}).get("source", ""),
        }
        related_mrs.append(related)

        for link in review_input.metadata.get("issue_links") or []:
            if not isinstance(link, dict):
                continue
            key = str(link.get("key") or link.get("url") or "")
            if not key or key in seen_issue_links:
                continue
            seen_issue_links.add(key)
            issue_links.append(link)

        context = str(review_input.metadata.get("project_context") or "").strip()
        if context:
            project_context_parts.append(
                f"## {project_path} !{review_input.mr_id} {review_input.source_branch} -> {review_input.target_branch}\n{context}"
            )
        included_files = review_input.metadata.get("project_context_included_files") or []
        changed_paths = [item.path for item in review_input.changed_files[:20]]
        context_files = [str(item) for item in included_files[:12]] if isinstance(included_files, list) else []
        cross_mr_contract_parts.append(
            "\n".join(
                [
                    f"- {project_path} !{review_input.mr_id}: {review_input.source_branch or '-'} -> {review_input.target_branch or '-'}",
                    f"  Changed files: {', '.join(changed_paths) or '-'}",
                    f"  Dependency/context signals: {', '.join(context_files) or '-'}",
                ]
            )
        )

        git_version_context = str(review_input.metadata.get("git_version_review_context") or "").strip()
        if git_version_context:
            git_version_context_parts.append(f"## {project_path} !{review_input.mr_id}\n{git_version_context}")

        raw_diff_parts.append(
            "\n".join(
                [
                    f"# MR: {review_input.mr_url}",
                    f"# Project: {project_path}",
                    f"# Branch: {review_input.source_branch} -> {review_input.target_branch}",
                    f"# Commit: {review_input.commit or '-'}",
                ]
            )
        )
        for changed_file in review_input.changed_files:
            original_path = changed_file.path.replace("\\", "/")
            prefixed_path = f"{prefix}/{original_path}"
            duplicate_key = (project_path.lower(), original_path, changed_file.diff)
            if duplicate_key in seen_changed_content:
                deduplicated_changed_files.append(
                    {
                        "project_path": project_path,
                        "file_path": original_path,
                        "mr_url": review_input.mr_url,
                    }
                )
                continue
            seen_changed_content.add(duplicate_key)
            changed_files.append(
                ChangedFile(
                    path=prefixed_path,
                    additions=changed_file.additions,
                    deletions=changed_file.deletions,
                    diff=changed_file.diff,
                )
            )
            raw_diff_parts.append(
                f"diff --git a/{prefixed_path} b/{prefixed_path}\n"
                f"--- a/{prefixed_path}\n"
                f"+++ b/{prefixed_path}\n"
                f"# Original file: {original_path}\n"
                f"# MR: {review_input.mr_url}\n"
                f"{changed_file.diff}"
            )
            file_links[prefixed_path] = {
                "mr_url": review_input.mr_url,
                "ref": review_input.commit or review_input.source_branch,
                "file_path": original_path,
            }

    project_context_limit = app_config_int(
        "local_context.consolidated_project_context_max_chars",
        "CONSOLIDATED_PROJECT_CONTEXT_MAX_CHARS",
        app_config_int("local_context.project_context_max_chars", "PROJECT_CONTEXT_MAX_CHARS", 80000),
    )
    git_version_context_limit = app_config_int("git_version.source_diff_context_max_chars", "GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS", 100000)
    metadata: dict[str, Any] = {
        "review_input_mode": "jira-consolidated-mrs",
        "network_stage": "jira-issue-consolidated-mr-review",
        "mr_type": "MULTI_MR",
        "related_merge_requests": related_mrs,
        "jira_summary": issue.summary,
        'jira_description': issue.final_description,
        "jira_status": issue.status,
        "jira_issue_type": issue.issue_type,
        "jira_components": issue.components,
        "jira_responsibles": issue.responsibles,
        "multi_mr_file_links": file_links,
        "configured_gitlab_project_count": len(configured_project_paths),
        "configured_gitlab_projects": configured_project_paths,
        "git_tools_project_match": "multi",
        "git_tools_project_path": "multiple",
        "git_tools_config": str(git_tools_config_path()),
        "git_tools_config_project_count": len(git_tools_project_entries()),
        "git_tools_multi_match_summary": {
            "matched": matched_count,
            "total": len(mr_inputs),
            "unmatched": len(mr_inputs) - matched_count,
        },
        "issue_links": issue_links,
        "run_group_id": run_group_id,
        "responsible_scope": [],
        "jira_sprint_memberships": [item.to_dict() for item in issue.sprint_memberships],
        "jira_current_sprint_id": issue.current_sprint_id,
        "jira_current_sprint_state": issue.current_sprint_state,
    }
    fingerprint = review_fingerprint_from_merge_requests(related_mrs)
    metadata["review_fingerprint"] = fingerprint["fingerprint"]
    metadata["review_stable_fingerprint"] = fingerprint["stable_fingerprint"]
    metadata["review_fingerprint_items"] = fingerprint["items"]
    metadata["review_stable_fingerprint_items"] = fingerprint["stable_items"]
    responsible_people = _responsible_people_from_related_mrs(related_mrs)
    if responsible_people:
        metadata["git_tools_responsible_people"] = responsible_people
        metadata["git_tools_responsible"] = "+".join(responsible_people)
        metadata["responsible_people"] = responsible_people
        metadata["responsible"] = "+".join(responsible_people)
        metadata["responsible_scope"] = responsible_people
    project_names = _distinct_values_from_related_mrs(related_mrs, "project_name")
    if not project_names:
        project_names = _distinct_values_from_related_mrs(related_mrs, "git_tools_module")
    if project_names:
        metadata["project_names"] = project_names
        metadata["project_name"] = project_names[0] if len(project_names) == 1 else "+".join(project_names)
    project_types = _distinct_values_from_related_mrs(related_mrs, "project_type")
    if project_types:
        metadata["project_types"] = project_types
        metadata["project_type"] = project_types[0] if len(project_types) == 1 else "mixed"
    applications = _distinct_values_from_related_mrs(related_mrs, "application")
    if applications:
        metadata["applications"] = applications
        metadata["application"] = applications[0] if len(applications) == 1 else "Unmapped"
    release_lines = _distinct_values_from_related_mrs(related_mrs, "release_line")
    if release_lines:
        metadata["release_lines"] = release_lines
        metadata["release_line"] = release_lines[0] if len(release_lines) == 1 else "Unmapped release line"
    llm_models = _distinct_values_from_related_mrs(related_mrs, "llm_model_config")
    if llm_models:
        metadata["llm_model_configs"] = llm_models
        metadata["llm_model_config"] = llm_models[0]
    if project_context_parts:
        metadata["project_context_path"] = "multiple local working copies"
        metadata["project_context_files_count"] = "multiple"
        metadata["project_context"] = _balanced_project_context(project_context_parts, project_context_limit)
    if cross_mr_contract_parts:
        metadata["cross_mr_contract_context"] = "\n".join(cross_mr_contract_parts)[:6000]
    if git_version_context_parts:
        metadata["mr_type"] = "MULTI_MR_WITH_GIT_VERSION"
        metadata["git_version_review_context"] = "\n\n".join(git_version_context_parts)[:git_version_context_limit]
    if deduplicated_changed_files:
        metadata["deduplicated_changed_files"] = deduplicated_changed_files

    combined = ReviewInput(
        project="jira-issue",
        mr_url="",
        mr_id=f"multi-mr-{len(mr_inputs)}",
        jira_key=issue.key,
        sprint=issue.sprint,
        source_branch="multiple",
        target_branch="multiple",
        commit="multiple",
        title=issue.summary,
        changed_files=changed_files,
        raw_diff="\n\n".join(raw_diff_parts),
        metadata=metadata,
    )
    _attach_review_scope_metadata(combined, issue)
    return combined


def _ensure_detailed_jira_review_runtime() -> None:
    os.environ["REPORT_DETAIL_LEVEL"] = app_config_str("report.detail_level", "REPORT_DETAIL_LEVEL", "detailed")
    _set_min_int_env("LLM_MAX_TOKENS", 6000)
    _set_min_int_env("LLM_MAX_DIFF_CHARS", 60000)
    _set_min_int_env(
        "PROJECT_CONTEXT_MAX_CHARS",
        app_config_int("local_context.project_context_max_chars", "PROJECT_CONTEXT_MAX_CHARS", 32000),
    )


def _set_min_int_env(name: str, minimum: int) -> None:
    try:
        current = int(os.getenv(name, "0") or "0")
    except ValueError:
        current = 0
    if current < minimum:
        os.environ[name] = str(minimum)


def _balanced_project_context(parts: list[str], maximum: int) -> str:
    """Keep useful context from every MR instead of allowing early MRs to consume it all."""
    if not parts or maximum <= 0:
        return ""
    if len("\n\n".join(parts)) <= maximum:
        return "\n\n".join(parts)

    per_part = max(1200, maximum // len(parts))
    return "\n\n".join(_trim_project_context_part(part, per_part) for part in parts)[:maximum]


def _trim_project_context_part(part: str, maximum: int) -> str:
    if len(part) <= maximum:
        return part
    marker = "\n\nCodebase Memory persistent architecture context "
    marker_index = part.find(marker)
    if marker_index < 0 or maximum < 2400:
        return part[:maximum] + "\n[Project context allocation truncated]\n"

    source_budget = max(800, int(maximum * 0.7))
    memory_budget = max(400, maximum - source_budget - 80)
    return (
        part[:source_budget]
        + "\n[Project context allocation truncated]\n"
        + part[marker_index : marker_index + memory_budget]
    )


def _multi_mr_file_prefix(review_input: ReviewInput) -> str:
    project_path = str(review_input.metadata.get("gitlab_project_path") or review_input.project or "project")
    project_path = project_path.replace("\\", "/").strip("/")
    mr_id = str(review_input.mr_id or "mr")
    return f"{project_path}!{mr_id}"


def list_reviewer_merge_requests(
    reviewer: str,
    days: int = 7,
    state: str = "opened,merged",
    limit: int = 100,
) -> dict[str, Any]:
    client = GitLabClient(base_url=_gitlab_base_url())
    mrs = client.list_merge_requests_for_reviewer(reviewer=reviewer, days=days, state=state, limit=limit)
    items: list[dict[str, Any]] = []
    for item in mrs:
        web_url = str(item.get("web_url") or "")
        project_match = _git_tools_project_match_for_mr_url(
            web_url,
            target_branch=str(item.get("target_branch") or ""),
            source_branch=str(item.get("source_branch") or ""),
        )
        items.append(
            {
                "project": (item.get("references") or {}).get("full", "").split("!", 1)[0],
                "iid": item.get("iid"),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "updated_at": item.get("updated_at", ""),
                "source_branch": item.get("source_branch", ""),
                "target_branch": item.get("target_branch", ""),
                "web_url": web_url,
                "git_tools_project_match": project_match.get("status", ""),
                "git_tools_group": project_match.get("group", ""),
                "git_tools_module": project_match.get("module", ""),
                "responsible": project_match.get("responsible", ""),
                "project_name": project_match.get("project_name", ""),
                "project_type": project_match.get("project_type", ""),
                "application": project_match.get("application", ""),
                "release_line": project_match.get("release_line", ""),
                "release_lines": project_match.get("release_lines", []),
                "llm_model_config": project_match.get("llm_model", ""),
            }
        )
    return {
        "reviewer": reviewer,
        "days": days,
        "state": state,
        "found": len(mrs),
        "items": items,
    }


def _discover_sprint_merge_requests(
    jira: JiraClient,
    gitlab: GitLabClient,
    issues: list[JiraIssue],
    state: str = "opened,merged",
    limit: int = 200,
    progress: Any = None,
    source_label: str = "",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    issues_without_mrs: list[dict[str, str]] = []
    excluded_dev_branch_mrs: list[dict[str, Any]] = []
    excluded_state_mrs: list[dict[str, Any]] = []
    excluded_branch_type_mrs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    gitlab_search_fallback = app_config_bool("review.discovery.gitlab_search_fallback", "SPRINT_MR_GITLAB_SEARCH_FALLBACK", True)
    history_discovery = app_config_bool("review.discovery.history_discovery", "SPRINT_MR_HISTORY_DISCOVERY", True)
    history_limit = app_config_int("review.discovery.history_limit", "SPRINT_MR_HISTORY_LIMIT", 100)
    branch_discovery = app_config_bool("review.discovery.branch_discovery", "SPRINT_BRANCH_DISCOVERY", True)
    branch_discovery_mode = app_config_str("review.discovery.branch_discovery_mode", "SPRINT_BRANCH_DISCOVERY_MODE", "missing-only").strip().lower()
    filter_to_projects = _should_filter_sprint_projects()
    allowed_project_paths = _sprint_branch_project_paths(gitlab) if (branch_discovery or filter_to_projects) else []
    branch_project_paths = allowed_project_paths if branch_discovery else []

    issue_total = len(issues)
    for issue_index, issue in enumerate(issues, 1):
        if len(items) >= limit:
            break
        _progress(
            progress,
            "discovery-issue",
            f"Discovering merge requests for {issue.key} ({issue_index}/{issue_total})",
            index=issue_index,
            total=issue_total,
            jira_key=issue.key,
            source=source_label,
        )
        records, issue_errors, issue_state_excluded = _merge_request_records_for_issue(jira, gitlab, issue, state=state)
        errors.extend(issue_errors)
        excluded_state_mrs.extend(issue_state_excluded)
        should_search_gitlab = (not records and gitlab_search_fallback) or history_discovery
        if should_search_gitlab:
            try:
                for mr in gitlab.list_merge_requests_for_issue(issue.key, state=state, limit=history_limit):
                    if (
                        app_config_bool(
                            "review.discovery.require_strong_history_reference",
                            "SPRINT_MR_REQUIRE_STRONG_HISTORY_REFERENCE",
                            True,
                        )
                        and not _gitlab_search_has_strong_issue_reference(issue.key, mr)
                    ):
                        continue
                    web_url = str(mr.get("web_url") or "")
                    if web_url:
                        records.append(_mr_record_from_gitlab_search(issue.key, mr))
                if not _mr_state_filter_is_all(state):
                    known_urls = {str(record.get("mr_url") or "") for record in records}
                    known_urls.update(str(item.get("mr_url") or "") for item in excluded_state_mrs)
                    for mr in gitlab.list_merge_requests_for_issue(issue.key, state="all", limit=history_limit):
                        if (
                            app_config_bool(
                                "review.discovery.require_strong_history_reference",
                                "SPRINT_MR_REQUIRE_STRONG_HISTORY_REFERENCE",
                                True,
                            )
                            and not _gitlab_search_has_strong_issue_reference(issue.key, mr)
                        ):
                            continue
                        web_url = str(mr.get("web_url") or "")
                        actual_state = str(mr.get("state") or "")
                        if not web_url or web_url in known_urls:
                            continue
                        if _mr_state_in_filter(actual_state, state):
                            records.append(_mr_record_from_gitlab_search(issue.key, mr))
                        else:
                            exclusion = _mr_state_exclusion(_mr_record_from_gitlab_search(issue.key, mr), actual_state, state)
                            excluded_state_mrs.append(exclusion)
                            _print_mr_state_skip(
                                len(items) + len(excluded_dev_branch_mrs) + len(excluded_state_mrs),
                                limit,
                                f"{issue.key} {web_url}",
                                actual_state,
                                state,
                            )
            except Exception as exc:
                errors.append({"jira_key": issue.key, "stage": "gitlab-search", "error": str(exc)})
        should_scan_branches = branch_project_paths and (
            branch_discovery_mode in {"always", "all"} or not records
        )
        if should_scan_branches:
            branch_records, branch_errors = _branch_merge_request_records_for_issue(
                gitlab,
                issue,
                branch_project_paths,
                state=state,
            )
            records.extend(branch_records)
            errors.extend(branch_errors)

        if not records:
            issue_state_excluded = any(str(item.get("jira_key") or "").upper() == issue.key.upper() for item in excluded_state_mrs)
            issue_branch_type_excluded = any(str(item.get("jira_key") or "").upper() == issue.key.upper() for item in excluded_branch_type_mrs)
            if not issue_state_excluded and not issue_branch_type_excluded:
                issues_without_mrs.append({"jira_key": issue.key, "summary": issue.summary})
            continue

        issue_added = False
        issue_excluded = False
        for record in records:
            mr_url = record["mr_url"]
            if allowed_project_paths and not _mr_url_in_project_paths(mr_url, allowed_project_paths):
                continue
            if mr_url in seen_urls:
                continue
            try:
                record = _hydrate_mr_record_for_routing(gitlab, record)
            except Exception as exc:
                errors.append({"jira_key": issue.key, "mr_url": mr_url, "stage": "mr-routing", "error": str(exc)})
            project_match = _git_tools_project_match_for_mr_url(
                mr_url,
                target_branch=str(record.get("target_branch") or ""),
                source_branch=str(record.get("source_branch") or ""),
            )
            record_with_issue = {**record, "jira_key": issue.key}
            branch_type_exclusion = _jira_sprint_branch_type_exclusion(record_with_issue)
            if branch_type_exclusion:
                excluded_branch_type_mrs.append(branch_type_exclusion)
                issue_excluded = True
                _print_branch_type_skip(
                    len(items) + len(excluded_branch_type_mrs),
                    limit,
                    f"{issue.key} {mr_url}",
                    str(record.get("source_branch") or ""),
                    str(branch_type_exclusion.get("ignored_branch_type") or ""),
                )
                continue
            if _is_dev_version_branch(str(record.get("target_branch") or ""), project_match):
                exclusion = _dev_branch_exclusion(record_with_issue, project_match)
                excluded_dev_branch_mrs.append(exclusion)
                issue_excluded = True
                _print_dev_branch_skip(
                    len(items) + len(excluded_dev_branch_mrs),
                    limit,
                    f"{issue.key} {mr_url}",
                    str(record.get("target_branch") or ""),
                    exclusion.get("dev_branch", []),
                )
                continue
            seen_urls.add(mr_url)
            issue_added = True
            items.append(
                {
                    "jira_key": issue.key,
                    "jira_summary": issue.summary,
                    "jira_status": issue.status,
                    "jira_assignee": issue.assignee,
                    "mr_url": mr_url,
                    "source": record.get("source", ""),
                    "gitlab_project": record.get("project_path", ""),
                    "state": record.get("state", ""),
                    "mr_state": record.get("state", ""),
                    "source_branch": record.get("source_branch", ""),
                    "target_branch": record.get("target_branch", ""),
                    "mr_id": record.get("mr_id") or record.get("iid", ""),
                    "head_sha": record.get("head_sha") or record.get("commit") or record.get("sha", ""),
                    "base_sha": record.get("base_sha", ""),
                    "merge_commit_sha": record.get("merge_commit_sha", ""),
                    "squash_commit_sha": record.get("squash_commit_sha", ""),
                    "git_tools_project_match": project_match.get("status", ""),
                    "git_tools_group": project_match.get("group", ""),
                    "git_tools_module": project_match.get("module", ""),
                    "responsible": project_match.get("responsible", ""),
                    "project_name": project_match.get("project_name", ""),
                    "project_type": project_match.get("project_type", ""),
                    "application": project_match.get("application", ""),
                    "release_line": project_match.get("release_line", ""),
                    "release_lines": project_match.get("release_lines", []),
                    "llm_model_config": project_match.get("llm_model", ""),
                    "dev_branch": _configured_dev_branches(project_match),
                }
            )
            if len(items) >= limit:
                break
        issue_state_excluded = any(str(item.get("jira_key") or "").upper() == issue.key.upper() for item in excluded_state_mrs)
        issue_branch_type_excluded = any(str(item.get("jira_key") or "").upper() == issue.key.upper() for item in excluded_branch_type_mrs)
        if not issue_added and not issue_excluded and not issue_state_excluded and not issue_branch_type_excluded:
            issues_without_mrs.append({"jira_key": issue.key, "summary": issue.summary})

    return {
        "mrs": items,
        "issues_without_mrs": issues_without_mrs,
        "excluded_dev_branch_mrs": excluded_dev_branch_mrs,
        "excluded_branch_type_mrs": excluded_branch_type_mrs,
        "excluded_state_mrs": excluded_state_mrs,
        "errors": errors,
    }


def _should_filter_sprint_projects() -> bool:
    configured = app_config_str("review.discovery.filter_to_projects", "SPRINT_FILTER_TO_PROJECTS", "").strip().lower()
    if configured:
        return configured not in {"0", "false", "no"}
    return bool(
        os.getenv("GIT_TOOLS_CONFIG", "").strip()
        or os.getenv("GIT_TOOLS_GROUPS", "").strip()
        or os.getenv("SPRINT_GITLAB_PROJECTS", "").strip()
    )


def _mr_url_in_project_paths(mr_url: str, allowed_project_paths: list[str]) -> bool:
    try:
        ref = parse_mr_url(mr_url)
    except Exception:
        return False
    project_path = normalize_project_path(ref.project_path)
    allowed = {normalize_project_path(path) for path in allowed_project_paths}
    return project_path in allowed


def _mr_record_from_gitlab_search(issue_key: str, mr: dict[str, Any]) -> dict[str, str]:
    return {
        "jira_key": issue_key,
        "mr_url": str(mr.get("web_url") or ""),
        "source": "gitlab-search",
        "project_path": str((mr.get("references") or {}).get("full", "")).split("!", 1)[0],
        "source_branch": str(mr.get("source_branch") or ""),
        "target_branch": str(mr.get("target_branch") or ""),
        "state": str(mr.get("state") or ""),
        "mr_id": str(mr.get("iid") or ""),
        "head_sha": str(mr.get("sha") or ""),
        "base_sha": str((mr.get("diff_refs") or {}).get("base_sha") or ""),
    }


def _gitlab_search_has_strong_issue_reference(issue_key: str, mr: dict[str, Any]) -> bool:
    """Reject broad history-search matches that only mention Jira in generated text.

    GitLab's search can match descriptions and accumulated release notes. That
    made release-branch and Company_GIT_VERSION MRs look like direct Issue MRs
    even when neither their title nor source branch identified the Jira issue.
    Jira links/development-panel records remain authoritative and bypass this
    fallback-only guard.
    """
    key = issue_key.strip().upper()
    if not key:
        return False
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", re.IGNORECASE)
    return any(
        pattern.search(str(mr.get(field) or ""))
        for field in ("title", "source_branch")
    )


def _merge_request_records_for_issue(
    jira: JiraClient,
    gitlab: GitLabClient,
    issue: JiraIssue,
    state: str = "opened,merged",
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, Any]]]:
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    excluded_state_mrs: list[dict[str, Any]] = []

    _append_mr_urls(records, _extract_gitlab_mr_urls([issue.summary, issue.final_description], gitlab.base_url), 'jira-issue-fields')

    if app_config_bool("review.discovery.jira_remote_link", "SPRINT_JIRA_REMOTE_LINK_DISCOVERY", True):
        try:
            remote_links = jira.fetch_issue_remote_links(issue.key)
            _append_mr_urls(records, _extract_gitlab_mr_urls(remote_links, gitlab.base_url), "jira-remote-link")
        except Exception as exc:
            errors.append({"jira_key": issue.key, "stage": "jira-remote-link", "error": str(exc)})

    if app_config_bool("review.discovery.jira_dev_panel", "SPRINT_JIRA_DEV_PANEL_DISCOVERY", True):
        try:
            dev_details = jira.fetch_issue_development_details(issue.id)
            _append_mr_urls(records, _extract_gitlab_mr_urls(dev_details, gitlab.base_url), "jira-development-panel")
        except Exception as exc:
            errors.append({"jira_key": issue.key, "stage": "jira-development-panel", "error": str(exc)})

    if records and not _mr_state_filter_is_all(state):
        kept: list[dict[str, str]] = []
        for record in records:
            matches, actual_state = _mr_state_matches(gitlab, record["mr_url"], state, issue.key, errors)
            if matches:
                record["state"] = actual_state or record.get("state", "")
                kept.append(record)
                continue
            exclusion = _mr_state_exclusion(record, actual_state, state)
            excluded_state_mrs.append(exclusion)
            _print_mr_state_skip(len(excluded_state_mrs), len(records), f"{issue.key} {record['mr_url']}", actual_state, state)
        records = kept

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        if record["mr_url"] in seen:
            continue
        seen.add(record["mr_url"])
        unique.append(record)
    return unique, errors, excluded_state_mrs


def _branch_merge_request_records_for_issue(
    gitlab: GitLabClient,
    issue: JiraIssue,
    project_paths: list[str],
    state: str = "opened,merged",
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    branch_limit = app_config_int("review.discovery.branch_search_limit", "SPRINT_BRANCH_SEARCH_LIMIT", 20)
    mr_limit = app_config_int("review.discovery.branch_mr_limit", "SPRINT_BRANCH_MR_LIMIT", 20)

    for project_path in project_paths:
        try:
            branches = gitlab.list_project_branches(project_path, search=issue.key, limit=branch_limit)
        except Exception as exc:
            errors.append({"jira_key": issue.key, "project": project_path, "stage": "branch-search", "error": str(exc)})
            continue

        for branch in branches:
            branch_name = str(branch.get("name") or "")
            if not _branch_matches_issue(branch_name, issue.key):
                continue
            try:
                mrs = gitlab.list_project_merge_requests(
                    project_path,
                    source_branch=branch_name,
                    state=state,
                    limit=mr_limit,
                )
            except Exception as exc:
                errors.append(
                    {
                        "jira_key": issue.key,
                        "project": project_path,
                        "branch": branch_name,
                        "stage": "branch-mr-search",
                        "error": str(exc),
                    }
                )
                continue
            for mr in mrs:
                web_url = str(mr.get("web_url") or "")
                if not web_url:
                    continue
                records.append(
                    {
                        "mr_url": web_url,
                        "source": "gitlab-branch",
                        "project_path": project_path,
                        "source_branch": branch_name,
                        "target_branch": str(mr.get("target_branch") or ""),
                    }
                )

    return records, errors


def _sprint_branch_project_paths(gitlab: GitLabClient) -> list[str]:
    values: list[str] = []
    git_tools_values = _git_tools_repository_urls()
    values.extend(git_tools_values)
    configured = app_config_str("review.discovery.extra_gitlab_projects", "SPRINT_GITLAB_PROJECTS", "")
    for item in re.split(r"[,;\n]+", configured):
        item = item.strip()
        if item:
            values.append(item)

    has_explicit_scope = bool(
        os.getenv("GIT_TOOLS_CONFIG", "").strip()
        or os.getenv("GIT_TOOLS_GROUPS", "").strip()
        or configured.strip()
    )
    if not has_explicit_scope and not git_tools_values:
        try:
            for project in load_projects():
                values.extend(project.repository_urls)
        except Exception:
            pass

    paths: list[str] = []
    for value in values:
        project_path = _gitlab_project_path_from_value(value, gitlab.base_url)
        if project_path and project_path not in paths:
            paths.append(project_path)

    limit = app_config_int("review.discovery.branch_project_limit", "SPRINT_BRANCH_PROJECT_LIMIT", 200)
    return paths[:limit]


def _git_tools_repository_urls() -> list[str]:
    config_path = git_tools_config_path()
    if not config_path.exists():
        return []
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    groups = _git_tools_groups()
    if yaml is None:
        return _repository_urls_from_git_tools_text(text, groups)
    try:
        payload = yaml.safe_load(text)
    except Exception:
        return _repository_urls_from_git_tools_text(text, groups)
    values = _collect_repository_urls(payload)
    if not groups:
        return values
    filtered: list[str] = []
    for group_name in groups:
        section = payload.get(group_name) if isinstance(payload, dict) else None
        filtered.extend(_collect_repository_urls(section))
    return filtered or values


def _git_tools_groups() -> set[str]:
    return set(app_config_list("git_tools.groups", "GIT_TOOLS_GROUPS", []))


def _collect_repository_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        repo_url = value.get("repository_url")
        if isinstance(repo_url, str) and repo_url.strip():
            urls.append(_clean_repository_url(repo_url))
        for item in value.values():
            urls.extend(_collect_repository_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_repository_urls(item))
    return urls


def _repository_urls_from_git_tools_text(text: str, groups: set[str] | None = None) -> list[str]:
    urls: list[str] = []
    selected_groups = groups or set()
    current_group = ""
    for line in (text or "").splitlines():
        group_match = re.match(r"^([^\s:#][^:#]*):\s*$", line)
        if group_match:
            current_group = group_match.group(1).strip()
            continue
        if selected_groups and current_group not in selected_groups:
            continue
        match = re.search(r"repository_url:\s*([^\r\n]+)", line)
        if match:
            url = _clean_repository_url(match.group(1))
            if url:
                urls.append(url)
    return urls


def _clean_repository_url(value: str) -> str:
    text = (value or "").strip().strip("'\"")
    match = re.search(r"https?://[^\s\"']+?\.git\b", text)
    return match.group(0) if match else text


def _gitlab_project_path_from_value(value: str, fallback_base_url: str) -> str:
    text = (value or "").strip().strip("'\"")
    if not text:
        return ""
    try:
        _base_url, project_path = parse_repository_url(text, fallback_base_url=fallback_base_url)
        return project_path.strip("/")
    except Exception:
        return text.removesuffix(".git").strip("/")


def _branch_matches_issue(branch_name: str, issue_key: str) -> bool:
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(issue_key)}(?!\d)", branch_name or "", re.I))


def _append_mr_urls(records: list[dict[str, str]], urls: list[str], source: str) -> None:
    for url in urls:
        records.append({"mr_url": url, "source": source})


def _extract_gitlab_mr_urls(value: Any, gitlab_base_url: str) -> list[str]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    base = re.escape(gitlab_base_url.rstrip("/"))
    patterns = [
        rf"{base}/[^\s\"'<>)]+/-/merge_requests/\d+",
        r"https?://[^\s\"'<>)]+/-/merge_requests/\d+",
    ]
    urls: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text):
            url = match.rstrip(".,;)")
            if url not in urls:
                urls.append(url)
    return urls


def _mr_state_filter_values(state: str) -> list[str]:
    text = (state or "opened,merged").strip().lower()
    if not text:
        return ["opened", "merged"]
    if text in {"all", "*"}:
        return ["all"]
    aliases = {"open": "opened", "opened": "opened", "merge": "merged", "merged": "merged"}
    values: list[str] = []
    for raw in re.split(r"[,;|/\s]+", text):
        item = aliases.get(raw.strip(), raw.strip())
        if item and item not in values:
            values.append(item)
    return values or ["opened", "merged"]


def _mr_state_filter_is_all(state: str) -> bool:
    return _mr_state_filter_values(state) == ["all"]


def _mr_state_in_filter(actual_state: str, state_filter: str) -> bool:
    values = _mr_state_filter_values(state_filter)
    return values == ["all"] or (actual_state or "").strip().lower() in values


def _mr_state_matches(
    gitlab: GitLabClient,
    mr_url: str,
    state: str,
    jira_key: str,
    errors: list[dict[str, str]],
) -> tuple[bool, str]:
    if _mr_state_filter_is_all(state):
        return True, ""
    try:
        ref = parse_mr_url(mr_url)
        client = gitlab if ref.base_url.rstrip("/") == gitlab.base_url.rstrip("/") else GitLabClient(ref.base_url)
        mr = client.fetch_merge_request(ref.project_path, ref.iid)
        actual_state = str(mr.get("state") or "")
        return _mr_state_in_filter(actual_state, state), actual_state
    except Exception as exc:
        errors.append({"jira_key": jira_key, "mr_url": mr_url, "stage": "mr-state-filter", "error": str(exc)})
        return True, ""


def _hydrate_mr_record_for_routing(gitlab: GitLabClient, record: dict[str, Any]) -> dict[str, Any]:
    """Fill source/target branch metadata before fetching a potentially huge diff.

    Jira remote links often only retain the MR URL.  Release-resource routing
    needs the source branch, but full review input creation also downloads the
    complete diff and local context.  Hydrating the small MR payload first
    keeps Company Config/SCR resources out of ordinary Jira review promptly.
    """
    if (
        str(record.get("source_branch") or "").strip()
        and str(record.get("target_branch") or "").strip()
        and str(record.get("head_sha") or record.get("commit") or "").strip()
        and str(record.get("mr_id") or record.get("iid") or "").strip()
    ):
        return record
    mr_url = str(record.get("mr_url") or "").strip()
    if not mr_url:
        return record
    ref = parse_mr_url(mr_url)
    client = gitlab if ref.base_url.rstrip("/") == gitlab.base_url.rstrip("/") else GitLabClient(ref.base_url)
    payload = client.fetch_merge_request(ref.project_path, ref.iid)
    return {
        **record,
        "project_path": str(record.get("project_path") or ref.project_path),
        "source_branch": str(payload.get("source_branch") or record.get("source_branch") or ""),
        "target_branch": str(payload.get("target_branch") or record.get("target_branch") or ""),
        "state": str(payload.get("state") or record.get("state") or ""),
        "mr_id": str(payload.get("iid") or record.get("mr_id") or ref.iid),
        "head_sha": str(payload.get("sha") or record.get("head_sha") or record.get("commit") or ""),
        "base_sha": str((payload.get("diff_refs") or {}).get("base_sha") or record.get("base_sha") or ""),
        "merge_commit_sha": str(payload.get("merge_commit_sha") or record.get("merge_commit_sha") or ""),
        "squash_commit_sha": str(payload.get("squash_commit_sha") or record.get("squash_commit_sha") or ""),
    }


def review_from_diff_text(
    diff_text: str,
    project: str = "",
    jira_key: str = "",
    sprint: str = "",
    source_branch: str = "",
    target_branch: str = "",
    context_repo: Path | str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> ReviewResult:
    detected_jira = jira_key or detect_jira_key(" ".join([source_branch, target_branch, diff_text[:1000]]))
    association = parse_issue_association(
        " ".join([source_branch, target_branch, diff_text[:4000]]),
        explicit_action_issue=detected_jira,
    )
    review_input = ReviewInput(
        project=project,
        jira_key=detected_jira,
        sprint=sprint,
        source_branch=source_branch,
        target_branch=target_branch,
        changed_files=parse_unified_diff(diff_text),
        raw_diff=diff_text,
        metadata=association_to_metadata(association),
    )
    if extra_metadata:
        review_input.metadata.update(extra_metadata)
    _attach_project_context(review_input, context_repo)
    return analyze(review_input)


def review_from_git_repo(
    repo: Path,
    source_branch: str,
    target_branch: str,
    project: str = "",
    jira_key: str = "",
    sprint: str = "",
    extra_metadata: dict[str, Any] | None = None,
) -> ReviewResult:
    if not repo.exists():
        raise FileNotFoundError(f"Repo path does not exist: {repo}")
    diff_text = _git_diff(repo, source_branch, target_branch)
    return review_from_diff_text(
        diff_text=diff_text,
        project=project or repo.name,
        jira_key=jira_key,
        sprint=sprint,
        source_branch=source_branch,
        target_branch=target_branch,
        context_repo=repo,
        extra_metadata=extra_metadata,
    )


def list_issue_branch_candidates(
    jira_key: str,
    target_branch: str = "",
    groups: str = "",
    projects: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    issue_key = (jira_key or "").strip().upper()
    if not issue_key:
        raise ValueError("--jira is required for --issue-branches.")
    entries = workspace_entries_for_issue_review(groups=groups, projects=projects)
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for entry in entries:
        if len(records) >= limit:
            break
        try:
            records.extend(_local_issue_branch_records(entry, issue_key, target_branch=target_branch))
        except Exception as exc:
            errors.append({"repo": str(entry.local_path), "project_path": entry.project_path, "error": str(exc)})
    return {"jira_key": issue_key, "found": len(records[:limit]), "items": records[:limit], "errors": errors}


def review_issue_branches(
    jira_key: str,
    target_branch: str = "",
    groups: str = "",
    projects: str = "",
    limit: int = 200,
    list_only: bool = False,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    candidates = list_issue_branch_candidates(
        jira_key=jira_key,
        target_branch=target_branch,
        groups=groups,
        projects=projects,
        limit=limit,
    )
    if list_only:
        return candidates

    target_output_dir = _batch_output_dir(output_dir)
    tracker = _resume_tracker(
        "issue-branches",
        target_output_dir,
        {
            "jira_key": candidates["jira_key"],
            "target_branch": target_branch,
            "groups": groups,
            "projects": projects,
            "limit": limit,
        },
    )
    reviewed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = list(candidates["errors"])
    skipped_completed = 0
    _print_batch_start("issue-branches", len(candidates["items"]), target_output_dir, tracker)
    for index, item in enumerate(candidates["items"], 1):
        resume_key = _issue_branch_resume_key(item, candidates["jira_key"])
        if tracker.is_done(resume_key):
            skipped_item = tracker.done_summary(resume_key)
            _print_skipped(index, len(candidates["items"]), f"{item['project_path']} {item['source_branch']}", skipped_item)
            reviewed.append(skipped_item)
            skipped_completed += 1
            continue
        print(f"[{index}/{len(candidates['items'])}] Reviewing {item['project_path']} {item['source_branch']}", file=sys.stderr, flush=True)
        tracker.mark_started(resume_key, item)
        try:
            result = review_from_git_repo(
                repo=Path(item["repo"]),
                source_branch=item["source_branch"],
                target_branch=item["target_branch"],
                project=item.get("module") or item["project_path"].split("/")[-1],
                jira_key=candidates["jira_key"],
                extra_metadata={
                    "gitlab_project_path": item["project_path"],
                    "local_workspace_group": item.get("group", ""),
                    "local_workspace_module": item.get("module", ""),
                    "local_workspace_repo": item["repo"],
                    "review_input_mode": "issue-branches",
                },
            )
            report_path = save_report(result, target_output_dir)
            gitnexus = save_to_gitnexus(result, report_path)
            append_review_history(result, report_path)
            reviewed_item = {
                **item,
                "report": str(report_path),
                "gitnexus_report": gitnexus["report_path"],
                "conclusion": result.conclusion,
                "severity_counts": result.severity_counts,
                "finding_count": len(result.findings),
            }
            reviewed.append(reviewed_item)
            tracker.mark_done(resume_key, reviewed_item)
            _print_done(index, len(candidates["items"]), f"{item['project_path']} {item['source_branch']}", reviewed_item)
        except KeyboardInterrupt:
            tracker.mark_interrupted(resume_key, item)
            raise
        except Exception as exc:
            errors.append({"repo": item.get("repo", ""), "source_branch": item.get("source_branch", ""), "error": str(exc)})
            tracker.mark_failed(resume_key, str(exc), item)
            _print_failed(index, len(candidates["items"]), f"{item.get('project_path', '')} {item.get('source_branch', '')}", str(exc))
    return {
        "jira_key": candidates["jira_key"],
        "found": candidates["found"],
        "processed": len(reviewed),
        "skipped_completed": skipped_completed,
        "resume_state": _resume_path(tracker),
        "errors": errors,
        "items": reviewed,
    }


def run_review_from_payload(payload: dict[str, Any], progress: Any = None) -> dict[str, Any]:
    mode = payload.get("mode", "diff")
    output_name = _text(payload.get("output_name")) or None
    output_dir = _path_or_default(payload.get("output_dir"))
    context_repo = _text(payload.get("context_repo")) or None
    language = _text(payload.get("report_language")) or None
    speed = _text(payload.get("speed"))
    report_min_severity = _text(payload.get("report_min_severity"))
    report_owner = _text(payload.get("web_report_owner"))
    force_rerun = bool(payload.get("rerun_confirmed"))
    if language:
        set_app_runtime_override("REPORT_LANGUAGE", language)
    if speed:
        apply_speed_profile(speed, force=True)
    if report_min_severity:
        set_app_runtime_override("REPORT_MIN_SEVERITY", normalize_severity(report_min_severity))
    configured_state = app_config_str("review.mr_states", "SPRINT_MR_STATE", "opened,merged")
    configured_limit = app_config_int("review.mr_limit", "SPRINT_MR_LIMIT", 200)
    _progress(progress, "request", f"Received {mode} review request", mode=mode)

    if mode == "sprint-preflight":
        preflight = sprint_review_preflight(
            _text(payload.get("sprint")),
            _text(payload.get("jira_project_key")) or app_config_str("jira.project_key", "JIRA_PROJECT_KEY", "ECHNL"),
        )
        return {
            "ok": bool(preflight.get("valid") and preflight.get("accessible")),
            "mode": "sprint-preflight",
            "preflight": preflight,
            "markdown": json.dumps(preflight, ensure_ascii=False, indent=2),
            "conclusion": "Sprint preflight ready" if preflight.get("valid") else "Sprint preflight failed",
            "finding_count": 0,
            "severity_counts": {},
        }
    if mode == "jira-filter":
        summary = review_jira_filter_merge_requests(
            filter_id=_text(payload.get("jira_filter")),
            state=_text(payload.get("state")) or configured_state,
            limit=int(payload.get("limit") or configured_limit),
            output_dir=output_dir,
            context_repo=context_repo,
            progress=progress,
            report_owner=report_owner,
            force_rerun=force_rerun,
        )
        return {
            "ok": not bool(summary.get("errors")),
            "mode": "jira-filter",
            "summary": summary,
            "markdown": json.dumps(summary, ensure_ascii=False, indent=2),
            "conclusion": "Jira filter review completed" if not summary.get("errors") else "Jira filter review completed with errors",
            "finding_count": sum(int(item.get("finding_count", 0) or 0) for item in summary.get("items", []) if isinstance(item, dict)),
            "severity_counts": _sum_summary_severity_counts(summary),
        }
    if mode == "sprint":
        project_key = _text(payload.get("jira_project_key")) or app_config_str("jira.project_key", "JIRA_PROJECT_KEY", "ECHNL")
        preflight = sprint_review_preflight(_text(payload.get("sprint")), project_key)
        if not preflight.get("valid") or not preflight.get("accessible") or preflight.get("empty"):
            raise ValueError(str(preflight.get("error") or "Sprint is invalid, empty, or inaccessible."))
        effective_review_mode = str(preflight.get("review_mode") or "batch-preview")
        requested_review_mode = _text(payload.get("review_mode"))
        if requested_review_mode and requested_review_mode != effective_review_mode:
            raise ValueError("Sprint readiness changed after preflight. Refresh the Sprint and confirm again.")
        if effective_review_mode == "batch-preview" and not bool(payload.get("batch_preview_confirmed")):
            raise ValueError("Batch Issue Preview confirmation is required for a Sprint that is not Development Done.")
        summary = review_sprint_merge_requests(
            sprint=_text(payload.get("sprint")),
            jira_project_key=project_key,
            state=_text(payload.get("state")) or configured_state,
            limit=int(payload.get("limit") or configured_limit),
            output_dir=output_dir,
            context_repo=context_repo,
            progress=progress,
            report_owner=report_owner,
            force_rerun=force_rerun,
            workflow_review_mode=effective_review_mode,
        )
        return {
            "ok": not bool(summary.get("errors")),
            "mode": "sprint",
            "summary": summary,
            "review_mode": effective_review_mode,
            "sprint_preflight": preflight,
            "markdown": json.dumps(summary, ensure_ascii=False, indent=2),
            "conclusion": "Sprint review completed" if not summary.get("errors") else "Sprint review completed with errors",
            "finding_count": sum(int(item.get("finding_count", 0) or 0) for item in summary.get("items", []) if isinstance(item, dict)),
            "severity_counts": _sum_summary_severity_counts(summary),
        }
    if mode == "jira":
        jira_keys = parse_jira_issue_keys(_text(payload.get("jira_key")))
        review_kwargs = {
            "state": _text(payload.get("state")) or configured_state,
            "limit": int(payload.get("limit") or configured_limit),
            "output_dir": output_dir,
            "context_repo": context_repo,
            "progress": progress,
            "report_owner": report_owner,
            "force_rerun": force_rerun,
        }
        summary = (
            review_jira_issues_merge_requests(jira_keys=jira_keys, **review_kwargs)
            if len(jira_keys) > 1
            else review_jira_issue_merge_requests(jira_key=jira_keys[0], **review_kwargs)
        )
        return {
            "ok": not bool(summary.get("errors")),
            "mode": "jira",
            "summary": summary,
            "markdown": json.dumps(summary, ensure_ascii=False, indent=2),
            "conclusion": "Jira review completed" if not summary.get("errors") else "Jira review completed with errors",
            "finding_count": sum(int(item.get("finding_count", 0) or 0) for item in summary.get("items", []) if isinstance(item, dict)),
            "severity_counts": _sum_summary_severity_counts(summary),
        }
    if mode == "release-gate":
        mr_url = _text(payload.get("mr_url")).strip()
        if not mr_url:
            raise ValueError("GIT_VERSION MR URL is required for Release Gate.")
        result = review_release_gate_from_mr_url(
            mr_url=mr_url,
            jira_key=_text(payload.get("jira_key")),
            sprint=_text(payload.get("sprint")),
            context_repo=context_repo,
            progress=progress,
        )
    elif mode == "mr":
        _progress(progress, "fetch-mr", f"Fetching MR {_text(payload.get('mr_url'))}", mr_url=_text(payload.get("mr_url")))
        result = review_from_mr_url(
            mr_url=_text(payload.get("mr_url")),
            jira_key=_text(payload.get("jira_key")),
            sprint=_text(payload.get("sprint")),
            context_repo=context_repo,
        )
    elif mode == "repo":
        _progress(progress, "diff", f"Preparing local repo diff {_text(payload.get('repo'))}", repo=_text(payload.get("repo")))
        result = review_from_git_repo(
            repo=Path(_text(payload.get("repo"))),
            source_branch=_text(payload.get("source_branch")),
            target_branch=_text(payload.get("target_branch")),
            project=_text(payload.get("project")),
            jira_key=_text(payload.get("jira_key")),
            sprint=_text(payload.get("sprint")),
        )
    else:
        _progress(progress, "diff", "Preparing pasted diff review")
        result = review_from_diff_text(
            diff_text=_text(payload.get("diff_text")),
            project=_text(payload.get("project")),
            jira_key=_text(payload.get("jira_key")),
            sprint=_text(payload.get("sprint")),
            source_branch=_text(payload.get("source_branch")),
            target_branch=_text(payload.get("target_branch")),
            context_repo=context_repo,
        )

    _progress(progress, "saving", "Saving report")
    if report_owner:
        result.review_input.metadata["web_report_owner"] = report_owner
    report_path = save_report(result, output_dir, output_name, language=language)
    gitnexus = save_to_gitnexus(result, report_path)
    append_review_history(result, report_path)
    _progress(progress, "done", f"DONE {result.review_input.jira_key or result.review_input.project}", report=str(report_path), severity_counts=result.severity_counts, finding_count=len(result.findings))
    release_gate = result.review_input.metadata.get("release_gate") or {}
    gate_status = str(release_gate.get("status") or "").strip().upper()
    conclusion = result.conclusion
    if mode == "release-gate":
        conclusion = f"Release Gate {gate_status or 'UNKNOWN'}"
    return {
        "ok": True,
        "mode": mode,
        "report": str(report_path),
        "report_name": report_path.name,
        "gitnexus": gitnexus,
        "markdown": render_markdown(result, language=language),
        "conclusion": conclusion,
        "finding_count": len(result.findings),
        "severity_counts": result.severity_counts,
        "release_gate": release_gate if isinstance(release_gate, dict) else {},
    }


def _sum_summary_severity_counts(summary: dict[str, Any]) -> dict[str, int]:
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
    for item in summary.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        severity_counts = item.get("severity_counts") or {}
        if not isinstance(severity_counts, dict):
            continue
        for severity in counts:
            counts[severity] += int(severity_counts.get(severity, 0) or 0)
    return counts


def _path_or_default(value: Any) -> Path:
    text = _text(value)
    return Path(text).expanduser() if text else report_output_dir()


def attach_git_version_locked_repository_reviews(review_input: ReviewInput, client: GitLabClient) -> None:
    if not app_config_bool("git_version.deep_review", "GIT_VERSION_DEEP_REVIEW", True):
        review_input.metadata["git_version_deep_review"] = "disabled"
        return
    context = extract_git_version_lock_context(review_input)
    if not context.get("is_git_version_mr"):
        return

    # This must be available before analyzer enriches the final report so the
    # deep-lock fetch failures can be rendered as release-gate findings.
    review_input.metadata["mr_type"] = "GIT_VERSION"
    _attach_release_gate_project_scope(review_input)

    max_repos = app_config_int("git_version.source_review_max_repos", "GIT_VERSION_SOURCE_REVIEW_MAX_REPOS", 30)
    max_files_per_repo = app_config_int("git_version.source_review_max_files_per_repo", "GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO", 80)
    max_context_chars = app_config_int("git_version.source_diff_context_max_chars", "GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS", 100000)

    tasks = _git_version_review_tasks(context)
    reviews: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    validation_errors: list[dict[str, str]] = []
    raw_parts: list[str] = []
    context_parts: list[str] = []
    context_budget = max_context_chars
    seen_files: set[tuple[str, str]] = set()
    previous_locks = _fetch_previous_git_version_locks(context, client)
    previous_build_locks = _fetch_previous_build_resource_locks(context, client)
    release_gate_resources: list[dict[str, Any]] = []
    release_gate_errors: list[dict[str, str]] = []
    source_tasks = [task for task in tasks if task.get("kind") == "source"]
    build_tasks = [task for task in tasks if task.get("kind") == "build-resource"]
    if not source_tasks:
        release_gate_errors.append(
            {
                "severity": "High",
                "file_path": "GIT_VERSION",
                "title": "GIT_VERSION has no locked development repository entries",
                "detail": "The release gate cannot establish the Sprint code scope because no git_version.yml repository lock was parsed.",
                "recommendation": "Include the versioned git_version.yml with immutable development repository commits, then re-scan the GIT_VERSION MR.",
            }
        )
    if not build_tasks:
        release_gate_errors.append(
            {
                "severity": "High",
                "file_path": "GIT_VERSION",
                "title": "GIT_VERSION has no locked build repository entry",
                "detail": "The release gate cannot verify Company Config/SCR resource inclusion without a build.yml build-repository commit lock.",
                "recommendation": "Include the versioned build.yml and lock the build repository commit after required build resources are merged.",
            }
        )
    if previous_locks:
        review_input.metadata["previous_git_version_locks"] = list(previous_locks.values())

    for task in tasks[:max_repos]:
        module = task["module"]
        repository_url = task["repository_url"]
        commit = task["commit"]
        if not repository_url or not commit:
            continue
        try:
            base_url, project_path = parse_repository_url(repository_url, fallback_base_url=client.base_url)
            repo_client = client if base_url.rstrip("/") == client.base_url.rstrip("/") else GitLabClient(base_url=base_url)
            commit_payload = repo_client.fetch_commit(project_path, commit)
            compare_context = (
                _compare_source_with_previous_lock(task, previous_locks, repo_client, project_path, commit)
                if task.get("kind") == "source"
                else _compare_build_resource_with_previous_lock(task, previous_build_locks, repo_client, project_path, commit)
            )
            diffs = (
                compare_context.get("diffs")
                if compare_context.get("diffs") is not None
                else repo_client.fetch_commit_diff(project_path, commit)
            )[:max_files_per_repo]
            resource_validation = (
                _validate_build_resource_commit(task, repo_client, project_path, commit)
                if task.get("kind") == "build-resource"
                else {}
            )
            if resource_validation.get("errors"):
                validation_errors.extend(resource_validation["errors"])
                for error in resource_validation["errors"]:
                    release_gate_errors.append(
                        {
                            "severity": "High",
                            "file_path": str(error.get("build_file") or task.get("build_file") or "GIT_VERSION"),
                            "title": "Locked build repository commit is missing required build resources",
                            "detail": (
                                f"{error.get('role', 'resource')} '{error.get('path', '-')}' cannot be read "
                                f"at locked commit {error.get('commit', commit)}: {error.get('error', '-')}."
                            ),
                            "recommendation": "Lock build.yml to a build repository commit that contains the required build.yml and git_version.yml resources, then re-scan.",
                        }
                    )
            issue_keys = _issue_keys(
                " ".join(
                    [
                        str(commit_payload.get("title") or ""),
                        str(commit_payload.get("message") or ""),
                        str(commit_payload.get("author_name") or ""),
                        str(compare_context.get("commit_messages") or ""),
                    ]
                )
            )
            review_record = {
                "kind": task["kind"],
                "module": module,
                "repository_url": repository_url,
                "project_path": project_path,
                "branch": task.get("branch", ""),
                "commit": commit,
                "title": commit_payload.get("title", ""),
                "message": commit_payload.get("message", ""),
                "committed_date": commit_payload.get("committed_date", ""),
                "issue_keys": issue_keys,
                "files_count": len(diffs),
            }
            if compare_context:
                review_record.update(
                    {
                        "compare_from_commit": compare_context.get("from_commit", ""),
                        "compare_to_commit": compare_context.get("to_commit", commit),
                        "compare_commits_count": compare_context.get("commits_count", 0),
                        "previous_git_version_file": compare_context.get("previous_git_version_file", ""),
                    }
                )
            if resource_validation:
                review_record["resource_validation"] = resource_validation
            if task.get("kind") == "build-resource":
                release_resource = _release_gate_build_resource(
                    task,
                    repo_client,
                    project_path,
                    commit,
                    diffs,
                    compare_context,
                )
                release_gate_resources.append(release_resource)
                release_gate_errors.extend(release_resource.get("errors") or [])
                review_record["release_gate_resource"] = release_resource
            reviews.append(review_record)
            header = (
                f"GIT_VERSION locked {task['kind']} repository: {module}\n"
                f"repo: {repository_url}\nbranch: {task.get('branch', '-')}\ncommit: {commit}\n"
                f"title: {commit_payload.get('title', '-')}\nissues: {', '.join(issue_keys) or '-'}\n"
            )
            if compare_context:
                header += (
                    "previous-to-current source compare:\n"
                    f"- previous_git_version_file: {compare_context.get('previous_git_version_file', '-')}\n"
                    f"- from: {compare_context.get('from_commit', '-')}\n"
                    f"- to: {compare_context.get('to_commit', commit)}\n"
                    f"- commits: {compare_context.get('commits_count', 0)}\n"
                )
            if resource_validation:
                header += _format_build_resource_validation(resource_validation)
            if context_budget > 0:
                snippet = header
            else:
                snippet = ""
            for diff_item in diffs:
                changed_file = _changed_file_from_commit_diff(task, diff_item)
                key = (changed_file.path, changed_file.diff)
                if key in seen_files:
                    continue
                seen_files.add(key)
                review_input.changed_files.append(changed_file)
                raw_part = _raw_diff_for_changed_file(changed_file)
                raw_parts.append(raw_part)
                if context_budget > 0:
                    addition = _context_addition_for_locked_change(task, changed_file, raw_part)
                    addition = addition[: min(len(addition), context_budget)]
                    snippet += "\n" + addition
                    context_budget -= len(addition)
            if snippet and context_budget >= 0:
                context_parts.append(snippet)
        except Exception as exc:
            release_gate_errors.append(
                {
                    "severity": "High",
                    "file_path": str(task.get("build_file") or task.get("module") or "GIT_VERSION"),
                    "title": "Locked repository commit could not be deep reviewed",
                    "detail": (
                        f"{task.get('kind', 'repository')} repository {repository_url}@{commit} could not be read "
                        f"for release-gate review: {exc}"
                    ),
                    "recommendation": "Confirm GitLab access and the immutable locked commit, then re-scan before approving the GIT_VERSION MR.",
                }
            )
            errors.append(
                {
                    "kind": task["kind"],
                    "module": module,
                    "repository_url": repository_url,
                    "commit": commit,
                    "error": str(exc),
                }
            )

    if raw_parts:
        review_input.raw_diff = "\n".join([review_input.raw_diff, *raw_parts]).strip()
    review_input.metadata["source_repository_reviews"] = reviews
    review_input.metadata["source_repository_review_errors"] = errors
    review_input.metadata["build_resource_validation_errors"] = validation_errors
    review_input.metadata["source_repository_diff_context"] = "\n\n".join(context_parts)[:max_context_chars]
    review_input.metadata["release_gate"] = {
        "status": "blocked" if release_gate_errors else "ready",
        "project": review_input.metadata.get("release_gate_project", ""),
        "project_path": review_input.metadata.get("release_gate_project_path", ""),
        "project_match": review_input.metadata.get("release_gate_project_match", "unknown"),
        "resources": release_gate_resources,
        "errors": release_gate_errors,
        "source_repository_count": len([item for item in reviews if item.get("kind") == "source"]),
        "build_resource_count": len(release_gate_resources),
        "scripts_source": "locked build repository commit only",
        "post_build_artifacts": ["database/db_change.scr.sha is generated by DPSBuild and is not a pre-build review requirement"],
    }


def _fetch_previous_git_version_locks(context: dict[str, Any], client: GitLabClient) -> dict[str, dict[str, str]]:
    if not app_config_bool("git_version.compare_previous", "GIT_VERSION_COMPARE_PREVIOUS", True):
        return {}
    locks: dict[str, dict[str, str]] = {}
    for build in context.get("builds", []) or []:
        if not isinstance(build, dict):
            continue
        build_repo_url = str(build.get("build_repo_url") or "")
        build_commit = str(build.get("build_repo_commit") or "")
        build_file = _normalize_repo_path(str(build.get("file") or ""))
        current_git_version = _referenced_git_version_path(build_file, str(build.get("git_version") or ""))
        if not build_repo_url or not build_commit or not current_git_version:
            continue
        try:
            base_url, project_path = parse_repository_url(build_repo_url, fallback_base_url=client.base_url)
            repo_client = client if base_url.rstrip("/") == client.base_url.rstrip("/") else GitLabClient(base_url=base_url)
            previous_file = _find_previous_git_version_file(repo_client, project_path, build_commit, current_git_version)
            if not previous_file:
                continue
            previous_text = repo_client.fetch_repository_file(project_path, previous_file, build_commit)
            for entry in parse_git_version_repository_entries(previous_text):
                module = str(entry.get("module") or "")
                commit = str(entry.get("commit") or "")
                if not module or not commit:
                    continue
                locks[module.lower()] = {
                    **{key: str(value) for key, value in entry.items()},
                    "previous_git_version_file": previous_file,
                    "build_repo_commit": build_commit,
                    "build_repo_url": build_repo_url,
                }
        except Exception:
            continue
    return locks


def _fetch_previous_build_resource_locks(context: dict[str, Any], client: GitLabClient) -> dict[str, dict[str, str]]:
    """Find the previous build-resource lock from the same locked build commit.

    The GIT_VERSION MR can deliberately lock a commit before its own build.yml
    is added. Looking inside that locked commit therefore gives the previous
    release's build-v*.yml, which is the correct baseline for resource diffs.
    """
    if not app_config_bool("git_version.compare_previous", "GIT_VERSION_COMPARE_PREVIOUS", True):
        return {}
    locks: dict[str, dict[str, str]] = {}
    for build in context.get("builds", []) or []:
        if not isinstance(build, dict):
            continue
        build_repo_url = str(build.get("build_repo_url") or "")
        build_commit = str(build.get("build_repo_commit") or "")
        build_file = _normalize_repo_path(str(build.get("file") or ""))
        if not build_repo_url or not build_commit or not build_file:
            continue
        try:
            base_url, project_path = parse_repository_url(build_repo_url, fallback_base_url=client.base_url)
            repo_client = client if base_url.rstrip("/") == client.base_url.rstrip("/") else GitLabClient(base_url=base_url)
            previous_file = _find_previous_build_file(repo_client, project_path, build_commit, build_file)
            if not previous_file:
                continue
            previous_summary = parse_build_summary(
                repo_client.fetch_repository_file(project_path, previous_file, build_commit)
            )
            previous_commit = str(previous_summary.get("build_repo_commit") or "").strip()
            if not previous_commit:
                continue
            locks[build_file] = {
                "previous_build_file": previous_file,
                "commit": previous_commit,
                "build_repo_url": build_repo_url,
            }
        except Exception:
            continue
    return locks


def _find_previous_git_version_file(
    client: GitLabClient,
    project_path: str,
    ref: str,
    current_git_version_path: str,
) -> str:
    current_path = PurePosixPath(_normalize_repo_path(current_git_version_path))
    current_version = _git_version_file_version(current_path.name)
    if not current_version:
        return ""
    directory = "" if str(current_path.parent) == "." else str(current_path.parent)
    current_key = _version_sort_key(current_version)
    candidates: list[tuple[tuple[int, ...], str]] = []
    for item in client.fetch_repository_tree(project_path, ref=ref, path=directory, recursive=False, limit=100):
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        item_path = str(item.get("path") or item.get("name") or "")
        item_name = PurePosixPath(item_path).name
        version = _git_version_file_version(item_name)
        if not version:
            continue
        key = _version_sort_key(version)
        if key and key < current_key:
            candidates.append((key, item_path))
    if not candidates:
        return ""
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def _find_previous_build_file(
    client: GitLabClient,
    project_path: str,
    ref: str,
    current_build_path: str,
) -> str:
    current_path = PurePosixPath(_normalize_repo_path(current_build_path))
    current_version = _build_file_version(current_path.name)
    if not current_version:
        return ""
    directory = "" if str(current_path.parent) == "." else str(current_path.parent)
    current_key = _version_sort_key(current_version)
    candidates: list[tuple[tuple[int, ...], str]] = []
    for item in client.fetch_repository_tree(project_path, ref=ref, path=directory, recursive=False, limit=100):
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        item_path = str(item.get("path") or item.get("name") or "")
        version = _build_file_version(PurePosixPath(item_path).name)
        key = _version_sort_key(version)
        if version and key and key < current_key:
            candidates.append((key, item_path))
    return sorted(candidates, key=lambda item: item[0])[-1][1] if candidates else ""


def _compare_source_with_previous_lock(
    task: dict[str, str],
    previous_locks: dict[str, dict[str, str]],
    repo_client: GitLabClient,
    project_path: str,
    commit: str,
) -> dict[str, Any]:
    previous = previous_locks.get(str(task.get("module") or "").lower())
    if not previous:
        return {}
    previous_commit = str(previous.get("commit") or "").strip()
    if not previous_commit or previous_commit == commit:
        return {}
    context: dict[str, Any] = {
        "from_commit": previous_commit,
        "to_commit": commit,
        "previous_git_version_file": previous.get("previous_git_version_file", ""),
    }
    try:
        payload = repo_client.compare_repository_refs(project_path, previous_commit, commit)
        diffs = payload.get("diffs")
        commits = payload.get("commits")
        if isinstance(diffs, list):
            context["diffs"] = diffs
        if isinstance(commits, list):
            context["commits_count"] = len(commits)
            context["commit_messages"] = " ".join(
                " ".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("message") or ""),
                        str(item.get("short_id") or ""),
                    ]
                )
                for item in commits
                if isinstance(item, dict)
            )
        return context
    except Exception as exc:
        context["compare_error"] = str(exc)
        return context


def _compare_build_resource_with_previous_lock(
    task: dict[str, str],
    previous_locks: dict[str, dict[str, str]],
    repo_client: GitLabClient,
    project_path: str,
    commit: str,
) -> dict[str, Any]:
    build_file = _normalize_repo_path(task.get("build_file") or task.get("module") or "")
    previous = previous_locks.get(build_file)
    if not previous:
        return {}
    previous_commit = str(previous.get("commit") or "").strip()
    if not previous_commit or previous_commit == commit:
        return {}
    context: dict[str, Any] = {
        "from_commit": previous_commit,
        "to_commit": commit,
        "previous_build_file": previous.get("previous_build_file", ""),
    }
    try:
        payload = repo_client.compare_repository_refs(project_path, previous_commit, commit)
        diffs = payload.get("diffs")
        commits = payload.get("commits")
        if isinstance(diffs, list):
            context["diffs"] = diffs
        if isinstance(commits, list):
            context["commits_count"] = len(commits)
            context["commit_messages"] = " ".join(
                " ".join(str(item.get(key) or "") for key in ("title", "message", "short_id"))
                for item in commits
                if isinstance(item, dict)
            )
        return context
    except Exception as exc:
        context["compare_error"] = str(exc)
        return context


def _release_gate_build_resource(
    task: dict[str, str],
    repo_client: GitLabClient,
    project_path: str,
    commit: str,
    diffs: list[dict[str, Any]],
    compare_context: dict[str, Any],
) -> dict[str, Any]:
    payload = _build_resource_payload_summary(diffs)
    build_file = _normalize_repo_path(task.get("build_file") or task.get("module") or "GIT_VERSION")
    record: dict[str, Any] = {
        "build_file": build_file,
        "repository": project_path,
        "commit": commit,
        "previous_commit": compare_context.get("from_commit", ""),
        "previous_build_file": compare_context.get("previous_build_file", ""),
        "payload": payload,
        "scripts": [],
        "database_scripts": [],
        "errors": [],
    }

    # DPSBuild.php and DBChangeParser.php describe DPS package assembly only.
    # Web build repositories remain subject to the generic locked build.yml /
    # git_version.yml validation above, without requiring DPS-only scripts.
    if not _is_dps_build_repository(project_path):
        record["applicable"] = False
        return record
    record["applicable"] = True

    # DPSBuild.php is the authoritative package-assembly rule. It is read only
    # from the build commit selected by build.yml, never from a local fallback.
    build_script_path = _normalize_repo_path(
        app_config_str(
            "review.release_gate.dps_build_script_path",
            "RELEASE_GATE_DPS_BUILD_SCRIPT_PATH",
            "company/SV/script/DPSBuild.php",
        )
    )
    _append_locked_script_check(
        record,
        repo_client,
        project_path,
        commit,
        build_script_path,
        "DPSBuild.php",
        required=True,
        build_file=build_file,
    )

    # Database runtime resources are conditional. Code-only and config-only
    # packages must not fail merely because DBChangeParser is not involved.
    if payload["database_files"]:
        parser_path = _normalize_repo_path(
            app_config_str(
                "review.release_gate.dps_db_parser_path",
                "RELEASE_GATE_DPS_DB_PARSER_PATH",
                "company/SV/script/DBChangeParser.php",
            )
        )
        config_path = _normalize_repo_path(
            app_config_str(
                "review.release_gate.dps_db_config_path",
                "RELEASE_GATE_DPS_DB_CONFIG_PATH",
                "company/SV/script/db_change.yml",
            )
        )
        _append_locked_script_check(
            record,
            repo_client,
            project_path,
            commit,
            parser_path,
            "DBChangeParser.php",
            required=True,
            build_file=build_file,
        )
        _append_locked_script_check(
            record,
            repo_client,
            project_path,
            commit,
            config_path,
            "db_change.yml",
            required=True,
            build_file=build_file,
        )
        for scr_path in payload["db_change_scripts"]:
            record["database_scripts"].append(
                _validate_locked_db_change_script(
                    repo_client,
                    project_path,
                    commit,
                    scr_path,
                    build_file,
                )
            )

    for check in record["scripts"]:
        if isinstance(check, dict) and check.get("required") and not check.get("exists"):
            record["errors"].append(_release_gate_error_from_check(check, build_file))
    for validation in record["database_scripts"]:
        if isinstance(validation, dict):
            record["errors"].extend(validation.get("errors") or [])
    return record


def _is_dps_build_repository(project_path: str) -> bool:
    return PurePosixPath((project_path or "").strip("/").lower()).name == "dps"


def _build_resource_payload_summary(diffs: list[dict[str, Any]]) -> dict[str, Any]:
    config_groups: dict[str, list[str]] = {}
    database_files: list[str] = []
    db_change_scripts: list[str] = []
    code_files: list[str] = []
    other_files: list[str] = []
    for item in diffs:
        if not isinstance(item, dict):
            continue
        path = _normalize_repo_path(str(item.get("new_path") or item.get("old_path") or ""))
        if not path:
            continue
        lower = path.lower()
        parts = [part.lower() for part in PurePosixPath(lower).parts]
        if "database" in parts or lower.endswith("db_change.scr"):
            database_files.append(path)
            if lower.endswith("db_change.scr"):
                db_change_scripts.append(path)
        elif "config" in parts or PurePosixPath(lower).name.startswith(("state_config", "state_cofig")):
            logical = _logical_build_config_path(path)
            company = _build_resource_company(path)
            config_groups.setdefault(logical, []).append(company)
        elif "site" in parts:
            code_files.append(path)
        else:
            other_files.append(path)
    return {
        "config_groups": [
            {"logical_path": path, "companies": sorted({value for value in companies if value})}
            for path, companies in sorted(config_groups.items())
        ],
        "config_file_count": sum(len(values) for values in config_groups.values()),
        "database_files": sorted(dict.fromkeys(database_files)),
        "db_change_scripts": sorted(dict.fromkeys(db_change_scripts)),
        "code_files": sorted(dict.fromkeys(code_files)),
        "other_files": sorted(dict.fromkeys(other_files)),
    }


def _logical_build_config_path(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    lowered = [part.lower() for part in parts]
    if "config" in lowered:
        return "/".join(parts[lowered.index("config") :])
    return path


def _build_resource_company(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    lowered = [part.lower() for part in parts]
    if "release" in lowered:
        index = lowered.index("release")
        if len(parts) > index + 2:
            return parts[index + 2]
    if "company" in lowered:
        index = lowered.index("company")
        if len(parts) > index + 1:
            return parts[index + 1]
    return "SV"


def _append_locked_script_check(
    record: dict[str, Any],
    client: GitLabClient,
    project_path: str,
    commit: str,
    path: str,
    role: str,
    *,
    required: bool,
    build_file: str,
) -> None:
    check: dict[str, Any] = {"role": role, "path": path, "required": required, "exists": False}
    try:
        content = client.fetch_repository_file(project_path, path, commit)
        check.update(
            {
                "exists": True,
                "size": len(content),
                "sha256": hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest(),
                "version": _script_version(content),
                "build_file": build_file,
            }
        )
    except Exception as exc:
        check["error"] = str(exc)
        check["build_file"] = build_file
    record["scripts"].append(check)


def _script_version(content: str) -> str:
    match = re.search(r"(?:SCRIPT_VERSION|VERSION)\s*[,)=]\s*['\"]?v?([0-9][A-Za-z0-9._-]*)", content)
    return match.group(1) if match else ""


def _release_gate_error_from_check(check: dict[str, Any], build_file: str) -> dict[str, str]:
    role = str(check.get("role") or "build runtime resource")
    path = str(check.get("path") or "-")
    return {
        "severity": "High",
        "file_path": build_file,
        "title": f"Locked build repository is missing {role}",
        "detail": (
            f"The build.yml locked commit cannot read '{path}', which is required for the detected release payload. "
            f"Error: {check.get('error') or 'file not found'}."
        ),
        "recommendation": "Push the required script/config resource before locking build.yml, then update the build repository commit and re-scan the GIT_VERSION MR.",
    }


def _validate_locked_db_change_script(
    client: GitLabClient,
    project_path: str,
    commit: str,
    scr_path: str,
    build_file: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": scr_path, "blocks": [], "references": [], "errors": []}
    try:
        content = client.fetch_repository_file(project_path, scr_path, commit)
    except Exception as exc:
        result["errors"].append(
            {
                "severity": "High",
                "file_path": build_file,
                "title": "Locked database change script is unavailable",
                "detail": f"The locked build commit cannot read '{scr_path}': {exc}",
                "recommendation": "Push the db_change.scr resource before locking build.yml and re-run the GIT_VERSION release-gate review.",
            }
        )
        return result

    seen: set[tuple[str, str, str, str]] = set()
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip().upper().startswith("-- MODULE:"):
            continue
        match = DB_CHANGE_HEADER_RE.match(line.strip())
        if not match:
            result["errors"].append(
                {
                    "severity": "High",
                    "file_path": scr_path,
                    "line": line_number,
                    "title": "db_change.scr block header is invalid for the locked DBChangeParser",
                    "detail": f"The locked DBChangeParser expects '-- MODULE: ..., VERSION: ..., COMPANY: ..., ENV: ...'; got '{line.strip()}'.",
                    "recommendation": "Use the exact DBChangeParser block-header format and re-run the release-gate review.",
                }
            )
            continue
        block = {key: value for key, value in match.groupdict().items()}
        block["line"] = line_number
        key = (block["module"].lower(), block["version"], block["company"].lower(), block["environment"].lower())
        if key in seen:
            result["errors"].append(
                {
                    "severity": "High",
                    "file_path": scr_path,
                    "line": line_number,
                    "title": "db_change.scr contains a duplicate module/version/company/environment block",
                    "detail": f"Duplicate block {key} can make execution order and rerun behavior ambiguous.",
                    "recommendation": "Keep one authoritative block per module, version, company and environment combination.",
                }
            )
        seen.add(key)
        result["blocks"].append(block)
    if not result["blocks"]:
        result["errors"].append(
            {
                "severity": "High",
                "file_path": scr_path,
                "title": "db_change.scr has no DBChangeParser v1.2-compatible blocks",
                "detail": "A database payload was locked, but no '-- MODULE: ..., VERSION: ..., COMPANY: ..., ENV: ...' blocks were found.",
                "recommendation": "Either remove the empty database payload or add valid DBChangeParser blocks and their referenced resources.",
            }
        )
    for reference in sorted(set(DB_CHANGE_REFERENCE_RE.findall(content))):
        if "{$" in reference or reference.lower().endswith("dbchangeparser.php"):
            continue
        resolved = _resolve_locked_db_reference(client, project_path, commit, scr_path, reference)
        result["references"].append({"reference": reference, "resolved": resolved})
        if not resolved:
            result["errors"].append(
                {
                    "severity": "High",
                    "file_path": scr_path,
                    "title": "db_change.scr references a missing execution resource",
                    "detail": f"The locked database script references '{reference}', but it cannot be read from build commit {commit}.",
                    "recommendation": "Push the referenced SQL/JS/PHP/shell file with the database resource, or correct the command path before locking the build commit.",
                }
            )
    return result


def _resolve_locked_db_reference(
    client: GitLabClient,
    project_path: str,
    commit: str,
    scr_path: str,
    reference: str,
) -> str:
    if reference.startswith(("/", "http://", "https://")):
        return ""
    parent = str(PurePosixPath(scr_path).parent)
    release_root = scr_path.split("/database/", 1)[0] if "/database/" in scr_path else parent
    candidates = [
        _normalize_repo_path(reference),
        _normalize_repo_path(str(PurePosixPath(parent) / reference)),
        _normalize_repo_path(str(PurePosixPath(release_root) / reference)),
        _normalize_repo_path(str(PurePosixPath(release_root) / "data" / PurePosixPath(reference).name)),
        _normalize_repo_path(str(PurePosixPath(release_root) / "script" / PurePosixPath(reference).name)),
        _normalize_repo_path(str(PurePosixPath(release_root) / "database" / PurePosixPath(reference).name)),
    ]
    for candidate in dict.fromkeys(candidates):
        try:
            client.fetch_repository_file(project_path, candidate, commit)
            return candidate
        except Exception:
            continue
    return ""


def _context_addition_for_locked_change(task: dict[str, str], changed_file: ChangedFile, raw_part: str) -> str:
    if task.get("kind") == "build-resource" and is_optimizable_build_resource(changed_file.path):
        return (
            f"[Build resource diff summarized] {changed_file.path}: "
            f"+{changed_file.additions}/-{changed_file.deletions}; full diff is retained for report rendering."
        )
    return raw_part


def _git_version_file_version(name: str) -> str:
    match = re.search(r"git_version-v(.+?)\.ya?ml$", name, re.I)
    return match.group(1) if match else ""


def _build_file_version(name: str) -> str:
    match = re.search(r"build-v(.+?)\.ya?ml$", name, re.I)
    return match.group(1) if match else ""


def _version_sort_key(version: str) -> tuple[int, ...]:
    numbers = [int(value) for value in re.findall(r"\d+", version)]
    return tuple(numbers)


def _git_version_review_tasks(context: dict[str, Any]) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    for entry in context.get("repositories", []) or []:
        if not isinstance(entry, dict):
            continue
        tasks.append(
            {
                "kind": "source",
                "module": str(entry.get("module") or "source"),
                "repository_url": str(entry.get("repository_url") or ""),
                "branch": str(entry.get("branch") or ""),
                "commit": str(entry.get("commit") or ""),
            }
        )
    for build in context.get("builds", []) or []:
        if not isinstance(build, dict):
            continue
        tasks.append(
            {
                "kind": "build-resource",
                "module": str(build.get("file") or "build-repository"),
                "build_file": str(build.get("file") or ""),
                "git_version": str(build.get("git_version") or ""),
                "repository_url": str(build.get("build_repo_url") or ""),
                "branch": str(build.get("build_repo_branch") or ""),
                "commit": str(build.get("build_repo_commit") or ""),
            }
        )

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for task in tasks:
        key = (task["kind"], task["repository_url"], task["commit"])
        if key not in seen and task["repository_url"] and task["commit"]:
            seen.add(key)
            result.append(task)
    return result


def _validate_build_resource_commit(
    task: dict[str, str],
    repo_client: GitLabClient,
    project_path: str,
    commit: str,
) -> dict[str, Any]:
    build_file = _normalize_repo_path(task.get("build_file") or task.get("module") or "")
    git_version_file = _referenced_git_version_path(build_file, task.get("git_version") or "")
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for role, file_path in (("build.yml", build_file), ("git_version.yml", git_version_file)):
        if not file_path:
            continue
        check: dict[str, Any] = {"role": role, "path": file_path, "exists": False}
        try:
            content = repo_client.fetch_repository_file(project_path, file_path, commit)
            check.update({"exists": True, "size": len(content)})
        except Exception as exc:
            error = {
                "role": role,
                "path": file_path,
                "commit": commit,
                "build_file": build_file,
                "git_version_file": git_version_file,
                "error": str(exc),
            }
            check["error"] = str(exc)
            errors.append(error)
        checks.append(check)

    return {
        "rule": "The build repository commit may be any commit after the required build resources were pushed; it does not need to equal the MR head.",
        "build_file": build_file,
        "git_version_file": git_version_file,
        "required_files": checks,
        "valid": not errors,
        "errors": errors,
    }


def _referenced_git_version_path(build_file: str, git_version: str) -> str:
    value = _normalize_repo_path(git_version)
    if not value:
        return ""
    if "/" in value:
        return value
    parent = str(PurePosixPath(build_file).parent)
    return value if parent in {"", "."} else f"{parent}/{value}"


def _normalize_repo_path(path: str) -> str:
    return path.strip().strip("'\"").replace("\\", "/").lstrip("/")


def _format_build_resource_validation(validation: dict[str, Any]) -> str:
    checks = validation.get("required_files") or []
    if not isinstance(checks, list) or not checks:
        return ""
    lines = ["build-resource lock validation:"]
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = "present" if check.get("exists") else "missing"
        lines.append(f"- {check.get('role', '-')}: {check.get('path', '-')} => {status}")
    return "\n".join(lines) + "\n"


def _changed_file_from_commit_diff(task: dict[str, str], diff_item: dict[str, Any]) -> ChangedFile:
    original_path = str(diff_item.get("new_path") or diff_item.get("old_path") or "unknown")
    safe_module = re.sub(r"[^A-Za-z0-9_.-]+", "_", task.get("module", "repo")).strip("_") or "repo"
    prefix = "locked_source" if task.get("kind") == "source" else "locked_build"
    path = f"{prefix}/{safe_module}/{original_path}"
    diff = str(diff_item.get("diff") or "")
    additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return ChangedFile(path=path, additions=additions, deletions=deletions, diff=diff)


def _raw_diff_for_changed_file(changed_file: ChangedFile) -> str:
    return f"diff --git a/{changed_file.path} b/{changed_file.path}\n--- a/{changed_file.path}\n+++ b/{changed_file.path}\n{changed_file.diff}"


def _issue_keys(text: str) -> list[str]:
    return sorted(set(re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", text or "")))


def post_gitlab_comment(mr_url: str, markdown: str, confirmed: bool = False) -> None:
    if not confirmed:
        raise PermissionError("GitLab writeback requires explicit confirmation.")
    client, ref = GitLabClient.from_mr_url(mr_url)
    summary = markdown[:9000]
    client.create_merge_request_note(ref.project_path, ref.iid, summary)


def _local_issue_branch_records(entry: WorkspaceEntry, issue_key: str, target_branch: str = "") -> list[dict[str, str]]:
    branches = _local_git_refs(entry.local_path)
    target = _select_local_target_branch(entry.local_path, target_branch)
    if not target:
        return []
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for branch in branches:
        if branch.endswith("/HEAD"):
            continue
        if branch == target:
            continue
        if not _branch_matches_issue(branch, issue_key):
            continue
        key = branch.replace("remotes/", "")
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "project_path": entry.project_path,
                "group": entry.group,
                "module": entry.module,
                "repo": str(entry.local_path),
                "source_branch": branch,
                "target_branch": target,
                "repository_url": entry.repository_url,
            }
        )
    return records


def _local_git_refs(repo: Path) -> list[str]:
    command = [
        "git",
        "-C",
        str(repo),
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
        "refs/remotes",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git branch scan failed")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _select_local_target_branch(repo: Path, requested: str = "") -> str:
    candidates = []
    if requested:
        candidates.append(requested)
    candidates.extend(["develop", "origin/develop", "main", "origin/main", "master", "origin/master"])
    for candidate in candidates:
        if _git_ref_exists(repo, candidate):
            return candidate
    return requested


def _git_ref_exists(repo: Path, ref: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    return completed.returncode == 0


def _git_diff(repo: Path, source_branch: str, target_branch: str) -> str:
    if not source_branch or not target_branch:
        raise ValueError("Both source_branch and target_branch are required for repo mode.")
    command = ["git", "-C", str(repo), "diff", f"{target_branch}...{source_branch}", "--"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return completed.stdout


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        return value["value"]
    return str(value)


def _gitlab_base_url() -> str:
    import os

    return os.getenv("GITLAB_URL", "https://gitlab.tx-tech.com")
