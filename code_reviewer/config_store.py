from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
STORE_FORMAT = "codereviewer-web-config-overrides/v1"
BACKUP_FORMAT = "codereviewer-web-config-backup/v1"
DELETE_MARKER = "$delete"
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BACKUP_RETENTION = 50
_CONFIG_STORE_LOCK = threading.RLock()
class ConfigStoreError(RuntimeError):
    """Base exception for persistent Web configuration failures."""


class ConfigRevisionConflict(ConfigStoreError):
    """Raised when another writer has changed the effective configuration."""


class InvalidConfigOverride(ConfigStoreError):
    """Raised when an override is unsafe or is not JSON-compatible."""


def web_config_overrides_path() -> Path:
    return Path(
        os.getenv("WEB_CONFIG_OVERRIDES_FILE", str(DATA_DIR / "web_config_overrides.json"))
    ).expanduser()


def web_config_backup_dir() -> Path:
    return Path(
        os.getenv("WEB_CONFIG_BACKUP_DIR", str(DATA_DIR / "config_backups"))
    ).expanduser()


def web_config_audit_path() -> Path:
    return Path(
        os.getenv("WEB_CONFIG_AUDIT_FILE", str(DATA_DIR / "web_config_audit.jsonl"))
    ).expanduser()


def deep_merge_config(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated effective config using recursive map merge semantics.

    Lists and scalar values replace the base value. A value of
    ``{"$delete": true}`` removes that map key from the effective result.
    """
    if not isinstance(base, dict) or not isinstance(overrides, dict):
        raise InvalidConfigOverride("Base configuration and overrides must be objects.")
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if _is_delete_marker(value):
            result.pop(key, None)
            continue
        current = result.get(key)
        if isinstance(value, dict):
            result[key] = deep_merge_config(current if isinstance(current, dict) else {}, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def config_revision(base: dict[str, Any], overrides: dict[str, Any]) -> str:
    effective = deep_merge_config(base, overrides)
    encoded = _canonical_json(effective).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_web_config_overrides(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path).expanduser() if path is not None else web_config_overrides_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigStoreError(f"Web configuration override store is invalid: {target}") from exc
    if not isinstance(payload, dict):
        raise ConfigStoreError(f"Web configuration override store must contain an object: {target}")
    if payload.get("format") == STORE_FORMAT:
        overrides = payload.get("overrides")
    else:
        # Backward-compatible support for an early direct-object overlay.
        overrides = payload
    validate_config_overrides(overrides)
    return copy.deepcopy(overrides)


def effective_config_payload(
    base: dict[str, Any],
    *,
    overrides_path: Path | str | None = None,
) -> dict[str, Any]:
    return deep_merge_config(base, load_web_config_overrides(overrides_path))


def validate_config_overrides(overrides: object, *, max_bytes: int | None = None) -> None:
    if not isinstance(overrides, dict):
        raise InvalidConfigOverride("Configuration overrides must be a JSON object.")
    if _is_delete_marker(overrides):
        raise InvalidConfigOverride("The effective configuration root cannot be deleted.")
    _validate_json_value(overrides, path=(), depth=0)
    try:
        limit = int(
            max_bytes
            if max_bytes is not None
            else os.getenv("WEB_CONFIG_MAX_BYTES", str(DEFAULT_MAX_BYTES))
        )
    except (TypeError, ValueError) as exc:
        raise InvalidConfigOverride("WEB_CONFIG_MAX_BYTES must be a positive integer.") from exc
    if limit <= 0:
        raise InvalidConfigOverride("WEB_CONFIG_MAX_BYTES must be a positive integer.")
    try:
        encoded = _canonical_json(overrides).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidConfigOverride("Configuration overrides must be valid JSON data.") from exc
    if len(encoded) > limit:
        raise InvalidConfigOverride(f"Configuration overrides exceed the {limit}-byte limit.")


class EffectiveConfigStore:
    """Atomic, revisioned persistence for Web-owned configuration overrides."""

    def __init__(
        self,
        *,
        overrides_path: Path | str | None = None,
        backup_dir: Path | str | None = None,
        audit_path: Path | str | None = None,
        backup_retention: int | None = None,
    ) -> None:
        self.overrides_path = (
            Path(overrides_path).expanduser()
            if overrides_path is not None
            else web_config_overrides_path()
        )
        self.backup_dir = (
            Path(backup_dir).expanduser() if backup_dir is not None else web_config_backup_dir()
        )
        self.audit_path = (
            Path(audit_path).expanduser() if audit_path is not None else web_config_audit_path()
        )
        try:
            configured_retention = int(
                backup_retention
                if backup_retention is not None
                else os.getenv("WEB_CONFIG_BACKUP_RETENTION", str(DEFAULT_BACKUP_RETENTION))
            )
        except (TypeError, ValueError) as exc:
            raise ConfigStoreError("WEB_CONFIG_BACKUP_RETENTION must be a positive integer.") from exc
        self.backup_retention = max(1, configured_retention)

    @property
    def lock_path(self) -> Path:
        return self.overrides_path.parent / f".{self.overrides_path.name}.lock"

    def load_overrides(self) -> dict[str, Any]:
        return load_web_config_overrides(self.overrides_path)

    def effective(self, base: dict[str, Any]) -> dict[str, Any]:
        return deep_merge_config(base, self.load_overrides())

    def revision(self, base: dict[str, Any]) -> str:
        return config_revision(base, self.load_overrides())

    def save_overrides(
        self,
        base: dict[str, Any],
        overrides: dict[str, Any],
        *,
        actor: str,
        expected_revision: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        safe_base = copy.deepcopy(base)
        safe_overrides = copy.deepcopy(overrides)
        validate_config_overrides(safe_overrides)
        actor_name = _validate_actor(actor)
        expected = _validate_revision(expected_revision)
        with _CONFIG_STORE_LOCK, _exclusive_file_lock(self.lock_path):
            return self._save_locked(
                safe_base,
                safe_overrides,
                actor=actor_name,
                expected_revision=expected,
                request_id=request_id,
                action="save",
            )

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(self.backup_dir.glob("*.json"), key=lambda item: item.name, reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT:
                continue
            result.append(
                {
                    "name": path.name,
                    "created_at": str(payload.get("created_at") or ""),
                    "revision": str(payload.get("revision") or ""),
                    "actor": str(payload.get("actor") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "size": path.stat().st_size,
                }
            )
        return result

    def restore_backup(
        self,
        base: dict[str, Any],
        backup_name: str,
        *,
        actor: str,
        expected_revision: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        safe_base = copy.deepcopy(base)
        actor_name = _validate_actor(actor)
        expected = _validate_revision(expected_revision)
        name = str(backup_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name) or Path(name).name != name:
            raise ConfigStoreError("Invalid configuration backup name.")
        backup_path = (self.backup_dir / name).resolve()
        backup_root = self.backup_dir.resolve()
        if backup_path.parent != backup_root:
            raise ConfigStoreError("Invalid configuration backup path.")
        with _CONFIG_STORE_LOCK, _exclusive_file_lock(self.lock_path):
            try:
                payload = json.loads(backup_path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise ConfigStoreError("Configuration backup was not found.") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigStoreError("Configuration backup is invalid.") from exc
            if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT:
                raise ConfigStoreError("Configuration backup has an unsupported format.")
            overrides = payload.get("overrides")
            validate_config_overrides(overrides)
            return self._save_locked(
                safe_base,
                overrides,
                actor=actor_name,
                expected_revision=expected,
                request_id=request_id,
                action="restore",
                restored_from=name,
            )

    def _save_locked(
        self,
        base: dict[str, Any],
        overrides: dict[str, Any],
        *,
        actor: str,
        expected_revision: str,
        request_id: str,
        action: str,
        restored_from: str = "",
    ) -> dict[str, Any]:
        current = load_web_config_overrides(self.overrides_path)
        current_revision = config_revision(base, current)
        if current_revision != expected_revision:
            raise ConfigRevisionConflict(
                "Configuration was updated by another session. Refresh and try again."
            )

        original_exists = self.overrides_path.exists()
        original_bytes = self.overrides_path.read_bytes() if original_exists else b""
        backup_name = self._write_backup_locked(
            current,
            revision=current_revision,
            actor=actor,
            reason=f"before-{action}",
        )
        now = _utc_now()
        document = {
            "format": STORE_FORMAT,
            "updated_at": now,
            "updated_by": actor,
            "overrides": copy.deepcopy(overrides),
        }
        changed_paths = _changed_paths(current, overrides)
        new_revision = config_revision(base, overrides)
        _atomic_write_json(self.overrides_path, document)
        try:
            self._append_audit(
                {
                    "timestamp": now,
                    "event_id": uuid.uuid4().hex,
                    "request_id": _safe_text(request_id, 160),
                    "actor": actor,
                    "action": action,
                    "revision_before": current_revision,
                    "revision_after": new_revision,
                    "changed_paths": changed_paths,
                    "backup": backup_name,
                    "restored_from": restored_from,
                }
            )
        except Exception:
            if original_exists:
                _atomic_write_bytes(self.overrides_path, original_bytes)
            else:
                self.overrides_path.unlink(missing_ok=True)
                _fsync_directory(self.overrides_path.parent)
            raise
        self._prune_backups_locked()
        return {
            "overrides": copy.deepcopy(overrides),
            "effective": deep_merge_config(base, overrides),
            "revision": new_revision,
            "previous_revision": current_revision,
            "backup": backup_name,
            "changed_paths": changed_paths,
        }

    def _write_backup_locked(
        self,
        overrides: dict[str, Any],
        *,
        revision: str,
        actor: str,
        reason: str,
    ) -> str:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        name = f"web-config-{timestamp}-{revision[:12]}.json"
        payload = {
            "format": BACKUP_FORMAT,
            "created_at": _utc_now(),
            "revision": revision,
            "actor": actor,
            "reason": reason,
            "overrides": copy.deepcopy(overrides),
        }
        _atomic_write_json(self.backup_dir / name, payload)
        return name

    def _append_audit(self, event: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = _canonical_json(event) + "\n"
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _prune_backups_locked(self) -> None:
        try:
            backups = sorted(
                self.backup_dir.glob("web-config-*.json"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            for path in backups[self.backup_retention :]:
                path.unlink(missing_ok=True)
        except OSError:
            # Retention is maintenance. A committed and audited config remains valid
            # even if an old backup cannot be removed immediately.
            return


def _validate_json_value(value: Any, *, path: tuple[str, ...], depth: int) -> None:
    if depth > 64:
        raise InvalidConfigOverride("Configuration overrides exceed the maximum nesting depth.")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and "-----BEGIN" in value.upper() and "PRIVATE KEY-----" in value.upper():
            raise InvalidConfigOverride("Private key material cannot be stored in Web configuration.")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidConfigOverride("Configuration numbers must be finite.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=(*path, str(index)), depth=depth + 1)
        return
    if isinstance(value, dict):
        if DELETE_MARKER in value and not _is_delete_marker(value):
            raise InvalidConfigOverride(
                f"{_display_path(path)} uses an invalid {DELETE_MARKER} marker."
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidConfigOverride("Configuration object keys must be strings.")
            if not key or len(key) > 256:
                raise InvalidConfigOverride("Configuration object keys must contain 1–256 characters.")
            if _sensitive_key(key):
                raise InvalidConfigOverride(
                    f"Sensitive field {_display_path((*path, key))} cannot be stored in Web configuration."
                )
            _validate_json_value(item, path=(*path, key), depth=depth + 1)
        return
    raise InvalidConfigOverride(
        f"{_display_path(path)} uses unsupported value type {type(value).__name__}."
    )


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    compact = normalized.replace("_", "")
    parts = set(normalized.split("_"))
    return (
        bool(parts.intersection({"password", "passwd", "secret", "credential", "credentials"}))
        or normalized == "token"
        or normalized.endswith("_token")
        or "api_key" in normalized
        or "apikey" in compact
        or "private_key" in normalized
        or "privatekey" in compact
    )


def _is_delete_marker(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) == 1
        and value.get(DELETE_MARKER) is True
    )


def _changed_paths(before: Any, after: Any, path: tuple[str, ...] = ()) -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[str] = []
        for key in sorted(set(before) | set(after), key=str.casefold):
            if key not in before:
                result.extend(_leaf_paths(after[key], (*path, key)))
            elif key not in after:
                result.extend(_leaf_paths(before[key], (*path, key)))
            else:
                result.extend(_changed_paths(before[key], after[key], (*path, key)))
        return result
    return [] if before == after else [_display_path(path)]


def _leaf_paths(value: Any, path: tuple[str, ...]) -> list[str]:
    if isinstance(value, dict) and value:
        result: list[str] = []
        for key in sorted(value, key=str.casefold):
            result.extend(_leaf_paths(value[key], (*path, key)))
        return result
    return [_display_path(path)]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _display_path(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<root>"


def _validate_actor(actor: str) -> str:
    value = str(actor or "").strip()
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]*", value):
        raise ConfigStoreError("A valid configuration actor is required.")
    return value


def _validate_revision(revision: str) -> str:
    value = str(revision or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ConfigStoreError("A valid expected configuration revision is required.")
    return value


def _safe_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
