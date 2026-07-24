# 部署 CodeReviewer 到 192.168.3.78

更新日期：2026-07-24

本文记录本次真实部署结果。通用部署原则和完整生产建议参见 [CodeReviewer-RHEL9-Deployment-Guide.md](CodeReviewer-RHEL9-Deployment-Guide.md)。

## 部署结果

| 项目 | 结果 |
| --- | --- |
| 主机 | `192.168.3.78`，RHEL 9.4 |
| CodeReviewer 版本 | `7.2.17` |
| 部署制品 | `codereviewer-7.2.17-975d039f56e0.tgz`，SHA-256 `535e3e42b667025c573d9b996ced805560d125c8e22dc547ecc6e402fb159e9b` |
| Python | 3.11.13 |
| Git | 2.52.0 |
| Codebase Memory | 0.9.0，Linux 本地 CLI 模式 |
| systemd | `codereviewer.service` 已启用且为 `active` |
| 监听地址 | `0.0.0.0:8765` |
| 访问地址 | <http://192.168.3.78:8765> |
| 健康检查 | `/api/version` 返回 `7.2.17`，`/api/health` 返回 `healthy` |
| 编译检查 | 通过 |
| RHEL9 测试 | 308 passed |

## 7.2.17 Sprint/Cycle 归属修复与 Legacy 治理

2026-07-24 将 `7.2.17` 功能提交 `975d039f56e01957a1fb65768653f7e97389af37` 部署到生产。

- 新报告持久化 Jira 当前 Sprint/Cycle 身份，当前交付报告无法唯一匹配 Cycle 时拒绝登记，不再静默回退 Legacy；
- 误归 Legacy 的本周报告、Finding、Handling 与 Pass 迁入 `e-Channel Sprint 1.4.78` 或 `Ad hoc Review · 2026-W30`；
- Scope 一致的 ECHNL-5749/5750 保留 Passed；ECHNL-5748 因当前 Scope 增加 MR，保留既有证据并回到 Pending；
- 清理后保留 15 个 Cycle、16 个 Run、35 个 Finding、26 个 Handling、5 个 Pass，16 个报告路径均存在；
- `Legacy` Cycle、Snapshot/Release Resource 中的 Legacy Sprint 与全部 `backfilled=1` 业务记录均为 0；
- SQLite `integrity_check=ok`、外键检查为 0，12 个账户及凭据指纹保持不变。

发布与数据治理备份：

```text
/var/backups/codereviewer/7.2.16+b202607241318-to-7.2.17-20260724-173933/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260724-173933
/var/backups/codereviewer/legacy-cleanup-7.2.17-20260724-174132/codereviewer-pre-cleanup.db
```

## 7.2.16+b202607241318 Responsible 边界与滚动保护

2026-07-24 将构建 `7.2.16+b202607241318` 部署到生产，发布代码固定为 `20260720` 分支提交 `869b907d336e229ec9f9158e4098dfb3788db51e`。

- Repository Responsible、Component-driven Delivery Responsible 与 Web Reviewer 权限分层，不再在报告最终拆分阶段相互覆盖；
- WVAdmin build/Web 公共仓库、MOMD/Trade Middle Office、AOP/LCA 的生产 Responsible 映射已逐项核对；
- 部署配置合并支持递归同步仓库模块的 `responsible`，同时保留 Linux 路径、生产端点、凭据及其他 runtime 配置；
- Run Review 自动折叠时保持 viewport；Progress 在拖动 scrollbar 未释放期间无限暂停自动跟随，释放后等待 60 秒才恢复；
- RHEL9 staging 与生产切换后均执行 `295/295` 测试；Workflow SQLite `integrity_check=ok`，4 个 Issue、4 个 Cycle、7 个 Run、22 个 Finding 及 7 份报告保持完整。

一致性备份与回滚：

```text
/var/backups/codereviewer/7.2.16+b202607231300-to-7.2.16+b202607241318-20260724-131848/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260724-131848
```

