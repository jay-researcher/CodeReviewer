from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterator

import yaml


def repository_nodes(
    payload: dict[str, Any],
    path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        item_path = path + (str(key),)
        if value.get("repository_url") and "branch" in value:
            yield item_path, value
        yield from repository_nodes(value, item_path)


def mapping_at(payload: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    current: Any = payload
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise RuntimeError(f"Production config is missing repository path: {'.'.join(path)}")
        current = current[segment]
    if not isinstance(current, dict):
        raise RuntimeError(f"Repository path is not a mapping: {'.'.join(path)}")
    return current


def sync_repository_branches(
    production: dict[str, Any],
    template: dict[str, Any],
    *,
    allow_no_changes: bool = False,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path, source in repository_nodes(template):
        target = mapping_at(production, path)
        source_url = str(source.get("repository_url") or "").rstrip("/")
        target_url = str(target.get("repository_url") or "").rstrip("/")
        if source_url != target_url:
            raise RuntimeError(
                f"Repository URL mismatch at {'.'.join(path)}: "
                f"production={target_url!r}, template={source_url!r}"
            )
        before = target.get("branch")
        after = source.get("branch")
        if before != after:
            changes.append(
                {
                    "path": ".".join(path + ("branch",)),
                    "before": before,
                    "after": after,
                }
            )
            target["branch"] = after
    if not changes and not allow_no_changes:
        raise RuntimeError("No repository branch patterns require synchronization.")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize repository branch patterns without changing production runtime policy."
    )
    parser.add_argument("production", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--changes", type=Path)
    parser.add_argument(
        "--allow-no-changes",
        action="store_true",
        help="Treat an already synchronized production configuration as a successful no-op.",
    )
    args = parser.parse_args()

    production = yaml.safe_load(args.production.read_text(encoding="utf-8")) or {}
    template = yaml.safe_load(args.template.read_text(encoding="utf-8")) or {}
    if not isinstance(production, dict) or not isinstance(template, dict):
        raise RuntimeError("Both configuration documents must contain YAML mappings.")

    changes = sync_repository_branches(
        production,
        template,
        allow_no_changes=args.allow_no_changes,
    )
    rendered = yaml.safe_dump(production, allow_unicode=True, sort_keys=False, width=120)
    if re.search(r"(?i)\b[A-Z]:[/\\]", rendered):
        raise RuntimeError("Production config contains a Windows absolute path.")
    args.output.write_text(rendered, encoding="utf-8")
    if args.changes:
        args.changes.write_text(
            json.dumps({"count": len(changes), "changes": changes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps({"count": len(changes), "changes": changes}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
