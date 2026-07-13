from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


FALSE_VALUES = {"0", "false", "no", "off"}


def resume_enabled() -> bool:
    return os.getenv("REVIEW_RESUME", "1").strip().lower() not in FALSE_VALUES


def stable_resume_key(*parts: Any) -> str:
    text = "\n".join(_stable_text(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class ResumeTracker:
    def __init__(self, scope: str, output_dir: Path, identity: dict[str, Any]) -> None:
        self.scope = _safe_name(scope)
        self.output_dir = Path(output_dir).expanduser()
        self.identity = identity
        self.enabled = resume_enabled()
        self.path = self._state_path()
        self.state: dict[str, Any] = self._initial_state()
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.getenv("REVIEW_RESET_RESUME", "").strip().lower() in {"1", "true", "yes", "on"}:
            self.path.unlink(missing_ok=True)
        self.state = self._load()

    def is_done(self, key: str) -> bool:
        return self.enabled and self.entry(key).get("status") == "done"

    def entry(self, key: str) -> dict[str, Any]:
        items = self.state.get("items")
        if not isinstance(items, dict):
            return {}
        entry = items.get(key)
        return entry if isinstance(entry, dict) else {}

    def done_summary(self, key: str) -> dict[str, Any]:
        summary = self.entry(key).get("summary")
        if isinstance(summary, dict):
            return {**summary, "resume_status": "skipped-completed"}
        return {"resume_status": "skipped-completed"}

    def mark_started(self, key: str, item: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self._set_item(key, "in-progress", item=item or {})

    def mark_done(self, key: str, summary: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._set_item(key, "done", summary=summary)

    def mark_failed(self, key: str, error: str, item: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self._set_item(key, "failed", item=item or {}, error=error)

    def mark_interrupted(self, key: str, item: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self._set_item(key, "interrupted", item=item or {}, error="Interrupted by user or process exit.")

    def _set_item(
        self,
        key: str,
        status: str,
        *,
        item: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        items = self.state.setdefault("items", {})
        if not isinstance(items, dict):
            self.state["items"] = {}
            items = self.state["items"]
        existing = items.get(key) if isinstance(items.get(key), dict) else {}
        entry = dict(existing)
        entry.update(
            {
                "status": status,
                "updated_at": _now(),
            }
        )
        if item is not None:
            entry["item"] = _jsonable(item)
        if summary is not None:
            entry["summary"] = _jsonable(summary)
            entry["completed_at"] = _now()
        if error:
            entry["error"] = error
        else:
            entry.pop("error", None)
        if status != "done":
            entry.pop("completed_at", None)
        items[key] = entry
        self.state["updated_at"] = _now()
        self._save()

    def _state_path(self) -> Path:
        identity_hash = stable_resume_key(self.identity)
        return self.output_dir / ".code_reviewer_resume" / f"{self.scope}-{identity_hash}.json"

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": self.scope,
            "identity": _jsonable(self.identity),
            "created_at": _now(),
            "updated_at": _now(),
            "items": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._initial_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.replace(backup)
            return self._initial_state()
        if not isinstance(data, dict):
            return self._initial_state()
        data.setdefault("schema_version", 1)
        data.setdefault("scope", self.scope)
        data.setdefault("identity", _jsonable(self.identity))
        items = data.setdefault("items", {})
        if isinstance(items, dict):
            for entry in items.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "done":
                    entry.pop("error", None)
                else:
                    entry.pop("completed_at", None)
        return data

    def _save(self) -> None:
        payload = json.dumps(self.state, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)


def _stable_text(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, default=str)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.+-]+", "-", value.strip())
    return text.strip(".-") or "review"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
