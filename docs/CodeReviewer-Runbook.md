# CodeReviewer Runbook

## Purpose

CodeReviewer reviews GitLab merge requests by combining:

- GitLab MR diff/context fetching
- GitNexus-style report storage and review history
- Rule-based review
- LLM review with Codex GPT 5.5 first, then CC Switch Claude code opus fallback
- Jira issue and sprint context from Jira plus local Jira/PRD data
- Local working-copy context for iTrade Client, WVAdmin, DPS9, and DPS11

## Network Facts

- GitLab `gitlab.tx-tech.com` is reachable through the VPN DIRECT rule.
- Codex can run in the same network session.
- CC Switch fallback defaults to the `Claude code opus` provider. DeepSeek V4 Pro remains available through CC Switch when explicitly selected.
- GitNexus storage is local file-based storage for generated reports and review history.

Direct Jira/MR/sprint review is now the default path.

## Usage Case Summary

### 1. Review One Jira Issue

Use this when the review starts from an `ECHNL` Jira issue. CodeReviewer fetches the issue, discovers related GitLab MRs from Jira fields, Jira remote links, Jira development details, GitLab search, and issue-key branch discovery across projects from `D:\TTL\vibe-coding\CodeReviewer\config.yml`.

```bash
python review.py --jira ECHNL-8888 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml

python review.py --jira ECHNL-8888 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --git-tools-groups=dps11-repository,wvadmin-repository,itrade-client
```

Review several Jira issues in one CLI batch with an English comma. Reports and resume checkpoints remain isolated per issue:

```bash
python review.py --jira ECHNL-8888,ECHNL-8889,ECHNL-8890 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

List discovered projects and MRs first:

```bash
python review.py --jira ECHNL-8888 --jira-mr-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml

python review.py --jira ECHNL-8888,ECHNL-8889 --jira-mr-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

The report combines Jira issue context, linked SVREQ/PRD context from `D:\TTL\jira-prd\data`, MR diffs, local working-copy context, Drupal/DPS review rules, and the detailed `ECHNL-5539.md` style guide. When the Jira issue has multiple related MRs, CodeReviewer performs one consolidated review across all discovered MRs. The `Related MRs` table shows each MR's GitLab request initiator (`Request By`) separately from the configured Responsible owner. If those MRs belong to different responsible owners, it saves one split report per responsible owner. Each split report keeps only that owner's `Related MRs`, changed files, findings, and file diffs, with changed files prefixed as `<gitlab project>!<mr iid>/<file>` so developers can trace every finding back to the right project diff.

In the Web UI, starting a single Jira issue review checks the current output directory first. If reports for that Jira key already exist, the UI shows the existing report links and requires two-step confirmation before creating another review job.

Report History supports online Markdown preview. Click a report name or `Preview` to render it inside the Web UI; use `Raw`, `Download`, or `Handling` only when the source Markdown or handling-result template is needed. The preview renders report diff anchors and collapsible `details` sections, and supports a maximize/restore wide-view modal for reading larger diffs without changing font size. In the maximized modal, use `Previous` / `Next` or left/right arrow keys to switch reports without closing the modal.

### 2. Review One Sprint

Use this before release or merge approval. CodeReviewer loads all reviewable Jira issues in the sprint, discovers related Open MRs by default, and produces detailed developer-facing reports. Findings are written with reproduction, impact, fix, and verification guidance so developers can adjust quickly and CodeReviewer can later support self-guarded auto-fix workflows.

```bash
python review.py --sprint=10068 --jira-project ECHNL --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml

python review.py --sprint=10068 --jira-project ECHNL --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --review-framework=drupal
```

List sprint MRs first:

```bash
python review.py --sprint=10068 --jira-project ECHNL --sprint-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

### 3. Review One Jira Filter

Use this when a saved Jira filter defines the release or audit issue scope. CodeReviewer loads all reviewable issues returned by the filter and reviews each issue through the same consolidated MR workflow as Sprint review.

```bash
python review.py --jira-filter=12345 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

List filter-linked MRs first:

