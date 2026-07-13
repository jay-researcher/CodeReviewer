from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Any

from .association import JIRA_KEY_RE
from .config import app_config_bool, app_config_int, app_config_str
from .models import ReviewInput


DEFAULT_JIRA_PRD_DATA_DIR = r"D:\TTL\jira-prd\data"


def attach_jira_prd_context(review_input: ReviewInput) -> None:
    mode = app_config_str("jira_prd.context", "JIRA_PRD_CONTEXT", "auto").strip().lower()
    if mode in {"", "none", "off", "0", "false", "no"}:
        return

    issue_keys = _collect_issue_keys(review_input)
    if mode == "auto" and not issue_keys:
        return

    data_dir = Path(os.getenv("JIRA_PRD_DATA_DIR", DEFAULT_JIRA_PRD_DATA_DIR))
    if not data_dir.exists() or not data_dir.is_dir():
        fetched = _fetch_missing_issue_docs(issue_keys, data_dir) if _auto_fetch_enabled() else []
        if fetched:
            review_input.metadata["jira_prd_context_auto_fetched"] = fetched
        if not data_dir.exists() or not data_dir.is_dir():
            review_input.metadata["jira_prd_context_error"] = f"Jira PRD data directory not found: {data_dir}"
            return

    context = build_jira_prd_context(
        data_dir=data_dir,
        issue_keys=issue_keys,
        max_chars=app_config_int("jira_prd.context_max_chars", "JIRA_PRD_CONTEXT_MAX_CHARS", 20000),
        per_issue_chars=app_config_int("jira_prd.context_per_issue_chars", "JIRA_PRD_CONTEXT_PER_ISSUE_CHARS", 6000),
        max_issues=app_config_int("jira_prd.context_max_issues", "JIRA_PRD_CONTEXT_MAX_ISSUES", 8),
    )
    if context["missing_issue_keys"] and _auto_fetch_enabled():
        fetched = _fetch_missing_issue_docs(context["missing_issue_keys"], data_dir)
        if fetched:
            _issue_file_index.cache_clear()
            context = build_jira_prd_context(
                data_dir=data_dir,
                issue_keys=issue_keys,
                max_chars=app_config_int("jira_prd.context_max_chars", "JIRA_PRD_CONTEXT_MAX_CHARS", 20000),
                per_issue_chars=app_config_int("jira_prd.context_per_issue_chars", "JIRA_PRD_CONTEXT_PER_ISSUE_CHARS", 6000),
                max_issues=app_config_int("jira_prd.context_max_issues", "JIRA_PRD_CONTEXT_MAX_ISSUES", 8),
            )
            review_input.metadata["jira_prd_context_auto_fetched"] = fetched
    if not context["text"]:
        return

    review_input.metadata["jira_prd_context"] = context["text"]
    review_input.metadata["jira_prd_context_dir"] = str(data_dir)
    review_input.metadata["jira_prd_context_issue_keys"] = context["issue_keys"]
    review_input.metadata["jira_prd_context_files"] = context["files"]
    review_input.metadata["jira_prd_context_missing_issue_keys"] = context["missing_issue_keys"]
    review_input.metadata["jira_prd_context_truncated"] = context["truncated"]


def _auto_fetch_enabled() -> bool:
    return app_config_bool("jira_prd.auto_fetch", "JIRA_PRD_AUTO_FETCH", True)


