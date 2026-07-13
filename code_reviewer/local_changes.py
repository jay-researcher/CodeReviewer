from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, app_config_int, app_config_str, gitnexus_config
from .diff_parser import parse_unified_diff
from .jira_client import build_issue_branch_name
from .models import ChangedFile
from .process_utils import run_utf8


@dataclass(slots=True)
class LocalChangeSet:
    changed_files: list[ChangedFile]
    raw_diff: str
    base_sha: str
    head_sha: str
    repository: str
    cache_hit: bool = False


_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def local_merge_request_changes(
    primary_repo: Path,
    project_path: str,
    iid: str,
    mr: dict[str, Any],
    jira_key: str = "",
) -> LocalChangeSet | None:
    refs = mr.get("diff_refs") if isinstance(mr.get("diff_refs"), dict) else {}
    base_sha = _commit_sha(refs.get("base_sha") or refs.get("start_sha"))
    head_sha = _commit_sha(refs.get("head_sha") or mr.get("sha"))
    if not base_sha or not head_sha or base_sha == head_sha:
        return None

    cached = _read_cache(project_path, base_sha, head_sha)
    if cached:
        return cached

    candidates = _repository_candidates(primary_repo, project_path, str(mr.get("target_branch") or ""))
    last_error = ""
    for repo in candidates:
        if not (repo / ".git").exists():
            continue
        lock = _repo_lock(repo)
        with lock:
            cached = _read_cache(project_path, base_sha, head_sha)
            if cached:
                return cached
            try:
                _ensure_mr_commits(repo, iid, mr, base_sha, head_sha, jira_key=jira_key)
                completed = run_utf8(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "diff",
                        "--no-ext-diff",
                        "--no-color",
                        "--no-textconv",
                        "--find-renames=50%",
                        "--unified=3",
                        base_sha,
                        head_sha,
                        "--",
                    ],
                    timeout=app_config_int("local_context.local_diff_timeout_seconds", "LOCAL_DIFF_TIMEOUT_SECONDS", 120),
                )
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout or "git diff failed").strip()[:1000])
                raw_diff = completed.stdout
                changed_files = parse_unified_diff(raw_diff)
                if not raw_diff.strip() or not changed_files:
                    return None
                result = LocalChangeSet(
                    changed_files=changed_files,
                    raw_diff=raw_diff,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    repository=str(repo),
                )
                _write_cache(project_path, iid, result)
                return result
            except Exception as exc:
                last_error = str(exc)
                continue
    if last_error:
        raise RuntimeError(f"local MR diff unavailable: {last_error}")
    return None


def _ensure_mr_commits(
    repo: Path,
    iid: str,
    mr: dict[str, Any],
    base_sha: str,
    head_sha: str,
    jira_key: str = "",
) -> None:
    if _has_commit(repo, base_sha) and _has_commit(repo, head_sha):
        return
    errors: list[str] = []
    target_branch = str(mr.get("target_branch") or "").strip()
    source_branch = str(mr.get("source_branch") or "").strip()

    # Fetch exact, authoritative refs independently. A missing source branch must
    # not invalidate target/MR refs in one multi-refspec fetch.
    if not _has_commit(repo, base_sha) and target_branch:
        _fetch_refspec(
            repo,
            f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
            errors,
        )
    if not _has_commit(repo, head_sha) and re.fullmatch(r"\d+", iid or ""):
        _fetch_refspec(
            repo,
            f"+refs/merge-requests/{iid}/head:refs/codereviewer/merge-requests/{iid}/head",
            errors,
        )
    if not _has_commit(repo, head_sha) and source_branch:
        _fetch_refspec(
            repo,
            f"+refs/heads/{source_branch}:refs/remotes/origin/{source_branch}",
            errors,
        )

    for sha in (base_sha, head_sha):
        if _has_commit(repo, sha):
            continue
        _fetch_object(repo, sha, errors)

    # Last resort for servers that reject direct SHA fetch and no longer expose
    # the MR ref. Resolve only branches that actually exist on the remote.
    if (not _has_commit(repo, base_sha) or not _has_commit(repo, head_sha)) and _valid_jira_key(jira_key):
        for branch in _remote_issue_branches(repo, jira_key, errors):
            _fetch_refspec(
                repo,
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                errors,
            )
            if _has_commit(repo, base_sha) and _has_commit(repo, head_sha):
                break

    missing = [sha for sha in (base_sha, head_sha) if not _has_commit(repo, sha)]
    if missing:
        detail = f"; fetch errors: {' | '.join(errors[-4:])}" if errors else ""
        raise RuntimeError(f"local repository is missing MR commit(s): {', '.join(missing)}{detail}")


# Prefixes tried (in order) when source_branch is unknown.
# Mirrors the conventions documented in the Runbook's "Branch and Jira Mapping Rules".
_SOURCE_BRANCH_PREFIXES = ["feature", "improvement", "task", "bug", "change-request"]
_DPS_BRANCH_LAYERS = ["API", "DAO", "BIZ", "CLI"]


def _fetch_refspec(repo: Path, refspec: str, errors: list[str]) -> bool:
    completed = run_utf8(
        ["git", "-C", str(repo), "fetch", "--no-tags", "origin", refspec],
        timeout=_fetch_timeout(),
    )
    if completed.returncode == 0:
        return True
    errors.append(_git_error(completed, f"fetch {refspec}"))
    return False


def _fetch_object(repo: Path, sha: str, errors: list[str]) -> bool:
    completed = run_utf8(
        ["git", "-C", str(repo), "fetch", "--no-tags", "origin", sha],
        timeout=_fetch_timeout(),
    )
    if completed.returncode == 0:
        return True
    errors.append(_git_error(completed, f"fetch {sha}"))
    return False


