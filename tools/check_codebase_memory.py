from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_reviewer.config import load_environment
from code_reviewer.repository_sync import _codebase_memory_executable, _run_codebase_memory_tool


def main() -> int:
    load_environment()
    completed = _run_codebase_memory_tool(
        _codebase_memory_executable(),
        "list_projects",
        {},
        timeout=30,
    )
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout or "Codebase Memory check failed").strip(), file=sys.stderr)
        return completed.returncode or 1
    try:
        payload = json.loads(completed.stdout or "{}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(completed.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