备份 SHA-256：`b75fe747b816b1e1e3e294b5bacc88e45183d36ea79a1cb3ecab11cc40e335`。

## 2026-07-24 Responsible Scope 热修复

生产版本号保持 `7.2.16+b202607231300`，代码更新到提交 `eed3029b08d76f2fb2b9680bea5f4f664cf53ff4`。

- 最终报告拆分保留 Jira Component-driven Responsible，不再由 MR 项目 fallback owner 覆盖；
- WVAdmin 项目的交付 Responsible fallback 统一为 `hieut.tran`，`wen.yi` 继续通过 Web Frontend Reviewer domain 审核 WVAdmin 与 Services Terminal；
- 部署配置合并同步 `responsible` 字段，生产 Linux 路径、端点、凭据及其他运行配置保持不变；
- ECHNL-5748 的 Jira Components 已确认为 `MOMD + WVAdmin`，规则推导为 `hieut.tran`；
- ECHNL-5748/5749 的 4 份报告、4 个 Run、2 个 Issue 与当前 Cycle Scope 已迁移到 `hieut.tran`；Hieu 可见对应 Issue History 与报告，Wen 仍具有 reviewer domain 访问权；
- 本机与 RHEL9 完整自动化回归均为 `294/294`，Workflow SQLite `integrity_check=ok`，生产 `/api/health=healthy`。

部署与数据修复备份：

```text
/var/backups/codereviewer/7.2.16+b202607231300-to-7.2.16+b202607231300-20260724-121252/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260724-121252
/var/backups/codereviewer/echln-5748-5749-responsible-repair-20260724-121501/
```

## 7.2.16+b202607231300 空 Scope 闭环与 Workflow 数据重置

2026-07-23 将构建 `7.2.16+b202607231300` 部署到生产，发布代码固定为 `20260720` 分支提交 `33f0269`，包含提交 `65c0ef0` 的 Cycle 空 Scope 闭环修复。

- RHEL 9 隔离 staging 执行 `293/293` 通过，制品 SHA-256 与归档可读性验证通过；
- 外部及服务器本机版本、健康、登录入口和未认证 Manager API 保护验证通过；
- 按用户明确授权，先完整备份，再清除 `/var/lib/codereviewer/code-review` 中 102 个报告/Resume 文件，并重建 `/opt/codereviewer/current/data/codereviewer.db`；
- 清理前 Workflow 包含 39 个 Issue、50 个 Cycle、59 个 Run、226 个 Finding；清理后 schema v3 完整性为 `ok`，16 个业务表全部为 0；
- 生产配置、12 个账户、角色、凭据和服务权限保持不变；报告根目录只保留应用启动时创建的空周目录；
- 后续必须从 Sprint Scan 开始，由 Jira 当前 Sprint 与实时 MR 重建 Live Cycle、Required Scope 和 Review Run。

一致性备份与回滚：

```text
/var/backups/codereviewer/7.2.16-to-7.2.16+b202607231300-20260723-130100/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260723-130100
```

备份 SHA-256：`df9b9487c5153e276267c62ab18f04402ecb3ac86bfc9f9879adba4e9ea5133f`。

## 7.2.16 Cycle 口径与 Sprint 工作流闭环

2026-07-23 将 7.2.16 部署到生产，发布制品固定 `20260720` 分支提交 `331fcb1ad023dc29873dbbac4802ca00d8da3dfa`。

验收结果：

