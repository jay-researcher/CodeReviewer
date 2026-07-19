from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


PROTECTED_PATH_KEYS = {"local_working_copy", "template_path", "workspace_roots"}


def linux_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    mappings = (
        ("D:/TTL/vibe-coding/git-tools/git-repos/", "/var/lib/codereviewer/git-repos/"),
        ("D:/TTL/vibe-coding/CodeReviewer/", "/opt/codereviewer/current/"),
        ("D:/TTL/vibe-coding/", "/var/lib/codereviewer/git-repos/"),
        ("D:/TTL/wvplaform/", "/var/lib/codereviewer/git-repos/"),
    )
    for source, target in mappings:
        if normalized.casefold().startswith(source.casefold()):
            return target + normalized[len(source) :]
    return normalized


def normalize_template_paths(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {item_key: normalize_template_paths(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [normalize_template_paths(item, key) for item in value]
    if isinstance(value, str) and (re.match(r"^[A-Za-z]:[/\\]", value) or key in PROTECTED_PATH_KEYS):
        return linux_path(value)
    return value


def merge(production: Any, template: Any, key: str = "") -> Any:
    if key in PROTECTED_PATH_KEYS and production not in (None, "", [], {}):
        return production
    if isinstance(production, dict) and isinstance(template, dict):
        result = dict(production)
        for item_key, template_value in template.items():
            result[item_key] = (
                merge(production.get(item_key), template_value, str(item_key))
                if item_key in production
                else normalize_template_paths(template_value, str(item_key))
            )
        return result
    return normalize_template_paths(template, key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge 7.2 policy into the RHEL production config.")
    parser.add_argument("production", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    production = yaml.safe_load(args.production.read_text(encoding="utf-8")) or {}
    template = yaml.safe_load(args.template.read_text(encoding="utf-8")) or {}
    merged = merge(production, template)
    rendered = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=120)
    if re.search(r"(?i)\b[A-Z]:[/\\]", rendered):
        raise RuntimeError("Merged production config still contains a Windows absolute path.")
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
