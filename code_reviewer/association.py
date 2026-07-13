from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
SVREQ_REF_RE = re.compile(r"#\s*(SVREQ-\d+)\b", re.I)
MESSAGE_LINK_RE = re.compile(
    r"(?P<summary>[^\n\r]+?)\s+(?P<link>https?://[^\s)]+/(?:browse|issues?)/(?P<key>[A-Z][A-Z0-9]+-\d+))",
    re.I,
)


@dataclass(slots=True)
class IssueAssociation:
    action_issue: str = ""
    svreq_issue: str = ""
    issue_keys: list[str] = field(default_factory=list)
    issue_links: list[dict[str, str]] = field(default_factory=list)
    source: str = ""


def parse_issue_association(
    text: str,
    explicit_action_issue: str = "",
    blocked_issue_keys: list[str] | None = None,
) -> IssueAssociation:
    keys = _unique(JIRA_KEY_RE.findall(text or ""))
    blocked = _unique(blocked_issue_keys or [])
    svreq_from_summary = ""
    match = SVREQ_REF_RE.search(text or "")
    if match:
        svreq_from_summary = match.group(1).upper()

    svreq_issue = _first_svreq(blocked) or svreq_from_summary or _first_svreq(keys)
    action_issue = explicit_action_issue or _first_non_svreq(keys)

    source_parts = []
    if svreq_from_summary:
        source_parts.append("summary-hash-reference")
    if blocked:
        source_parts.append("blocked-relationship")
    if not source_parts and keys:
        source_parts.append("jira-key-detection")

    return IssueAssociation(
        action_issue=action_issue,
        svreq_issue=svreq_issue,
        issue_keys=_unique([*keys, *blocked, explicit_action_issue, svreq_issue]),
        issue_links=parse_gitlab_message_summary_links(text),
        source=",".join(source_parts) or "none",
    )


def parse_gitlab_message_summary_links(text: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for match in MESSAGE_LINK_RE.finditer(text or ""):
        links.append(
            {
                "key": match.group("key").upper(),
                "summary": match.group("summary").strip(" -:\t"),
                "url": match.group("link"),
            }
        )
    return links


def association_to_metadata(association: IssueAssociation) -> dict[str, Any]:
    return {
        "action_issue": association.action_issue,
        "svreq_issue": association.svreq_issue,
        "issue_keys": association.issue_keys,
        "issue_links": association.issue_links,
        "association_source": association.source,
    }


def _first_svreq(keys: list[str]) -> str:
    return next((key.upper() for key in keys if key.upper().startswith("SVREQ-")), "")


def _first_non_svreq(keys: list[str]) -> str:
    return next((key.upper() for key in keys if not key.upper().startswith("SVREQ-")), "")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = (value or "").upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
