# Jira-GitLab Integration Guide

> Jira Issue 首次 Sprint 交付后的再次处理流程及 description/comment 合并规则，参见 [Jira Issue 再处理及审查规则](JIRA-ISSUE-REPROCESSING.md)。

## Overview

This guide shows you how to connect Jira issues to GitLab branches across multiple projects, enabling:
- ✅ Track a single Jira issue across 3+ projects (iTrade, DPS, WVAdmin)
- ✅ Auto-link MRs to corresponding Jira issues
- ✅ View cross-project work in a unified sprint dashboard
- ✅ Sync MR status changes back to Jira

### Your Workflow

```
Jira Issue: ECHNL-5552 "Data migration"
│
├─ iTrade Client: branch feature/ECHNL-5552
│  └─ MR #198 → Jira issue linked
│
├─ DPS (DAO layer): branch feature/DAO#ECHNL-5552
│  └─ MR #456 → Jira issue linked
│
├─ DPS (BIZ layer): branch feature/BIZ#ECHNL-5552
│  └─ MR #457 → Jira issue linked
│
├─ DPS (API layer): branch feature/API#ECHNL-5552
│  └─ MR #458 → Jira issue linked
│
└─ WVAdmin: branch feature/ECHNL-5552
   └─ MR #789 → Jira issue linked

Dashboard shows: All MRs linked to ECHNL-5552, status synced
```

### Branch Naming Convention

All development GitLab branches should be based on action/development Jira issues such as `ECHNL-*`.

Web projects:

```text
feature/ECHNL-8888
bug/ECHNL-6666
task/ECHNL-6668
improvement/ECHNL-6669
change-request/ECHNL-6667
```

DPS middle-office backend projects:

```text
feature/API#ECHNL-8888
feature/DAO#ECHNL-8888
feature/BIZ#ECHNL-8888
feature/CLI#ECHNL-8888
```

`SVREQ` is only the PRD/requirement Jira space. Development branch mappings should use `ECHNL` or another action/development Jira space.

---

## Step 1: Get Your Jira API Token

