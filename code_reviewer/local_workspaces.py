from __future__ import annotations

import json
import os
import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from .gitlab_client import parse_repository_url
from .config import (
    app_config_bool,
    app_config_int,
    app_config_list,
    git_tools_config_path,
    load_effective_config_payload,
)


@dataclass(slots=True)
class WorkspaceEntry:
    project_path: str
    local_path: Path
    group: str = ""
    module: str = ""
    repository_url: str = ""
    responsible: str = ""
    project_name: str = ""
    project_type: str = ""
    llm_model: str = ""
    application: str = ""
    release_line: str = ""
    release_lines: list[str] = field(default_factory=list)
    dev_branch: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    source: str = ""


SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    ".workspace",
    "__pycache__",
    "node_modules",
    "vendor",
    "var",
    "cache",
    "coverage",
    "dist",
    "build",
    "reports",
    "outputs",
    "db",
}


def resolve_workspace_for_project_path(project_path: str, branch: str = "") -> WorkspaceEntry | None:
    if not app_config_bool("local_context.auto", "LOCAL_CONTEXT_AUTO", True):
        return None
    normalized = _normalize_project_path(project_path)
    if not normalized:
        return None
    entries = [
        entry
        for entry in load_workspace_entries()
        if _normalize_project_path(entry.project_path) == normalized
    ]
    branch_key = _normalize_branch(branch)
    if branch_key:
        matched = [
            entry
            for entry in entries
            if any(
                branch_key == _normalize_branch(item)
                or (
                    any(character in item for character in "*?[")
                    and fnmatch.fnmatchcase(branch_key, _normalize_branch(item))
                )
                for item in entry.branches
            )
        ]
        if matched:
            entries = matched
    for entry in entries:
        if entry.local_path.is_dir():
            return entry
    return None


def resolve_context_repo_for_project_path(project_path: str, branch: str = "") -> Path | None:
    entry = resolve_workspace_for_project_path(project_path, branch=branch)
    return entry.local_path if entry else None


def workspace_entries_for_issue_review(groups: str = "", projects: str = "") -> list[WorkspaceEntry]:
    entries = load_workspace_entries(groups=groups, projects=projects)
    return [entry for entry in entries if entry.local_path.is_dir() and (entry.local_path / ".git").exists()]


def git_tools_project_entries(groups: str = "") -> list[WorkspaceEntry]:
    return _git_tools_entries(groups=groups)


def normalize_project_path(value: str) -> str:
    return _normalize_project_path(value)


def load_workspace_entries(groups: str = "", projects: str = "") -> list[WorkspaceEntry]:
    git_projects = _git_tools_entries(groups=groups)
    explicit = _explicit_workspace_entries()
    roots = _discover_root_workspace_entries(git_projects)
    configured = [entry for entry in git_projects if str(entry.local_path) not in {"", "."}]
    entries = [*configured, *explicit, *roots]

    if not entries:
        return []

    git_by_path = {_normalize_project_path(item.project_path): item for item in git_projects}
    git_by_group_module = {
        (item.group, item.module): item
        for item in git_projects
        if item.group and item.module
    }
    allowed_projects = {_normalize_project_path(item) for item in _split_values(projects)}
    result: list[WorkspaceEntry] = []
    seen: set[str] = set()
    for entry in entries:
        normalized = _normalize_project_path(entry.project_path)
        if normalized not in git_by_path and (entry.group, entry.module) in git_by_group_module:
            git_entry = git_by_group_module[(entry.group, entry.module)]
            entry.project_path = git_entry.project_path
            if not entry.repository_url:
                entry.repository_url = git_entry.repository_url
            if not entry.responsible:
                entry.responsible = git_entry.responsible
            if not entry.project_name:
                entry.project_name = git_entry.project_name
            if not entry.project_type:
                entry.project_type = git_entry.project_type
            if not entry.llm_model:
                entry.llm_model = git_entry.llm_model
            if not entry.application:
                entry.application = git_entry.application
            if not entry.release_line:
                entry.release_line = git_entry.release_line
            if not entry.release_lines:
                entry.release_lines = git_entry.release_lines
            if not entry.dev_branch:
                entry.dev_branch = git_entry.dev_branch
            if not entry.branches:
                entry.branches = git_entry.branches
            normalized = _normalize_project_path(entry.project_path)
        if not normalized:
            continue
        if allowed_projects and normalized not in allowed_projects:
            continue
        if git_by_path and normalized not in git_by_path:
            continue
        if normalized in git_by_path:
            git_entry = git_by_path[normalized]
            if not entry.group:
                entry.group = git_entry.group
            if not entry.module:
                entry.module = git_entry.module
            if not entry.repository_url:
                entry.repository_url = git_entry.repository_url
            if not entry.responsible:
                entry.responsible = git_entry.responsible
            if not entry.project_name:
                entry.project_name = git_entry.project_name
            if not entry.project_type:
                entry.project_type = git_entry.project_type
            if not entry.llm_model:
                entry.llm_model = git_entry.llm_model
            if not entry.application:
                entry.application = git_entry.application
            if not entry.release_line:
                entry.release_line = git_entry.release_line
            if not entry.release_lines:
                entry.release_lines = git_entry.release_lines
            if not entry.dev_branch:
                entry.dev_branch = git_entry.dev_branch
            if not entry.branches:
                entry.branches = git_entry.branches
        key = f"{normalized}|{entry.local_path.resolve() if entry.local_path.exists() else entry.local_path}"
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _explicit_workspace_entries() -> list[WorkspaceEntry]:
    path = Path(os.getenv("LOCAL_WORKSPACE_CONFIG", "data/local_workspaces.yml")).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    payload: Any = None
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
    elif yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        parsed = _workspace_entries_from_payload(payload, path.parent)
        if parsed:
            return parsed
    return _workspace_entries_from_text(text, path.parent)