- RHEL9 隔离 staging 使用合并后的生产配置执行 `291/291` 测试通过，`requirements.txt` 未变化，生产虚拟环境未修改；
- 外部及服务器本机 `/api/version=7.2.16`、`/api/health=healthy`，登录页返回 HTTP 200，`codereviewer.service` 已启用且为 `active`；
- `review.discovery.require_strong_history_reference=true` 与 `COMPANY_GIT_VERSION` Release Gate 路由已进入生产配置；生产端点、凭据、Linux 路径及其他运行策略保持不变；
- 12 个生产账户的角色、凭据指纹及启停状态保持一致；`web_users.json` 为 `0600 codereviewer:codereviewer`，EnvironmentFile 为 `0640 root:codereviewer`；
- Workflow SQLite `integrity_check=ok`，schema v3；38 个 Issue、38 个 Legacy Cycle、59 个 Run、226 个 Finding 及其他历史表计数在切换前后保持完整；
- 本次部署未自动执行 Sprint Scan 或生成报告。既有 Legacy Cycle 继续用于审计；用户扫描当前 Sprint 后才会建立 Live Cycle，并由 `Current cycles` 视图与 Legacy 历史分开统计；
- 制品与完整系统备份 SHA-256、归档可读性、一键回滚脚本语法、未认证 Manager API 的 HTTP 401 保护及静态资源均验证通过。

两次预切换检查曾在停服前安全终止：第一次发现生产配置合并白名单遗漏新增 review policy，第二次发现 YAML 重复 `review.discovery` 会覆盖强引用开关。两项均补齐回归后重新生成制品，生产 7.2.15 在此期间未中断。

一致性备份与回滚：

```text
/var/backups/codereviewer/7.2.15-to-7.2.16-20260723-101219/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260723-101219
```

## 7.2.15 Responsible、报告 Scope 与 Web 体验完善

2026-07-23 将 7.2.15 部署到生产，固定 GitHub `20260720` 分支提交 `4a862db9bfc71fd7332d800ef50e78a1916bb479`。

验收结果：

- 本机和 RHEL9 staging 完整自动化测试均为 `284/284` 通过；
- 外部及服务器本机 `/api/version=7.2.15`、`/api/health=healthy`，`codereviewer.service` 为 `active`；
- Web Frontend Reviewer domain、Codex 300 秒真实活动超时、15 秒心跳、900 秒绝对上限，以及 DPS 两次审核和 42,000 字符重试策略均已合并到生产配置；生产端点、凭据、Linux 路径与其他运行策略保持不变；
- 12 个生产账户的角色、凭据指纹与启停状态保持一致，`kelvinh.wu` 为普通 Developer；`web_users.json` 权限为 `0600`；
- Workflow SQLite `integrity_check=ok`，schema 与历史计数在切换前后保持完整；
- 发布归档 SHA-256、完整系统备份校验、一键回滚脚本语法及外部访问均验证通过。

一致性备份与回滚：

```text
/var/backups/codereviewer/7.2.14-to-7.2.15-20260723-010925/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260723-010925
```

## 7.2.14 Sprint Overview 报告覆盖修复

2026-07-22 将 Sprint Overview 报告 Key 解析和统计口径修复部署到生产，固定 GitHub `20260720` 分支提交 `ee1c37e8943408d21fe94179e9c6c960c2dfaa3a`。

验收结果：

- 下划线报告名 `ECHNL-5747_iTrade-Client-...md` 正确归属 `ECHNL-5747`，不再误识别为 `CLIENT-7`；
- Overview 分别统计唯一 Jira Issue、必需的 `Issue × Application + Release Line` Scope 和历史报告文件；
- Sprint 1.4.77 实时复算为 6/10 唯一 Issue、7/11 必需 Scope 已有报告、10 个历史报告文件；
- iTrade Client 7.5.0 为 1/1、7.5.1 为 4/4，跨版本 Issue 分别计入两个 Scope；
- 本地和 RHEL9 staging `262/262` 测试通过，外部及本机 `/api/version`、`/api/health` 通过；
- 11 个生产账户凭据指纹、角色及启停状态一致；Workflow SQLite schema v3、`release_line` 列与历史行数完整。

一致性备份与回滚：

```text
/var/backups/codereviewer/7.2.13-to-7.2.14-20260722-210106/system-backup.tgz
/usr/local/sbin/codereviewer-rollback-20260722-210106
```

## 7.2.13 验收反馈热更新

