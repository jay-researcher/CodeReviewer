from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from .config_store import effective_config_payload

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False

from .models import ProjectConfig

ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"
DATA_DIR = ROOT_DIR / "data"
CONFIG_FILE = DATA_DIR / "projects.json"
HISTORY_FILE = DATA_DIR / "review_history.jsonl"
TTL_ROOT_DIR = ROOT_DIR.parents[1] if len(ROOT_DIR.parents) > 1 else ROOT_DIR.parent
DEFAULT_REPORT_OUTPUT_BASE_DIR = TTL_ROOT_DIR / "code-review"
DEFAULT_GIT_TOOLS_CONFIG = ROOT_DIR / "config.yml"
FALSE_VALUES = {"", "0", "false", "no", "off"}
TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_CC_SWITCH_PROVIDER = "Claude code opus"

FAST_SPEED_PROFILE = {
    "LLM_CODEX_TIMEOUT_SECONDS": "300",
    "LLM_TIMEOUT_SECONDS": "180",
    "LLM_MAX_TOKENS": "6000",
    "LLM_MAX_DIFF_CHARS": "60000",
    "MAX_FINDINGS": "500",
    "REPORT_DETAIL_LEVEL": "detailed",
    "PROJECT_CONTEXT_MAX_CHARS": "80000",
    "PROJECT_CONTEXT_MAX_TREE_FILES": "500",
    "PROJECT_CONTEXT_MAX_FILE_CHARS": "12000",
    "WEB_BUILD_CONTEXT_MAX_CHARS": "20000",
    "WEB_BUILD_REFERENCE_MAX_CHARS": "10000",
    "GIT_VERSION_SOURCE_REVIEW_MAX_REPOS": "12",
    "GIT_VERSION_SOURCE_REVIEW_MAX_FILES_PER_REPO": "60",
    "GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS": "100000",
}
_SPEED_PROFILE_BASELINE: dict[str, str | None] = {}


def normalize_speed(value: str | None = None) -> str:
    text = (value or "").strip().lower().replace("_", "-")
    if text in {"", "default", "normal", "standard"}:
        return "standard"
    if text in {"fast", "priority"}:
        return "fast"
    return "standard"


def codex_service_tier_for_speed(speed: str | None = None) -> str:
    return "priority" if normalize_speed(speed) == "fast" else "default"


