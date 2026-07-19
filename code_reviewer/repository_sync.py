from __future__ import annotations

import json
import os
import fnmatch
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import DATA_DIR, ROOT_DIR, app_config_bool, app_config_int, app_config_list, app_config_str
from .local_workspaces import WorkspaceEntry, git_tools_project_entries, normalize_project_path
from .process_utils import run_utf8
from .remote_codebase_memory import remote_enabled, remote_index_matches, run_remote_tool


@dataclass(slots=True)
class RepositorySyncResult:
    project_path: str
    local_path: str
    branch: str
    commit: str = ""
    action: str = ""
    indexed: bool = False
    index_status: str = "disabled"
    codebase_memory_project: str = ""
    codebase_memory_context: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_SYNC_CACHE: dict[tuple[str, str], RepositorySyncResult] = {}
_INDEX_CACHE: set[tuple[str, str]] = set()
_PERSISTENT_INDEX_CACHE: dict[tuple[str, str], bool] = {}
_INDEX_THREAD_LOCK = threading.Lock()


def sync_workspace(entry: WorkspaceEntry, branch: str | None = None, *, index: bool = True, force: bool = False) -> RepositorySyncResult:
    configured_branch = _select_branch(entry, branch)
    try:
        selected_branch = resolve_branch_pattern(entry, configured_branch)
    except Exception as exc:
        return RepositorySyncResult(
            project_path=entry.project_path,
            local_path=str(entry.local_path),
            branch=configured_branch,
            action="failed",
            error=str(exc),
        )
    cache_key = (str(entry.local_path.resolve()), selected_branch.lower())
    if not force and cache_key in _SYNC_CACHE:
        return _SYNC_CACHE[cache_key]

    result = RepositorySyncResult(
        project_path=entry.project_path,
        local_path=str(entry.local_path),
        branch=selected_branch,
    )
    try:
        _ensure_clone(entry, selected_branch)
        _verify_origin(entry)
        _fetch(entry.local_path, selected_branch)
        result.action = _fast_forward_if_safe(entry.local_path, selected_branch)
        result.commit = _resolved_commit(entry.local_path, selected_branch)
        if index:
            _attach_codebase_memory(result, entry, selected_branch)
    except Exception as exc:
        result.error = str(exc)
        result.action = result.action or "failed"
    _SYNC_CACHE[cache_key] = result
    return result


def sync_all_workspaces(groups: str = "", *, index: bool = True, force: bool = False) -> list[RepositorySyncResult]:
    results: list[RepositorySyncResult] = []
    seen: set[tuple[str, str]] = set()
    for entry in git_tools_project_entries(groups=groups):
        try:
            branches = resolve_configured_branches(entry)
        except Exception as exc:
            results.append(
                RepositorySyncResult(
                    project_path=entry.project_path,
                    local_path=str(entry.local_path),
                    branch=", ".join(entry.branches),
                    action="failed",
                    error=str(exc),
                )
            )
            continue
        for branch in branches:
            key = (str(entry.local_path).lower(), branch.lower())
            if key in seen:
                continue
            seen.add(key)
            result = sync_workspace(entry, branch=branch, index=index, force=force)
            if result.error and "couldn't find remote ref" in result.error and branch:
                fallback = sync_workspace(entry, branch="", index=False, force=force)
                if not fallback.error:
                    fallback.branch = branch
                    fallback.action = f"configured branch {branch} not found; fetched available remote refs"
                    fallback.index_status = "not indexed: configured branch is unavailable"
                    fallback.indexed = False
                result = fallback
            results.append(result)
    return results


def _attach_codebase_memory(result: RepositorySyncResult, entry: WorkspaceEntry, branch: str) -> None:
    memory_name = _memory_project_name(entry, branch)
    result.codebase_memory_project = memory_name
    project_path = normalize_project_path(entry.project_path).lower()
    skip_patterns = app_config_list(
        "local_context.codebase_memory_skip_project_patterns",
        "CODEBASE_MEMORY_SKIP_PROJECT_PATTERNS",
        ["web-sv-build"],
    )
    matched_pattern = next((value for value in skip_patterns if value.lower() in project_path), "")
    if matched_pattern:
        result.indexed = False
        result.index_status = f"skipped: project matches {matched_pattern}"
        return
    try:
        if _persistent_index_matches(memory_name, result.commit):
            result.indexed = True
            result.index_status = "cached-persistent"
            result.codebase_memory_context = codebase_memory_architecture(memory_name)
            _INDEX_CACHE.add((memory_name, result.commit))
            return
        memory_source = _prepare_codebase_memory_source(entry.local_path, result.commit, memory_name)
        result.indexed, result.index_status = index_codebase_memory(
            memory_source,
            result.commit,
            project_name=memory_name,
        )
        if result.indexed:
            result.codebase_memory_context = codebase_memory_architecture(memory_name)
    except Exception as exc:
        # Codebase Memory enriches review context, but it must not block MR diff review.
        result.indexed = False
        result.index_status = f"skipped: {str(exc)[:500]}"