2026-07-21 将 7.2.13 验收反馈整改与 TTL × Jay 结晶 Logo 同步到生产，固定 GitHub `20260720` 分支提交 `0779b3cc50b1d826e27fad07e64428a6572b8f78`。本次是 7.2.13 到 7.2.13 的受保护热更新：生产配置分支模式已是目标通配符，因此分支同步按幂等 no-op 处理，未覆盖 Linux 路径、端点、token 或运行策略。

验收结果：

- staging 完整自动化测试 `259/259` 通过；
- `/api/version=7.2.13`、`/api/health=healthy`，外部 `192.168.3.78:8765` 可访问；
- 登录页和 `/assets/ttl-jay-crystal-logo.png` 返回 HTTP 200，页面已引用 Logo；
- 10 个生产账户的凭据指纹、角色及启停状态保持一致，`web_users.json` 为 `0600 codereviewer:codereviewer`；
- Workflow SQLite `integrity_check=ok`、schema v3，历史 Issue、Run、Finding、Discussion 和 Snapshot 数量未下降；
- 未认证访问 Manager 用户 API 仍返回 HTTP 401。

一致性备份：

```text
/var/backups/codereviewer/7.2.13-to-7.2.13-20260721-091500/system-backup.tgz
/var/backups/codereviewer/7.2.13-to-7.2.13-20260721-091500/system-backup.tgz.sha256
```

一键还原：

```bash
sudo /usr/local/sbin/codereviewer-rollback-latest
```

当前固定回滚入口为 `/usr/local/sbin/codereviewer-rollback-20260721-091500`，用于恢复本次最终文档同步前已通过验收的 7.2.13 热更新状态、生产配置、用户、数据库、报告和 Jira/PRD 缓存。更早的 `/usr/local/sbin/codereviewer-rollback-20260721-091000` 仍可用于恢复到本轮反馈上线前的 7.2.13 baseline。

## 7.2.13 升级与回滚记录

2026-07-20 将生产环境从 7.2.0 升级到 7.2.13。部署使用 GitHub `20260714` 分支固定提交 `27d7e4cbcaa8617a7b1108367276977d3475f61f` 的 `git archive` 制品，不包含本地未跟踪文件。

生产发布采用以下保护措施：

- 在停止服务后对应用、EnvironmentFile、systemd Unit、`current/data`、`/var/lib/codereviewer/data`、报告及 Jira/PRD 缓存创建一致性归档，并验证 SHA-256 和 tar 可读性；
- 不覆盖生产 `config.yml`，只从 7.2.13 模版合并 `application`、`release_line` 和 `release_lines`；生产 `jira_prd.auto_fetch=true`、Linux working copy、端点、超时及运行策略保持不变；
- `requirements.txt` 依赖集合没有变化，因此生产虚拟环境未修改；
- 首次启动临时关闭账户清理，并在启动前后比对全部 10 个账户的凭据指纹、角色及启停状态；
- SQLite 在事务内从 schema v2 幂等升级至 v3；`integrity_check=ok`，新增 `review_runs.release_line`，旧工作流表记录数量未下降；
- `/api/version`、`/api/health`、登录页、Robot Challenge、登录背景资源及未认证 Manager API 保护均通过；
- 服务保持 `codereviewer:codereviewer` 运行，监听既有生产地址 `0.0.0.0:8765`；`web_users.json` 保持 `0600 codereviewer:codereviewer`。

首次切换后的业务与数据校验已经通过，但最终收集日志时，RHEL 9 的 `journalctl --since` 拒绝带时区偏移的 ISO 时间格式，发布脚本因此自动执行回滚。回滚成功恢复 7.2.0、SQLite schema v2、全部账户与数据；修正为 RHEL 接受的本地时间格式后，第二次发布及独立验收全部通过。

一致性备份：

```text
/var/backups/codereviewer/7.2.0-to-7.2.13-20260720-001524/system-backup.tgz
/var/backups/codereviewer/7.2.0-to-7.2.13-20260720-001524/system-backup.tgz.sha256
```

