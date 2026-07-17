#!/usr/bin/env python3
"""Small end-to-end test for the pman daemon and PTY capture."""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PMAN = Path(os.environ.get("PMAN_BIN", str(ROOT / "pman.py"))).expanduser().resolve()


def pman_command(*args: str) -> list[str]:
    if PMAN.suffix == ".py":
        return [sys.executable, str(PMAN), *args]
    return [str(PMAN), *args]


def call(env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        pman_command(*args),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        timeout=10,
    )


def tasks(env: dict[str, str]) -> list[dict[str, object]]:
    return json.loads(call(env, "list", "--json").stdout)


def wait_until(predicate, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


def drain_pty(fd: int, seconds: float = 0.3) -> bytes:
    deadline = time.monotonic() + seconds
    output = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="pman-test-"))
    env = os.environ.copy()
    env["PMAN_HOME"] = str(temp)
    shell_pid = None
    shell_fd = None
    ui_shell_pid = None
    ui_shell_fd = None
    external_process = None
    try:
        # Emulate a pre-version-handshake daemon. The new client must detect it,
        # confirm there are no active tasks, shut it down, and start protocol 3.
        legacy_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        legacy_path = temp / "pman.sock"
        legacy_socket.bind(str(legacy_path))
        legacy_socket.listen(4)

        def serve_legacy() -> None:
            running = True
            while running:
                client, _ = legacy_socket.accept()
                line = bytearray()
                while b"\n" not in line:
                    line.extend(client.recv(4096))
                command = json.loads(bytes(line).partition(b"\n")[0]).get("cmd")
                if command == "ping":
                    reply = {"ok": True, "pid": os.getpid()}
                elif command == "list":
                    reply = {"ok": True, "tasks": []}
                elif command == "shutdown":
                    reply = {"ok": True}
                    running = False
                else:
                    reply = {"ok": False, "error": f"unknown command: {command}"}
                client.sendall((json.dumps(reply) + "\n").encode())
                client.close()
            legacy_socket.close()
            try:
                legacy_path.unlink()
            except OSError:
                pass

        legacy_thread = threading.Thread(target=serve_legacy, daemon=True)
        legacy_thread.start()
        assert json.loads(call(env, "list", "--json").stdout) == []
        legacy_thread.join(timeout=3)
        assert "protocol 3" in call(env, "doctor").stdout

        started = call(
            env,
            "run",
            "-n",
            "ticker",
            "--",
            sys.executable,
            "-u",
            "-c",
            "import time; [(print(f'tick {i}', flush=True), time.sleep(.05)) for i in range(100)]",
        )
        task_id = started.stdout.split()[1]
        wait_until(lambda: any(t["id"] == task_id for t in tasks(env)))

        call(env, "pause", task_id)
        assert next(t for t in tasks(env) if t["id"] == task_id)["status"] == "paused"
        call(env, "resume", task_id)
        assert next(t for t in tasks(env) if t["id"] == task_id)["status"] == "running"

        redirected = temp / "redirected.log"
        default_log = temp / "logs" / f"{task_id}.log"
        wait_until(lambda: default_log.exists() and "tick" in default_log.read_text())
        call(env, "redirect", task_id, str(redirected))
        wait_until(lambda: next(t for t in tasks(env) if t["id"] == task_id)["status"] == "exited")

        task = next(t for t in tasks(env) if t["id"] == task_id)
        assert task["exit_code"] == 0, task
        assert "tick" in default_log.read_text(), "initial log is empty"
        redirected_text = redirected.read_text()
        assert "tick" in redirected_text, "redirected log is empty"
        assert "tick 99" in redirected_text, "final output did not reach redirected log"

        long_job = call(env, "run", "-n", "sleeper", "--", "sleep", "30")
        long_id = long_job.stdout.split()[1]
        call(env, "stop", long_id)
        wait_until(lambda: next(t for t in tasks(env) if t["id"] == long_id)["status"] == "exited")
        assert next(t for t in tasks(env) if t["id"] == long_id)["exit_code"] < 0

        # Unmanaged/system-view processes can be controlled without adoption.
        external_process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        call(env, "signal-pid", str(external_process.pid), "stop")
        time.sleep(0.1)
        stat = (Path("/proc") / str(external_process.pid) / "stat").read_text()
        assert stat[stat.rfind(")") + 2] in {"T", "t"}
        call(env, "signal-pid", str(external_process.pid), "cont")
        call(env, "signal-pid", str(external_process.pid), "term")
        assert external_process.wait(timeout=3) < 0
        external_process = None

        # Reproduce the cross-shell workflow exactly: shell A starts and stops
        # the job, while a newly opened shell B discovers and adopts it.
        shell_pid, shell_fd = pty.fork()
        if shell_pid == 0:
            os.execvpe("bash", ["bash", "--noprofile", "--norc", "-i"], env)
        os.set_blocking(shell_fd, False)
        drain_pty(shell_fd)
        os.write(
            shell_fd,
            b"python3 -u -c 'import time; [(print(f\"ADOPT{i}\",flush=True),time.sleep(.12)) for i in range(30)]; print(\"ADOPT_DONE\",flush=True)'\n",
        )
        time.sleep(0.35)
        drain_pty(shell_fd, 0.1)
        os.write(shell_fd, b"\x1a")
        time.sleep(0.25)
        drain_pty(shell_fd)
        os.write(shell_fd, b"jobs -p\n")
        time.sleep(0.15)
        jobs_output = drain_pty(shell_fd).replace(b"\r", b"")
        target_pid = int(next(line for line in jobs_output.splitlines() if line.strip().isdigit()))
        ui_shell_pid, ui_shell_fd = pty.fork()
        if ui_shell_pid == 0:
            os.execvpe("bash", ["bash", "--noprofile", "--norc", "-i"], env)
        os.set_blocking(ui_shell_fd, False)
        drain_pty(ui_shell_fd)
        ui_command = " ".join(shlex.quote(part) for part in pman_command("tui"))
        os.write(ui_shell_fd, f"{ui_command}\n".encode())
        time.sleep(0.6)
        os.write(ui_shell_fd, b"/")
        time.sleep(0.15)
        os.write(ui_shell_fd, f"{target_pid}\n".encode())
        time.sleep(0.3)
        os.write(ui_shell_fd, b"i")
        time.sleep(0.7)
        os.write(ui_shell_fd, b"q")
        time.sleep(0.3)
        drain_pty(ui_shell_fd)

        wait_until(lambda: any(t.get("mode") == "adopted" for t in tasks(env)))
        adopted = next(t for t in tasks(env) if t.get("mode") == "adopted")
        adopted_log = Path(str(adopted["log_path"]))
        wait_until(lambda: adopted_log.exists() and "ADOPT_DONE" in adopted_log.read_text())
        wait_until(lambda: next(t for t in tasks(env) if t["id"] == adopted["id"])["status"] == "exited")
        try:
            os.kill(shell_pid, signal.SIGKILL)
        except OSError:
            pass
        os.close(shell_fd)
        shell_pid, shell_fd = None, None
        try:
            os.kill(ui_shell_pid, signal.SIGKILL)
        except OSError:
            pass
        os.close(ui_shell_fd)
        ui_shell_pid, ui_shell_fd = None, None

        # Smoke-test curses itself through a real pseudo-terminal. This catches
        # headless/TERM initialization errors that the CLI tests cannot see.
        master, slave = pty.openpty()
        tui_env = env.copy()
        tui_env.setdefault("TERM", "xterm-256color")
        tui_process = subprocess.Popen(
            pman_command("tui"),
            env=tui_env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        time.sleep(0.5)
        smoke_output = bytearray(drain_pty(master, 0.15))
        os.write(master, b"\t")  # USER -> ALL
        time.sleep(0.2)
        smoke_output.extend(drain_pty(master, 0.15))
        os.write(master, b"?")
        time.sleep(0.2)
        smoke_output.extend(drain_pty(master, 0.15))
        os.write(master, b"q")  # close help
        time.sleep(0.15)
        smoke_output.extend(drain_pty(master, 0.1))
        os.write(master, b"q")  # exit TUI
        tui_rc = tui_process.wait(timeout=5)
        output = smoke_output
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master)
        assert tui_rc == 0, output.decode(errors="replace")
        assert b"ALL" in output, "TUI did not render the all-system process view"
        assert b"help" in output and b"q/?" in output, "TUI did not render shortcut help:\n" + output.decode(errors="replace")

        print("integration test passed")
        return 0
    finally:
        if shell_pid:
            try:
                os.kill(shell_pid, signal.SIGKILL)
            except OSError:
                pass
        if shell_fd is not None:
            try:
                os.close(shell_fd)
            except OSError:
                pass
        if ui_shell_pid:
            try:
                os.kill(ui_shell_pid, signal.SIGKILL)
            except OSError:
                pass
        if ui_shell_fd is not None:
            try:
                os.close(ui_shell_fd)
            except OSError:
                pass
        if external_process and external_process.poll() is None:
            external_process.kill()
        call(env, "_shutdown", check=False)
        time.sleep(0.1)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
