from __future__ import annotations

import re
from typing import Any

from .config import report_min_severity, severity_meets_minimum
from .diff_parser import added_lines_with_numbers
from .git_version_review import enrich_git_version_review
from .knowledge_context import attach_knowledge_context
from .llm_provider import llm_metadata, preview_llm_prompt_budget, run_llm_review
from .models import Finding, ReviewInput, ReviewResult


SECRET_ASSIGN_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.-]*(?:password|passwd|pwd|token|secret|private[_-]?key|access[_-]?key|api[_-]?key)[A-Za-z0-9_.-]*)\s*[:=]\s*(?P<value>.+)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(glpat-|sk-[A-Za-z0-9]|ghp_[A-Za-z0-9]|xox[baprs]-|bearer\s+[A-Za-z0-9]|-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY)",
    re.I,
)
SQL_RE = re.compile(
    r"\b(?:select\s+.+?\s+from|insert\s+into|update\s+[\w.]+\s+set|delete\s+from)\b.*(?:\+|%|\{|\$)",
    re.I,
)
DEBUG_RE = re.compile(r"\b(var_dump|print_r|console\.log|debugger|pdb\.set_trace|dd\()", re.I)
INVOLVED_FILE_HEADING_RE = re.compile(
    r"(?:Involved\s+File\s+Lists?|涉及的文件列表|涉及文件列表|涉及的文件清单|涉及文件清单|涉及文件)",
    re.I,
)
NEXT_JIRA_SECTION_RE = re.compile(
    r"\b(?:Acceptance\s+Criteria|Test(?:ing)?\s+Notes?|Implementation\s+Notes?|Release\s+Notes?|Risk|Remarks?|Additional\s+Remarks?)\b|"
    r"(?:验收标准|测试|实现说明|发布说明|风险|备注|补充)",
    re.I,
)
PATH_LIKE_RE = re.compile(
    r"(?:[A-Za-z0-9_.#@+()!-]+[\\/])+[A-Za-z0-9_.#@+()!-]+(?:\.[A-Za-z0-9_+-]+)?"
)


def analyze(review_input: ReviewInput, progress: Any = None) -> ReviewResult:
    attach_knowledge_context(review_input)
    findings: list[Finding] = []

    for changed_file in review_input.changed_files:
        file_path = changed_file.path
        for line_no, line in added_lines_with_numbers(changed_file.diff):
            stripped = line.strip()
            if not stripped:
                continue
            findings.extend(_line_findings(file_path, line_no, stripped))

        findings.extend(_file_findings(changed_file.path, changed_file.additions, changed_file.deletions))

    findings.extend(_architecture_findings(review_input))
    findings.extend(_jira_involved_file_findings(review_input))
    findings.extend(enrich_git_version_review(review_input))
    budget = preview_llm_prompt_budget(review_input)
    _progress_context_budget(progress, review_input, budget)
    llm_output = run_llm_review(review_input)
    llm_findings, suppressed_notes = _filter_llm_findings(review_input, llm_output.findings)
    llm_output.notes.extend(suppressed_notes)
    findings.extend(llm_findings)
    review_input.metadata.update(llm_metadata(llm_output))
    findings = _deduplicate(findings)
    findings = _filter_report_severity(review_input, findings)

    conclusion = _conclusion(findings)
    risk_summary = _risk_summary(review_input, findings)
    test_suggestions = _test_suggestions(review_input, findings)
    return ReviewResult(
        review_input=review_input,
        findings=findings,
        conclusion=conclusion,
        risk_summary=risk_summary,
        test_suggestions=test_suggestions,
    )