一键还原：

```bash
sudo /usr/local/sbin/codereviewer-rollback-latest
```

固定回滚入口为 `/usr/local/sbin/codereviewer-rollback-20260720-001524`。脚本会校验备份、停止服务、保留失败版本、恢复 7.2.0 应用/配置/用户/数据库/报告/缓存，并验证版本、账户投影及 SQLite 完整性。

### 生产分支模式修正

部署后复核发现，生产 `config.yml` 仍保留 7.2.0 的具体补丁分支，例如 WVAdmin `1.0.83`、iTrade Client `7.5.0.56`/`7.5.1.38`、DPS `9.3.78`/`11.2.83`。2026-07-20 完成配置级修正：

- 对全部 51 个包含 `repository_url` 的源码及构建仓库校验仓库 URL 后，只同步 `branch` 字段；
- WVAdmin 使用 `1.0.*`，Services Terminal 使用 `5.0.*`；
- iTrade Client 使用 `7.5.0.*` 和 `7.5.1.*`；
- DPS9/DPS11 使用 `9.3.*` 和 `11.2.*`；
- `dev_branch`、生产 Linux 路径、运行端点、超时及 `jira_prd.auto_fetch=true` 未改变；
- 重启后 `/api/health=healthy`，用户文件哈希、SQLite schema v3 和历史数据保持一致。

配置备份与独立回滚：

```text
/var/backups/codereviewer/config-branch-patterns-20260720-065957
/usr/local/sbin/codereviewer-rollback-config-latest
```

该配置级回滚不会替换现有 `/usr/local/sbin/codereviewer-rollback-latest`；后者仍用于完整恢复到 7.2.0。

## 7.2.0 升级与回滚记录

2026-07-17 将生产环境从 7.0.3 升级到 7.2.0。发布前停止服务并备份应用、EnvironmentFile、systemd Unit、用户及工作流数据、报告、Jira/PRD 缓存；制品解压后按字段合并 7.2.0 业务策略与生产 `config.yml`，保留全部 Linux working copy 路径。

验证结果：

- RHEL9 完整自动化测试 `182/182` 通过；
- 线上 10 个账户的用户名与 Credential Hash 在发布前后逐一比对一致；
- `/api/version`、`/login`、Robot Challenge 和未认证 Manager API 保护通过；
- GitLab 443、Codex CLI 可执行文件和生产 DIRECT 网络路径检查通过；
- `web_users.json` 保持 `0600 codereviewer:codereviewer`；
- 旧的明文初始凭据文件未被制品覆盖，权限已收紧至 `0600 codereviewer:codereviewer`，后续应在完成相关账户密码轮换后安全移除。

一致性备份：

```text
/var/backups/codereviewer/7.0.3-to-7.2.0-20260717-203915/system-backup.tgz
/var/backups/codereviewer/7.0.3-to-7.2.0-20260717-203915/system-backup.tgz.sha256
```

一键还原：

```bash
sudo /usr/local/sbin/codereviewer-rollback-latest
```

固定回滚入口为 `/usr/local/sbin/codereviewer-rollback-20260717-203915`。脚本会先验证备份 SHA-256 和 tar 可读性，停止服务，保留失败版本目录，恢复 7.0.3 应用、配置和数据，再启动服务并验证版本。首次发布尝试因生产配置契约测试失败触发了同一回滚流程，实际验证已成功恢复 7.0.3；随后完成字段级配置合并并成功发布 7.2.0。

## 7.0.3 升级记录

2026-07-16 将生产环境从 6.23.0 升级到 7.0.3。部署使用 GitHub `20260714` 分支固定提交 `c66e767064915bd8664ad1af2a10fa7c135a56c2` 的 `git archive` 制品，上传后先在 staging 目录完成 SHA-256、编译和 98 项 RHEL9 测试，再进行短暂停机切换。

数据保护措施和验证结果：

