from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ProjectConfig:
    key: str
    display_name: str
    dev_config_file: str = ""
    build_config_file: str = ""
    repository_urls: list[str] = field(default_factory=list)
    default_branch: str = ""
    version: str = ""


@dataclass(slots=True)
class ChangedFile:
    path: str
    additions: int = 0
    deletions: int = 0
    diff: str = ""


@dataclass(slots=True)
class ReviewInput:
    project: str = ""
    mr_url: str = ""
    mr_id: str = ""
    jira_key: str = ""
    sprint: str = ""
    source_branch: str = ""
    target_branch: str = ""
    commit: str = ""
    title: str = ""
    author: str = ""
    changed_files: list[ChangedFile] = field(default_factory=list)
    raw_diff: str = ""
    generated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Finding:
    severity: str
    file_path: str
    line: int | None
    title: str
    detail: str
    recommendation: str
    category: str = "General"


@dataclass(slots=True)
class ReviewResult:
    review_input: ReviewInput
    findings: list[Finding]
    conclusion: str
    risk_summary: list[str]
    test_suggestions: list[str]

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts
