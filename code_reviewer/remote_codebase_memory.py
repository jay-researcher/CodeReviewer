from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def remote_enabled() -> bool:
    return bool(os.getenv("CODEBASE_MEMORY_SSH_HOST", "").strip())


def run_remote_tool(
    tool: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    repo: Path | None = None,
    commit: str = "",
) -> subprocess.CompletedProcess[str]:
    try:
        client = _connect(timeout)
    except Exception as exc:
        return _completed(tool, 1, "", f"Codebase Memory SSH connection failed: {exc}")
    try:
        remote_payload = dict(payload)
        project = str(remote_payload.get("name") or remote_payload.get("project") or "").strip()
        if tool == "index_repository":
            if repo is None:
                return _completed(tool, 1, "", "Remote indexing requires a local repository snapshot")
            remote_repo = _upload_snapshot(client, repo, project, commit, timeout)
            remote_payload["repo_path"] = remote_repo
        completed = _exec_cli(client, tool, remote_payload, timeout)
        response_failed = False
        try:
            response = json.loads(completed.stdout or "{}")
            response_failed = isinstance(response, dict) and str(response.get("status") or "").lower() in {"error", "failed"}
        except json.JSONDecodeError:
            pass
        if tool == "index_repository" and completed.returncode == 0 and not response_failed and project and commit:
            _write_remote_marker(client, project, commit, timeout)
        return completed
    except Exception as exc:
        return _completed(tool, 1, "", f"Remote Codebase Memory {tool} failed: {exc}")
    finally:
        client.close()


def remote_index_matches(project: str, commit: str, timeout: int = 15) -> bool:
    if not remote_enabled() or not project or not commit:
        return False
    try:
        client = _connect(timeout)
        marker = _remote_marker(project, commit)
        status, _, _ = _exec(client, f"test -f {shlex.quote(marker)}", timeout)
        return status == 0
    except Exception:
        return False
    finally:
        if "client" in locals():
            client.close()


def _connect(timeout: int):
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("paramiko is required for CODEBASE_MEMORY_SSH_HOST") from exc

    host = os.getenv("CODEBASE_MEMORY_SSH_HOST", "").strip()
    user, password = _credentials(host)
    client = paramiko.SSHClient()
    known_hosts = Path(os.getenv("CODEBASE_MEMORY_SSH_KNOWN_HOSTS", str(Path.home() / ".ssh" / "known_hosts"))).expanduser()
    if known_hosts.is_file():
        client.load_host_keys(str(known_hosts))
    strict = os.getenv("CODEBASE_MEMORY_SSH_STRICT_HOST_KEY", "1").strip().lower() not in {"0", "false", "no", "off"}
    client.set_missing_host_key_policy(paramiko.RejectPolicy() if strict else paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=int(os.getenv("CODEBASE_MEMORY_SSH_PORT", "22")),
        username=user,
        password=password or None,
        key_filename=os.getenv("CODEBASE_MEMORY_SSH_KEY_FILE") or None,
        passphrase=os.getenv("CODEBASE_MEMORY_SSH_KEY_PASSPHRASE") or None,
        timeout=min(max(timeout, 1), 30),
        banner_timeout=min(max(timeout, 1), 30),
        auth_timeout=min(max(timeout, 1), 30),
        allow_agent=True,
        look_for_keys=True,
    )
    return client


def _credentials(host: str) -> tuple[str, str]:
    user = os.getenv("CODEBASE_MEMORY_SSH_USER", "").strip()
    password = os.getenv("CODEBASE_MEMORY_SSH_PASSWORD", "")
    config_path = os.getenv("CODEBASE_MEMORY_SSH_CONFIG", "").strip()
    if config_path and Path(config_path).is_file():
        text = Path(config_path).read_text(encoding="utf-8")
        block = _host_config_block(text, host)
        user_match = re.search(r"(?m)^\s+user:\s*([^\s#]+)", block)
        password_match = re.search(r"(?m)^\s+(?:password|passphase):\s*([^\r\n#]+)", block)
        user = user or (user_match.group(1).strip("\"'") if user_match else "")
        password = password or (password_match.group(1).strip().strip("\"'") if password_match else "")
    return user or os.getenv("USERNAME", ""), password