def _workspace_entries_from_payload(payload: dict[str, Any], base: Path) -> list[WorkspaceEntry]:
    entries: list[WorkspaceEntry] = []

    projects = payload.get("projects")
    if isinstance(projects, dict):
        for key, value in projects.items():
            if isinstance(value, str):
                entries.append(_workspace_entry(str(key), value, base, source="workspace-config"))
            elif isinstance(value, dict):
                project_path = str(value.get("project_path") or value.get("repository_url") or key)
                entries.append(
                    _workspace_entry(
                        project_path,
                        str(value.get("path") or value.get("local_path") or ""),
                        base,
                        group=str(value.get("group") or ""),
                        module=str(value.get("module") or ""),
                        repository_url=str(value.get("repository_url") or ""),
                        responsible=str(value.get("responsible") or ""),
                        project_name=str(value.get("project_name") or ""),
                        project_type=str(value.get("type") or value.get("project_type") or ""),
                        llm_model=str(value.get("llm_model") or ""),
                        application=str(value.get("application") or ""),
                        release_line=str(value.get("release_line") or ""),
                        release_lines=_string_list(value.get("release_lines")),
                        dev_branch=_string_list(value.get("dev_branch")),
                        branches=_string_list(value.get("branch") or value.get("branches")),
                        source="workspace-config",
                    )
                )
    elif isinstance(projects, list):
        for value in projects:
            if not isinstance(value, dict):
                continue
            project_path = str(value.get("project_path") or value.get("repository_url") or "")
            entries.append(
                _workspace_entry(
                    project_path,
                    str(value.get("path") or value.get("local_path") or ""),
                    base,
                    group=str(value.get("group") or ""),
                module=str(value.get("module") or ""),
                repository_url=str(value.get("repository_url") or ""),
                responsible=str(value.get("responsible") or ""),
                project_name=str(value.get("project_name") or ""),
                project_type=str(value.get("type") or value.get("project_type") or ""),
                llm_model=str(value.get("llm_model") or ""),
                application=str(value.get("application") or ""),
                release_line=str(value.get("release_line") or ""),
                release_lines=_string_list(value.get("release_lines")),
                dev_branch=_string_list(value.get("dev_branch")),
                branches=_string_list(value.get("branch") or value.get("branches")),
                source="workspace-config",
            )
        )

    groups = payload.get("groups")
    if isinstance(groups, dict):
        for group, modules in groups.items():
            if not isinstance(modules, dict):
                continue
            for module, value in modules.items():
                if isinstance(value, str):
                    entries.append(_workspace_entry(str(module), value, base, group=str(group), module=str(module), source="workspace-config"))
                elif isinstance(value, dict):
                    entries.append(
                        _workspace_entry(
                            str(value.get("project_path") or value.get("repository_url") or module),
                            str(value.get("path") or value.get("local_path") or ""),
                            base,
                            group=str(group),
                            module=str(module),
                            repository_url=str(value.get("repository_url") or ""),
                            responsible=str(value.get("responsible") or ""),
                            project_name=str(value.get("project_name") or ""),
                            project_type=str(value.get("type") or value.get("project_type") or ""),
                            llm_model=str(value.get("llm_model") or ""),
                            application=str(value.get("application") or ""),
                            release_line=str(value.get("release_line") or ""),
                            release_lines=_string_list(value.get("release_lines")),
                            dev_branch=_string_list(value.get("dev_branch")),
                            branches=_string_list(value.get("branch") or value.get("branches")),
                            source="workspace-config",
                        )
                    )

    return [entry for entry in entries if entry.project_path and str(entry.local_path)]