### Option A: Cloud Jira (Atlassian Cloud)
1. Go to: https://id.atlassian.com/manage/api-tokens
2. Click **Create API token**
3. Give it a name (e.g., "CodeReviewer")
4. Copy the token (you'll see it only once)

### Option B: Self-Hosted Jira
1. Go to: `https://jira.tx-tech.com/secure/ViewProfile.jspa`
2. Click **API tokens** tab
3. Click **Create API token**
4. Copy the token

**Note:** You'll also need your Jira **email/username** (the account you logged in with).

---

## Step 2: Configure CodeReviewer

### Update `.env` file

Copy `.env.example` to `.env` and fill in Jira details:

```bash
# From GitLab (existing)
GITLAB_TOKEN=glpat-xxxxx
GITLAB_URL=https://gitlab.tx-tech.com

# From Jira (new)
JIRA_URL=https://jira.tx-tech.com
JIRA_USERNAME=your-email@company.com
JIRA_TOKEN=ATATT3xFfGH0xxxxx...
```

**⚠️ Important:** Add `.env` to `.gitignore` to avoid committing secrets!

```bash
echo ".env" >> .gitignore
```

---

## Step 3: Update Your Project Mappings

Create a configuration file mapping your GitLab projects to Jira:

**File:** `code_reviewer/project_config.py`

Important Jira space rule:

- `SVREQ` is the PRD/requirement Jira space.
- GitLab development branches map to action/development Jira spaces such as `ECHNL`.
- Do not set `jira_project_key="SVREQ"` for iTrade/WVAdmin/DPS GitLab development project mappings.

```python
from code_reviewer.jira_gitlab_bridge import ProjectMapping

# Define your projects and their mappings
PROJECT_MAPPINGS = [
    ProjectMapping(
        gitlab_project_slug="wvp-sv/itrade-client",
        gitlab_project_display="iTrade Client",
        jira_project_key="ECHNL",  # Development/action Jira space
        branch_patterns=["feature/*", "improvement/*", "task/*", "bug/*", "change-request/*"],
        default_branch="itrade-client",  # Version branch name
    ),
    ProjectMapping(
        gitlab_project_slug="wvp-sv/dps11/microsrvs/drupalservices",
        gitlab_project_display="DPS",
        jira_project_key="ECHNL",  # Development space
        branch_patterns=[
            "feature/API#*", "feature/DAO#*", "feature/BIZ#*", "feature/CLI#*",
            "improvement/API#*", "improvement/DAO#*", "improvement/BIZ#*", "improvement/CLI#*",
            "task/API#*", "task/DAO#*", "task/BIZ#*", "task/CLI#*",
            "bug/API#*", "bug/DAO#*", "bug/BIZ#*", "bug/CLI#*",
            "change-request/API#*", "change-request/DAO#*", "change-request/BIZ#*", "change-request/CLI#*",
        ],
        default_branch="11.2.82",  # Version branch name (e.g., 11.2.82.11)
    ),
    ProjectMapping(
        gitlab_project_slug="wvp-sv/dps11/microsrvs/wvadmin",
        gitlab_project_display="WVAdmin",
        jira_project_key="ECHNL",
        branch_patterns=["feature/*", "improvement/*", "task/*", "bug/*", "change-request/*"],
        default_branch="1.0.82",  # Version branch name (e.g., 1.0.82.8)
    ),
]
```

---

## Step 4: Use the Integration

### 4.1 Discover All Branches for a Jira Issue

See all branches across projects that are related to one Jira issue:

```python
from code_reviewer.jira_client import JiraClient
from code_reviewer.gitlab_client import GitLabClient
from code_reviewer.jira_gitlab_bridge import JiraGitLabBridgeService
from code_reviewer.project_config import PROJECT_MAPPINGS

jira = JiraClient()
gitlab = GitLabClient()
bridge = JiraGitLabBridgeService(jira, gitlab)

# Find all branches for ECHNL-5552 across all projects
branches = bridge.discover_branches_for_issue("ECHNL-5552", PROJECT_MAPPINGS)

for branch in branches:
    print(f"{branch.project_slug}: {branch.branch_name}")
    if branch.mr_url:
        print(f"  MR: {branch.mr_url}")
```

**Output:**
```
DPS: feature/DAO#ECHNL-5552
  MR: https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/456
DPS: feature/BIZ#ECHNL-5552
  MR: https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/457
iTrade Client: feature/ECHNL-5552
  MR: https://gitlab.tx-tech.com/wvp-sv/itrade-client/-/merge_requests/198
WVAdmin: feature/ECHNL-5552
  MR: https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/wvadmin/-/merge_requests/789
```

### 4.2 Link a GitLab MR to a Jira Issue

Auto-detect the Jira key from MR title/branch and link it:

```python
mr_url = "https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/456"
jira_key = bridge.link_mr_to_jira_issue(mr_url)
print(f"Linked to {jira_key}")  # Output: "Linked to ECHNL-5552"
```

### 4.3 Get Sprint Cross-Project Report

See all work across projects for a sprint:

```python
report = bridge.generate_sprint_cross_project_report(
    sprint_name="e-Channel Sprint 1.4.69",
    jira_project_key="ECHNL",
    project_mappings=PROJECT_MAPPINGS,
)

for issue in report['issues']:
    print(f"{issue['key']}: {issue['summary']}")
    print(f"  Assignee: {issue['assignee']}")
    for branch in issue['branches']:
        status = "✓" if branch['mr_url'] else "✗"
        print(f"    {status} {branch['project']}: {branch['branch']}")
```

**Output:**
```
ECHNL-5552: Data migration & schema changes
  Assignee: John Doe
    ✓ DPS: feature/DAO#ECHNL-5552
    ✓ DPS: feature/BIZ#ECHNL-5552
    ✓ iTrade Client: feature/ECHNL-5552
    ✓ WVAdmin: feature/ECHNL-5552

ECHNL-5553: API authentication flow
  Assignee: Jane Smith
    ✓ DPS: feature/API#ECHNL-5553
    ✗ iTrade Client: (no branch yet)
```

### 4.4 Sync MR Status to Jira

When an MR is merged, auto-transition the Jira issue:

```python
bridge.sync_jira_issue_to_mr_status("ECHNL-5552", "merged")
# Issue is now transitioned to "In Review" in Jira
```

---

## Step 5: Integrate with `review.py` Script

Update the review script to link MRs to Jira automatically:

**File:** `code_reviewer/review_service.py`

```python
def review_from_mr_url(mr_url: str, jira_key: str = "", sprint: str = "") -> ReviewResult:
    client, _ = GitLabClient.from_mr_url(mr_url)
    review_input = client.review_input_from_mr(mr_url, jira_key=jira_key, sprint=sprint)
    
    # NEW: Auto-link to Jira
    if not jira_key:
        try:
            from .jira_client import JiraClient
            from .jira_gitlab_bridge import JiraGitLabBridgeService
            
            jira = JiraClient()
            gitlab = GitLabClient.from_mr_url(mr_url)[0]
            bridge = JiraGitLabBridgeService(jira, gitlab)
            
            detected_jira = bridge.link_mr_to_jira_issue(mr_url)
            review_input.jira_key = detected_jira
        except Exception as e:
            print(f"Warning: Could not link to Jira: {e}")
    
    return analyze(review_input)
```

### Usage:
```bash
# Auto-detect Jira issue and link
python review.py \
  --mr-url https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/456

# Or specify explicitly
python review.py \
  --mr-url https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/drupalservices/-/merge_requests/456 \
  --jira ECHNL-5552
```

---

## Step 6: Web Dashboard Integration

The web UI can display cross-project work:

```python
@app.route('/api/sprint/<sprint_name>')
def get_sprint_view(sprint_name):
    """Show all work across projects for a sprint."""
    report = bridge.generate_sprint_cross_project_report(
        sprint_name=sprint_name,
        jira_project_key="ECHNL",
        project_mappings=PROJECT_MAPPINGS,
    )
    return {
        "sprint": sprint_name,
        "issues": report['issues'],
        "total_branches": sum(len(i['branches']) for i in report['issues']),
    }
```

**Frontend display:**
```
Sprint: e-Channel Sprint 1.4.69

ECHNL-5552: Data migration
├─ DPS DAO (feature/DAO#ECHNL-5552) [MR #456] Status: Open
├─ DPS BIZ (feature/BIZ#ECHNL-5552) [MR #457] Status: In Review
├─ iTrade Client (feature/ECHNL-5552) [MR #198] Status: Merged
└─ WVAdmin (feature/ECHNL-5552) [MR #789] Status: Draft

ECHNL-5553: API authentication
├─ DPS API (feature/API#ECHNL-5553) [MR #458] Status: In Review
└─ iTrade Client (feature/ECHNL-5553) [MR #199] Status: Open
```

---

## Troubleshooting

### Error: "Jira configuration incomplete"
**Fix:** Check that `.env` has all three Jira fields:
```
JIRA_URL=https://jira.tx-tech.com
JIRA_USERNAME=your-email@company.com
JIRA_TOKEN=ATATT3xFfGH0xxxxx
```

### Error: "401 Unauthorized"
**Fix:** Verify your Jira API token is correct. Test it:
```bash
curl -u your-email@company.com:$JIRA_TOKEN \
  https://jira.tx-tech.com/rest/api/3/myself
```

### MRs not detected for an issue
**Fix:** Ensure branch names follow naming convention:
- ✅ `feature/ECHNL-5552`
- ✅ `bug/ECHNL-6666`
- ✅ `task/ECHNL-6668`
- ✅ `change-request/ECHNL-6667`
- ✅ `feature/API#ECHNL-5552`
- ✅ `feature/DAO#ECHNL-5552`
- ✅ `feature/BIZ#ECHNL-5552`
- ✅ `feature/CLI#ECHNL-5552`
- ❌ `ECHNL-5552-bugfix` (key not detected)

Legacy branches such as `DAO#ECHNL-5552` and `DAO-ECHNL-5552` can still be parsed, but new branches should use `<issue type>/<layer>#<issue key>`.

Update `branch_patterns` in `PROJECT_MAPPINGS` if needed.

---

## Next: Integration with GitNexus

Once this works, we can add:
1. **Impact analysis** — Show how changes in feature/DAO#ECHNL-5552 affect feature/API#ECHNL-5552
2. **Risk scoring** — Cross-project blast radius
3. **Automated suggestions** — "This DAO change requires API review"

Ready to proceed?
