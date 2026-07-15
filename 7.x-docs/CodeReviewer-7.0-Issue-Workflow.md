# CodeReviewer 7.0 Issue Review Workflow

> Language: English | [中文版](CodeReviewer-7.0-Issue-Workflow.zh-CN.md)

Version: 7.0.0  
Development branch: `20260714`  
Acceptance URL: `http://127.0.0.1:8765`  
Production deployment: explicitly excluded until local acceptance is approved.

## Roles and data scope

| Function | Developer | Auditor / Leader | Manager | Remarks |
| --- | --- | --- | --- | --- |
| View assigned Issue Reviews | Yes | Yes | Yes | Developer and Auditor are scoped by `responsible`; Manager sees all. |
| Run one Issue Review | - | Yes | Yes | Hidden for Developer and rejected by the API. |
| Run Sprint/Filter Review | - | - | Yes | Release management workflow. |
| Submit finding handling | Yes | Yes | Yes | Fixed, Jira follow-up, or Not an issue. |
| Approve Not an issue | - | Yes | Yes | Developer submissions remain pending until approved. |
| Re-scan | - | Yes | Yes | Fixed blockers are not cleared until absent from a later Run. |
| Manager Exception | - | - | Yes | Requires a Pending Jira draft and an audit reason. |
| Manual Pass | - | Yes | Yes | Auditor remains restricted to owned responsible scope. |
| Discuss / Report Preview | Yes | Yes | Yes | Discussion is associated with Issue, Run and optional Finding. |

Trial Developer mapping:

| Responsible | Developer accounts |
| --- | --- |
| `wen.yi` | `gerhard.guo`, `bryan.tan` |
| `kevin.tan` | `vincentgr.wang`, `kelvinh.wu` |

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
2. Confirm `gerhard.guo`/`bryan.tan` see only `wen.yi` scope and `vincentgr.wang`/`kelvinh.wu` see only `kevin.tan` scope.
3. Submit each handling type.
4. Verify Developer Not an issue requires Leader approval.
5. Verify fixed Critical/High cannot Pass before a clean Re-scan.
6. Create a Jira follow-up, edit/preview its ADF, add Expand → table/list/screenshot, and reopen it from Pending Jira.
7. Verify only Manager sees and can use Manager Exception.
8. Discuss an Issue/Run and reopen the history.
9. Verify Auditor can Pass only responsible Issues and Manager can view all.
10. Confirm no connection or deployment to `192.168.3.78` occurred.
