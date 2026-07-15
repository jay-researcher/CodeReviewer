# CodeReviewer

Python MVP for GitLab MR code review based on [the original requirements](docs/requirement-original.md).

## Documentation

- [CodeReviewer 7.x User Manual](7.x-docs/CodeReviewer%20User%20Mannual.md) — living manual; update it for every user-facing 7.x change
- [Setup guide](docs/SETUP.md)
- [Code review flow](docs/code-review-flow.md)
- [Jira integration guide](docs/JIRA_INTEGRATION_GUIDE.md)
- [Jira issue reprocessing and review](docs/JIRA-ISSUE-REPROCESSING.md)
- [Operations runbook](docs/CodeReviewer-Runbook.md)
- [RHEL 9 deployment guide](docs/CodeReviewer-RHEL9-Deployment-Guide.md)
- [Issue Review workflow and local acceptance](7.x-docs/CodeReviewer-7.0-Issue-Workflow.md)
- [Issue Review 工作流与本地验收（中文）](7.x-docs/CodeReviewer-7.0-Issue-Workflow.zh-CN.md)

## Run Web App

```bash
python web.py --host 127.0.0.1 --port 8765
```

For access from another computer on the LAN:

```bash
python web.py --lan --port 8768 --allow-ip 192.168.3.170
```

This binds to `0.0.0.0` and prints LAN URLs such as `http://192.168.3.170:8768`. If another computer still cannot connect, allow the port in Windows Firewall.

The Web UI enforces a client IP whitelist before login. Loopback IPs are allowed by default. Configure LAN clients with one of:

```bash
set WEB_IP_WHITELIST=192.168.3.170,192.168.3.0/24
python web.py --lan --port 8768 --allow-ip 192.168.3.*
```

You can also create `data\web_ip_whitelist.txt`, one IP/CIDR/wildcard entry per line.

Open:

```text
http://127.0.0.1:8765
```

The Web UI requires login. Passwords are stored as PBKDF2-SHA256 hashes in:

```text
data\web_users.json
```

New accounts receive a 12-character random initial password. One-time local credentials are written to `data\initial_credentials_20260714.txt`; move or delete that file after secure handoff. The login page also includes a robot-prevention arithmetic challenge. `WEB_USERS_FILE` can move user storage outside the application directory.

Version 7.0 adds role-scoped `Issue Reviews` and `Pending Jira` workspaces. Critical/High findings follow `Handling → Re-scan → Leader Pass`; fixed blockers require a clean later Review Run, Developer `not-issue` decisions require Auditor/Manager approval, and only Manager can record a fully audited Jira follow-up exception. The workflow severity gate is configurable under `app.review_workflow`.

Report History supports online Markdown preview. Click a report name or `Preview` to render it inside the Web UI; use `Raw` or `Download` from the preview panel when the original Markdown file is needed. The preview renders report diff anchors and collapsible `details` blocks, and supports a maximize/restore wide-view modal for reading larger diffs without changing font size. In the maximized modal, use `Previous` / `Next` or left/right arrow keys to switch reports without closing the modal.

## Run CLI

## Usage Cases

### 1. Review One Jira Issue

Use this when you want to start from an `ECHNL` Jira issue and let CodeReviewer find related GitLab MRs from Jira links plus branch/MR discovery across projects listed in `D:\TTL\vibe-coding\CodeReviewer\config.yml`.

```bash
python review.py --jira ECHNL-8888 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --git-tools-groups=dps11-repository,wvadmin-repository,itrade-client
```

Review multiple Jira issues in one CLI batch by separating keys with an English comma. Each issue is still reviewed as its own consolidated MR change set and produces its own report:

```bash
python review.py --jira ECHNL-8888,ECHNL-8889,ECHNL-8890 --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

List the discovered GitLab projects and related MRs before running review:

```bash
python review.py --jira ECHNL-8888 --jira-mr-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml

python review.py --jira ECHNL-8888,ECHNL-8889 --jira-mr-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