def _progress_context_budget(progress: Any, review_input: ReviewInput, budget: dict[str, Any]) -> None:
    if not progress or not isinstance(budget, dict):
        return
    try:
        sections = budget.get("sections") if isinstance(budget.get("sections"), dict) else {}
        progress(
            {
                "event": "context-size",
                "message": (
                    f"LLM context {budget.get('final_chars', budget.get('original_chars', '-'))}/"
                    f"{budget.get('max_chars', '-')} chars"
                ),
                "jira_key": review_input.jira_key,
                "original_chars": budget.get("original_chars"),
                "final_chars": budget.get("final_chars"),
                "max_chars": budget.get("max_chars"),
                "trimmed_chars": budget.get("trimmed_chars"),
                "hard_truncated": budget.get("hard_truncated", False),
                "sections": sections,
            }
        )
    except Exception:
        pass


def _line_findings(file_path: str, line_no: int | None, line: str) -> list[Finding]:
    findings: list[Finding] = []
    if (
        _looks_like_hardcoded_secret(line)
        and not line.startswith(("#", "//", "*"))
        and not _is_dps_state_config_path(file_path)
    ):
        findings.append(
            Finding(
                severity="Critical",
                file_path=file_path,
                line=line_no,
                title="Possible hard-coded secret",
                detail="The added line looks like it may contain a password, token, secret, or private key assignment.",
                recommendation="Move sensitive values to environment variables or a secret manager, and rotate the exposed value if it is real.",
                category="Security",
            )
        )

    if SQL_RE.search(line):
        findings.append(
            Finding(
                severity="High",
                file_path=file_path,
                line=line_no,
                title="Possible dynamic SQL construction",
                detail=(
                    "Rule-based precheck found an added SQL-shaped statement that appears to be assembled dynamically. "
                    "Before merge, verify the user-controlled input path, the exact SQL sink, and whether the project DB "
                    "abstraction or placeholders are used. Treat this as blocking only when the value reaches SQL without "
                    "parameter binding."
                ),
                recommendation=(
                    "Use parameterized queries or the existing database abstraction layer. Add a regression test with unsafe "
                    "characters in the relevant request/CLI option, and capture the generated query or DB call to prove the "
                    "value is bound instead of concatenated."
                ),
                category="Security",
            )
        )

    if DEBUG_RE.search(line):
        findings.append(
            Finding(
                severity="Medium",
                file_path=file_path,
                line=line_no,
                title="Debug statement added",
                detail="Debug-only code was added in the diff.",
                recommendation="Remove debug statements before merging, or guard them behind the existing logging configuration.",
                category="Maintainability",
            )
        )

    if "TODO" in line or "FIXME" in line:
        findings.append(
            Finding(
                severity="Low",
                file_path=file_path,
                line=line_no,
                title="Unresolved TODO/FIXME",
                detail="The diff introduces a TODO/FIXME marker.",
                recommendation="Resolve it in this MR or link it to a Jira issue with clear ownership.",
                category="Maintainability",
            )
        )

    return findings


def _is_dps_state_config_path(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        name.startswith(("state_config", "state_cofig"))
        and name.endswith((".yml", ".yaml"))
    ) or bool(re.search(r"/state_c(?:onfig|ofig)(?:[._-][a-z0-9-]+)?\.ya?ml$", normalized))


def _looks_like_hardcoded_secret(line: str) -> bool:
    match = SECRET_ASSIGN_RE.search(line)
    if not match:
        return False
    key = match.group("key").lower()
    value = match.group("value").strip().rstrip(",;")
    normalized_value = value.strip("'\"").strip()
    benign_values = {
        "",
        "null",
        "undefined",
        "false",
        "true",
        "y",
        "n",
        "-1",
        "0",
        "1",
        "\"\"",
        "''",
    }
    if normalized_value.lower() in benign_values:
        return False
    if "token" in key:
        if SECRET_VALUE_RE.search(value):
            return True
        compact = re.sub(r"[^A-Za-z0-9]", "", normalized_value)
        return len(compact) >= 24 and not normalized_value.startswith("$")
    if normalized_value.startswith(("process.env.", "os.getenv(", "settings.", "config.")):
        return False
    return bool(SECRET_VALUE_RE.search(value) or normalized_value not in {"***", "REDACTED", "placeholder"})


