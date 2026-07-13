from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_reviewer.web_app import run


def _acquire_instance_lock(port: int) -> BinaryIO:
    lock_path = Path(__file__).resolve().parent / "data" / f"web-{port}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        raise RuntimeError(f"CodeReviewer Web is already running on port {port}.") from None
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CodeReviewer web app.")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8765")))
    parser.add_argument("--lan", action="store_true", help="Listen on all network interfaces so other computers can access the Web UI.")
    parser.add_argument("--allow-ip", action="append", default=[], help="Allow a client IP/CIDR/wildcard to access the Web UI. Can be repeated.")
    args = parser.parse_args()
    if args.allow_ip:
        existing = os.getenv("WEB_IP_WHITELIST", "")
        values = [item for item in [existing, *args.allow_ip] if item]
        os.environ["WEB_IP_WHITELIST"] = ",".join(values)
    try:
        instance_lock = _acquire_instance_lock(args.port)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        run(host="0.0.0.0" if args.lan else args.host, port=args.port)
    finally:
        instance_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