The review combines Jira issue description, linked local Jira/PRD context from `D:\TTL\jira-prd\data`, related GitLab MR diffs, local project context when configured, Drupal/DPS rules, and the `ECHNL-5539.md` report style. When one Jira issue has multiple related MRs, CodeReviewer reviews them together as one consolidated Jira-level change set. If those MRs belong to different responsible owners, it saves one split report per responsible owner. Each split report includes only that owner's `Related MRs`, changed files, findings, and file diffs, with changed files prefixed as `<gitlab project>!<mr iid>/<file>` so cross-project findings can link back to the right diff.

In the Web UI, starting a single Jira issue review checks the current output directory first. If reports for that Jira key already exist, the UI shows the existing report links and requires two-step confirmation before creating another review job.

### 2. Review One Sprint

Use this before release/merge review. CodeReviewer reads all reviewable Jira issues in the sprint, discovers related Open MRs by default, and writes detailed developer-facing reports with reproduction, impact, fix, and verification guidance.

```bash
python review.py --sprint=10068 --jira-project ECHNL --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --review-framework=drupal
```

List sprint-linked MRs first:

```bash
python review.py --sprint=10068 --jira-project ECHNL --sprint-list-only --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml
```

### 3. Review One Jira Filter

Use this when a saved Jira filter already defines the release or audit scope. CodeReviewer loads all reviewable issues returned by the filter and reviews each issue through the same consolidated MR workflow as Sprint review.

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

The detailed report style is controlled by:

```bash
set REVIEW_TEMPLATE_PATH=D:\TTL\vibe-coding\CodeReviewer\docs\ECHNL-5539.md
set REVIEW_TEMPLATE_MAX_CHARS=16000
```

Review a GitLab MR:

```bash
set GITLAB_TOKEN=glpat-REPLACE_WITH_YOUR_TOKEN
python review.py --mr-url https://gitlab.example.com/group/project/-/merge_requests/123 --jira ECHNL-8888
```

Review a GitLab MR with local full-project context:

```bash
python review.py --mr-url https://gitlab.example.com/group/project/-/merge_requests/123 --context-repo D:\TTL\vibe-coding\dps11
```

The local context includes a repository tree sample, key project files, changed files' full local contents, and nearby files from the changed directories. Tune prompt size with `PROJECT_CONTEXT_MAX_CHARS`, `PROJECT_CONTEXT_MAX_TREE_FILES`, and `PROJECT_CONTEXT_MAX_FILE_CHARS`.

When `LOCAL_CONTEXT_AUTO=1`, CodeReviewer matches the GitLab project and MR target branch to `repository_url`, `branch`, and `local_working_copy` in `D:\TTL\vibe-coding\CodeReviewer\config.yml`. Before review it fetches the target branch, preserves local changes, and reads context directly from `origin/<target-branch>` rather than trusting the current checkout:

```bash
python review.py --mr-url https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/momd/-/merge_requests/291 --jira ECHNL-5630
```

Synchronize every configured working copy and branch without running a review:

```bash
python review.py --sync-repositories
```

Use `--sync-no-index` to fetch repositories without updating Codebase Memory. Configured product branches that do not exist in an individual split repository fall back to fetching all available remote refs during bulk synchronization; an MR target branch remains strict and must exist.

CodeReviewer integrates `codebase-memory-mcp` as a local persistent code graph. It reuses an existing graph when its indexed HEAD matches the target commit, otherwise indexes the exact target-branch snapshot without modifying the working tree, then adds architecture, symbol, and connected-dependency context for changed files to the LLM review.

MR changes are calculated from immutable GitLab base/head commit SHAs using the configured `local_working_copy` whenever possible. Complete diffs are compressed under GitNexus `changes/` and reused for subsequent reviews; matching Codebase Memory source repositories and GitLab diff APIs provide ordered fallbacks.

For local issue-branch review across iTrade Client, WVAdmin split projects, and DPS microservices, map local repos in `data/local_workspaces.yml` and run:

```bash
python review.py --issue-branches --jira ECHNL-8888 --target-branch develop --git-tools-groups=dps11-repository,wvadmin-repository,itrade-client
```

List matching local branches without running LLM review:

```bash
python review.py --issue-branches --issue-branch-list-only --jira ECHNL-8888 --target-branch develop
```

Code review also studies local Jira/PRD issue docs by default from `D:\TTL\jira-prd\data`. It matches detected issue keys such as `ECHNL-8888` and linked `SVREQ-1234`, then injects the local Markdown/JSON issue context into the LLM prompt so findings can be checked against requirement intent and traceability.

