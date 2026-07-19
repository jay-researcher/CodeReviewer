from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cc_switch import load_cc_switch_provider
from .config import (
    DEFAULT_CC_SWITCH_PROVIDER,
    ROOT_DIR,
    app_config_bool,
    app_config_int,
    app_config_str,
    llm_config,
    report_language,
    report_min_severity,
)
from .models import Finding, ReviewInput
from .resource_optimizer import optimize_prompt_diff


SEVERITIES = {"Critical", "High", "Medium", "Low", "Warning"}


@dataclass(slots=True)
class LLMReviewOutput:
    provider: str
    model: str
    findings: list[Finding]
    notes: list[str]
    reasoning_effort: str = ""
    speed: str = "standard"


def run_llm_review(review_input: ReviewInput) -> LLMReviewOutput:
    config = llm_config()
    provider = _normalize_provider(str(config["provider"]))
    dps_codex_required = _dps_review_requires_codex(review_input)
    if dps_codex_required:
        provider = "codex-cli"
    configured_llm_model = _configured_llm_model(review_input)
    if configured_llm_model:
        config["model"] = configured_llm_model
        config["codex_model"] = configured_llm_model
    model = _model_for_provider(provider, str(config["model"]), config)
    timeout = int(config["timeout_seconds"])
    codex_timeout = int(config.get("codex_timeout_seconds") or timeout)
    max_retries = _retry_count(config.get("max_retries"))
    if dps_codex_required:
        max_retries = _retry_count(config.get("dps_codex_max_retries") or 1)
    reasoning_effort = str(config.get("reasoning_effort") or "high")
    speed = str(config.get("speed") or "standard")
    _assert_llm_runtime_level(reasoning_effort, speed)

    if provider in {"", "none", "rule-based", "local-rules"}:
        if _llm_require_success():
            raise RuntimeError("LLM provider is disabled, but LLM_REQUIRE_SUCCESS=1.")
        return LLMReviewOutput(
            provider="rule-based",
            model="local-rules",
            findings=[],
            notes=["LLM provider is disabled; rule-based review was used."],
            reasoning_effort=reasoning_effort,
            speed=speed,
        )

    prompt = _review_prompt(review_input)
    if provider == "auto":
        return _run_auto_review(prompt, timeout, config, reasoning_effort, speed, max_retries)

    return _run_single_review(
        provider,
        model,
        prompt,
        codex_timeout if provider == "codex-cli" else timeout,
        reasoning_effort,
        speed,
        str(config.get("codex_service_tier") or ""),
        max_retries=max_retries,
        require_success=True if dps_codex_required else None,
        no_fallback_reason="DPS GitLab projects require codex-cli review." if dps_codex_required else "",
        cc_switch_selector=str(config.get("cc_switch_provider") or DEFAULT_CC_SWITCH_PROVIDER),
    )


def _configured_llm_model(review_input: ReviewInput) -> str:
    value = str(review_input.metadata.get("llm_model_config") or "").strip()
    if value:
        return value
    values = review_input.metadata.get("llm_model_configs") or []
    if isinstance(values, list):
        for item in values:
            text = str(item or "").strip()
            if text:
                return text
    return ""


def _llm_require_success() -> bool:
    return app_config_bool("llm.require_success", "LLM_REQUIRE_SUCCESS", True)


def _llm_require_structured_output() -> bool:
    return app_config_bool("llm.require_structured_output", "LLM_REQUIRE_STRUCTURED_OUTPUT", True)


