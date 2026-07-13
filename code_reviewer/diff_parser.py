from __future__ import annotations

import re

from .models import ChangedFile


FILE_HEADER_RE = re.compile(r"^\+\+\+\s+b/(.+)$", re.MULTILINE)


def parse_unified_diff(raw_diff: str) -> list[ChangedFile]:
    if not raw_diff.strip():
        return []

    sections: list[tuple[str, list[str]]] = []
    current_path = ""
    current_lines: list[str] = []

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            if current_path or current_lines:
                sections.append((current_path, current_lines))
            current_path = _path_from_diff_git(line)
            current_lines = [line]
            continue
        if line.startswith("+++ b/"):
            current_path = line.removeprefix("+++ b/")
        current_lines.append(line)

    if current_path or current_lines:
        sections.append((current_path, current_lines))

    files: list[ChangedFile] = []
    for path, lines in sections:
        diff = "\n".join(lines)
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        files.append(ChangedFile(path=path or "unknown", additions=additions, deletions=deletions, diff=diff))
    return files


def added_lines_with_numbers(file_diff: str) -> list[tuple[int | None, str]]:
    results: list[tuple[int | None, str]] = []
    new_line_no: int | None = None
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line_no = int(match.group(1)) if match else None
            continue
        if line.startswith("+") and not line.startswith("+++"):
            results.append((new_line_no, line[1:]))
        if new_line_no is not None and not line.startswith("-"):
            new_line_no += 1
    return results


def _path_from_diff_git(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4 and parts[3].startswith("b/"):
        return parts[3][2:]
    return ""