```bash
python review.py --jira-filter=12345 --jira-filter-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

Jira issue, Jira filter, and Sprint review only process issues whose Jira status is `Development Done` by default. Configure multiple reviewable statuses, or disable the gate intentionally:

```bash
set JIRA_REVIEW_ALLOWED_STATUSES=Development Done,Ready for Release
set JIRA_REVIEW_ALLOWED_STATUSES=all
```

Detailed report style:

```bash
set REVIEW_TEMPLATE_PATH=D:\TTL\vibe-coding\CodeReviewer\docs\ECHNL-5539.md
set REVIEW_TEMPLATE_MAX_CHARS=16000
```

### 4. Resume Interrupted Batch Review

Batch checkpoints are enabled by default for sprint, Jira issue, reviewer MR, and local issue-branch review. If the terminal is closed or `Ctrl+C` is pressed, re-run the same command with the same `--output-dir`; CodeReviewer skips completed items and retries the unfinished item.

For Web review jobs, use `Pause`, `Resume`, and `Stop` on the job card. The controls are per job and cooperative: they apply at the next safe checkpoint, and stopped jobs are marked as `canceled`.
Refreshing the browser does not clear server-side Web job progress; the page restores visible jobs and continues polling active jobs when it loads again.

State files are stored under:

```text
<output-dir>\.code_reviewer_resume\
```

During the run, look for these terminal lines:

```text
Batch: sprint consolidated Jira reviews; total=19
Output dir: D:\TTL\code-review\e-channel-sprint20260710
Resume state: D:\TTL\code-review\e-channel-sprint20260710\.code_reviewer_resume\sprint-mrs-xxxx.json
[1/19] DONE ECHNL-5630 (1 MR(s)) -> report: D:\TTL\code-review\e-channel-sprint20260710\owner\ECHNL-5630_pass.md (Critical=0 High=0 Medium=0 Low=0; findings=0)
[2/19] FAILED ECHNL-5629: <error>
[3/19] SKIP DONE ECHNL-5624 (1 MR(s)) -> report: D:\TTL\code-review\e-channel-sprint20260710\owner\ECHNL-5624_pass.md (Critical=0 High=0 Medium=0 Low=0; findings=0)
```

Useful controls:

```bash
python review.py --sprint=10068 --output-dir=D:\TTL\code-review\e-channel-sprint20260710
python review.py --sprint=10068 --output-dir=D:\TTL\code-review\e-channel-sprint20260710 --no-resume
python review.py --sprint=10068 --output-dir=D:\TTL\code-review\e-channel-sprint20260710 --reset-resume
```

## Recommended Workflow

### 1. Check Network

```bash
cd /d D:\TTL\vibe-coding\CodeReviewer
python review.py --network-check
```

Use the recommendation printed by the command to choose the next step.

### 2. Direct Review MR

Run this when GitLab and Codex are both reachable.

```bash
set LLM_NETWORK_MODE=direct
set LLM_PROVIDER=auto
set LLM_CODEX_MODEL=gpt-5.5
set "LLM_CC_SWITCH_PROVIDER=Claude code opus"
python review.py --mr-url "https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/momd/-/merge_requests/270" --context-repo D:\TTL\vibe-coding\momd
```

`auto` review order:

1. Codex CLI with GPT 5.5
2. CC Switch Claude code opus fallback
3. Rule-based findings are always preserved

`--context-repo` adds local full-project context to the LLM review. The prompt includes a bounded repository tree sample, changed files, direct dependencies, and relevant framework descriptors. Generated reports, CI files, README files, source maps, and minified assets are excluded unless directly relevant to the changed code.

Prompt size limits:

```bash
set PROJECT_CONTEXT_MAX_CHARS=80000
set PROJECT_CONTEXT_MAX_TREE_FILES=500
set PROJECT_CONTEXT_MAX_FILE_CHARS=12000
```

## Common Commands

### Report Language and Clickable Findings

Reports default to Simplified Chinese:

```bash
set REPORT_LANGUAGE=zh-CN
```

Override per CLI run when needed:

```bash
python review.py --mr-url "<gitlab-mr-url>" --report-language en
```

Each changed-file name and finding location links to the report's file diff section. When MR metadata is available, the change summary also includes a Code link to GitLab. Each finding keeps a collapsible related diff snippet for quick inspection.

Reports default to `Medium` and above findings only:

```bash
set REPORT_MIN_SEVERITY=Medium
python review.py --jira ECHNL-8888 --report-min-severity High
python review.py --jira ECHNL-8888 --report-min-severity Low
```

Use `High` for a stricter release gate, or `Low` / `Warning` when you intentionally want improvement and clarification items in the output. The report records the effective threshold and how many findings were filtered out.

For DPS9/DPS11 backend database changes, review `db_change.scr` and referenced SQL/shell/resource files as the database-change authority. Do not judge these GIT_VERSION-delivered database updates by Drupal update-hook/install-schema expectations. Check command ordering, referenced file existence, previous DB version alignment, idempotency/rerun safety, rollback/backup expectation, and environment scope.

For DPS `state_config.yml` / `state_config.<env>.yml`, including historical `state_cofig.<env>.yml` spelling, token and encryption values are expected environment configuration. Report them only when there is concrete evidence of invalid environment scoping, broken reference/encryption format, cross-environment leakage, or real leaked credentials outside the centralized config mechanism.

### Batch Review MRs by Reviewer

Default reviewer and lookback can be configured by environment variables:

```bash
set REVIEWER_EMAIL=jay.wince@tx-tech.com
set REVIEWER_LOOKBACK_DAYS=7
set REVIEWER_MR_STATE=opened
set REVIEWER_MR_LIMIT=100
```

Query and review every MR for the reviewer in the recent window:

```bash
python review.py --reviewer-mrs
```

List matching MRs first:

```bash
python review.py --reviewer-mrs --reviewer-list-only --reviewer-limit 20
```

By default, reviewer batch review only processes Open MRs. Override reviewer, window, or state when you intentionally need another scope:

```bash
python review.py --reviewer-mrs --reviewer jay.wince@tx-tech.com --reviewer-days 7 --reviewer-state all
```

### Batch Review MRs by Jira Sprint

Use this when Jira and GitLab are integrated and Jira issues expose GitLab MRs through remote links or the Jira development panel.

```bash
python review.py --sprint=10068 --jira-project ECHNL --context-repo=D:\TTL\wvplaform\dps11.2.83
```

For Drupal/DPS review limited to DPS9 and DPS11 projects from git-tools config:

```bash
python review.py --sprint=10068 --jira-project ECHNL --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --git-tools-groups=dps9-repository,dps11-repository --review-framework=drupal
```

Discovery order:

- Jira sprint query: `project = ECHNL AND sprint = 10068`
- Jira issue fields, remote links, and development details for GitLab MR URLs
- Configured GitLab projects are searched for issue-key branches, including web-style branches such as `feature/ECHNL-8888` and DPS layer branches such as `feature/DAO#ECHNL-8888`, `feature/BIZ#ECHNL-8888`, `feature/CLI#ECHNL-8888`, and `feature/API#ECHNL-8888`
- GitLab global MR search by issue key when Jira has no MR link and `SPRINT_MR_GITLAB_SEARCH_FALLBACK=1`
- Historical MR supplementation when `SPRINT_MR_HISTORY_DISCOVERY=1`; this runs even when Jira already links the latest MR, then excludes configured development-version targets.