def _retry_count(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 3


def _assert_llm_runtime_level(reasoning_effort: str, speed: str) -> None:
    required_effort = app_config_str("llm.required_reasoning_effort", "LLM_REQUIRED_REASONING_EFFORT", reasoning_effort).strip()
    required_speed = app_config_str("llm.required_speed", "LLM_REQUIRED_SPEED", speed).strip()
    if _reasoning_rank(reasoning_effort) < _reasoning_rank(required_effort):
        raise RuntimeError(
            f"LLM reasoning effort downgrade detected: effective '{reasoning_effort}' "
            f"is lower than required '{required_effort}'."
        )
    if _speed_rank(speed) < _speed_rank(required_speed):
        raise RuntimeError(
            f"LLM speed downgrade detected: effective '{speed}' is lower than required '{required_speed}'."
        )


def _reasoning_rank(value: str) -> int:
    normalized = (value or "").strip().lower().replace("_", "-").replace(" ", "-")
    ranks = {
        "none": 0,
        "minimal": 1,
        "low": 2,
        "medium": 3,
        "standard": 3,
        "high": 4,
        "extra-high": 5,
        "xhigh": 5,
        "max": 5,
    }
    return ranks.get(normalized, ranks.get(normalized.replace("--", "-"), 3))


def _speed_rank(value: str) -> int:
    normalized = (value or "").strip().lower().replace("_", "-").replace(" ", "-")
    ranks = {
        "standard": 1,
        "default": 1,
        "normal": 1,
        "fast": 2,
        "priority": 2,
    }
    return ranks.get(normalized, 1)


def _run_auto_review(
    prompt: str,
    timeout: int,
    config: dict[str, Any],
    reasoning_effort: str,
    speed: str,
    max_retries: int,
) -> LLMReviewOutput:
    notes: list[str] = []
    codex_timeout = int(config.get("codex_timeout_seconds") or timeout)
    codex_service_tier = str(config.get("codex_service_tier") or "")
    network_mode = str(config.get("network_mode") or "auto").strip().lower()
    attempts: list[tuple[str, str, str, int]] = []
    if network_mode != "non-vpn":
        attempts.append(("codex-cli", str(config.get("codex_model") or "gpt-5.6-sol"), "", codex_timeout))
    else:
        notes.append("LLM_NETWORK_MODE=non-vpn; skipped Codex by explicit network-mode override.")
    if bool(config.get("fallback_to_cc_switch")):
        attempts.append(
            (
                "cc-switch",
                str(config.get("cc_switch_model") or config.get("model") or config.get("codex_model") or ""),
                str(config.get("cc_switch_provider") or DEFAULT_CC_SWITCH_PROVIDER),
                timeout,
            )
        )
    else:
        notes.append("Automatic CC Switch fallback is disabled; CPA/Codex is the only automatic review path.")
    for provider_index, (provider, model, selector, provider_timeout) in enumerate(attempts):
        for attempt in range(1, max_retries + 1):
            try:
                if provider == "cc-switch":
                    text, provider_name, selected_model = _call_cc_switch_api(prompt, model, provider_timeout, selector=selector)
                    output_provider = f"cc-switch:{provider_name}"
                    output_model = selected_model
                else:
                    text = _call_codex_cli(
                        prompt,
                        model,
                        provider_timeout,
                        reasoning_effort=reasoning_effort,
                        speed=speed,
                        service_tier=codex_service_tier,
                    )
                    output_provider = provider
                    output_model = model
                findings, provider_notes = _parse_llm_response_strict(text)
                if attempt > 1:
                    provider_notes = [f"{output_provider} succeeded on attempt {attempt}/{max_retries}.", *provider_notes]
                return LLMReviewOutput(
                    provider=output_provider,
                    model=output_model,
                    findings=findings,
                    notes=[*notes, *(provider_notes or [f"{output_provider} review completed."])],
                    reasoning_effort=reasoning_effort,
                    speed=speed,
                )
            except Exception as exc:
                brief = _brief_error(exc)
                if attempt < max_retries:
                    notes.append(f"{provider} attempt {attempt}/{max_retries} failed, retrying: {brief}")
                    continue
                if provider_index + 1 < len(attempts):
                    notes.append(f"{provider} failed after {max_retries} attempt(s), falling back: {brief}")
                else:
                    notes.append(f"{provider} failed after {max_retries} attempt(s); no automatic fallback: {brief}")

    message = "LLM auto review failed: " + " | ".join(notes or ["no provider attempts ran"])
    if _llm_require_success():
        raise RuntimeError(message)
    return LLMReviewOutput(
        provider="auto",
        model=str(config.get("model") or config.get("codex_model") or "gpt-5.6-sol"),
        findings=[],
        notes=[message],
        reasoning_effort=reasoning_effort,
        speed=speed,
    )


def _run_single_review(
    provider: str,
    model: str,
    prompt: str,
    timeout: int,
    reasoning_effort: str,
    speed: str,
    codex_service_tier: str,
    max_retries: int,
    require_success: bool | None = None,
    no_fallback_reason: str = "",
    cc_switch_selector: str = "",
) -> LLMReviewOutput:
    strict_success = _llm_require_success() if require_success is None else require_success
    if provider not in {"codex-cli", "deepseek", "claude", "cc-switch"}:
        if strict_success:
            raise RuntimeError(f"Unsupported LLM provider '{provider}'.")
        return LLMReviewOutput(
            provider=provider,
            model=model,
            findings=[],
            notes=[f"Unsupported LLM provider '{provider}'. Rule-based review was used."],
            reasoning_effort=reasoning_effort,
            speed=speed,
        )

    last_error = ""
    for attempt in range(1, max_retries + 1):
        output_provider = provider
        output_model = model
        try:
            if provider == "codex-cli":
                text = _call_codex_cli(
                    prompt,
                    model,
                    timeout,
                    reasoning_effort=reasoning_effort,
                    speed=speed,
                    service_tier=codex_service_tier,
                )
            elif provider == "deepseek":
                text = _call_deepseek_api(prompt, model, timeout)
            elif provider == "claude":
                text = _call_claude_api(prompt, model, timeout)
            else:
                text, provider_name, output_model = _call_cc_switch_api(prompt, model, timeout, selector=cc_switch_selector)
                output_provider = f"cc-switch:{provider_name}"
            findings, notes = _parse_llm_response_strict(text)
            if no_fallback_reason:
                notes = [no_fallback_reason, *notes]
            if attempt > 1:
                notes = [f"{output_provider} succeeded on attempt {attempt}/{max_retries}.", *notes]
            return LLMReviewOutput(
                provider=output_provider,
                model=output_model,
                findings=findings,
                notes=notes or ["LLM review completed."],
                reasoning_effort=reasoning_effort,
                speed=speed,
            )
        except Exception as exc:
            last_error = _brief_error(exc)
            if attempt < max_retries:
                continue

    if strict_success:
        hint = ""
        if provider == "codex-cli":
            hint = (
                f" {no_fallback_reason}"
                if no_fallback_reason
                else " Automatic provider fallback is disabled; verify CPA/Codex connectivity and retry."
            )
        raise RuntimeError(f"LLM provider '{provider}' failed after {max_retries} attempt(s): {last_error}{hint}")
    return LLMReviewOutput(
        provider=provider,
        model=model,
        findings=[],
        notes=[f"LLM review skipped because provider failed after {max_retries} attempt(s): {last_error}"],
        reasoning_effort=reasoning_effort,
        speed=speed,
    )


def _dps_review_requires_codex(review_input: ReviewInput) -> bool:
    if not app_config_bool("llm.dps_require_codex", "DPS_REVIEW_REQUIRE_CODEX", True):
        return False
    return _looks_like_dps_project(review_input)


def _looks_like_dps_project(review_input: ReviewInput) -> bool:
    values: list[str] = [
        review_input.project,
        str(review_input.metadata.get("gitlab_project_path") or ""),
        str(review_input.metadata.get("git_tools_project_path") or ""),
        str(review_input.metadata.get("git_tools_group") or ""),
        str(review_input.metadata.get("git_tools_module") or ""),
    ]
    related = review_input.metadata.get("related_merge_requests") or []
    if isinstance(related, list):
        for item in related:
            if not isinstance(item, dict):
                continue
            values.extend(
                [
                    str(item.get("project") or ""),
                    str(item.get("project_path") or ""),
                    str(item.get("git_tools_group") or ""),
                    str(item.get("git_tools_module") or ""),
                ]
            )
    normalized_values = [value.lower().replace("\\", "/").strip() for value in values if value.strip()]
    for value in normalized_values:
        if "drupalservices" in value or any(
            token in value for token in ("dps-repository", "dps9-repository", "dps11-repository")
        ):
            return True
        if re.search(r"(?:^|/)(?:dps|dps9|dps11)(?:/|$)", value):
            return True
    return False


def llm_metadata(output: LLMReviewOutput) -> dict[str, Any]:
    return {
        "llm_provider": output.provider,
        "llm_model": output.model,
        "llm_reasoning_effort": output.reasoning_effort,
        "llm_speed": output.speed,
        "llm_notes": output.notes,
    }


def _call_codex_cli(
    prompt: str,
    model: str,
    timeout: int,
    reasoning_effort: str = "high",
    speed: str = "standard",
    service_tier: str = "",
) -> str:
    codex = _resolve_codex_cli()
    if not codex:
        raise RuntimeError("codex CLI was not found. Set CODEX_CLI_PATH to the full path of codex.exe.")
    process_env = os.environ.copy()
    cc_provider = load_cc_switch_provider(os.getenv("LLM_CC_SWITCH_CODEX_PROVIDER", "current"), app_type="codex")
    if cc_provider:
        for key, value in cc_provider.env.items():
            if value and key not in process_env:
                process_env[key] = value

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json", delete=False) as handle:
        schema_path = Path(handle.name)
        json.dump(_review_output_schema(), handle)
    command = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-c",
        f'model_reasoning_effort="{_codex_reasoning_effort(reasoning_effort)}"',
        "--sandbox",
        "read-only",
        "--cd",
        str(ROOT_DIR),
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
    ]
    tier = service_tier or ("priority" if speed == "fast" else "default")
    if tier:
        command.extend(["-c", f'service_tier="{tier}"'])
    disabled_feature = os.getenv("LLM_CODEX_DISABLE_FEATURE", "shell_snapshot").strip()
    if disabled_feature:
        command[4:4] = ["--disable", disabled_feature]
    if app_config_bool("llm.codex_ignore_user_config", "LLM_CODEX_IGNORE_USER_CONFIG", True):
        command.extend(["--ignore-user-config"])
        for feature in ("plugins", "remote_plugin", "apps"):
            command.extend(["--disable", feature])
    if app_config_bool("llm.codex_force_http", "LLM_CODEX_FORCE_HTTP", True):
        provider_id = "codereviewer_http"
        base_url = app_config_str(
            "llm.codex_http_base_url",
            "LLM_CODEX_HTTP_BASE_URL",
            "https://chatgpt.com/backend-api/codex",
        ).rstrip("/")
        api_key_env = app_config_str(
            "llm.codex_http_api_key_env",
            "LLM_CODEX_HTTP_API_KEY_ENV",
            "",
        ).strip()
        if api_key_env and not process_env.get(api_key_env):
            hint = (
                " Reopen PowerShell/VS Code after changing a Windows User environment variable, "
                f"or import it into the current session before retrying."
                if os.name == "nt"
                else ""
            )
            raise RuntimeError(f"Codex HTTP provider requires environment variable {api_key_env}.{hint}")
        stream_retries = app_config_int("llm.codex_stream_max_retries", "LLM_CODEX_STREAM_MAX_RETRIES", 2)
        provider_config = [
            ("model_provider", f'"{provider_id}"'),
            (f"model_providers.{provider_id}.name", '"CodeReviewer HTTP"'),
            (f"model_providers.{provider_id}.base_url", f'"{base_url}"'),
            (f"model_providers.{provider_id}.wire_api", '"responses"'),
        ]
        if api_key_env:
            provider_config.append((f"model_providers.{provider_id}.env_key", f'"{api_key_env}"'))
        provider_config.extend(
            [
                (f"model_providers.{provider_id}.requires_openai_auth", "false" if api_key_env else "true"),
                (f"model_providers.{provider_id}.supports_websockets", "false"),
                (f"model_providers.{provider_id}.stream_max_retries", str(max(0, stream_retries))),
            ]
        )
        for key, value in provider_config:
            command.extend(["-c", f"{key}={value}"])
    if model and model != "local-rules":
        command.extend(["--model", model])
    command.append("-")

    try:
        # Pass bytes explicitly. On Windows, subprocess text mode can otherwise
        # fall back to the active ANSI code page in its writer thread and leave
        # Codex waiting until timeout when a prompt contains BOM/CJK characters.
        completed = subprocess.run(
            command,
            input=prompt.encode("utf-8"),
            capture_output=True,
            text=False,
            env=process_env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            _decode_subprocess_output(item)
            for item in (
                getattr(exc, "stderr", ""),
                getattr(exc, "stdout", ""),
            )
            if item
        )
        detail = f": {_brief_error(output)}" if output else ""
        raise RuntimeError(f"timed out after {timeout} seconds{detail}") from exc
    finally:
        output = output_path.read_text(encoding="utf-8", errors="ignore") if output_path.exists() else ""
        output_path.unlink(missing_ok=True)
        schema_path.unlink(missing_ok=True)
    stdout = _decode_subprocess_output(completed.stdout)
    stderr = _decode_subprocess_output(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(_brief_error((stderr or stdout).strip() or "codex CLI failed"))
    return output or stdout


def _decode_subprocess_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _codex_reasoning_effort(value: str) -> str:
    normalized = (value or "high").strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "": "high",
        "standard": "medium",
        "extra-high": "xhigh",
        "extra_high": "xhigh",
        "x-high": "xhigh",
        "xhigh": "xhigh",
        "max": "xhigh",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        return "high"
    return normalized


def _resolve_codex_cli() -> str:
    configured = os.getenv("CODEX_CLI_PATH", "").strip().strip("\"'")
    if configured and Path(configured).exists():
        return configured

    from_path = shutil.which("codex") or shutil.which("codex.exe")
    if from_path:
        return from_path

    candidates: list[Path] = []
    user_profile = os.getenv("USERPROFILE", "")
    if user_profile:
        extensions_dir = Path(user_profile) / ".vscode" / "extensions"
        if extensions_dir.exists():
            candidates.extend(extensions_dir.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"))

    appdata = os.getenv("APPDATA", "")
    if appdata:
        npm_codex = Path(appdata) / "npm" / "codex.cmd"
        if npm_codex.exists():
            candidates.append(npm_codex)

    existing = [item for item in candidates if item.exists()]
    if not existing:
        return ""
    newest = max(existing, key=lambda item: item.stat().st_mtime)
    return str(newest)


def _call_deepseek_api(prompt: str, model: str, timeout: int) -> str:
    cc_selector = os.getenv("LLM_CC_SWITCH_PROVIDER", "").strip()
    if cc_selector:
        text, _, _ = _call_cc_switch_api(prompt, model, timeout, selector=cc_selector)
        return text
    token = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_TOKEN")
    if not token:
        raise RuntimeError("set DEEPSEEK_API_KEY or DEEPSEEK_TOKEN.")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a senior code reviewer. Return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    data = _post_json(
        f"{base_url}/chat/completions",
        payload,
        timeout,
        {"Authorization": f"Bearer {token}"},
    )
    return data["choices"][0]["message"]["content"]


def _call_claude_api(prompt: str, model: str, timeout: int) -> str:
    cc_selector = os.getenv("LLM_CC_SWITCH_PROVIDER", "").strip()
    if os.getenv("LLM_USE_CC_SWITCH", "").lower() in {"1", "true", "yes"} or cc_selector:
        text, _, _ = _call_cc_switch_api(
            prompt,
            model,
            timeout,
            selector=cc_selector
            or app_config_str("llm.cc_switch_provider", "LLM_CC_SWITCH_PROVIDER", DEFAULT_CC_SWITCH_PROVIDER),
        )
        return text
    token = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_CODE_OPUS_API_TOKEN")
    if not token:
        raise RuntimeError("set CLAUDE_API_KEY, ANTHROPIC_API_KEY, or CLAUDE_CODE_OPUS_API_TOKEN.")
    base_url = os.getenv("CLAUDE_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    payload = {
        "model": model,
        "max_tokens": app_config_int("llm.max_tokens", "LLM_MAX_TOKENS", 3000),
        "temperature": 0,
        "system": "You are a senior code reviewer. Return strict JSON only.",
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post_json(
        f"{base_url.rstrip('/')}/v1/messages",
        payload,
        timeout,
        {
            "x-api-key": token,
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
        },
    )
    content = data.get("content", [])
    return "\n".join(item.get("text", "") for item in content if item.get("type") == "text")


def _call_cc_switch_api(prompt: str, model: str, timeout: int, selector: str = "") -> tuple[str, str, str]:
    provider = load_cc_switch_provider(
        selector or app_config_str("llm.cc_switch_provider", "LLM_CC_SWITCH_PROVIDER", DEFAULT_CC_SWITCH_PROVIDER),
        app_type="claude",
    )
    if provider is None:
        raise RuntimeError("CC Switch provider config was not found.")
    selected_model = _select_cc_switch_model(provider, model)
    if not selected_model:
        raise RuntimeError(f"CC Switch provider '{provider.name}' has no model configured.")
    token = provider.api_key
    if not token:
        raise RuntimeError(f"CC Switch provider '{provider.name}' has no API key/token configured.")
    base_url = provider.base_url or "https://api.anthropic.com"
    auth_header = "x-api-key"
    if "AUTH_TOKEN" in provider.env and "ANTHROPIC_API_KEY" not in provider.env:
        auth_header = "Authorization"
        token = f"Bearer {token}"
    payload = {
        "model": selected_model,
        "max_tokens": app_config_int("llm.max_tokens", "LLM_MAX_TOKENS", 3000),
        "temperature": 0,
        "system": "You are a senior code reviewer. Return strict JSON only.",
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post_json(
        f"{base_url.rstrip('/')}/v1/messages",
        payload,
        timeout,
        {
            auth_header: token,
            "anthropic-version": provider.env.get("ANTHROPIC_VERSION", os.getenv("ANTHROPIC_VERSION", "2023-06-01")),
        },
    )
    content = data.get("content", [])
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    if not text and "choices" in data:
        text = data["choices"][0]["message"]["content"]
    return text, provider.name, selected_model


def _select_cc_switch_model(provider: Any, requested_model: str) -> str:
    explicit = os.getenv("LLM_CC_SWITCH_MODEL", "").strip()
    if explicit:
        return explicit

    requested = (requested_model or "").strip()
    provider_model = str(getattr(provider, "model", "") or "").strip()
    if not requested or requested == "local-rules":
        return provider_model

    if _cc_switch_model_looks_incompatible(provider, requested):
        if provider_model:
            return provider_model
        raise RuntimeError(
            f"CC Switch provider '{getattr(provider, 'name', '-')}' cannot use model '{requested}'. "
            "Set LLM_CC_SWITCH_MODEL or configure a provider-compatible model in cc-switch."
        )
    return requested


def _cc_switch_model_looks_incompatible(provider: Any, model: str) -> bool:
    normalized = (model or "").strip().lower()
    if not normalized:
        return False
    if normalized == "local-rules":
        return True

    provider_text = " ".join(
        [
            str(getattr(provider, "id", "") or ""),
            str(getattr(provider, "name", "") or ""),
            str(getattr(provider, "base_url", "") or ""),
            " ".join(f"{key}={value}" for key, value in getattr(provider, "env", {}).items()),
        ]
    ).lower()

    if "deepseek" in provider_text:
        return not normalized.startswith("deepseek-")
    if "anthropic" in provider_text or "claude" in provider_text:
        return normalized.startswith(("gpt-", "deepseek-"))
    if "openai" in provider_text:
        return normalized.startswith(("claude-", "deepseek-"))
    return False


def _post_json(url: str, payload: dict[str, Any], timeout: int, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {body[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc}") from exc


def _review_prompt(review_input: ReviewInput) -> str:
    is_git_version = _is_git_version_review(review_input)
    max_chars = app_config_int("llm.max_diff_chars", "LLM_MAX_DIFF_CHARS", 60000)
    if is_git_version:
        max_chars = app_config_int(
            "llm.git_version_max_diff_chars",
            "LLM_GIT_VERSION_MAX_DIFF_CHARS",
            20000,
        )
    diff, diff_optimization = optimize_prompt_diff(review_input.changed_files, review_input.raw_diff, max_chars)
    review_input.metadata["llm_diff_optimization"] = diff_optimization
    review_input.metadata["llm_review_profile"] = "git-version-release-gate" if is_git_version else "standard"
    language = _prompt_language(report_language())
    if len(review_input.raw_diff) > max_chars and "[Diff truncated by LLM_MAX_DIFF_CHARS]" not in diff:
        diff += "\n\n[Diff truncated by LLM_MAX_DIFF_CHARS]"
    project_context = str(review_input.metadata.get('project_context') or '').strip()
    jira_description = str(review_input.metadata.get('jira_description') or '').strip()
    jira_prd_context = str(review_input.metadata.get("jira_prd_context") or "").strip()
    git_version_context = str(review_input.metadata.get("git_version_review_context") or "").strip()
    cross_mr_contract_context = str(review_input.metadata.get("cross_mr_contract_context") or "").strip()
    framework_context = _framework_review_context(review_input)
    review_template_context = _review_template_context()
    file_summary = "\n".join(
        f"- {item.path}: +{item.additions}/-{item.deletions}" for item in review_input.changed_files[:200]
    )
    related_mrs = _related_mrs_context(review_input)
    current_scope_value = review_input.metadata.get("current_review_scope") or {
        "jira_key": review_input.jira_key,
        "sprint": review_input.sprint,
        "merge_requests": review_input.metadata.get("related_merge_requests") or [],
        "diff_policy": "The supplied diff is the current revision review target.",
    }
    target_context_value = review_input.metadata.get("current_target_context") or {
        "target_branch": review_input.target_branch,
        "policy": "Target code is compatibility context only.",
    }
    historical_context_value = review_input.metadata.get("historical_requirement_context") or {
        "summary": jira_description,
        "excludes_previous_cycle_diffs": True,
    }
    current_review_scope = json.dumps(current_scope_value, ensure_ascii=False, indent=2, default=str)
    current_target_context = json.dumps(target_context_value, ensure_ascii=False, indent=2, default=str)
    historical_requirement_context = json.dumps(historical_context_value, ensure_ascii=False, indent=2, default=str)
    minimum_severity = report_min_severity()
    prompt = f"""Review this GitLab MR or consolidated Jira issue diff as a release-blocking senior reviewer.

Return strict JSON only, with this shape:
{{
  "findings": [
    {{
      "severity": "Critical|High|Medium|Low|Warning",
      "file_path": "path or Architecture",
      "line": 123,
      "title": "short title",
      "detail": "detailed evidence, failure mode, business/data impact, and Jira/cross-MR relationship",
      "recommendation": "concrete fix plan plus SQL/Mongo/API/CLI/UI verification and regression checks",
      "category": "Security|Correctness|Performance|Maintainability|Testing|DPS Layering|General"
    }}
  ],
  "notes": ["brief review notes"]
}}

Use deep reasoning before writing the JSON, but do not expose chain-of-thought. Only return the JSON object.
Review depth must match the ECHNL-5539 migration review style: connect the business requirement, changed code, data model, runtime side effects, rollback safety, and verification plan.

Finding detail contract:
- Report minimum severity is {minimum_severity}. Do not return findings below this severity. By default this means omit Low and Warning observations unless the configured minimum is lowered.
- For every Critical/High/Medium finding, `detail` must be developer-facing and specific. Include evidence from the diff, the runtime failure mode, business/data impact, and how it relates to the Jira/SVREQ requirement or another related MR.
- For every Critical/High/Medium finding, `recommendation` must include concrete code-level fix steps and a verification plan. Prefer exact SQL/Mongo queries, Drush/CLI/API calls, unit/integration tests, rerun/dry-run checks, rollback checks, and UAT smoke checks when applicable.
- Do not return generic static-analysis wording such as "possible dynamic SQL" without explaining the exact input source, sink, affected command/API, and whether it is exploitable. If the evidence is not strong, either omit it or mark it Low/Medium with the exact verification needed.
- Merge repeated symptoms into one root-cause finding when several files/MRs show the same issue. Explain all affected MRs in that one finding.
- If local Jira/PRD context is missing, explicitly use the Jira issue summary, branch names, MR titles, and code behavior to infer intent, and say what still needs requirement confirmation.

Focus areas:
- Correctness, security, data integrity, backward compatibility, tests, and DPS API/DAO/BIZ/CLI layering.
- Drupal framework review when applicable: service definitions and dependency injection, route permissions/access, entity/query APIs, render/cache metadata, update hook idempotency, config schema/YAML consistency, logging/translation, and Drupal-safe handling of user input, files, and paths.
- Data/file migration safety: exact target scope, source-of-truth records, file copy before DB update, partial failure handling, rollback, rerun/idempotency, backup behavior, and status reporting.
- DB safety: updateMany vs updateOne, language/version rows, WHERE/condition precision, old/new value guards, transaction boundaries, and cross-store consistency such as MongoDB plus MySQL.
- File/path safety: stream wrapper correctness, realpath normalization, source/destination overlap, missing files, overwrite behavior, hash/size validation, and environment-specific paths.
- Business compatibility: old and new config keys, allowlist changes, gray release/rollback compatibility, cached or historical data, duplicate business keys, and app/module scoping.
- Jira/PRD alignment: study the local Jira/PRD issue context when provided, including ECHNL action issues and linked SVREQ requirement references. Check whether the code actually implements the intended behavior, misses described cases, or changes behavior outside the issue scope.
- Consolidated Jira issue review: when multiple MRs are provided, review them as one end-to-end change set for the Jira issue. Check cross-project compatibility, missing paired changes, inconsistent API/DAO/BIZ/CLI/frontend behavior, branch/target mismatch, duplicate or contradictory logic, and whether all MRs together satisfy the Jira/SVREQ requirement.
- Scope boundary: Current Review Scope and its incremental diff are actionable; Current Target Context is compatibility evidence only; Historical Requirement Context is background only. The former Final Jira issue description is intentionally separated across current follow-up and historical requirement context. Never attribute an old-cycle diff to this run.
- Detailed developer-facing report style: when you find an issue, write enough detail for a developer to reproduce the failure mode, locate the affected code, understand business impact, apply a concrete fix, and verify it with SQL/Mongo/API/UI checks.
- DPS layering: API/DAO/BIZ/CLI callers, changed query shape, error handling, auth, and whether all impacted layers changed together.
- DPS database-change policy: for DPS9/DPS11 backend projects, database update work is extracted into `db_change.scr`, normally through GIT_VERSION MR build resources. Do not review this as a Drupal update-hook/install-schema mechanism. Instead, review `db_change.scr` self-consistency: command ordering, referenced SQL/shell/resource files, missing command inputs, idempotency/rerun safety, rollback/backup expectations, environment scope, and whether the commands match the previous database version or prior `db_change.scr` definitions when context is available.
- DPS environment configuration policy: environment-specific settings are centralized in `state_config.yml` / `state_config.<env>.yml` files such as poc, sit, uat, preprod, and prod; historical `state_cofig.<env>.yml` spelling may also exist. Token/encryption-related values in those files are expected environment configuration; do not report them as suspected hard-coded secrets merely because they contain token/secret/password-like keys. Only report them when there is concrete evidence of a real leaked credential outside the expected config mechanism, invalid environment scoping, broken encryption/reference format, or cross-environment leakage.
- GIT_VERSION MR review: validate git_version.yml repository locks, branch-to-commit traceability, duplicate YAML keys, full 40-char commit SHAs, build.yml git_version reference, build-code repository branch/commit lock, and company/environment build scope. For build.yml self-locking, version.git_repository.commit/version.git_version4config.git_repository.commit may point to any build repository commit after the required build resources were pushed; it does not have to equal the current MR head. Only report that lock when it is missing, invalid, unfetchable, or the locked commit does not contain the required build.yml/git_version.yml resources. Also review the actual locked source repository commit diffs and build-resource commit diffs supplied in the context: check syntax risks, code processing logic, config/resource packaging logic, and whether commit messages/release notes/Jira ECHNL issues/SVREQ issues line up with the changed behavior. Treat the build repository branch as the revision/base version branch when configured that way; do not flag patch-version-looking file names as branch misuse. Later patch build versions are derived from build history (*.bh) by incrementing the patch number from 1, and generated git_version-v<revision-or-patch>.yml / build-v<revision-or-patch>.yml files may be valid depending on package type.
- Verification: concrete SQL/Mongo/query checks, unit/integration tests, dry-run, rerun, rollback, and production/UAT smoke checks.
- Only report findings that are actionable and tied to the diff or local project context.
Do not invent findings. If a risk is uncertain, mark it Medium or Low and describe the exact verification needed.
Write all user-facing JSON values in this output language: {language}.
Do not include Markdown fences, commentary, or any text outside the JSON object.

Project: {review_input.project or "-"}
MR: {review_input.mr_url or review_input.mr_id or "-"}
Jira: {review_input.jira_key or "-"}
Source branch: {review_input.source_branch or "-"}
Target branch: {review_input.target_branch or "-"}
Commit: {review_input.commit or "-"}

Current Review Scope (the only actionable review target):
{current_review_scope}

Current revision MRs:
{related_mrs or "-"}

Cross-MR implementation contracts:
{cross_mr_contract_context or "-"}

Current revision changed files:
{file_summary or "-"}

Current Target Context (compatibility/impact context only; do not report it as a change by itself):
{current_target_context}

Target-branch related project context:
{project_context or "-"}

Historical Requirement Context (background only; never treat previous-cycle diffs as current changes):
{historical_requirement_context}

Local Jira/PRD issue context:
{jira_prd_context or "-"}

GIT_VERSION MR context:
{git_version_context or "-"}

Framework review context:
{framework_context or "-"}

Review report template/style guide:
{review_template_context or "-"}

Current Review Scope incremental diff (base SHA to current head SHA only):
```diff
{diff}
```
"""
    return _enforce_prompt_budget(review_input, prompt)


def _enforce_prompt_budget(review_input: ReviewInput, prompt: str) -> str:
    is_git_version = _is_git_version_review(review_input)
    if is_git_version:
        max_chars = app_config_int(
            "llm.git_version_prompt_max_chars",
            "LLM_GIT_VERSION_PROMPT_MAX_CHARS",
            60000,
        )
    else:
        max_chars = app_config_int("llm.prompt_max_chars", "LLM_PROMPT_MAX_CHARS", 160000)
    if max_chars <= 0:
        review_input.metadata["llm_prompt_chars"] = len(prompt)
        review_input.metadata["llm_context_budget"] = {"enabled": False, "original_chars": len(prompt), "final_chars": len(prompt)}
        return prompt
    if is_git_version:
        target_chars = app_config_int(
            "llm.git_version_prompt_target_chars",
            "LLM_GIT_VERSION_PROMPT_TARGET_CHARS",
            45000,
        )
    else:
        target_chars = app_config_int("llm.prompt_target_chars", "LLM_PROMPT_TARGET_CHARS", 0)
    trim_target_chars = max_chars
    if 0 < target_chars < max_chars:
        trim_target_chars = target_chars

    original_chars = len(prompt)
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "max_chars": max_chars,
        "target_chars": trim_target_chars,
        "original_chars": original_chars,
        "final_chars": original_chars,
        "trimmed_chars": 0,
        "sections": {},
    }
    if original_chars <= trim_target_chars:
        review_input.metadata["llm_prompt_chars"] = original_chars
        review_input.metadata["llm_context_budget"] = diagnostics
        return prompt

    trim_plan = [
        ('historical_requirement_context', 'Historical Requirement Context (background only; never treat previous-cycle diffs as current changes):\n', '\n\nLocal Jira/PRD issue context:', 4000),
        ("review_template_context", "Review report template/style guide:\n", "\n\nCurrent Review Scope incremental diff", 2500),
        ("project_context", "Target-branch related project context:\n", "\n\nHistorical Requirement Context", 4000 if is_git_version else app_config_int("llm.prompt_min_project_context_chars", "LLM_PROMPT_MIN_PROJECT_CONTEXT_CHARS", 12000)),
        ("jira_prd_context", "Local Jira/PRD issue context:\n", "\n\nGIT_VERSION MR context:", 3000 if is_git_version else app_config_int("llm.prompt_min_jira_prd_context_chars", "LLM_PROMPT_MIN_JIRA_PRD_CONTEXT_CHARS", 8000)),
        ("git_version_context", "GIT_VERSION MR context:\n", "\n\nFramework review context:", app_config_int("llm.git_version_prompt_min_context_chars", "LLM_GIT_VERSION_PROMPT_MIN_CONTEXT_CHARS", 16000) if is_git_version else app_config_int("llm.prompt_min_git_version_context_chars", "LLM_PROMPT_MIN_GIT_VERSION_CONTEXT_CHARS", 12000)),
        ("framework_context", "Framework review context:\n", "\n\nReview report template/style guide:", 3000),
        ("current_review_scope", "Current Review Scope (the only actionable review target):\n", "\n\nCurrent revision MRs:", 2500),
        ("related_mrs_context", "Current revision MRs:\n", "\n\nCross-MR implementation contracts:", 4000),
        ("cross_mr_contract_context", "Cross-MR implementation contracts:\n", "\n\nCurrent revision changed files:", 2500),
        ("diff", "Current Review Scope incremental diff (base SHA to current head SHA only):\n```diff\n", "\n```", app_config_int("llm.git_version_prompt_min_diff_chars", "LLM_GIT_VERSION_PROMPT_MIN_DIFF_CHARS", 8000) if is_git_version else app_config_int("llm.prompt_min_diff_chars", "LLM_PROMPT_MIN_DIFF_CHARS", 30000)),
    ]
    for name, start_marker, end_marker, _min_chars in trim_plan:
        content = _section_content(prompt, start_marker, end_marker)
        if content is not None:
            diagnostics["sections"][name] = {
                "original_chars": len(content),
                "final_chars": len(content),
                "trimmed_chars": 0,
            }

    if (
        original_chars > max_chars
        and app_config_str("llm.prompt_over_budget", "LLM_PROMPT_OVER_BUDGET", "trim").strip().lower() in {"fail", "error", "strict"}
    ):
        diagnostics["over_budget"] = True
        diagnostics["trimmed_chars"] = 0
        review_input.metadata["llm_prompt_chars"] = original_chars
        review_input.metadata["llm_context_budget"] = diagnostics
        raise RuntimeError(_prompt_budget_error_message(diagnostics))

    current = prompt
    for name, start_marker, end_marker, min_chars in trim_plan:
        if len(current) <= trim_target_chars:
            break
        content = _section_content(current, start_marker, end_marker)
        if content is None:
            continue
        before_len = len(content)
        target = max(min_chars, before_len - (len(current) - trim_target_chars))
        if target >= before_len:
            continue
        trimmed = _trim_section_content(content, target, name)
        current = _replace_section_content(current, start_marker, end_marker, trimmed)
        diagnostics["sections"][name] = {
            "original_chars": before_len,
            "final_chars": len(trimmed),
            "trimmed_chars": before_len - len(trimmed),
        }

    if len(current) > max_chars:
        hard_max = (
            app_config_int("llm.git_version_prompt_hard_max_chars", "LLM_GIT_VERSION_PROMPT_HARD_MAX_CHARS", max_chars)
            if is_git_version
            else app_config_int("llm.prompt_hard_max_chars", "LLM_PROMPT_HARD_MAX_CHARS", max_chars)
        )
        if hard_max > 0 and len(current) > hard_max:
            current = current[:hard_max] + "\n[Prompt hard-truncated by LLM_PROMPT_HARD_MAX_CHARS]\n"
            diagnostics["hard_truncated"] = True

    diagnostics["final_chars"] = len(current)
    diagnostics["trimmed_chars"] = original_chars - len(current)
    review_input.metadata["llm_prompt_chars"] = len(current)
    review_input.metadata["llm_context_budget"] = diagnostics
    return current


def _is_git_version_review(review_input: ReviewInput) -> bool:
    if str(review_input.metadata.get("mr_type") or "").strip().upper() == "GIT_VERSION":
        return True
    branch = str(review_input.source_branch or "").upper().replace("-", "_")
    return "GIT_VERSION" in branch


def preview_llm_prompt_budget(review_input: ReviewInput) -> dict[str, Any]:
    _review_prompt(review_input)
    budget = review_input.metadata.get("llm_context_budget")
    return budget if isinstance(budget, dict) else {}


def _prompt_budget_error_message(diagnostics: dict[str, Any]) -> str:
    sources = _prompt_budget_source_summary(diagnostics)
    source_text = f" Largest sections: {sources}." if sources else ""
    return (
        "LLM prompt context is over budget: "
        f"{diagnostics.get('original_chars', '-')} chars > {diagnostics.get('max_chars', '-')} chars. "
        f"{source_text} "
        "Set LLM_PROMPT_OVER_BUDGET=trim to auto-trim low-priority context, or reduce "
        "LLM_MAX_DIFF_CHARS / PROJECT_CONTEXT_MAX_CHARS / JIRA_PRD_CONTEXT_MAX_CHARS / "
        "GIT_VERSION_SOURCE_DIFF_CONTEXT_MAX_CHARS."
    )


def _prompt_budget_source_summary(diagnostics: dict[str, Any]) -> str:
    sections = diagnostics.get("sections")
    if not isinstance(sections, dict):
        return ""
    ranked: list[tuple[str, int]] = []
    for name, details in sections.items():
        if not isinstance(details, dict):
            continue
        try:
            ranked.append((str(name), int(details.get("original_chars") or 0)))
        except (TypeError, ValueError):
            continue
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ", ".join(f"{name}={size}" for name, size in ranked[:5] if size)


def _section_content(prompt: str, start_marker: str, end_marker: str) -> str | None:
    start = prompt.find(start_marker)
    if start < 0:
        return None
    content_start = start + len(start_marker)
    end = prompt.find(end_marker, content_start)
    if end < 0:
        return None
    return prompt[content_start:end]


def _replace_section_content(prompt: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = prompt.find(start_marker)
    if start < 0:
        return prompt
    content_start = start + len(start_marker)
    end = prompt.find(end_marker, content_start)
    if end < 0:
        return prompt
    return prompt[:content_start] + replacement + prompt[end:]


def _trim_section_content(content: str, target_chars: int, section_name: str) -> str:
    marker = f"\n[Context budget trimmed {max(0, len(content) - target_chars)} chars from {section_name}]\n"
    budget = max(0, target_chars - len(marker))
    if budget <= 0:
        return marker.strip()
    if budget < 2000:
        return content[:budget] + marker
    head_len = max(1, int(budget * 0.75))
    tail_len = max(0, budget - head_len)
    return content[:head_len] + marker + (content[-tail_len:] if tail_len else "")


def _related_mrs_context(review_input: ReviewInput) -> str:
    related = review_input.metadata.get("related_merge_requests") or []
    if not isinstance(related, list) or not related:
        return ""
    lines: list[str] = []
    for item in related[:80]:
        if not isinstance(item, dict):
            continue
        match = str(item.get("git_tools_project_match") or "-")
        group = str(item.get("git_tools_group") or "")
        module = str(item.get("git_tools_module") or "")
        if group or module:
            match = f"{match} {group}/{module}"
        lines.append(
            "- "
            + " | ".join(
                [
                    str(item.get("mr_url") or "-"),
                    str(item.get("project_path") or item.get("project") or "-"),
                    f"{item.get('source_branch') or '-'} -> {item.get('target_branch') or '-'}",
                    f"commit {item.get('commit') or '-'}",
                    f"files {item.get('file_count') or 0}",
                    f"config {match}",
                ]
            )
        )
    return "\n".join(lines)


def _prompt_language(language: str) -> str:
    value = (language or "zh-CN").lower()
    if value.startswith("en"):
        return "English"
    if value in {"zh", "zh-cn", "zh_cn", "cn"}:
        return "Simplified Chinese"
    return language


def _review_template_context() -> str:
    configured = app_config_str("review.template_path", "REVIEW_TEMPLATE_PATH", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(ROOT_DIR / 'docs' / 'ECHNL-5539.md')
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        limit = app_config_int("review.template_max_chars", "REVIEW_TEMPLATE_MAX_CHARS", 16000)
        return text[:limit]
    return ""


def _framework_review_context(review_input: ReviewInput) -> str:
    framework = app_config_str("review.framework", "REVIEW_FRAMEWORK", "auto").strip().lower()
    if framework in {"", "none", "off", "0", "false", "no"}:
        return ""
    if framework not in {"auto", "drupal", "drupal-framework"}:
        return ""
    if framework == "auto" and not _looks_like_drupal_review(review_input):
        return ""
    skill_path = Path(os.getenv("DRUPAL_SKILL_PATH", r"C:\Users\xuejie.xiao\.codex\skills\drupal-framework"))
    parts: list[str] = []
    for path in (skill_path / "SKILL.md", skill_path / "references" / "drupal-review.md"):
        if not path.exists():
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    if parts:
        return "\n\n".join(parts)[: int(os.getenv("DRUPAL_SKILL_CONTEXT_MAX_CHARS", "12000"))]
    return (
        "Drupal framework review: check services/dependency injection, routing/access, Entity API, Database API "
        "placeholders, render cacheability, config schema/YAML, update hook idempotency, logging/translation, "
        "file/path safety, and DPS/WVMO DAO/API/BIZ/CLI layer responsibilities."
    )


def _looks_like_drupal_review(review_input: ReviewInput) -> bool:
    project_path = str(review_input.metadata.get("gitlab_project_path") or review_input.project or "").lower()
    if any(token in project_path for token in ("/dps/", "/dps11/", "dps11", "wvmo", "micromod", "microsrvs")):
        return True
    drupal_patterns = (
        ".module",
        ".install",
        ".services.yml",
        ".routing.yml",
        ".permissions.yml",
        "/modules/",
        "\\modules\\",
        "/src/",
        "\\src\\",
    )
    for changed_file in review_input.changed_files:
        path = changed_file.path.lower()
        if any(pattern in path for pattern in drupal_patterns):
            return True
    return False


def _parse_llm_response(text: str) -> tuple[list[Finding], list[str]]:
    payload = _extract_json(text)
    raw_findings = payload.get("findings", []) if isinstance(payload, dict) else []
    notes = payload.get("notes", []) if isinstance(payload, dict) else []
    findings: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "Medium")).title()
        if severity not in SEVERITIES:
            severity = "Medium"
        line = item.get("line")
        findings.append(
            Finding(
                severity=severity,
                file_path=str(item.get("file_path") or "Architecture"),
                line=int(line) if isinstance(line, int) or (isinstance(line, str) and line.isdigit()) else None,
                title=str(item.get("title") or "LLM review finding"),
                detail=str(item.get("detail") or item.get("description") or ""),
                recommendation=str(item.get("recommendation") or "Review and address this issue before merge."),
                category=str(item.get("category") or "LLM Review"),
            )
        )
    return findings, [str(item) for item in notes if item]


def _parse_llm_response_strict(text: str) -> tuple[list[Finding], list[str]]:
    if _llm_require_structured_output():
        return _parse_llm_response(text)
    return _safe_parse_llm_response(text)


def _safe_parse_llm_response(text: str) -> tuple[list[Finding], list[str]]:
    try:
        return _parse_llm_response(text)
    except Exception as exc:
        heuristic_findings = _parse_text_findings(text)
        excerpt = re.sub(r"\s+", " ", text.strip())[:1200]
        notes = [f"Provider returned non-JSON output. Parse error: {exc}"]
        if excerpt:
            notes.append(f"Provider output excerpt: {excerpt}")
        return heuristic_findings, notes


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        candidate = _first_balanced_json_object(stripped)
        if candidate:
            return json.loads(candidate)
    raise RuntimeError("provider returned non-JSON review output.")


def _first_balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _parse_text_findings(text: str) -> list[Finding]:
    findings: list[Finding] = []
    pattern = re.compile(
        r"(?P<severity>Critical|High|Medium|Low|Warning)\s*[:\-]\s*(?P<title>[^\n]+)(?P<body>.*?)(?=\n\s*(?:Critical|High|Medium|Low|Warning)\s*[:\-]|\Z)",
        re.I | re.S,
    )
    for match in pattern.finditer(text or ""):
        severity = match.group("severity").title()
        body = re.sub(r"\s+", " ", match.group("body")).strip()
        findings.append(
            Finding(
                severity=severity if severity in SEVERITIES else "Medium",
                file_path=_extract_file_path(body) or "Architecture",
                line=None,
                title=match.group("title").strip(" -*#"),
                detail=body[:1200] or match.group("title").strip(),
                recommendation="Review the provider output and address the described risk before merge.",
                category="LLM Review",
            )
        )
    return findings


def _extract_file_path(text: str) -> str:
    match = re.search(r"([\w./\\-]+\.(?:php|py|js|ts|dart|java|sql|yml|yaml|json))", text or "")
    return match.group(1) if match else ""


def _brief_error(error: object) -> str:
    text = str(error)
    for marker in ("-------- user ", "\nuser ", " user Review this GitLab"):
        index = text.find(marker)
        if index >= 0:
            tail_start = _diagnostic_tail_start(text, index + len(marker))
            tail = text[tail_start:] if tail_start >= 0 else ""
            text = text[:index].rstrip() + " <prompt omitted> " + tail
            break
    text = re.sub(r"['\"]?[A-Z]:\\[^,'\"\]\s]+", "<path>", text)
    text = re.sub(r"['\"]?/[^,'\"\]\s]+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if "timed out after" in text:
        match = re.search(r"timed out after \d+(?:\.\d+)? seconds", text)
        return match.group(0) if match else "timed out"
    diagnostic = _diagnostic_error_lines(text)
    if diagnostic:
        return diagnostic[:500]
    return text[:350]


def _diagnostic_tail_start(text: str, start: int) -> int:
    candidates = [
        match.start()
        for pattern in (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", r"\b(?:ERROR|WARN):", r"\bERROR\b", r"\bWARN\b")
        for match in re.finditer(pattern, text[start:])
    ]
    return start + min(candidates) if candidates else -1


def _diagnostic_error_lines(text: str) -> str:
    patterns = (
        "stream disconnected",
        "Reconnecting",
        "ERROR",
        "unauthorized",
        "forbidden",
        "rate limit",
        "timed out",
        "output schema",
        "not valid JSON",
        "shell snapshot",
    )
    lines = []
    for raw_line in re.split(r"(?:\\n|\r?\n)", text):
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.lower() in line.lower() for pattern in patterns):
            lines.append(line)
    return " | ".join(lines[-6:])


def _review_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "severity": {"type": "string", "enum": ["Critical", "High", "Medium", "Low", "Warning"]},
                        "file_path": {"type": "string"},
                        "line": {"type": ["integer", "null"]},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["severity", "file_path", "line", "title", "detail", "recommendation", "category"],
                },
            },
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["findings", "notes"],
    }


def _normalize_provider(provider: str) -> str:
    value = provider.strip().lower().replace("_", "-")
    aliases = {
        "codex": "codex-cli",
        "openai-codex": "codex-cli",
        "preferred": "auto",
        "codex-deepseek": "auto",
        "codex-then-deepseek": "auto",
        "ccswitch": "cc-switch",
        "cc-switch-current": "cc-switch",
        "deepseek-v4": "deepseek",
        "deepseek-v4-pro": "deepseek",
        "claude-code-opus": "claude",
        "claude-opus": "claude",
        "anthropic": "claude",
    }
    return aliases.get(value, value)


def _model_for_provider(provider: str, configured_model: str, config: dict[str, Any] | None = None) -> str:
    if configured_model and configured_model != "local-rules":
        return configured_model
    defaults = {
        "auto": str((config or {}).get("codex_model") or "gpt-5.6-sol"),
        "codex-cli": str((config or {}).get("codex_model") or os.getenv("LLM_CODEX_MODEL", "gpt-5.6-sol")),
        "deepseek": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "claude": os.getenv("CLAUDE_MODEL", "claude-opus-4-1"),
    }
    return defaults.get(provider, configured_model or "local-rules")
