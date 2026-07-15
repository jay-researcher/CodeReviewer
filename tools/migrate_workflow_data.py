from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_reviewer.config import DATA_DIR  # noqa: E402
from code_reviewer.web_app import WEB_THREADS_DIR, WEB_USERS_FILE, _sync_workflow_history, ensure_web_users  # noqa: E402
from code_reviewer.workflow_store import workflow_db_path, workflow_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate CodeReviewer users, review history and Web threads.")
    parser.add_argument("--no-backup", action="store_true", help="Skip the timestamped backup (not recommended).")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = DATA_DIR / "migration_backups" / stamp
    if not args.no_backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for source in (WEB_USERS_FILE, DATA_DIR / "review_history.jsonl"):
            if source.is_file():
                shutil.copy2(source, backup_dir / source.name)
        if WEB_THREADS_DIR.is_dir():
            shutil.copytree(WEB_THREADS_DIR, backup_dir / "web_threads")
    ensure_web_users()
    _sync_workflow_history()
    store = workflow_store()
    issues = store.list_issues(view_all=True)
    summary = {
        "database": str(workflow_db_path()),
        "backup": str(backup_dir) if not args.no_backup else "skipped",
        "issue_count": len(issues),
        "run_count": sum(int(item.get("run_count") or 0) for item in issues),
        "finding_count_latest_runs": sum(int(item.get("finding_count") or 0) for item in issues),
        "web_users_file": str(WEB_USERS_FILE),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
