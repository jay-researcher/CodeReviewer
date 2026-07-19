# CodeReviewer 构建仓库与应用边界知识

版本基线：CodeReviewer 7.2.13  
配置权威来源：`config.yml`

## 1. 核心结论

CodeReviewer 必须区分三个不同维度：

1. **应用（Application）**：发布、报告拆分和 Release Gate 的主要业务边界。
2. **项目类型（Project Type）**：`frontend` 或 `backend`，用于选择审核规则和技术上下文。
3. **源码/构建仓库（Source/Build Repository）**：源码 MR 提供本轮增量；构建仓库的 GIT_VERSION MR 锁定最终发布内容。

`frontend/backend` 不能替代应用名。iTrade Client、Services Terminal、WVAdmin 都是 frontend，但它们拥有不同的源码仓库、构建仓库和 GIT_VERSION Release Gate。

## 2. 应用、源码与构建仓库映射

| 应用 | 类型 | 主版本/版本线 | `config.yml` 源码节点 | 构建仓库 |
| --- | --- | --- | --- | --- |
| iTrade Client | frontend | 主版本 `7.5`；版本线 `7.5.0.x`、`7.5.1.x` | `itrade-client` 下的 iTrade Client repositories | `build-repository.itrade-client` → `web-sv-build/webfe/itrade-client` |
| Services Terminal | frontend | `5.0.x` | `itrade-client.services-terminal` | `build-repository.services-terminal` → `web-sv-build/webfe/services-terminal` |
| WVAdmin | frontend | `1.0.x` | `wvadmin-repository` | `build-repository.wvadmin` → `web-sv-build/webfe/wvadmin` |
| DPS | backend | DPS9 `9.3.x`；DPS11 `11.2.x` | `dps9-repository`、`dps11-repository` | `build-repository.dps` → `web-sv-build/dps` |

iTrade Client 的应用身份保持为一个，但 `7.5.0.x` 与 `7.5.1.x` 是两个并行交付版本线，均归属于 `7.5` 主版本。DPS 是 Release Gate 的应用汇总边界，DPS9、DPS11 是该应用的后端主版本线。普通代码审核和构建锁校验必须继续保留具体版本线，不能把 iTrade Client 7.5.0/7.5.1 或 DPS9/DPS11 的源码、资源和门禁结果混在同一版本审核范围内。

iTrade Client 的版本线应由配置条目及 MR target/source branch 的版本模式共同判定，不能仅根据相同的 repository URL 推断。匹配 `7.5.0.*` 的 MR 归入 7.5.0.x，匹配 `7.5.1.*` 的 MR 归入 7.5.1.x；无法判定时标记为 `Unmapped release line`，不得合并进另一条版本线。

## 3. 报告拆分规则

一个 Jira Issue 可以涉及多个应用。报告首先按应用拆分，同一应用内再根据配置元数据保留项目类型、源码仓库、MR 和 Responsible：

```text
Jira Issue
  └─ Review Cycle
      └─ Run Group
          ├─ WVAdmin report
          ├─ iTrade Client report
          │   └─ 7.5.0.x or 7.5.1.x release line
          ├─ Services Terminal report
          └─ DPS report
              └─ DPS9 or DPS11 release line
```

规则：

- 同一 Issue、同一应用的多个源码 MR 合并为一份应用报告，保留每个 MR 的来源和 changed files。
- 同一应用存在多个并行版本线时，先按版本线隔离；只有同一版本线内的多个源码 MR 才合并为一份报告。
- 同一 Issue 涉及多个应用时，每个应用生成独立报告、Finding 清单、Handling 状态和审核快照。
- `type` 仍作为报告元数据和审核规则选择条件，但不应作为唯一拆分键。
- 报告名使用应用名，不重复附加可由应用确定的 `frontend/backend`。

示例：

```text
ECHNL-8888_WVAdmin_has-issue-high.md
ECHNL-8888_iTrade-Client-7.5.0_has-issue-high.md
ECHNL-8888_iTrade-Client-7.5.1_has-issue-medium.md
ECHNL-8888_Services-Terminal_has-issue-medium.md
ECHNL-8888_DPS9_has-issue-critical.md
ECHNL-8888_DPS11_has-issue-high.md
```

## 4. Responsible 与访问范围

Responsible 应来自本轮应用报告实际包含的配置项目，而不是仅保存 Jira Issue 上的一个拼接字符串：

- 普通源码报告使用相关源码 repository 的 Responsible。
- Company Config、SCR 等 deferred 构建资源使用对应构建 repository 的 Responsible。
- 一个应用报告包含多个 Responsible 时，保存结构化 `responsible_scope` 集合。
- Developer/Auditor 的可见性取本 Review Cycle 内各应用报告 `responsible_scope` 的并集；Manager 保持全局可见。
- Issue 主记录上的单一 `responsible` 只可作为显示或兼容字段，不能成为多应用访问控制的唯一依据。

## 5. GIT_VERSION Release Gate

Release Gate 一次审核一个应用构建仓库中的 GIT_VERSION MR：

