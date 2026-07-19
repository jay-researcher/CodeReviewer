from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


UNMAPPED_APPLICATION = "Unmapped"
UNMAPPED_RELEASE_LINE = "Unmapped release line"


@dataclass(frozen=True)
class ReviewScope:
    """Stable business boundary used to split one Jira review into reports."""

    application: str
    release_line: str
    isolation_key: str = ""

    @property
    def filename_component(self) -> str:
        if self.application == "WVAdmin" and self.release_line != UNMAPPED_RELEASE_LINE:
            return "WVAdmin"
        if self.application == "Services Terminal" and self.release_line != UNMAPPED_RELEASE_LINE:
            return "Services-Terminal"
        if self.application == "iTrade Client" and self.release_line != UNMAPPED_RELEASE_LINE:
            return f"iTrade-Client-{self.release_line}"
        if self.application == "DPS" and self.release_line in {"DPS9", "DPS11"}:
            return self.release_line

        application = _safe_component(self.application)
        release_line = _safe_component(self.release_line)
        isolation = _safe_component(self.isolation_key)
        return "-".join(part for part in (application, release_line, isolation) if part)


def group_merge_requests_by_review_scope(
    related_mrs: list[object],
) -> dict[ReviewScope, list[dict[str, object]]]:
    groups: dict[ReviewScope, list[dict[str, object]]] = {}
    for item in related_mrs:
        if not isinstance(item, dict):
            continue
        scope = review_scope_for_merge_request(item)
        groups.setdefault(scope, []).append(item)
    return groups


def review_scope_for_merge_request(item: Mapping[str, object]) -> ReviewScope:
    identity = _identity_text(item)
    application = _application(item.get("application"), identity)
    release_line = _release_line(item, application, identity)
    isolation_key = ""
    if application == UNMAPPED_APPLICATION or release_line == UNMAPPED_RELEASE_LINE:
        # Never merge an unknown project/version with another unknown scope. A later
        # configuration fix can then reclassify it without contaminating a report.
        isolation_key = _isolation_key(item)
    return ReviewScope(application, release_line, isolation_key)


def review_scope_filename_component(item: Mapping[str, object]) -> str:
    return review_scope_for_merge_request(item).filename_component


def _application(value: object, identity: str) -> str:
    explicit = str(value or "").strip()
    normalized = re.sub(r"[^a-z0-9]+", "", explicit.lower())
    aliases = {
        "itrade": "iTrade Client",
        "itradeclient": "iTrade Client",
        "wvadmin": "WVAdmin",
        "serviceterminal": "Services Terminal",
        "servicesterminal": "Services Terminal",
        "dps": "DPS",
        "dps9": "DPS",
        "dps11": "DPS",
        "unmapped": UNMAPPED_APPLICATION,
    }
    if normalized in aliases:
        return aliases[normalized]

    # Services Terminal is nested under the legacy `itrade-client` config node,
    # so its more specific identity must win over the iTrade token.
    if re.search(r"\bservices?-terminal\b", identity):
        return "Services Terminal"
    if re.search(r"\bwvadmin\b", identity):
        return "WVAdmin"
    if re.search(r"\bdps(?:9|11)?\b", identity):
        return "DPS"
    if re.search(r"\bitrade-client\b", identity):
        return "iTrade Client"
    return UNMAPPED_APPLICATION


def _release_line(
    item: Mapping[str, object],
    application: str,
    identity: str,
) -> str:
    explicit = str(item.get("release_line") or "").strip()
    candidates = [
        explicit,
        str(item.get("application") or ""),
        str(item.get("target_branch") or ""),
        str(item.get("source_branch") or ""),
        str(item.get("branch") or ""),
        identity,
    ]
    if application == "iTrade Client":
        return _first_matching_release(
            candidates,
            (
                ("7.5.0", r"(?<!\d)7[._-]5[._-]0(?!\d)"),
                ("7.5.1", r"(?<!\d)7[._-]5[._-]1(?!\d)"),
            ),
        )
    if application == "DPS":
        return _first_matching_release(
            candidates,
            (
                ("DPS9", r"(?<![a-z0-9])dps[._-]?9(?!\d)|(?<!\d)9[._-]3(?!\d)"),
                ("DPS11", r"(?<![a-z0-9])dps[._-]?11(?!\d)|(?<!\d)11[._-]2(?!\d)"),
            ),
        )
    if application == "WVAdmin":
        return _normalized_single_release(explicit, "1.0")
    if application == "Services Terminal":
        return _normalized_single_release(explicit, "5.0")
    return _normalized_explicit_release(explicit) or UNMAPPED_RELEASE_LINE


def _first_matching_release(
    values: list[str],
    patterns: tuple[tuple[str, str], ...],
) -> str:
    for value in values:
        text = value.lower()
        for label, pattern in patterns:
            if re.search(pattern, text):
                return label
    return UNMAPPED_RELEASE_LINE


def _normalized_single_release(explicit: str, default: str) -> str:
    normalized = _normalized_explicit_release(explicit)
    return normalized if normalized and normalized != UNMAPPED_RELEASE_LINE else default


def _normalized_explicit_release(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in {"unmapped", "unmapped release line", "unknown"}:
        return UNMAPPED_RELEASE_LINE
    return re.sub(r"(?:\.\*|\.x)$", "", text, flags=re.I)


def _identity_text(item: Mapping[str, object]) -> str:
    values = [
        item.get("project_path"),
        item.get("project"),
        item.get("project_name"),
        item.get("git_tools_group"),
        item.get("git_tools_module"),
        item.get("file_prefix"),
        item.get("mr_url"),
    ]
    return " ".join(str(value or "").replace("\\", "/").lower() for value in values)


def _isolation_key(item: Mapping[str, object]) -> str:
    project = str(
        item.get("project_path")
        or item.get("project")
        or item.get("git_tools_module")
        or item.get("project_name")
        or "project"
    ).replace("\\", "/").strip("/")
    project_leaf = project.rsplit("/", 1)[-1]
    mr_id = str(item.get("mr_id") or "").strip()
    return "-".join(part for part in (_safe_component(project_leaf), _safe_component(mr_id)) if part) or "scope"


def _safe_component(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.+-]+", "-", str(value or "").strip())
    return text.strip("._-")
