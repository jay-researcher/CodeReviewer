from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any
from base64 import b64encode

from .config import app_config_int

JIRA_SEARCH_FIELDS = [
    "summary",
    "description",
    "assignee",
    "status",
    "issuetype",
    "labels",
    "components",
    "customfield_10036",
    "customfield_10005",
    "customfield_10006",
]

_DESCRIPTION_TEMPLATE_COLUMNS = (("截图", "说明", "补充"), ("screenshot", "description", "additional remarks"))
_DESCRIPTION_TEMPLATE_COMMON = (
    ("受影响的项目或功能范围", "涉及的文件清单"),
    ("affected project or functional scope", "involved file lists"),
)
_DESCRIPTION_TEMPLATE_REQUIREMENT = (
    ("需求描述", "需求分析", "解决方案", "预期结果"),
    ("requirement description", "requirement analysis", "proposed solution", "expected result"),
)
_DESCRIPTION_TEMPLATE_BUG = (
    ("问题描述", "问题分析", "解决方法"),
    ("bug description", "bug analysis", "workaround"),
)


@dataclass(slots=True)
class JiraIssue:
    """Represents a Jira issue with related branches."""
    key: str
    summary: str
    description: str
    assignee: str
    status: str
    sprint: str
    issue_type: str
    labels: list[str]
    epic_link: str | None = None
    id: str = ""
    description_comments: list[str] | None = None
    sprint_memberships: list["SprintMembership"] = field(default_factory=list)
    current_sprint_id: str = ""
    current_sprint_state: str = ""
    components: list[str] = field(default_factory=list)
    responsibles: list[str] = field(default_factory=list)

    @property
    def final_description(self) -> str:
        """Merge the original description with later formal description comments."""
        parts = [self.description.strip()]
        parts.extend(item.strip() for item in (self.description_comments or []) if item.strip())
        return "\n\n".join(part for part in parts if part)


@dataclass(slots=True)
class SprintMembership:
    """A normalized Jira Software Sprint membership retained for cycle history."""

    id: str = ""
    name: str = ""
    state: str = "unknown"
    start_date: str = ""
    end_date: str = ""
    complete_date: str = ""
    board_id: str = ""
    joined_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class IssueBranchMapping:
    """Maps a Jira issue to GitLab branches across projects."""
    jira_key: str
    project_slug: str  # e.g., "wvadmin", "dps11", "itrade-client"
    branch_name: str   # e.g., "feature/ECHNL-5552", "feature/DAO#ECHNL-5552"
    mr_iid: int | None = None  # GitLab MR IID if MR exists
    mr_url: str | None = None