def _host_config_block(text: str, host: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(host)}:\s*\n(?P<body>.*?)(?=^[^\s#][^\n]*:\s*(?:#.*)?$|\Z)", text)
    return match.group("body") if match else ""


def _upload_snapshot(client: Any, repo: Path, project: str, commit: str, timeout: int) -> str:
    safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project or repo.name).strip("._-") or "project"
    safe_commit = re.sub(r"[^A-Fa-f0-9]+", "", commit)[:40] or "snapshot"
    root = PurePosixPath(os.getenv("CODEBASE_MEMORY_REMOTE_SOURCE_ROOT", "/var/lib/codebase-memory-mcp/sources"))
    destination = str(root / f"{safe_project}__{safe_commit[:12]}")
    marker = f"{destination}/.codereviewer-snapshot"
    status, _, _ = _exec(client, f"test -f {shlex.quote(marker)}", timeout)
    if status == 0:
        return destination

    with tempfile.TemporaryDirectory(prefix="codereviewer-cbm-") as temp:
        archive = Path(temp) / "snapshot.tar.gz"
        completed = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar.gz", "-o", str(archive), "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "git archive failed")[:500])
        remote_archive = f"{destination}.tar.gz"
        _exec_checked(client, f"mkdir -p {shlex.quote(str(root))}", timeout)
        sftp = client.open_sftp()
        try:
            sftp.put(str(archive), remote_archive)
        finally:
            sftp.close()
        command = (
            f"rm -rf {shlex.quote(destination)} && mkdir -p {shlex.quote(destination)} && "
            f"tar -xzf {shlex.quote(remote_archive)} -C {shlex.quote(destination)} && "
            f"touch {shlex.quote(marker)} && rm -f {shlex.quote(remote_archive)}"
        )
        _exec_checked(client, command, timeout)
    return destination


def _exec_cli(client: Any, tool: str, payload: dict[str, Any], timeout: int) -> subprocess.CompletedProcess[str]:
    binary = os.getenv("CODEBASE_MEMORY_REMOTE_COMMAND", "/usr/local/bin/codebase-memory-mcp")
    # The 0.9.x native binary reads one JSON payload from stdin when no
    # positional JSON argument is provided. Its Python wrapper documents
    # --args-file -, but the deployed native binary does not accept '-' there.
    command = f"{shlex.quote(binary)} cli {shlex.quote(tool)}"
    status, output, error = _exec(client, command, timeout, json.dumps(payload, ensure_ascii=False))
    return _completed(tool, status, output, error)


def _write_remote_marker(client: Any, project: str, commit: str, timeout: int) -> None:
    marker = _remote_marker(project, commit)
    _exec_checked(client, f"mkdir -p {shlex.quote(str(PurePosixPath(marker).parent))} && touch {shlex.quote(marker)}", timeout)


def _remote_marker(project: str, commit: str) -> str:
    safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project).strip("._-") or "project"
    safe_commit = re.sub(r"[^A-Fa-f0-9]+", "", commit)[:40]
    root = PurePosixPath(os.getenv("CODEBASE_MEMORY_REMOTE_STATE_ROOT", "/var/lib/codebase-memory-mcp/codereviewer-state"))
    return str(root / f"{safe_project}__{safe_commit}.ready")


def _exec(client: Any, command: str, timeout: int, input_text: str | None = None) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if input_text is not None:
        stdin.write(input_text)
        stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), output, error


def _exec_checked(client: Any, command: str, timeout: int) -> str:
    status, output, error = _exec(client, command, timeout)
    if status != 0:
        raise RuntimeError((error or output or f"remote command exited {status}")[:500])
    return output


def _completed(tool: str, status: int, output: str, error: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["remote-codebase-memory", tool], status, output, error)