def _file_findings(file_path: str, additions: int, deletions: int) -> list[Finding]:
    findings: list[Finding] = []
    total = additions + deletions
    if total >= 500:
        findings.append(
            Finding(
                severity="Medium",
                file_path=file_path,
                line=None,
                title="Large file-level change",
                detail=f"This file has {additions} additions and {deletions} deletions.",
                recommendation="Consider splitting the MR or adding a short design note that explains the migration/refactor scope.",
                category="Reviewability",
            )
        )
    if file_path.lower().endswith((".sql", ".php", ".py")) and "migration" in file_path.lower():
        findings.append(
            Finding(
                severity="Medium",
                file_path=file_path,
                line=None,
                title="Migration script needs rollback and rerun safety",
                detail="Migration-like files should be safe to rerun and should avoid updating DB state before validating files/data.",
                recommendation="Document rollback behavior, add dry-run support, and write per-record success/skip/error logs.",
                category="Data Migration",
            )
        )
    return findings


def _architecture_findings(review_input: ReviewInput) -> list[Finding]:
    paths = [item.path.replace("\\", "/") for item in review_input.changed_files]
    findings: list[Finding] = []
    has_api = any("/API/" in path or "/Controller/" in path for path in paths)
    has_biz = any("/BIZ/" in path for path in paths)
    has_dao = any("/DAO/" in path for path in paths)
    has_cli = any("/CLI/" in path or "Command" in path for path in paths)

    if has_api and not has_biz:
        findings.append(
            Finding(
                severity="Medium",
                file_path="Architecture",
                line=None,
                title="API change without BIZ-layer change",
                detail="The MR touches API/controller code but no BIZ layer files were detected.",
                recommendation="Confirm whether business validation belongs in BIZ and whether controller code is only orchestration.",
                category="DPS Layering",
            )
        )
    if has_dao and not (has_biz or has_cli):
        findings.append(
            Finding(
                severity="Medium",
                file_path="Architecture",
                line=None,
                title="DAO change without caller-layer change",
                detail="The MR touches DAO code but no BIZ/CLI caller changes were detected.",
                recommendation="Confirm all callers handle changed query shape, transaction behavior, and error cases.",
                category="DPS Layering",
            )
        )
    if has_cli and (has_dao or has_biz):
        findings.append(
            Finding(
                severity="Low",
                file_path="Architecture",
                line=None,
                title="CLI flow touches backend layers",
                detail="The MR includes CLI plus backend-layer changes.",
                recommendation="Add a dry-run mode or operator note when this CLI performs data changes.",
                category="DPS Layering",
            )
        )
    return findings


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, int | None, str]] = set()
    result: list[Finding] = []
    for finding in findings:
        key = (finding.file_path, finding.title, finding.line, finding.severity)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Warning": 4}
    return sorted(result, key=lambda item: (severity_order.get(item.severity, 9), item.file_path, item.line or 0))


def _filter_report_severity(review_input: ReviewInput, findings: list[Finding]) -> list[Finding]:
    minimum = report_min_severity()
    kept = [finding for finding in findings if severity_meets_minimum(finding.severity, minimum)]
    removed = [finding for finding in findings if not severity_meets_minimum(finding.severity, minimum)]
    review_input.metadata["report_min_severity"] = minimum
    if removed:
        counts: dict[str, int] = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
        for finding in findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        removed_counts: dict[str, int] = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
        for finding in removed:
            removed_counts[finding.severity] = removed_counts.get(finding.severity, 0) + 1
        review_input.metadata["unfiltered_severity_counts"] = counts
        review_input.metadata["filtered_out_severity_counts"] = removed_counts
        review_input.metadata["filtered_out_finding_count"] = len(removed)
    else:
        review_input.metadata.pop("unfiltered_severity_counts", None)
        review_input.metadata.pop("filtered_out_severity_counts", None)
        review_input.metadata["filtered_out_finding_count"] = 0
    return kept


