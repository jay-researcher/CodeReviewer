# CodeReviewer 7.0 Issue Review Workflow

> Language: English | [中文版](CodeReviewer-7.0-Issue-Workflow.zh-CN.md)

Version: 7.2.12
Development branch: `20260714`  

> 7.2.12 UI clarification: Sprint Overview does not inherit the home Jira field and separates readiness from issue cards with Overview / Sprint issues tabs. Issue Review application cards use a readable three-column desktop grid, Problems show distinct Problem and Suggestion previews, and dialogs follow shared S/M/L/XL/Full sizing.
>
> 7.2.11 clarification: single-Issue review shows the existing-report preflight in Progress; Problems includes problem/suggestion previews and identifies Company Config versus SCR from structured deferred-resource metadata. Branch configuration accepts exact values and version wildcards. Jira REST remains authoritative and the only Jira write boundary; Rovo is read-only candidate context and local jira-prd/RAG is disabled by default.
>
> 7.2.0 access clarification: Manager-only User Management maintains roles, active state and responsible scopes. Create/reset returns a one-time temporary password, while deactivation, role changes and password changes revoke existing sessions.
>
> 7.1.7 UI clarification: required markers on the Login page remain inline on the right side of Username, Password and Robot Check labels.
>
> 7.1.6 UI clarification: Review Communication uses an icon-only copy action, resizable Reply/Follow-up fields and an information hint instead of a permanent guidance block. Release Notes has a bounded scrollable dialog. Issue Review metrics now show Critical, High and Medium progress plus one combined Manager exception/blocker summary.
>
> 7.1.5 UI clarification: Follow-up Draft can be copied, Reply/Follow-up cards share the available column height, Report History filters share one right edge, and each finding in the report Problem List is independently collapsible.
>
> 7.1.4 UI clarification: the GIT_VERSION Release Gate uses a compact two-column action card. Sprint handoff context stays on the left; the MR label/action header and full-width URL input stay on the right, with responsive single-column fallback.
>
> 7.1.3 clarification: deferred Company Config/SCR reports use a project/resource filename, and every GIT_VERSION Release Gate must match one configured GitLab project before LLM review. The gate still reviews every locked source/build dependency belonging to that product. The standalone Web app exchanges Jira descriptions as ADF and can progressively enhance editing with a locally built Atlaskit bundle; Jira Forge UI components are not treated as directly embeddable Web widgets.
>
> 7.1.2 clarification: one Jira Issue remains the stable parent record. Re-delivery in a new Sprint closes the previous active Review Cycle and creates a new Cycle. The Web `Overview` groups current and historical cycles by Sprint, while `History & Snapshots` nests every Run and immutable review snapshot under its Sprint Cycle.
Acceptance URL: `http://127.0.0.1:8765`  
Production deployment: explicitly excluded until local acceptance is approved.

## Roles and data scope

| Function | Developer | Auditor / Leader | Manager | Remarks |
| --- | --- | --- | --- | --- |
| View assigned Issue Reviews | Yes | Yes | Yes | Developer and Auditor are scoped by `responsible`; Manager sees all. |
| Run one Issue Review | - | Yes | Yes | Hidden for Developer and rejected by the API. |
| Run Sprint/Filter Review | - | - | Yes | Release management workflow. |
| Run GIT_VERSION Release Gate | - | - | Yes | Web-native release gate; CLI is an operational fallback only. |
| Submit finding handling | Yes | Yes | Yes | Fixed, Jira follow-up, or Not an issue. |
| Approve Not an issue | - | Yes | Yes | Developer submissions remain pending until approved. |
| Re-scan | - | Yes | Yes | Fixed blockers are not cleared until absent from a later Run. |
| Manager Exception | - | - | Yes | Requires a Pending Jira draft and an audit reason. |
| Manual Pass | - | Yes | Yes | Auditor remains restricted to owned responsible scope. |
| Discuss / Report Preview | Yes | Yes | Yes | Discussion is associated with Issue, Run and optional Finding. |
| Manage users | - | - | Yes | Create, activate/deactivate, assign role/scope and reset password. |