List only before running the full review:

```bash
python review.py --sprint=10068 --sprint-list-only --sprint-limit 50
```

Fast list mode for quick discovery checks:

```bash
python review.py --sprint=10068 --sprint-list-only --sprint-fast-list --sprint-limit 50
```

Useful defaults:

```bash
set JIRA_PROJECT_KEY=ECHNL
set JIRA_SEARCH_API=auto
set JIRA_PRD_CONTEXT=auto
set JIRA_PRD_DATA_DIR=D:\TTL\jira-prd\data
set JIRA_PRD_CONTEXT_MAX_CHARS=20000
set JIRA_PRD_CONTEXT_PER_ISSUE_CHARS=6000
set JIRA_PRD_CONTEXT_MAX_ISSUES=8
set JIRA_SPRINT_MAX_ISSUES=500
set SPRINT_MR_STATE=opened
set SPRINT_MR_LIMIT=200
set SPRINT_MR_GITLAB_SEARCH_FALLBACK=1
set SPRINT_MR_HISTORY_DISCOVERY=1
set SPRINT_MR_HISTORY_LIMIT=100
set SPRINT_JIRA_REMOTE_LINK_DISCOVERY=1
set SPRINT_JIRA_DEV_PANEL_DISCOVERY=1
set SPRINT_BRANCH_DISCOVERY=1
set SPRINT_BRANCH_DISCOVERY_MODE=missing-only
set GIT_TOOLS_CONFIG=D:\TTL\vibe-coding\CodeReviewer\config.yml
set GIT_TOOLS_GROUPS=
set GIT_TOOLS_REQUIRE_MR_MATCH=0
set SPRINT_GITLAB_PROJECTS=
set SPRINT_BRANCH_PROJECT_LIMIT=200
set SPRINT_BRANCH_SEARCH_LIMIT=20
set SPRINT_BRANCH_MR_LIMIT=20
set REVIEW_FRAMEWORK=drupal
set DRUPAL_SKILL_PATH=C:\Users\xuejie.xiao\.codex\skills\drupal-framework
set DRUPAL_SKILL_CONTEXT_MAX_CHARS=12000
```

By default, Sprint and Jira issue consolidated review process Open and Merged MRs (`SPRINT_MR_STATE=opened,merged`). Use `--sprint-state all` or `SPRINT_MR_STATE=all` only when the release audit intentionally needs Closed/Locked MRs.

`GIT_TOOLS_CONFIG` is the primary ECHNL GitLab project source and includes iTrade Client, Services Terminal, WVAdmin, DPS9, DPS11, and build repositories. CodeReviewer automatically normalizes the MR URL project path and checks it against this config because both values represent the same GitLab origin. Reports show `GitLab Project Match` as `matched <group>/<module>` or `unmatched`. Set `GIT_TOOLS_REQUIRE_MR_MATCH=1` for release-gate runs that should fail when an MR is outside the configured repositories. `SPRINT_GITLAB_PROJECTS` is optional; use it only for extra projects not present in git-tools config.

`JIRA_PRD_DATA_DIR` points to the local Jira/PRD issue mirror. During review, CodeReviewer reads matching local issue Markdown/JSON files, such as `ECHNL-8888.md`, plus linked local issue docs when available, and injects them as requirement context before the LLM evaluates the diff.

If Jira returns `410` for `/rest/api/3/search`, keep `JIRA_SEARCH_API=auto` or set `JIRA_SEARCH_API=enhanced`; CodeReviewer will use Jira Cloud `/rest/api/3/search/jql` with `nextPageToken` pagination.

### Direct MR Review

Use this as the default when GitLab and the selected LLM provider are both reachable.

```bash
python review.py --mr-url "<gitlab-mr-url>"
```

### Local Working Copy Review

Use this when each GitLab project has a local working copy. This is the preferred path for detailed reviews because CodeReviewer can combine:

- GitLab MR or local branch diff
- Jira REST authoritative issue data plus optional Rovo Jira/Confluence/Teamwork Graph candidate context
- Local full-project context from the matching working copy
- Drupal/DPS review rules where applicable
- The detailed `ECHNL-5539.md` report style guide

Define `repository_url`, `branch`, and `local_working_copy` for every repository in `D:\TTL\vibe-coding\CodeReviewer\config.yml`. This is the primary mapping for iTrade Client, Services Terminal, WVAdmin, DPS9, DPS11, and build repositories.

`branch` and `branches` accept both exact values and version wildcards. Exact
values such as `7.5.1.38` remain compatible. A wildcard such as `7.5.1.*` is
resolved against remote heads and only the greatest numeric version is used.
When multiple version lines are configured, each pattern selects one branch;
for example `[7.5.0.*, 7.5.1.*]` synchronizes the latest 7.5.0 and 7.5.1
branches. A missing match is reported as a configuration error instead of
silently selecting an unrelated branch.

Useful defaults:

```bash
set LOCAL_CONTEXT_AUTO=1
set REPOSITORY_SYNC_REQUIRED=1
set CODEBASE_MEMORY_ENABLED=1
set REVIEW_TEMPLATE_PATH=D:\TTL\vibe-coding\CodeReviewer\docs\ECHNL-5539.md
set REVIEW_TEMPLATE_MAX_CHARS=16000
```

Synchronize all configured repositories and update the persistent code graph:

```powershell
python review.py --sync-repositories
```

For fetch-only maintenance:

```powershell
python review.py --sync-repositories --sync-no-index
```

The checked-in provider boundary is Rovo-first and read-only for knowledge:
Jira REST remains authoritative for issue fields and the only Jira write
provider; Rovo retrieves candidate Jira, Confluence, and Teamwork Graph
context. `app.knowledge.local_jira_prd_enabled` is `false`, so CodeReviewer
does not scan `D:\TTL\jira-prd` or build local PRD/RAG context. If Rovo
credentials or the JiraReviewer adapter are unavailable, enrichment is skipped
and the code review continues with Jira REST and GitLab evidence.

Review-time synchronization is strict for the MR target branch. If GitLab has deleted that branch but the MR still provides an immutable `diff_refs.base_sha`, CodeReviewer records the missing branch as a synchronization warning and builds project context from the exact base commit instead of failing the review. Bulk synchronization records a fallback and fetches all remote refs when a product version branch is not present in one split repository. Local modifications are never reset or overwritten; only a clean working tree already checked out on the configured branch is fast-forwarded.

`codebase-memory-mcp` indexes an exact target-branch snapshot with repository artifact persistence disabled, so it does not add `.codebase-memory` files to source repositories. Its local SQLite knowledge graph supplies architecture and changed-file dependency context to the LLM prompt.

CodeReviewer permits only one `index_repository` process across all Web jobs and application processes. If another index is active, the new request skips graph enrichment instead of starting a second memory-heavy process. Repositories exceeding `CODEBASE_MEMORY_MAX_REPOSITORY_FILES` (default `50000` Git-tracked files) are also skipped. `CODEBASE_MEMORY_TIMEOUT_SECONDS` defaults to `120`; a timed-out indexer and its child process tree are terminated. These safeguards affect graph enrichment only and never block the underlying MR review.

### Remote Linux Codebase Memory over SSH

When CodeReviewer runs on Windows but the Linux binary is installed on another host, configure remote CLI mode instead of `CODEBASE_MEMORY_COMMAND`:

```dotenv
CODEBASE_MEMORY_SSH_HOST=192.168.3.78
CODEBASE_MEMORY_SSH_USER=root
CODEBASE_MEMORY_SSH_CONFIG=D:\TTL\devops\config.yml
CODEBASE_MEMORY_SSH_STRICT_HOST_KEY=1
CODEBASE_MEMORY_REMOTE_COMMAND=/root/.local/bin/codebase-memory-mcp
CODEBASE_MEMORY_REMOTE_SOURCE_ROOT=/var/lib/codebase-memory-mcp/sources
CODEBASE_MEMORY_REMOTE_STATE_ROOT=/var/lib/codebase-memory-mcp/codereviewer-state
```

The SSH config file may provide `user` plus `password` or the legacy `passphase` field for the selected host. Credentials are read in memory and are not copied into `.env`. Prefer a dedicated non-root account and SSH key for production. Strict host-key validation is enabled by default and uses `%USERPROFILE%\.ssh\known_hosts` unless `CODEBASE_MEMORY_SSH_KNOWN_HOSTS` is set.

Remote mode creates a compressed `git archive` for the exact local target commit, uploads it to the remote source root, runs the native CLI on that snapshot, and stores a project/commit ready marker. Port `9749` is not required; it is only the optional graph UI. Verify the connection without indexing:

```powershell
python tools\check_codebase_memory.py
```

MR review with automatic local context:

```bash
python review.py --mr-url "<gitlab-mr-url>" --jira ECHNL-8888 --review-framework=drupal
```

Review local branches matching a Jira issue across configured working copies:

```bash
python review.py --issue-branches --jira ECHNL-8888 --target-branch develop --git-tools-groups=dps11-repository,wvadmin-repository,itrade-client
```

List matching local branches first:

```bash
python review.py --issue-branches --issue-branch-list-only --jira ECHNL-8888 --target-branch develop
```

### Review a Local Git Diff

```bash
python review.py --repo D:\TTL\vibe-coding\dps11 --source-branch ECHNL-8888 --target-branch dps11 --project dps11
```

### Review a Saved Diff File

```bash
python review.py --diff-file examples\sample.diff --project dps11 --jira ECHNL-8888
```

### Review a GIT_VERSION MR

GIT_VERSION MRs update `git_version*.yml` and/or `build*.yml` to lock development repositories and build-code repository commits.

```bash
set WEB_BUILD_TOOLS_DIR=D:\TTL\vibe-coding\web-build-tools
python review.py --mr-url "<gitlab-mr-url>" --context-repo D:\TTL\vibe-coding\<project>
```

During review, CodeReviewer fetches:

- Every source repository commit defined in `git_version.yml`
- When available, the previous `git_version-v*.yml` from the same build-resource directory and the previous-to-current source repository compare diff
- The build-code repository commit defined in `build.yml`
- Commit title/message issue keys for Jira/SVREQ traceability
- Commit diffs for source code, config, and build resources
- Required build resources at the locked build-code commit (`build.yml` and the referenced `git_version.yml`)

Release-gate routing:

- `Company_Config` / version-suffixed Company Config branches and DPS `SCR` branches are deferred from ordinary Jira/Sprint review. Their full resource effect is reviewed from the build repository commit locked by GIT_VERSION.
- `GIT_VERSION` branches must use explicit MR mode (`python review.py --mr-url "<GIT_VERSION MR URL>"`). Jira/Sprint/Filter consolidation records them as `SKIP BRANCH-TYPE ... required_review_mode=mr` rather than mixing them with the related Jira issue's ordinary MR review.
- Remote-link-only MRs are hydrated for source/target branch metadata before a full diff is downloaded, so a large Company Config resource is not needlessly added to ordinary review context.

For DPS build repositories, CodeReviewer reads the authoritative runtime scripts from the exact build commit locked by `build.yml`:

- `company/SV/script/DPSBuild.php` is always required for the DPS release gate.
- `company/SV/script/DBChangeParser.php` and `company/SV/script/db_change.yml` are required only when the locked payload contains `database/` content or `db_change.scr`.
- `db_change.scr` is checked for the `DBChangeParser v1.2.2` header format, duplicate module/version/company/environment blocks, and referenced SQL/JS/PHP/shell resources.
- `database/db_change.scr.sha` is intentionally not required during pre-build review because DPSBuild generates it during package construction; verify it during post-build package validation instead.
- Do not use a local machine copy of DPSBuild or DBChangeParser as a fallback. Non-DPS Web build repositories do not receive DPS-specific script checks.

The report includes `GIT_VERSION 摘要`:

- Locked development repositories from `git_version.yml`
- Branch and commit/tag per project
- Build repository branch and locked commit from `build.yml`
- Version, referenced `git_version.yml`, company list, environments
- Fetched locked repository code review table
- Previous-to-current compare information for locked source repositories
- `locked_source/...` and `locked_build/...` file diffs

Review checks include duplicate YAML keys, empty/invalid commit locks, build repository self-lock commit, bh/build-history based patch version derivation, version-file suffix alignment, release-note-to-locked-source traceability, and build company/environment scope. The build repository self-lock commit does not need to equal the current MR head; it should point to a commit after the required build resources were pushed, and CodeReviewer verifies those resources exist at the locked commit.

### Web Build Script Facts

These facts are derived from `D:\TTL\vibe-coding\web-build-tools\documents` and the resource scripts under `D:\TTL\vibe-coding\web-build-tools\resources`.

- `source_provide_method` supports `clone`, `base_version`, and `in_place`.
- Relative `version.git_version` paths are resolved from the `build.yml` version directory; absolute paths are also supported.
- Patch/package versions are derived from `*.bh` build history files. If `<ver_number>.bh` is missing or empty, the package version is the base version; otherwise the next version increments the last matching patch number.
- Build history rows are written as `packageVersion, appName, developmentGitCommit, buildGitCommit, buildStart, buildEnd, buildDuration`.
- `CreateGIT_VERSIONMR.js` uses a three-commit flow: add release resources, update `build.yml` commit to the first commit, then finalize `build.yml` commit to the second commit. So a valid `build.yml` commit may point to the previous build-resource commit, not the MR head.
- For iTrade/Services release notes, an `SV` item means the build companies are read from the selected build template; otherwise company-specific release-note keys drive the generated `companies` list.
- When a build/development commit is configured, `build.js` fetches that exact commit and switches the configured branch to it.
- iTrade `build.js` intentionally reads `replace_with_ttl.companines`; this is a compatibility key, not a typo unless the script changes.
- Release resource copy intentionally excludes `build*.yml`, `git_version*.yml`, release notes, and `*.bh` files from company directories.

## Branch and Jira Mapping Rules

Local Diff commit retrieval follows this order:

1. Fetch the configured target branch when the MR base commit is missing.
2. Fetch GitLab `refs/merge-requests/<MR IID>/head` when the MR head commit is missing.
3. Fetch the known source branch, then exact base/head SHA objects if still needed.
4. Only when exact refs fail and a valid Jira key is available, resolve existing standard and DPS branch-name candidates with `git ls-remote` and fetch the matching branch.

MR IID and Jira key are never interchangeable. For example, MR IID `280` identifies `refs/merge-requests/280/head`; Jira key `ECHNL-5658` identifies branches such as `improvement/ECHNL-5658` or `feature/API#ECHNL-5658`.

- `SVREQ` is the PRD/requirement Jira space.
- GitLab development branches map to action/development Jira spaces such as `ECHNL`.
- Do not set GitLab development project mappings such as iTrade, WVAdmin, or DPS to `jira_project_key="SVREQ"`.

Web projects use:

```text
feature/ECHNL-8888
bug/ECHNL-6666
task/ECHNL-6668
improvement/ECHNL-6669
change-request/ECHNL-6667
```

DPS middle-office backend projects use:

```text
feature/API#ECHNL-8888
feature/DAO#ECHNL-8888
feature/BIZ#ECHNL-8888
feature/CLI#ECHNL-8888
```

Legacy branches such as `DAO#ECHNL-8888` or `DAO-ECHNL-8888` can still be detected, but new branches should follow the current format.

### Save to a Specific Report Directory or File

By default, generated reports are saved under `D:\TTL\code-review\e-channel-sprintYYYYMMDD`, where `YYYYMMDD` is the final actual working day in the current Monday-Sunday cycle using the mainland China holiday/adjusted-workday calendar. Adjusted Saturday or Sunday workdays become the cycle end; holidays can move it earlier. If the entire cycle is non-working, the most recent actual workday is used. Set `REPORT_OUTPUT_BASE_DIR` to change only the base folder, or set `REPORT_OUTPUT_DIR` / `--output-dir` for an exact directory.