def _workspace_entries_from_text(text: str, base: Path) -> list[WorkspaceEntry]:
    entries: list[WorkspaceEntry] = []
    current_group = ""
    for raw_line in text.splitlines():
        if re.match(r"^\s{2}[A-Za-z0-9_.-]+:\s*$", raw_line):
            current_group = raw_line.strip().rstrip(":")
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().strip("'\"")
        value = value.strip().strip("'\"")
        if not value:
            continue
        if "/" in key:
            entries.append(_workspace_entry(key, value, base, source="workspace-config-text"))
        elif current_group and raw_line.startswith("    "):
            entries.append(_workspace_entry(key, value, base, group=current_group, module=key, source="workspace-config-text"))
    return entries


def _workspace_entry(
    project_or_url: str,
    local_path: str,
    base: Path,
    group: str = "",
    module: str = "",
    repository_url: str = "",
    responsible: str = "",
    project_name: str = "",
    project_type: str = "",
    llm_model: str = "",
    application: str = "",
    release_line: str = "",
    release_lines: list[str] | None = None,
    dev_branch: list[str] | None = None,
    branches: list[str] | None = None,
    source: str = "",
) -> WorkspaceEntry:
    project_path = _project_path_from_value(project_or_url)
    path = Path(local_path).expanduser()
    if not path.is_absolute():
        path = base / path
    return WorkspaceEntry(
        project_path=project_path,
        local_path=path,
        group=group,
        module=module,
        repository_url=repository_url or project_or_url,
        responsible=responsible,
        project_name=project_name,
        project_type=project_type,
        llm_model=llm_model,
        application=application,
        release_line=release_line,
        release_lines=release_lines or ([release_line] if release_line else []),
        dev_branch=dev_branch or [],
        branches=branches or [],
        source=source,
    )


def _git_tools_entries(groups: str = "") -> list[WorkspaceEntry]:
    config_path = git_tools_config_path()
    if not config_path.exists():
        return []
    selected_groups = set(_split_values(groups)) if groups else set(app_config_list("git_tools.groups", "GIT_TOOLS_GROUPS", []))
    payload = load_effective_config_payload()
    if isinstance(payload, dict):
        return _git_tools_entries_from_payload(payload, selected_groups)
    return []


def _git_tools_entries_from_payload(payload: dict[str, Any], groups: set[str]) -> list[WorkspaceEntry]:
    entries: list[WorkspaceEntry] = []
    for group, modules in payload.items():
        if groups and group not in groups:
            continue
        if not isinstance(modules, dict):
            continue
        _collect_git_tools_payload_entries(entries, str(group), modules)
    return entries


