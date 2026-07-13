from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is optional at runtime
    yaml = None  # type: ignore[assignment]

from .models import ChangedFile, Finding, ReviewInput


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
YAML_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.#/-]+)\s*:\s*(?P<value>.*)$")
ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
SCR_HEADER_RE = re.compile(
    r"^--\s*MODULE:\s*(?P<module>\w+),\s*VERSION:\s*(?P<version>[\da-z.\-+{}$_]+),\s*"
    r"COMPANY:\s*(?P<company>\w+),\s*ENV:\s*(?P<environment>\w+)\s*$",
    re.I,
)


def enrich_git_version_review(review_input: ReviewInput) -> list[Finding]:
    context = extract_git_version_lock_context(review_input)
    if not context["is_git_version_mr"]:
        return []

    git_version_files = context["git_version_files"]
    build_files = context["build_files"]
    build_history_files = context["build_history_files"]
    git_entries = context["repositories"]
    build_summaries = context["builds"]
    findings = list(context["findings"])

    review_input.metadata["mr_type"] = "GIT_VERSION"
    review_input.metadata["git_version_files"] = git_version_files
    review_input.metadata["build_files"] = build_files
    review_input.metadata["build_history_files"] = build_history_files

    review_input.metadata["git_version_summary"] = {
        "git_version_files": git_version_files,
        "build_files": build_files,
        "build_history_files": build_history_files,
        "repositories": git_entries[:80],
        "builds": build_summaries,
    }
    compat_notes = _known_build_config_compat_notes()
    if compat_notes:
        review_input.metadata["build_config_compat_notes"] = compat_notes
    review_input.metadata["git_version_review_context"] = _git_version_prompt_context(
        review_input, git_entries, build_summaries
    )
    findings.extend(_source_repository_fetch_findings(review_input))
    findings.extend(_build_resource_validation_findings(review_input))
    findings.extend(_release_gate_validation_findings(review_input))
    findings.extend(_release_note_traceability_findings(review_input))
    return findings


def extract_git_version_lock_context(review_input: ReviewInput) -> dict[str, Any]:
    relevant_files = [item for item in review_input.changed_files if _is_git_version_file(item.path)]
    git_version_files = [item.path for item in relevant_files if _is_git_version_yml(item.path)]
    build_files = [item.path for item in relevant_files if _is_build_yml(item.path)]
    build_history_files = [item.path for item in relevant_files if _is_build_history_file(item.path)]
    findings: list[Finding] = []
    git_entries: list[dict[str, str]] = []
    build_summaries: list[dict[str, Any]] = []

    for changed_file in relevant_files:
        available_lines = _new_side_lines(changed_file.diff)
        parsed = _parse_yaml_text("\n".join(text for _, text in available_lines))
        findings.extend(_duplicate_key_findings(changed_file.path, available_lines))

        if _is_git_version_yml(changed_file.path):
            entries = _git_version_entries(parsed)
            git_entries.extend(entries)
            findings.extend(_git_version_findings(changed_file.path, entries, available_lines))
        elif _is_build_yml(changed_file.path):
            summary = _build_summary(parsed)
            if summary:
                summary["file"] = changed_file.path
                build_summaries.append(summary)
            findings.extend(_build_findings(changed_file.path, summary, available_lines))

    findings.extend(_version_pair_findings(git_version_files, build_files))
    return {
        "is_git_version_mr": bool(relevant_files),
        "git_version_files": git_version_files,
        "build_files": build_files,
        "build_history_files": build_history_files,
        "repositories": git_entries,
        "builds": build_summaries,
        "findings": findings,
    }


def parse_git_version_repository_entries(text: str) -> list[dict[str, str]]:
    return _git_version_entries(_parse_yaml_text(text))


def parse_build_summary(text: str) -> dict[str, Any]:
    """Parse a build.yml using the same tolerant YAML handling as MR review."""
    return _build_summary(_parse_yaml_text(text))


def _is_git_version_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("locked_source/", "locked_build/")):
        return False
    name = Path(normalized).name.lower()
    return (name.endswith((".yml", ".yaml")) and ("git_version" in name or "build" in name)) or name.endswith(".bh")


def _is_git_version_yml(path: str) -> bool:
    return "git_version" in Path(path.replace("\\", "/")).name.lower()


