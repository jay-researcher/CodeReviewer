from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, HISTORY_FILE, app_config_int, gitnexus_config
from .models import ReviewResult


def append_review_history(result: ReviewResult, report_path: Path, writebacks: list[dict[str, Any]] | None = None) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    entry = review_history_entry(result, report_path, writebacks or [])
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_review_history(limit: int = 100) -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines:
        payload = line.strip().lstrip("\ufeff")
        if not payload:
            continue
        try:
            entry = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return list(reversed(entries[-limit:]))


def save_to_gitnexus(result: ReviewResult, report_path: Path) -> dict[str, str]:
    config = gitnexus_config()
    storage_dir = Path(config["storage_path"])
    reports_dir = storage_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    stored_report = reports_dir / report_path.name
    if report_path.resolve() != stored_report.resolve():
        shutil.copyfile(report_path, stored_report)

    entry = review_history_entry(result, stored_report, [])
    index_file = storage_dir / config["index_file"]
    with index_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    metadata_file = stored_report.with_suffix(".metadata.json")
    metadata_file.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "storage": "gitnexus-file",
        "report_path": str(stored_report),
        "metadata_path": str(metadata_file),
        "index_path": str(index_file),
    }


def review_history_entry(result: ReviewResult, report_path: Path, writebacks: list[dict[str, Any]]) -> dict[str, Any]:
    source = result.review_input
    return {
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "report_path": str(report_path),
        "project": source.project,
        "mr_url": source.mr_url,
        "mr_id": source.mr_id,
        "jira_key": source.jira_key,
        "sprint": source.sprint,
        "source_branch": source.source_branch,
        "target_branch": source.target_branch,
        "commit": source.commit,
        "conclusion": result.conclusion,
        "severity_counts": result.severity_counts,
        "finding_count": len(result.findings),
        "metadata": _compact_metadata(source.metadata),
        "writebacks": writebacks,
        "changed_files": _compact_changed_files(source.changed_files),
    }


def _compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact = dict(metadata)
    if "project_context" in compact:
        compact["project_context"] = "[omitted from persisted metadata]"
    if "jira_prd_context" in compact:
        compact["jira_prd_context"] = "[omitted from persisted metadata]"
    if "git_version_review_context" in compact:
        compact["git_version_review_context"] = "[omitted from persisted metadata]"
    if "source_repository_diff_context" in compact:
        compact["source_repository_diff_context"] = "[omitted from persisted metadata]"
    return compact


def _compact_changed_files(changed_files: list[Any]) -> list[dict[str, Any]]:
    per_file_limit = max(0, app_config_int("report.history_diff_file_max_chars", "REPORT_HISTORY_DIFF_FILE_MAX_CHARS", 20000))
    remaining = max(0, app_config_int("report.history_diff_total_chars", "REPORT_HISTORY_DIFF_TOTAL_CHARS", 1000000))
    compact: list[dict[str, Any]] = []
    for item in changed_files:
        payload = asdict(item)
        diff = str(payload.get("diff") or "")
        limit = min(per_file_limit, remaining)
        if len(diff) > limit:
            payload["diff"] = diff[:limit]
            payload["diff_truncated"] = True
            payload["diff_original_chars"] = len(diff)
        remaining = max(0, remaining - len(str(payload.get("diff") or "")))
        compact.append(payload)
    return compact