def index_codebase_memory(repo: Path, commit: str = "", project_name: str = "") -> tuple[bool, str]:
    if not app_config_bool("local_context.codebase_memory_enabled", "CODEBASE_MEMORY_ENABLED", True):
        return False, "disabled"
    resolved = _codebase_memory_executable()
    if not resolved:
        return False, "binary-not-found"
    cache_key = (project_name or str(repo.resolve()), commit)
    if cache_key in _INDEX_CACHE:
        return True, "cached"
    maximum_files = app_config_int(
        "local_context.codebase_memory_max_repository_files",
        "CODEBASE_MEMORY_MAX_REPOSITORY_FILES",
        50000,
    )
    tracked_files = _tracked_file_count(repo, maximum_files)
    if maximum_files > 0 and tracked_files > maximum_files:
        return False, f"skipped: repository has more than {maximum_files} tracked files"
    payload = json.dumps(
        {
            "repo_path": str(repo.resolve()),
            "name": project_name or repo.name,
            "mode": app_config_str("local_context.codebase_memory_index_mode", "CODEBASE_MEMORY_INDEX_MODE", "moderate"),
            "persistence": app_config_bool(
                "local_context.codebase_memory_persistence",
                "CODEBASE_MEMORY_PERSISTENCE",
                True,
            ),
        },
        ensure_ascii=False,
    )
    with _codebase_memory_index_lock() as acquired:
        if not acquired:
            return False, "skipped: another Codebase Memory index is already running"
        timeout = app_config_int("local_context.codebase_memory_timeout_seconds", "CODEBASE_MEMORY_TIMEOUT_SECONDS", 120)
        completed = _run_codebase_memory_tool(
            resolved,
            "index_repository",
            json.loads(payload),
            timeout=timeout,
            repo=repo,
            commit=commit,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "index command failed").strip()
        return False, f"failed: {detail[:500]}"
    try:
        response = json.loads(completed.stdout)
    except Exception:
        response = {}
    if isinstance(response, dict) and str(response.get("status") or "").lower() in {"error", "failed"}:
        return False, f"failed: {str(response.get('hint') or response.get('error') or completed.stdout)[:500]}"
    _INDEX_CACHE.add(cache_key)
    _PERSISTENT_INDEX_CACHE[cache_key] = True
    return True, "indexed"


def _tracked_file_count(repo: Path, stop_after: int) -> int:
    """Count Git-tracked files without walking untracked dependencies or caches."""
    completed = _run(["git", "-C", str(repo), "ls-files", "-z"], timeout=30)
    if completed.returncode != 0:
        return 0
    count = completed.stdout.count("\0")
    return stop_after + 1 if stop_after > 0 and count > stop_after else count


@contextmanager
def _codebase_memory_index_lock():
    """Allow only one memory-heavy indexer across threads and app processes."""
    if not _INDEX_THREAD_LOCK.acquire(blocking=False):
        yield False
        return
    lock_handle = None
    locked = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_handle = (DATA_DIR / "codebase-memory-index.lock").open("a+b")
        lock_handle.seek(0)
        if lock_handle.read(1) == b"":
            lock_handle.write(b"0")
            lock_handle.flush()
        lock_handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError):
            pass
        yield locked
    finally:
        if locked and lock_handle is not None:
            try:
                lock_handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_handle is not None:
            lock_handle.close()
        _INDEX_THREAD_LOCK.release()