1. Sprint Review 汇总各应用的普通源码报告，以及 deferred Company Config/SCR。
2. Manager 为本次要发布的应用选择对应构建仓库的 GIT_VERSION MR。
3. Release Gate 读取 MR 中版本化的 `git_version.yml` 和 `build.yml`。
4. `git_version.yml` 用于核对发布所锁定的源码 repository、branch 和 commit。
5. `build.yml` 用于核对构建参数、版本资源以及构建仓库锁。
6. CodeReviewer 对比前后版本锁，检查构建资源、Company Config、SCR/数据库变更及引用文件。
7. 同一 Sprint 发布多个应用时，分别运行各应用的 Release Gate；应用级结果再汇总为 Sprint release readiness。

选择 MR 时的判断标准：

| 要发布的应用/版本线 | 应输入的 MR |
| --- | --- |
| iTrade Client 7.5.0.x | `build-repository.itrade-client` 中目标/版本分支属于 `7.5.0.*` 的 GIT_VERSION MR |
| iTrade Client 7.5.1.x | `build-repository.itrade-client` 中目标/版本分支属于 `7.5.1.*` 的 GIT_VERSION MR |
| Services Terminal | `build-repository.services-terminal` 对应 GitLab project 的 GIT_VERSION MR |
| WVAdmin | `build-repository.wvadmin` 对应 GitLab project 的 GIT_VERSION MR |
| DPS9 / DPS11 | `build-repository.dps` 对应 GitLab project 的 GIT_VERSION MR，并核对其版本分支属于 `9.3.*` 或 `11.2.*` |

普通源码 MR、Company Config MR 或 SCR MR 不能代替 GIT_VERSION MR 运行最终 Release Gate。

## 6. 构建工具知识来源

### 6.1 前端构建

本地知识源：`D:\TTL\vibe-coding\web-build-tools`

该项目覆盖 iTrade Client、Services Terminal、WVAdmin。相关资源目录分别包含各应用的：

- `git_version.yml`；
- `build.yml`；
- 构建脚本；
- Company/SV、公司配置、环境配置和 release notes 处理。

关键约束：

- 每个前端应用拥有独立构建仓库和发布版本线。
- iTrade Client 的构建仓库同时维护 `7.5.0.*`、`7.5.1.*`；两者同属 `7.5` 主版本，但必须分别锁定和审核。
- GIT_VERSION 分支至少需要完成版本锁、构建锁及用最终 commit 回写构建锁的提交过程。
- `git_version.yml` 是否包含相应 GitLab repository 配置会影响应用配置包是否进入构建。

### 6.2 DPS 后端构建与部署

本地知识源：`D:\TTL\vibe-coding\dps-build&deploy-tools`

DPS 构建仓库同时编排 DPS9、DPS11 的多个后端源码 repository，并生成代码、配置、数据、脚本和数据库部署资源。

关键约束：

- `release/<version>/git_version.yml` 锁定该版本需要构建的源码。
- `release/<version>/build.yml` 锁定构建仓库的 branch/commit 和构建参数。
- `config11`、`database11`、`script11`、`data11` 是 DPS11 覆写层；DPS9 使用默认层。
- 配置采用 `company/SV → company/<company> → release/<version>/SV → release/<version>/<company>` 的继承/覆写顺序。
- SCR、`db_change.scr`、MySQL/MongoDB 资源及部署脚本属于 Release Gate 必须核对的后端发布证据。

## 7. 配置与实现约束

- `config.yml #build-repository` 是应用构建仓库映射的权威来源。
- 新增应用时必须同时配置稳定的 `application`、`type`、repository URL、版本分支和 Responsible。
- Services Terminal 虽然当前源码配置位于 `itrade-client` 顶层节点下，但它是独立应用；实现不能仅用 YAML 顶层节点推断应用。
- iTrade Client 7.5.0/7.5.1 可能使用相同 repository URL；实现必须结合配置条目和 MR branch 判定 `release_line`。
- Sprint Overview、Issue Review History、报告文件名和 Release Gate 都应使用同一套规范化应用标识。
- 无法映射应用时标记为 `Unmapped` 并阻止该项进入最终 Release Gate，而不是猜测归属。

## 8. 7.2.13 实现状态

7.2.13 已完成应用边界落地：

1. 配置 repository 可持久化 `application`、`release_line` 或 `release_lines`；同 URL 的 iTrade Client 源码仓库结合 MR source/target branch 识别 7.5.0/7.5.1。
2. LLM 输入和报告均按 `Application + Release Line` 切分；同范围多个 MR 合并，不同范围互不污染。
3. `type` 仅用于选择 frontend/backend 审核规则，不再作为报告主分组键。
4. Issue 可见性使用全部 Review Run 的 `responsible_scope` 并集，不再由最后写入的 Responsible 决定。
5. Sprint Overview、Issue Review、报告文件名与 Release Gate 共用 WVAdmin、Services Terminal、iTrade Client 7.5.0/7.5.1、DPS9/DPS11 标识。
6. 无法确认的应用或版本线进入隔离的 `Unmapped` 范围，并阻止 Release Gate 就绪。

## 9. 维护规则

以下任一内容变化时，应同步更新本文档及 7.x User Manual：

- `config.yml #build-repository`；
- 应用、iTrade Client 版本线或 DPS 主版本线；
- GIT_VERSION/build 文件格式；
- 前端或 DPS 构建工具的锁定、继承与资源规则；
- 应用报告拆分、Responsible 或 Release Gate 汇总规则。
