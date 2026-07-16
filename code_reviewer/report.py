from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from .config import app_config_bool, app_config_int, report_language
from .models import ChangedFile, Finding
from .models import ReviewInput, ReviewResult


def render_markdown(result: ReviewResult, language: str | None = None) -> str:
    language = _normalize_language(language or report_language())
    labels = _labels(language)
    sep = ": " if language == "en" else "："
    source = result.review_input
    counts = result.severity_counts
    audit_conclusion_heading = "Review Conclusion" if language == "en" else "审核结论"
    handling_template_heading = "Handling Template" if language == "en" else "处理模版"
    other_heading = "Other" if language == "en" else "其他"
    lines = [
        f"# {source.jira_key or source.project or 'MR'} {labels['title']}",
        _report_metadata_comment(source),
        "",
        f"## {labels['basic_info']}",
        "",
        f"- {labels['project']}{sep}{source.project or '-'}",
        f"- MR{sep}{source.mr_url or source.mr_id or '-'}",
        f"- Source Branch{sep}{source.source_branch or '-'}",
        f"- Target Branch{sep}{source.target_branch or '-'}",
        f"- Commit{sep}{source.commit or '-'}",
        f"- Jira{sep}{source.jira_key or '-'}",
        f"- Sprint{sep}{source.sprint or '-'}",
        f"- MR Type{sep}{source.metadata.get('mr_type', '-')}",
        f"- Project Type{sep}{source.metadata.get('project_type') or source.metadata.get('git_tools_project_type') or '-'}",
        f"- LLM Provider{sep}{source.metadata.get('llm_provider', '-')}",
        f"- LLM Model{sep}{source.metadata.get('llm_model', '-')}",
        f"- LLM Reasoning Effort{sep}{source.metadata.get('llm_reasoning_effort', '-')}",
        f"- LLM Speed{sep}{source.metadata.get('llm_speed', '-')}",
        f"- LLM Prompt Chars{sep}{source.metadata.get('llm_prompt_chars', '-')}",
        f"- LLM Context Budget{sep}{_llm_context_budget_summary(source.metadata)}",
        f"- Report Minimum Severity{sep}{source.metadata.get('report_min_severity', 'Medium')}",
        f"- Filtered Findings Below Minimum{sep}{source.metadata.get('filtered_out_finding_count', 0)}",
        f"- SVREQ{sep}{source.metadata.get('svreq_issue', '-')}",
        f"- Action Issue{sep}{source.metadata.get('action_issue', '-')}",
        f"- {labels['responsible']}{sep}{_responsible_summary(source.metadata)}",
        f"- GitLab Project Match{sep}{_git_tools_project_match_summary(source.metadata)}",
        f"- Jira PRD Context{sep}{_jira_prd_context_summary(source.metadata)}",
        f"- Network Stage{sep}{source.metadata.get('network_stage', '-')}",
        f"- {labels['project_context']}{sep}{_project_context_summary(source.metadata)}",
        f"- {labels['review_time']}{sep}{source.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    related_mrs = source.metadata.get("related_merge_requests") or []
    if isinstance(related_mrs, list) and related_mrs:
        lines.extend([f"## {labels['related_mrs']}", ""])
        lines.append(
            f"| MR | {labels['request_by']} | {labels['status']} | {labels['project']} | Source Branch | Target Branch | {labels['commit']} | {labels['responsible']} | GitLab Project Match | {labels['files']} |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: |")
        for item in related_mrs:
            if not isinstance(item, dict):
                continue
            mr_url = str(item.get("mr_url") or "")
            mr_label = f"!{item.get('mr_id')}" if item.get("mr_id") else "MR"
            mr_cell = f"[{mr_label}]({mr_url})" if mr_url else mr_label
            commit = str(item.get("commit") or "-")
            if len(commit) > 12 and re.fullmatch(r"[0-9a-fA-F]+", commit):
                commit = commit[:12]
            lines.append(
                "| "
                + " | ".join(
                    [
                        mr_cell,
                        str(item.get("request_by") or "-"),
                        _mr_state_label(str(item.get("state") or item.get("status") or "-")),
                        f"`{item.get('project_path') or item.get('project') or '-'}`",
                        f"`{item.get('source_branch') or '-'}`",
                        f"`{item.get('target_branch') or '-'}`",
                        f"`{commit}`",
                        str(item.get("responsible") or "-"),
                        _related_mr_match_summary(item),
                        str(item.get("file_count") or 0),
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(
        [
        f"## {audit_conclusion_heading}",
        "",
        f"{'Conclusion' if language == 'en' else '结论'}{sep}{_localize_text(result.conclusion, language)}",
        "",
        "Finding Counts:" if language == "en" else "问题统计：",
        f"Critical{sep}{counts.get('Critical', 0)}",
        f"High{sep}{counts.get('High', 0)}",
        f"Medium{sep}{counts.get('Medium', 0)}",
        f"Low{sep}{counts.get('Low', 0)}",
        f"Warning{sep}{counts.get('Warning', 0)}",
        "",
        *(_risk_summary_lines(result.risk_summary, language)),
        "",
        f"## {labels['change_summary']}",
        "",
        ]
    )

    if source.changed_files:
        lines.append(f"| {labels['file']} | {labels['additions']} | {labels['deletions']} | {labels['code_link']} |")
        lines.append("| --- | ---: | ---: | --- |")
        for changed_file in source.changed_files:
            file_link = _file_link_from_metadata(source.metadata, changed_file.path) or _file_link(source.mr_url, source.commit or source.source_branch, changed_file.path)
            diff_anchor = _diff_anchor(changed_file.path)
            file_cell = f"[`{changed_file.path}`](#{diff_anchor})"
            code_cell = f"[{labels['view_code']}]({file_link})" if file_link else "-"
            lines.append(f"| {file_cell} | {changed_file.additions} | {changed_file.deletions} | {code_cell} |")
    else:
        lines.append(f"- {labels['no_changed_files']}")

    git_version_summary = source.metadata.get("git_version_summary") or {}
    if git_version_summary:
        lines.extend(["", f"## {labels['git_version_summary']}", ""])
        lines.extend(_render_git_version_summary(git_version_summary, source.metadata, labels))

    lines.extend(["", f"## {labels['findings']}", ""])
    finding_diff_remaining = max(0, app_config_int("report.finding_diff_total_chars", "REPORT_FINDING_DIFF_TOTAL_CHARS", 500000))
    finding_diff_file_limit = max(0, app_config_int("report.finding_diff_max_chars", "REPORT_FINDING_DIFF_MAX_CHARS", 4000))
    if result.findings:
        for index, finding in enumerate(result.findings, 1):
            changed_file = _find_changed_file(source.changed_files, finding.file_path)
            location = _location_markdown(source.mr_url, source.commit or source.source_branch, finding, changed_file)
            snippet_limit = min(finding_diff_file_limit, finding_diff_remaining)
            diff = _diff_snippet(changed_file.diff if changed_file else "", finding.line, max_chars=snippet_limit) if snippet_limit else ""
            finding_diff_remaining = max(0, finding_diff_remaining - len(diff))
            lines.extend([f"### {index}. [{finding.severity}] {_localize_text(finding.title, language)}", ""])
            if _is_involved_file_mismatch_finding(finding):
                lines.extend(
                    [
                        f"- {labels['category']}{sep}{_localize_text(finding.category, language)}",
                        f"- {labels['location']}{sep}{location}",
                        "",
                        *_involved_file_mismatch_table(source, language),
                        "",
                        f"- {labels['recommendation']}{sep}{_localize_text(finding.recommendation, language)}",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"- {labels['category']}{sep}{_localize_text(finding.category, language)}",
                        f"- {labels['location']}{sep}{location}",
                        f"- {labels['problem']}{sep}{_localize_text(finding.detail, language)}",
                        f"- {labels['recommendation']}{sep}{_localize_text(finding.recommendation, language)}",
                        "",
                    ]
                )
            if diff:
                lines.extend(
                    [
                        f"<details><summary>{labels['related_diff']}</summary>",
                        "",
                        f"[{labels['view_full_file_diff']}](#{_diff_anchor(changed_file.path) if changed_file else ''})",
                        "",
                        "```diff",
                        diff,
                        "```",
                        "",
                        "</details>",
                        "",
                    ]
                )
    else:
        lines.append(f"- {labels['no_findings']}")

    lines.extend(["", f"## {handling_template_heading}", ""])
    lines.extend(_handling_template_body_lines(render_handling_result_template(result, language=language)))

    if source.changed_files:
        lines.extend(["", f"## {labels['file_diffs']}", ""])
        report_diff_remaining = max(0, app_config_int("report.diff_total_chars", "REPORT_DIFF_TOTAL_CHARS", 3000000))
        report_diff_file_limit = max(0, app_config_int("report.diff_file_max_chars", "REPORT_DIFF_FILE_MAX_CHARS", 120000))
        for index, changed_file in enumerate(source.changed_files, 1):
            diff_limit = min(report_diff_file_limit, report_diff_remaining)
            rendered_diff, diff_truncated = _bounded_diff_text(changed_file.diff.strip(), diff_limit)
            report_diff_remaining = max(0, report_diff_remaining - len(rendered_diff))
            lines.extend(
                [
                    f'<a id="{_diff_anchor(changed_file.path)}"></a>',
                    f"### {index}. `{changed_file.path}`",
                    "",
                    f"<details open><summary>{labels['view_diff']}</summary>",
                    "",
                    "```diff",
                    rendered_diff or labels["no_diff"],
                    "```",
                    "",
                    *( [f"> Diff abbreviated in this report ({len(changed_file.diff)} source characters). Use the GitLab code link in Change Summary for the complete change.", ""] if diff_truncated else [] ),
                    "</details>",
                    "",
                ]
            )

    lines.extend(["", f"## {labels['test_suggestions']}", ""])
    lines.extend(f"- {_localize_text(item, language)}" for item in _test_suggestion_report_lines(result, language))

    other_lines = _other_report_lines(source, language, labels)
    if other_lines:
        lines.extend(["", f"## {other_heading}", ""])
        lines.extend(other_lines)

    return "\n".join(lines).strip() + "\n"


def _report_metadata_comment(source: ReviewInput) -> str:
    metadata = source.metadata or {}
    payload = {
        "schema": "code_reviewer_report_metadata_v1",
        "jira_key": source.jira_key,
        "mr_id": source.mr_id,
        "generated_at": source.generated_at.isoformat(timespec="seconds"),
        "responsible": metadata.get("responsible", ""),
        "web_report_owner": metadata.get("web_report_owner", ""),
        "review_fingerprint": metadata.get("review_fingerprint", ""),
        "review_stable_fingerprint": metadata.get("review_stable_fingerprint", ""),
        "review_fingerprint_items": metadata.get("review_fingerprint_items", []),
        "review_stable_fingerprint_items": metadata.get("review_stable_fingerprint_items", []),
    }
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- code_reviewer_metadata: {compact} -->"


def _test_suggestion_report_lines(result: ReviewResult, language: str) -> list[str]:
    source = result.review_input
    metadata = source.metadata or {}
    base = [_localize_text(item, language) for item in result.test_suggestions]
    issue_summary = str(metadata.get("jira_summary") or source.title or "").strip()
    paths = [item.path.replace("\\", "/") for item in source.changed_files]
    findings = result.findings
    frontend_paths = _paths_matching(paths, (".js", ".ts", ".tsx", ".jsx", ".vue", ".dart", ".html", ".css", ".scss"))
    backend_paths = [path for path in paths if re.search(r"/(src|lib|app|modules?)/", path.lower()) or path.lower().endswith((".php", ".java", ".py"))]
    data_paths = [path for path in paths if "/dao/" in path.lower() or path.lower().endswith((".sql", ".scr", ".yml", ".yaml", ".json"))]
    page_paths = [path for path in frontend_paths if "/pages/" in path.lower() or "/components/" in path.lower() or "/html/" in path.lower()]
    finding_focus = _finding_focus_titles(findings)
    suggestions: list[str] = []
    if language == "en":
        if issue_summary:
            suggestions.append(f"Requirement acceptance case: use test data that matches `{issue_summary}`, execute the exact page/API/job flow named by the Jira issue, and verify the changed behavior, persisted result, and downstream display/export value all match the requirement.")
        if page_paths:
            suggestions.append(f"UI regression case: open {', '.join(_short_paths(page_paths, 3))}; verify initial load, refresh, search/filter/sort, empty result, backend error, and role/permission boundaries with real test accounts.")
        elif frontend_paths:
            suggestions.append(f"Frontend regression case: exercise the screens/components affected by {', '.join(_short_paths(frontend_paths, 3))}; verify route entry, data loading, validation messages, disabled/loading states, and rollback to previous navigation.")
        if backend_paths:
            suggestions.append(f"Service/API case: call the affected backend entry points around {', '.join(_short_paths(backend_paths, 3))}; cover success, invalid input, permission denied, not-found, retry/idempotency, and response compatibility with existing callers.")
        if data_paths:
            suggestions.append(f"Data/config case: validate {', '.join(_short_paths(data_paths, 3))}; cover script/config syntax, repeated execution, missing optional values, rollback expectation, and downstream consumers.")
        if paths:
            suggestions.append("Impact-scope case: run one end-to-end regression that starts from the Jira business entry and crosses every changed GitLab MR/project in this report, so split frontend/build/backend changes are validated together.")
        if findings:
            suggestions.append(f"Finding regression case: add fix-validation tests for {finding_focus}; each Critical/High/Medium item should fail before the fix and pass after the fix.")
    else:
        if issue_summary:
            suggestions.append(f"需求验收用例：准备一笔符合 `{issue_summary}` 前置条件的测试数据，按 Jira 描述的真实页面/API/批处理入口执行主流程，逐项核对页面展示、接口返回、落库/状态变化、下游页面或导出结果是否与需求一致。")
        if page_paths:
            suggestions.append(f"页面回归用例：打开 {', '.join(_short_paths(page_paths, 3))}；分别验证首次进入、刷新后状态保持、搜索/筛选/排序、空数据、接口异常提示、不同角色/权限账号的可见性和操作限制。")
        elif frontend_paths:
            suggestions.append(f"前端回归用例：围绕 {', '.join(_short_paths(frontend_paths, 3))} 对应页面或组件执行入口跳转、数据加载、表单校验、按钮 loading/disabled、异常提示和返回上一页等操作。")
        if backend_paths:
            suggestions.append(f"服务/API 用例：针对 {', '.join(_short_paths(backend_paths, 3))} 相关入口覆盖成功、非法参数、权限不足、数据不存在、重复提交/重试幂等、旧调用方响应兼容性。")
        if data_paths:
            suggestions.append(f"数据/配置用例：校验 {', '.join(_short_paths(data_paths, 3))} 的语法和执行效果；覆盖重复执行、缺少可选配置、回滚预期，以及使用这些配置/脚本的下游查询或任务。")
        if paths:
            suggestions.append("影响范围联调用例：从 Jira 对应业务入口发起一条端到端流程，串联本报告涉及的所有 MR/项目，确认前端、构建资源、后端或配置变更组合后行为一致。")
        if findings:
            suggestions.append(f"问题回归用例：针对 {finding_focus} 补充修复验证；每个 Critical/High/Medium 问题至少有一个“修复前失败、修复后通过”的可重复用例。")
    return _unique_text_lines([*suggestions, *base])


def _risk_summary_lines(risk_summary: list[str], language: str) -> list[str]:
    if not risk_summary:
        return []
    title = "Risk Summary:" if language == "en" else "风险摘要："
    return ["", title, *[f"- {_localize_text(item, language)}" for item in risk_summary]]


def _is_involved_file_mismatch_finding(finding: Finding) -> bool:
    return (finding.title or "").strip().lower() == "jira involved file list does not match mr diff"


def _involved_file_mismatch_table(source: ReviewInput, language: str) -> list[str]:
    check = source.metadata.get("jira_involved_files_check") if isinstance(source.metadata, dict) else None
    if not isinstance(check, dict):
        return _fallback_involved_file_table(language)
    expected = _unique_text_lines([str(item) for item in check.get("expected") or []])
    actual = _unique_text_lines([_normalize_report_actual_changed_path(item.path) for item in source.changed_files])
    if not actual:
        actual = _unique_text_lines([str(item) for item in check.get("actual") or []])
    deferred_actual = _unique_text_lines([str(item) for item in check.get("deferred_actual") or []])
    effective_actual = _unique_text_lines([*actual, *deferred_actual])
    deferred_keys = {item.lower() for item in deferred_actual}
    missing = {item.lower() for item in expected if not _first_matching_path(item, effective_actual)}
    unexpected = {item.lower() for item in effective_actual if not _first_matching_path(item, expected)}
    headers = (
        ("Expected File Lists", "Commit File Lists", "Remarks")
        if language == "en"
        else ("Expected File Lists 预期文件列表", "Commit File Lists 实际提交文件", "Remarks 备注、说明")
    )
    lines = [
        f"| {headers[0]} | {headers[1]} | {headers[2]} |",
        "| --- | --- | --- |",
    ]
    matched_actual: set[str] = set()
    for expected_path in expected:
        match = _first_matching_path(expected_path, effective_actual)
        if match:
            matched_actual.add(match.lower())
            if match.lower() in deferred_keys:
                remark = (
                    "Matched in deferred Company Config/SCR MR; verify at the GIT_VERSION release gate"
                    if language == "en"
                    else "在延后处理的 Company Config/SCR MR 中匹配；由 GIT_VERSION 发布闸门校验"
                )
            else:
                remark = "Matched" if language == "en" else "匹配"
            lines.append(f"| `{_table_cell(expected_path)}` | `{_table_cell(match)}` | {remark} |")
        else:
            remark = "Expected in Jira but not changed in MR diff" if language == "en" else "Jira 已列出，但 MR diff 未提交"
            if expected_path.lower() in missing or not actual:
                lines.append(f"| `{_table_cell(expected_path)}` | - | {remark} |")
    for actual_path in effective_actual:
        if actual_path.lower() in matched_actual:
            continue
        if actual_path.lower() in unexpected or not _first_matching_path(actual_path, expected):
            if actual_path.lower() in deferred_keys:
                remark = (
                    "Changed in deferred Company Config/SCR MR but not listed in Jira"
                    if language == "en"
                    else "延后处理的 Company Config/SCR MR 已提交，但 Jira 涉及文件清单未列出"
                )
            else:
                remark = "Changed in MR but not listed in Jira" if language == "en" else "MR 已提交，但 Jira 涉及文件清单未列出"
            lines.append(f"| - | `{_table_cell(actual_path)}` | {remark} |")
    return lines


def _fallback_involved_file_table(language: str) -> list[str]:
    headers = (
        ("Expected File Lists", "Commit File Lists", "Remarks")
        if language == "en"
        else ("Expected File Lists 预期文件列表", "Commit File Lists 实际提交文件", "Remarks 备注、说明")
    )
    note = "Mismatch details were not available in metadata." if language == "en" else "报告元数据中未找到可展开的文件清单差异。"
    return [
        f"| {headers[0]} | {headers[1]} | {headers[2]} |",
        "| --- | --- | --- |",
        f"| - | - | {note} |",
    ]


def _first_matching_path(path: str, candidates: list[str]) -> str:
    for candidate in candidates:
        if _paths_match_for_report(path, candidate):
            return candidate
    return ""


def _paths_match_for_report(left: str, right: str) -> bool:
    a = _clean_report_path(left).lower()
    b = _clean_report_path(right).lower()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _clean_report_path(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\\", "/").strip().strip("`'\" ")).strip("/")


def _normalize_report_actual_changed_path(value: str) -> str:
    text = _clean_report_path(value)
    return re.sub(r"^[^!]+!\d+/", "", text)


def _table_cell(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _paths_matching(paths: list[str], suffixes: tuple[str, ...]) -> list[str]:
    return [path for path in paths if path.lower().endswith(suffixes)]


def _short_paths(paths: list[str], limit: int) -> list[str]:
    result: list[str] = []
    for path in paths[:limit]:
        result.append(path if len(path) <= 96 else "..." + path[-93:])
    return result


def _finding_focus_titles(findings: list[Finding]) -> str:
    important = [item for item in findings if item.severity in {"Critical", "High", "Medium"}]
    titles = [f"[{item.severity}] {item.title}" for item in important[:3]]
    return "；".join(titles) if titles else "本报告问题列表"


def _other_report_lines(source: ReviewInput, language: str, labels: dict[str, str]) -> list[str]:
    metadata = source.metadata or {}
    lines: list[str] = []
    deferred_lines = _deferred_release_gate_report_lines(metadata, language)
    if deferred_lines:
        lines.extend(deferred_lines)
    llm_lines = _llm_execution_report_lines(metadata, language)
    if llm_lines:
        lines.extend([f"### {labels['llm_notes']}", "", *llm_lines, ""])
    issue_links = metadata.get("issue_links") or []
    if isinstance(issue_links, list) and issue_links:
        lines.extend([f"### {labels['issue_links']}", ""])
        for item in issue_links:
            if isinstance(item, dict):
                lines.append(f"- {item.get('key', '-')}: {item.get('summary', '-')} {item.get('url', '')}")
        lines.append("")
    return lines


def _deferred_release_gate_report_lines(metadata: dict[str, object], language: str) -> list[str]:
    resources = metadata.get("deferred_release_gate_resources") or []
    if not isinstance(resources, list) or not resources:
        return []
    heading = "Deferred Build Resources" if language == "en" else "已延后至 GIT_VERSION 的构建资源"
    description = (
        "These Company Config/SCR MRs are intentionally excluded from the ordinary Jira review. "
        "They must be included by the locked build repository commit and checked in the GIT_VERSION release-gate review."
        if language == "en"
        else "这些 Company Config/SCR MR 已从普通 Jira 审查中延后处理；必须在 GIT_VERSION 锁定的构建仓库 commit 中被包含，并由发布闸门统一校验。"
    )
    headers = ("Role", "MR", "Source Branch", "Target Branch", "Changed Files") if language == "en" else ("资源类型", "MR", "源分支", "目标分支", "变更文件")
    lines = [f"### {heading}", "", f"- {description}", "", f"| {headers[0]} | {headers[1]} | {headers[2]} | {headers[3]} | {headers[4]} |", "| --- | --- | --- | --- | --- |"]
    for item in resources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("mr_url") or "")
        label = url.rsplit("/", 1)[-1] if url else "-"
        mr = f"[{label}]({url})" if url else label
        changed_files = [str(value) for value in item.get("changed_file_paths") or [] if str(value).strip()]
        changed_files_text = "<br>".join(f"`{_table_cell(value)}`" for value in changed_files[:20]) or "-"
        if len(changed_files) > 20:
            changed_files_text += f"<br>... +{len(changed_files) - 20}"
        lines.append(
            f"| `{str(item.get('release_gate_role') or item.get('ignored_branch_type') or '-')}` | {mr} | "
            f"`{str(item.get('source_branch') or '-')}` | `{str(item.get('target_branch') or '-')}` | {changed_files_text} |"
        )
    return [*lines, ""]


def _llm_execution_report_lines(metadata: dict[str, object], language: str) -> list[str]:
    lines: list[str] = []
    llm_notes = metadata.get("llm_notes") or []
    if isinstance(llm_notes, list):
        lines.extend(f"- {_localize_text(str(item), language)}" for item in llm_notes if str(item).strip())
    auto_fetched = metadata.get("jira_prd_context_auto_fetched") or []
    if isinstance(auto_fetched, list) and auto_fetched:
        if language == "en":
            lines.append(f"- Local Jira/PRD data was missing and was fetched on demand with `fetch_jira.py --depth 2`: {', '.join(str(item) for item in auto_fetched)}.")
        else:
            lines.append(f"- 本地 Jira/PRD 数据缺失，已即时调用 `fetch_jira.py --depth 2` 抓取：{', '.join(str(item) for item in auto_fetched)}。")
    missing = metadata.get("jira_prd_context_missing_issue_keys") or []
    if isinstance(missing, list) and missing:
        if language == "en":
            lines.append(f"- Local Jira/PRD documents are still missing after fetch attempt: {', '.join(str(item) for item in missing)}.")
        else:
            lines.append(f"- 自动抓取后仍缺少本地 Jira/PRD 文档：{', '.join(str(item) for item in missing)}。")
    if not lines:
        return []
    return lines


def _unique_text_lines(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def report_filename(
    project: str = "",
    mr_id: str = "",
    jira_key: str = "",
    severity_counts: dict[str, int] | None = None,
    responsible: str = "",
    simplified: bool = False,
    scope: str = "",
) -> str:
    status = _report_status_suffix(severity_counts or {})
    if simplified or responsible:
        if "chunk" in (mr_id or "").lower():
            base_parts = [jira_key or "NO-JIRA", scope, mr_id, status]
        else:
            base_parts = [jira_key or mr_id or "NO-JIRA", scope, status]
    else:
        base_parts = [project, mr_id, jira_key, status]
    base = "_".join(part for part in base_parts if part) or "review-report"
    base = re.sub(r"[^A-Za-z0-9_.+-]+", "_", base).strip("_")
    return f"{base}.md"


def save_report(result: ReviewResult, output_dir: Path, filename: str | None = None, language: str | None = None) -> Path:
    if not filename:
        output_dir = _responsible_output_dir(output_dir, result.review_input.metadata)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = result.review_input
    project_prefix = _report_project_prefix(source.project, source.metadata)
    responsible_prefix = _report_filename_responsible(source.metadata)
    simplified_name = bool(responsible_prefix) or bool(_web_report_owner(source.metadata))
    target = output_dir / (
        filename
        or report_filename(
            project_prefix,
            source.mr_id,
            source.jira_key,
            result.severity_counts,
            responsible=responsible_prefix,
            simplified=simplified_name,
            scope=str(source.metadata.get("split_report_project_type") or ""),
        )
    )
    if _web_report_owner(source.metadata):
        target = _avoid_overwrite_report_path(target)
    source.metadata["report_filename"] = target.name
    markdown = render_markdown(result, language=language)
    hard_limit = max(1000000, app_config_int("report.markdown_hard_max_chars", "REPORT_MARKDOWN_HARD_MAX_CHARS", 12000000))
    if len(markdown) > hard_limit:
        raise RuntimeError(
            f"Rendered report is too large ({len(markdown)} chars > {hard_limit}). "
            "Reduce report diff budgets or split the review into smaller chunks."
        )
    target.write_text(markdown, encoding="utf-8")
    return target


def save_reports(result: ReviewResult, output_dir: Path, filename: str | None = None, language: str | None = None) -> list[tuple[ReviewResult, Path]]:
    if filename:
        return [(result, save_report(result, output_dir, filename=filename, language=language))]
    saved: list[tuple[ReviewResult, Path]] = []
    for report_result in split_result_by_responsible(result):
        saved.append((report_result, save_report(report_result, output_dir, language=language)))
    return saved


def split_result_by_responsible(result: ReviewResult) -> list[ReviewResult]:
    related_mrs = result.review_input.metadata.get("related_merge_requests") or []
    if not isinstance(related_mrs, list) or not related_mrs:
        return [result]
    groups = _related_mr_groups_by_report_scope(related_mrs)
    if len(groups) <= 1:
        return [result]

    split_results: list[ReviewResult] = []
    for group_key, items in groups.items():
        responsible, project_type = group_key
        prefixes = [_related_mr_prefix(item) for item in items]
        prefixes = [prefix for prefix in prefixes if prefix]
        changed_files = [
            _copy_changed_file(changed_file)
            for changed_file in result.review_input.changed_files
            if _path_matches_any_prefix(changed_file.path, prefixes)
        ]
        changed_paths = {changed_file.path.replace("\\", "/") for changed_file in changed_files}
        findings = [
            finding
            for finding in result.findings
            if _finding_matches_split(finding, changed_paths, prefixes, items)
        ]
        metadata = copy.deepcopy(result.review_input.metadata)
        metadata["related_merge_requests"] = [copy.deepcopy(item) for item in items]
        metadata["responsible_people"] = _unique_sorted_people(re.split(r"[+,;]+", responsible))
        metadata["responsible"] = "+".join(metadata["responsible_people"])
        metadata["split_from_responsible"] = _canonical_responsible_name(result.review_input.metadata)
        metadata["split_report_responsible"] = responsible
        if project_type:
            metadata["project_type"] = project_type
            metadata["git_tools_project_type"] = project_type
            metadata["split_report_project_type"] = project_type
        metadata["split_report_count"] = len(groups)
        metadata["split_report_file_prefixes"] = prefixes
        metadata["multi_mr_file_links"] = _filter_file_links(metadata.get("multi_mr_file_links"), changed_paths)
        _set_split_project_metadata(metadata, items)
        split_input = replace(
            result.review_input,
            mr_id=_split_mr_id(items),
            changed_files=changed_files,
            raw_diff=_raw_diff_for_changed_files(changed_files),
            metadata=metadata,
        )
        split_results.append(
            ReviewResult(
                review_input=split_input,
                findings=findings,
                conclusion=_conclusion_for_findings(findings),
                risk_summary=result.risk_summary,
                test_suggestions=result.test_suggestions,
            )
        )
    return split_results


def render_handling_result_template(result: ReviewResult, language: str | None = None) -> str:
    language = _normalize_language(language or report_language())
    source = result.review_input
    report_name = str(source.metadata.get("report_filename") or "").strip()
    if not report_name:
        project_prefix = _report_project_prefix(source.project, source.metadata)
        responsible_prefix = _report_filename_responsible(source.metadata)
        report_name = report_filename(
            project_prefix,
            source.mr_id,
            source.jira_key,
            result.severity_counts,
            responsible=responsible_prefix,
            simplified=bool(responsible_prefix) or bool(_web_report_owner(source.metadata)),
            scope=str(source.metadata.get("split_report_project_type") or ""),
        )
    title = _handling_title(report_name, language)
    return _render_handling_template(title, _findings_for_handling_template(result.findings), language)


def render_handling_result_template_from_markdown(markdown: str, report_name: str, language: str | None = None) -> str:
    language = _normalize_language(language or report_language())
    title = _handling_title(report_name, language)
    return _render_handling_template(title, _parse_findings_from_markdown(markdown), language)


def handling_result_filename(report_name: str) -> str:
    base = Path(report_name or "code-review-report").name
    if base.lower().endswith(".md"):
        base = base[:-3]
    base = re.sub(r"[^A-Za-z0-9_.+-]+", "_", base).strip("_") or "code-review-report"
    return f"{base}_handling-result.md"


def _handling_template_body_lines(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    return lines


def _handling_title(report_name: str, language: str) -> str:
    if language == "en":
        return f"{report_name} Code Review Report Handling Result"
    return f"{report_name} 代码审核报告处理结果说明"


def _findings_for_handling_template(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(finding.severity, finding.title) for finding in findings]


def _parse_findings_from_markdown(markdown: str) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    pattern = re.compile(r"^###\s+\d+\.\s+\[([^\]]+)\]\s+(.+?)\s*$", re.M)
    for match in pattern.finditer(markdown or ""):
        severity = match.group(1).strip()
        title = re.sub(r"\s+", " ", match.group(2).strip())
        if title:
            findings.append((severity, title))
    return findings


def _render_handling_template(title: str, findings: list[tuple[str, str]], language: str) -> str:
    if language == "en":
        lines = [
            f"# {title}",
            "",
            "Handling options:",
            "",
            "1. If it affects requirement or functional implementation, fix it before the version is released.",
            "2. If the current requirement/function is already implemented and this is only an improvement, create a separate Jira issue.",
            "3. If it is confirmed not to be an issue, clarify the reason.",
            "",
            "Recommended handling text:",
            "",
            "- Option 1: Fixed, pass.",
            "- Option 2: Not blocking, follow up with a separate Jira issue.",
            "- Option 3: Not an issue, pass.",
            "",
        ]
        if not findings:
            lines.extend(["No findings need handling.", ""])
            return "\n".join(lines).strip() + "\n"
        for index, (severity, finding_title) in enumerate(findings, 1):
            lines.extend(
                [
                    f"{index}. [{severity}] {finding_title}",
                    "Remarks:",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    lines = [
        f"# {title}",
        "",
        "处理方式说明：",
        "",
        "1. 如果影响到需求、功能实现，需要整改后再出版本；",
        "2. 如果已经实现当前需求、功能，只是改善项，则安排另报 Jira issue 处理；",
        "3. 如果判定不是问题，则澄清说明一下；",
        "",
        "处理结果说明建议：",
        "",
        "- 处理方式 #1：已整改，Pass通过；",
        "- 处理方式 #2：不是阻碍，另报issue跟进；",
        "- 处理方式 #3：不是问题，Pass通过；",
        "",
    ]
    if not findings:
        lines.extend(["未发现需要处理的问题。", ""])
        return "\n".join(lines).strip() + "\n"
    for index, (severity, finding_title) in enumerate(findings, 1):
        lines.extend(
            [
                f"{index}. [{severity}] {finding_title}",
                "说明：",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _related_mr_groups_by_report_scope(related_mrs: list[object]) -> dict[tuple[str, str], list[dict[str, object]]]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for item in related_mrs:
        if not isinstance(item, dict):
            continue
        owners = _responsible_people_for_split(str(item.get("responsible") or ""))
        if not owners:
            owners = ["unassigned"]
        project_type = _normalize_report_project_type(item.get("project_type"))
        for responsible in owners:
            groups.setdefault((responsible, project_type), []).append(item)
    return groups


def _normalize_report_project_type(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"frontend", "front-end", "web", "client"}:
        return "frontend"
    if text in {"backend", "back-end", "server", "api"}:
        return "backend"
    return text


def _canonical_responsible_text(value: str) -> str:
    return "+".join(_unique_sorted_people([item.strip() for item in re.split(r"[+,;]+", value or "") if item.strip()]))


def _responsible_people_for_split(value: str) -> list[str]:
    return _unique_sorted_people([item.strip() for item in re.split(r"[+,;]+", value or "") if item.strip()])


def _related_mr_prefix(item: dict[str, object]) -> str:
    prefix = str(item.get("file_prefix") or "").replace("\\", "/").strip("/")
    if prefix:
        return prefix
    project_path = str(item.get("project_path") or item.get("project") or "").replace("\\", "/").strip("/")
    mr_id = str(item.get("mr_id") or "").strip()
    if project_path and mr_id:
        return f"{project_path}!{mr_id}"
    return ""


def _path_matches_any_prefix(path: str, prefixes: list[str]) -> bool:
    normalized = (path or "").replace("\\", "/").strip("/")
    for prefix in prefixes:
        clean_prefix = prefix.replace("\\", "/").strip("/")
        if normalized == clean_prefix or normalized.startswith(clean_prefix + "/"):
            return True
    return False


def _copy_changed_file(changed_file: ChangedFile) -> ChangedFile:
    return ChangedFile(
        path=changed_file.path,
        additions=changed_file.additions,
        deletions=changed_file.deletions,
        diff=changed_file.diff,
    )


def _finding_matches_split(
    finding: Finding,
    changed_paths: set[str],
    prefixes: list[str],
    related_items: list[dict[str, object]],
) -> bool:
    file_path = (finding.file_path or "").replace("\\", "/").strip("/")
    if _path_matches_any_prefix(file_path, prefixes):
        return True
    if file_path and any(path.endswith("/" + file_path) or path == file_path for path in changed_paths):
        return True

    haystack = "\n".join(
        [
            file_path,
            finding.title or "",
            finding.detail or "",
            finding.recommendation or "",
        ]
    )
    if any(prefix and prefix in haystack for prefix in prefixes):
        return True
    for item in related_items:
        tokens = [
            str(item.get("mr_url") or ""),
            str(item.get("project_path") or ""),
            str(item.get("project") or ""),
            str(item.get("mr_id") or ""),
        ]
        if any(token and token in haystack for token in tokens):
            return True

    scoped = file_path.lower() not in {"", "-", "architecture", "general", "jira issue", "jira"}
    return not scoped


def _filter_file_links(value: object, changed_paths: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(path): copy.deepcopy(link) for path, link in value.items() if str(path).replace("\\", "/") in changed_paths}


def _set_split_project_metadata(metadata: dict[str, object], items: list[dict[str, object]]) -> None:
    project_names = _unique_sorted_people([str(item.get("project_name") or "").strip() for item in items if str(item.get("project_name") or "").strip()])
    modules = _unique_sorted_people([str(item.get("git_tools_module") or "").strip() for item in items if str(item.get("git_tools_module") or "").strip()])
    project_paths = _unique_sorted_people([str(item.get("project_path") or item.get("project") or "").strip() for item in items if str(item.get("project_path") or item.get("project") or "").strip()])
    if project_names:
        metadata["project_names"] = project_names
        metadata["project_name"] = project_names[0] if len(project_names) == 1 else "+".join(project_names)
    elif modules:
        metadata["project_names"] = modules
        metadata["project_name"] = modules[0] if len(modules) == 1 else "+".join(modules)
    if project_paths:
        metadata["git_tools_project_path"] = project_paths[0] if len(project_paths) == 1 else "multiple"
        metadata["split_report_gitlab_projects"] = project_paths


def _split_mr_id(items: list[dict[str, object]]) -> str:
    ids = [str(item.get("mr_id") or "").strip() for item in items if str(item.get("mr_id") or "").strip()]
    if len(ids) == 1:
        return ids[0]
    return f"multi-mr-{len(items)}"


def _raw_diff_for_changed_files(changed_files: list[ChangedFile]) -> str:
    parts: list[str] = []
    for changed_file in changed_files:
        path = changed_file.path.replace("\\", "/")
        parts.append(
            "\n".join(
                [
                    f"diff --git a/{path} b/{path}",
                    f"--- a/{path}",
                    f"+++ b/{path}",
                    changed_file.diff,
                ]
            )
        )
    return "\n".join(parts)


def _conclusion_for_findings(findings: list[Finding]) -> str:
    counts: dict[str, int] = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    if counts.get("Critical", 0):
        return "Blocked: Critical risk found; fix before merge."
    if counts.get("High", 0):
        return "Changes required: High risk found; fix it or provide an explicit waiver."
    if counts.get("Medium", 0) or counts.get("Low", 0) or counts.get("Warning", 0):
        return "Needs review: no blocking issue found, but medium/low risks still need reviewer confirmation."
    return "Passed: no obvious risk found."


def _report_project_prefix(default_project: str, metadata: dict[str, object]) -> str:
    project_name = str(metadata.get("project_name") or metadata.get("git_tools_project_name") or "").strip()
    if project_name:
        return project_name
    module = str(metadata.get("git_tools_module") or "").strip()
    if module:
        return module
    return default_project


def _responsible_output_dir(output_dir: Path, metadata: dict[str, object]) -> Path:
    owner = _web_report_owner(metadata)
    if owner:
        return output_dir / _safe_path_component(owner)
    if not app_config_bool("report.group_by_responsible", "REPORT_GROUP_BY_RESPONSIBLE", True):
        return output_dir
    responsible = _responsible_folder_name(metadata)
    return output_dir / responsible if responsible else output_dir


def _responsible_folder_name(metadata: dict[str, object]) -> str:
    responsible = _canonical_responsible_name(metadata)
    return _safe_path_component(responsible)


def _report_filename_responsible(metadata: dict[str, object]) -> str:
    if _web_report_owner(metadata):
        return ""
    responsible = _canonical_responsible_name(metadata)
    return _safe_path_component(responsible) or "unassigned"


def _web_report_owner(metadata: dict[str, object]) -> str:
    owner = str(metadata.get("web_report_owner") or "").strip()
    return owner if owner and owner.lower() not in {"admin", "root"} else owner


def _avoid_overwrite_report_path(target: Path) -> Path:
    if not target.exists():
        return target
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = target.with_name(f"{target.stem}_rescan-{stamp}{target.suffix}")
    counter = 2
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}_rescan-{stamp}-{counter}{target.suffix}")
        counter += 1
    return candidate


def _canonical_responsible_name(metadata: dict[str, object]) -> str:
    people = metadata.get("responsible_people") or []
    values: list[str] = []
    if isinstance(people, list):
        values.extend(str(item).strip() for item in people if str(item).strip())
    responsible = str(metadata.get("responsible") or "").strip()
    if responsible:
        values.extend(item.strip() for item in re.split(r"[+,;]+", responsible) if item.strip())
    return "+".join(_unique_sorted_people(values))


def _unique_sorted_people(values: list[str]) -> list[str]:
    by_key: dict[str, str] = {}
    for value in values:
        key = value.strip().lower()
        if key and key not in by_key:
            by_key[key] = value.strip()
    return [by_key[key] for key in sorted(by_key)]


def _safe_path_component(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.+-]+", "_", value.strip())
    return text.strip("._-")


def _report_status_suffix(severity_counts: dict[str, int]) -> str:
    for severity in ("Critical", "High", "Medium", "Low", "Warning"):
        if severity_counts.get(severity, 0) > 0:
            return f"has-issue-{severity.lower()}"
    return "pass"


def _mr_state_label(value: str) -> str:
    normalized = (value or "").strip().lower()
    labels = {
        "opened": "Open",
        "open": "Open",
        "closed": "Closed",
        "close": "Closed",
        "merged": "Merged",
        "locked": "Locked",
    }
    return labels.get(normalized, value or "-")


def _labels(language: str) -> dict[str, str]:
    if language == "en":
        return {
            "title": "Code Review Report",
            "basic_info": "Basic Info",
            "project": "Project",
            "responsible": "Responsible",
            "review_time": "Review Time",
            "conclusion": "Conclusion",
            "risk_stats": "Risk Stats",
            "change_summary": "Change Summary",
            "file": "File",
            "additions": "Additions",
            "deletions": "Deletions",
            "code_link": "Code",
            "view_code": "Code",
            "file_diffs": "File Diffs",
            "view_diff": "View Diff",
            "view_full_file_diff": "View full file diff",
            "no_diff": "No diff content was loaded.",
            "findings": "Findings",
            "handling_template": "Handling Template",
            "category": "Category",
            "location": "Location",
            "problem": "Problem",
            "recommendation": "Recommendation",
            "related_diff": "Related Diff",
            "test_suggestions": "Test Suggestions",
            "risk_summary": "Risk Summary",
            "llm_notes": "LLM Execution Notes",
            "issue_links": "Jira / GitLab Links",
            "project_context": "Local Project Context",
            "related_mrs": "Related MRs",
            "request_by": "Request By",
            "status": "Status",
            "git_version_summary": "GIT_VERSION Summary",
            "git_version_repositories": "Locked Repositories",
            "git_version_builds": "Build Configs",
            "build_history_files": "Build History Files",
            "locked_repo_reviews": "Locked Repository Code Reviews",
            "kind": "Kind",
            "issues": "Issues",
            "files": "Files",
            "resource_check": "Resource Check",
            "compare": "Compare",
            "present": "present",
            "missing": "missing",
            "commit_title": "Commit Title",
            "module": "Module",
            "branch": "Branch",
            "commit": "Commit",
            "repository": "Repository",
            "version": "Version",
            "git_version_file": "git_version",
            "build_branch": "Build Branch",
            "companies": "Companies",
            "no_changed_files": "No changed files were loaded.",
            "no_findings": "No obvious issues found.",
        }
    return {
        "title": "代码审查报告",
        "basic_info": "基本信息",
        "project": "项目",
        "responsible": "Responsible",
        "review_time": "Review 时间",
        "conclusion": "总体结论",
        "risk_stats": "风险统计",
        "change_summary": "变更摘要",
        "file": "文件",
        "additions": "新增",
        "deletions": "删除",
        "code_link": "代码",
        "view_code": "代码",
        "file_diffs": "文件 Diff",
        "view_diff": "查看 Diff",
        "view_full_file_diff": "查看完整文件 Diff",
        "no_diff": "未加载到 diff 内容。",
        "findings": "问题列表",
        "handling_template": "处理模版",
        "category": "类型",
        "location": "位置",
        "problem": "问题",
        "recommendation": "建议",
        "related_diff": "相关 Diff",
        "test_suggestions": "测试建议",
        "risk_summary": "风险摘要",
        "llm_notes": "LLM 执行记录",
        "issue_links": "Jira / GitLab 关联",
        "project_context": "本地项目上下文",
        "related_mrs": "关联 MR",
        "request_by": "Request By 请求发起人",
        "status": "状态",
        "git_version_summary": "GIT_VERSION 摘要",
        "git_version_repositories": "锁定开发仓库",
        "git_version_builds": "构建配置",
        "build_history_files": "Build History 文件",
        "locked_repo_reviews": "锁定仓库代码审查",
        "kind": "类型",
        "issues": "Issue",
        "files": "文件数",
        "resource_check": "资源校验",
        "compare": "Compare",
        "present": "存在",
        "missing": "缺失",
        "commit_title": "Commit 标题",
        "module": "模块",
        "branch": "分支",
        "commit": "Commit",
        "repository": "仓库",
        "version": "版本",
        "git_version_file": "git_version",
        "build_branch": "构建分支",
        "companies": "公司",
        "no_changed_files": "未加载到变更文件。",
        "no_findings": "未发现明显问题。",
    }


def _normalize_language(language: str) -> str:
    value = (language or "zh-CN").lower()
    if value.startswith("en"):
        return "en"
    return "zh-CN"


def _location_markdown(mr_url: str, ref: str, finding: Finding, changed_file: ChangedFile | None = None) -> str:
    location = finding.file_path
    if finding.line:
        location = f"{location}:{finding.line}"
    if changed_file:
        return f"[`{location}`](#{_diff_anchor(changed_file.path)})"
    link = _file_link(mr_url, ref, finding.file_path, finding.line)
    return f"[`{location}`]({link})" if link else f"`{location}`"


def _file_link(mr_url: str, ref: str, file_path: str, line: int | None = None) -> str:
    if not mr_url or not ref or not file_path or file_path in {"Architecture", "GIT_VERSION"}:
        return ""
    if file_path.replace("\\", "/").startswith(("locked_source/", "locked_build/")):
        return ""
    match = re.match(r"^(https?://[^/]+)/(.+)/-/merge_requests/\d+", mr_url)
    if not match:
        return ""
    base_url, project_path = match.groups()
    encoded_ref = quote(ref, safe="")
    encoded_file = quote(file_path.replace("\\", "/"), safe="/")
    anchor = f"#L{line}" if line else ""
    return f"{base_url}/{project_path}/-/blob/{encoded_ref}/{encoded_file}{anchor}"


def _file_link_from_metadata(metadata: dict[str, object], file_path: str, line: int | None = None) -> str:
    links = metadata.get("multi_mr_file_links")
    if not isinstance(links, dict):
        return ""
    item = links.get(file_path) or links.get(file_path.replace("\\", "/"))
    if not isinstance(item, dict):
        return ""
    return _file_link(
        str(item.get("mr_url") or ""),
        str(item.get("ref") or ""),
        str(item.get("file_path") or file_path),
        line,
    )


def _find_changed_file(changed_files: list[ChangedFile], file_path: str) -> ChangedFile | None:
    normalized = file_path.replace("\\", "/")
    for item in changed_files:
        if item.path.replace("\\", "/") == normalized:
            return item
    suffix_matches = [
        item
        for item in changed_files
        if item.path.replace("\\", "/").endswith(f"/{normalized}")
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


def _diff_anchor(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").lower()
    anchor = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return f"diff-{anchor or 'file'}"


def _diff_snippet(file_diff: str, target_line: int | None, context: int = 4, max_chars: int = 4000) -> str:
    if not file_diff.strip() or max_chars <= 0:
        return ""
    lines = file_diff.splitlines()
    if target_line is None:
        return _bounded_diff_text("\n".join(lines[: min(len(lines), 40)]), max_chars)[0]

    numbered: list[tuple[int | None, str]] = []
    new_line_no: int | None = None
    for line in lines:
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line_no = int(match.group(1)) if match else None
            numbered.append((None, line))
            continue
        numbered.append((new_line_no, line))
        if new_line_no is not None and not line.startswith("-"):
            new_line_no += 1

    hit_indexes = [index for index, (line_no, _) in enumerate(numbered) if line_no == target_line]
    if not hit_indexes:
        return _bounded_diff_text("\n".join(lines[: min(len(lines), 40)]), max_chars)[0]
    index = hit_indexes[0]
    start = max(0, index - context)
    end = min(len(numbered), index + context + 1)
    while start > 0 and not numbered[start][1].startswith("@@"):
        start -= 1
    return _bounded_diff_text("\n".join(line for _, line in numbered[start:end]), max_chars)[0]


def _bounded_diff_text(text: str, max_chars: int, max_line_chars: int = 2000) -> tuple[str, bool]:
    if not text or max_chars <= 0:
        return "", bool(text)
    original_length = len(text)
    output: list[str] = []
    used = 0
    truncated = False
    for line in text.splitlines():
        bounded_line = line
        if len(bounded_line) > max_line_chars:
            bounded_line = bounded_line[:max_line_chars] + " ... [line abbreviated]"
            truncated = True
        separator = 1 if output else 0
        available = max_chars - used - separator
        if available <= 0:
            truncated = True
            break
        if len(bounded_line) > available:
            bounded_line = bounded_line[:available]
            truncated = True
        output.append(bounded_line)
        used += separator + len(bounded_line)
        if used >= max_chars:
            truncated = truncated or used < original_length
            break
    rendered = "\n".join(output)
    return rendered, truncated or len(rendered) < original_length


def _localize_text(text: str, language: str) -> str:
    if language == "en":
        return _ZH_TO_EN.get(text, text)
    detected = re.match(
        r"Detected (\d+) finding\(s\): (\d+) Critical, (\d+) High, (\d+) Medium, (\d+) Low(?:, (\d+) Warning)?\.",
        text,
    )
    if detected:
        total, critical, high, medium, low, warning = detected.groups()
        return f"检测到 {total} 个问题：{critical} Critical，{high} High，{medium} Medium，{low} Low，{warning or 0} Warning。"
    duplicate_key = re.match(
        r"Key '([^']+)' appears more than once under '([^']+)'\. YAML keeps only the last value, so the earlier locked value can be silently ignored\.",
        text,
    )
    if duplicate_key:
        key, parent = duplicate_key.groups()
        return f"Key '{key}' 在 '{parent}' 下出现多次。YAML 只会保留最后一个值，前面的锁定值可能被静默忽略。"
    version_suffix = re.match(
        r"git_version files use versions (.+), while build files use versions (.+)\.",
        text,
    )
    if version_suffix:
        return f"git_version 文件使用版本 {version_suffix.group(1)}，build 文件使用版本 {version_suffix.group(2)}。"
    fetch_error = re.match(
        r"(.+) (.+)@([0-9a-fA-F]+) could not be fetched: (.+)",
        text,
    )
    if fetch_error:
        module, repo, commit, error = fetch_error.groups()
        return f"{module} {repo}@{commit} 拉取失败：{error}"
    traceability = re.match(
        r"Release notes add ECHNL issues (.+), but fetched locked source commit context only shows (.+) for source commit\(s\) (.+)\. Missing traceability: (.+)\.",
        text,
    )
    if traceability:
        release_issues, source_issues, commits, missing = traceability.groups()
        return (
            f"Release notes 新增 ECHNL issue：{release_issues}；"
            f"但已拉取的锁定源码 commit 上下文只显示 {source_issues}，source commit 为 {commits}。"
            f"缺少可追踪证据：{missing}。"
        )
    return _EN_TO_ZH.get(text, text)


def _project_context_summary(metadata: dict[str, object]) -> str:
    path = str(metadata.get("project_context_path") or "")
    if not path:
        return "-"
    count = metadata.get("project_context_files_count", "-")
    ref = str(metadata.get("project_context_ref") or metadata.get("project_context_branch") or "working-tree")
    commit = str(metadata.get("project_context_commit") or "")
    sync = metadata.get("repository_sync")
    sync_text = ""
    if isinstance(sync, dict):
        sync_text = f"; sync: {sync.get('action') or '-'}"
    memory_status = str(metadata.get("codebase_memory_status") or "")
    memory_text = f"; Codebase Memory: {memory_status}" if memory_status else ""
    source_text = f"{ref}{'@' + commit[:12] if commit else ''}"
    included = metadata.get("project_context_included_files") or []
    if isinstance(included, list) and included:
        files = ", ".join(str(item) for item in included[:8])
        if len(included) > 8:
            files += ", ..."
        return f"{path} ({count} files scanned; ref: {source_text}{sync_text}{memory_text}; included: {files})"
    return f"{path} ({count} files scanned; ref: {source_text}{sync_text}{memory_text})"


def _llm_context_budget_summary(metadata: dict[str, object]) -> str:
    budget = metadata.get("llm_context_budget")
    if not isinstance(budget, dict):
        return "-"
    if not budget.get("enabled"):
        return "disabled"
    original = budget.get("original_chars", "-")
    final = budget.get("final_chars", "-")
    max_chars = budget.get("max_chars", "-")
    trimmed = budget.get("trimmed_chars", 0)
    sections = budget.get("sections")
    section_text = ""
    if isinstance(sections, dict) and sections:
        section_text = "; " + ", ".join(
            f"{name}:{details.get('trimmed_chars', 0)}"
            for name, details in sections.items()
            if isinstance(details, dict)
        )
    hard = "; hard-truncated" if budget.get("hard_truncated") else ""
    return f"{final}/{max_chars} chars (original {original}; trimmed {trimmed}{section_text}{hard})"


def _git_tools_project_match_summary(metadata: dict[str, object]) -> str:
    status = str(metadata.get("git_tools_project_match") or "")
    if not status:
        return "-"
    if status == "multi":
        summary = metadata.get("git_tools_multi_match_summary")
        if isinstance(summary, dict):
            return (
                f"multi MR: matched {summary.get('matched', 0)}/{summary.get('total', 0)}, "
                f"unmatched {summary.get('unmatched', 0)}"
            )
        return "multi MR"
    project_path = str(metadata.get("git_tools_project_path") or "-")
    config = str(metadata.get("git_tools_config") or "-")
    count = metadata.get("git_tools_config_project_count", "-")
    if status == "matched":
        group = str(metadata.get("git_tools_group") or "-")
        module = str(metadata.get("git_tools_module") or "-")
        repository = str(metadata.get("git_tools_repository_url") or "")
        suffix = f" | {repository}" if repository else ""
        return f"matched {group}/{module} ({project_path}){suffix}"
    if status == "unmatched":
        return f"unmatched {project_path}; not found in {config} ({count} configured projects)"
    if status == "not-configured":
        return f"not configured; {config} loaded {count} projects"
    return status


def _related_mr_match_summary(item: dict[str, object]) -> str:
    status = str(item.get("git_tools_project_match") or "-")
    group = str(item.get("git_tools_group") or "")
    module = str(item.get("git_tools_module") or "")
    if status == "matched" and (group or module):
        return f"{status} `{group}/{module}`"
    return status


def _responsible_summary(metadata: dict[str, object]) -> str:
    responsible = _canonical_responsible_name(metadata)
    if responsible:
        return responsible.replace("+", ", ")
    return str(metadata.get("git_tools_responsible") or "-")


def _jira_prd_context_summary(metadata: dict[str, object]) -> str:
    path = str(metadata.get("jira_prd_context_dir") or "")
    issue_keys = metadata.get("jira_prd_context_issue_keys") or []
    files = metadata.get("jira_prd_context_files") or []
    missing = metadata.get("jira_prd_context_missing_issue_keys") or []
    if not path and not issue_keys and not missing:
        return "-"
    parts: list[str] = []
    if path:
        parts.append(path)
    if isinstance(issue_keys, list) and issue_keys:
        parts.append("issues: " + ", ".join(str(item) for item in issue_keys[:8]))
    if isinstance(files, list) and files:
        parts.append(f"files: {len(files)}")
    if isinstance(missing, list) and missing:
        parts.append("missing: " + ", ".join(str(item) for item in missing[:8]))
    if metadata.get("jira_prd_context_truncated"):
        parts.append("truncated")
    return " | ".join(parts) if parts else "-"


def _render_git_version_summary(summary: object, metadata: dict[str, object], labels: dict[str, str]) -> list[str]:
    if not isinstance(summary, dict):
        return []
    lines: list[str] = []
    repositories = summary.get("repositories") or []
    builds = summary.get("builds") or []
    build_history_files = summary.get("build_history_files") or []
    if isinstance(repositories, list) and repositories:
        lines.extend([f"### {labels['git_version_repositories']}", ""])
        lines.append(
            f"| {labels['module']} | {labels['branch']} | {labels['commit']} | {labels['repository']} |"
        )
        lines.append("| --- | --- | --- | --- |")
        for item in repositories[:120]:
            if not isinstance(item, dict):
                continue
            commit = str(item.get("commit") or item.get("tag") or "-")
            repo = str(item.get("repository_url") or "-")
            lines.append(
                f"| `{item.get('module', '-')}` | `{item.get('branch', '-')}` | `{commit}` | {repo} |"
            )
        lines.append("")
    if isinstance(builds, list) and builds:
        lines.extend([f"### {labels['git_version_builds']}", ""])
        lines.append(
            f"| {labels['file']} | {labels['version']} | {labels['git_version_file']} | {labels['build_branch']} | {labels['commit']} | {labels['companies']} |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in builds:
            if not isinstance(item, dict):
                continue
            companies = item.get("companies") or []
            if isinstance(companies, list):
                companies = ", ".join(str(value) for value in companies[:40])
            lines.append(
                f"| `{item.get('file', '-')}` | `{item.get('ver_number', '-')}` | `{item.get('git_version', '-')}` | `{item.get('build_repo_branch', '-')}` | `{item.get('build_repo_commit', '-')}` | {companies or '-'} |"
            )
        lines.append("")
    if isinstance(build_history_files, list) and build_history_files:
        lines.extend([f"### {labels['build_history_files']}", ""])
        lines.extend(f"- `{item}`" for item in build_history_files)
        lines.append("")
    source_reviews = metadata.get("source_repository_reviews") or []
    if isinstance(source_reviews, list) and source_reviews:
        lines.extend([f"### {labels['locked_repo_reviews']}", ""])
        lines.append(
            f"| {labels['kind']} | {labels['module']} | {labels['branch']} | {labels['commit']} | {labels['issues']} | {labels['files']} | {labels['compare']} | {labels['resource_check']} | {labels['commit_title']} |"
        )
        lines.append("| --- | --- | --- | --- | --- | ---: | --- | --- | --- |")
        for item in source_reviews[:120]:
            if not isinstance(item, dict):
                continue
            issues = item.get("issue_keys") or []
            if isinstance(issues, list):
                issues = ", ".join(str(value) for value in issues)
            compare = _compare_summary(item)
            resource_check = _resource_validation_summary(item, labels)
            lines.append(
                f"| `{item.get('kind', '-')}` | `{item.get('module', '-')}` | `{item.get('branch', '-')}` | `{item.get('commit', '-')}` | {issues or '-'} | {item.get('files_count', '-')} | {compare} | {resource_check} | {str(item.get('title', '-')).replace('|', '/')} |"
            )
        lines.append("")
    source_errors = metadata.get("source_repository_review_errors") or []
    if isinstance(source_errors, list) and source_errors:
        lines.extend(["### Locked Repository Fetch Errors", ""])
        for item in source_errors:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('module', '-')}` `{item.get('commit', '-')}`: {item.get('error', '-')}")
        lines.append("")
    release_gate = metadata.get("release_gate") or {}
    if isinstance(release_gate, dict) and release_gate:
        is_english = labels.get("title") == "Code Review Report"
        gate_heading = "Release Gate" if is_english else "发布闸门"
        status_label = "Status" if is_english else "状态"
        script_source_label = "Script source" if is_english else "脚本来源"
        resource_groups_label = "Configured company/environment resource groups:" if is_english else "公司/环境配置资源分组："
        blocker_label = "Release-gate blockers:" if is_english else "发布闸门阻塞项："
        boundary_label = "Post-build validation boundary:" if is_english else "构建后校验边界："
        status = str(release_gate.get("status") or "unknown").upper()
        lines.extend([f"### {gate_heading}", "", f"- {status_label}: **{status}**", f"- {script_source_label}: {release_gate.get('scripts_source') or '-'}", ""])
        resources = release_gate.get("resources") or []
        if isinstance(resources, list) and resources:
            lines.append("| Locked Build Commit | Config Files | Database Files | Code Files | Previous Build Lock |" if is_english else "| 锁定构建 Commit | 配置文件 | 数据库文件 | 代码文件 | 上一个构建锁定 |")
            lines.append("| --- | ---: | ---: | ---: | --- |")
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                payload = resource.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            f"`{str(resource.get('commit') or '-')[:12]}`",
                            str(payload.get("config_file_count") or 0),
                            str(len(payload.get("database_files") or [])),
                            str(len(payload.get("code_files") or [])),
                            f"`{str(resource.get('previous_commit') or '-')[:12]}`",
                        ]
                    )
                    + " |"
                )
            lines.append("")
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                payload = resource.get("payload") or {}
                groups = payload.get("config_groups") if isinstance(payload, dict) else []
                if not isinstance(groups, list) or not groups:
                    continue
                lines.append(resource_groups_label)
                for group in groups[:40]:
                    if isinstance(group, dict):
                        companies = ", ".join(str(item) for item in group.get("companies") or []) or "SV"
                        lines.append(f"- `{group.get('logical_path', '-')}`: {companies}")
                lines.append("")
            script_rows: list[tuple[str, dict[str, object]]] = []
            database_rows: list[tuple[str, dict[str, object]]] = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                commit = str(resource.get("commit") or "-")[:12]
                for check in resource.get("scripts") or []:
                    if isinstance(check, dict):
                        script_rows.append((commit, check))
                for check in resource.get("database_scripts") or []:
                    if isinstance(check, dict):
                        database_rows.append((commit, check))
            if script_rows:
                lines.extend(
                    [
                        "Locked runtime script checks:" if is_english else "锁定运行时脚本校验：",
                        "",
                        "| Build Commit | Runtime Resource | Version | Status |" if is_english else "| 构建 Commit | 运行时资源 | 版本 | 状态 |",
                        "| --- | --- | --- | --- |",
                    ]
                )
                for commit, check in script_rows:
                    state = "present" if check.get("exists") else "missing"
                    if not is_english:
                        state = "存在" if check.get("exists") else "缺失"
                    lines.append(f"| `{commit}` | `{check.get('path', '-')}` | `{check.get('version') or '-'}` | {state} |")
                lines.append("")
            if database_rows:
                lines.extend(
                    [
                        "Locked database payload checks:" if is_english else "锁定数据库包校验：",
                        "",
                        "| Build Commit | db_change.scr | Blocks | Referenced Resources | Errors |" if is_english else "| 构建 Commit | db_change.scr | 区块数 | 引用资源数 | 错误数 |",
                        "| --- | --- | ---: | ---: | ---: |",
                    ]
                )
                for commit, check in database_rows:
                    lines.append(
                        f"| `{commit}` | `{check.get('path', '-')}` | {len(check.get('blocks') or [])} | "
                        f"{len(check.get('references') or [])} | {len(check.get('errors') or [])} |"
                    )
                lines.append("")
        errors = release_gate.get("errors") or []
        if isinstance(errors, list) and errors:
            lines.append(blocker_label)
            for item in errors:
                if isinstance(item, dict):
                    lines.append(f"- [{item.get('severity', 'High')}] {item.get('title', '-')}: {item.get('detail', '-')}")
            lines.append("")
        artifacts = release_gate.get("post_build_artifacts") or []
        if isinstance(artifacts, list) and artifacts:
            lines.extend([boundary_label, *[f"- {item}" for item in artifacts], ""])
    return lines


def _compare_summary(item: dict[str, object]) -> str:
    from_commit = str(item.get("compare_from_commit") or "")
    to_commit = str(item.get("compare_to_commit") or "")
    if not from_commit:
        return "-"
    commits_count = item.get("compare_commits_count", "-")
    previous_file = str(item.get("previous_git_version_file") or "")
    previous = f"<br>`{previous_file}`" if previous_file else ""
    return f"`{from_commit[:10]}` -> `{(to_commit or str(item.get('commit') or ''))[:10]}` ({commits_count}){previous}"


def _resource_validation_summary(item: dict[str, object], labels: dict[str, str]) -> str:
    validation = item.get("resource_validation")
    if not isinstance(validation, dict):
        return "-"
    checks = validation.get("required_files") or []
    if not isinstance(checks, list) or not checks:
        return "-"
    parts: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = labels["present"] if check.get("exists") else labels["missing"]
        role = str(check.get("role") or "-").replace("|", "/")
        path = str(check.get("path") or "-").replace("|", "/")
        parts.append(f"{role}:{status} `{path}`")
    return "<br>".join(parts) if parts else "-"


_EN_TO_ZH = {
    "Blocked: Critical risk found; fix before merge.": "阻塞：存在 Critical 风险，建议修复后再合并。",
    "Changes required: High risk found; fix it or provide an explicit waiver.": "需修改：存在 High 风险，建议修复或给出明确豁免说明。",
    "Needs review: no blocking issue found, but medium/low risks still need reviewer confirmation.": "需复核：未发现阻塞问题，但仍有中低风险需要 reviewer 确认。",
    "Passed: no obvious risk found.": "通过：未发现明显风险。",
    "Possible hard-coded secret": "疑似硬编码敏感信息",
    "The added line looks like it may contain a password, token, secret, or private key assignment.": "新增代码看起来包含密码、token、secret 或 private key 赋值。",
    "Move sensitive values to environment variables or a secret manager, and rotate the exposed value if it is real.": "将敏感值移到环境变量或密钥管理工具中；如果该值真实有效，需要轮换。",
    "Possible dynamic SQL construction": "疑似动态 SQL 拼接",
    "The added SQL appears to be assembled dynamically, which can introduce injection risk or fragile query behavior.": "新增 SQL 似乎通过动态拼接生成，可能引入注入风险或脆弱查询行为。",
    "Use parameterized queries or the project database abstraction layer, and add tests for unsafe input.": "使用参数化查询或项目数据库抽象层，并补充不安全输入测试。",
    "Debug statement added": "新增调试语句",
    "Debug-only code was added in the diff.": "本次 diff 新增了仅用于调试的代码。",
    "Remove debug statements before merging, or guard them behind the existing logging configuration.": "合并前删除调试语句，或通过现有日志配置进行保护。",
    "Unresolved TODO/FIXME": "未解决的 TODO/FIXME",
    "The diff introduces a TODO/FIXME marker.": "本次 diff 引入了 TODO/FIXME 标记。",
    "Resolve it in this MR or link it to a Jira issue with clear ownership.": "在本 MR 中解决，或关联到有明确负责人的 Jira issue。",
    "Large file-level change": "单文件变更过大",
    "Migration script needs rollback and rerun safety": "迁移脚本需要回滚与重复执行安全性",
    "API change without BIZ-layer change": "API 变更未伴随 BIZ 层变更",
    "DAO change without caller-layer change": "DAO 变更未伴随调用层变更",
    "The MR touches DAO code but no BIZ/CLI caller changes were detected.": "MR 修改了 DAO 代码，但未检测到 BIZ/CLI 调用层变更。",
    "Confirm all callers handle changed query shape, transaction behavior, and error cases.": "确认所有调用方都能处理查询结果结构、事务行为和错误场景的变化。",
    "CLI flow touches backend layers": "CLI 流程涉及后端层变更",
    "Duplicate YAML key in GIT_VERSION config": "GIT_VERSION 配置存在重复 YAML key",
    "Repository lock has no commit or tag": "仓库锁定缺少 commit 或 tag",
    "Invalid locked commit format": "锁定 commit 格式不正确",
    "Repository URL missing in GIT_VERSION entry": "GIT_VERSION 条目缺少仓库 URL",
    "Branch missing in GIT_VERSION entry": "GIT_VERSION 条目缺少分支",
    "build.yml does not reference git_version.yml": "build.yml 未引用 git_version.yml",
    "Build repository commit is not locked": "构建仓库 commit 未锁定",
    "Invalid build repository commit format": "构建仓库 commit 格式不正确",
    "Locked build repository commit is missing required build resources": "锁定的构建仓库 commit 缺少必要构建资源",
    "git_version.yml and build.yml version suffix differ": "git_version.yml 与 build.yml 版本后缀不一致",
    "Locked repository commit was not fetched for deep review": "未能拉取锁定仓库 commit 进行深度审查",
    "Release notes issues are not traceable to locked source commit": "Release notes issue 无法追踪到锁定源码 commit",
    "No active companies configured for build": "build.yml 未配置 active companies",
    "git_version.yml changed without build.yml": "git_version.yml 变更未伴随 build.yml",
    "build.yml changed without git_version.yml": "build.yml 变更未伴随 git_version.yml",
    "YAML keeps only the last value, so the earlier locked value can be silently ignored.": "YAML 只保留最后一个值，前面的锁定值可能被静默忽略。",
    "Keep exactly one key per mapping. For commit locks, remove the stale commit and verify the final commit is the intended one.": "每个 mapping 中只保留一个 key。对于 commit 锁定，需要删除过期 commit，并确认最终 commit 是预期值。",
    "Lock each repository to a 40-character commit SHA unless the release process explicitly uses immutable tags.": "除非发布流程明确使用不可变 tag，否则每个仓库都应锁定到 40 位 commit SHA。",
    "Use the exact full Git commit SHA for the selected branch.": "使用所选分支上的完整 Git commit SHA。",
    "Set repository_url so the build can fetch the locked source repository deterministically.": "设置 repository_url，确保构建可以确定性拉取被锁定的源码仓库。",
    "Record the source branch used to select the locked commit for traceability.": "记录用于选择锁定 commit 的源分支，保证可追溯。",
    "The build config should point to the git_version.yml file that locks development repositories.": "构建配置应指向用于锁定开发仓库的 git_version.yml 文件。",
    "Set version.git_version to the intended git_version*.yml in the same version directory unless an absolute path is intentionally used.": "除非明确使用绝对路径，否则应将 version.git_version 设置为同版本目录下预期的 git_version*.yml。",
    "build.yml does not lock the build-code repository commit.": "build.yml 未锁定构建代码仓库 commit。",
    "Set version.git_repository.commit or version.git_version4config.git_repository.commit to a build repository commit after the required build resources were pushed.": "将 version.git_repository.commit 或 version.git_version4config.git_repository.commit 设置为必要构建资源推送后的构建仓库 commit。",
    "Lock version.git_repository.commit/version.git_version4config.git_repository.commit to a build repository commit after the required build.yml and git_version.yml resources were pushed, or correct the referenced resource path.": "将 version.git_repository.commit/version.git_version4config.git_repository.commit 锁定到必要 build.yml 和 git_version.yml 资源推送后的构建仓库 commit，或修正引用的资源路径。",
    "Use the full commit SHA for the build-code repository.": "构建代码仓库应使用完整 commit SHA。",
    "Confirm whether this MR intentionally mixes version files. For the same build target, git_version-v<version>.yml and build-v<version>.yml should usually align with the revision/base version or the bh-derived patch version.": "确认该 MR 是否有意混用版本文件。对于同一构建目标，git_version-v<version>.yml 和 build-v<version>.yml 通常应与 revision/base version 或 bh 推导出的 patch version 保持一致。",
    "Fetch the locked repository commit diff and review the actual code changes before approving this GIT_VERSION MR.": "批准该 GIT_VERSION MR 前，需要拉取锁定仓库 commit diff 并审查真实代码变更。",
    "Confirm the locked git_version commit contains the source/config changes for every release-note Jira issue. If those changes are in earlier commits, provide the previous-version-to-current-version compare evidence; otherwise update git_version.yml or release notes.": "确认锁定的 git_version commit 包含每个 release-note Jira issue 对应的源码/配置变更。如果这些变更位于更早的提交，需要提供从上一版本到当前版本的 compare 证据；否则应更新 git_version.yml 或 release notes。",
    "build.yml companies list is empty, so company configuration packages may not be produced.": "build.yml companies 列表为空，可能不会产出公司配置包。",
    "Confirm this is code-only build intent; otherwise include the required company codes.": "确认这是否是只构建代码包；否则需要加入必要的公司代码。",
    "The MR changes development repository locks but no build.yml change was detected.": "MR 修改了开发仓库锁定，但未检测到 build.yml 变更。",
    "Confirm whether build.yml already points to the intended git_version file and build repository commit. For GIT_VERSION branches, the build repository commit often needs a final self-lock update.": "确认 build.yml 是否已指向预期的 git_version 文件和构建仓库 commit。对于 GIT_VERSION 分支，构建仓库 commit 应锁定到构建资源推送后的有效提交。",
    "The MR changes build configuration but no git_version.yml lock change was detected.": "MR 修改了构建配置，但未检测到 git_version.yml 锁定变更。",
    "Confirm this is a build-only config MR; otherwise include the repository lock file for the version.": "确认这是否是纯构建配置 MR；否则应包含该版本的仓库锁定文件。",
    "GIT_VERSION": "GIT_VERSION",
    "Security": "安全",
    "Maintainability": "可维护性",
    "Reviewability": "可审查性",
    "Data Migration": "数据迁移",
    "DPS Layering": "DPS 分层",
    "Architecture": "架构",
    "Run the affected module's existing unit/integration test suite before merge.": "合并前运行受影响模块现有的单元测试/集成测试。",
    "Add regression coverage for each fixed business rule or data migration path.": "为每个修复的业务规则或数据迁移路径补充回归覆盖。",
    "Add tests for unsafe input, missing auth, and secret/config handling.": "补充不安全输入、缺失鉴权和密钥/配置处理相关测试。",
    "Run migration dry-run, rerun, partial-failure, and rollback scenarios.": "运行迁移 dry-run、重复执行、部分失败和回滚场景验证。",
    "Run browser/mobile UI smoke tests for changed screens and API error states.": "对变更页面和 API 错误状态执行浏览器/移动端冒烟测试。",
    "MR touches more than 20 files; review scope may be too broad for one merge request.": "MR 涉及超过 20 个文件；单次合并请求的审查范围可能过大。",
    "No Jira issue key was detected; requirement traceability needs manual confirmation.": "未检测到 Jira issue key；需求可追溯性需要人工确认。",
    "No changed files were loaded; verify GitLab token, MR URL, or local diff input.": "未加载到变更文件；请检查 GitLab token、MR URL 或本地 diff 输入。",
    "LLM provider is disabled; rule-based review was used.": "LLM provider 已禁用；本次使用规则审查。",
    "LLM_NETWORK_MODE=non-vpn; skipped Codex by explicit network-mode override.": "LLM_NETWORK_MODE=non-vpn；已按显式网络模式配置跳过 Codex。",
    "Provider returned non-JSON output. Parse error: provider returned non-JSON review output.": "Provider 返回了非 JSON 输出。解析错误：provider 返回的审查结果不是 JSON。",
}

_ZH_TO_EN = {value: key for key, value in _EN_TO_ZH.items()}