```bash
python review.py --mr-url https://gitlab.example.com/group/project/-/merge_requests/123 --jira ECHNL-8888 --jira-prd-data D:\TTL\jira-prd\data --jira-prd-context auto
```

Use `--jira-prd-context off` to disable local issue context for a run. Tune prompt size with `JIRA_PRD_CONTEXT_MAX_CHARS`, `JIRA_PRD_CONTEXT_PER_ISSUE_CHARS`, and `JIRA_PRD_CONTEXT_MAX_ISSUES`.

Review a local repository branch diff:

```bash
python review.py --repo D:\TTL\vibe-coding\dps11 --source-branch ECHNL-8888 --target-branch dps11 --project dps11
```

Review a saved unified diff:

```bash
git diff target...source > change.diff
python review.py --diff-file change.diff --project dps11 --jira ECHNL-8888
```

Reports are written to `D:\TTL\code-review\e-channel-sprintYYYYMMDD` by default. `YYYYMMDD` is the final actual working day of the current Monday-Sunday cycle according to `data/china-mainland-work-calendar.json`. A Saturday or Sunday marked as an adjusted workday becomes the week end; a holiday Friday moves the week end earlier; a completely non-working holiday week uses the latest preceding actual workday. Override the report directory with `REPORT_OUTPUT_DIR` or `--output-dir`, or override only the base folder with `REPORT_OUTPUT_BASE_DIR`:

Update the bundled calendar annually from the State Council holiday notice, or set `CHINA_WORK_CALENDAR_FILE` to a maintained JSON file containing `holidays` and adjusted weekend `workdays` arrays. If the file or year is unavailable, CodeReviewer falls back to Monday-Friday.

```bash
set REPORT_OUTPUT_DIR=D:\TTL\review-reports
python review.py --diff-file change.diff --project dps11 --jira ECHNL-8888

set REPORT_OUTPUT_BASE_DIR=D:\TTL\code-review
python review.py --diff-file change.diff --project dps11 --jira ECHNL-8888

python review.py --diff-file change.diff --project dps11 --jira ECHNL-8888 --output-dir D:\TTL\review-reports
```

Generated report files are grouped under `<output-dir>/<responsible>/` by default, where `responsible` is read from `D:\TTL\vibe-coding\CodeReviewer\config.yml`. For example: `D:\TTL\code-review\e-channel-sprint20260710\wen.yi\ECHNL-5592_has-issue-medium.md`. If a consolidated Jira issue contains MRs owned by different responsible people, CodeReviewer saves separate responsible-specific reports, such as `kevin.tan\ECHNL-5308_has-issue-medium.md` and `wen.yi\ECHNL-5308_has-issue-high.md`. Set `REPORT_GROUP_BY_RESPONSIBLE=0` for flat output.

`--output` can still be used for an exact filename or full file path. If `--output` points to a directory, the tool writes the default report filename inside that directory. When both `--output-dir` and `--output` are provided, the directory comes from `--output-dir` and the filename comes from `--output`.

Auto-generated report paths use `<responsible>/<ECHNL-issue-key>_<status>.md`, where status is `pass` when no findings are found, or `has-issue-critical/high/medium/low/warning` based on the highest severity.

Report language defaults to Simplified Chinese. Override it with either environment configuration or CLI:

```bash
set REPORT_LANGUAGE=zh-CN
python review.py --diff-file change.diff --project dps11 --jira ECHNL-8888 --report-language en
```

Report findings default to `Medium` and above. Lower or raise the report threshold per run when needed:

```bash
python review.py --jira ECHNL-8888 --report-min-severity High
python review.py --jira ECHNL-8888 --report-min-severity Low

set REPORT_MIN_SEVERITY=Medium
```

With the default `Medium` threshold, `Low` and `Warning` findings are filtered out of report findings, severity counts, handling templates, filenames, and Web history. The report Basic Info records the effective threshold and the number of filtered findings.

Finding locations and changed-file names in the Markdown report are clickable. They jump to the report's file diff section, while an extra Code link points to GitLab when MR URL and branch/commit metadata are available.