def _collect_git_tools_payload_entries(
    entries: list[WorkspaceEntry],
    group: str,
    value: dict[str, Any],
    *,
    module: str = "",
    inherited: dict[str, Any] | None = None,
) -> None:
    """Read both flat git-tools config and JiraReviewer nested project config."""
    inherited = dict(inherited or {})
    for key in (
        "responsible",
        "project_name",
        "llm_model",
        "application",
        "release_line",
        "release_lines",
        "dev_branch",
        "branch",
        "branches",
        "type",
        "project_type",
    ):
        if key in value and value.get(key) not in (None, "", []):
            inherited[key] = value.get(key)

    url = _clean_repository_url(str(value.get("repository_url") or ""))
    project_path = _project_path_from_value(url)
    if project_path:
        responsible = _people_text(value.get("responsible", inherited.get("responsible", "")))
        project_type = _normalize_project_type(value.get("type") or value.get("project_type") or inherited.get("type") or inherited.get("project_type"))
        branches = _string_list(value.get("branch") or value.get("branches") or inherited.get("branch") or inherited.get("branches"))
        release_line = str(value.get("release_line") or inherited.get("release_line") or "").strip()
        release_lines = _string_list(value.get("release_lines") or inherited.get("release_lines"))
        if release_line and release_line not in release_lines:
            release_lines.insert(0, release_line)
        base = dict(
            project_path=project_path,
            group=group,
            module=module,
            repository_url=url,
            responsible=responsible,
            project_name=str(value.get("project_name") or inherited.get("project_name") or ""),
            project_type=project_type,
            llm_model=str(value.get("llm_model") or inherited.get("llm_model") or ""),
            application=str(value.get("application") or inherited.get("application") or ""),
            release_line=release_line,
            release_lines=release_lines,
            dev_branch=_string_list(value.get("dev_branch") or inherited.get("dev_branch")),
            branches=branches,
            source="git-tools",
        )
        copies = value.get("working_copies")
        added_copy = False
        if isinstance(copies, list):
            for copy in copies:
                if not isinstance(copy, dict):
                    continue
                copy_branches = _string_list(copy.get("branch") or copy.get("branches")) or branches
                copy_path = _local_path(copy.get("local_working_copy"), config_base=git_tools_config_path().parent)
                entries.append(WorkspaceEntry(local_path=copy_path, branches=copy_branches, **base))
                added_copy = True
        if not added_copy:
            entries.append(
                WorkspaceEntry(
                    local_path=_local_path(value.get("local_working_copy"), config_base=git_tools_config_path().parent),
                    **base,
                )
            )

    for child_key, child in value.items():
        if not isinstance(child, dict) or child_key == "working_copies":
            continue
        _collect_git_tools_payload_entries(
            entries,
            group,
            child,
            module=str(child_key),
            inherited=inherited,
        )


def _people_text(value: Any) -> str:
    if isinstance(value, list):
        return "+".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_project_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"frontend", "front-end", "web", "client"}:
        return "frontend"
    if text in {"backend", "back-end", "server", "api"}:
        return "backend"
    return text


