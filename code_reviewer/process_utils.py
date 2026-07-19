from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Sequence


def run_utf8(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        return _run_utf8_windows(command, input_text=input_text, timeout=timeout)

    creationflags = 0
    start_new_session = False
    start_new_session = True

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as drain_exc:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                stdout = _timeout_output(drain_exc.stdout, exc.stdout)
                stderr = _timeout_output(drain_exc.stderr, exc.stderr)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
        detail = f"process timed out after {timeout} seconds"
        stderr = f"{stderr.rstrip()}\n{detail}".strip()
        return subprocess.CompletedProcess(list(command), 124, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            taskkill = subprocess.Popen(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                taskkill.wait(timeout=2)
            except subprocess.TimeoutExpired:
                taskkill.kill()
            if taskkill.returncode not in {0, None} and process.poll() is None:
                process.kill()
        except OSError:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


def _timeout_output(*values: str | bytes | None) -> str:
    for value in values:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
    return ""


def _run_utf8_windows(
    command: Sequence[str],
    *,
    input_text: str | None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Bound Windows execution even when descendants inherit output handles.

    subprocess.communicate() can wait for a surviving grandchild's inherited
    pipes after the direct process exits. Daemon drainers and bounded joins keep
    the review timeout authoritative while preserving available UTF-8 output.
    """
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def drain(pipe: object, key: str) -> None:
        try:
            while True:
                value = pipe.read(4096)  # type: ignore[attr-defined]
                if not value:
                    return
                chunks[key].append(value)
        except (OSError, ValueError):
            return
        finally:
            try:
                pipe.close()  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if process.stdin is not None:
        try:
            process.stdin.write(input_text or "")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

    for reader in readers:
        reader.join(timeout=0.25)
    stdout = "".join(chunks["stdout"])
    stderr = "".join(chunks["stderr"])
    if timed_out:
        detail = f"process timed out after {timeout} seconds"
        stderr = f"{stderr.rstrip()}\n{detail}".strip()
        return subprocess.CompletedProcess(list(command), 124, stdout, stderr)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
