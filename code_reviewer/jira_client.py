from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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

    @property
    def final_description(self) -> str:
        """Merge the original description with later formal description comments."""
        parts = [self.description.strip()]
        parts.extend(item.strip() for item in (self.description_comments or []) if item.strip())
        return "\n\n".join(part for part in parts if part)


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

        if not all([self.base_url, self.username, self.api_token]):
            raise ValueError(
                "Jira configuration incomplete. Set JIRA_URL, JIRA_USERNAME, JIRA_TOKEN env vars."
            )

    def fetch_issue(self, issue_key: str) -> JiraIssue:
        """Fetch a Jira issue by key (e.g., ECHNL-5552)."""
        encoded_key = urllib.parse.quote(issue_key)
        try:
            response = self._request_json(f"/rest/api/3/issue/{encoded_key}")
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            response = self._request_json(f"/rest/api/3/issues/{encoded_key}")

        issue = _jira_issue_from_item(response)
        issue.description_comments = self.fetch_description_template_comments(issue.key, issue.issue_type)
        return issue

    def search_issues_by_sprint(self, sprint_name: str, project_key: str = "") -> list[JiraIssue]:
        """Search for issues in a sprint by sprint ID or sprint name."""
        sprint_value = (sprint_name or "").strip()
        jql_sprint = sprint_value if re.fullmatch(r"\d+", sprint_value) else f'"{sprint_value}"'
        jql = f"sprint = {jql_sprint}"
        if project_key:
            jql = f'project = {project_key} AND {jql}'

        max_issues = app_config_int("jira.sprint_max_issues", "JIRA_SPRINT_MAX_ISSUES", 500)
        return self.search_issues_by_jql(jql, max_issues=max_issues)

    def search_issues_by_filter_id(self, filter_id: str, max_issues: int | None = None) -> list[JiraIssue]:
        """Search for issues returned by a saved Jira filter ID."""
        value = (filter_id or "").strip()
        if not value:
            raise ValueError("Jira filter ID is required.")
        jql_filter = value if re.fullmatch(r"\d+", value) else f'"{value}"'
        max_results = max_issues if max_issues is not None else app_config_int("jira.filter_max_issues", "JIRA_FILTER_MAX_ISSUES", 500)
        return self.search_issues_by_jql(f"filter = {jql_filter}", max_issues=max_results)

    def search_issues_by_jql(self, jql: str, max_issues: int) -> list[JiraIssue]:
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
        for issue in issues:
            issue.description_comments = self.fetch_description_template_comments(issue.key, issue.issue_type)
        return issues

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

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Jira API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Jira API connection failed: {exc}") from exc

    @staticmethod
    def _extract_sprint_name(sprint_field: Any) -> str:
        """Extract sprint name from Jira custom field."""
        if not sprint_field:
            return ""
        # Sprint field can be a list or a string with format: "com.atlassian.greenhopper.service.sprint.Sprint@xxx[id=123,...]"
        if isinstance(sprint_field, list) and sprint_field:
            sprint_field = sprint_field[0]
        if isinstance(sprint_field, str):
            match = re.search(r"name=([^,\]]+)", sprint_field)
            return match.group(1) if match else sprint_field
        return ""


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


def _jira_issue_from_item(item: dict[str, Any]) -> JiraIssue:

    fields = item.get("fields") or {}
    assignee = fields.get("assignee") or {}
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    labels = fields.get("labels") or []
    return JiraIssue(
        key=str(item.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        description=_plain_text(fields.get("description")),
        assignee=str(assignee.get("displayName") or "Unassigned"),
        status=str(status.get("name") or "Unknown"),
        sprint=JiraClient._extract_sprint_name(fields.get("customfield_10005")),
        issue_type=str(issue_type.get("name") or ""),
        labels=labels if isinstance(labels, list) else [],
        epic_link=fields.get("customfield_10006") or None,
        id=str(item.get("id") or ""),
    )


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