Every MR review automatically checks whether the MR's GitLab project path matches a repository defined in `GIT_TOOLS_CONFIG` / `D:\TTL\vibe-coding\CodeReviewer\config.yml`. The report Basic Info includes `GitLab Project Match` with the matched group/module, for example `dps11-repository/momd`. Set `GIT_TOOLS_REQUIRE_MR_MATCH=1` to fail the run when an MR is outside the configured repositories.

Batch review recent MRs for a reviewer:

```bash
python review.py --reviewer-mrs --reviewer jay.wince@tx-tech.com --reviewer-days 7 --reviewer-state all
```

Batch review all GitLab MRs linked from Jira issues in a sprint:

```bash
python review.py --sprint=10068 --jira-project ECHNL --context-repo=D:\TTL\wvplaform\dps11.2.83
```

Sprint mode first reads GitLab MR links from Jira remote links / Jira development details, then falls back to GitLab MR search by issue key when no Jira link is found.
It also scans configured GitLab projects for issue-key branches such as `feature/ECHNL-8888` and DPS layer branches such as `feature/DAO#ECHNL-8888`, `feature/BIZ#ECHNL-8888`, `feature/CLI#ECHNL-8888`, and `feature/API#ECHNL-8888`.
By default the project list comes from `D:\TTL\vibe-coding\CodeReviewer\config.yml`, then `SPRINT_GITLAB_PROJECTS`, then `data/projects.json`.
Jira/sprint consolidated review excludes MRs whose target branch is a development-version branch. Configure explicit branches with `dev_branch` in `CodeReviewer/config.yml`; when omitted, CodeReviewer treats the uppercase project key/module as the dev branch, for example `services-terminal -> SERVICES-TERMINAL`. Excluded MRs appear in `excluded_dev_branch_mrs` and terminal output as `SKIP DEV-BRANCH`.

Limit sprint branch discovery to DPS9/DPS11 and force Drupal framework review:

```bash
python review.py --sprint=10068 --jira-project ECHNL --git-tools-config=D:\TTL\vibe-coding\CodeReviewer\config.yml --git-tools-groups=dps9-repository,dps11-repository --review-framework=drupal
```

For DPS9/DPS11 backend database changes, CodeReviewer treats `db_change.scr` as the build-delivered database change entry point. It reviews command ordering, referenced SQL/shell/resource files, previous database version alignment, rerun safety, rollback/backup expectations, and environment scope. It does not require Drupal update hooks for these DPS database changes.

DPS environment settings are centralized in `state_config.yml` and `state_config.<env>.yml` files, with historical `state_cofig.<env>.yml` spelling also supported. Token/encryption-like values in those files are not reported as hard-coded secrets unless there is concrete evidence of invalid scoping, broken reference/encryption format, cross-environment leakage, or real leaked credentials outside that config mechanism.

List sprint MRs without running review:

```bash
python review.py --sprint=10068 --sprint-list-only --sprint-limit 50
```

Fast list mode skips slower per-issue Jira remote-link/development-panel calls and branch scans:

```bash
python review.py --sprint=10068 --sprint-list-only --sprint-fast-list --sprint-limit 50
```

## Resume Interrupted Batch Reviews

Batch review checkpoints are enabled by default for `--sprint`, `--jira`, `--reviewer-mrs`, and `--issue-branches`.
If the terminal is closed or `Ctrl+C` is pressed, re-run the same `python review.py` command with the same `--output-dir`; completed items are skipped and the current unfinished item is retried.

In the Web UI, each active review job card supports `Pause`, `Resume`, and `Stop`. These controls are cooperative and take effect at the next safe checkpoint; stopped jobs are shown as `canceled` instead of failed.
If the browser is refreshed while the Web server is still running, the Progress panel restores visible review jobs and resumes polling active jobs automatically.

Checkpoint files are stored under:

```text
<output-dir>\.code_reviewer_resume\
```

Terminal progress now prints report paths as soon as each item finishes:

```text
Batch: sprint consolidated Jira reviews; total=19
Output dir: D:\TTL\code-review\e-channel-sprint20260710
Resume state: D:\TTL\code-review\e-channel-sprint20260710\.code_reviewer_resume\sprint-mrs-xxxx.json
[1/19] DONE ECHNL-5630 (1 MR(s)) -> report: D:\TTL\code-review\e-channel-sprint20260710\owner\ECHNL-5630_pass.md (Critical=0 High=0 Medium=0 Low=0; findings=0)
[2/19] SKIP DONE ECHNL-5629 (1 MR(s)) -> report: D:\TTL\code-review\e-channel-sprint20260710\owner\ECHNL-5629_has-issue-medium.md (Critical=0 High=0 Medium=1 Low=0; findings=1)
```

