"""
Example: Using Jira-GitLab Bridge to manage cross-project sprints

Scenario: ECHNL-5552 (Data migration task) requires changes across:
- iTrade Client (branch: feature/ECHNL-5552)
- DPS DAO layer (branch: feature/DAO#ECHNL-5552)
- DPS BIZ layer (branch: feature/BIZ#ECHNL-5552)
- WVAdmin (branch: feature/ECHNL-5552)
"""

from pathlib import Path
from code_reviewer.jira_client import JiraClient
from code_reviewer.gitlab_client import GitLabClient
from code_reviewer.jira_gitlab_bridge import JiraGitLabBridgeService, ProjectMapping

# 1. Initialize clients
jira = JiraClient()
gitlab = GitLabClient(base_url="https://gitlab.tx-tech.com")
bridge = JiraGitLabBridgeService(jira, gitlab)

# 2. Define your project mappings
project_mappings = [
    ProjectMapping(
        gitlab_project_slug="wvp-sv/itrade-client",
        gitlab_project_display="iTrade Client",
        jira_project_key="ECHNL",
        branch_patterns=["feature/*", "improvement/*", "task/*", "bug/*", "change-request/*"],
        default_branch="itrade-client",  # Version branch (e.g., 7.5.1.38)
    ),
    ProjectMapping(
        gitlab_project_slug="wvp-sv/dps11/microsrvs/wvadmin",
        gitlab_project_display="WVAdmin",
        jira_project_key="ECHNL",
        branch_patterns=["feature/*", "improvement/*", "task/*", "bug/*", "change-request/*"],
        default_branch="1.0.82",  # Version branch
    ),
    ProjectMapping(
        gitlab_project_slug="wvp-sv/dps11/microsrvs/drupalservices",
        gitlab_project_display="DPS",
        jira_project_key="ECHNL",
        branch_patterns=[
            "feature/API#*",
            "feature/DAO#*",
            "feature/BIZ#*",
            "feature/CLI#*",
            "improvement/API#*",
            "improvement/DAO#*",
            "improvement/BIZ#*",
            "improvement/CLI#*",
            "task/API#*",
            "task/DAO#*",
            "task/BIZ#*",
            "task/CLI#*",
            "bug/API#*",
            "bug/DAO#*",
            "bug/BIZ#*",
            "bug/CLI#*",
            "change-request/API#*",
            "change-request/DAO#*",
            "change-request/BIZ#*",
            "change-request/CLI#*",
        ],
        default_branch="11.2.82",  # Version branch (e.g., 11.2.82.11)
    ),
]

# 3. Discover all branches for an issue across projects
print("\n=== Discovering branches for ECHNL-5552 ===")
branches = bridge.discover_branches_for_issue("ECHNL-5552", project_mappings)
for branch in branches:
    print(f"  {branch.project_slug}: {branch.branch_name} (MR: {branch.mr_url or 'Not found'})")

# 4. Fetch Jira issue details
print("\n=== Jira Issue Details ===")
issue = jira.fetch_issue("ECHNL-5552")
print(f"  Key: {issue.key}")
print(f"  Summary: {issue.summary}")
print(f"  Assignee: {issue.assignee}")
print(f"  Status: {issue.status}")
print(f"  Sprint: {issue.sprint}")

# 5. Link a GitLab MR to the Jira issue
print("\n=== Linking MR to Jira ===")
mr_url = "https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/456"
jira_key = bridge.link_mr_to_jira_issue(mr_url)
print(f"  Linked MR to {jira_key}")

# 6. Generate sprint report showing all cross-project work
print("\n=== Sprint Cross-Project Report ===")
report = bridge.generate_sprint_cross_project_report(
    sprint_name="e-Channel Sprint 1.4.69",
    jira_project_key="ECHNL",
    project_mappings=project_mappings,
)

print(f"Sprint: {report['sprint']}")
for issue_data in report['issues']:
    print(f"\n  {issue_data['key']}: {issue_data['summary']}")
    print(f"    Assignee: {issue_data['assignee']}")
    print(f"    Status: {issue_data['status']}")
    for branch in issue_data['branches']:
        print(f"    - {branch['project']}: {branch['branch']} (MR: {branch['mr_url'] or 'N/A'})")

# 7. Sync MR status to Jira
print("\n=== Syncing MR Status to Jira ===")
bridge.sync_jira_issue_to_mr_status("ECHNL-5552", "merged")
print("  Issue transitioned to 'In Review'")