def _jira_involved_file_findings(review_input: ReviewInput) -> list[Finding]:
    description = str(review_input.metadata.get("jira_description") or "").strip()
    expected = _extract_involved_file_list(description)
    if not expected:
        review_input.metadata.pop("jira_involved_files_check", None)
        return []

    actual = [_normalize_actual_changed_path(item.path) for item in review_input.changed_files]
    actual = _unique_preserve_order([item for item in actual if item])
    deferred_actual = _deferred_release_gate_file_paths(review_input)
    excluded_deferred = [item for item in expected if _path_matches_any(item, deferred_actual)]
    comparison_expected = [item for item in expected if item not in excluded_deferred]
    missing = [item for item in comparison_expected if not _path_matches_any(item, actual)]
    unexpected = [item for item in actual if not _path_matches_any(item, comparison_expected)]
    review_input.metadata["jira_involved_files_check"] = {
        "expected": comparison_expected,
        "original_expected": expected,
        "actual": actual,
        "deferred_actual": deferred_actual,
        "excluded_deferred": excluded_deferred,
        "effective_actual": actual,
        "missing": missing,
        "unexpected": unexpected,
    }
    if not missing and not unexpected:
        return []

    details: list[str] = []
    if missing:
        details.append("Expected in Jira but not changed: " + ", ".join(missing[:20]))
    if unexpected:
        details.append("Changed in MR but not listed in Jira: " + ", ".join(unexpected[:20]))
    if len(missing) > 20 or len(unexpected) > 20:
        details.append("Only the first 20 paths in each mismatch direction are shown.")
    return [
        Finding(
            severity="Critical",
            file_path="Jira Issue",
            line=None,
            title="Jira involved file list does not match MR diff",
            detail="; ".join(details),
            recommendation=(
                "Clarify or update the ECHNL issue description's Involved FIle Lists / 涉及的文件清单 so it matches the real MR "
                "file scope, or confirm in the MR why the implementation scope intentionally differs."
            ),
            category="Traceability",
        )
    ]


def _deferred_release_gate_file_paths(review_input: ReviewInput) -> list[str]:
    resources = review_input.metadata.get("deferred_release_gate_resources") or []
    if not isinstance(resources, list):
        return []
    paths: list[str] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        role = re.sub(r"[^a-z0-9]+", "_", str(resource.get("release_gate_role") or "").lower()).strip("_")
        if role not in {"company_config", "scr"}:
            continue
        for value in resource.get("changed_file_paths") or []:
            normalized = _normalize_actual_changed_path(str(value))
            if normalized:
                paths.append(normalized)
    return _unique_preserve_order(paths)


def _extract_involved_file_list(description: str) -> list[str]:
    if not description:
        return []
    headings = list(INVOLVED_FILE_HEADING_RE.finditer(description))
    if not headings:
        return []
    values: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(description)
        section = description[heading.end() : end]
        next_section = NEXT_JIRA_SECTION_RE.search(section)
        if next_section and next_section.start() > 0:
            section = section[: next_section.start()]
        values.extend(_clean_expected_path(item) for item in PATH_LIKE_RE.findall(section))
    return _unique_preserve_order([item for item in values if _looks_like_source_path(item)])


def _clean_expected_path(value: str) -> str:
    text = (value or "").strip().strip("`'\"，,;；。.)]}")
    text = text.replace("\\", "/")
    text = re.sub(r"\s+", "", text)
    if "!" in text:
        repository, file_path = text.rsplit("!", 1)
        if repository and "/" in file_path:
            text = file_path
    return text.strip("/")


def _normalize_actual_changed_path(value: str) -> str:
    text = (value or "").replace("\\", "/").strip("/")
    text = re.sub(r"^[^!]+!\d+/", "", text)
    return text