The bundled `data/china-mainland-work-calendar.json` contains official non-working dates and adjusted weekend workdays. Refresh it annually from the State Council holiday notice. To use a centrally maintained calendar, set `CHINA_WORK_CALENDAR_FILE`; its JSON shape is `{\"holidays\": [\"YYYY-MM-DD\"], \"workdays\": [\"YYYY-MM-DD\"]}`. Missing or invalid calendar data safely falls back to normal Monday-Friday weekdays.

```bash
set REPORT_OUTPUT_BASE_DIR=D:\TTL\code-review
python review.py --mr-url "<gitlab-mr-url>"

set REPORT_OUTPUT_DIR=D:\TTL\review-reports
python review.py --mr-url "<gitlab-mr-url>"

python review.py --mr-url "<gitlab-mr-url>" --output-dir D:\TTL\review-reports

python review.py --mr-url "<gitlab-mr-url>" --output reports\my-review.md
```

By default, generated filenames are saved under `<output-dir>/<responsible>/`, where `responsible` comes from `D:\TTL\vibe-coding\CodeReviewer\config.yml`. Example:

```text
D:\TTL\code-review\e-channel-sprint20260710\wen.yi\ECHNL-5592_has-issue-medium.md
```

Set `REPORT_GROUP_BY_RESPONSIBLE=0` for flat output directly under the output directory. If a consolidated Jira issue contains MRs owned by different responsible people, CodeReviewer saves separate responsible-specific reports, such as `kevin.tan\ECHNL-5308_has-issue-medium.md` and `wen.yi\ECHNL-5308_has-issue-high.md`.

`--output` controls the filename, full file path, or an output directory. If `--output` points to a directory, the default report filename is written inside that directory. If `--output-dir` is also provided, the directory comes from `--output-dir` and the filename comes from `--output`.

### Development-Version Branch Filtering

For Jira and sprint consolidated review, CodeReviewer excludes MRs whose target branch is a development-version branch. `CodeReviewer/config.yml` can define explicit branches:

```yaml
dev_branch:
  - "ITRADE_CLIENT_7.5.0"
  - "ITRADE_CLIENT_7.5.1"
```

When `dev_branch` is omitted, the default dev branch is the uppercase project key/module, for example `services-terminal -> SERVICES-TERMINAL`. Excluded MRs are reported in `excluded_dev_branch_mrs` and printed as `SKIP DEV-BRANCH`.

### Temporary Branch-Type Filtering

For daily Jira and sprint review, CodeReviewer routes MR source branches using `app.review.release_gate.branch_prefixes`. The default routing is:

```text
Company Config / DPS SCR -> defer to GIT_VERSION release gate
GIT_VERSION -> explicit MR-mode release-gate review
```

This rule is case-insensitive and matches a configured prefix plus optional version suffix. For example, `Company_Config/ECHNL-1234`, `DPS11_Config-1.4.74`, and `DPS11_SCR-1.4.74` are deferred; `GIT_VERSION/ECHNL-1234` and `DPS11_GIT_VERSION-1.4.74` are directed to explicit MR mode. Deferred MRs remain visible in `excluded_branch_type_mrs`, Web progress, and the ordinary Jira report's `Other` tab so the GIT_VERSION gate can be followed up.

Although Company Config/SCR MRs are excluded from ordinary code findings, CodeReviewer retains or fetches their changed-file paths for Jira involved-file validation. A Jira-expected file found in one of these deferred MRs is shown as matched at the GIT_VERSION release gate rather than incorrectly reported as missing. Genuine missing paths and files absent from the Jira list remain in the blocking mismatch finding. The `Deferred Build Resources` table lists the associated changed files for traceability.

Auto-generated report filenames include status:

```text
<project>_<mr>_<jira>_pass.md
<project>_<mr>_<jira>_has-issue-critical.md
<project>_<mr>_<jira>_has-issue-high.md
<project>_<mr>_<jira>_has-issue-medium.md
<project>_<mr>_<jira>_has-issue-low.md
```

### Post to GitLab

GitLab writeback is never silent. Review the report first, then explicitly confirm.

```bash
python review.py --mr-url "<gitlab-mr-url>" --post-gitlab-comment --yes
```

Run writeback only when GitLab is reachable through the current network or DIRECT rule.

## Web UI

Start a local web UI:

```bash
python web.py --host 127.0.0.1 --port 8768
```

Use LAN mode when other computers need access:

```bash
python web.py --lan --port 8768
```

`127.0.0.1` is local-only. LAN mode binds to `0.0.0.0` and prints reachable LAN URLs. If `http://192.168.x.x:8768` is still unavailable from another computer, open the port in Windows Firewall or verify both machines are on the same network.

Open:

```text
http://127.0.0.1:8768
```

The Web UI requires login. Usernames are responsible owner names from `CodeReviewer/config.yml`, such as `wen.yi`, `kevin.tan`, or `sunny.cheng`. Generated passwords are stored locally in:

```text
D:\TTL\vibe-coding\CodeReviewer\data\web_users.json
```

Passwords are random 8-character strong passwords containing uppercase, lowercase, digit, and special character. The login page includes a robot-prevention arithmetic challenge. Treat the credential file as sensitive local data and send only the relevant responsible user's password to that team leader.

