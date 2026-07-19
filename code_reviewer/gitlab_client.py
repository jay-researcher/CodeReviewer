from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import app_config_bool, gitlab_token
from .association import association_to_metadata, parse_issue_association
from .models import ChangedFile, ReviewInput


@dataclass(slots=True)
class MergeRequestRef:
    base_url: str
    project_path: str
    iid: str


class GitLabClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or gitlab_token()

    @classmethod
    def from_mr_url(cls, mr_url: str) -> tuple["GitLabClient", MergeRequestRef]:
        ref = parse_mr_url(mr_url)
        return cls(ref.base_url), ref

    def fetch_merge_request(self, project_path: str, iid: str) -> dict[str, Any]:
        project = urllib.parse.quote(project_path, safe="")
        return self._request_json(f"/api/v4/projects/{project}/merge_requests/{iid}")

    def find_user(self, reviewer: str) -> dict[str, Any] | None:
        value = (reviewer or "").strip()
        if not value:
            return None
        candidates = [value]
        if "@" in value:
            candidates.append(value.split("@", 1)[0])
        for candidate in candidates:
            query = urllib.parse.urlencode({"search": candidate})
            users = self._request_json(f"/api/v4/users?{query}")
            if not isinstance(users, list):
                continue
            exact = _pick_user(users, value)
            if exact:
                return exact
        return None

    def list_merge_requests_for_reviewer(
        self,
        reviewer: str,
        days: int = 7,
        state: str = "opened",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        states = _gitlab_state_values(state)
        if len(states) > 1:
            merged: list[dict[str, Any]] = []
            for item_state in states:
                merged.extend(
                    self.list_merge_requests_for_reviewer(
                        reviewer=reviewer,
                        days=days,
                        state=item_state,
                        limit=limit,
                    )
                )
            return _dedupe_merge_requests(merged, limit)

        user = self.find_user(reviewer)
        reviewer_username = (user or {}).get("username") or _username_from_reviewer(reviewer)
        updated_after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        params = {
            "scope": "all",
            "state": state,
            "updated_after": updated_after,
            "order_by": "updated_at",
            "sort": "desc",
            "per_page": min(max(limit, 1), 100),
        }
        if reviewer_username:
            params["reviewer_username"] = reviewer_username
        if user and user.get("id"):
            params["reviewer_id"] = str(user["id"])

        mrs = self._request_paged_json("/api/v4/merge_requests", params=params, limit=limit)
        if mrs or not reviewer_username:
            return mrs

        params.pop("reviewer_id", None)
        params["reviewer_username"] = reviewer_username
        return self._request_paged_json("/api/v4/merge_requests", params=params, limit=limit)

    def list_merge_requests_for_issue(
        self,
        issue_key: str,
        state: str = "opened",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        states = _gitlab_state_values(state)
        if len(states) > 1:
            merged: list[dict[str, Any]] = []
            for item_state in states:
                merged.extend(self.list_merge_requests_for_issue(issue_key, state=item_state, limit=limit))
            return _dedupe_merge_requests(merged, limit)

        key = (issue_key or "").strip().upper()
        if not key:
            return []
        params = {
            "scope": "all",
            "state": state,
            "search": key,
            "in": "title,description",
            "order_by": "updated_at",
            "sort": "desc",
            "per_page": min(max(limit, 1), 100),
        }
        try:
            mrs = self._request_paged_json("/api/v4/merge_requests", params=params, limit=limit)
        except RuntimeError:
            params.pop("in", None)
            mrs = self._request_paged_json("/api/v4/merge_requests", params=params, limit=limit)
        return [item for item in mrs if _merge_request_mentions_issue(item, key)]

    def list_project_branches(self, project_path: str, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
        project = urllib.parse.quote(project_path, safe="")
        params: dict[str, str | int] = {"per_page": min(max(limit, 1), 100)}
        if search:
            params["search"] = search
        return self._request_paged_json(f"/api/v4/projects/{project}/repository/branches", params=params, limit=limit)

    def list_project_merge_requests(
        self,
        project_path: str,
        source_branch: str = "",
        state: str = "opened",
        target_branch: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        states = _gitlab_state_values(state)
        if len(states) > 1:
            merged: list[dict[str, Any]] = []
            for item_state in states:
                merged.extend(
                    self.list_project_merge_requests(
                        project_path,
                        source_branch=source_branch,
                        state=item_state,
                        target_branch=target_branch,
                        limit=limit,
                    )
                )
            return _dedupe_merge_requests(merged, limit)

        project = urllib.parse.quote(project_path, safe="")
        params: dict[str, str | int] = {
            "state": state,
            "order_by": "updated_at",
            "sort": "desc",
            "per_page": min(max(limit, 1), 100),
        }
        if source_branch:
            params["source_branch"] = source_branch
        if target_branch:
            params["target_branch"] = target_branch
        return self._request_paged_json(f"/api/v4/projects/{project}/merge_requests", params=params, limit=limit)

    def fetch_merge_request_changes(self, project_path: str, iid: str) -> dict[str, Any] | list[dict[str, Any]]:
        project = urllib.parse.quote(project_path, safe="")
        changes_error = ""
        try:
            payload = self._request_json(f"/api/v4/projects/{project}/merge_requests/{iid}/changes")
            if _is_merge_request_changes_payload(payload):
                return payload
            changes_error = _describe_gitlab_payload(payload)
        except RuntimeError as exc:
            changes_error = str(exc)

        try:
            payload = self._request_json(f"/api/v4/projects/{project}/merge_requests/{iid}/diffs")
        except RuntimeError as exc:
            raise RuntimeError(
                f"GitLab MR diff fetch failed for {project_path}!{iid}; "
                f"/changes returned {changes_error or 'no usable changes'}, /diffs failed: {exc}"
            ) from exc
        if _is_merge_request_changes_payload(payload):
            return payload
        raise RuntimeError(
            f"GitLab MR diff response is empty or invalid for {project_path}!{iid}; "
            f"/changes returned {changes_error or 'no usable changes'}, "
            f"/diffs returned {_describe_gitlab_payload(payload)}."
        )

    def fetch_commit(self, project_path: str, commit_sha: str) -> dict[str, Any]:
        project = urllib.parse.quote(project_path, safe="")
        commit = urllib.parse.quote(commit_sha, safe="")
        return self._request_json(f"/api/v4/projects/{project}/repository/commits/{commit}")

    def fetch_commit_diff(self, project_path: str, commit_sha: str) -> list[dict[str, Any]]:
        project = urllib.parse.quote(project_path, safe="")
        commit = urllib.parse.quote(commit_sha, safe="")
        payload = self._request_json(f"/api/v4/projects/{project}/repository/commits/{commit}/diff")
        return payload if isinstance(payload, list) else []

    def compare_repository_refs(self, project_path: str, from_ref: str, to_ref: str) -> dict[str, Any]:
        project = urllib.parse.quote(project_path, safe="")
        query = urllib.parse.urlencode({"from": from_ref, "to": to_ref, "straight": "true"})
        payload = self._request_json(f"/api/v4/projects/{project}/repository/compare?{query}")
        return payload if isinstance(payload, dict) else {}

    def fetch_repository_tree(
        self,
        project_path: str,
        ref: str,
        path: str = "",
        recursive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "ref": ref,
            "per_page": min(max(limit, 1), 100),
            "recursive": "true" if recursive else "false",
        }
        if path:
            params["path"] = path.strip("/")
        project = urllib.parse.quote(project_path, safe="")
        return self._request_paged_json(f"/api/v4/projects/{project}/repository/tree", params=params, limit=limit)

    def fetch_repository_file(self, project_path: str, file_path: str, ref: str) -> str:
        project = urllib.parse.quote(project_path, safe="")
        encoded_file = urllib.parse.quote(file_path.strip("/"), safe="")
        query = urllib.parse.urlencode({"ref": ref})
        payload = self._request_json(f"/api/v4/projects/{project}/repository/files/{encoded_file}?{query}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"GitLab repository file response is not an object for {file_path}@{ref}.")
        content = str(payload.get("content") or "")
        if str(payload.get("encoding") or "").lower() == "base64":
            return base64.b64decode(content).decode("utf-8", errors="replace")
        return content

    def create_merge_request_note(self, project_path: str, iid: str, body: str) -> dict[str, Any]:
        project = urllib.parse.quote(project_path, safe="")
        return self._request_json(
            f"/api/v4/projects/{project}/merge_requests/{iid}/notes",
            method="POST",
            payload={"body": body},
        )

    def mr_web_url_to_review_input(self, mr: dict[str, Any], jira_key: str = "", sprint: str = "") -> ReviewInput:
        web_url = mr.get("web_url") or ""
        if not web_url:
            project_path = mr.get("references", {}).get("full", "").split("!", 1)[0]
            iid = str(mr.get("iid") or "")
            web_url = f"{self.base_url}/{project_path}/-/merge_requests/{iid}"
        return self.review_input_from_mr(web_url, jira_key=jira_key, sprint=sprint)

    def review_input_from_mr(self, mr_url: str, jira_key: str = "", sprint: str = "") -> ReviewInput:
        ref = parse_mr_url(mr_url)
        mr = self.fetch_merge_request(ref.project_path, ref.iid)
        if not isinstance(mr, dict):
            raise RuntimeError(
                f"GitLab MR response is empty or invalid for {ref.project_path}!{ref.iid}: "
                f"{_describe_gitlab_payload(mr)}."
            )
        changed_files: list[ChangedFile] = []
        raw_diff_parts: list[str] = []
        diff_metadata: dict[str, Any] = {"diff_source": "gitlab-api"}
        local_error = ""
        local_changes = None
        if app_config_bool("local_context.prefer_local_mr_diff", "PREFER_LOCAL_MR_DIFF", True):
            try:
                from .local_changes import local_merge_request_changes
                from .local_workspaces import resolve_workspace_for_project_path

                workspace = resolve_workspace_for_project_path(
                    ref.project_path,
                    branch=str(mr.get("target_branch") or ""),
                )
                if workspace:
                    local_jira_key = jira_key or detect_jira_key(
                        " ".join(
                            [
                                str(mr.get("title") or ""),
                                str(mr.get("description") or ""),
                                str(mr.get("source_branch") or ""),
                            ]
                        )
                    )
                    local_changes = local_merge_request_changes(
                        workspace.local_path,
                        ref.project_path,
                        ref.iid,
                        mr,
                        jira_key=local_jira_key,
                    )
            except Exception as exc:
                local_error = str(exc)

        if local_changes:
            changed_files = local_changes.changed_files
            raw_diff_parts = [local_changes.raw_diff]
            diff_metadata = {
                "diff_source": "gitnexus-local-cache" if local_changes.cache_hit else "local-working-copy",
                "diff_base_sha": local_changes.base_sha,
                "diff_head_sha": local_changes.head_sha,
                "diff_repository": local_changes.repository,
                "diff_cache_hit": local_changes.cache_hit,
            }
        else:
            changes_payload = self.fetch_merge_request_changes(ref.project_path, ref.iid)
            changes = _merge_request_changes_from_payload(changes_payload)
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("new_path") or change.get("old_path") or "unknown"
                diff = change.get("diff", "")
                additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
                deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
                changed_files.append(ChangedFile(path=path, additions=additions, deletions=deletions, diff=diff))
                raw_diff_parts.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{diff}")
            if local_error:
                diff_metadata["local_diff_error"] = local_error[:1000]

        association_text = " ".join(
            [
                mr.get("title", ""),
                mr.get("description", "") or "",
                mr.get("source_branch", "") or "",
            ]
        )
        association = parse_issue_association(association_text, explicit_action_issue=jira_key)
        detected_jira = jira_key or association.action_issue or detect_jira_key(association_text)

        author = mr.get("author") if isinstance(mr.get("author"), dict) else {}
        request_by = _merge_request_author_label(author)

        return ReviewInput(
            project=ref.project_path.split("/")[-1],
            mr_url=mr_url,
            mr_id=str(mr.get("iid") or ref.iid),
            jira_key=detected_jira,
            sprint=sprint,
            source_branch=mr.get("source_branch", ""),
            target_branch=mr.get("target_branch", ""),
            commit=(mr.get("sha") or ""),
            title=mr.get("title", ""),
            author=request_by,
            changed_files=changed_files,
            raw_diff="\n".join(raw_diff_parts),
            generated_at=datetime.now(),
            metadata={
                "gitlab_project_path": ref.project_path,
                "diff_base_sha": str((mr.get("diff_refs") or {}).get("base_sha") or ""),
                "diff_head_sha": str((mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha") or ""),
                "mr_state": str(mr.get("state") or ""),
                "mr_status": str(mr.get("state") or ""),
                "mr_created_at": str(mr.get("created_at") or ""),
                "mr_updated_at": str(mr.get("updated_at") or ""),
                "mr_merged_at": str(mr.get("merged_at") or ""),
                "mr_closed_at": str(mr.get("closed_at") or ""),
                "mr_request_by": request_by,
                **diff_metadata,
                **association_to_metadata(association),
            },
        )

    def _request_json(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        if os.getenv("GITLAB_USE_CURL_FIRST", "1").lower() not in {"0", "false", "no"}:
            fallback = self._request_json_with_curl(path, method=method, payload=payload)
            if fallback is not None:
                return fallback

        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GitLab API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            fallback = self._request_json_with_curl(path, method=method, payload=payload)
            if fallback is not None:
                return fallback
            raise RuntimeError(f"GitLab API connection failed: {exc}") from exc

    def _request_paged_json(self, path: str, params: dict[str, str | int], limit: int = 100) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while len(results) < limit:
            page_params = {**params, "page": page}
            query = urllib.parse.urlencode(page_params)
            payload = self._request_json(f"{path}?{query}")
            if not isinstance(payload, list) or not payload:
                break
            results.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < int(params.get("per_page", 100)):
                break
            page += 1
        return results[:limit]

    def _request_json_with_curl(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any | None:
        if not shutil.which("curl.exe") and not shutil.which("curl"):
            return None
        curl = shutil.which("curl.exe") or shutil.which("curl")
        assert curl is not None
        command = [
            curl,
            "-sS",
            "--max-time",
            "120",
            "-X",
            method,
            "-H",
            "Accept: application/json",
        ]
        if self.token:
            command.extend(["-H", f"PRIVATE-TOKEN: {self.token}"])
        if payload is not None:
            command.extend(["-H", "Content-Type: application/json", "--data", json.dumps(payload)])
        command.append(f"{self.base_url}{path}")
        completed = subprocess.run(command, capture_output=True, text=False, check=False, timeout=130)
        if completed.returncode != 0:
            return None
        stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"GitLab API returned non-JSON response: {stdout[:300]}")


def parse_mr_url(mr_url: str) -> MergeRequestRef:
    parsed = urllib.parse.urlparse(mr_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("MR URL must be an absolute GitLab URL.")
    match = re.match(r"^/(.+)/-/merge_requests/(\d+)", parsed.path)
    if not match:
        raise ValueError("MR URL must match /group/project/-/merge_requests/<iid>.")
    base_url = os.getenv("GITLAB_URL", f"{parsed.scheme}://{parsed.netloc}")
    return MergeRequestRef(base_url=base_url, project_path=urllib.parse.unquote(match.group(1)), iid=match.group(2))


def _is_merge_request_changes_payload(payload: Any) -> bool:
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict):
        return False
    changes = payload.get("changes")
    if isinstance(changes, list):
        return bool(changes)
    diffs = payload.get("diffs")
    return isinstance(diffs, list) and bool(diffs)


def _merge_request_changes_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        changes = payload.get("changes")
        if isinstance(changes, list):
            return [item for item in changes if isinstance(item, dict)]
        diffs = payload.get("diffs")
        if isinstance(diffs, list):
            return [item for item in diffs if isinstance(item, dict)]
    return []


def _describe_gitlab_payload(payload: Any) -> str:
    if payload is None:
        return "empty JSON null"
    if isinstance(payload, dict):
        keys = ", ".join(sorted(str(key) for key in payload.keys())[:8])
        return f"object without changes/diffs list (keys: {keys or '-'})"
    if isinstance(payload, list):
        return "empty list payload" if not payload else "list payload"
    return f"{type(payload).__name__} payload"


def _merge_request_author_label(author: dict[str, Any]) -> str:
    """Return a concise, stable requester label from GitLab MR author data."""
    name = str(author.get("name") or "").strip()
    username = str(author.get("username") or "").strip()
    if name and username and name.lower() != username.lower():
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "")


def parse_repository_url(repository_url: str, fallback_base_url: str = "") -> tuple[str, str]:
    value = (repository_url or "").strip().strip("'\"")
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@"):
        match = re.match(r"^git@([^:]+):(.+)$", value)
        if not match:
            raise ValueError(f"Unsupported Git repository URL: {repository_url}")
        host, project_path = match.groups()
        base_url = fallback_base_url or f"https://{host}"
        return base_url.rstrip("/"), urllib.parse.unquote(project_path.strip("/"))

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        return base_url.rstrip("/"), urllib.parse.unquote(parsed.path.strip("/"))
    if fallback_base_url:
        return fallback_base_url.rstrip("/"), value.strip("/")
    raise ValueError(f"Unsupported Git repository URL: {repository_url}")


def detect_jira_key(text: str) -> str:
    match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", text or "")
    return match.group(1) if match else ""


def _username_from_reviewer(reviewer: str) -> str:
    value = (reviewer or "").strip()
    return value.split("@", 1)[0] if "@" in value else value


def _pick_user(users: list[dict[str, Any]], reviewer: str) -> dict[str, Any] | None:
    value = reviewer.lower()
    username = _username_from_reviewer(reviewer).lower()
    for user in users:
        if str(user.get("email", "")).lower() == value:
            return user
    for user in users:
        if str(user.get("username", "")).lower() == username:
            return user
    return users[0] if users else None


def _gitlab_state_values(state: str) -> list[str]:
    text = (state or "opened").strip().lower()
    if not text:
        return ["opened"]
    if text in {"all", "*"}:
        return ["all"]
    values: list[str] = []
    aliases = {"open": "opened", "opened": "opened", "merge": "merged", "merged": "merged"}
    for raw in re.split(r"[,;|/\s]+", text):
        item = aliases.get(raw.strip(), raw.strip())
        if item and item not in values:
            values.append(item)
    return values or ["opened"]


def _dedupe_merge_requests(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("web_url") or item.get("id") or item.get("iid") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _merge_request_mentions_issue(mr: dict[str, Any], issue_key: str) -> bool:
    text = " ".join(
        [
            str(mr.get("title") or ""),
            str(mr.get("description") or ""),
            str(mr.get("source_branch") or ""),
            str(mr.get("target_branch") or ""),
            str(mr.get("web_url") or ""),
        ]
    ).upper()
    return issue_key.upper() in text