def _fetch_missing_issue_docs(issue_keys: list[str], data_dir: Path) -> list[str]:
    script = Path(os.getenv("JIRA_PRD_FETCH_SCRIPT", r"D:\TTL\jira-prd\fetch_jira.py"))
    if not script.exists():
        return []
    depth = app_config_str("jira_prd.fetch_depth", "JIRA_PRD_FETCH_DEPTH", "2").strip() or "2"
    timeout = app_config_int("jira_prd.fetch_timeout_seconds", "JIRA_PRD_FETCH_TIMEOUT_SECONDS", 240)
    fetched: list[str] = []
    for key in _unique([item for item in issue_keys if item]):
        if _issue_doc_exists(data_dir, key):
            fetched.append(key)
            continue
        command = [sys.executable, str(script), key, "--depth", depth, "--quiet"]
        try:
            completed = subprocess.run(
                command,
                cwd=str(script.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
            )
        except Exception:
            completed = None
        if completed is not None and completed.returncode == 0 and _issue_doc_exists(data_dir, key):
            fetched.append(key)
            continue
        if _fetch_single_issue_doc(key, data_dir, script, depth):
            fetched.append(key)
    return fetched


def _issue_doc_exists(data_dir: Path, key: str) -> bool:
    normalized = (key or "").upper()
    if not normalized or not data_dir.exists():
        return False
    for suffix in (".md", ".json"):
        try:
            for path in data_dir.rglob(f"*{suffix}"):
                if path.is_file() and path.stem.upper() == normalized:
                    return True
        except OSError:
            return False
    return False


def _fetch_single_issue_doc(key: str, data_dir: Path, script: Path, depth: str) -> bool:
    try:
        module = _load_fetch_jira_module(script)
        env = module.load_env()
        client = module.JiraClient(
            env.get("JIRA_URL", "https://tx-tech.atlassian.net/"),
            env.get("JIRA_USERNAME", ""),
            env.get("JIRA_TOKEN", ""),
        )
        issue = client.fetch_issue(key)
        comments: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        if int(depth or "2") >= 2:
            try:
                comments = client.fetch_comments(key)
            except Exception:
                comments = []
            try:
                attachments = client.fetch_attachments(key)
            except Exception:
                attachments = []
        output_dir = data_dir / "jira-epics"
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {"issue": issue, "comments": comments, "attachments": attachments}
        json_path = output_dir / f"{key}.json"
        md_path = output_dir / f"{key}.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(_build_single_issue_md(module, key, issue, comments, attachments), encoding="utf-8")
        _update_single_issue_index(data_dir, key, issue, json_path, md_path)
        return _issue_doc_exists(data_dir, key)
    except Exception:
        return False


def _load_fetch_jira_module(script: Path) -> Any:
    spec = importlib.util.spec_from_file_location("code_reviewer_fetch_jira", str(script))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Jira fetch script: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_single_issue_md(module: Any, key: str, issue: dict[str, Any], comments: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> str:
    fields = issue.get("fields") or {}
    rendered = issue.get("renderedFields") or {}
    title = fields.get("summary") or key
    issue_type = module.fmt_field(fields.get("issuetype"))
    status = module.fmt_field(fields.get("status"))
    priority = module.fmt_field(fields.get("priority"))
    assignee = module.fmt_field(fields.get("assignee"))
    reporter = module.fmt_field(fields.get("reporter"))
    created = fields.get("created", "")
    updated = fields.get("updated", "")
    description = module.fmt_description(rendered.get("description") or "") or module.fmt_description(str(fields.get("description") or ""))
    lines = [
        f"# {key}: {title}",
        "",
        f"> **Type**: {issue_type} | **Status**: {status} | **Priority**: {priority}",
        f"> **Assignee**: {assignee} | **Reporter**: {reporter}",
        f"> **Created**: {created} | **Updated**: {updated}",
        "",
        "## Description",
        "",
        description or "_No description available._",
        "",
    ]
    if comments:
        lines.extend([f"## Comments ({len(comments)})", ""])
        for comment in comments:
            author = module.fmt_field(comment.get("author", {}), default="unknown")
            created_at = comment.get("created", "")
            body = comment.get("body", "")
            plain = module.fmt_description(body) if "<" in str(body) else str(body)
            lines.extend([f"### Comment by {author} ({created_at})", "", plain.strip(), ""])
    if attachments:
        lines.extend([f"## Attachments ({len(attachments)})", ""])
        for attachment in attachments:
            filename = attachment.get("filename", "unknown")
            size = attachment.get("size", "?")
            url = attachment.get("downloadUrl", "")
            lines.append(f"- [{filename}]({url}) ({size} bytes)" if url else f"- {filename} ({size} bytes)")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _update_single_issue_index(data_dir: Path, key: str, issue: dict[str, Any], json_path: Path, md_path: Path) -> None:
    index_path = data_dir / "jira-index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8", errors="ignore")) if index_path.exists() else {}
    except Exception:
        payload = {}
    entries = payload.setdefault("issues", [])
    if not isinstance(entries, list):
        entries = []
        payload["issues"] = entries
    fields = issue.get("fields") or {}
    entries[:] = [item for item in entries if not (isinstance(item, dict) and str(item.get("key") or "").upper() == key.upper())]
    entries.append(
        {
            "key": key,
            "title": fields.get("summary", ""),
            "status": str(fields.get("status", {}).get("name", "") if isinstance(fields.get("status"), dict) else fields.get("status") or ""),
            "file": str(json_path),
            "md_file": str(md_path),
        }
    )
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_jira_prd_context(
    data_dir: Path,
    issue_keys: list[str],
    max_chars: int = 20000,
    per_issue_chars: int = 6000,
    max_issues: int = 8,
) -> dict[str, Any]:
    index = _issue_file_index(str(data_dir))
    queue = _unique(issue_keys)
    selected: list[str] = []
    missing: list[str] = []
    files: list[str] = []
    parts: list[str] = []
    visited: set[str] = set()
    budget = max(max_chars, 0)
    truncated = False

    while queue and len(selected) < max(max_issues, 1) and budget > 0:
        key = queue.pop(0).upper()
        if key in visited:
            continue
        visited.add(key)
        entry = index.get(key)
        if not entry:
            missing.append(key)
            continue

        block, block_files = _issue_block(key, entry, data_dir, per_issue_chars)
        if not block:
            missing.append(key)
            continue

        selected.append(key)
        files.extend(block_files)
        if len(block) > budget:
            parts.append(block[:budget] + "\n[Jira PRD context truncated]\n")
            budget = 0
            truncated = True
            break
        parts.append(block)
        budget -= len(block)

        for linked_key in _extract_issue_keys(block):
            if linked_key not in visited and linked_key not in queue and linked_key in index:
                queue.append(linked_key)

    if queue and len(selected) >= max(max_issues, 1):
        truncated = True

    header = [
        "Local Jira/PRD context from jira-prd data.",
        "Use this as requirement intent and traceability context. If this conflicts with the diff or live Jira/GitLab data, call out the uncertainty instead of inventing facts.",
    ]
    if missing:
        header.append(f"Missing local issue docs: {', '.join(missing)}")
    text = "\n".join(header + ["", *parts]).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[Jira PRD context truncated]\n"
        truncated = True

    return {
        "text": text,
        "issue_keys": selected,
        "files": _unique_preserve(files),
        "missing_issue_keys": missing,
        "truncated": truncated,
    }