def _is_build_yml(path: str) -> bool:
    return "build" in Path(path.replace("\\", "/")).name.lower() and Path(path).suffix.lower() in {".yml", ".yaml"}


def _is_build_history_file(path: str) -> bool:
    return Path(path.replace("\\", "/")).name.lower().endswith(".bh")


def _new_side_lines(file_diff: str) -> list[tuple[int | None, str]]:
    result: list[tuple[int | None, str]] = []
    new_line_no: int | None = None
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line_no = int(match.group(1)) if match else None
            continue
        if line.startswith(("diff --git", "--- ", "+++ ")):
            continue
        if line.startswith("-"):
            continue
        text = line[1:] if line.startswith("+") else line
        result.append((new_line_no, text))
        if new_line_no is not None:
            new_line_no += 1
    return result


def _parse_yaml_text(text: str) -> Any:
    if not text.strip():
        return {}
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text) or {}
            if isinstance(parsed, dict) and parsed:
                return parsed
        except Exception:
            pass
    return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    raw_lines = text.splitlines()
    for index, raw_line in enumerate(raw_lines):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if stripped.startswith("- "):
            if isinstance(parent, list):
                parent.append(_parse_scalar(stripped[2:]))
            continue
        match = YAML_KEY_RE.match(raw_line)
        if not match or not isinstance(parent, dict):
            continue
        key = match.group("key")
        value = _strip_inline_comment(match.group("value").strip())
        if value == "":
            child: Any = [] if _next_significant_is_list(raw_lines, index, indent) else {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _next_significant_is_list(lines: list[str], current_index: int, current_indent: int) -> bool:
    for raw_line in lines[current_index + 1 :]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        return indent > current_indent and raw_line.strip().startswith("- ")
    return False


def _parse_scalar(value: str) -> Any:
    value = _strip_inline_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value in {"[]", ""}:
        return []
    if value in {"{}", "null", "NULL", "~"}:
        return {}
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def _strip_inline_comment(value: str) -> str:
    quote = ""
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = "" if quote == char else char if not quote else quote
        if char == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _duplicate_key_findings(file_path: str, lines: list[tuple[int | None, str]]) -> list[Finding]:
    findings: list[Finding] = []
    stack: list[tuple[int, str]] = []
    seen: dict[tuple[str, int], tuple[int | None, str]] = {}
    for line_no, text in lines:
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        match = YAML_KEY_RE.match(text)
        if not match:
            continue
        indent = len(match.group("indent").replace("\t", "  "))
        key = match.group("key")
        stack = [(level, item) for level, item in stack if level < indent]
        parent = "/".join(item for _, item in stack)
        seen_key = (f"{parent}/{key}", indent)
        if seen_key in seen:
            severity = "Critical" if key == "commit" else "High"
            findings.append(
                Finding(
                    severity=severity,
                    file_path=file_path,
                    line=line_no,
                    title="Duplicate YAML key in GIT_VERSION config",
                    detail=f"Key '{key}' appears more than once under '{parent or '<root>'}'. YAML keeps only the last value, so the earlier locked value can be silently ignored.",
                    recommendation="Keep exactly one key per mapping. For commit locks, remove the stale commit and verify the final commit is the intended one.",
                    category="GIT_VERSION",
                )
            )
        seen[seen_key] = (line_no, key)
        if match.group("value").strip() == "":
            stack.append((indent, key))
    return findings


def _git_version_entries(parsed: Any) -> list[dict[str, str]]:
    if not isinstance(parsed, dict):
        return []
    entries: list[dict[str, str]] = []
    for module, config in parsed.items():
        if not isinstance(config, dict):
            continue
        entries.append(
            {
                "module": str(module),
                "branch": str(config.get("branch") or ""),
                "repository_url": str(config.get("repository_url") or ""),
                "commit": str(config.get("commit") or ""),
                "tag": str(config.get("tag") or ""),
            }
        )
    return entries


def _git_version_findings(
    file_path: str, entries: list[dict[str, str]], lines: list[tuple[int | None, str]]
) -> list[Finding]:
    findings: list[Finding] = []
    if not entries:
        return findings
    for entry in entries:
        module = entry["module"]
        commit = entry["commit"].strip().strip("'\"")
        tag = entry["tag"].strip().strip("'\"")
        if not commit and not tag:
            findings.append(
                Finding(
                    severity="High",
                    file_path=file_path,
                    line=_module_line(lines, module),
                    title="Repository lock has no commit or tag",
                    detail=f"GIT_VERSION entry '{module}' does not lock a commit or tag.",
                    recommendation="Lock each repository to a 40-character commit SHA unless the release process explicitly uses immutable tags.",
                    category="GIT_VERSION",
                )
            )
        elif commit and not COMMIT_RE.match(commit):
            findings.append(
                Finding(
                    severity="High",
                    file_path=file_path,
                    line=_module_line(lines, module),
                    title="Invalid locked commit format",
                    detail=f"GIT_VERSION entry '{module}' uses commit '{commit}', which is not a 40-character SHA.",
                    recommendation="Use the exact full Git commit SHA for the selected branch.",
                    category="GIT_VERSION",
                )
            )
        if not entry["repository_url"]:
            findings.append(
                Finding(
                    severity="High",
                    file_path=file_path,
                    line=_module_line(lines, module),
                    title="Repository URL missing in GIT_VERSION entry",
                    detail=f"GIT_VERSION entry '{module}' has no repository_url.",
                    recommendation="Set repository_url so the build can fetch the locked source repository deterministically.",
                    category="GIT_VERSION",
                )
            )
        if not entry["branch"]:
            findings.append(
                Finding(
                    severity="Medium",
                    file_path=file_path,
                    line=_module_line(lines, module),
                    title="Branch missing in GIT_VERSION entry",
                    detail=f"GIT_VERSION entry '{module}' has no branch.",
                    recommendation="Record the source branch used to select the locked commit for traceability.",
                    category="GIT_VERSION",
                )
            )
    return findings


def _build_summary(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    version = parsed.get("version") if isinstance(parsed.get("version"), dict) else {}
    build = parsed.get("build") if isinstance(parsed.get("build"), dict) else {}
    repo = version.get("git_repository")
    if not isinstance(repo, dict):
        repo = version.get("git_version4config", {})
        repo = repo.get("git_repository") if isinstance(repo, dict) else {}
    if not isinstance(repo, dict):
        repo = {}
    source_provide_method = build.get("source_provide_method", "")
    return {
        "build_on": parsed.get("build_on", ""),
        "ver_number": version.get("ver_number", ""),
        "git_version": version.get("git_version", ""),
        "build_repo_branch": repo.get("branch", ""),
        "build_repo_url": repo.get("repository_url", ""),
        "build_repo_commit": repo.get("commit", ""),
        "build_type": build.get("type", ""),
        "source_provide_method": source_provide_method,
        "environments": build.get("environments", []),
        "companies": parsed.get("companies", []),
        "build_template": parsed.get("build_template", []),
    }


def _build_findings(
    file_path: str, summary: dict[str, Any], lines: list[tuple[int | None, str]]
) -> list[Finding]:
    findings: list[Finding] = []
    if not summary:
        return findings
    commit = str(summary.get("build_repo_commit") or "").strip().strip("'\"")
    git_version = str(summary.get("git_version") or "").strip().strip("'\"")
    companies = summary.get("companies")
    if not git_version:
        findings.append(
            Finding(
                severity="High",
                file_path=file_path,
                line=_key_line(lines, "git_version"),
                title="build.yml does not reference git_version.yml",
                detail="The build config should point to the git_version.yml file that locks development repositories.",
                recommendation="Set version.git_version to the intended git_version*.yml in the same version directory unless an absolute path is intentionally used.",
                category="GIT_VERSION",
            )
        )
    if not commit:
        findings.append(
            Finding(
                severity="High",
                file_path=file_path,
                line=_key_line(lines, "commit"),
                title="Build repository commit is not locked",
                detail="build.yml does not lock the build-code repository commit.",
                recommendation="Set version.git_repository.commit or version.git_version4config.git_repository.commit to a build repository commit after the required build resources were pushed.",
                category="GIT_VERSION",
            )
        )
    elif not COMMIT_RE.match(commit):
        findings.append(
            Finding(
                severity="High",
                file_path=file_path,
                line=_key_line(lines, "commit"),
                title="Invalid build repository commit format",
                detail=f"build.yml uses build repository commit '{commit}', which is not a 40-character SHA.",
                recommendation="Use the full commit SHA for the build-code repository.",
                category="GIT_VERSION",
            )
        )
    if isinstance(companies, list) and not companies:
        findings.append(
            Finding(
                severity="Medium",
                file_path=file_path,
                line=_key_line(lines, "companies"),
                title="No active companies configured for build",
                detail="build.yml companies list is empty, so company configuration packages may not be produced.",
                recommendation="Confirm this is code-only build intent; otherwise include the required company codes.",
                category="GIT_VERSION",
            )
        )
    return findings


def _version_pair_findings(git_version_files: list[str], build_files: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    git_versions = {_version_from_git_version_name(path) for path in git_version_files}
    build_versions = {_version_from_build_name(path) for path in build_files}
    git_versions.discard("")
    build_versions.discard("")
    if git_versions and build_versions and git_versions.isdisjoint(build_versions):
        findings.append(
            Finding(
                severity="Medium",
                file_path="GIT_VERSION",
                line=None,
                title="git_version.yml and build.yml version suffix differ",
                detail=f"git_version files use versions {sorted(git_versions)}, while build files use versions {sorted(build_versions)}.",
                recommendation="Confirm whether this MR intentionally mixes version files. For the same build target, git_version-v<version>.yml and build-v<version>.yml should usually align with the revision/base version or the bh-derived patch version.",
                category="GIT_VERSION",
            )
        )
    return findings


def _git_version_prompt_context(
    review_input: ReviewInput, git_entries: list[dict[str, str]], build_summaries: list[dict[str, Any]]
) -> str:
    max_chars = int(os.getenv("WEB_BUILD_CONTEXT_MAX_CHARS", "20000"))
    lines: list[str] = [
        "MR type: GIT_VERSION MR",
        "Purpose: lock development repositories via git_version.yml and lock build-code repository/config via build.yml.",
        "Version model: the build repository branch may be the revision/base version branch. Later build versions are derived from build history (*.bh) by incrementing the patch version number from 1. Generated files may be git_version-v<revision-or-patch-version>.yml and build-v<revision-or-patch-version>.yml depending on package type.",
        "Build repository lock rule: version.git_repository.commit/version.git_version4config.git_repository.commit may point to any build repository commit after the required build resources were pushed. It does not have to equal the current MR head. Report a build repository commit lock only when it is missing, invalid, cannot be fetched, or the locked commit does not contain the required build.yml/git_version.yml resources.",
        "",
        "web build domain rules:",
        _web_build_domain_rules(),
        "",
        "git_version.yml entries:",
    ]
    for entry in git_entries[:80]:
        lines.append(
            f"- {entry['module']}: branch={entry['branch'] or '-'}, commit={entry['commit'] or entry['tag'] or '-'}, repo={entry['repository_url'] or '-'}"
        )
    lines.extend(["", "build.yml summaries:"])
    for summary in build_summaries:
        companies = summary.get("companies")
        if isinstance(companies, list):
            companies = ", ".join(str(item) for item in companies[:40])
        lines.append(
            f"- {summary.get('file', '-')}: ver_number={summary.get('ver_number') or '-'}, git_version={summary.get('git_version') or '-'}, build_branch={summary.get('build_repo_branch') or '-'}, build_commit={summary.get('build_repo_commit') or '-'}, companies={companies or '-'}, envs={summary.get('environments') or '-'}"
        )
    build_history_files = review_input.metadata.get("build_history_files") or []
    if isinstance(build_history_files, list) and build_history_files:
        lines.extend(["", "build history files:", *[f"- {item}" for item in build_history_files]])
    source_reviews = review_input.metadata.get("source_repository_reviews") or []
    if isinstance(source_reviews, list) and source_reviews:
        lines.extend(["", "locked source/build repository commits fetched for code review:"])
        for item in source_reviews[:80]:
            if not isinstance(item, dict):
                continue
            issues = item.get("issue_keys") or []
            if isinstance(issues, list):
                issues = ", ".join(str(value) for value in issues)
            validation = _source_review_validation_summary(item)
            compare = ""
            if item.get("compare_from_commit"):
                compare = (
                    f"; compare_from={item.get('compare_from_commit')} to={item.get('compare_to_commit') or item.get('commit', '-')}"
                    f" commits={item.get('compare_commits_count', '-')}"
                    f" previous_git_version_file={item.get('previous_git_version_file', '-')}"
                )
            lines.append(
                f"- {item.get('kind', 'source')} {item.get('module', '-')}: repo={item.get('repository_url', '-')}, branch={item.get('branch', '-')}, commit={item.get('commit', '-')}, files={item.get('files_count', '-')}, issues={issues or '-'}, title={item.get('title', '-')}{compare}{validation}"
            )
    source_errors = review_input.metadata.get("source_repository_review_errors") or []
    if isinstance(source_errors, list) and source_errors:
        lines.extend(["", "locked repository fetch errors:"])
        for item in source_errors[:40]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('module', '-')}: {item.get('error', '-')}")
    release_gate = review_input.metadata.get("release_gate") or {}
    if isinstance(release_gate, dict) and release_gate:
        lines.extend(["", f"release gate status: {release_gate.get('status', 'unknown')}"])
        for resource in release_gate.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            payload = resource.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            lines.append(
                "- build resource lock "
                f"{resource.get('commit', '-')}: config={payload.get('config_file_count', 0)}, "
                f"database={len(payload.get('database_files') or [])}, code={len(payload.get('code_files') or [])}, "
                f"previous={resource.get('previous_commit') or '-'}"
            )
        for error in release_gate.get("errors") or []:
            if isinstance(error, dict):
                lines.append(f"- gate error: {error.get('title', '-')}: {error.get('detail', '-')}")
    source_diff_context = str(review_input.metadata.get("source_repository_diff_context") or "").strip()
    if source_diff_context:
        lines.extend(["", "locked source/build repository diff snippets:", source_diff_context])
    compat_notes = review_input.metadata.get("build_config_compat_notes") or []
    if isinstance(compat_notes, list) and compat_notes:
        lines.extend(["", "known build config compatibility notes:", *[f"- {item}" for item in compat_notes]])
    lines.extend(["", "web-build-tools reference:", _web_build_reference()])
    return "\n".join(lines)[:max_chars]


def _source_review_validation_summary(item: dict[str, Any]) -> str:
    validation = item.get("resource_validation")
    if not isinstance(validation, dict):
        return ""
    checks = validation.get("required_files") or []
    if not isinstance(checks, list) or not checks:
        return ""
    parts: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = "present" if check.get("exists") else "missing"
        parts.append(f"{check.get('role', '-')}: {check.get('path', '-')}={status}")
    return "; build_resource_validation=" + ", ".join(parts) if parts else ""


def _source_repository_fetch_findings(review_input: ReviewInput) -> list[Finding]:
    findings: list[Finding] = []
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return findings
    errors = review_input.metadata.get("source_repository_review_errors") or []
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            findings.append(
                Finding(
                    severity="Medium",
                    file_path="GIT_VERSION",
                    line=None,
                    title="Locked repository commit was not fetched for deep review",
                    detail=f"{item.get('module', '-')} {item.get('repository_url', '-')}@{item.get('commit', '-')} could not be fetched: {item.get('error', '-')}",
                    recommendation="Fetch the locked repository commit diff and review the actual code changes before approving this GIT_VERSION MR.",
                    category="GIT_VERSION",
                )
            )
    return findings


def _build_resource_validation_findings(review_input: ReviewInput) -> list[Finding]:
    findings: list[Finding] = []
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return findings
    errors = review_input.metadata.get("build_resource_validation_errors") or []
    if not isinstance(errors, list):
        return findings
    for item in errors:
        if not isinstance(item, dict):
            continue
        findings.append(
            Finding(
                severity="High",
                file_path=str(item.get("build_file") or "GIT_VERSION"),
                line=None,
                title="Locked build repository commit is missing required build resources",
                detail=f"build.yml locks build repository commit {item.get('commit', '-')}, but {item.get('role', '-')} '{item.get('path', '-')}' could not be read at that commit: {item.get('error', '-')}",
                recommendation="Lock version.git_repository.commit/version.git_version4config.git_repository.commit to a build repository commit after the required build.yml and git_version.yml resources were pushed, or correct the referenced resource path.",
                category="GIT_VERSION",
            )
        )
    return findings


def _release_gate_validation_findings(review_input: ReviewInput) -> list[Finding]:
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return []
    gate = review_input.metadata.get("release_gate") or {}
    if not isinstance(gate, dict):
        return []
    findings: list[Finding] = []
    for item in gate.get("errors") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "High")
        findings.append(
            Finding(
                severity=severity if severity in {"Critical", "High", "Medium", "Low", "Warning"} else "High",
                file_path=str(item.get("file_path") or "GIT_VERSION"),
                line=item.get("line") if isinstance(item.get("line"), int) else None,
                title=str(item.get("title") or "GIT_VERSION release gate is not ready"),
                detail=str(item.get("detail") or "The locked build resources do not satisfy the release-gate checks."),
                recommendation=str(item.get("recommendation") or "Correct the locked build resources and run the GIT_VERSION review again."),
                category="Release Gate",
            )
        )
    return findings


def _release_note_traceability_findings(review_input: ReviewInput) -> list[Finding]:
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return []
    release_note_issues = _release_note_issue_lines(review_input)
    if not release_note_issues:
        return []
    source_reviews = review_input.metadata.get("source_repository_reviews") or []
    source_issues: set[str] = set()
    source_commits: list[str] = []
    for item in source_reviews:
        if not isinstance(item, dict) or item.get("kind") != "source":
            continue
        source_commits.append(str(item.get("commit") or ""))
        issues = item.get("issue_keys") or []
        if isinstance(issues, list):
            source_issues.update(str(value).upper() for value in issues if str(value).upper().startswith("ECHNL-"))
    if not source_issues:
        return []

    release_issues = set(release_note_issues)
    action_issue = str(review_input.jira_key or review_input.metadata.get("action_issue") or "").upper()
    missing = sorted(release_issues - source_issues)
    if not missing:
        return []
    if action_issue and action_issue not in missing and len(missing) <= 1:
        return []

    line = release_note_issues.get(action_issue) or next(iter(release_note_issues.values()), None)
    severity = "High" if action_issue and action_issue in missing else "Medium"
    commits = ", ".join(item for item in source_commits if item) or "-"
    return [
        Finding(
            severity=severity,
            file_path=_release_note_file(review_input),
            line=line,
            title="Release notes issues are not traceable to locked source commit",
            detail=(
                f"Release notes add ECHNL issues {', '.join(sorted(release_issues))}, "
                f"but fetched locked source commit context only shows {', '.join(sorted(source_issues))} "
                f"for source commit(s) {commits}. Missing traceability: {', '.join(missing)}."
            ),
            recommendation=(
                "Confirm the locked git_version commit contains the source/config changes for every release-note Jira issue. "
                "If those changes are in earlier commits, provide the previous-version-to-current-version compare evidence; otherwise update git_version.yml or release notes."
            ),
            category="GIT_VERSION",
        )
    ]


def _release_note_issue_lines(review_input: ReviewInput) -> dict[str, int | None]:
    issues: dict[str, int | None] = {}
    for changed_file in review_input.changed_files:
        name = Path(changed_file.path.replace("\\", "/")).name.lower()
        if "release notes" not in name:
            continue
        deleted_lines = _deleted_line_texts(changed_file.diff)
        for line_no, text in _added_lines(changed_file.diff):
            if text in deleted_lines:
                continue
            for issue in ISSUE_RE.findall(text):
                issue = issue.upper()
                if issue.startswith("ECHNL-"):
                    issues.setdefault(issue, line_no)
    return issues


def _added_lines(file_diff: str) -> list[tuple[int | None, str]]:
    result: list[tuple[int | None, str]] = []
    new_line_no: int | None = None
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line_no = int(match.group(1)) if match else None
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result.append((new_line_no, line[1:]))
        if new_line_no is not None and not line.startswith("-"):
            new_line_no += 1
    return result


def _deleted_line_texts(file_diff: str) -> set[str]:
    return {
        line[1:]
        for line in file_diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    }


def _release_note_file(review_input: ReviewInput) -> str:
    for changed_file in review_input.changed_files:
        if "release notes" in Path(changed_file.path.replace("\\", "/")).name.lower():
            return changed_file.path
    return "GIT_VERSION"


def _web_build_reference() -> str:
    root = Path(os.getenv("WEB_BUILD_TOOLS_DIR", r"D:\TTL\vibe-coding\web-build-tools"))
    docs_dir = root / "documents"
    resources_dir = root / "resources"
    max_chars = int(os.getenv("WEB_BUILD_REFERENCE_MAX_CHARS", "12000"))
    chunks: list[str] = []
    if docs_dir.exists():
        for path in sorted(docs_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunks.append(f"--- {path.name} ---\n{text[:4000]}")
    if resources_dir.exists():
        yml_files = sorted(resources_dir.glob("*/build*.yml")) + sorted(resources_dir.glob("*/git_version*.yml"))
        chunks.append("Reference resource yml files:\n" + "\n".join(f"- {item.relative_to(resources_dir)}" for item in yml_files[:40]))
    return "\n\n".join(chunks)[:max_chars]


def _web_build_domain_rules() -> str:
    return "\n".join(
        [
            "- Supported source_provide_method values are clone, base_version, and in_place.",
            "- build.yml version.git_version is resolved relative to the build.yml/version directory when it is a relative file name; absolute paths are also supported by the build scripts.",
            "- Build package versions are derived from build history (*.bh). If <ver_number>.bh is missing or empty, the package version is the base version; otherwise the next package version increments the last matching patch number.",
            "- A build history row is written as packageVersion, appName, developmentGitCommit, buildGitCommit, buildStart, buildEnd, buildDuration.",
            "- CreateGIT_VERSIONMR uses a three-commit flow: commit 1 adds release notes/YML resources, commit 2 sets build.yml commit to commit 1, and commit 3 sets build.yml commit to commit 2. Therefore a valid build.yml commit may intentionally point to the previous build-resource commit, not the MR head.",
            "- For iTrade/Services release notes, an SV item means build companies are read from the selected build template; otherwise company-specific release-note keys drive the build.yml companies list.",
            "- When build.yml provides a build repository commit, build.js fetches that exact commit and switches the configured branch to it; otherwise it fetches the configured branch head.",
            "- The same exact commit-lock behavior is used for development repositories from git_version.yml.",
            "- iTrade build.js consumes replace_with_ttl.companines as a historical compatibility key for ECHNL-4741; do not report it as a typo unless the script no longer reads that key.",
            "- When release resources are copied into company directories, build*.yml, git_version*.yml, release notes, and *.bh files are intentionally ignored.",
            "- DPS9/DPS11 backend database changes are centralized in db_change.scr and its referenced SQL/shell/resource files, usually through GIT_VERSION MR build resources. Review db_change.scr self-consistency, command ordering, referenced file existence, previous database version alignment, idempotency/rerun safety, environment scope, and missing execution inputs. Do not require or recommend Drupal update hooks/install schema as the review standard for these DPS database changes.",
            "- DPS environment settings are centralized in state_config.yml and state_config.<env>.yml files such as poc, sit, uat, preprod, and prod; historical state_cofig.<env>.yml spelling may also exist. Token/encryption values in those files are expected environment configuration; only report them when there is concrete evidence of invalid environment scoping, broken reference/encryption format, cross-environment leakage, or real leaked credentials outside this mechanism.",
        ]
    )


def _known_build_config_compat_notes() -> list[str]:
    root = Path(os.getenv("WEB_BUILD_TOOLS_DIR", r"D:\TTL\vibe-coding\web-build-tools"))
    resources_dir = root / "resources"
    notes: list[str] = []
    if not resources_dir.exists():
        return notes
    for path in sorted(resources_dir.glob("*/build.js")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        normalized = text.replace(" ", "")
        if "replaceWithTTL?.companines" in normalized or "replaceWithTTL.companines" in normalized:
            project = path.parent.name
            notes.append(
                f"{project}: replace_with_ttl.companines is a known compatibility key consumed by build.js; do not report it as a spelling typo unless the build script changes away from that key."
            )
    return notes[:20]


def _module_line(lines: list[tuple[int | None, str]], module: str) -> int | None:
    pattern = re.compile(rf"^\s*{re.escape(module)}\s*:")
    for line_no, text in lines:
        if pattern.match(text):
            return line_no
    return None


def _key_line(lines: list[tuple[int | None, str]], key: str) -> int | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for line_no, text in lines:
        if pattern.match(text):
            return line_no
    return None


def _version_from_git_version_name(path: str) -> str:
    match = re.search(r"git_version-v(.+?)\.ya?ml$", Path(path.replace("\\", "/")).name, re.I)
    return match.group(1) if match else ""


def _version_from_build_name(path: str) -> str:
    match = re.search(r"build-v(.+?)\.ya?ml$", Path(path.replace("\\", "/")).name, re.I)
    return match.group(1) if match else ""
