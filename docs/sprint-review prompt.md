# Jira Filter 提取 Prompt 模版

## 基础版本（提取 sprint + issue keys）

```text
请提取当前 filter 包含的 sprint id 及名称；提取 filter 包含的所有 issue keys，使用逗号分隔；

按照下面的 yaml 模版，整理输出：

sv-sprint:
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>

sprint-jira-issues:
  - "<ISSUE-KEY>"

要求：
1. 列出 filter 中涉及的所有 sprint（去重），包含 sprint_name 和 sprint_id
2. 列出 filter 中所有匹配的 issue key（去重），每个 issue key 用双引号包裹
3. 输出末尾汇总 sprint 数量和 issue 总数
```

---

## 进阶版本（指定 Filter ID）

```text
请读取 Jira Filter ID: {FILTER_ID}，提取其中包含的：
1. 所有 sprint id 及名称（去重）
2. 所有 issue keys（去重）

按照下面的 yaml 模版输出：

sv-sprint:
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>

sprint-jira-issues:
  - "<ISSUE-KEY>"

末尾汇总 sprint 数量和 issue 总数。
```

---

## 定制版本 A：按项目分组

```text
请读取 Jira Filter ID: {FILTER_ID}，提取其中包含的：
1. 所有 sprint id 及名称（去重）
2. 所有 issue keys（去重），按项目 (project key) 分组

按照下面的 yaml 模版输出：

sv-sprint:
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>

sprint-jira-issues-by-project:
  <PROJECT_KEY>:
    - "<ISSUE-KEY>"
    - "<ISSUE-KEY>"
  <PROJECT_KEY>:
    - "<ISSUE-KEY>"

要求：
1. sprint 列表去重，按 sprint_id 升序排列
2. issue keys 按项目 key 分组，每组内按 issue 编号升序排列
3. 末尾汇总：sprint 数量、各项目 issue 数量、issue 总数
```

---

## 定制版本 B：按 Sprint 分组 Issues

```text
请读取 Jira Filter ID: {FILTER_ID}，提取其中包含的：
1. 所有 sprint id 及名称（去重）
2. 所有 issue keys（去重），按所属 sprint 分组

按照下面的 yaml 模版输出：

sv-sprint:
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>
    issues:
      - "<ISSUE-KEY>"
      - "<ISSUE-KEY>"
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>
    issues:
      - "<ISSUE-KEY>"

要求：
1. sprint 按 sprint_id 升序排列
2. 每个 sprint 下列出属于该 sprint 的所有 issue keys
3. 若某 issue 属于多个 sprint，在每个相关 sprint 下都列出
4. 末尾汇总：sprint 数量、各 sprint issue 数量、issue 总数（去重）
```

---

## 定制版本 C：按项目 + Sprint 双层分组

```text
请读取 Jira Filter ID: {FILTER_ID}，提取其中包含的：
1. 所有 sprint id 及名称（去重）
2. 所有 issue keys（去重），先按项目分组，再按 sprint 分组

按照下面的 yaml 模版输出：

sv-sprint:
  -
    sprint_name: <sprint名称>
    sprint_id: <sprint ID>

sprint-jira-issues-by-project-and-sprint:
  <PROJECT_KEY>:
    <sprint_id>:
      sprint_name: <sprint名称>
      issues:
        - "<ISSUE-KEY>"
        - "<ISSUE-KEY>"
    <sprint_id>:
      sprint_name: <sprint名称>
      issues:
        - "<ISSUE-KEY>"
  <PROJECT_KEY>:
    <sprint_id>:
      sprint_name: <sprint名称>
      issues:
        - "<ISSUE-KEY>"

要求：
1. 第一层按项目 key 分组
2. 第二层按 sprint_id 分组（升序）
3. 每个 sprint 节点包含 sprint_name 和该项目在该 sprint 下的 issue 列表
4. 若某 issue 属于多个 sprint，在每个相关 sprint 下都列出
5. 末尾汇总表格：

| 项目 | Sprint | Issue 数量 |
|------|--------|-----------|
| ... | ... | ... |
| **合计** | | **总数** |
```

---

## 使用说明

| 变量 | 说明 | 示例 |
|------|------|------|
| `{FILTER_ID}` | Jira Filter 的 ID | `10234` |

### 使用步骤

1. 选择适合的模版版本（基础 / 按项目 / 按 Sprint / 双层分组）
2. 替换 `{FILTER_ID}` 为实际值，或直接在 filter 页面发送 prompt
3. 发送给 Rovo，等待输出结果
4. 可将输出直接用于 CI/CD 配置、发版清单、Sprint Review 等场景
