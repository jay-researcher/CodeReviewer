from __future__ import annotations

import importlib
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import app_config_bool, app_config_int, app_config_str
from .jira_prd_context import attach_jira_prd_context
from .models import ReviewInput


@dataclass(frozen=True, slots=True)
class KnowledgeProviderPolicy:
    authoritative_issue_provider: str
    primary_context_provider: str
    jira_write_provider: str
    rovo_read_only: bool
    local_jira_prd_enabled: bool


def knowledge_provider_policy() -> KnowledgeProviderPolicy:
    """Return the enforceable provider boundary used by code review.

    Jira REST remains authoritative. Rovo may enrich context, but its content
    cannot overwrite Jira fields or perform Jira writes.
    """
    return KnowledgeProviderPolicy(
        authoritative_issue_provider=app_config_str(
            "knowledge.authoritative_issue_provider",
            "AUTHORITATIVE_ISSUE_PROVIDER",
            "jira_rest",
        ).strip().casefold(),
        primary_context_provider=app_config_str(
            "knowledge.primary_context_provider",
            "ROVO_KNOWLEDGE_PROVIDER",
            "local_jira_prd",
        ).strip().casefold(),
        jira_write_provider=app_config_str(
            "knowledge.jira_write_provider",
            "JIRA_WRITE_PROVIDER",
            "jira_rest",
        ).strip().casefold(),
        rovo_read_only=app_config_bool(
            "knowledge.rovo.read_only",
            "ROVO_READ_ONLY",
            True,
        ),
        local_jira_prd_enabled=app_config_bool(
            "knowledge.local_jira_prd_enabled",
            "LOCAL_JIRA_PRD_ENABLED",
            True,
        ),
    )


def attach_knowledge_context(review_input: ReviewInput) -> None:
    policy = knowledge_provider_policy()
    _validate_policy(policy)
    review_input.metadata["knowledge_provider_policy"] = asdict(policy)

    if policy.local_jira_prd_enabled:
        attach_jira_prd_context(review_input)
    if policy.primary_context_provider == "rovo_mcp":
        _attach_rovo_context(review_input)


def _validate_policy(policy: KnowledgeProviderPolicy) -> None:
    if policy.authoritative_issue_provider != "jira_rest":
        raise RuntimeError("knowledge.authoritative_issue_provider must remain jira_rest")
    if policy.jira_write_provider != "jira_rest":
        raise RuntimeError("knowledge.jira_write_provider must remain jira_rest")
    if policy.primary_context_provider == "rovo_mcp" and not policy.rovo_read_only:
        raise RuntimeError("Rovo integration is retrieval-only; knowledge.rovo.read_only must be true")


