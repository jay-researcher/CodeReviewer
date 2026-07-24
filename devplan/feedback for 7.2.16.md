- 修复与改善Sprint Review “The read operation timed out”：Jira GET 请求增加有限重试，例如最多 2 次并采用短退避。单个 Issue Comment 超时后记录 warning 并继续扫描，不阻断整个 Sprint。Comment 请求并发化，但限制为 4–6 个并发，避免冲击 Jira。错误信息加入 Jira Key、接口和阶段。Progress 展示 Loading Jira comments 12/30 等实际进度。只有 Sprint 搜索本身失败时才判定整个 Scan 失败；辅助 Comment 失败应返回“部分完成 + 警告”。
- User Management：responsible per application scope ![alt text](image-33.png)

## 7.2.16+b202607241318 功能清单

### Responsible、Repository 与 Reviewer 边界

- Repository Responsible、Jira Component-driven Delivery Responsible 与 Reviewer 权限作为三个独立维度处理，不允许相互覆盖。
- 修复最终报告按应用 Scope 拆分时，Git Tools 仓库 fallback owner 覆盖 Jira Component-driven Responsible 的问题；推导出的负责人会贯穿报告目录、隐藏元数据与 Workflow Scope。
- WVAdmin 前端构建仓库 `build-repository.wvadmin` 的 Repository Responsible 为 `wen.yi`。
- WVAdmin/MOMD 与 Trade Middle Office 仓库的 Repository Responsible 为 `hieut.tran`。
- AOP/LCA 仓库 `low_code_designable`、`low_code_renderable`、`low_code_application`、`account_middle_office`、`form_designable` 的 Repository Responsible 为 `victorcz.xu`。
- Web 公共仓库 `coms`、`workflow_app`、`base`、`common` 的 Repository Responsible 为 `wen.yi`。
- Jira Component-driven Delivery Responsible 继续按应用与 Components 推导；无驱动规则时才回退到 Repository Responsible。
- `wen.yi` 继续作为 Web Frontend Reviewer，可审核 WVAdmin 与 Services Terminal；Reviewer 权限不会改变报告的 Delivery Responsible。
- 部署配置合并同步 `responsible` 字段，避免生产保留旧的仓库负责人映射。

### Run Review 页面滚动稳定性

- 点击 Run Review 后仍自动折叠输入区，但不再调用 `Progress.scrollIntoView()` 将整个页面顶到上方。
- 折叠前记录浏览器 viewport；折叠后保持原页面位置，避免顶部导航、Run Review 标题与上下文移出视口。
- 键盘焦点仍使用 `preventScroll` 安全转移到 Progress，兼顾无障碍操作且不改变页面位置。

### Progress 自动滚动保护

- 鼠标滚轮、键盘、触摸或拖动垂直滚动条后，Progress 自动跟随暂停 60 秒。
- 用户按住 scrollbar thumb 期间无限暂停；即使超过 60 秒，只要未释放就不得自动滚动。
- 使用全局 `pointerup` / `pointercancel` 捕获释放，避免轮询重建 Progress DOM 后丢失拖动状态。
- 用户释放后重新开始完整的 60 秒静默期；静默期内再次操作会重新计时。
- 静默期结束后才恢复自动跟随最新事件；用户主动点击 `Jump latest` 可立即跳至最新内容。