def _persistent_index_matches(project_name: str, commit: str) -> bool:
    if not project_name or not commit:
        return False
    cache_key = (project_name, commit)
    if cache_key in _PERSISTENT_INDEX_CACHE:
        return _PERSISTENT_INDEX_CACHE[cache_key]
    if remote_enabled():
        matches = remote_index_matches(
            project_name,
            commit,
            app_config_int("local_context.codebase_memory_status_timeout_seconds", "CODEBASE_MEMORY_STATUS_TIMEOUT_SECONDS", 15),
        )
        _PERSISTENT_INDEX_CACHE[cache_key] = matches
        return matches
    executable = _codebase_memory_executable()
    if not executable:
        _PERSISTENT_INDEX_CACHE[cache_key] = False
        return False
    completed = _run_codebase_memory_tool(
        executable,
        "index_status",
        {"project": project_name},
        timeout=app_config_int(
            "local_context.codebase_memory_status_timeout_seconds",
            "CODEBASE_MEMORY_STATUS_TIMEOUT_SECONDS",
            15,
        ),
    )
    matches = False
    if completed.returncode == 0:
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            response = {}
        git = response.get("git") if isinstance(response, dict) else {}
        matches = bool(
            isinstance(response, dict)
            and str(response.get("status") or "").lower() == "ready"
            and isinstance(git, dict)
            and str(git.get("head_sha") or "").lower() == commit.lower()
        )
    _PERSISTENT_INDEX_CACHE[cache_key] = matches
    return matches


def codebase_memory_architecture(project_name: str) -> str:
    executable = _codebase_memory_executable()
    if not executable or not project_name:
        return ""
    try:
        completed = _run_codebase_memory_tool(
            executable,
            "get_architecture",
            {"project": project_name, "aspects": ["overview"]},
            timeout=app_config_int("local_context.codebase_memory_query_timeout_seconds", "CODEBASE_MEMORY_QUERY_TIMEOUT_SECONDS", 60),
        )
    except Exception as exc:
        return f"Codebase Memory architecture query failed: {exc}"
    if completed.returncode != 0:
        return f"Codebase Memory architecture query failed: {_command_error(completed)}"
    maximum = app_config_int("local_context.codebase_memory_context_max_chars", "CODEBASE_MEMORY_CONTEXT_MAX_CHARS", 20000)
    return completed.stdout.strip()[:maximum]


def codebase_memory_change_context(project_name: str, changed_files: list[str]) -> str:
    executable = _codebase_memory_executable()
    if not executable or not project_name or not changed_files:
        return ""
    maximum_files = app_config_int("local_context.codebase_memory_max_changed_files", "CODEBASE_MEMORY_MAX_CHANGED_FILES", 12)
    maximum_chars = app_config_int("local_context.codebase_memory_change_context_max_chars", "CODEBASE_MEMORY_CHANGE_CONTEXT_MAX_CHARS", 16000)
    blocks: list[str] = []
    used = 0
    for file_path in changed_files[:maximum_files]:
        payload = json.dumps(
            {
                "project": project_name,
                "file_pattern": file_path.replace("\\", "/"),
                "include_connected": True,
                "limit": 40,
            },
            ensure_ascii=False,
        )
        try:
            completed = _run_codebase_memory_tool(
                executable,
                "search_graph",
                json.loads(payload),
                timeout=app_config_int("local_context.codebase_memory_query_timeout_seconds", "CODEBASE_MEMORY_QUERY_TIMEOUT_SECONDS", 60),
            )
        except Exception as exc:
            blocks.append(f"Changed file {file_path}: graph query failed: {exc}")
            continue
        if completed.returncode != 0:
            blocks.append(f"Changed file {file_path}: graph query failed: {_command_error(completed)}")
            continue
        block = f"Changed file {file_path} symbols and connected dependencies:\n{completed.stdout.strip()}"
        remaining = maximum_chars - used
        if remaining <= 0:
            break
        blocks.append(block[:remaining])
        used += len(block)
    return "\n\n".join(blocks)[:maximum_chars]


def _select_branch(entry: WorkspaceEntry, requested: str | None) -> str:
    if requested is not None:
        return requested.strip()
    return entry.branches[0].strip() if entry.branches else ""


def resolve_configured_branches(entry: WorkspaceEntry) -> list[str]:
    """Resolve exact branches and version wildcards to concrete remote refs.

    Each wildcard selects the greatest matching version. Exact values remain
    unchanged, which keeps existing config.yml files backward compatible.
    """
    configured = [item.strip() for item in (entry.branches or [""]) if item is not None]
    if not any(_is_branch_pattern(item) for item in configured):
        return _unique_branches(configured or [""])
    remote = _remote_branch_names(entry)
    resolved: list[str] = []
    for item in configured:
        if not _is_branch_pattern(item):
            resolved.append(item)
            continue
        matches = [name for name in remote if fnmatch.fnmatchcase(name.casefold(), item.casefold())]
        if not matches:
            raise RuntimeError(
                f"no remote branch matches configured pattern {item!r} for {entry.project_path}"
            )
        resolved.append(max(matches, key=_version_branch_sort_key))
    return _unique_branches(resolved)


