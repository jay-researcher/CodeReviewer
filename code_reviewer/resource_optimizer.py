from __future__ import annotations

import re
from pathlib import PurePosixPath

from .config import app_config_bool, app_config_int
from .models import ChangedFile


WEB_RESOURCE_EXTENSIONS = {
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
}

WEB_RESOURCE_NAME_PATTERNS = (
    re.compile(r"(^|[._-])company([._-]|$)", re.I),
    re.compile(r"\.min\.(?:css|js)$", re.I),
    re.compile(r"\.(?:bundle|chunk)\.(?:css|js)$", re.I),
)

WEB_RESOURCE_DIRS = {
    "assets",
    "asset",
    "static",
    "public",
    "images",
    "image",
    "fonts",
    "font",
    "style",
    "styles",
    "css",
}

BUILD_RESOURCE_CONFIG_DIRS = {"company", "release"}
BUILD_RESOURCE_CONFIG_NAMES = {
    "state_config.yml",
    "state_cofig.yml",
    "db_change.yml",
}


def is_optimizable_web_resource(path: str) -> bool:
    normalized = _normalize_path(path)
    if not normalized:
        return False
    if is_optimizable_build_resource(normalized):
        return True
    pure_path = PurePosixPath(normalized)
    suffix = pure_path.suffix.lower()
    name = pure_path.name
    if suffix in WEB_RESOURCE_EXTENSIONS:
        return True
    if any(pattern.search(name) for pattern in WEB_RESOURCE_NAME_PATTERNS):
        return True
    parts = {part.lower() for part in pure_path.parts}
    return bool(parts & WEB_RESOURCE_DIRS) and suffix in {".js", ".ts", ".tsx", ".jsx", ".json", ".html", ".svg"}


def is_optimizable_build_resource(path: str) -> bool:
    """Return true for repetitive company/environment build configuration resources.

    Build repositories deliberately repeat the same client/state configuration
    for several companies. Those files are release-critical, but sending every
    full copy to the LLM is low signal and easily exhausts the prompt budget.
    """
    normalized = _normalize_path(path).lower()
    pure_path = PurePosixPath(normalized)
    name = pure_path.name
    parts = [part.lower() for part in pure_path.parts]
    if name in BUILD_RESOURCE_CONFIG_NAMES or name.startswith(("state_config.", "state_cofig.")):
        return True
    if pure_path.suffix.lower() not in {".yml", ".yaml", ".json"}:
        return False
    if "config" not in parts:
        return False
    return bool(set(parts) & BUILD_RESOURCE_CONFIG_DIRS) or "locked_build" in parts


def resource_context_file_limit(path: str, default_limit: int) -> int:
    if not app_config_bool("local_context.optimize_web_resources", "LOCAL_CONTEXT_OPTIMIZE_WEB_RESOURCES", True):
        return default_limit
    if not is_optimizable_web_resource(path):
        return default_limit
    configured = app_config_int(
        "local_context.resource_context_file_max_chars",
        "LOCAL_CONTEXT_RESOURCE_FILE_MAX_CHARS",
        1200,
    )
    return max(200, min(default_limit, configured))