- 原 6.23.0 运行目录完整保留在 `/opt/codereviewer/releases/previous-6.23.0-20260716-191527`；
- 切换前停止服务并再次复制 `/opt/codereviewer/current/data`，保留 `web_users.json`、Review History、Discussion、GitNexus 报告及索引；
- 原有 6 个账户全部保留，并使用升级前密码逐一通过新版 Login API 和 Robot Challenge 验证；
- 7.0.3 将原有明文密码等价迁移为 PBKDF2-SHA256 Hash，实际密码没有改变；
- 新增 4 个试用 Developer 账户后，线上共 10 个用户，认证文件权限保持为 `0600 codereviewer:codereviewer`；
- 新建 `/opt/codereviewer/current/data/codereviewer.db` 保存 7.x Issue Review 工作流，所有原数据文件均通过存在性和 SHA-256 校验；
- 生产 `.env` 原文件备份为 `/etc/codereviewer/codereviewer.env.backup-20260716-191718`，没有写入源码或制品。

7.0.3 默认通过 Codex CLI 接入 `192.168.3.170:8318/v1` CPA Responses API，`fallback_to_cc_switch: false`。升级时发现生产旧 CPA Key 已失效并返回 HTTP 401，更新为当前 CPA 客户端 Key 后，远端 `review.py --codex-check` 已通过。CC Switch 不再作为自动 fallback。

首次部署时 GitHub token 尚缺少 `Contents: write`，因此本次部署使用 `git archive` 生成同一提交的制品，经 SSH/SFTP 传输后解压；运行目录中的 `.deployment-commit` 固定记录上述提交 SHA。2026-07-14 已补齐 `Contents: Read and write` 并成功推送 `main`，后续更新改用 GitHub 仓库作为发布源。

## 实际目录和权限

```text
/opt/codereviewer/current                 应用代码
/opt/codereviewer/venv                    Python 3.11 虚拟环境
/opt/codereviewer/bin/codebase-memory-mcp Linux MCP CLI
/etc/codereviewer/codereviewer.env        生产环境变量
/var/lib/codereviewer/git-repos           GitLab 工作副本
/var/lib/codereviewer/code-review         Review 报告和需求文档
/var/lib/codereviewer/jira-prd/data       Jira/PRD 缓存
/var/lib/codereviewer/data                运行数据
/var/log/codereviewer                     日志目录
```

服务账号为 `codereviewer`，shell 为 `/sbin/nologin`。`codereviewer.env` 权限为 `0640 root:codereviewer`。生产 token 没有写入应用代码、systemd unit 或本文档。

## 本次安装和配置

RHEL9 原来只有 Python 3.9，已安装：

```bash
dnf install -y python3.11 python3.11-pip git
```

该事务同时按 RHEL 仓库依赖升级了 OpenSSL 和 SQLite 相关包。虚拟环境使用 `/opt/codereviewer/venv`，并通过 `requirements.txt` 安装依赖。

部署中发现 `jira==3.12.0` 不存在，已修正为 `jira==3.10.5`。接入 CLIProxyAPI API-key provider 后，CodeReviewer 版本升级到 6.23.0。

生产 `config.yml` 做了以下主机侧转换：

- `D:/TTL/vibe-coding/git-tools/git-repos` 改为 `/var/lib/codereviewer/git-repos`；
- Review 模板改为 `/opt/codereviewer/current/docs/ECHNL-5539.md`；
- workspace roots 改为 Linux 路径；
- 已安装 `/opt/jira-prd/fetch_jira.py`，并设置 `jira_prd.auto_fetch: true`、按需抓取深度为 2；
- 检查确认生产 `config.yml` 中剩余 `D:/TTL` 路径数量为 0。

生产环境从本机 `.env` 安全传输，然后做了以下覆盖：

