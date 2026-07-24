from __future__ import annotations

import argparse
from copy import deepcopy
import re
from pathlib import Path
from typing import Any

import yaml


SCOPE_KEYS = ("application", "release_line", "release_lines", "responsible")
APP_POLICY_KEYS = ("review_domains",)
LLM_POLICY_KEYS = (
    "codex_activity_timeout_seconds",
    "codex_absolute_timeout_seconds",
    "codex_progress_heartbeat_seconds",
    "dps_codex_max_retries",
    "dps_codex_retry_prompt_chars",
)
REVIEW_POLICY_PATHS = (
    ("app", "review", "discovery", "require_strong_history_reference"),
    ("app", "review", "release_gate", "branch_prefixes", "git_version"),
)
REQUIRED_SCOPE_PATHS = (
    ("dps9-repository",),
    ("dps11-repository",),
    ("build-repository", "itrade-client"),
    ("build-repository", "services-terminal"),
    ("build-repository", "wvadmin"),
    ("build-repository", "dps"),
    ("itrade-client", "itrade-client-7.5.0"),
    ("itrade-client", "itrade-client"),
    ("itrade-client", "services-terminal"),
    ("wvadmin-repository",),
)


def _mapping_at(payload: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    current: Any = payload
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise RuntimeError(f"Production config is missing required scope path: {'.'.join(path)}")
        current = current[segment]
    if not isinstance(current, dict):
        raise RuntimeError(f"Scope path is not a mapping: {'.'.join(path)}")
    return current


def _value_at(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise RuntimeError(f"Template is missing required review policy: {'.'.join(path)}")
        current = current[segment]
    return current


def _set_value_at(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current: dict[str, Any] = payload
    for segment in path[:-1]:
        child = current.setdefault(segment, {})
        if not isinstance(child, dict):
            raise RuntimeError(f"Production review policy path is not a mapping: {'.'.join(path[:-1])}")
        current = child
    current[path[-1]] = deepcopy(value)


def merge_release_scopes(production: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    # Deliberately update only portable review-boundary and review-policy fields.
    # Endpoints, credentials, auto-fetch settings and Linux paths remain
    # production-owned.
    for path in REQUIRED_SCOPE_PATHS:
        target = _mapping_at(production, path)
        source = _mapping_at(template, path)
        copied = False
        for key in SCOPE_KEYS:
            if key in source:
                target[key] = source[key]
                copied = True
        if not copied:
            raise RuntimeError(f"Template has no release scope at: {'.'.join(path)}")
    production_app = _mapping_at(production, ("app",))
    template_app = _mapping_at(template, ("app",))
    for key in APP_POLICY_KEYS:
        if key not in template_app:
            raise RuntimeError(f"Template is missing required app policy: app.{key}")
        production_app[key] = template_app[key]
    production_llm = _mapping_at(production, ("app", "llm"))
    template_llm = _mapping_at(template, ("app", "llm"))
    for key in LLM_POLICY_KEYS:
        if key not in template_llm:
            raise RuntimeError(f"Template is missing required LLM policy: app.llm.{key}")
        production_llm[key] = template_llm[key]
    for path in REVIEW_POLICY_PATHS:
        _set_value_at(production, path, _value_at(template, path))
    return production


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge portable application scope and review policy fields into production config."
    )
    parser.add_argument("production", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    production = yaml.safe_load(args.production.read_text(encoding="utf-8")) or {}
    template = yaml.safe_load(args.template.read_text(encoding="utf-8")) or {}
    if not isinstance(production, dict) or not isinstance(template, dict):
        raise RuntimeError("Both configuration documents must contain YAML mappings.")
    merged = merge_release_scopes(production, template)
    rendered = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=120)
    if re.search(r"(?i)\b[A-Z]:[/\\]", rendered):
        raise RuntimeError("Production config contains a Windows absolute path.")
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