def _looks_like_source_path(value: str) -> bool:
    text = value.strip()
    if not text or "/" not in text:
        return False
    lowered = text.lower()
    if any(token in lowered for token in ("merge_requests", "/browse/", "atlassian.net/", "gitlab.", "http/")):
        return False
    if re.search(r"\.(?:php|inc|module|install|yml|yaml|json|js|ts|tsx|jsx|vue|css|scss|sql|xml|html|twig|md|java|py|sh|ps1|properties|conf|ini|lock)$", text, re.I):
        return True
    return len(text.split("/")) >= 2


def _path_matches_any(path: str, candidates: list[str]) -> bool:
    return any(_paths_match(path, candidate) for candidate in candidates)


def _paths_match(left: str, right: str) -> bool:
    a = _clean_expected_path(left).lower()
    b = _clean_expected_path(right).lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if a.endswith("/"):
        return b.startswith(a)
    if b.endswith("/"):
        return a.startswith(b)
    return a.endswith(f"/{b}") or b.endswith(f"/{a}")


def _unique_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _filter_llm_findings(review_input: ReviewInput, findings: list[Finding]) -> tuple[list[Finding], list[str]]:
    result: list[Finding] = []
    notes: list[str] = []
    suppressed_self_lock = False
    suppressed_compat_key = False
    for finding in findings:
        if _is_allowed_build_resource_self_lock_false_positive(review_input, finding):
            suppressed_self_lock = True
            continue
        if _is_known_build_config_compat_false_positive(review_input, finding):
            suppressed_compat_key = True
            continue
        result.append(finding)

    reduced = _remove_subsumed_git_version_traceability_findings(review_input, result)
    if len(reduced) != len(result):
        notes.append("Suppressed duplicate GIT_VERSION traceability findings already covered by a higher severity finding.")
    if suppressed_self_lock:
        notes.append(
            "Suppressed a GIT_VERSION build self-lock finding because the locked build commit contains the required build resources."
        )
    if suppressed_compat_key:
        notes.append("Suppressed a known build config compatibility key finding.")
    return reduced, notes


def _is_allowed_build_resource_self_lock_false_positive(review_input: ReviewInput, finding: Finding) -> bool:
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return False
    if review_input.metadata.get("build_resource_validation_errors"):
        return False
    reviews = review_input.metadata.get("source_repository_reviews") or []
    has_valid_build_resource_lock = any(
        isinstance(item, dict)
        and item.get("kind") == "build-resource"
        and isinstance(item.get("resource_validation"), dict)
        and item["resource_validation"].get("valid") is True
        for item in reviews
    )
    if not has_valid_build_resource_lock:
        return False

    text = " ".join([finding.file_path, finding.title, finding.detail, finding.recommendation]).lower()
    mentions_build_lock = any(
        token in text
        for token in [
            "build.yml",
            "git_repository.commit",
            "build repository commit",
            "self-lock",
            "自锁",
            "构建仓库",
        ]
    )
    compares_to_mr_head = any(
        token in text
        for token in [
            "current mr",
            "mr head",
            "mr commit",
            "mr 提交",
            "当前 mr",
            "当前的 mr",
        ]
    )
    return mentions_build_lock and compares_to_mr_head


def _is_known_build_config_compat_false_positive(review_input: ReviewInput, finding: Finding) -> bool:
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return False
    compat_notes = review_input.metadata.get("build_config_compat_notes") or []
    if not isinstance(compat_notes, list) or not any("replace_with_ttl.companines" in str(item) for item in compat_notes):
        return False
    text = " ".join([finding.file_path, finding.title, finding.detail, finding.recommendation]).lower()
    return "companines" in text and any(token in text for token in ["typo", "spelling", "拼写", "疑似"])