Trial Developer mapping:

| Responsible | Developer accounts |
| --- | --- |
| `wen.yi` | `gerhard.guo`, `bryan.tan` |
| `kevin.tan` | `vincentgr.wang` |
| `kelvinh.wu` | `benyq.feng` |

`kelvinh.wu` and `luckxh.chen` are Auditors with self-owned responsible scopes.

## Review Cycle and incremental boundary

One Jira Issue remains one top-level history record. Each later Sprint treatment creates a Review Cycle, and one logical review creates a Run Group containing its frontend/backend Runs:

```text
Issue → Sprint membership → Review Cycle → Run Group → project-type Runs
```

An MR revision is identified by GitLab project, MR IID and Head SHA. An unchanged revision already reviewed in an earlier Cycle is excluded; a new Head SHA is reviewed again. Only the current Cycle's `base_sha → head_sha` diffs are finding sources. Target-branch latest code is context only, and the original Description plus earlier formal template Comments are historical requirement context without earlier Cycle diffs.

Company Config and SCR revisions are persisted as Deferred Release Resources. A deferred-only Issue is a successful “no code changes to review / release gate pending” result, not a missing-MR failure.

When every Finding in the latest Run Group has a handling submission, CodeReviewer creates an immutable Review Snapshot. Approval, Manager Exception and Manual Pass create later snapshot revisions without replacing earlier evidence.

## State and gate rules

The Issue lifecycle is:

```text
Not Reviewed → Generating → Handling → Re-scan Required
             → Re-scanning → Ready for Pass → Passed
```

The default blocking severities are Critical and High. Configure them in `config.yml`:

```yaml
app:
  review_workflow:
    blocking_severities: [Critical, High]
    require_rescan_for_fixed: true
    follow_up_unblocks_blocking: false
    manager_override_enabled: true
```

Rules:

1. `fixed` on a blocking finding changes the Issue to `Re-scan Required`; it does not immediately clear the gate.
2. A later Review Run must no longer contain the finding fingerprint.
3. Developer `not-issue` requires Auditor or Manager approval.
4. `follow-up` creates an ADF Jira draft and does not clear a blocking finding by default.
5. Manager may record an exception only after the draft exists and a reason is provided. It remains visibly labelled `Manager Exception` in Pass readiness and audit data.
6. A new Run invalidates the previous Pass association.

## Issue Reviews

`Issue Reviews` aggregates report history by ECHNL Issue and displays:

- current lifecycle status;
- responsible scope;
- Critical/High counts;
- fixed/follow-up/not-issue/pending counts;
- latest Run and total Run count;
- new and persisting findings;
- created and updated time;
- discussions, Pending Jira drafts and Pass records.

Finding lineage uses a stable fingerprint derived from Issue, project/file, category/rule, and normalized title. Report sequence numbers are retained for display but are not used as durable identity.

GIT_VERSION MRs use a dedicated release-gate LLM context budget. Deterministic YAML, commit-lock, and resource-integrity checks run locally; the LLM context prioritizes locked-repository diffs, release-gate results, and material configuration changes so repetitive build configuration does not exhaust the deep-review model timeout.

## Web-native Sprint and Release Gate flow

The normal business workflow does not require a Manager to open a terminal:

```text
Web Run Review / Sprint
  → inspect normal frontend/backend reports and deferred Company Config/SCR
  → Continue to Release Gate from the Sprint Job
  → confirm or enter the GIT_VERSION MR URL
  → Run Release Gate
  → inspect READY/BLOCKED, locked repositories, build resources and blockers
  → open the report online and make the release decision
```

Sprint Review records Company Config/SCR as deferred and excludes those build resources from the ordinary code prompt. If discovery finds a GIT_VERSION MR, the completed Job offers `Continue to Release Gate`; otherwise, Manager can enter the full MR URL directly in the Release Gate workspace.

