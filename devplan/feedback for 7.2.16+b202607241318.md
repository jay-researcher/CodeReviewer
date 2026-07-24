- [本机已完成，待 Review] Issue Review History > Issue Review Details 面板：采用全屏大规格 Dialog，重新组织顶部导航、Cycle 上下文、应用级报告与问题处理区域；统一字段高度、留白、状态色和空状态，使 Overview、Issues 与 Report History 的信息层级一致。![alt text](image-34.png)

- [本机已完成，待部署] Issue Review Details / Progress / Report History 状态一致性与视觉优化：
  - Delivery Cycle 改为独立字段组，Label 与 Select 保持舒适间距；下拉框高度统一为 40px，并与 Cycle 状态对齐。
  - Resume 的 `status=done` 不再直接等同于报告存在；如果 Resume 记录的单份或多份报告文件已被清理，当前 Run 自动标记为 stale 并重新审核，不再错误显示 `SKIP DONE`。
  - Progress 显示 `Previous completion ... has no report artifact; rebuilding`，明确说明自动重建原因。
  - Report History 空状态区分“No matching reports”和“No reports in this range”，提供清除搜索、运行 Issue Review或扩大历史范围的下一步指引。
  - 验收结果：完整自动化测试 308 项通过、2 项跳过；内嵌 JavaScript 语法校验通过；本机 `127.0.0.1:8765` 健康，版本暂维持 `7.2.16+b202607241318`；尚未部署 3.78。