def resolve_branch_pattern(entry: WorkspaceEntry, configured_branch: str) -> str:
    """Return a concrete branch for one exact value or wildcard pattern."""
    value = (configured_branch or "").strip()
    if not _is_branch_pattern(value):
        return value
    matches = [
        name
        for name in _remote_branch_names(entry)
        if fnmatch.fnmatchcase(name.casefold(), value.casefold())
    ]
    if not matches:
        raise RuntimeError(
            f"no remote branch matches configured pattern {value!r} for {entry.project_path}"
        )
    return max(matches, key=_version_branch_sort_key)


def _remote_branch_names(entry: WorkspaceEntry) -> list[str]:
    repo = entry.local_path
    if (repo / ".git").is_dir():
        command = ["git", "-C", str(repo), "ls-remote", "--heads", "origin"]
    else:
        command = ["git", "ls-remote", "--heads", entry.repository_url]
    completed = _run(
        command,
        timeout=app_config_int(
            "local_context.branch_resolution_timeout_seconds",
            "BRANCH_RESOLUTION_TIMEOUT_SECONDS",
            60,
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot list remote branches for {entry.project_path}: {_command_error(completed)}"
        )
    names: list[str] = []
    for line in completed.stdout.splitlines():
        _sha, separator, ref = line.strip().partition("\t")
        if separator and ref.startswith("refs/heads/"):
            names.append(ref.removeprefix("refs/heads/"))
    return _unique_branches(names)


def _is_branch_pattern(value: str) -> bool:
    return any(character in (value or "") for character in "*?[")


def _version_branch_sort_key(value: str) -> tuple[tuple[int, ...], str]:
    """Sort dotted version branches naturally, with a deterministic tie-break."""
    numbers = tuple(int(item) for item in re.findall(r"\d+", value or ""))
    return numbers, (value or "").casefold()


def _unique_branches(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = (value or "").strip().casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append((value or "").strip())
    return result or [""]


def _ensure_clone(entry: WorkspaceEntry, branch: str) -> None:
    repo = entry.local_path
    if (repo / ".git").exists():
        return
    if repo.exists() and any(repo.iterdir()):
        raise RuntimeError(f"local_working_copy exists but is not a Git repository: {repo}")
    repo.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "clone"]
    if branch:
        command.extend(["--branch", branch])
    command.extend([entry.repository_url, str(repo)])
    completed = _run(command, timeout=int(os.getenv("REPOSITORY_CLONE_TIMEOUT_SECONDS", "900")))
    if completed.returncode != 0:
        raise RuntimeError(f"git clone failed for {entry.project_path}: {_command_error(completed)}")


def _verify_origin(entry: WorkspaceEntry) -> None:
    completed = _run(["git", "-C", str(entry.local_path), "config", "--get", "remote.origin.url"])
    actual = completed.stdout.strip() if completed.returncode == 0 else ""
    if normalize_project_path(actual) != normalize_project_path(entry.repository_url):
        raise RuntimeError(
            f"origin mismatch for {entry.local_path}: configured={entry.repository_url}, actual={actual or '-'}"
        )


def _fetch(repo: Path, branch: str) -> None:
    command = ["git", "-C", str(repo), "fetch", "--prune", "origin"]
    if branch:
        command.append(f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    completed = _run(command, timeout=int(os.getenv("REPOSITORY_FETCH_TIMEOUT_SECONDS", "300")))
    if completed.returncode != 0:
        raise RuntimeError(f"git fetch failed for {repo}: {_command_error(completed)}")


def _fast_forward_if_safe(repo: Path, branch: str) -> str:
    if not branch:
        return "fetched"
    current = _current_branch(repo)
    if current.lower() != branch.lower():
        return f"fetched (checkout remains {current or 'detached'})"
    status = _run(["git", "-C", str(repo), "status", "--porcelain"]).stdout.strip()
    if status:
        return "fetched (working tree has local changes)"
    completed = _run(["git", "-C", str(repo), "merge", "--ff-only", f"refs/remotes/origin/{branch}"])
    if completed.returncode != 0:
        raise RuntimeError(f"fast-forward failed for {repo} branch {branch}: {_command_error(completed)}")
    return "fetched and fast-forwarded"


def _resolved_commit(repo: Path, branch: str) -> str:
    ref = f"refs/remotes/origin/{branch}" if branch else "refs/remotes/origin/HEAD"
    completed = _run(["git", "-C", str(repo), "rev-parse", ref])
    if completed.returncode != 0 and not branch:
        ref = "HEAD"
        completed = _run(["git", "-C", str(repo), "rev-parse", ref])
    if completed.returncode != 0:
        raise RuntimeError(f"cannot resolve {ref} in {repo}: {_command_error(completed)}")
    return completed.stdout.strip()


def _current_branch(repo: Path) -> str:
    return _run(["git", "-C", str(repo), "branch", "--show-current"]).stdout.strip()


def _prepare_codebase_memory_source(repo: Path, commit: str, project_name: str) -> Path:
    root = Path(os.getenv("CODEBASE_MEMORY_SOURCE_ROOT", str(DATA_DIR / "codebase-memory-sources"))).expanduser().resolve()
    destination = _codebase_memory_source_destination(root, project_name, commit)
    if root != destination and root not in destination.parents:
        raise RuntimeError(f"unsafe Codebase Memory source path: {destination}")
    created = False
    if not (destination / ".git").exists():
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Codebase Memory source exists but is not a Git repository: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = _run(["git", "clone", "--shared", "--no-checkout", str(repo), str(destination)], timeout=300)
        if completed.returncode != 0:
            raise RuntimeError(f"cannot create Codebase Memory source for {project_name}: {_command_error(completed)}")
        created = True
    if not created:
        status = _run(["git", "-C", str(destination), "status", "--porcelain"]).stdout.strip()
        if status:
            alternate = _codebase_memory_source_destination(root, project_name, commit, force_commit_suffix=True)
            if alternate != destination:
                return _prepare_codebase_memory_source_at_destination(repo, commit, project_name, root, alternate)
            raise RuntimeError(f"Codebase Memory source has unexpected local changes: {destination}")
    completed = _run(
        ["git", "-C", str(destination), "checkout", "--detach", commit],
        timeout=app_config_int("local_context.codebase_memory_checkout_timeout_seconds", "CODEBASE_MEMORY_CHECKOUT_TIMEOUT_SECONDS", 30),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot update Codebase Memory source {project_name} to {commit}: {_command_error(completed)}")
    return destination


def _prepare_codebase_memory_source_at_destination(
    repo: Path,
    commit: str,
    project_name: str,
    root: Path,
    destination: Path,
) -> Path:
    if root != destination and root not in destination.parents:
        raise RuntimeError(f"unsafe Codebase Memory source path: {destination}")
    if not (destination / ".git").exists():
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Codebase Memory source exists but is not a Git repository: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = _run(["git", "clone", "--shared", "--no-checkout", str(repo), str(destination)], timeout=300)
        if completed.returncode != 0:
            raise RuntimeError(f"cannot create Codebase Memory source for {project_name}: {_command_error(completed)}")
    completed = _run(
        ["git", "-C", str(destination), "checkout", "--detach", commit],
        timeout=app_config_int("local_context.codebase_memory_checkout_timeout_seconds", "CODEBASE_MEMORY_CHECKOUT_TIMEOUT_SECONDS", 30),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot update Codebase Memory source {project_name} to {commit}: {_command_error(completed)}")
    return destination


def _codebase_memory_source_destination(
    root: Path,
    project_name: str,
    commit: str,
    *,
    force_commit_suffix: bool = False,
) -> Path:
    if force_commit_suffix:
        suffix = (commit or "unknown")[:12]
        return (root / f"{project_name}__{suffix}").resolve()
    return (root / project_name).resolve()


def _memory_project_name(entry: WorkspaceEntry, branch: str) -> str:
    value = f"{entry.project_path}-{branch or 'HEAD'}"
    return "".join(char if char.isalnum() or char in "._-" else "__" for char in value)


def _codebase_memory_executable() -> str:
    if remote_enabled():
        return "remote-ssh"
    bundled = ROOT_DIR / "tools" / "codebase-memory-mcp" / "codebase-memory-mcp.exe"
    executable = os.getenv("CODEBASE_MEMORY_COMMAND", str(bundled) if bundled.is_file() else "codebase-memory-mcp")
    return shutil.which(executable) or (executable if Path(executable).is_file() else "")


def _run_codebase_memory_tool(
    executable: str,
    tool: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    repo: Path | None = None,
    commit: str = "",
) -> subprocess.CompletedProcess[str]:
    if remote_enabled():
        return run_remote_tool(tool, payload, timeout=timeout, repo=repo, commit=commit)
    return run_utf8(
        [executable, "cli", tool],
        input_text=json.dumps(payload, ensure_ascii=False),
        timeout=timeout,
    )


def _run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run_utf8(command, timeout=timeout)


def _command_error(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()[:1000]
