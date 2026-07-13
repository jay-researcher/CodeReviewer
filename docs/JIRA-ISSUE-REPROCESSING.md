# Jira Issue 再处理及审查规则

## 适用场景

Jira Issue 完成第一个 Sprint 的处理和交付后，如果后续仍需继续处理，不覆盖或删除第一次交付的信息，而是通过新的 description 模版 comment 补充本次跟进工作。

## 再处理流程

1. 根据 Issue description 模版说明需要跟进的工作；修复问题必须使用 Bug 缺陷类模版。
2. 将 Issue 状态按 `Backlog -> Open` 流程重新打开。
3. 设置本次处理所属的 Sprint。
4. 处理完成、交付之前，通过 comment 补充测试说明和测试结果。
5. 测试说明与测试结果必须分别回复为独立 comments，不得写在同一个 comment 中。

## 最终 Issue Description

CodeReviewer 按以下顺序形成审查使用的最终 Issue description：

1. 原始 Issue description；
2. 按创建时间升序排列的、包含完整 Issue description 模版的 comments。

普通沟通、测试说明和测试结果 comments 不会合并到最终 description。

## Description 模版判定

Comment 必须包含一个三列表格，否则不会被视为 description 模版 comment。

| 语言 | 第一列 | 第二列 | 第三列 |
| --- | --- | --- | --- |
| 中文 | 截图 | 说明 | 补充 |
| English | Screenshot | Description | Additional remarks |

### 非缺陷类第二列

- 需求描述 / Requirement Description
- 需求分析 / Requirement Analysis
- 解决方案 / Proposed Solution
- 预期结果 / Expected Result

### Bug 缺陷类第二列

- 问题描述 / Bug Description
- 问题分析 / Bug Analysis
- 解决方法 / Workaround

### 第三列公共内容

- 受影响的项目或功能范围 / Affected Project or Functional Scope
- 涉及的文件清单 / Involved File Lists

## Review 行为

- 单 Jira Issue、Jira Filter、Sprint 和多 Issue 审查使用相同的合并规则。
- 最终 description 会用于涉及文件清单核对、GitLab MR 链接发现以及 LLM 审查上下文。
- 多个合规模版 comments 全部保留，并按 Jira 返回的创建时间顺序合并。
