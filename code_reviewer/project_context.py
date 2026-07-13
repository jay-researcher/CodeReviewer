from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from .models import ReviewInput
from .config import app_config_int
from .process_utils import run_utf8
from .resource_optimizer import is_optimizable_web_resource, resource_context_file_limit


SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    "coverage",
}

KEY_FILES = {
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "package.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "composer.json",
    "Dockerfile",
    "docker-compose.yml",
}

CONTEXT_NOISE_FILE_NAMES = {
    "readme.md",
    ".gitlab-ci.yml",
    "phpcs-report.xml",
    "report.xml",
    "coverage.xml",
    "junit.xml",
}

CONTEXT_NOISE_SUFFIXES = (
    ".report.xml",
    ".min.js",
    ".min.css",
    ".map",
)

PHP_SYMBOL_RE = re.compile(r"\b(?:use|extends|implements|new)\s+\\?([A-Za-z_][\w\\]*)")
JS_IMPORT_RE = re.compile(r"\b(?:from\s*|require\s*\()['\"]([^'\"]+)['\"]")

TEXT_EXTENSIONS = {
    ".java",
    ".kt",
    ".groovy",
    ".php",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".dart",
    ".go",
    ".sql",
    ".xml",
    ".json",
    ".yml",
    ".yaml",
    ".properties",
    ".ini",
    ".conf",
    ".cfg",
    ".md",
    ".txt",
    ".sh",
    ".bat",
    ".ps1",
}


def attach_project_context(review_input: ReviewInput, repo: Path | str | None, ref: str = "") -> None:
    if not repo:
        return
    repo_path = Path(repo).expanduser()
    context = build_project_context(repo_path, [item.path for item in review_input.changed_files], ref=ref)
    review_input.metadata["project_context_path"] = str(repo_path)
    review_input.metadata["project_context_files_count"] = context["files_count"]
    review_input.metadata["project_context_included_files"] = context["included_files"]
    review_input.metadata["project_context_truncated"] = context["truncated"]
    review_input.metadata["project_context_ref"] = context["ref"]
    review_input.metadata["project_context_commit"] = context["commit"]
    review_input.metadata["project_context"] = context["text"]


def build_project_context(repo: Path, changed_paths: list[str], ref: str = "") -> dict[str, object]:
    if not repo.exists() or not repo.is_dir():
        raise FileNotFoundError(f"Project context path does not exist or is not a directory: {repo}")

    max_chars = app_config_int("local_context.project_context_max_chars", "PROJECT_CONTEXT_MAX_CHARS", 80000)
    max_tree_files = app_config_int("local_context.project_context_max_tree_files", "PROJECT_CONTEXT_MAX_TREE_FILES", 120)
    max_selection_files = app_config_int(
        "local_context.project_context_max_selection_files",
        "PROJECT_CONTEXT_MAX_SELECTION_FILES",
        3000,
    )
    max_file_chars = app_config_int("local_context.project_context_max_file_chars", "PROJECT_CONTEXT_MAX_FILE_CHARS", 12000)

    resolved_ref, commit = _resolve_git_ref(repo, ref)
    selection_limit = max(max_tree_files, max_selection_files)
    files = _list_git_files(repo, resolved_ref, selection_limit) if resolved_ref else _list_files(repo, selection_limit)
    selected = _select_context_files(repo, files, changed_paths, git_ref=resolved_ref)
    tree_files = files[:max_tree_files]

    lines: list[str] = [
        f"Local project context root: {repo}",
        f"Context source ref: {resolved_ref or 'working-tree'}",
        f"Context commit: {commit or '-'}",
        "",
        "Repository tree sample:",
    ]
    lines.extend(f"- {path}" for path in tree_files)
    lines.extend(["", "Relevant local file contents:"])

    included: list[str] = []
    budget = max_chars - len("\n".join(lines))
    truncated = len(files) >= selection_limit

    for relative in selected:
        if budget <= 0:
            truncated = True
            break
        absolute = repo / relative
        file_limit = resource_context_file_limit(relative, max_file_chars)
        text = _read_git_text(repo, resolved_ref, relative, file_limit) if resolved_ref else _read_text(absolute, file_limit)
        if not text:
            continue
        if is_optimizable_web_resource(relative):
            text = (
                "[Web resource context optimized]\n"
                "This file is a static/style/company resource. Only a bounded excerpt is included in the LLM context; "
                "review the full diff in the Markdown report when visual/style validation is required.\n\n"
                f"{text}"
            )
        block = f"\n--- file: {relative} ---\n{text}\n"
        if len(block) > budget:
            block = block[:budget] + "\n[Project context truncated]\n"
            truncated = True
        lines.append(block)
        included.append(relative)
        budget -= len(block)

    return {
        "text": "\n".join(lines)[:max_chars],
        "files_count": len(files),
        "included_files": included,
        "truncated": truncated,
        "ref": resolved_ref or "working-tree",
        "commit": commit,
    }


def _list_files(repo: Path, limit: int) -> list[str]:
    result: list[str] = []
    for root, dirnames, filenames in os.walk(repo):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".gradle")]
        base = Path(root)
        for filename in sorted(filenames):
            absolute = base / filename
            relative = absolute.relative_to(repo).as_posix()
            result.append(relative)
            if len(result) >= limit:
                return result
    return result