The Web UI also enforces a client IP whitelist before login. Loopback IPs are allowed by default. For LAN access, configure one of:

```bat
set WEB_IP_WHITELIST=192.168.3.170,192.168.3.0/24
python web.py --lan --port 8768 --allow-ip 192.168.3.*
```

Alternatively, create:

```text
D:\TTL\vibe-coding\CodeReviewer\data\web_ip_whitelist.txt
```

The whitelist supports exact IPs, CIDR ranges, and simple wildcard entries. Only set `WEB_TRUST_PROXY=1` when CodeReviewer is behind a trusted reverse proxy that controls `X-Forwarded-For` or `X-Real-IP`.

Web UI features:

- Check Network
- Run review from GitLab MR URL
- Review local repo or pasted diff
- Add local full-project context for MR/diff review
- Preview Markdown report
- View report history and runtime config
- Configure report output directory per run
- Download a single Markdown report
- Download all Markdown reports under one responsible-owner folder as a zip archive
- Responsible-login access control for reports and downloads

Use `Output Directory` to point the Web UI at a sprint report directory such as `D:\TTL\code-review\e-channel-sprint20260710`. The `Responsible Downloads` section lists owner folders and provides one zip download per owner, which can be sent to each team leader.

## Storage Locations

Generated reports:

```text
D:\TTL\code-review\e-channel-sprintYYYYMMDD by default, or REPORT_OUTPUT_DIR
```

GitNexus-style storage:

```text
data/gitnexus/
```

GitNexus report copy:

```text
data/gitnexus/reports/
```

GitNexus index:

```text
data/gitnexus/review_index.jsonl
```

Review history:

```text
data/review_history.jsonl
```

## Configuration

Use `.env` for local private configuration. Do not commit real tokens.

Important settings:

```bash
GITLAB_URL=https://gitlab.tx-tech.com
GITLAB_TOKEN=<gitlab-token>

LLM_PROVIDER=auto
LLM_NETWORK_MODE=auto
LLM_CODEX_MODEL=gpt-5.5
LLM_CODEX_TIMEOUT_SECONDS=300
LLM_REASONING_EFFORT=high
CODEX_CLI_PATH=
LLM_CC_SWITCH_PROVIDER=Claude code opus
LLM_TIMEOUT_SECONDS=180
LLM_MAX_RETRIES=3
LLM_MAX_DIFF_CHARS=30000
DPS_REVIEW_REQUIRE_CODEX=1

GITNEXUS_STORAGE_PATH=data/gitnexus
GITNEXUS_INDEX_FILE=review_index.jsonl
```

Preferred token management:

- GitLab token from `.env` or environment variables
- Claude code opus token/model/base URL from CC Switch
- Codex auth from local Codex CLI setup

## LLM Behavior

Default provider:

```bash
LLM_PROVIDER=auto
```

Auto mode behavior:

- If `LLM_NETWORK_MODE=non-vpn`, Codex is skipped by explicit override.
- If `LLM_NETWORK_MODE=direct`, `vpn`, or `auto`, Codex GPT 5.5 is tried first.
- Codex is called with `model_reasoning_effort="high"` by default.
- `LLM_SPEED=fast` maps Codex to `service_tier="priority"`; `LLM_SPEED=standard` uses the default service tier.
- `LLM_REQUIRE_SUCCESS=1` is the default. If all LLM providers fail, CodeReviewer exits instead of generating a static-rule-only report.
- `LLM_REQUIRE_STRUCTURED_OUTPUT=1` is the default. If the provider does not return valid JSON, CodeReviewer exits instead of converting free text into weak fallback findings.
- `LLM_REQUIRED_REASONING_EFFORT` and `LLM_REQUIRED_SPEED` are release-gate minimums. If the effective run is lower than either value, CodeReviewer exits before review.
- `LLM_TIMEOUT_SECONDS=180`, `LLM_CODEX_TIMEOUT_SECONDS=300`, and `LLM_MAX_RETRIES=3` are the default release-review execution policy.
- On Windows/PowerShell, keep `LLM_CODEX_DISABLE_FEATURE=shell_snapshot` unless a newer Codex CLI proves shell snapshots work reliably.
- If Codex fails, times out, or returns unusable output, Claude code opus from CC Switch is tried.
- If the GitLab project is DPS/DPS9/DPS11, `DPS_REVIEW_REQUIRE_CODEX=1` forces `codex-cli`; after the configured retries fail, CodeReviewer exits and does not fallback to CC Switch.
- If all configured LLM attempts fail, fix Codex/CC Switch/network settings and rerun. Do not approve from a rule-only report.
- If you use `CLIProxyAPI` or another local AI proxy, ensure it is started from a terminal session with the proxy environment variables already injected. For PowerShell:

```powershell
$env:HTTP_PROXY="socks5://127.0.0.1:10809"
$env:HTTPS_PROXY="socks5://127.0.0.1:10809"
$env:ALL_PROXY="socks5://127.0.0.1:10809"
```

- The `CLIProxyAPI` management UI may show the OAuth callback page, but backend token exchange can still fail with `unsupported_country_region_territory` if the request exits through an unsupported region or bypasses the proxy. In that case, the fix is to use a supported proxy/VPN exit and make sure the CLI backend process also uses it.
- If `cli-proxy-api.exe` is launched from a service/systemd unit, inject `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` into the service environment instead of relying only on the browser or a separate shell.