- Web：`WEB_HOST=0.0.0.0`、`WEB_PORT=8765`；
- IP 白名单：`192.168.3.0/24,127.0.0.1,::1`；
- 报告根目录：`/var/lib/codereviewer/code-review`；
- 中国大陆工作日历：应用内置 JSON 文件；
- Codebase Memory：`/opt/codereviewer/bin/codebase-memory-mcp`；
- 删除 Windows 客户端使用的全部 `CODEBASE_MEMORY_SSH_*` 和 `CODEBASE_MEMORY_REMOTE_*` 项，避免同机部署再次 SSH 回本机。

## Codebase Memory 接入

原 Linux CLI 位于 root 用户目录，服务账号无法直接安全复用。部署时复制到公共应用目录：

```bash
install -o root -g codereviewer -m 0755 \
  /root/.local/bin/codebase-memory-mcp \
  /opt/codereviewer/bin/codebase-memory-mcp
```

验证结果：

```text
codebase-memory-mcp 0.9.0
```

CodeReviewer 和 MCP 位于同一主机，因此不需要启动 MCP Web 服务、开放 9749，或配置 SSH remote adapter。MCP 索引将使用 `codereviewer` 的 HOME 和缓存目录，避免继续占用 root 用户状态目录。

## systemd 和日常操作

```bash
systemctl status codereviewer.service
systemctl restart codereviewer.service
systemctl enable codereviewer.service
journalctl -u codereviewer.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8765/api/version
cat /opt/codereviewer/current/.deployment-commit
```

重新执行编译和测试：

```bash
cd /opt/codereviewer/current
runuser -u codereviewer -- \
  /opt/codereviewer/venv/bin/python -m compileall -q \
  code_reviewer review.py web.py tools/check_codebase_memory.py

runuser -u codereviewer -- env \
  REPORT_OUTPUT_BASE_DIR=/var/lib/codereviewer/code-review \
  /opt/codereviewer/venv/bin/python -m pytest -q
```

## 尚需补齐的生产能力

Web 服务、基础配置、GitLab/Jira token、本地 MCP、Jira/PRD 同步和 Codex CLI 均已部署，但以下外部组件尚不存在：

- `/opt/web-build-tools`，依赖该目录的 Web Build Tools 上下文不可用；
- Nginx 和 HTTPS 终止；当前为内网 HTTP 直连；

当前 `firewalld` 未运行，8765 端口可从局域网访问。应用层已经限制 `192.168.3.0/24` 和 loopback，但生产环境仍建议启用主机防火墙，并使用 Nginx HTTPS 反向代理后把 `WEB_HOST` 改回 `127.0.0.1`。

## Jira/PRD 知识库同步

Jira/PRD 抓取器已安装并验证：

```text
/opt/jira-prd/fetch_jira.py
/opt/jira-prd/.env                      0640 root:codereviewer
/opt/jira-prd/data                      -> /var/lib/codereviewer/jira-prd/data
/var/lib/codereviewer/jira-prd/data     运行数据
```

`codereviewer-jira-prd.timer` 已启用，每周一至周五按 Asia/Shanghai 时区在 08:00、12:00、16:00、20:00 自动同步。CodeReviewer 对缺失 issue 仍可通过 `jira_prd.auto_fetch: true` 即时抓取。

首次同步已成功生成 16 个 Epic 的索引，并验证 CodeReviewer 能从该目录构建上下文。为避免超大 Epic 占用大量内存，`fetch_jira.py` 在读取到 `--max-children + 1` 条子任务时立即停止并跳过该 Epic，而不是先加载全部子任务。

```bash
systemctl status codereviewer-jira-prd.timer
systemctl start codereviewer-jira-prd.service
journalctl -u codereviewer-jira-prd.service -n 100 --no-pager
```

## CLIProxyAPI / Codex 接入

CLIProxyAPI 7.2.71 运行在 Windows 主机 `192.168.3.170:8318`，绑定到该局域网地址而不是 `0.0.0.0`。3.78 已安装 Node.js 22 和官方 `@openai/codex`，Codex CLI 位于 `/usr/local/bin/codex`。