def _git_tools_entries_from_text(text: str, groups: set[str]) -> list[WorkspaceEntry]:
    entries: list[WorkspaceEntry] = []
    group = ""
    module = ""
    fields: dict[str, Any] = {}
    group_fields: dict[str, Any] = {}
    list_field = ""

    def flush() -> None:
        if groups and group not in groups:
            return
        url = _clean_repository_url(fields.get("repository_url", ""))
        project_path = _project_path_from_value(url)
        if project_path:
            entries.append(
                WorkspaceEntry(
                    project_path=project_path,
                    local_path=_local_path(fields.get("local_working_copy", ""), config_base=git_tools_config_path().parent),
                    group=group,
                    module=module,
                    repository_url=url,
                    responsible=fields.get("responsible", ""),
                    project_name=fields.get("project_name", ""),
                    project_type=_normalize_project_type(fields.get("type") or fields.get("project_type")),
                    llm_model=fields.get("llm_model", ""),
                    application=fields.get("application", ""),
                    release_line=fields.get("release_line", ""),
                    release_lines=_string_list(fields.get("release_lines", "")),
                    dev_branch=_string_list(fields.get("dev_branch", "")),
                    branches=_string_list(fields.get("branch", "")),
                    source="git-tools",
                )
            )

    for raw_line in text.splitlines():
        group_match = re.match(r"^([^\s:#][^:#]*):\s*$", raw_line)
        if group_match:
            flush()
            group = group_match.group(1).strip()
            module = ""
            fields = {}
            group_fields = {}
            list_field = ""
            continue
        module_match = re.match(r"^\s{2}([A-Za-z0-9_.-]+):\s*$", raw_line)
        if module_match:
            flush()
            module = module_match.group(1).strip()
            fields = dict(group_fields)
            list_field = ""
            continue
        if groups and group not in groups:
            continue
        group_field_match = re.match(r"^\s{2}([A-Za-z0-9_.-]+):\s*([^\r\n]+)", raw_line)
        if group_field_match and not module:
            key = group_field_match.group(1).strip()
            value = group_field_match.group(2).strip().strip("'\"")
            group_fields[key] = value
            fields[key] = value
            continue
        list_match = re.match(r"^\s{6}-\s*([^\r\n]+)", raw_line)
        if list_match and list_field:
            values = fields.setdefault(list_field, [])
            if isinstance(values, list):
                values.append(list_match.group(1).strip().strip("'\""))
            continue
        empty_match = re.match(r"^\s{4}([A-Za-z0-9_.-]+):\s*$", raw_line)
        if empty_match:
            list_field = empty_match.group(1).strip()
            fields[list_field] = []
            continue
        match = re.match(r"^\s{4}([A-Za-z0-9_.-]+):\s*([^\r\n]+)", raw_line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip().strip("'\"")
        if not value:
            fields[key] = []
            list_field = key
            continue
        fields[key] = value
        list_field = ""
    flush()
    return entries


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip().strip("'\"")
    if not text:
        return []
    text = text.strip("[]")
    return [item.strip().strip("'\"") for item in re.split(r"[,;]+", text) if item.strip().strip("'\"")]


def _discover_root_workspace_entries(git_projects: list[WorkspaceEntry]) -> list[WorkspaceEntry]:
    roots = [Path(item).expanduser() for item in app_config_list("local_context.workspace_roots", "LOCAL_WORKSPACE_ROOTS", [])]
    if not roots:
        return []
    allowed = {_normalize_project_path(item.project_path) for item in git_projects}
    max_depth = app_config_int("local_context.workspace_scan_max_depth", "LOCAL_WORKSPACE_SCAN_MAX_DEPTH", 5)
    entries: list[WorkspaceEntry] = []
    for root in roots:
        if not root.is_dir():
            continue
        for repo in _iter_git_repositories(root, max_depth=max_depth):
            remote = _git_remote_origin(repo)
            project_path = _project_path_from_value(remote)
            normalized = _normalize_project_path(project_path)
            if not normalized:
                continue
            if allowed and normalized not in allowed:
                continue
            entries.append(WorkspaceEntry(project_path=project_path, local_path=repo, repository_url=remote, source="workspace-root"))
    return entries


def _iter_git_repositories(root: Path, max_depth: int) -> list[Path]:
    repos: list[Path] = []
    root = root.resolve()
    for current, dirnames, _filenames in os.walk(root):
        path = Path(current)
        depth = len(path.relative_to(root).parts)
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        try:
            names = os.listdir(path)
        except OSError:
            dirnames[:] = []
            continue
        if ".git" in names:
            repos.append(path)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
    return repos


def _git_remote_origin(repo: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except Exception:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _project_path_from_value(value: str) -> str:
    text = _clean_repository_url(value)
    if not text:
        return ""
    try:
        _base, project_path = parse_repository_url(text, fallback_base_url=os.getenv("GITLAB_URL", "https://gitlab.tx-tech.com"))
        return _normalize_project_path(project_path)
    except Exception:
        return _normalize_project_path(text)


def _clean_repository_url(value: str) -> str:
    text = (value or "").strip().strip("'\"")
    match = re.search(r"(?:https?://[^\s\"']+?\.git|git@[^\"'\s]+?\.git)", text)
    return match.group(0) if match else text


def _normalize_project_path(value: str) -> str:
    text = (value or "").strip().strip("'\"")
    if not text:
        return ""
    if text.endswith(".git"):
        text = text[:-4]
    text = text.replace("\\", "/").strip("/")
    for prefix in ("https://gitlab.tx-tech.com/", "http://gitlab.tx-tech.com/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("git@gitlab.tx-tech.com:"):
        text = text.split(":", 1)[1]
    return text.lower()


def _normalize_branch(value: str) -> str:
    return (value or "").strip().lower()


def _local_path(value: Any, config_base: Path) -> Path:
    text = str(value or "").strip().strip("'\"")
    if not text:
        return Path()
    path = Path(text).expanduser()
    return path if path.is_absolute() else config_base / path


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\n]+", value or "") if item.strip()]
