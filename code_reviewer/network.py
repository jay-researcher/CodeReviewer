from __future__ import annotations

import socket
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass

from .config import llm_config


@dataclass(slots=True)
class NetworkStatus:
    gitlab_host: str
    gitlab_port_open: bool
    gitlab_hint: str
    codex_available: bool
    codex_hint: str
    deepseek_hint: str
    recommended_action: str


def check_network(gitlab_url: str = "https://gitlab.tx-tech.com") -> NetworkStatus:
    parsed = urllib.parse.urlparse(gitlab_url)
    host = parsed.hostname or "gitlab.tx-tech.com"
    gitlab_open = _tcp_open(host, 443, timeout=5)
    codex_available = _codex_available()

    if gitlab_open and not codex_available:
        action = "GitLab is reachable, but Codex is unavailable. Use CC Switch fallback or fix Codex CLI/auth."
    elif not gitlab_open and codex_available:
        action = "Codex is available, but GitLab is not reachable. Check VPN DIRECT rule, or review a local repo/diff."
    elif gitlab_open and codex_available:
        action = "GitLab and the Codex CLI binary are reachable. Run --codex-check to verify real Codex execution."
    else:
        action = "Neither GitLab nor Codex is reachable. Fix network or review a local repo/diff after switching to CC Switch."

    return NetworkStatus(
        gitlab_host=host,
        gitlab_port_open=gitlab_open,
        gitlab_hint="GitLab is reachable via current network/DIRECT rule." if gitlab_open else "GitLab is not reachable; check VPN DIRECT rule or use local repo/diff review.",
        codex_available=codex_available,
        codex_hint="Codex CLI binary is available; use python review.py --codex-check to test model execution." if codex_available else "Codex CLI is unavailable or not responsive in this environment.",
        deepseek_hint="CC Switch Claude code opus remains available as fallback.",
        recommended_action=action,
    )


def check_network_dict(gitlab_url: str = "https://gitlab.tx-tech.com") -> dict[str, object]:
    return asdict(check_network(gitlab_url))


def _tcp_open(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _codex_available() -> bool:
    from .llm_provider import _resolve_codex_cli

    codex = _resolve_codex_cli()
    if not codex:
        return False
    try:
        completed = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
