from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import paramiko


DEFAULT_CONFIG = Path(r"D:\TTL\devops\config.yml")
DEFAULT_KNOWN_HOSTS = Path(r"D:\TTL\.ssh_known_hosts_378")
HOST = "192.168.3.78"


def _credentials(config_path: Path) -> tuple[str, str]:
    content = config_path.read_text(encoding="utf-8")
    section = content.split("127.0.0.1:", 1)[0]
    user_match = re.search(r"^\s*user:\s*([^\r\n#]+)", section, re.MULTILINE)
    password_match = re.search(r"^\s*passphase:\s*([^\r\n#]+)", section, re.MULTILINE)
    if not user_match or not password_match:
        raise RuntimeError(f"Host credentials were not found in {config_path}")
    clean = lambda value: value.strip().strip("\"'")
    return clean(user_match.group(1)), clean(password_match.group(1))


def connect(config_path: Path, known_hosts: Path) -> paramiko.SSHClient:
    username, password = _credentials(config_path)
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        HOST,
        username=username,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )
    return client


def run(client: paramiko.SSHClient, command: str) -> int:
    _, stdout, stderr = client.exec_command(command, get_pty=False)
    for chunk in iter(lambda: stdout.read(65536), b""):
        sys.stdout.buffer.write(chunk)
    for chunk in iter(lambda: stderr.read(65536), b""):
        sys.stderr.buffer.write(chunk)
    return stdout.channel.recv_exit_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an authenticated command on the configured 3.78 host.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Remote shell command.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--known-hosts", type=Path, default=DEFAULT_KNOWN_HOSTS)
    args = parser.parse_args()
    command = " ".join(args.command).strip()
    if not command:
        parser.error("a remote command is required")
    with connect(args.config, args.known_hosts) as client:
        return run(client, command)


if __name__ == "__main__":
    raise SystemExit(main())