def _collect_issue_keys(review_input: ReviewInput) -> list[str]:
    values: list[str] = [
        review_input.jira_key,
        review_input.sprint,
        review_input.source_branch,
        review_input.target_branch,
        review_input.title,
    ]
    metadata = review_input.metadata or {}
    for key in ("action_issue", "svreq_issue"):
        values.append(str(metadata.get(key) or ""))
    issue_keys = metadata.get("issue_keys")
    if isinstance(issue_keys, list):
        values.extend(str(item) for item in issue_keys)
    issue_links = metadata.get("issue_links")
    if isinstance(issue_links, list):
        for item in issue_links:
            if isinstance(item, dict):
                values.append(str(item.get("key") or ""))
                values.append(str(item.get("summary") or ""))
    values.append(review_input.raw_diff[: int(os.getenv("JIRA_PRD_SCAN_DIFF_CHARS", "20000"))])
    return _unique(_extract_issue_keys("\n".join(values)))


@lru_cache(maxsize=8)
def _issue_file_index(data_dir: str) -> dict[str, dict[str, str]]:
    root = Path(data_dir)
    index: dict[str, dict[str, str]] = {}

    index_file = root / "jira-index.json"
    if index_file.exists():
        try:
            payload = json.loads(index_file.read_text(encoding="utf-8", errors="ignore"))
            _collect_index_entries(payload, index)
        except Exception:
            pass

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".json"}:
            continue
        key = path.stem.upper()
        if not JIRA_KEY_RE.fullmatch(key):
            continue
        entry = index.setdefault(key, {})
        if path.suffix.lower() == ".md":
            entry.setdefault("md_file", str(path))
        elif path.name.lower() != "jira-index.json":
            entry.setdefault("json_file", str(path))

    return index