class JiraClient:
    """Jira REST API v3 client."""

    def __init__(self, base_url: str = "", username: str = "", api_token: str = "") -> None:
        self.base_url = (base_url or os.getenv("JIRA_URL", "")).rstrip("/")
        self.username = username or os.getenv("JIRA_USERNAME", "")
        self.api_token = api_token or os.getenv("JIRA_TOKEN", "")
        self.warnings: list[dict[str, str]] = []

        if not all([self.base_url, self.username, self.api_token]):
            raise ValueError(
                "Jira configuration incomplete. Set JIRA_URL, JIRA_USERNAME, JIRA_TOKEN env vars."
            )

    def fetch_issue(self, issue_key: str) -> JiraIssue:
        """Fetch a Jira issue by key (e.g., ECHNL-5552)."""
        encoded_key = urllib.parse.quote(issue_key)
        try:
            response = self._request_json(f"/rest/api/3/issue/{encoded_key}?expand=changelog")
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            response = self._request_json(f"/rest/api/3/issues/{encoded_key}?expand=changelog")

        issue = _jira_issue_from_item(response)
        issue.description_comments = self.fetch_description_template_comments(issue.key, issue.issue_type)
        return issue

    def search_issues_by_sprint(
        self,
        sprint_name: str,
        project_key: str = "",
        progress: Any = None,
    ) -> list[JiraIssue]:
        """Search for issues in a sprint by sprint ID or sprint name."""
        sprint_value = (sprint_name or "").strip()
        jql_sprint = sprint_value if re.fullmatch(r"\d+", sprint_value) else f'"{sprint_value}"'
        jql = f"sprint = {jql_sprint}"
        if project_key:
            jql = f'project = {project_key} AND {jql}'

        max_issues = app_config_int("jira.sprint_max_issues", "JIRA_SPRINT_MAX_ISSUES", 500)
        return self.search_issues_by_jql(jql, max_issues=max_issues, progress=progress)

    def search_issues_by_filter_id(
        self,
        filter_id: str,
        max_issues: int | None = None,
        progress: Any = None,
    ) -> list[JiraIssue]:
        """Search for issues returned by a saved Jira filter ID."""
        value = (filter_id or "").strip()
        if not value:
            raise ValueError("Jira filter ID is required.")
        jql_filter = value if re.fullmatch(r"\d+", value) else f'"{value}"'
        max_results = max_issues if max_issues is not None else app_config_int("jira.filter_max_issues", "JIRA_FILTER_MAX_ISSUES", 500)
        return self.search_issues_by_jql(
            f"filter = {jql_filter}",
            max_issues=max_results,
            progress=progress,
        )

    def search_issues_by_jql(
        self,
        jql: str,
        max_issues: int,
        progress: Any = None,
    ) -> list[JiraIssue]:
        """Search Jira issues by JQL with Jira Cloud enhanced-search support."""
        search_api = os.getenv("JIRA_SEARCH_API", "auto").strip().lower()
        if search_api == "legacy":
            issues = self._search_issues_legacy(jql, max_issues)
        else:
            try:
                issues = self._search_issues_enhanced(jql, max_issues)
            except RuntimeError as exc:
                if search_api in {"auto", ""} and _should_fallback_to_legacy_search(exc):
                    issues = self._search_issues_legacy(jql, max_issues)
                else:
                    raise
        self.warnings = self._load_description_comments(issues, progress=progress)
        return issues

    def sprint_preflight(self, sprint: str, project_key: str = "") -> dict[str, Any]:
        """Return Web-safe Sprint validity and Development Done readiness data.

        A successful JQL query proves that the authenticated user can query the
        Sprint, including valid empty Sprints. Jira returns an authorization or
        invalid-Sprint error before this method can incorrectly mark it valid.
        """
        value = (sprint or "").strip()
        if not value:
            raise ValueError("Sprint ID or name is required.")
        sprint_record: dict[str, Any] = {}
        if value.isdigit() and hasattr(self, "username") and hasattr(self, "api_token"):
            sprint_record = self._request_json(f"/rest/agile/1.0/sprint/{urllib.parse.quote(value)}")
            if not isinstance(sprint_record, dict) or not sprint_record.get("id"):
                raise ValueError(f"Sprint {value} was not found or is not accessible.")
        issues = self.search_issues_by_sprint(value, project_key=project_key)
        configured_done = os.getenv("JIRA_DEVELOPMENT_DONE_STATUS", "Development Done").strip() or "Development Done"
        not_done = [
            {"jira_key": issue.key, "summary": issue.summary, "status": issue.status, "assignee": issue.assignee}
            for issue in issues
            if issue.status.strip().casefold() != configured_done.casefold()
        ]
        issue_rows = [
            {
                "jira_key": issue.key,
                "summary": issue.summary,
                "status": issue.status,
                "assignee": issue.assignee,
                "development_done": issue.status.strip().casefold() == configured_done.casefold(),
                "sprints": [membership.to_dict() for membership in issue.sprint_memberships],
                "current_sprint_id": issue.current_sprint_id,
            }
            for issue in issues
        ]
        ready = bool(issue_rows) and not not_done
        if not issue_rows:
            raise ValueError(
                f"Sprint {value} has no accessible Issues. Verify the Sprint ID, project scope, and Jira permissions."
            )
        return {
            "valid": True,
            "accessible": True,
            "sprint": value,
            "sprint_id": str(sprint_record.get("id") or value),
            "sprint_name": str(sprint_record.get("name") or value),
            "sprint_state": str(sprint_record.get("state") or ""),
            "project_key": project_key,
            "issue_count": len(issue_rows),
            "development_done_status": configured_done,
            "development_done_count": len(issue_rows) - len(not_done),
            "not_development_done_count": len(not_done),
            "all_development_done": ready,
            "review_mode": "final-sprint" if ready else "batch-preview",
            "requires_confirmation": bool(not_done),
            "empty": not issue_rows,
            "issues": issue_rows,
            "not_development_done_issues": not_done,
            "warnings": list(getattr(self, "warnings", [])),
            "partial": bool(getattr(self, "warnings", [])),
        }

    def _load_description_comments(
        self,
        issues: list[JiraIssue],
        *,
        progress: Any = None,
    ) -> list[dict[str, str]]:
        total = len(issues)
        if not total:
            return []
        workers = max(
            4,
            min(6, app_config_int("jira.comment_workers", "JIRA_COMMENT_WORKERS", 5)),
        )
        warnings: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
            futures = {
                pool.submit(
                    self.fetch_description_template_comments,
                    issue.key,
                    issue.issue_type,
                ): issue
                for issue in issues
            }
            for completed, future in enumerate(as_completed(futures), 1):
                issue = futures[future]
                comment_failed = False
                try:
                    issue.description_comments = future.result()
                except Exception as exc:
                    comment_failed = True
                    issue.description_comments = []
                    warning = {
                        "jira_key": issue.key,
                        "stage": "jira-comments",
                        "endpoint": f"/rest/api/3/issue/{issue.key}/comment",
                        "error": str(exc),
                    }
                    warnings.append(warning)
                if callable(progress):
                    event = "jira-comments-warning" if comment_failed else "jira-comments"
                    message = (
                        f"Loading Jira comments {completed}/{total} · warning for {issue.key}"
                        if event == "jira-comments-warning"
                        else f"Loading Jira comments {completed}/{total}"
                    )
                    try:
                        progress(
                            {
                                "event": event,
                                "message": message,
                                "current": completed,
                                "total": total,
                                "jira_key": issue.key,
                                "stage": "jira-comments",
                            }
                        )
                    except Exception:
                        pass
        return warnings

    def fetch_description_template_comments(self, issue_key: str, issue_type: str = "") -> list[str]:
        """Fetch all chronological comments containing the formal issue-description table."""
        encoded_key = urllib.parse.quote(issue_key)
        start_at = 0
        matched: list[str] = []
        while True:
            query = urllib.parse.urlencode({"startAt": start_at, "maxResults": 100, "orderBy": "created"})
            response = self._request_json(f"/rest/api/3/issue/{encoded_key}/comment?{query}")
            comments = response.get("comments", []) if isinstance(response, dict) else []
            for comment in comments:
                body = comment.get("body") if isinstance(comment, dict) else None
                text = _plain_text(body).strip()
                if is_description_template_comment(body, text, issue_type):
                    matched.append(text)
            start_at += len(comments)
            total = int(response.get("total") or 0) if isinstance(response, dict) else 0
            if not comments or len(comments) < 100 or (total and start_at >= total):
                break
        return matched

    def _search_issues_enhanced(self, jql: str, max_issues: int) -> list[JiraIssue]:
        issues: list[JiraIssue] = []
        next_page_token = ""
        seen_tokens: set[str] = set()
        while len(issues) < max_issues:
            page_size = min(100, max_issues - len(issues))
            payload: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": JIRA_SEARCH_FIELDS,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            response = self._request_json("/rest/api/3/search/jql", method="POST", payload=payload)
            page = response.get("issues", []) if isinstance(response, dict) else []
            if not page:
                break
            for item in page:
                issues.append(_jira_issue_from_item(item))
            token = str(response.get("nextPageToken") or "")
            if response.get("isLast") is True or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            next_page_token = token

        return issues

    def _search_issues_legacy(self, jql: str, max_issues: int) -> list[JiraIssue]:
        issues: list[JiraIssue] = []
        start_at = 0
        while len(issues) < max_issues:
            page_size = min(100, max_issues - len(issues))
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": page_size,
                "fields": JIRA_SEARCH_FIELDS,
            }
            response = self._request_json("/rest/api/3/search", method="POST", payload=payload)
            page = response.get("issues", []) if isinstance(response, dict) else []
            if not page:
                break
            for item in page:
                issues.append(_jira_issue_from_item(item))
            start_at += len(page)
            total = int(response.get("total") or 0)
            if len(page) < page_size or (total and start_at >= total):
                break

        return issues

    def fetch_issue_remote_links(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch Jira remote links. GitLab MR links often appear here when Jira/GitLab are integrated."""
        encoded_key = urllib.parse.quote(issue_key)
        try:
            response = self._request_json(f"/rest/api/3/issue/{encoded_key}/remotelink")
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            response = self._request_json(f"/rest/api/3/issues/{encoded_key}/remotelink")
        return response if isinstance(response, list) else []

    def fetch_issue_development_details(self, issue_id: str) -> dict[str, Any]:
        """Fetch Jira development-panel details when GitLab integration stores MR data there."""
        if not issue_id:
            return {}
        application_types = [
            os.getenv("JIRA_GITLAB_APPLICATION_TYPE", "GitLab"),
            "gitlab",
            "com.gitlab.integration.application",
        ]
        seen: set[str] = set()
        for application_type in application_types:
            if not application_type or application_type in seen:
                continue
            seen.add(application_type)
            query = urllib.parse.urlencode(
                {
                    "issueId": issue_id,
                    "applicationType": application_type,
                    "dataType": "merge_request",
                }
            )
            try:
                response = self._request_json(f"/rest/dev-status/1.0/issue/detail?{query}")
            except RuntimeError:
                continue
            return response if isinstance(response, dict) else {}
        return {}

    def add_comment(self, issue_key: str, comment_body: str) -> dict[str, Any]:
        """Add a comment to a Jira issue."""
        path = f"/rest/api/3/issues/{issue_key}/comments"
        payload = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment_body}],
                    }
                ],
            }
        }
        return self._request_json(path, method="POST", payload=payload)

    def add_link_to_issue(self, issue_key: str, link_url: str, link_title: str = "") -> dict[str, Any]:
        """Add a link (e.g., MR URL) to a Jira issue."""
        path = f"/rest/api/3/issues/{issue_key}/remotelink"
        payload = {
            "url": link_url,
            "title": link_title or link_url,
            "application": {"type": "com.atlassian.jira.plugin.system.issuetabpanels:gitlab-issue-panel"},
        }
        return self._request_json(path, method="POST", payload=payload)

    def transition_issue(self, issue_key: str, transition_name: str) -> dict[str, Any]:
        """Transition issue to a new status (e.g., 'In Progress', 'Done')."""
        # First, get available transitions
        path = f"/rest/api/3/issues/{issue_key}/transitions"
        response = self._request_json(path)

        transition_id = None
        for trans in response.get("transitions", []):
            if trans.get("name").lower() == transition_name.lower():
                transition_id = trans.get("id")
                break

        if not transition_id:
            raise ValueError(f"Transition '{transition_name}' not found for issue {issue_key}")

        # Perform transition
        payload = {"transition": {"id": transition_id}}
        return self._request_json(path, method="POST", payload=payload)

    def _request_json(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        """Make authenticated request to Jira API."""
        data = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Basic auth: username:api_token (base64)
        credentials = b64encode(f"{self.username}:{self.api_token}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        configured_attempts = app_config_int("jira.get_max_attempts", "JIRA_GET_MAX_ATTEMPTS", 2)
        attempts = max(1, min(3, configured_attempts)) if method.upper() == "GET" else 1
        backoff = max(
            0.0,
            min(
                2.0,
                app_config_int("jira.retry_backoff_ms", "JIRA_RETRY_BACKOFF_MS", 350) / 1000,
            ),
        )
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="ignore")
                transient = exc.code == 429 or 500 <= exc.code < 600
                if transient and attempt < attempts:
                    time.sleep(backoff * attempt)
                    continue
                raise RuntimeError(
                    f"Jira API {method.upper()} {path} failed at request "
                    f"attempt {attempt}/{attempts}: HTTP {exc.code}: {body}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < attempts:
                    time.sleep(backoff * attempt)
                    continue
                raise RuntimeError(
                    f"Jira API {method.upper()} {path} failed at request "
                    f"attempt {attempt}/{attempts}: {exc}"
                ) from exc
        raise RuntimeError(f"Jira API {method.upper()} {path} failed without a response.")

    @staticmethod
    def _extract_sprint_name(sprint_field: Any) -> str:
        """Extract sprint name from Jira custom field."""
        if not sprint_field:
            return ""
        selected = select_current_sprint(parse_sprint_memberships(sprint_field))
        return selected.name if selected else ""


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_plain_text(item) for item in value)
    if isinstance(value, dict):
        parts: list[str] = []
        text = value.get("text")
        if isinstance(text, str):
            parts.append(text)
        for child in value.get("content", []) or []:
            parts.append(_plain_text(child))
        return " ".join(part for part in parts if part)
    return str(value)


def _contains_adf_table(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get('type') or '').lower() == 'table':
            return True
        return any(_contains_adf_table(child) for child in (value.get('content') or []))
    if isinstance(value, list):
        return any(_contains_adf_table(child) for child in value)
    return False


def is_description_template_comment(body: Any, text: str, issue_type: str = '') -> bool:
    '''Identify a Jira comment that is a complete requirement/bug description template.'''
    normalized = re.sub(r'\s+', ' ', (text or '').casefold()).strip()
    if not normalized:
        return False
    has_table = _contains_adf_table(body) or ('|' in (text or '') and (text or '').count('|') >= 4)
    if not has_table:
        return False
    if not any(all(label in normalized for label in labels) for labels in _DESCRIPTION_TEMPLATE_COLUMNS):
        return False
    if not any(all(label in normalized for label in labels) for labels in _DESCRIPTION_TEMPLATE_COMMON):
        return False
    bug_issue = any(token in (issue_type or '').casefold() for token in ('bug', 'defect', '\u7f3a\u9677'))
    required_rows = _DESCRIPTION_TEMPLATE_BUG if bug_issue else _DESCRIPTION_TEMPLATE_REQUIREMENT
    return any(all(label in normalized for label in labels) for labels in required_rows)


def normalize_sprint_state(value: Any) -> str:
    state = str(value or "").strip().casefold()
    if state in {"closed", "complete", "completed", "done"}:
        return "complete"
    if state in {"active", "started", "open"}:
        return "active"
    if state in {"future", "planned", "pending"}:
        return "future"
    return state or "unknown"


def parse_sprint_memberships(value: Any, changelog: Any = None) -> list[SprintMembership]:
    """Normalize every Sprint representation returned by Jira Cloud/Server."""
    values = value if isinstance(value, list) else ([value] if value not in (None, "") else [])
    memberships: list[SprintMembership] = []
    for item in values:
        parsed = _parse_sprint_membership(item)
        if parsed and (parsed.id or parsed.name):
            memberships.append(parsed)

    joined_by_key = _sprint_joined_at_from_changelog(changelog)
    by_identity: dict[tuple[str, str], SprintMembership] = {}
    for item in memberships:
        key = (item.id, item.name.casefold())
        item.joined_at = joined_by_key.get(item.id) or joined_by_key.get(item.name.casefold()) or item.joined_at
        previous = by_identity.get(key)
        if previous is None or _sprint_recency_key(item) >= _sprint_recency_key(previous):
            by_identity[key] = item
    return sorted(by_identity.values(), key=_sprint_sort_key)


def select_current_sprint(
    memberships: list[SprintMembership],
    preferred_id: str = "",
    preferred_name: str = "",
) -> SprintMembership | None:
    """Choose the current review cycle Sprint without discarding old memberships."""
    preferred_id = str(preferred_id or "").strip()
    preferred_name = str(preferred_name or "").strip().casefold()
    for membership in memberships:
        if preferred_id and membership.id == preferred_id:
            return membership
        if preferred_name and membership.name.casefold() == preferred_name:
            return membership
    if not memberships:
        return None
    priority = {"active": 3, "future": 2, "complete": 1, "unknown": 0}
    return max(
        memberships,
        key=lambda item: (priority.get(item.state, 0), _sprint_recency_key(item), _numeric_sort(item.id)),
    )


def _parse_sprint_membership(value: Any) -> SprintMembership | None:
    if isinstance(value, dict):
        return SprintMembership(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            state=normalize_sprint_state(value.get("state")),
            start_date=str(value.get("startDate") or value.get("start_date") or ""),
            end_date=str(value.get("endDate") or value.get("end_date") or ""),
            complete_date=str(value.get("completeDate") or value.get("complete_date") or ""),
            board_id=str(value.get("originBoardId") or value.get("rapidViewId") or value.get("board_id") or ""),
            joined_at=str(value.get("joinedAt") or value.get("joined_at") or ""),
        )
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return _parse_sprint_membership(payload)
        except json.JSONDecodeError:
            pass
    # Legacy GreenHopper values use a comma-delimited Java toString format.
    content_match = re.search(r"\[(.*)\]\s*$", text)
    content = content_match.group(1) if content_match else text
    pairs = {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"(?:^|,)([A-Za-z][A-Za-z0-9_]*)=([^,\]]*)", content)
    }
    if pairs:
        return _parse_sprint_membership(pairs)
    # Some Jira instances return only the Sprint name.
    return SprintMembership(name=text)


def _sprint_joined_at_from_changelog(changelog: Any) -> dict[str, str]:
    histories = changelog.get("histories", []) if isinstance(changelog, dict) else []
    result: dict[str, str] = {}
    for history in histories if isinstance(histories, list) else []:
        if not isinstance(history, dict):
            continue
        created = str(history.get("created") or "")
        for item in history.get("items") or []:
            if not isinstance(item, dict) or str(item.get("field") or "").casefold() != "sprint":
                continue
            for raw in (item.get("toString"), item.get("to")):
                for membership in parse_sprint_memberships(raw):
                    for key in (membership.id, membership.name.casefold()):
                        if key:
                            result[key] = max(result.get(key, ""), created)
    return result


def _numeric_sort(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _sprint_recency_key(item: SprintMembership) -> str:
    return item.joined_at or item.start_date or item.complete_date or item.end_date or ""


def _sprint_sort_key(item: SprintMembership) -> tuple[str, int, str]:
    return (_sprint_recency_key(item), _numeric_sort(item.id), item.name.casefold())


def _jira_issue_from_item(item: dict[str, Any]) -> JiraIssue:

    fields = item.get("fields") or {}
    assignee = fields.get("assignee") or {}
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    labels = fields.get("labels") or []
    memberships = parse_sprint_memberships(fields.get("customfield_10005"), item.get("changelog"))
    current_sprint = select_current_sprint(memberships)
    return JiraIssue(
        key=str(item.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        description=_plain_text(fields.get("description")),
        assignee=str(assignee.get("displayName") or "Unassigned"),
        status=str(status.get("name") or "Unknown"),
        sprint=current_sprint.name if current_sprint else "",
        issue_type=str(issue_type.get("name") or ""),
        labels=labels if isinstance(labels, list) else [],
        epic_link=fields.get("customfield_10006") or None,
        id=str(item.get("id") or ""),
        sprint_memberships=memberships,
        current_sprint_id=current_sprint.id if current_sprint else "",
        current_sprint_state=current_sprint.state if current_sprint else "",
        components=_jira_option_names(fields.get("components")),
        responsibles=_jira_option_names(fields.get("customfield_10036")),
    )


def _jira_option_names(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("displayName") or item.get("value") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _should_fallback_to_legacy_search(exc: RuntimeError) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("404", "405", "not found", "method not allowed"))


ISSUE_TYPE_BRANCH_PREFIXES = {
    "feature",
    "improvement",
    "task",
    "bug",
    "change-request",
    "change-reqeust",  # Accept historical typo in branch names.
}
DPS_LAYER_PREFIXES = {"API", "DAO", "BIZ", "CLI"}


@dataclass(slots=True)
class BranchNamingInfo:
    issue_type: str = ""
    layer: str = ""
    jira_key: str = ""
    is_valid_current_format: bool = False
    is_legacy_format: bool = False


def parse_issue_branch_name(branch_name: str) -> BranchNamingInfo:
    normalized = (branch_name or "").strip()
    current = re.match(
        r"^(?P<issue_type>feature|improvement|task|bug|change-request|change-reqeust)/"
        r"(?:(?P<layer>API|DAO|BIZ|CLI)#)?(?P<jira>[A-Z][A-Z0-9]+-\d+)$",
        normalized,
        re.I,
    )
    if current:
        return BranchNamingInfo(
            issue_type=current.group("issue_type").lower(),
            layer=(current.group("layer") or "").upper(),
            jira_key=current.group("jira").upper(),
            is_valid_current_format=True,
        )

    legacy = re.search(r"\b(?:(API|DAO|BIZ|CLI)[#-])?([A-Z][A-Z0-9]+-\d+)\b", normalized)
    if legacy:
        return BranchNamingInfo(
            layer=(legacy.group(1) or "").upper(),
            jira_key=legacy.group(2).upper(),
            is_legacy_format=True,
        )
    return BranchNamingInfo()


def build_issue_branch_name(issue_key: str, issue_type: str = "feature", layer: str = "") -> str:
    issue_type = (issue_type or "feature").strip().lower()
    if issue_type not in ISSUE_TYPE_BRANCH_PREFIXES:
        issue_type = "feature"
    layer = (layer or "").strip().upper()
    if layer and layer not in DPS_LAYER_PREFIXES:
        layer = ""
    suffix = f"{layer}#{issue_key.upper()}" if layer else issue_key.upper()
    return f"{issue_type}/{suffix}"


def detect_jira_key_from_branch(branch_name: str) -> str:
    """
    Extract Jira issue key from branch name.

    Examples:
    - 'feature/ECHNL-5552' -> 'ECHNL-5552'
    - 'bug/ECHNL-6666' -> 'ECHNL-6666'
    - 'change-request/ECHNL-6667' -> 'ECHNL-6667'
    - 'feature/API#ECHNL-5552' -> 'ECHNL-5552'
    - 'feature/DAO#ECHNL-5552' -> 'ECHNL-5552'
    - legacy 'DAO#ECHNL-5552' -> 'ECHNL-5552'
    - legacy 'DAO-ECHNL-5552' -> 'ECHNL-5552'
    """
    return parse_issue_branch_name(branch_name).jira_key