Release Gate uses the same Web Job queue as other reviews and supports progress, pause, stop, retry, report preview, and history recovery. The server restricts it to Manager and verifies before the LLM call that the MR contains versioned `git_version.yml`/`build.yml` resources.

- `READY`: deterministic checks found no lock/resource error and final analysis found no configured Critical/High blocker; Manager must still read the report.
- `BLOCKED`: source/build locks, locked resources, SCR/database payload, an LLM blocking finding, or another gate check failed; fix and rerun in Web.
- Job `FAILED`: invalid non-GIT_VERSION input, authentication/network failure, or execution error; this is distinct from business `BLOCKED`.

`python review.py --mr-url <GIT_VERSION MR>` remains available for automation and troubleshooting, but is no longer required for the normal Web release workflow.

## Pending Jira and ADF

The canonical description is ADF JSON (`version: 1`, `type: doc`). It is validated by the API and can be edited or previewed. Supported nodes include paragraphs, headings, panels, code blocks, tables, ordered/unordered lists, Expand, Nested Expand in table cells, and media.

An Expand can contain tables, ordered lists, unordered lists, and screenshots. Screenshot uploads are stored as draft attachments and emitted as `mediaSingle/media` nodes. A future JiraReviewer integration must upload attachments to Jira first and replace the local media IDs with Jira Media Services identifiers before creating the real Issue.

The constraints follow Atlassian's official [ADF document structure](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/), [Expand node](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/expand/), [Nested Expand node](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/nestedExpand/), and [Media node](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/media/). `nestedExpand` is restricted to `tableCell`/`tableHeader`; normal `expand` is top-level.

The React/Vite island under `frontend/adf-editor` uses Atlaskit's editor and renderer. The REST API and stored ADF remain framework-neutral for the planned Flutter/Dart client.

## Storage and migration

The default workflow database is `data/codereviewer.db` using SQLite WAL and foreign keys. Override with:

```text
CODEREVIEWER_DB_FILE=/var/lib/codereviewer/data/codereviewer.db
WEB_USERS_FILE=/var/lib/codereviewer/data/web_users.json
WEB_THREADS_DIR=/var/lib/codereviewer/data/web_threads
JIRA_DRAFT_ATTACHMENTS_DIR=/var/lib/codereviewer/data/jira_draft_attachments
```

Run the idempotent migration locally:

```powershell
py -V:Astral/CPython3.14.6 tools\migrate_workflow_data.py
```

It backs up users, `review_history.jsonl`, and legacy Web threads, hashes legacy plaintext passwords, registers historical Review Runs/Findings, and imports existing handling/discussion/Pass records.

SQLite is accessed through the workflow repository boundary. A future MongoDB or purpose-specific implementation can replace the repository without changing Web/Flutter API contracts.

## Local acceptance checklist

1. Log in with each trial Developer and confirm Run Review is not visible.
2. Confirm `gerhard.guo`/`bryan.tan` see only `wen.yi`, `vincentgr.wang` sees only `kevin.tan`, and `benyq.feng` sees only `kelvinh.wu`.
3. Submit each handling type.
4. Verify Developer Not an issue requires Leader approval.
5. Verify fixed Critical/High cannot Pass before a clean Re-scan.
6. Create a Jira follow-up, edit/preview its ADF, add Expand → table/list/screenshot, and reopen it from Pending Jira.
7. Verify only Manager sees and can use Manager Exception.
8. Discuss an Issue/Run and reopen the history.
9. Verify Auditor can Pass only responsible Issues and Manager can view all.
10. Confirm no connection or deployment to `192.168.3.78` occurred.
11. As Manager, run Sprint Review and hand off from the completed Job to Release Gate.
12. Verify an ordinary MR is rejected and a GIT_VERSION MR displays READY/BLOCKED and resource metrics.