def _collect_index_entries(value: Any, index: dict[str, dict[str, str]]) -> None:
    if isinstance(value, dict):
        key = str(value.get("key") or "").upper()
        if JIRA_KEY_RE.fullmatch(key):
            entry = index.setdefault(key, {})
            md_file = str(value.get("md_file") or "")
            json_file = str(value.get("file") or value.get("json_file") or "")
            if md_file:
                entry.setdefault("md_file", md_file)
            if json_file:
                entry.setdefault("json_file", json_file)
            child_md_files = value.get("child_md_files")
            if isinstance(child_md_files, list):
                entry.setdefault("child_md_files", "\n".join(str(item) for item in child_md_files if item))
        for item in value.values():
            _collect_index_entries(item, index)
    elif isinstance(value, list):
        for item in value:
            _collect_index_entries(item, index)


def _issue_block(key: str, entry: dict[str, str], data_dir: Path, per_issue_chars: int) -> tuple[str, list[str]]:
    files: list[str] = []
    md_file = _existing_path(entry.get("md_file", ""), data_dir)
    json_file = _existing_path(entry.get("json_file", ""), data_dir)

    if md_file:
        text = _read_text(md_file)
        files.append(str(md_file))
        block = f"## {key}\nSource: {md_file}\n\n{text.strip()}"
    elif json_file:
        text = _read_json_summary(json_file)
        files.append(str(json_file))
        block = f"## {key}\nSource: {json_file}\n\n{text.strip()}"
    else:
        return "", []

    child_files = [item for item in (entry.get("child_md_files") or "").splitlines() if item.strip()]
    if child_files and os.getenv("JIRA_PRD_INCLUDE_CHILDREN", "0").lower() in {"1", "true", "yes"}:
        child_parts: list[str] = []
        for child in child_files[: int(os.getenv("JIRA_PRD_CONTEXT_MAX_CHILDREN", "5"))]:
            child_path = _existing_path(child, data_dir)
            if child_path:
                files.append(str(child_path))
                child_parts.append(f"### Child: {child_path.name}\n{_read_text(child_path).strip()}")
        if child_parts:
            block = f"{block}\n\n" + "\n\n".join(child_parts)

    limit = max(per_issue_chars, 500)
    if len(block) > limit:
        block = block[:limit] + "\n[Issue context truncated]\n"
    return block, files


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_json_summary(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return _read_text(path)
    return json.dumps(payload, ensure_ascii=False, indent=2)[: int(os.getenv("JIRA_PRD_JSON_MAX_CHARS", "6000"))]


def _existing_path(value: str, data_dir: Path) -> Path | None:
    text = (value or "").strip().strip("'\"")
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = data_dir / path
    try:
        if path.exists() and path.is_file():
            return path
    except OSError:
        return None
    return None


def _extract_issue_keys(text: str) -> list[str]:
    return _unique(JIRA_KEY_RE.findall(text or ""))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = (value or "").upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value or ""
        normalized = text.lower()
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result