生产 EnvironmentFile 使用以下配置；实际 API key 只保存在 `/etc/codereviewer/codereviewer.env`，不要写入源码或文档：

```bash
CODEX_CLI_PATH=/usr/local/bin/codex
OPENAI_API_KEY=<CLIProxyAPI api-key>
LLM_CODEX_HTTP_API_KEY_ENV=OPENAI_API_KEY
CODE_REVIEW_OVERRIDE_LLM_CODEX_FORCE_HTTP=1
CODE_REVIEW_OVERRIDE_LLM_CODEX_HTTP_BASE_URL=http://192.168.3.170:8318/v1
```

CodeReviewer 6.23.0 在配置 `codex_http_api_key_env` 后，为 Codex 自定义 provider 注入 `env_key=OPENAI_API_KEY` 并关闭交互式 OpenAI 登录要求。未配置该项时，原有 ChatGPT/Codex 登录模式保持不变。

验证结果：

```text
3.78 -> CLIProxyAPI /v1/models: HTTP 200，10 models
3.78 -> CLIProxyAPI /v1/responses: HTTP 200
Codex CLI: /usr/local/bin/codex
Codex check passed.
{"findings":[],"notes":["codex-check-ok"]}
```

验证命令：

```bash
systemd-run --wait --pipe --collect --quiet \
  --uid=codereviewer --gid=codereviewer \
  --property=WorkingDirectory=/opt/codereviewer/current \
  --property=EnvironmentFile=/etc/codereviewer/codereviewer.env \
  /opt/codereviewer/venv/bin/python review.py \
  --codex-check --codex-check-timeout 180
```

Windows 当前已有名为 `cli-proxy-api` 的 Public 入站允许规则。创建仅允许 `192.168.3.78` 的收窄规则需要管理员权限；应在管理员 PowerShell 中移除/禁用宽泛规则，并只允许来源 `192.168.3.78` 访问 TCP 8318。即使 API key 校验已启用，也不建议对整个局域网开放。

CLIProxyAPI 当前依赖 Windows 主机和登录会话持续运行。Windows 重启、休眠、IP 变化或代理进程退出都会使 DPS Review 失败；长期生产建议把 CLIProxyAPI 配置为受控的开机启动服务，或迁移到固定的服务器节点。

## 后续 GitHub 更新流程

GitHub 首次推送已经完成。建议把现有制品目录保留为回滚副本，再部署固定 SHA：

```bash
systemctl stop codereviewer.service
mv /opt/codereviewer/current /opt/codereviewer/releases/previous-4af1960
git clone https://github.com/jay-researcher/CodeReviewer.git /opt/codereviewer/current
cd /opt/codereviewer/current
git checkout <approved-commit-sha>
chown -R codereviewer:codereviewer /opt/codereviewer/current
/opt/codereviewer/venv/bin/pip install -r requirements.txt
systemctl start codereviewer.service
curl -fsS http://127.0.0.1:8765/api/version
```

执行更新前应备份生产 `config.yml` 的 Linux 路径转换，并确认不会被仓库中的 Windows 开发配置覆盖。更稳妥的长期方案是将生产配置独立放到 `/etc/codereviewer`，由环境变量指向该文件。

## Release Notes 收尾检查

版本部署到 `192.168.3.78` 并通过版本、健康状态和站点访问验证后，生产发布尚未结束，还必须完成以下文档收尾：

1. 更新 `7.x-docs/7.x release notess.md` 中对应版本的发布状态，记录实际部署日期、生产地址、commit 和验证结果。
2. 更新 `7.x-docs/public release notes.md` 中对应版本的公开状态。
3. 移除对应版本中“仅更新本地验收环境；未部署到 192.168.3.78”或含义相同的过期文字，不得同时保留“已部署”和“未部署”两种冲突状态。
4. 确认站点 Release Notes 弹窗显示的是更新后的公开内容，再提交并推送文档变更。

仅完成本地验收、尚未部署生产时，应继续保留“仅本地”状态。