def _remove_subsumed_git_version_traceability_findings(
    review_input: ReviewInput,
    findings: list[Finding],
) -> list[Finding]:
    if review_input.metadata.get("mr_type") != "GIT_VERSION":
        return findings
    has_high_traceability = any(
        finding.severity in {"Critical", "High"} and _is_git_version_traceability_text(finding)
        for finding in findings
    )
    if not has_high_traceability:
        return findings
    result: list[Finding] = []
    for finding in findings:
        if finding.severity in {"Medium", "Low"} and _is_git_version_traceability_text(finding):
            continue
        result.append(finding)
    return result


def _is_git_version_traceability_text(finding: Finding) -> bool:
    text = " ".join([finding.file_path, finding.title, finding.detail, finding.recommendation]).lower()
    mentions_release_notes = any(token in text for token in ["release notes", "发布说明", "release note"])
    mentions_locked_source = any(token in text for token in ["git_version", "locked source", "锁定源码", "锁定提交"])
    mentions_issue = bool(re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", text, re.I)) or "echnl" in text
    return mentions_release_notes and mentions_locked_source and mentions_issue


def _conclusion(findings: list[Finding]) -> str:
    if any(item.severity == "Critical" for item in findings):
        return "阻塞：存在 Critical 风险，建议修复后再合并。"
    if any(item.severity == "High" for item in findings):
        return "需修改：存在 High 风险，建议修复或给出明确豁免说明。"
    if findings:
        return "需复核：未发现阻塞问题，但仍有中低风险需要 reviewer 确认。"
    return "通过：未发现明显风险。"


def _risk_summary(review_input: ReviewInput, findings: list[Finding]) -> list[str]:
    risks: list[str] = []
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Warning": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    risks.append(
        f"Detected {len(findings)} finding(s): "
        f"{counts['Critical']} Critical, {counts['High']} High, {counts['Medium']} Medium, "
        f"{counts['Low']} Low, {counts['Warning']} Warning."
    )
    if len(review_input.changed_files) > 20:
        risks.append("MR touches more than 20 files; review scope may be too broad for one merge request.")
    if not review_input.jira_key:
        risks.append("No Jira issue key was detected; requirement traceability needs manual confirmation.")
    if not review_input.changed_files:
        risks.append("No changed files were loaded; verify GitLab token, MR URL, or local diff input.")
    return risks


def _test_suggestions(review_input: ReviewInput, findings: list[Finding]) -> list[str]:
    suggestions = [
        "Run the affected module's existing unit/integration test suite before merge.",
        "Add regression coverage for each fixed business rule or data migration path.",
    ]
    categories = {finding.category for finding in findings}
    if "Security" in categories:
        suggestions.append("Add tests for unsafe input, missing auth, and secret/config handling.")
    if "Data Migration" in categories:
        suggestions.append("Run migration dry-run, rerun, partial-failure, and rollback scenarios.")
    if any(path.path.lower().endswith((".dart", ".ts", ".js", ".vue")) for path in review_input.changed_files):
        suggestions.append("Run browser/mobile UI smoke tests for changed screens and API error states.")
    release_gate = review_input.metadata.get("release_gate") or {}
    if isinstance(release_gate, dict):
        resources = release_gate.get("resources") or []
        has_database_payload = any(
            isinstance(item, dict)
            and isinstance(item.get("payload"), dict)
            and bool(item["payload"].get("database_files"))
            for item in resources
        )
        has_config_payload = any(
            isinstance(item, dict)
            and isinstance(item.get("payload"), dict)
            and bool(item["payload"].get("config_file_count"))
            for item in resources
        )
        if has_config_payload:
            suggestions.append(
                "Build one representative company/environment configuration package and verify the final state_config/client_config values follow the documented company and release override order."
            )
        if has_database_payload:
            suggestions.append(
                "On an isolated database copy, execute the locked db_change.scr through the packaged DBChangeParser; verify version records, referenced SQL/JS/PHP/Drush resources, rerun behavior, and rollback/partial-failure handling before release."
            )
    return suggestions
