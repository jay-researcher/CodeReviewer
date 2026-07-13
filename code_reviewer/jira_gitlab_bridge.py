from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitlab_client import GitLabClient
from .jira_client import JiraClient, IssueBranchMapping, build_issue_branch_name, detect_jira_key_from_branch


@dataclass(slots=True)
class ProjectMapping:
    """Maps a GitLab project to Jira space."""
    gitlab_project_slug: str  # e.g., "wvp-sv/dps11/microsrvs/wvadmin"
    gitlab_project_display: str  # e.g., "WVAdmin"
    jira_project_key: str  # e.g., "ECHNL"
    branch_patterns: list[str]  # e.g., ["feature/*", "feature/DAO#*", "feature/BIZ#*"]
    default_branch: str  # e.g., "1.0.82", "11.2.82"


class JiraGitLabBridgeService:
    """
    Links Jira issues to GitLab branches across multiple projects.

    Example workflow:
    1. Developer creates Jira issue ECHNL-5552
    2. Creates branches: feature/ECHNL-5552 (Web), feature/DAO#ECHNL-5552 (DPS), etc.
    3. Opens MRs on each branch
    4. Service auto-links all MRs to the same Jira issue
    5. Dashboard shows unified view of work across projects
    """

    def __init__(self, jira_client: JiraClient, gitlab_client: GitLabClient) -> None:
        self.jira = jira_client
        self.gitlab = gitlab_client

    def discover_branches_for_issue(
        self, issue_key: str, project_mappings: list[ProjectMapping]
    ) -> list[IssueBranchMapping]:
        """
        Discover GitLab branches related to a Jira issue across all projects.

        Args:
            issue_key: Jira issue key (e.g., "ECHNL-5552")
            project_mappings: List of project mappings to search in

        Returns:
            List of IssueBranchMapping objects with branch names and MR info
        """
        mappings = []

        for project_map in project_mappings:
            # Search for branches matching the issue key
            branches = self._find_branches_in_project(project_map.gitlab_project_slug, issue_key)

            for branch_name in branches:
                # Try to find an MR for this branch
                mr_info = self._find_mr_for_branch(project_map.gitlab_project_slug, branch_name, project_map.default_branch)

                mapping = IssueBranchMapping(
                    jira_key=issue_key,
                    project_slug=project_map.gitlab_project_display,
                    branch_name=branch_name,
                    mr_iid=mr_info.get("iid") if mr_info else None,
                    mr_url=mr_info.get("web_url") if mr_info else None,
                )
                mappings.append(mapping)

        return mappings

    def link_mr_to_jira_issue(self, mr_url: str, jira_key: str = "") -> str:
        """
        Link a GitLab MR to a Jira issue by:
        1. Extracting Jira key from MR title/branch if not provided
        2. Adding MR link to Jira issue
        3. Adding review comment to MR with Jira link

        Args:
            mr_url: GitLab MR URL
            jira_key: Optional explicit Jira issue key

        Returns:
            The Jira issue key used
        """
        # Parse MR URL to get details
        from .gitlab_client import parse_mr_url
        ref = parse_mr_url(mr_url)
        mr = self.gitlab.fetch_merge_request(ref.project_path, ref.iid)

        # Detect Jira key from MR title, description, or source branch
        detected_key = jira_key or detect_jira_key_from_branch(
            " ".join([
                mr.get("title", ""),
                mr.get("description", "") or "",
                mr.get("source_branch", "") or "",
            ])
        )

        if not detected_key:
            raise ValueError(f"Could not detect Jira issue key from MR: {mr_url}")

        # Add link in Jira
        try:
            self.jira.add_link_to_issue(
                detected_key,
                mr_url,
                f"GitLab MR: {mr.get('title', '')} ({ref.project_path})",
            )
        except Exception as e:
            # Link might already exist, continue
            print(f"Warning: Could not add Jira link: {e}")

        return detected_key

    def sync_jira_issue_to_mr_status(self, issue_key: str, mr_status: str) -> None:
        """
        Sync Jira issue status to match MR status.
        E.g., MR merged → transition issue to "In Review" or "Done"
        """
        # Map MR status to Jira transitions
        status_map = {
            "merged": "In Review",
            "closed": "In Review",
            "opened": "In Progress",
        }

        jira_transition = status_map.get(mr_status)
        if jira_transition:
            try:
                self.jira.transition_issue(issue_key, jira_transition)
            except Exception as e:
                print(f"Warning: Could not transition {issue_key}: {e}")

    def generate_sprint_cross_project_report(
        self, sprint_name: str, jira_project_key: str, project_mappings: list[ProjectMapping]
    ) -> dict:
        """
        Generate a report of all work across projects for a sprint.

        Output:
        {
            "sprint": "e-Channel Sprint 1.4.69",
            "issues": [
                {
                    "key": "ECHNL-5552",
                    "summary": "Data migration...",
                    "branches": [
                        {"project": "DPS", "branch": "feature/DAO#ECHNL-5552", "mr_url": "..."},
                        {"project": "iTrade", "branch": "feature/ECHNL-5552", "mr_url": "..."},
                    ]
                }
            ]
        }
        """
        # Fetch all issues in sprint from Jira
        issues = self.jira.search_issues_by_sprint(sprint_name, jira_project_key)

        report = {"sprint": sprint_name, "issues": []}

        for issue in issues:
            # Find branches for this issue across all projects
            branches = self.discover_branches_for_issue(issue.key, project_mappings)

            if branches:  # Only include issues that have branches
                report["issues"].append({
                    "key": issue.key,
                    "summary": issue.summary,
                    "assignee": issue.assignee,
                    "status": issue.status,
                    "branches": [
                        {
                            "project": b.project_slug,
                            "branch": b.branch_name,
                            "mr_url": b.mr_url,
                            "mr_iid": b.mr_iid,
                        }
                        for b in branches
                    ],
                })

        return report

    def _find_branches_in_project(self, project_path: str, issue_key: str) -> list[str]:
        """Find all branches in a project that contain the issue key."""
        issue = issue_key.upper()
        try:
            branches = self.gitlab.list_project_branches(project_path, search=issue_key, limit=100)
            names = [str(item.get("name") or "") for item in branches if isinstance(item, dict)]
            return [
                name
                for name in names
                if detect_jira_key_from_branch(name).upper() == issue or issue in name.upper()
            ]
        except Exception:
            patterns = [
                build_issue_branch_name(issue_key, "feature"),
                build_issue_branch_name(issue_key, "improvement"),
                build_issue_branch_name(issue_key, "task"),
                build_issue_branch_name(issue_key, "bug"),
                build_issue_branch_name(issue_key, "change-request"),
                build_issue_branch_name(issue_key, "feature", "API"),
                build_issue_branch_name(issue_key, "feature", "DAO"),
                build_issue_branch_name(issue_key, "feature", "BIZ"),
                build_issue_branch_name(issue_key, "feature", "CLI"),
            ]
            return patterns

    def _find_mr_for_branch(self, project_path: str, branch_name: str, target_branch: str) -> dict | None:
        """Find an open MR for a branch in a project."""
        try:
            mrs = self.gitlab.list_project_merge_requests(
                project_path,
                source_branch=branch_name,
                target_branch=target_branch,
                state="all",
                limit=20,
            )
            if not mrs and target_branch:
                mrs = self.gitlab.list_project_merge_requests(
                    project_path,
                    source_branch=branch_name,
                    state="all",
                    limit=20,
                )
            return mrs[0] if mrs else None
        except Exception:
            return None