def git_tools_config_path() -> Path:
    return Path(os.getenv("GIT_TOOLS_CONFIG", str(DEFAULT_GIT_TOOLS_CONFIG))).expanduser()


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _parse_simple_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    lowered = text.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_simple_yaml_scalar(item) for item in _split_inline_yaml_list(inner)]
    if re.fullmatch(r"[-+]?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"[-+]?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _split_inline_yaml_list(value: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_double:
            current.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            current.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
            continue
        if char == "," and not in_single and not in_double:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(char)
    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Small YAML subset parser used when PyYAML is unavailable.

    The project configuration intentionally keeps `app:` settings to simple
    maps, scalar values, and scalar lists. This fallback keeps production
    installs from silently ignoring config.yml when PyYAML is not installed.
    """
    entries: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        entries.append((indent, line.strip()))
    if not entries:
        return {}

    def parse_block(start: int, indent: int) -> tuple[Any, int]:
        is_list = start < len(entries) and entries[start][0] == indent and entries[start][1].startswith("- ")
        if is_list:
            values: list[Any] = []
            index = start
            while index < len(entries):
                line_indent, line = entries[index]
                if line_indent < indent or (line_indent == indent and not line.startswith("- ")):
                    break
                if line_indent > indent:
                    break
                item_text = line[2:].strip()
                if not item_text:
                    if index + 1 < len(entries) and entries[index + 1][0] > indent:
                        child, index = parse_block(index + 1, entries[index + 1][0])
                        values.append(child)
                    else:
                        values.append(None)
                        index += 1
                    continue
                if ":" in item_text and not item_text.lower().startswith(("http://", "https://")):
                    key, raw_value = item_text.split(":", 1)
                    item: dict[str, Any] = {}
                    if raw_value.strip():
                        item[key.strip().strip("\"'")] = _parse_simple_yaml_scalar(raw_value)
                        index += 1
                    elif index + 1 < len(entries) and entries[index + 1][0] > indent:
                        child, index = parse_block(index + 1, entries[index + 1][0])
                        item[key.strip().strip("\"'")] = child
                    else:
                        item[key.strip().strip("\"'")] = {}
                        index += 1
                    values.append(item)
                    continue
                values.append(_parse_simple_yaml_scalar(item_text))
                index += 1
            return values, index

        values: dict[str, Any] = {}
        index = start
        while index < len(entries):
            line_indent, line = entries[index]
            if line_indent < indent or line.startswith("- "):
                break
            if line_indent > indent:
                break
            if ":" not in line:
                index += 1
                continue
            key, raw_value = line.split(":", 1)
            key = key.strip().strip("\"'")
            raw_value = raw_value.strip()
            if raw_value:
                values[key] = _parse_simple_yaml_scalar(raw_value)
                index += 1
                continue
            if index + 1 < len(entries) and entries[index + 1][0] > indent:
                child, index = parse_block(index + 1, entries[index + 1][0])
                values[key] = child
            else:
                values[key] = {}
                index += 1
        return values, index

    parsed, _ = parse_block(0, entries[0][0])
    return parsed if isinstance(parsed, dict) else {}


@lru_cache(maxsize=8)
def _base_config_payload(config_path: str) -> dict[str, Any]:
    path = Path(config_path).expanduser()
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="ignore")
    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
    parsed = _parse_simple_yaml(text)
    if parsed:
        return parsed
    try:
        return json.loads(text)
    except Exception:
        return {}


@lru_cache(maxsize=8)
def _config_payload(config_path: str) -> dict[str, Any]:
    base = _base_config_payload(config_path)
    try:
        configured_path = git_tools_config_path().resolve()
        requested_path = Path(config_path).expanduser().resolve()
    except OSError:
        configured_path = git_tools_config_path().absolute()
        requested_path = Path(config_path).expanduser().absolute()
    if requested_path != configured_path:
        return base
    return effective_config_payload(base)


def load_base_config_payload() -> dict[str, Any]:
    """Return the deployment-owned config.yml payload without Web overrides."""
    return _base_config_payload(str(git_tools_config_path()))


def load_effective_config_payload() -> dict[str, Any]:
    """Return the effective config.yml payload including Web-owned overrides."""
    return _config_payload(str(git_tools_config_path()))


def clear_config_cache() -> None:
    """Make a committed configuration override visible to subsequent reads."""
    _config_payload.cache_clear()
    _base_config_payload.cache_clear()


def load_app_config() -> dict[str, Any]:
    """Load application policy defaults from config.yml top-level `app` section."""
    payload = load_effective_config_payload()
    app = payload.get("app") if isinstance(payload, dict) else None
    return app if isinstance(app, dict) else {}


def app_config_get(path: str, default: Any = None) -> Any:
    value: Any = load_app_config()
    for part in [item for item in path.split(".") if item]:
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _override_env_name(env_key: str) -> str:
    return f"CODE_REVIEW_OVERRIDE_{env_key}" if env_key else ""


def app_config_value(path: str, env_key: str = "", default: Any = None) -> Any:
    override_key = _override_env_name(env_key)
    if override_key and override_key in os.environ:
        return os.environ.get(override_key)
    configured = app_config_get(path, None)
    if configured is not None:
        return configured
    if env_key and env_key in os.environ:
        return os.environ.get(env_key)
    return default


def set_app_runtime_override(env_key: str, value: object) -> None:
    if env_key:
        os.environ[_override_env_name(env_key)] = str(value)


def app_config_str(path: str, env_key: str = "", default: str = "") -> str:
    value = app_config_value(path, env_key, default)
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value if value is not None else default)


def app_config_int(path: str, env_key: str = "", default: int = 0) -> int:
    value = app_config_value(path, env_key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def app_config_bool(path: str, env_key: str = "", default: bool = False) -> bool:
    value = app_config_value(path, env_key, default)
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return bool(default)


def app_config_list(path: str, env_key: str = "", default: Any = None) -> list[str]:
    value = app_config_value(path, env_key, default if default is not None else [])
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,;|\n]+", str(value or "")) if item.strip()]


def apply_speed_profile(speed: str | None = None, *, force: bool = False) -> str:
    selected = normalize_speed(speed if speed is not None else app_config_str("llm.speed", "LLM_SPEED", "standard"))
    if speed is not None:
        set_app_runtime_override("LLM_SPEED", selected)
    os.environ["LLM_SPEED"] = selected
    if selected == "fast":
        for key, value in FAST_SPEED_PROFILE.items():
            if force and key not in _SPEED_PROFILE_BASELINE:
                _SPEED_PROFILE_BASELINE[key] = os.environ.get(key)
            if force or not os.getenv(key):
                os.environ[key] = value
    elif force and _SPEED_PROFILE_BASELINE:
        for key, value in _SPEED_PROFILE_BASELINE.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _SPEED_PROFILE_BASELINE.clear()
    return selected


def _load_windows_user_environment(
    names: tuple[str, ...] = ("OPENAI_API_KEY",),
    *,
    windows: bool | None = None,
) -> list[str]:
    """Refresh selected user-scoped variables for already-open Windows shells.

    Windows broadcasts environment changes to newly started applications, but an
    existing PowerShell/VS Code process keeps its old environment snapshot.  Read
    only the explicitly approved credential names and never overwrite a value
    already supplied by the process or .env file.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return []
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
    except (ImportError, OSError):
        return []
    loaded: list[str] = []
    try:
        for name in names:
            if os.environ.get(name):
                continue
            try:
                value, _value_type = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            text = str(value or "").strip()
            if text:
                os.environ[name] = text
                loaded.append(name)
    finally:
        winreg.CloseKey(key)
    return loaded


def load_environment() -> None:
    env_file = ROOT_DIR / ".env"
    loaded = load_dotenv(env_file)
    if not loaded and env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    _load_windows_user_environment()


load_environment()
apply_speed_profile(None, force=False)


def ensure_directories() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)
    report_output_dir().mkdir(parents=True, exist_ok=True)


def load_projects() -> list[ProjectConfig]:
    ensure_directories()
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return [ProjectConfig(**item) for item in data.get("projects", [])]

    projects = discover_projects_from_samples()
    save_projects(projects)
    return projects


def save_projects(projects: list[ProjectConfig]) -> None:
    ensure_directories()
    payload = {"projects": [asdict(project) for project in projects]}
    CONFIG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def get_project(project_key: str) -> ProjectConfig | None:
    normalized = project_key.lower()
    for project in load_projects():
        if project.key.lower() == normalized or project.display_name.lower() == normalized:
            return project
    return None


def discover_projects_from_samples() -> list[ProjectConfig]:
    files = list(ROOT_DIR.glob("*#git_version-*.yml")) + list(ROOT_DIR.glob("*#build-*.yml"))
    grouped: dict[str, dict[str, str]] = {}
    for file_path in files:
        prefix, kind = file_path.name.split("#", 1)
        grouped.setdefault(prefix, {})
        if kind.startswith("git_version"):
            grouped[prefix]["dev_config_file"] = file_path.name
        elif kind.startswith("build"):
            grouped[prefix]["build_config_file"] = file_path.name

    projects: list[ProjectConfig] = []
    for key, values in sorted(grouped.items()):
        config_files = [ROOT_DIR / value for value in values.values() if value]
        repositories: list[str] = []
        branches: list[str] = []
        for config_file in config_files:
            text = config_file.read_text(encoding="utf-8", errors="ignore")
            repositories.extend(re.findall(r"repository_url:\s*[\"']?([^\"'\s]+)", text))
            branches.extend(re.findall(r"branch:\s*[\"']?([^\"'\s]+)", text))

        version = _version_from_filename(values.get("build_config_file", "")) or _version_from_filename(
            values.get("dev_config_file", "")
        )
        projects.append(
            ProjectConfig(
                key=key,
                display_name=_display_name(key),
                dev_config_file=values.get("dev_config_file", ""),
                build_config_file=values.get("build_config_file", ""),
                repository_urls=sorted(set(repositories)),
                default_branch=branches[0] if branches else "",
                version=version,
            )
        )
    return projects


def gitlab_token() -> str:
    return os.getenv("GITLAB_TOKEN", "")


def jira_token() -> str:
    return os.getenv("JIRA_TOKEN", "")


def llm_config() -> dict[str, str | int]:
    speed = normalize_speed(app_config_str("llm.speed", "LLM_SPEED", "standard"))
    codex_service_tier = app_config_str("llm.codex_service_tier", "LLM_CODEX_SERVICE_TIER", "")
    return {
        "provider": app_config_str("llm.provider", "LLM_PROVIDER", "codex-cli"),
        "fallback_to_cc_switch": app_config_bool(
            "llm.fallback_to_cc_switch", "LLM_FALLBACK_TO_CC_SWITCH", False
        ),
        "model": app_config_str("llm.model", "LLM_MODEL", "local-rules"),
        "codex_model": app_config_str("llm.codex_model", "LLM_CODEX_MODEL", os.getenv("CODEX_MODEL", "gpt-5.6-sol")),
        "cc_switch_model": app_config_str("llm.cc_switch_model", "LLM_CC_SWITCH_MODEL", ""),
        "codex_timeout_seconds": app_config_int("llm.codex_timeout_seconds", "LLM_CODEX_TIMEOUT_SECONDS", 300),
        "reasoning_effort": app_config_str("llm.reasoning_effort", "LLM_REASONING_EFFORT", "high"),
        "speed": speed,
        "codex_service_tier": codex_service_tier or codex_service_tier_for_speed(speed),
        "cc_switch_provider": app_config_str("llm.cc_switch_provider", "LLM_CC_SWITCH_PROVIDER", DEFAULT_CC_SWITCH_PROVIDER),
        "network_mode": app_config_str("llm.network_mode", "LLM_NETWORK_MODE", os.getenv("NETWORK_MODE", "auto")),
        "use_cc_switch": app_config_str("llm.use_cc_switch", "LLM_USE_CC_SWITCH", ""),
        "timeout_seconds": app_config_int("llm.timeout_seconds", "LLM_TIMEOUT_SECONDS", 180),
        "max_retries": app_config_int("llm.max_retries", "LLM_MAX_RETRIES", 3),
        "dps_codex_max_retries": app_config_int("llm.dps_codex_max_retries", "DPS_CODEX_MAX_RETRIES", 2),
        "dps_codex_retry_prompt_chars": app_config_int(
            "llm.dps_codex_retry_prompt_chars",
            "DPS_CODEX_RETRY_PROMPT_CHARS",
            42000,
        ),
    }


def gitnexus_config() -> dict[str, str]:
    return {
        "storage_path": os.getenv("GITNEXUS_STORAGE_PATH", str(DATA_DIR / "gitnexus")),
        "index_file": os.getenv("GITNEXUS_INDEX_FILE", "review_index.jsonl"),
    }


def report_language() -> str:
    return app_config_str("report.language", "REPORT_LANGUAGE", "zh-CN")


SEVERITY_ORDER = {
    "Critical": 5,
    "High": 4,
    "Medium": 3,
    "Low": 2,
    "Warning": 1,
}


def normalize_severity(value: str | None, default: str = "Medium") -> str:
    text = (value or "").strip().lower()
    aliases = {
        "critical": "Critical",
        "crit": "Critical",
        "blocker": "Critical",
        "high": "High",
        "medium": "Medium",
        "med": "Medium",
        "low": "Low",
        "warning": "Warning",
        "warn": "Warning",
        "info": "Warning",
    }
    return aliases.get(text, aliases.get((default or "Medium").strip().lower(), "Medium"))


def report_min_severity() -> str:
    configured = app_config_value("report.min_severity", "REPORT_MIN_SEVERITY", None)
    if configured is None:
        configured = os.getenv("REVIEW_REPORT_MIN_SEVERITY") or "Medium"
    return normalize_severity(
        str(configured),
        default="Medium",
    )


def severity_rank(value: str | None) -> int:
    return SEVERITY_ORDER.get(normalize_severity(value, default="Warning"), 0)


def severity_meets_minimum(severity: str | None, minimum: str | None = None) -> bool:
    return severity_rank(severity) >= severity_rank(minimum or report_min_severity())


def report_output_dir() -> Path:
    value = os.getenv("REPORT_OUTPUT_DIR", "").strip()
    return Path(value).expanduser() if value else default_report_output_dir()


def default_report_output_dir(today: date | None = None) -> Path:
    work_week_end = _report_work_week_end(today or date.today())
    base = Path(os.getenv("REPORT_OUTPUT_BASE_DIR", str(DEFAULT_REPORT_OUTPUT_BASE_DIR))).expanduser()
    return base / f"e-channel-sprint{work_week_end:%Y%m%d}"


def _report_friday(today: date) -> date:
    """Backward-compatible alias for the calendar-aware work-week end."""
    return _report_work_week_end(today)


def _report_work_week_end(today: date) -> date:
    calendar = _china_work_calendar()
    monday = today - timedelta(days=today.weekday())
    week = [monday + timedelta(days=offset) for offset in range(7)]
    workdays = [value for value in week if _is_china_workday(value, calendar)]
    if workdays:
        return workdays[-1]

    # A whole Monday-Sunday period can be covered by Spring Festival. Keep
    # holiday-time reports with the most recent real delivery work week.
    candidate = monday - timedelta(days=1)
    for _ in range(31):
        if _is_china_workday(candidate, calendar):
            return candidate
        candidate -= timedelta(days=1)
    return today + timedelta(days=4 - today.weekday())


def _china_work_calendar() -> dict[str, set[date]]:
    configured = app_config_str(
        "report.china_work_calendar_file",
        "CHINA_WORK_CALENDAR_FILE",
        str(ROOT_DIR / "data" / "china-mainland-work-calendar.json"),
    )
    path = Path(configured).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"holidays": set(), "workdays": set()}
    return {
        "holidays": _calendar_dates(payload.get("holidays") if isinstance(payload, dict) else []),
        "workdays": _calendar_dates(payload.get("workdays") if isinstance(payload, dict) else []),
    }