- User Management：将当前扁平的 `Responsible identifiers` 升级为 **Responsible per Application Scope**。

  **问题**

  当前用户只能配置一组全局 Responsible identifier，无法准确表达“一位同事负责多个应用”“同一应用由多人负责”以及“只负责某个应用中的部分报告”。例如 `hieut.tran` 同时负责 Services Terminal 和 WVAdmin 的部分工作，扁平配置可能导致授权范围跨应用扩散。

  **授权模型**

  - 非 Manager 用户的最小授权单位为 `Application Scope AND Responsible identifier`。
  - 同一授权项中的 Application 与 Responsible 使用 AND 判断。
  - 同一用户配置的多个授权项之间使用 OR 判断。
  - Manager 保持全局权限，不应用 Responsible per Application Scope 限制。
  - 报告真实负责人和代审权限必须分开建模：
    - `Responsible`：报告及工作归属。
    - `Application Scope`：授权生效的应用边界。
    - `Review grants`：允许用户跨 Responsible 代审，不改变报告真实 Responsible。

  ```text
  allowed =
    is_manager
    OR any(
      report.application_scope == grant.application_scope
      AND report.responsible_scope contains grant.responsible_identifier
    )
  ```

  **示例**

  | Application Scope | Responsible identifier | 权限范围 |
  | --- | --- | --- |
  | Services Terminal | `hieut.tran` | Services Terminal 中 Responsible 包含 `hieut.tran` 的报告 |
  | WVAdmin | `hieut.tran` | WVAdmin 中 Responsible 包含 `hieut.tran` 的部分报告 |
  | WVAdmin | `wen.yi` | 如配置为代审授权，可审核 WVAdmin 中 Wen Yi 负责的报告 |

  **建议数据结构**

  ```json
  {
    "responsible_scopes": [
      {
        "application": "Services Terminal",
        "responsibles": ["hieut.tran"]
      },
      {
        "application": "WVAdmin",
        "responsibles": ["hieut.tran"]
      }
    ],
    "review_grants": []
  }
  ```

  **Web 设计**

  - User Management 中将 Responsible Scope 按 Application 分组展示。
  - 每个 Application 使用清晰的 Scope 卡片或紧凑表格，支持选择一个或多个 Responsible identifier。
  - 用户负责多个应用时显示多行 Scope；同一应用存在多个 Responsible 时在该 Scope 内显示多个标识。
  - 每个 Scope 显示权限摘要，例如 `WVAdmin · 1 Responsible`，并明确区分“工作归属”和“代审权限”。
  - 无授权的应用显示 `No access`，避免空白状态产生歧义。
  - 保存前展示最终 Effective Access 摘要，便于管理员确认是否意外扩大权限。

  **兼容与迁移**

  - 现有扁平字段 `responsibles` 不应自动解释为“所有应用”，以免扩大历史账号权限。
  - 升级时应根据现有报告、项目配置及明确的人员职责生成待确认的 Application Scope 映射。
  - 迁移未确认的用户应显示 `Migration required`，由 Manager 确认后启用新授权。
  - 后端接口、Issue Review History、Reports、Sprint Review 和 Report Preview 必须使用同一授权函数，避免各页面过滤结果不一致。

  **验收标准**

  - `hieut.tran` 可同时查看 Services Terminal 与 WVAdmin 中授权给自己的报告。
  - `hieut.tran` 不会因拥有 WVAdmin Scope 而看到 WVAdmin 中其他 Responsible 的报告。
  - 同一用户可配置多个 Application Scope，同一 Scope 可配置多个 Responsible。
  - Manager 始终具有全局访问能力。
  - Issue Review History、Report 列表、Report Preview、Sprint Review 统计及相关 API 的可见数据保持一致。
  - 未映射、旧格式及迁移中账号不会获得隐式的全应用权限。

  **微前端多应用 Issue 补充规则**

  - 本规则中的 `Base` 是 WVAdmin 微前端架构中的前端基座应用，不是 DPS 后端 Base。
  - 前端与后端分别创建 Jira Issue，因此此处只处理一个前端 Issue 涉及多个微前端应用的情况，不再执行前后端拆分。
  - Jira Component `WVAdmin` 表示微前端平台或技术域，不能单独作为 Responsible 的最终判断依据。
  - 当 Issue 同时包含 AOP、TMO、Base 等多个前端应用时，应按前端 Application Scope 分别生成报告和确定 Responsible：

  | Frontend Application Scope | 主要识别依据 | Responsible |
  | --- | --- | --- |
  | AOP | Account Opening System、AOP/LCA Repository 或对应文件路径 | `victorcz.xu` |
  | TMO / Services Terminal | Trade Middle Office、TMO/Services Terminal Repository 或对应文件路径 | `hieut.tran` |
  | Base / WVAdmin 基座 | Base Repository 或微前端基座文件路径 | `wen.yi` |

  - Component 同时包含 `WVAdmin`、`Account Opening System`、`Trade Middle Office` 时：
    - `WVAdmin` 只用于确认所属微前端体系。
    - `Account Opening System` 驱动 AOP Scope。
    - `Trade Middle Office` 驱动 TMO / Services Terminal Scope。
    - 只有 MR 或文件清单实际涉及 Base 项目时，才额外生成 Base / WVAdmin 基座 Scope。
  - 不得先从整个 Issue 提取 Victor、Hieu 等人员列表，再把所有人员批量赋给全部应用。
  - Responsible 必须保存为明确的 Application-to-Responsible 对应关系：

  ```json
  {
    "application_scopes": [
      {
        "application": "AOP",
        "responsibles": ["victorcz.xu"]
      },
      {
        "application": "Services Terminal",
        "responsibles": ["hieut.tran"]
      },
      {
        "application": "WVAdmin",
        "release_line": "Base",
        "responsibles": ["wen.yi"]
      }
    ]
  }
  ```

  - 同一个 Issue 应在列表中保持唯一，但可关联多份应用级报告；UI 展示各 Application Scope 及其 Responsible。
  - 同一个 MR 跨越多个微前端应用时，同一 MR 可以被多个 Scope 引用，但每份报告只能分析属于该应用的文件 Diff，不能在各报告中重复审核整个 MR。
  - Scope 推导顺序为：Frontend Repository / MR → 文件路径所属微前端应用 → 业务 Component → `config.yml` 对应项目 Responsible → `Mapping required`。
  - User Management 和数据访问也必须按应用级 Scope 判断：
    - Hieu 访问 TMO / Services Terminal 授权范围。
    - Victor 访问 AOP 授权范围。
    - Wen Yi 访问 WVAdmin Base 授权范围。
    - 用户可以看到同一个 Jira Issue，但只能进入其有权访问的应用报告。

  **处理状态**

  - [本机已完成，待 Review] User Management 已支持 `Application Scope AND Responsible identifier` 配对授权，同一用户可配置多个应用，同一应用可配置多个 Responsible。
  - [本机已完成，待 Review] 旧扁平账号不会解释为所有应用；系统按项目配置生成最小权限映射，并显示 `Migration required`，Manager Review 并保存后转为显式映射。
  - [本机已完成，待 Review] Reports、Projects、Issue Review History、Issue Details、Sprint Review Coverage 共用应用与 Responsible 配对判断；多应用 Issue 仅返回当前用户可访问的 Run，非全 Cycle 权限时禁用 Manual Pass。
  - [本机已完成，待 Review] `review_domains` 保持为独立 Reviewer grant，不修改报告真实 Responsible。
  - [本机已完成，待 Review] WVAdmin 微前端项目已细分：AOP/LCA 项目归 `AOP / victorcz.xu`，Trade Middle Office 归 `Services Terminal / hieut.tran`，Base/Common/Coms/Workflow 维持 `WVAdmin / wen.yi`，MOMD 维持 `WVAdmin / hieut.tran`。