def _select_context_files(repo: Path, files: list[str], changed_paths: list[str], git_ref: str = "") -> list[str]:
    normalized_files = {path.replace("\\", "/"): path for path in files}
    selected: list[str] = []

    for changed in changed_paths:
        normalized = changed.replace("\\", "/")
        if normalized in normalized_files:
            selected.append(normalized_files[normalized])
        elif not git_ref and (repo / normalized).is_file():
            selected.append(normalized)

    for path in _dependency_context_files(repo, normalized_files, selected, git_ref):
        selected.append(path)

    for path in files:
        if _is_context_descriptor(path):
            selected.append(path)

    changed_dirs = {str(Path(path.replace("\\", "/")).parent).replace("\\", "/") for path in changed_paths}
    sibling_limit = app_config_int(
        "local_context.project_context_max_sibling_files",
        "PROJECT_CONTEXT_MAX_SIBLING_FILES",
        2,
    )
    sibling_counts: dict[str, int] = {}
    for path in files:
        if path in selected:
            continue
        parent = str(Path(path).parent).replace("\\", "/")
        if (
            parent in changed_dirs
            and sibling_counts.get(parent, 0) < sibling_limit
            and _looks_context_text_path(path)
        ):
            selected.append(path)
            sibling_counts[parent] = sibling_counts.get(parent, 0) + 1

    deduped: list[str] = []
    seen: set[str] = set()
    for path in selected:
        if path not in seen and not _is_context_noise(path) and (
            is_optimizable_web_resource(path) or (_looks_text_path(path) if git_ref else _looks_text(repo / path))
        ):
            seen.add(path)
            deduped.append(path)
    return deduped


def _dependency_context_files(
    repo: Path,
    normalized_files: dict[str, str],
    changed_paths: list[str],
    git_ref: str,
) -> list[str]:
    maximum = app_config_int(
        "local_context.project_context_max_dependency_files",
        "PROJECT_CONTEXT_MAX_DEPENDENCY_FILES",
        6,
    )
    if maximum <= 0:
        return []

    names: set[str] = set()
    relative_imports: set[str] = set()
    for changed in changed_paths:
        normalized = changed.replace("\\", "/")
        text = _read_git_text(repo, git_ref, normalized, 8000) if git_ref else _read_text(repo / normalized, 8000)
        if not text:
            continue
        for value in PHP_SYMBOL_RE.findall(text):
            names.add(value.rsplit("\\", 1)[-1].lower())
        for value in JS_IMPORT_RE.findall(text):
            if value.startswith("."):
                relative_imports.add(value)

    candidates: list[str] = []
    for path in normalized_files:
        if _is_context_noise(path):
            continue
        stem = PurePosixPath(path).stem.lower()
        if stem in names:
            candidates.append(path)

    for changed in changed_paths:
        base = PurePosixPath(changed.replace("\\", "/")).parent
        for imported in relative_imports:
            target = (base / imported).as_posix()
            for suffix in ("", ".php", ".js", ".ts", ".tsx", ".jsx", ".vue"):
                candidate = normalized_files.get(target + suffix)
                if candidate:
                    candidates.append(candidate)

    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if path not in seen and path not in changed_paths:
            seen.add(path)
            result.append(path)
            if len(result) >= maximum:
                break
    return result


def _is_context_descriptor(path: str) -> bool:
    name = Path(path).name
    lower = name.lower()
    return name in KEY_FILES or lower.endswith((".info.yml", ".services.yml", ".routing.yml", ".permissions.yml", ".schema.yml"))


def _is_context_noise(path: str) -> bool:
    lower = Path(path).name.lower()
    return lower in CONTEXT_NOISE_FILE_NAMES or lower.endswith(CONTEXT_NOISE_SUFFIXES)


def _looks_context_text_path(path: str) -> bool:
    return not _is_context_noise(path) and _looks_text_path(path)


def _looks_text(path: Path) -> bool:
    return path.is_file() and (path.name in KEY_FILES or path.suffix.lower() in TEXT_EXTENSIONS)


def _looks_text_path(path: str) -> bool:
    value = Path(path)
    return value.name in KEY_FILES or value.suffix.lower() in TEXT_EXTENSIONS


def _resolve_git_ref(repo: Path, requested: str) -> tuple[str, str]:
    if not requested or not (repo / ".git").exists():
        return "", ""
    candidates = [f"refs/remotes/origin/{requested}", requested]
    for candidate in candidates:
        completed = _git(repo, ["rev-parse", "--verify", candidate])
        if completed.returncode == 0:
            return candidate, completed.stdout.strip()
    return "", ""


def _list_git_files(repo: Path, ref: str, limit: int) -> list[str]:
    completed = _git(repo, ["ls-tree", "-r", "--name-only", ref], timeout=60)
    if completed.returncode != 0:
        return []
    result: list[str] = []
    for value in completed.stdout.splitlines():
        path = value.strip().replace("\\", "/")
        if not path or any(part in SKIP_DIRS for part in Path(path).parts):
            continue
        result.append(path)
        if len(result) >= limit:
            break
    return result


def _read_git_text(repo: Path, ref: str, relative: str, max_chars: int) -> str:
    completed = _git(repo, ["show", f"{ref}:{relative}"], timeout=30)
    if completed.returncode != 0 or "\x00" in completed.stdout[:1000]:
        return ""
    text = completed.stdout
    if len(text) > max_chars:
        return text[:max_chars] + "\n[File truncated]"
    return text


def _git(repo: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run_utf8(["git", "-C", str(repo), *args], timeout=timeout)


def _read_text(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if "\x00" in text[:1000]:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "\n[File truncated]"
    return text