def _calendar_dates(values: Any) -> set[date]:
    result: set[date] = set()
    for value in values if isinstance(values, list) else []:
        try:
            result.add(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return result


def _is_china_workday(value: date, calendar: dict[str, set[date]]) -> bool:
    if value in calendar.get("workdays", set()):
        return True
    if value in calendar.get("holidays", set()):
        return False
    return value.weekday() < 5


def jira_spaces() -> list[str]:
    values = app_config_list("jira.spaces", "JIRA_SPACES", "SVREQ,ECHNL,CORE")
    return values


def sprint_prefixes() -> dict[str, str]:
    configured = app_config_get("jira.sprint_prefixes")
    if isinstance(configured, dict):
        return {str(key).strip(): str(value).strip() for key, value in configured.items() if str(key).strip()}
    value = app_config_str("jira.sprint_prefixes", "SPRINT_PREFIXES", "SVREQ=SVREQ Sprint,ECHNL=e-Channel Sprint,CORE=Core Sprint")
    result: dict[str, str] = {}
    for item in value.split(","):
        if "=" in item:
            key, prefix = item.split("=", 1)
            result[key.strip()] = prefix.strip()
    return result


def _display_name(key: str) -> str:
    return {
        "dps11": "DPS / DrupalServices",
        "itrade-client": "iTrade Client",
        "wvadmin": "WVAdmin",
    }.get(key, key)


def _version_from_filename(filename: str) -> str:
    match = re.search(r"-v(.+)\.yml$", filename)
    return match.group(1) if match else ""