def _remote_issue_branches(repo: Path, jira_key: str, errors: list[str]) -> list[str]:
    candidates = _issue_branch_candidates(jira_key)
    if not candidates:
        return []
    completed = run_utf8(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin", *[f"refs/heads/{item}" for item in candidates]],
        timeout=_fetch_timeout(),
    )
    if completed.returncode != 0:
        errors.append(_git_error(completed, f"resolve branches for {jira_key}"))
        return []
    existing = {
        line.split(None, 1)[1].removeprefix("refs/heads/")
        for line in completed.stdout.splitlines()
        if len(line.split(None, 1)) == 2 and line.split(None, 1)[1].startswith("refs/heads/")
    }
    return [candidate for candidate in candidates if candidate in existing]


def _issue_branch_candidates(jira_key: str) -> list[str]:
    key = (jira_key or "").strip().upper()
    if not _valid_jira_key(key):
        return []
    candidates: list[str] = []
    for prefix in _SOURCE_BRANCH_PREFIXES:
        candidates.append(build_issue_branch_name(key, issue_type=prefix))
        candidates.extend(build_issue_branch_name(key, issue_type=prefix, layer=layer) for layer in _DPS_BRANCH_LAYERS)
    candidates.extend(f"{layer}#{key}" for layer in _DPS_BRANCH_LAYERS)
    candidates.extend(f"{layer}-{key}" for layer in _DPS_BRANCH_LAYERS)
    candidates.append(key)
    return list(dict.fromkeys(candidates))


def _valid_jira_key(value: str) -> bool:
    return re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", (value or "").strip().upper()) is not None


def _fetch_timeout() -> int:
    return app_config_int("local_context.local_diff_fetch_timeout_seconds", "LOCAL_DIFF_FETCH_TIMEOUT_SECONDS", 120)


def _git_error(completed: Any, operation: str) -> str:
    detail = str(completed.stderr or completed.stdout or "git command failed").strip().replace("\n", " ")
    return f"{operation}: {detail[:500]}"


def _has_commit(repo: Path, sha: str) -> bool:
    completed = run_utf8(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], timeout=15)
    return completed.returncode == 0


def _repository_candidates(primary_repo: Path, project_path: str, branch: str) -> list[Path]:
    candidates = [primary_repo.resolve()]
    root = Path(
        app_config_str(
            "local_context.codebase_memory_source_root",
            "CODEBASE_MEMORY_SOURCE_ROOT",
            str(DATA_DIR / "codebase-memory-sources"),
        )
    ).expanduser().resolve()
    memory_name = _memory_project_name(project_path, branch)
    if root.is_dir():
        exact = root / memory_name
        if exact.is_dir():
            candidates.append(exact)
        candidates.extend(sorted(root.glob(f"{memory_name}__*"), key=lambda item: item.stat().st_mtime, reverse=True))
    result: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _cache_paths(project_path: str, base_sha: str, head_sha: str) -> tuple[Path, Path]:
    storage = Path(gitnexus_config()["storage_path"]).resolve() / "changes"
    project_key = hashlib.sha256(project_path.lower().encode("utf-8")).hexdigest()[:16]
    directory = storage / project_key
    stem = f"{base_sha}_{head_sha}"
    return directory / f"{stem}.diff.gz", directory / f"{stem}.json"


def _read_cache(project_path: str, base_sha: str, head_sha: str) -> LocalChangeSet | None:
    diff_path, metadata_path = _cache_paths(project_path, base_sha, head_sha)
    if not diff_path.is_file():
        return None
    try:
        with gzip.open(diff_path, "rt", encoding="utf-8", errors="replace") as handle:
            raw_diff = handle.read()
        changed_files = parse_unified_diff(raw_diff)
        if not changed_files:
            return None
        repository = ""
        if metadata_path.is_file():
            payload = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
            repository = str(payload.get("repository") or "") if isinstance(payload, dict) else ""
        return LocalChangeSet(changed_files, raw_diff, base_sha, head_sha, repository, cache_hit=True)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(project_path: str, iid: str, result: LocalChangeSet) -> None:
    diff_path, metadata_path = _cache_paths(project_path, result.base_sha, result.head_sha)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    temp_diff = diff_path.with_suffix(diff_path.suffix + ".tmp")
    with gzip.open(temp_diff, "wt", encoding="utf-8") as handle:
        handle.write(result.raw_diff)
    temp_diff.replace(diff_path)
    metadata = {
        "project_path": project_path,
        "mr_iid": iid,
        "base_sha": result.base_sha,
        "head_sha": result.head_sha,
        "repository": result.repository,
        "changed_files": [asdict(item) | {"diff": "[stored in diff.gz]"} for item in result.changed_files],
    }
    temp_metadata = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_metadata.replace(metadata_path)
    _prune_project_cache(metadata_path.parent)


def _prune_project_cache(directory: Path) -> None:
    maximum = app_config_int(
        "local_context.local_diff_cache_max_entries_per_project",
        "LOCAL_DIFF_CACHE_MAX_ENTRIES_PER_PROJECT",
        200,
    )
    if maximum <= 0:
        return
    metadata_files = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for metadata_path in metadata_files[maximum:]:
        diff_path = directory / f"{metadata_path.stem}.diff.gz"
        metadata_path.unlink(missing_ok=True)
        diff_path.unlink(missing_ok=True)


def _repo_lock(repo: Path) -> threading.Lock:
    key = str(repo.resolve()).lower()
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


def _commit_sha(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else ""


def _memory_project_name(project_path: str, branch: str) -> str:
    value = f"{project_path}-{branch or 'HEAD'}"
    return "".join(char if char.isalnum() or char in "._-" else "__" for char in value)