def optimize_prompt_diff(changed_files: list[ChangedFile], raw_diff: str, max_chars: int) -> tuple[str, dict[str, object]]:
    if not app_config_bool("llm.optimize_web_resources", "LLM_OPTIMIZE_WEB_RESOURCES", True):
        return _trim_total(raw_diff, max_chars), {"enabled": False, "optimized_files": []}
    if max_chars <= 0:
        return raw_diff, {"enabled": True, "optimized_files": []}
    if not changed_files:
        return _trim_total(raw_diff, max_chars), {"enabled": True, "optimized_files": []}

    resource_file_limit = app_config_int("llm.resource_diff_max_chars", "LLM_RESOURCE_DIFF_MAX_CHARS", 4000)
    resource_total_limit = app_config_int("llm.resource_diff_total_chars", "LLM_RESOURCE_DIFF_TOTAL_CHARS", 16000)
    line_limit = app_config_int("llm.resource_diff_added_line_limit", "LLM_RESOURCE_DIFF_ADDED_LINE_LIMIT", 80)
    resource_file_limit = max(800, resource_file_limit)
    resource_total_limit = max(0, resource_total_limit)
    line_limit = max(10, line_limit)

    logic_files = [item for item in changed_files if not is_optimizable_web_resource(item.path)]
    resource_files = [item for item in changed_files if is_optimizable_web_resource(item.path)]

    optimized_files: list[dict[str, object]] = []
    parts: list[str] = []
    current_chars = 0
    resource_chars = 0

    for item in [*logic_files, *resource_files]:
        is_resource = is_optimizable_web_resource(item.path)
        part = _render_changed_file_diff(item)
        if is_resource:
            remaining_resource = max(0, resource_total_limit - resource_chars)
            if remaining_resource <= 0:
                part = _resource_omitted_summary(item, "resource diff total budget exhausted")
            else:
                optimized = _optimized_resource_diff(item, min(resource_file_limit, remaining_resource), line_limit)
                part = optimized["diff"]
                optimized_files.append(
                    {
                        "path": item.path,
                        "original_chars": len(_render_changed_file_diff(item)),
                        "final_chars": len(part),
                        "reason": optimized["reason"],
                    }
                )
            resource_chars += len(part)
        if current_chars + len(part) > max_chars:
            remaining = max_chars - current_chars
            if remaining <= 200:
                parts.append("\n[Diff truncated by LLM_MAX_DIFF_CHARS]\n")
                break
            parts.append(part[:remaining] + "\n[Diff truncated by LLM_MAX_DIFF_CHARS]\n")
            current_chars = max_chars
            break
        parts.append(part)
        current_chars += len(part)

    diff = "\n\n".join(parts).strip()
    diagnostics = {
        "enabled": True,
        "max_chars": max_chars,
        "resource_diff_max_chars": resource_file_limit,
        "resource_diff_total_chars": resource_total_limit,
        "optimized_files": optimized_files,
        "resource_file_count": len(resource_files),
        "logic_file_count": len(logic_files),
        "final_chars": len(diff),
        "original_chars": len(raw_diff),
    }
    return diff, diagnostics


def _optimized_resource_diff(changed_file: ChangedFile, max_chars: int, line_limit: int) -> dict[str, str]:
    rendered = _render_changed_file_diff(changed_file)
    if len(rendered) <= max_chars:
        return {"diff": rendered, "reason": "under resource budget"}

    interesting_lines = _interesting_diff_lines(changed_file.diff, line_limit)
    summary_lines = [
        _resource_summary_header(changed_file),
        "Resource diff optimized for LLM context; full diff remains available in the Markdown report.",
        f"Original diff chars: {len(rendered)}; prompt resource limit: {max_chars}.",
        "",
        "Representative changed lines:",
        *(interesting_lines or ["[No representative text lines extracted from this resource diff.]"]),
    ]
    summarized = "\n".join(summary_lines)
    if len(summarized) > max_chars:
        summarized = summarized[:max_chars] + "\n[Resource diff summary truncated]\n"
    return {"diff": summarized, "reason": "large web resource summarized"}


def _resource_omitted_summary(changed_file: ChangedFile, reason: str) -> str:
    return "\n".join(
        [
            _resource_summary_header(changed_file),
            f"Resource diff omitted from LLM prompt: {reason}.",
            "Full diff remains available in the Markdown report.",
        ]
    )


def _resource_summary_header(changed_file: ChangedFile) -> str:
    path = _normalize_path(changed_file.path)
    label = "Build resource summary" if is_optimizable_build_resource(path) else "Web resource summary"
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"[{label}] {path}: +{changed_file.additions}/-{changed_file.deletions}",
        ]
    )


def _interesting_diff_lines(diff: str, limit: int) -> list[str]:
    results: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index ")):
            continue
        if line.startswith("@@"):
            results.append(line)
            continue
        if not line.startswith(("+", "-")):
            continue
        text = line.strip()
        if len(text) > 240:
            text = text[:240] + " ..."
        results.append(text)
        if len(results) >= limit:
            break
    return results


def _render_changed_file_diff(changed_file: ChangedFile) -> str:
    path = _normalize_path(changed_file.path)
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            changed_file.diff,
        ]
    )


def _trim_total(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[Diff truncated by LLM_MAX_DIFF_CHARS]"


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")