- [本机已完成，待 Review] Sprint Review / Cycle / Resume / Overview 统计闭环：
  - Sprint 扫描创建的空 Cycle 不再被视为“已审核”；只有同一 Sprint 且存在已持久化 Review Run 的 MR revision 才允许跳过。
  - Legacy / Unknown Sprint 的旧 Run 不再抑制当前命名 Sprint 的报告生成。
  - Resume key 纳入 Sprint 边界，同一 MR 在不同 Sprint 不会因旧 `done` 记录而跳过。
  - 本周新报告在报告文件 metadata 与 History 中同时持久化 Sprint/Cycle 身份；即使 History 需要重建，也不会丢失交付归属。
  - 标记为当前交付的报告若缺少 Sprint 身份，只允许绑定唯一的开放非 Legacy Cycle；无法唯一确定时拒绝登记，绝不自动降级到 `Legacy / Unknown Sprint`。
  - 因此 Sprint Review 后 Overview 不会再出现“任务完成但当前 Cycle 0 report、全部进度重置为 0%”的假完成状态。
  - Pass 不跨 Cycle 静默继承；当前 Cycle 必须存在完整应用级报告，再按现有 Pass 完整性规则处理。

- [本机已完成，待 Review] Web 交互与视觉一致性：
  - Sprint View 筛选器和 Delivery Cycle 控件统一高度、Label 间距与状态徽标对齐。
  - Run Review 开始后折叠输入面板时保持浏览器视口位置，不再把页面顶部顶出可视区。
  - 用户拖动 Progress 垂直滚动条期间暂停自动滚动；即使拖动超过 1 分钟，只要未释放仍不会恢复，释放后再按延迟策略恢复。
  - Report History、无当前 Cycle Run、无匹配报告等空状态提供明确原因和下一步操作。

- **本轮验收状态**
  - 自动化测试：308 项通过，2 项跳过。
  - 前端校验：内嵌 JavaScript 语法通过；登录页、登录 Challenge、Health、Version 本机 HTTP 冒烟通过。
  - 代码质量：`git diff --check` 通过。
  - 部署边界：仅部署本机 `127.0.0.1:8765`；未部署 `192.168.3.78`。
  - 版本策略：本机候选仍基于 `7.2.16+b202607241318`，待功能 Review 后再决定是否生成新的 3.78 部署版本。