Controls:

```bash
# Disable checkpoints for one run.
python review.py --sprint=10068 --no-resume

# Clear the matching checkpoint and run from the beginning.
python review.py --sprint=10068 --reset-resume --output-dir=D:\TTL\code-review\e-channel-sprint20260710
```

## Network-aware MR workflow

Current network posture:

- GitLab `gitlab.tx-tech.com` is reachable through the VPN DIRECT rule.
- Codex can run in the same network session.
- CC Switch fallback defaults to the `Claude code opus` provider. DeepSeek can still be selected explicitly when needed.

Recommended direct flow:

```bash
# Optional: inspect current network posture.
python review.py --network-check

# Verify real Codex model execution, not only codex --version.
python review.py --codex-check --codex-check-timeout 180

# GitLab and Codex can be used together.
set LLM_NETWORK_MODE=direct
set LLM_PROVIDER=auto
python review.py --mr-url https://gitlab.example.com/group/project/-/merge_requests/123 --context-repo D:\TTL\vibe-coding\dps11
```

## Branch and Jira mapping

- `SVREQ` is the PRD/requirement Jira space.
- GitLab development branches should map to action/development Jira spaces such as `ECHNL`.
- Web branch format: `<issue type>/<issue key>`, for example `feature/ECHNL-8888`, `bug/ECHNL-6666`, `task/ECHNL-6668`, `change-request/ECHNL-6667`.
- DPS middle-office backend branch format: `<issue type>/<layer>#<issue key>`, for example `feature/API#ECHNL-8888`, `feature/DAO#ECHNL-8888`, `feature/BIZ#ECHNL-8888`, `feature/CLI#ECHNL-8888`.

## GIT_VERSION MR Review

MRs that change `git_version*.yml` or `build*.yml` are treated as `GIT_VERSION` MRs. The reviewer now fetches and reviews the actual commit diffs for every repository locked in `git_version.yml`, plus the build-code repository commit locked in `build.yml`. When a previous `git_version-v*.yml` exists in the same version directory, CodeReviewer compares the previous locked source commit to the current locked source commit and reviews that full version delta.

The report adds a `GIT_VERSION 摘要` section with locked repositories, branches, commits, build repository branch/commit, version, companies, source commit issue keys, and fetched file counts. Additional checks cover duplicate YAML keys, missing or invalid 40-character commits, build repository self-locking, `build.yml` referencing the intended `git_version.yml`, bh/build-history based patch version derivation, version-file suffix alignment, source commit Jira/SVREQ traceability context, and company/environment build scope. For build repository self-locking, `version.git_repository.commit` / `version.git_version4config.git_repository.commit` is valid when it points to a build repository commit after the required build resources were pushed; it does not need to equal the MR head. CodeReviewer verifies that locked commit contains the required `build.yml` and referenced `git_version.yml` resources. Build reference context is read from `WEB_BUILD_TOOLS_DIR`，defaulting to `D:\TTL\vibe-coding\web-build-tools`.

CodeReviewer also loads web-build script facts from `D:\TTL\vibe-coding\web-build-tools`: `source_provide_method` supports `clone` / `base_version` / `in_place`; relative `version.git_version` paths are resolved from the `build.yml` directory; package versions are derived from `*.bh`; and `CreateGIT_VERSIONMR.js` uses the expected three-commit resource-finalization flow. iTrade's `replace_with_ttl.companines` is treated as a known compatibility key because the current `build.js` reads that field.

## Configuration

The app auto-discovers project metadata from files like:

- `itrade-client#git_version-v7.5.1.38.yml`
- `itrade-client#build-v7.5.1.38.yml`
- `wvadmin#git_version-v1.0.82.8.yml`
- `dps11#git_version-v11.2.82.10.yml`

On first run it creates `data/projects.json`.

Sensitive values must be provided through environment variables, not committed files:

- `GITLAB_TOKEN`
- `GITLAB_URL`
- `JIRA_TOKEN`
- `JIRA_URL`

Optional runtime configuration:

- `LLM_PROVIDER`, `LLM_MODEL`, `LLM_NETWORK_MODE`, `LLM_SPEED`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`
- `LLM_CODEX_TIMEOUT_SECONDS`, `LLM_REASONING_EFFORT`, `LLM_CC_SWITCH_PROVIDER`, `LLM_USE_CC_SWITCH`, `LLM_MAX_TOKENS`, `LLM_MAX_DIFF_CHARS`
- For CLIProxyAPI/OpenAI-compatible Responses endpoints, set `LLM_CODEX_HTTP_API_KEY_ENV=OPENAI_API_KEY`, store the proxy key in `OPENAI_API_KEY`, and override the policy URL with `CODE_REVIEW_OVERRIDE_LLM_CODEX_HTTP_BASE_URL=http://host:port/v1`.
- `LLM_REQUIRE_SUCCESS`, `LLM_REQUIRE_STRUCTURED_OUTPUT`, `LLM_REQUIRED_REASONING_EFFORT`, `LLM_REQUIRED_SPEED`
- `REVIEW_FRAMEWORK`, `DRUPAL_SKILL_PATH`, `DRUPAL_SKILL_CONTEXT_MAX_CHARS`
- `GITNEXUS_STORAGE_PATH`, `GITNEXUS_INDEX_FILE`
- `JIRA_SPACES`, `SPRINT_PREFIXES`, `JIRA_PROJECT_KEY`, `JIRA_SEARCH_API`, `JIRA_SPRINT_MAX_ISSUES`
- `JIRA_FILTER_MAX_ISSUES`, `JIRA_REVIEW_ALLOWED_STATUSES`
- `JIRA_PRD_CONTEXT`, `JIRA_PRD_DATA_DIR`, `JIRA_PRD_CONTEXT_MAX_CHARS`, `JIRA_PRD_CONTEXT_PER_ISSUE_CHARS`, `JIRA_PRD_CONTEXT_MAX_ISSUES`
- `REVIEW_TEMPLATE_PATH`, `REVIEW_TEMPLATE_MAX_CHARS`
- `LOCAL_CONTEXT_AUTO`, `LOCAL_WORKSPACE_CONFIG`, `LOCAL_WORKSPACE_ROOTS`, `LOCAL_WORKSPACE_SCAN_MAX_DEPTH`, `ISSUE_BRANCH_REVIEW_LIMIT`
- `SPRINT_MR_STATE`, `SPRINT_MR_LIMIT`, `SPRINT_MR_GITLAB_SEARCH_FALLBACK`
- `SPRINT_MR_HISTORY_DISCOVERY` and `SPRINT_MR_HISTORY_LIMIT` supplement Jira links with earlier Open/Merged MRs for the same issue; configured development-version targets remain excluded.
- `SPRINT_JIRA_REMOTE_LINK_DISCOVERY`, `SPRINT_JIRA_DEV_PANEL_DISCOVERY`
- `SPRINT_BRANCH_DISCOVERY`, `SPRINT_GITLAB_PROJECTS`, `SPRINT_BRANCH_PROJECT_LIMIT`, `SPRINT_BRANCH_SEARCH_LIMIT`, `SPRINT_BRANCH_MR_LIMIT`
- `SPRINT_BRANCH_DISCOVERY_MODE` (`missing-only` by default, or `always` for exhaustive branch scans)
- `GIT_TOOLS_CONFIG`, `GIT_TOOLS_GROUPS`, `GIT_TOOLS_REQUIRE_MR_MATCH`
- `REPORT_GROUP_BY_RESPONSIBLE`

MR discovery defaults to Open merge requests only: `REVIEWER_MR_STATE=opened` and `SPRINT_MR_STATE=opened`. Use `--reviewer-state all`, `--sprint-state all`, or the matching environment variable only when a review intentionally needs merged, closed, or locked MRs.

LLM provider examples:

```bash
# Preferred: Codex CLI GPT 5.5 first, then CC Switch Claude code opus.
set LLM_PROVIDER=auto
set LLM_NETWORK_MODE=direct
set LLM_CODEX_MODEL=gpt-5.6-sol
set LLM_CODEX_TIMEOUT_SECONDS=300
set LLM_TIMEOUT_SECONDS=180
set LLM_MAX_RETRIES=3
set LLM_SPEED=standard
set LLM_REASONING_EFFORT=high
set CODEX_CLI_PATH=C:\path\to\codex.exe
set "LLM_CC_SWITCH_PROVIDER=Claude code opus"
set DPS_REVIEW_REQUIRE_CODEX=1

# CC Switch only, using the provider configured in CC Switch.
set LLM_PROVIDER=cc-switch
set "LLM_CC_SWITCH_PROVIDER=Claude code opus"
```

Speed profile:

```bash
# Codex Fast / priority service tier while preserving detailed review depth.
set LLM_SPEED=fast
set LLM_REASONING_EFFORT=high
set LLM_CODEX_TIMEOUT_SECONDS=300
set LLM_TIMEOUT_SECONDS=180
set LLM_MAX_TOKENS=6000
set LLM_MAX_DIFF_CHARS=60000
set REPORT_DETAIL_LEVEL=detailed
set PROJECT_CONTEXT_MAX_CHARS=80000
set PROJECT_CONTEXT_MAX_TREE_FILES=500
set PROJECT_CONTEXT_MAX_FILE_CHARS=12000
set GIT_VERSION_SOURCE_REVIEW_MAX_REPOS=12
set GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO=60
set GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS=100000
```

The same values are available in `.env.speed.example`.
You can also pass `--speed fast` on `review.py`; Codex maps that to `service_tier="priority"` while keeping `LLM_REASONING_EFFORT` and detailed report settings independent from speed. Use lowercase Codex reasoning values in environment variables: `low`, `medium`, `high`, or `xhigh`. UI labels such as `High` and `Extra High` are normalized before CodeReviewer calls Codex.

The review prompt is tuned for deep release-risk review, similar to `ECHNL-5539.md`: migration scope, DB/file update ordering, idempotency, rollback, compatibility, DPS layering, and concrete SQL/Mongo/UAT verification checks.

By default, release reviews use a 300-second Codex timeout, 180-second fallback-provider timeout, and 3 retries. Reviews fail fast when all LLM providers fail, when the provider returns non-JSON output, or when the effective reasoning effort/speed is below the required level. DPS/DPS9/DPS11 GitLab projects force `codex-cli`; after the configured retries fail, CodeReviewer exits instead of falling back to CC Switch. This prevents static-rule-only reports from being mistaken for detailed ECHNL-5539-style reviews. To intentionally allow fallback behavior for local experiments only, set `LLM_REQUIRE_SUCCESS=0` or `LLM_REQUIRE_STRUCTURED_OUTPUT=0`.

CC Switch should hold the fallback provider credentials so keys are not duplicated in this repo. The default CC Switch selector is `Claude code opus`; set `LLM_CC_SWITCH_PROVIDER=DeepSeek` only when DeepSeek fallback is intentionally required.

GitLab/Jira writeback is never silent. For CLI use, generate and review the Markdown report first, then re-run with both `--post-gitlab-comment` and `--yes` to confirm.

Review traceability is stored in `data/review_history.jsonl`. Markdown reports and metadata are also copied into the configured GitNexus storage path.

## Current Scope

Implemented:

- Project discovery from existing build/git version files.
- GitLab MR diff loading through GitLab API.
- Local git branch diff review.
- Unified diff review.
- Rule-based findings for secrets, dynamic SQL, debug code, TODO/FIXME, large files, migrations, and DPS layer signals.
- Markdown report generation.
- Jira/SVREQ association parsing from branch text, summary hash references, and GitLab message summary links.
- Configurable LLM provider adapters with default Codex GPT 5.5 first and CC Switch Claude code opus fallback.
- GitNexus report storage, immutable SHA-keyed MR change cache, and JSONL review history.
- Web UI and JSON API using Python standard library.
- Web downloads for a single Markdown report or a responsible-owner folder zip.
- Web responsible login with generated strong passwords and robot verification.
- CLI script.

Next recommended steps:

- Add live Jira API sync for SVREQ/action issue relationships.
- Add richer UI controls for switching CC Switch providers per review.
- Add Flutter frontend consuming the existing JSON APIs.