def _attach_rovo_context(review_input: ReviewInput) -> None:
    if not app_config_bool("knowledge.rovo.enabled", "ROVO_KNOWLEDGE_ENABLED", True):
        review_input.metadata["rovo_knowledge_status"] = "disabled"
        return

    auth_mode = app_config_str(
        "knowledge.rovo.auth_mode",
        "ROVO_MCP_AUTH_MODE",
        "basic",
    ).strip().casefold()
    token = os.getenv("ATLASSIAN_ROVO_MCP_TOKEN", "")
    if auth_mode == "basic":
        token = token or os.getenv("JIRA_TOKEN", "")
    if not token:
        review_input.metadata["rovo_knowledge_status"] = "credential-missing"
        return

    adapter_home = Path(
        app_config_str(
            "knowledge.rovo.adapter_home",
            "JIRA_REVIEWER_HOME",
            str(Path(__file__).resolve().parents[2] / "JiraReviewer"),
        )
    ).expanduser()
    if not (adapter_home / "jira_provider.py").is_file():
        review_input.metadata["rovo_knowledge_status"] = (
            f"adapter-unavailable: {adapter_home}"
        )
        return

    query = _rovo_query(review_input)
    if not query:
        review_input.metadata["rovo_knowledge_status"] = "query-empty"
        return

    config = {
        "JIRA_WRITE_PROVIDER": "jira_rest",
        "ROVO_KNOWLEDGE_PROVIDER": "rovo_mcp",
        "ROVO_KNOWLEDGE_ENABLED": "true",
        "ROVO_MCP_AUTH_MODE": auth_mode,
        "ATLASSIAN_ROVO_MCP_TOKEN": token,
        "ATLASSIAN_ROVO_MCP_ENDPOINT": app_config_str(
            "knowledge.rovo.endpoint",
            "ATLASSIAN_ROVO_MCP_ENDPOINT",
            "https://mcp.atlassian.com/v1/mcp",
        ),
        "ATLASSIAN_ROVO_MCP_TIMEOUT_SECONDS": str(
            app_config_int(
                "knowledge.rovo.timeout_seconds",
                "ATLASSIAN_ROVO_MCP_TIMEOUT_SECONDS",
                60,
            )
        ),
        "ATLASSIAN_CLOUD_ID": os.getenv("ATLASSIAN_CLOUD_ID", ""),
        "JIRA_USERNAME": os.getenv("JIRA_USERNAME", ""),
        "JIRA_TOKEN": os.getenv("JIRA_TOKEN", ""),
    }
    try:
        provider = _load_jira_reviewer_provider(adapter_home)
        results, status = provider.rovo_search_knowledge(
            config,
            query,
            limit=app_config_int(
                "knowledge.rovo.max_results",
                "ROVO_KNOWLEDGE_MAX_RESULTS",
                8,
            ),
        )
        references = [
            {
                "source": str(getattr(item, "source", "")),
                "title": str(getattr(item, "title", "")),
                "url": str(getattr(item, "url", "")),
                "excerpt": str(getattr(item, "excerpt", "")),
                "key": str(getattr(item, "key", "")),
            }
            for item in results
        ]
        review_input.metadata["rovo_knowledge_status"] = str(
            getattr(status, "status", "ok")
        )
        review_input.metadata["rovo_knowledge_references"] = references
        context = _format_rovo_context(references)
        if context:
            review_input.metadata["rovo_knowledge_context"] = context
            local = str(review_input.metadata.get("jira_prd_context") or "").strip()
            review_input.metadata["jira_prd_context"] = (
                f"{context}\n\n{local}".strip()
            )
    except Exception as exc:
        # Knowledge enrichment cannot block a code review. Jira REST issue data
        # and deterministic GitLab evidence remain available.
        review_input.metadata["rovo_knowledge_status"] = (
            f"rovo-unavailable: {str(exc).splitlines()[0][:300]}"
        )


def _load_jira_reviewer_provider(adapter_home: Path) -> Any:
    path = str(adapter_home.resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("jira_provider")


def _rovo_query(review_input: ReviewInput) -> str:
    jira_description = str(review_input.metadata.get("jira_description") or "")
    return "\n".join(
        item
        for item in (
            review_input.jira_key,
            review_input.title,
            jira_description[:2000],
            review_input.source_branch,
            review_input.target_branch,
        )
        if str(item).strip()
    )[:4000]


def _format_rovo_context(references: list[dict[str, str]]) -> str:
    if not references:
        return ""
    maximum = app_config_int(
        "knowledge.rovo.context_max_chars",
        "ROVO_KNOWLEDGE_CONTEXT_MAX_CHARS",
        12000,
    )
    lines = [
        "Rovo retrieval-only candidate context.",
        "Jira issue fields and status remain authoritative from Jira REST; do not overwrite them from these summaries.",
    ]
    for item in references:
        lines.append(
            f"- [{item.get('source') or 'Rovo'}] "
            f"{item.get('key') or item.get('title') or '-'}"
            f" | {item.get('url') or '-'}"
            f" | {item.get('excerpt') or '-'}"
        )
    return "\n".join(lines)[: max(maximum, 0)]
