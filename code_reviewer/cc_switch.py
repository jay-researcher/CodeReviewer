from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CCSwitchProvider:
    id: str
    name: str
    app_type: str
    env: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return (
            self.env.get("ANTHROPIC_MODEL")
            or self.env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
            or self.env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or self.env.get("OPENAI_MODEL")
            or self.env.get("MODEL")
            or ""
        )

    @property
    def base_url(self) -> str:
        return (
            self.env.get("ANTHROPIC_BASE_URL")
            or self.env.get("OPENAI_BASE_URL")
            or self.env.get("DEEPSEEK_BASE_URL")
            or ""
        )

    @property
    def api_key(self) -> str:
        return (
            self.env.get("ANTHROPIC_API_KEY")
            or self.env.get("ANTHROPIC_AUTH_TOKEN")
            or self.env.get("OPENAI_API_KEY")
            or self.env.get("DEEPSEEK_API_KEY")
            or ""
        )


def load_cc_switch_provider(selector: str = "", app_type: str = "claude") -> CCSwitchProvider | None:
    root = Path(os.getenv("CC_SWITCH_HOME", str(Path.home() / ".cc-switch")))
    db_path = Path(os.getenv("CC_SWITCH_DB", str(root / "cc-switch.db")))
    if not db_path.exists():
        return None

    settings = _load_settings(root)
    current_id = settings.get(f"currentProvider{app_type.capitalize()}", "")
    selector = _normalize_provider_selector(selector, app_type)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if selector.lower() == "current" and current_id:
            row = conn.execute(
                "select * from providers where id=? and app_type=?",
                (current_id, app_type),
            ).fetchone()
        else:
            row = conn.execute(
                """
                select * from providers
                where app_type=? and (lower(id)=lower(?) or lower(name)=lower(?))
                order by is_current desc, sort_index asc
                limit 1
                """,
                (app_type, selector, selector),
            ).fetchone()
            if row is None and selector.lower() == "current":
                row = conn.execute(
                    "select * from providers where app_type=? order by is_current desc, sort_index asc limit 1",
                    (app_type,),
                ).fetchone()
        return _provider_from_row(row) if row else None
    finally:
        conn.close()


def _normalize_provider_selector(selector: str, app_type: str) -> str:
    value = (selector or "current").strip()
    normalized = value.lower().replace("_", "-").replace(" ", "-")
    if app_type == "claude" and normalized in {"claude-code-opus", "claude-opus", "opus"}:
        return "current"
    return value


def provider_summary(provider: CCSwitchProvider) -> dict[str, str]:
    return {
        "id": provider.id,
        "name": provider.name,
        "app_type": provider.app_type,
        "model": provider.model,
        "base_url": provider.base_url,
        "api_key_source": "cc-switch" if provider.api_key else "",
    }


def _provider_from_row(row: sqlite3.Row) -> CCSwitchProvider:
    settings_config = _loads(row["settings_config"])
    env = settings_config.get("env") or {}
    if isinstance(env, str):
        env = _loads(env)
    auth = settings_config.get("auth") or {}
    if isinstance(auth, str):
        auth = _loads(auth)
    if isinstance(auth, dict):
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if auth.get(key) and not env.get(key):
                env[key] = auth[key]
    return CCSwitchProvider(
        id=row["id"],
        name=row["name"],
        app_type=row["app_type"],
        env={str(key): str(value) for key, value in env.items()},
        meta=_loads(row["meta"]),
    )


def _load_settings(root: Path) -> dict[str, Any]:
    settings_path = root / "settings.json"
    if not settings_path.exists():
        return {}
    return _loads(settings_path.read_text(encoding="utf-8", errors="ignore"))


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