The prompt is tuned for deep review in the style of `ECHNL-5539.md`: business-scope validation, exact migration target selection, DB/file update ordering, idempotency, rollback safety, compatibility with old and new data, DPS API/DAO/BIZ/CLI layer impact, and concrete SQL/Mongo/UAT verification.

## Speed vs Depth

Use Speed to request the Codex Fast / priority service tier. Speed is independent from reasoning effort and report detail, so release review can stay detailed.

Fast Detailed:

```bash
LLM_SPEED=fast
LLM_REASONING_EFFORT=high
LLM_CODEX_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=180
LLM_MAX_TOKENS=6000
LLM_MAX_DIFF_CHARS=60000
REPORT_DETAIL_LEVEL=detailed
PROJECT_CONTEXT_MAX_CHARS=80000
PROJECT_CONTEXT_MAX_TREE_FILES=500
PROJECT_CONTEXT_MAX_FILE_CHARS=12000
GIT_VERSION_SOURCE_REVIEW_MAX_REPOS=12
GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO=60
GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS=100000
```

Balanced:

```bash
LLM_REASONING_EFFORT=medium
LLM_CODEX_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=300
LLM_MAX_DIFF_CHARS=30000
PROJECT_CONTEXT_MAX_CHARS=50000
GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO=60
GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS=80000
```

Deep:

```bash
LLM_SPEED=standard
LLM_REASONING_EFFORT=high
LLM_CODEX_TIMEOUT_SECONDS=900
LLM_TIMEOUT_SECONDS=900
LLM_MAX_DIFF_CHARS=60000
PROJECT_CONTEXT_MAX_CHARS=80000
GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO=120
GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS=180000
```

## Report Fields to Check

In each Markdown report, check:

- `LLM Provider`
- `LLM Model`
- `LLM Reasoning Effort`
- `LLM Speed`
- `Network Stage`
- `总体结论`
- `风险统计`
- `问题列表`
- `LLM 执行记录`
- `Jira / GitLab 关联`

Network stages:

- `gitlab-fetch-and-review`
- `gitlab-reviewer-query`
- `issue-branches`

## Troubleshooting

### GitLab Fetch Fails

Likely cause: missing/incorrect VPN DIRECT rule, DNS/proxy issue, or token.

Actions:

```bash
python review.py --network-check
```

If direct review cannot fetch GitLab, fix the DIRECT rule/token first, or review a local repo/diff while GitLab is unavailable.

### Codex Times Out

First verify real Codex execution:

```bash
python review.py --codex-check --codex-check-timeout 180
```

`python review.py --network-check` only verifies that the Codex CLI binary exists and GitLab is reachable. It does not prove that Codex can complete a model request.

If `--codex-check` reports `Invalid value: 'High'` or `Invalid value: 'Extra High'`, use lowercase Codex reasoning values in environment variables:

```bash
set LLM_REASONING_EFFORT=high
```

CodeReviewer normalizes UI labels before invoking Codex, so `High` and `Extra High` are accepted by the tool, but lowercase values are preferred in `.env`.

If `--codex-check` or a manual Codex run shows:

```text
stream disconnected - retrying sampling request
ERROR: Reconnecting...
Falling back from WebSockets to HTTPS transport. request timed out
```

then Codex CLI is starting correctly, but the terminal session cannot keep the model stream alive. Check proxy/VPN/DIRECT rules for OpenAI/Codex traffic, then rerun `--codex-check`. For non-DPS projects, `LLM_PROVIDER=auto` can fallback to CC Switch Claude code opus. For DPS projects, Codex is required and the review should stop until Codex connectivity is fixed.

Codex can also be slow on large diffs.

Options:

```bash
set LLM_CODEX_TIMEOUT_SECONDS=300
python review.py --mr-url "<gitlab-mr-url>"
```

Or skip Codex:

```bash
set LLM_NETWORK_MODE=non-vpn
python review.py --mr-url "<gitlab-mr-url>"
```

### Codex CLI Not Found

If the report says `codex CLI was not found`, set `CODEX_CLI_PATH` to the full path of `codex.exe`.

Example:

```bash
set CODEX_CLI_PATH=C:\Users\xuejie.xiao\.vscode\extensions\openai.chatgpt-26.623.70822-win32-x64\bin\windows-x86_64\codex.exe
python review.py --mr-url "<gitlab-mr-url>"
```

The tool also tries to auto-detect Codex from:

- `PATH`
- `%USERPROFILE%\.vscode\extensions\openai.chatgpt-*\bin\windows-x86_64\codex.exe`
- `%APPDATA%\npm\codex.cmd`

### DeepSeek Returns Non-JSON

By default the review exits because `LLM_REQUIRE_STRUCTURED_OUTPUT=1`. Fix the provider/model output and rerun.

Try reducing diff size:

```bash
set LLM_MAX_DIFF_CHARS=20000
python review.py --mr-url "<gitlab-mr-url>"
```

For local experiments only, set `LLM_REQUIRE_STRUCTURED_OUTPUT=0` to allow heuristic parsing.

### Report Missing Latest LLM Notes

Regenerate the report:

```bash
python review.py --mr-url "<gitlab-mr-url>" --output reports\latest-review.md
```

## Safe Operating Rules

- Do not commit `.env`.
- Do not paste real tokens into README, runbooks, reports, or source files.
- Do not post GitLab comments without reading the report first.
- Use `--post-gitlab-comment --yes` only after manual confirmation.
- Prefer direct `--jira`, `--sprint`, or `--mr-url` review with local context configured.
