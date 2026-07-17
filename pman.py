#!/usr/bin/env python3
"""pman - a small, dependency-free PTY process manager for headless Linux."""

from __future__ import annotations

import argparse
import curses
import errno
import fcntl
import json
import os
import pty
import pwd
import re
import selectors
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
import uuid
from pathlib import Path
from typing import Any


APP = "pman"
VERSION = "1.0.0"
PROTOCOL_VERSION = 3
DETACH_KEY = b"\x1d"  # Ctrl-]
PAUSE_KEY = b"\x1a"  # Ctrl-Z
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])")
STATE_NAMES = {
    "R": "running",
    "S": "sleeping",
    "D": "diskwait",
    "T": "stopped",
    "t": "tracing",
    "Z": "zombie",
    "I": "idle",
    "X": "dead",
    "x": "dead",
    "W": "paging",
}
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
USER_NAMES: dict[int, str] = {}


def bundled_resource(name: str) -> Path | None:
    """Return a resource embedded by a one-file PyInstaller build, if present."""
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    resource = Path(bundle_root) / "helpers" / name
    if not resource.is_file():
        return None
    try:
        resource.chmod(0o700)
    except OSError:
        pass
    return resource


def reptyr_binary() -> str | None:
    """Prefer the reptyr helper embedded in a standalone binary."""
    embedded = bundled_resource("reptyr")
    return str(embedded) if embedded else shutil.which("reptyr")


def paths() -> tuple[Path, Path, Path, Path]:
    override = os.environ.get("PMAN_HOME")
    if override:
        home = Path(override).expanduser().resolve()
        sock = home / "pman.sock"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
        home = base / APP
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        sock = Path(runtime) / "pman.sock" if runtime else home / "pman.sock"
    return home, sock, home / "state.json", home / "logs"


def prepare_paths() -> tuple[Path, Path, Path, Path]:
    home, sock, state, logs = paths()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home, sock, state, logs


def now_text(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def terminal_size(fd: int = 0) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(fd)
        return size.lines, size.columns
    except OSError:
        return 24, 80


def set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def process_info(pid: int) -> dict[str, Any] | None:
    """Read stable-enough process metadata from procfs without ps dependencies."""
    proc = Path("/proc") / str(pid)
    try:
        stat_text = (proc / "stat").read_text(errors="replace")
        right = stat_text.rfind(")")
        if right < 0:
            return None
        comm = stat_text[stat_text.find("(") + 1 : right]
        fields = stat_text[right + 2 :].split()
        state, ppid, pgrp, session, tty_nr = fields[:5]
        raw_cmd = (proc / "cmdline").read_bytes().rstrip(b"\0")
        argv = [part.decode(errors="replace") for part in raw_cmd.split(b"\0") if part]
        command = shlex.join(argv) if argv else f"[{comm}]"
        try:
            cwd = os.readlink(proc / "cwd")
        except OSError:
            cwd = "?"
        try:
            tty_name = os.readlink(proc / "fd" / "0")
            if tty_name.startswith("/dev/"):
                tty_name = tty_name[5:]
            elif not tty_name.startswith("pty:"):
                tty_name = "-"
        except OSError:
            tty_name = "?" if int(tty_nr) else "-"
        uid = proc.stat().st_uid
        if uid not in USER_NAMES:
            try:
                USER_NAMES[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                USER_NAMES[uid] = str(uid)
        user = USER_NAMES[uid]
        return {
            "pid": pid,
            "ppid": int(ppid),
            "pgrp": int(pgrp),
            "session": int(session),
            "tty_nr": int(tty_nr),
            "proc_state": state,
            "uid": uid,
            "user": user,
            "name": comm,
            "argv": argv,
            "command": command,
            "cwd": cwd,
            "tty": tty_name,
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "rss_bytes": max(0, int(fields[21])) * PAGE_SIZE,
            "threads": int(fields[17]),
            "start_ticks": int(fields[19]),
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError, OSError):
        return None


class ProcessScanner:
    def __init__(self) -> None:
        self.samples: dict[int, tuple[int, float]] = {}
        self.clock_ticks = float(os.sysconf("SC_CLK_TCK"))

    def scan(self, excluded_pids: set[int]) -> list[dict[str, Any]]:
        now = time.monotonic()
        found: list[dict[str, Any]] = []
        next_samples: dict[int, tuple[int, float]] = {}
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in excluded_pids:
                continue
            info = process_info(pid)
            if not info:
                continue
            ticks = int(info["cpu_ticks"])
            previous = self.samples.get(pid)
            cpu = 0.0
            if previous and now > previous[1] and ticks >= previous[0]:
                cpu = (ticks - previous[0]) / self.clock_ticks / (now - previous[1]) * 100.0
            next_samples[pid] = (ticks, now)
            state_name = STATE_NAMES.get(info["proc_state"], info["proc_state"])
            info.update(
                {
                    "id": f"@{pid}",
                    "status": f"{state_name[:7]}*",
                    "state_name": state_name,
                    "external": True,
                    "managed": False,
                    "cpu_percent": cpu,
                    "memory_mb": info["rss_bytes"] / (1024 * 1024),
                    "exit_code": None,
                    "started_at": None,
                    "ended_at": None,
                    "log_path": "not captured",
                    "adoptable": bool(info["tty_nr"] and pid > 2 and (info["uid"] == os.geteuid() or os.geteuid() == 0)),
                }
            )
            found.append(info)
        self.samples = next_samples
        return found


def ancestor_pids(pid: int) -> set[int]:
    result: set[int] = set()
    current = pid
    while current > 0 and current not in result:
        result.add(current)
        info = process_info(current)
        if not info:
            break
        current = int(info["ppid"])
    return result


class PmanDaemon:
    def __init__(self) -> None:
        self.home, self.sock_path, self.state_path, self.logs_dir = prepare_paths()
        self.selector = selectors.DefaultSelector()
        self.server: socket.socket | None = None
        self.tasks: dict[str, dict[str, Any]] = {}
        self.masters: dict[int, str] = {}
        self.logs: dict[str, Any] = {}
        self.clients: dict[socket.socket, dict[str, Any]] = {}
        self.running = True
        self._load_state()

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
            for task in data.get("tasks", []):
                # A new daemon cannot recover the old PTY master. Mark stale jobs
                # explicitly instead of pretending they are still controllable.
                if task.get("status") in {"running", "paused"}:
                    task["status"] = "lost"
                    task["ended_at"] = time.time()
                self.tasks[task["id"]] = task
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            self.tasks = {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        clean = sorted(self.tasks.values(), key=lambda item: item.get("started_at", 0))
        tmp.write_text(json.dumps({"version": 1, "tasks": clean}, ensure_ascii=False, indent=2))
        os.replace(tmp, self.state_path)

    def setup(self) -> None:
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                raise SystemExit(f"cannot remove stale socket: {self.sock_path}")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(self.sock_path))
        except OSError as exc:
            raise SystemExit(f"cannot bind socket {self.sock_path}: {exc}")
        os.chmod(self.sock_path, 0o600)
        self.server.listen(32)
        self.server.setblocking(False)
        self.selector.register(self.server, selectors.EVENT_READ, self._accept)
        (self.home / "daemon.pid").write_text(str(os.getpid()))

    def _accept(self, server: socket.socket) -> None:
        client, _ = server.accept()
        client.setblocking(False)
        self.clients[client] = {"mode": "command", "buffer": bytearray(), "task_id": None}
        self.selector.register(client, selectors.EVENT_READ, self._client_event)

    def _close_client(self, client: socket.socket) -> None:
        try:
            self.selector.unregister(client)
        except Exception:
            pass
        self.clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

    def _client_event(self, client: socket.socket) -> None:
        info = self.clients.get(client)
        if not info:
            return
        try:
            data = client.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_client(client)
            return
        if not data:
            self._close_client(client)
            return
        if info["mode"] == "attach":
            task_id = info["task_id"]
            task = self.tasks.get(task_id)
            master = task.get("master_fd") if task else None
            # master_fd is not serialized; look it up from the live map.
            if master is None:
                master = next((fd for fd, tid in self.masters.items() if tid == task_id), None)
            if master is not None:
                try:
                    os.write(master, data)
                except OSError:
                    pass
            return
        info["buffer"].extend(data)
        if b"\n" not in info["buffer"]:
            if len(info["buffer"]) > 1024 * 1024:
                self._reply(client, {"ok": False, "error": "request too large"}, close=True)
            return
        line, _, rest = bytes(info["buffer"]).partition(b"\n")
        info["buffer"] = bytearray(rest)
        try:
            request = json.loads(line)
            self._dispatch(client, request)
        except (ValueError, TypeError) as exc:
            self._reply(client, {"ok": False, "error": f"bad request: {exc}"}, close=True)

    def _reply(self, client: socket.socket, value: dict[str, Any], close: bool) -> None:
        try:
            client.setblocking(True)
            client.sendall(json_bytes(value))
            client.setblocking(False)
        except OSError:
            close = True
        if close:
            self._close_client(client)

    def _task_public(self, task: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in task.items() if key not in {"master_fd", "supervisor_pid"}}

    def _dispatch(self, client: socket.socket, req: dict[str, Any]) -> None:
        cmd = req.get("cmd")
        if cmd == "ping":
            self._reply(
                client,
                {
                    "ok": True,
                    "pid": os.getpid(),
                    "protocol": PROTOCOL_VERSION,
                    "features": ["adopt", "process-scan", "external-signals", "three-views"],
                },
                close=True,
            )
        elif cmd == "list":
            tasks = sorted(self.tasks.values(), key=lambda item: item.get("started_at", 0), reverse=True)
            self._reply(client, {"ok": True, "tasks": [self._task_public(t) for t in tasks]}, close=True)
        elif cmd == "start":
            try:
                task = self._start_task(req)
                self._reply(client, {"ok": True, "task": self._task_public(task)}, close=True)
            except Exception as exc:
                self._reply(client, {"ok": False, "error": str(exc)}, close=True)
        elif cmd == "adopt":
            try:
                task = self._adopt_task(req)
                self._reply(client, {"ok": True, "task": self._task_public(task)}, close=True)
            except Exception as exc:
                self._reply(client, {"ok": False, "error": str(exc)}, close=True)
        elif cmd == "signal":
            try:
                task = self._signal_task(str(req.get("id", "")), str(req.get("signal", "")))
                self._reply(client, {"ok": True, "task": self._task_public(task)}, close=True)
            except Exception as exc:
                self._reply(client, {"ok": False, "error": str(exc)}, close=True)
        elif cmd == "signal_external":
            try:
                info = self._signal_external(req)
                self._reply(client, {"ok": True, "process": info}, close=True)
            except Exception as exc:
                self._reply(client, {"ok": False, "error": str(exc)}, close=True)
        elif cmd == "set_log":
            try:
                task = self._set_log(str(req.get("id", "")), str(req.get("path", "")))
                self._reply(client, {"ok": True, "task": self._task_public(task)}, close=True)
            except Exception as exc:
                self._reply(client, {"ok": False, "error": str(exc)}, close=True)
        elif cmd == "remove":
            task_id = str(req.get("id", ""))
            task = self.tasks.get(task_id)
            if not task:
                self._reply(client, {"ok": False, "error": "task not found"}, close=True)
            elif task.get("status") in {"running", "paused"}:
                self._reply(client, {"ok": False, "error": "stop the task before removing it"}, close=True)
            else:
                self.tasks.pop(task_id, None)
                self._save_state()
                self._reply(client, {"ok": True}, close=True)
        elif cmd == "attach":
            task_id = str(req.get("id", ""))
            task = self.tasks.get(task_id)
            live_fd = next((fd for fd, tid in self.masters.items() if tid == task_id), None)
            if not task or live_fd is None or task.get("status") not in {"running", "paused"}:
                self._reply(client, {"ok": False, "error": "task is not attachable"}, close=True)
            else:
                set_winsize(live_fd, int(req.get("rows", 24)), int(req.get("cols", 80)))
                self._reply(client, {"ok": True, "task": self._task_public(task)}, close=False)
                if client in self.clients:
                    self.clients[client]["mode"] = "attach"
                    self.clients[client]["task_id"] = task_id
        elif cmd == "shutdown":
            self._reply(client, {"ok": True}, close=True)
            self.running = False
        else:
            self._reply(client, {"ok": False, "error": f"unknown command: {cmd}"}, close=True)

    def _start_task(self, req: dict[str, Any]) -> dict[str, Any]:
        argv = req.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("argv must be a non-empty string list")
        cwd = Path(str(req.get("cwd") or os.getcwd())).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"working directory does not exist: {cwd}")
        task_id = uuid.uuid4().hex[:8]
        name = str(req.get("name") or Path(argv[0]).name)[:80]
        requested_log = req.get("log_path")
        log_path = Path(str(requested_log)).expanduser().resolve() if requested_log else self.logs_dir / f"{task_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab", buffering=0)
        env = os.environ.copy()
        overrides = req.get("env") or {}
        if isinstance(overrides, dict):
            env.update({str(k): str(v) for k, v in overrides.items()})

        pid, master = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except BaseException as exc:
                os.write(2, f"pman: cannot start {argv[0]}: {exc}\r\n".encode())
                os._exit(127)

        os.set_blocking(master, False)
        task = {
            "id": task_id,
            "name": name,
            "argv": argv,
            "command": shlex.join(argv),
            "cwd": str(cwd),
            "pid": pid,
            "supervisor_pid": pid,
            "status": "running",
            "exit_code": None,
            "started_at": time.time(),
            "ended_at": None,
            "log_path": str(log_path),
        }
        self.tasks[task_id] = task
        self.masters[master] = task_id
        self.logs[task_id] = log_handle
        self.selector.register(master, selectors.EVENT_READ, self._master_event)
        self._save_state()
        return task

    def _adopt_task(self, req: dict[str, Any]) -> dict[str, Any]:
        reptyr = reptyr_binary()
        if not reptyr:
            raise RuntimeError("reptyr is required (Debian/Ubuntu: apt install reptyr)")
        try:
            target_pid = int(req.get("pid"))
        except (TypeError, ValueError):
            raise ValueError("a numeric target pid is required")
        if target_pid <= 2 or target_pid in {os.getpid(), os.getppid()}:
            raise ValueError("refusing to adopt a system or manager process")
        info = process_info(target_pid)
        if not info:
            raise ValueError("target process no longer exists")
        if info["uid"] != os.geteuid() and os.geteuid() != 0:
            raise PermissionError("target belongs to another user")
        if any(t.get("adopted_pid") == target_pid and t.get("status") in {"running", "paused"} for t in self.tasks.values()):
            raise ValueError("target is already managed")
        if info["tty_nr"] == 0:
            raise ValueError("target has no controlling terminal to adopt")

        target_pgid = int(info["pgrp"])
        was_stopped = info["proc_state"] in {"T", "t"}
        if not was_stopped:
            os.killpg(target_pgid, signal.SIGSTOP)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                current = process_info(target_pid)
                if current and current["proc_state"] in {"T", "t"}:
                    break
                time.sleep(0.02)

        task_id = uuid.uuid4().hex[:8]
        name = str(req.get("name") or info["name"])[:80]
        requested_log = req.get("log_path")
        log_path = Path(str(requested_log)).expanduser().resolve() if requested_log else self.logs_dir / f"{task_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab", buffering=0)

        helper_pid, master = pty.fork()
        if helper_pid == 0:
            try:
                os.execv(reptyr, [reptyr, str(target_pid)])
            except BaseException as exc:
                os.write(2, f"pman: cannot run reptyr: {exc}\r\n".encode())
                os._exit(127)

        os.set_blocking(master, False)
        task = {
            "id": task_id,
            "name": name,
            "argv": info["argv"],
            "command": info["command"],
            "cwd": info["cwd"],
            "pid": target_pid,
            "supervisor_pid": helper_pid,
            "adopted_pid": target_pid,
            "adopted_pgid": target_pgid,
            "mode": "adopted",
            "status": "paused",
            "exit_code": None,
            "started_at": time.time(),
            "ended_at": None,
            "log_path": str(log_path),
        }
        self.tasks[task_id] = task
        self.masters[master] = task_id
        self.logs[task_id] = log_handle
        self.selector.register(master, selectors.EVENT_READ, self._master_event)
        self._save_state()

        # reptyr has no success handshake: on success it stays alive as the PTY
        # bridge; common permission/process-group failures exit immediately.
        time.sleep(0.25)
        waited, status = os.waitpid(helper_pid, os.WNOHANG)
        if waited:
            error_data = bytearray()
            while True:
                try:
                    chunk = os.read(master, 65536)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                error_data.extend(chunk)
            if error_data:
                log_handle.write(error_data)
            self._close_master(master)
            self.tasks.pop(task_id, None)
            self._save_state()
            if not was_stopped:
                try:
                    os.killpg(target_pgid, signal.SIGCONT)
                except OSError:
                    pass
            detail = ANSI_RE.sub("", error_data.decode(errors="replace")).strip()
            raise RuntimeError(detail or f"reptyr failed with status {status}")

        os.killpg(target_pgid, signal.SIGCONT)
        task["status"] = "running"
        self._save_state()
        return task

    def _signal_task(self, task_id: str, sig_name: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError("task not found")
        mapping = {
            "stop": signal.SIGSTOP,
            "cont": signal.SIGCONT,
            "term": signal.SIGTERM,
            "kill": signal.SIGKILL,
            "int": signal.SIGINT,
            "hup": signal.SIGHUP,
        }
        sig = mapping.get(sig_name.lower())
        if sig is None:
            raise ValueError(f"unsupported signal: {sig_name}")
        if task.get("status") not in {"running", "paused"}:
            raise ValueError("task is not running")
        signal_group = int(task.get("adopted_pgid") or task["pid"])
        try:
            os.killpg(signal_group, sig)
        except ProcessLookupError:
            self._reap_children()
            raise ValueError("process no longer exists")
        if sig == signal.SIGSTOP:
            task["status"] = "paused"
        elif sig == signal.SIGCONT:
            task["status"] = "running"
        self._save_state()
        return task

    def _signal_external(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            pid = int(req.get("pid"))
        except (TypeError, ValueError):
            raise ValueError("a numeric pid is required")
        protected = ancestor_pids(os.getpid()) | {1, 2}
        protected.update(int(task.get("supervisor_pid") or -1) for task in self.tasks.values())
        protected.update(int(task.get("pid") or -1) for task in self.tasks.values() if task.get("status") in {"running", "paused"})
        if pid in protected or pid <= 2:
            raise PermissionError("refusing to signal a protected pman/system process")
        info = process_info(pid)
        if not info:
            raise ValueError("process no longer exists")
        if info["uid"] != os.geteuid() and os.geteuid() != 0:
            raise PermissionError("process belongs to another user")
        mapping = {
            "stop": signal.SIGSTOP,
            "cont": signal.SIGCONT,
            "term": signal.SIGTERM,
            "kill": signal.SIGKILL,
            "int": signal.SIGINT,
            "hup": signal.SIGHUP,
        }
        sig_name = str(req.get("signal", "")).lower()
        sig = mapping.get(sig_name)
        if sig is None:
            raise ValueError(f"unsupported signal: {sig_name}")
        os.kill(pid, sig)
        return {"pid": pid, "signal": sig_name, "name": info["name"]}

    def _set_log(self, task_id: str, new_path: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError("task not found")
        if not new_path:
            raise ValueError("log path cannot be empty")
        path = Path(new_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        new_handle = open(path, "ab", buffering=0)
        old = self.logs.get(task_id)
        self.logs[task_id] = new_handle
        task["log_path"] = str(path)
        self._save_state()
        if old:
            old.close()
        return task

    def _master_event(self, master: int) -> None:
        task_id = self.masters.get(master)
        if not task_id:
            return
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno not in {errno.EIO, errno.EBADF}:
                return
            data = b""
        if not data:
            self._close_master(master)
            self._reap_children()
            return
        handle = self.logs.get(task_id)
        if handle:
            try:
                handle.write(data)
            except OSError:
                pass
        for client, info in list(self.clients.items()):
            if info.get("mode") == "attach" and info.get("task_id") == task_id:
                try:
                    client.send(data)
                except (BlockingIOError, InterruptedError):
                    # The complete output remains in the log even if a slow live
                    # viewer misses a screenful.
                    pass
                except OSError:
                    self._close_client(client)

    def _close_master(self, master: int) -> None:
        task_id = self.masters.pop(master, None)
        try:
            self.selector.unregister(master)
        except Exception:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        if task_id:
            handle = self.logs.pop(task_id, None)
            if handle:
                handle.close()

    def _reap_children(self) -> None:
        for task in self.tasks.values():
            if task.get("status") not in {"running", "paused"}:
                continue
            pid = int(task.get("supervisor_pid") or task["pid"])
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = pid, 0
            if not waited:
                continue
            if os.WIFEXITED(status):
                task["exit_code"] = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                task["exit_code"] = -os.WTERMSIG(status)
            else:
                continue
            task["status"] = "exited"
            task["ended_at"] = time.time()
            for fd, tid in list(self.masters.items()):
                if tid == task["id"]:
                    self._close_master(fd)
            self._save_state()

    def run(self) -> None:
        self.setup()
        try:
            while self.running:
                for key, _ in self.selector.select(timeout=0.4):
                    callback = key.data
                    callback(key.fileobj)
                self._reap_children()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        # Managed processes deliberately survive client/TUI exits, but not a
        # daemon shutdown. Gracefully terminate them to avoid orphaned PTYs.
        for task in self.tasks.values():
            if task.get("status") in {"running", "paused"}:
                try:
                    os.killpg(int(task.get("adopted_pgid") or task["pid"]), signal.SIGTERM)
                except OSError:
                    pass
        for client in list(self.clients):
            self._close_client(client)
        for master in list(self.masters):
            self._close_master(master)
        if self.server:
            try:
                self.selector.unregister(self.server)
            except Exception:
                pass
            self.server.close()
        try:
            self.sock_path.unlink()
        except OSError:
            pass
        try:
            (self.home / "daemon.pid").unlink()
        except OSError:
            pass


def raw_request(payload: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    _, sock_path, _, _ = prepare_paths()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(sock_path))
    client.sendall(json_bytes(payload))
    data = bytearray()
    while b"\n" not in data:
        chunk = client.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
    client.close()
    if not data:
        raise RuntimeError("daemon returned no response")
    result = json.loads(bytes(data).partition(b"\n")[0])
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown daemon error"))
    return result


def daemon_info() -> dict[str, Any] | None:
    try:
        return raw_request({"cmd": "ping"}, timeout=0.25)
    except (OSError, RuntimeError, ValueError):
        return None


def daemon_alive() -> bool:
    return daemon_info() is not None


def ensure_daemon() -> None:
    info = daemon_info()
    if info and info.get("protocol") == PROTOCOL_VERSION:
        return
    if info:
        # A daemon owns live PTY masters, so killing an old daemon with active
        # jobs would also disrupt those jobs. Upgrade automatically only when
        # it is safe; otherwise give an explicit recovery path.
        try:
            old_tasks = raw_request({"cmd": "list"}, timeout=1.0).get("tasks", [])
        except Exception:
            old_tasks = []
        active = [task for task in old_tasks if task.get("status") in {"running", "paused"}]
        if active:
            ids = ", ".join(str(task.get("id", "?")) for task in active[:5])
            raise RuntimeError(
                f"old pman daemon protocol {info.get('protocol', 1)} has active jobs ({ids}); "
                "finish/stop them with the old daemon, then reopen pman"
            )
        try:
            raw_request({"cmd": "shutdown"}, timeout=1.0)
        except Exception:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and daemon_info():
            time.sleep(0.05)
    home, sock_path, _, _ = prepare_paths()
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass
    daemon_log = open(home / "daemon.log", "ab", buffering=0)
    daemon_command = [sys.executable, "_daemon"] if getattr(sys, "frozen", False) else [
        sys.executable,
        str(Path(__file__).resolve()),
        "_daemon",
    ]
    subprocess.Popen(
        daemon_command,
        stdin=subprocess.DEVNULL,
        stdout=daemon_log,
        stderr=daemon_log,
        start_new_session=True,
        close_fds=True,
    )
    daemon_log.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if daemon_alive():
            return
        time.sleep(0.05)
    raise RuntimeError(f"could not start daemon; see {home / 'daemon.log'}")


def request(payload: dict[str, Any], start: bool = True) -> dict[str, Any]:
    if start:
        ensure_daemon()
    return raw_request(payload)


def resolve_task_id(fragment: str) -> str:
    tasks = request({"cmd": "list"})["tasks"]
    exact = [task["id"] for task in tasks if task["id"] == fragment]
    if exact:
        return exact[0]
    matches = [task["id"] for task in tasks if task["id"].startswith(fragment) or task["name"] == fragment]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"task not found: {fragment}")
    raise RuntimeError(f"ambiguous task: {fragment}")


def attach(task_fragment: str) -> None:
    task_id = resolve_task_id(task_fragment)
    _, sock_path, _, _ = prepare_paths()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(sock_path))
    rows, cols = terminal_size()
    client.sendall(json_bytes({"cmd": "attach", "id": task_id, "rows": rows, "cols": cols}))
    response = bytearray()
    while b"\n" not in response:
        chunk = client.recv(65536)
        if not chunk:
            raise RuntimeError("daemon disconnected during attach")
        response.extend(chunk)
    line, _, leftover = bytes(response).partition(b"\n")
    result = json.loads(line)
    if not result.get("ok"):
        client.close()
        raise RuntimeError(result.get("error", "attach failed"))
    if not os.isatty(0):
        client.close()
        raise RuntimeError("attach requires a terminal")

    saved = termios.tcgetattr(0)
    client.setblocking(False)
    tty.setraw(0)
    pause_after_detach = False
    try:
        os.write(1, f"\r\n[pman attached to {task_id}; Ctrl-] detaches, Ctrl-Z pauses]\r\n".encode())
        if leftover:
            os.write(1, leftover)
        while True:
            readable, _, _ = __import__("select").select([0, client], [], [])
            if client in readable:
                try:
                    data = client.recv(65536)
                except BlockingIOError:
                    data = b""
                if not data:
                    break
                os.write(1, data)
            if 0 in readable:
                data = os.read(0, 4096)
                if not data:
                    break
                detach_pos = data.find(DETACH_KEY)
                pause_pos = data.find(PAUSE_KEY)
                positions = [pos for pos in (detach_pos, pause_pos) if pos >= 0]
                if positions:
                    pos = min(positions)
                    if pos:
                        client.sendall(data[:pos])
                    pause_after_detach = pause_pos == pos
                    break
                client.sendall(data)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        client.close()
    if pause_after_detach:
        try:
            request({"cmd": "signal", "id": task_id, "signal": "stop"})
            os.write(1, f"\r\n[pman detached; task {task_id} is paused]\r\n".encode())
        except RuntimeError as exc:
            os.write(1, f"\r\n[pman detached; could not pause: {exc}]\r\n".encode())
    else:
        os.write(1, f"\r\n[pman detached; task {task_id} continues in background]\r\n".encode())


def short_command(task: dict[str, Any], width: int) -> str:
    text = task.get("command") or ""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def set_cursor(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


def prompt(stdscr: Any, label: str, default: str = "") -> str | None:
    rows, cols = stdscr.getmaxyx()
    curses.echo()
    set_cursor(1)
    stdscr.timeout(-1)
    try:
        stdscr.move(rows - 1, 0)
        stdscr.clrtoeol()
        shown = f"{label}{default}"
        stdscr.addnstr(rows - 1, 0, shown, cols - 1)
        stdscr.refresh()
        raw = stdscr.getstr(rows - 1, min(len(label), cols - 1), max(1, cols - len(label) - 1))
        value = raw.decode(errors="replace")
        return value if value else default
    except (KeyboardInterrupt, curses.error):
        return None
    finally:
        curses.noecho()
        set_cursor(0)
        stdscr.timeout(250)


def view_log(stdscr: Any, task: dict[str, Any]) -> None:
    path = Path(task["log_path"])
    offset = 0
    while True:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 250_000))
                text = handle.read().decode(errors="replace")
        except OSError as exc:
            text = f"Cannot read {path}: {exc}"
        text = ANSI_RE.sub("", text).replace("\r", "")
        rows, cols = stdscr.getmaxyx()
        wrapped: list[str] = []
        for line in text.splitlines() or [""]:
            wrapped.extend([line[i : i + max(1, cols - 1)] for i in range(0, max(1, len(line)), max(1, cols - 1))])
        visible = max(1, rows - 2)
        max_offset = max(0, len(wrapped) - visible)
        offset = min(offset, max_offset)
        start = max(0, len(wrapped) - visible - offset)
        stdscr.erase()
        title = f" LOG {task['id']}  {path}  (↑↓/PgUp/PgDn, g=end, q=back) "
        stdscr.addnstr(0, 0, title, cols - 1, curses.A_REVERSE)
        for y, line in enumerate(wrapped[start : start + visible], 1):
            try:
                stdscr.addnstr(y, 0, line, cols - 1)
            except curses.error:
                pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key == curses.KEY_UP:
            offset = min(max_offset, offset + 1)
        elif key == curses.KEY_DOWN:
            offset = max(0, offset - 1)
        elif key == curses.KEY_PPAGE:
            offset = min(max_offset, offset + visible)
        elif key == curses.KEY_NPAGE:
            offset = max(0, offset - visible)
        elif key == ord("g"):
            offset = 0


def view_help(stdscr: Any) -> None:
    lines = [
        "pman keyboard help",
        "",
        "Navigation",
        "  Up/Down, PgUp/PgDn, Home/End   select a process",
        "  Tab                            MANAGED -> USER -> ALL view",
        "  /                              search PID/user/name/command",
        "  c                              clear search",
        "  s                              cycle CPU/memory/PID/name sort",
        "",
        "Process actions",
        "  i                              adopt a TTY process and capture output",
        "  o                              adopt/redirect future output to a file",
        "  Enter or a                     attach in foreground",
        "  Ctrl-]                         detach; process keeps running",
        "  Ctrl-Z                         detach and pause an attached task",
        "  Space                          pause or continue",
        "  l                              view a managed task log",
        "  t                              SIGTERM (external processes ask first)",
        "  k                              SIGKILL (always asks first)",
        "  n                              start a new managed command",
        "  d                              remove a finished pman record",
        "",
        "  q or Esc                       close help / quit main TUI",
        "",
        "External processes end in *. No-TTY processes support signals but cannot",
        "have historical stdout/stderr captured. Protected processes reject actions.",
    ]
    offset = 0
    while True:
        rows, cols = stdscr.getmaxyx()
        visible = max(1, rows - 2)
        max_offset = max(0, len(lines) - visible)
        offset = min(offset, max_offset)
        stdscr.erase()
        stdscr.addnstr(0, 0, " pman help · q/? closes · Up/Down scrolls ".ljust(cols), cols, curses.A_REVERSE | curses.A_BOLD)
        for y, line in enumerate(lines[offset : offset + visible], 1):
            try:
                attr = curses.A_BOLD if line in {"Navigation", "Process actions", "pman keyboard help"} else curses.A_NORMAL
                stdscr.addnstr(y, 0, line, cols - 1, attr)
            except curses.error:
                pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), ord("?"), 27):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max_offset, offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - visible)
        elif key == curses.KEY_NPAGE:
            offset = min(max_offset, offset + visible)


def tui(stdscr: Any, shell_pid: int) -> None:
    set_cursor(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    stdscr.keypad(True)
    stdscr.timeout(250)
    scanner = ProcessScanner()
    view_modes = ["managed", "user", "all"]
    view_mode = "user"
    sort_modes = ["cpu", "memory", "pid", "name"]
    sort_mode = "cpu"
    search_text = ""
    selected = 0
    selected_token: str | None = None
    cached_scan: list[dict[str, Any]] = []
    last_scan = 0.0
    tasks: list[dict[str, Any]] = []
    message = "Tab switches MANAGED/USER/ALL; stopped terminal jobs sort to the top."
    protected = ancestor_pids(os.getpid()) | {1, 2, shell_pid}
    try:
        daemon_pid = int(request({"cmd": "ping"})["pid"])
        protected.add(daemon_pid)
    except Exception:
        daemon_pid = -1

    while True:
        old_token = selected_token
        try:
            managed = request({"cmd": "list"})["tasks"]
            now = time.monotonic()
            if now - last_scan >= 0.75:
                # Show protected processes (including PID 1 and the current
                # shell) in ALL view, but never offer destructive actions for
                # them. Only hide the TUI client and daemon implementation.
                cached_scan = scanner.scan({os.getpid(), daemon_pid})
                last_scan = now

            process_by_pid = {int(proc["pid"]): proc for proc in cached_scan}
            decorated_managed: list[dict[str, Any]] = []
            managed_pids: set[int] = set()
            for original in managed:
                task = dict(original)
                pid = int(task.get("pid") or -1)
                if task.get("status") in {"running", "paused"}:
                    managed_pids.add(pid)
                live = process_by_pid.pop(pid, None)
                task.update(
                    {
                        "managed": True,
                        "external": False,
                        "cpu_percent": live.get("cpu_percent", 0.0) if live else 0.0,
                        "memory_mb": live.get("memory_mb", 0.0) if live else 0.0,
                        "user": live.get("user", str(os.geteuid())) if live else str(os.geteuid()),
                        "uid": live.get("uid", os.geteuid()) if live else os.geteuid(),
                        "tty": live.get("tty", "-") if live else "-",
                        "threads": live.get("threads", 0) if live else 0,
                        "ppid": live.get("ppid", "-") if live else "-",
                        "pgrp": live.get("pgrp", pid) if live else pid,
                        "state_name": "stopped" if task.get("status") == "paused" else task.get("status", "?"),
                        "protected": False,
                        "adoptable": False,
                    }
                )
                decorated_managed.append(task)

            external = []
            for proc in process_by_pid.values():
                pid = int(proc["pid"])
                if pid in managed_pids or proc.get("ppid") == daemon_pid:
                    continue
                proc["protected"] = pid in protected
                proc["same_shell_job"] = proc.get("ppid") == shell_pid
                if proc["protected"]:
                    proc["adoptable"] = False
                external.append(proc)

            if view_mode == "managed":
                visible = decorated_managed
            elif view_mode == "user":
                visible = decorated_managed + [proc for proc in external if int(proc["uid"]) == os.geteuid()]
            else:
                visible = decorated_managed + external

            if search_text:
                needle = search_text.casefold()
                visible = [
                    task
                    for task in visible
                    if needle in " ".join(
                        str(task.get(key, "")) for key in ("id", "pid", "user", "name", "command", "status", "tty")
                    ).casefold()
                ]

            def task_sort_key(task: dict[str, Any]) -> tuple[Any, ...]:
                stopped = task.get("state_name") in {"stopped", "tracing"} or task.get("status") == "paused"
                if task.get("same_shell_job") and stopped:
                    priority = 0
                elif task.get("external") and stopped:
                    priority = 1
                elif task.get("managed") and task.get("status") in {"running", "paused"}:
                    priority = 2
                else:
                    priority = 3
                if sort_mode == "cpu":
                    value: Any = -float(task.get("cpu_percent", 0.0))
                elif sort_mode == "memory":
                    value = -float(task.get("memory_mb", 0.0))
                elif sort_mode == "name":
                    value = str(task.get("name", "")).casefold()
                else:
                    value = int(task.get("pid") or 0)
                return priority, value, int(task.get("pid") or 0)

            tasks = sorted(visible, key=task_sort_key)
        except Exception as exc:
            tasks = []
            message = str(exc)

        if old_token:
            match = next((i for i, task in enumerate(tasks) if task["id"] == old_token), None)
            if match is not None:
                selected = match
        selected = max(0, min(selected, max(0, len(tasks) - 1)))
        selected_token = tasks[selected]["id"] if tasks else None

        rows, cols = stdscr.getmaxyx()
        stdscr.erase()
        title = f" pman · {view_mode.upper()} · sort={sort_mode} · {len(tasks)} processes"
        if search_text:
            title += f" · filter={search_text}"
        try:
            stdscr.addnstr(0, 0, title.ljust(cols), cols, curses.A_REVERSE | curses.A_BOLD)
            help_lines = [
                "NAV  Up/Down select  PgUp/PgDn page  Tab view  / search  c clear  s sort  ? full help",
                "PROC i adopt/output  o redirect  Enter/a attach  Space pause/resume  l log",
                "CTRL t term  k kill  n new  d remove  q quit  attached: Ctrl-] detach, Ctrl-Z pause",
            ]
            for help_y, help_line in enumerate(help_lines, 1):
                stdscr.addnstr(help_y, 0, help_line, cols - 1, curses.A_DIM)
            if cols >= 120:
                header = f"{'ID':9} {'PID':>7} {'USER':10} {'STATE':9} {'CPU%':>6} {'MEM':>7} {'TTY':10} {'NAME':16} COMMAND"
            elif cols >= 90:
                header = f"{'ID':9} {'PID':>7} {'USER':9} {'STATE':9} {'CPU%':>6} {'MEM':>7} {'NAME':14} COMMAND"
            else:
                header = f"{'ID':9} {'PID':>7} {'STATE':9} {'CPU%':>6} NAME"
            stdscr.addnstr(5, 0, header, cols - 1, curses.A_BOLD | curses.A_UNDERLINE)
            max_items = max(0, rows - 9)
            view_start = max(0, selected - max_items + 1)
            for screen_y, (index, task) in enumerate(enumerate(tasks[view_start : view_start + max_items], view_start), 6):
                status = str(task.get("status", "?"))
                pid = str(task.get("pid") or "-")
                cpu = float(task.get("cpu_percent", 0.0))
                memory = float(task.get("memory_mb", 0.0))
                mem_text = f"{memory:.0f}M" if memory < 10240 else f"{memory / 1024:.1f}G"
                if cols >= 120:
                    line = (
                        f"{task['id'][:9]:9} {pid:>7} {str(task.get('user', '?'))[:10]:10} {status[:9]:9} "
                        f"{cpu:6.1f} {mem_text:>7} {str(task.get('tty', '-'))[:10]:10} {task['name'][:16]:16} "
                        f"{short_command(task, max(5, cols - 94))}"
                    )
                elif cols >= 90:
                    line = (
                        f"{task['id'][:9]:9} {pid:>7} {str(task.get('user', '?'))[:9]:9} {status[:9]:9} "
                        f"{cpu:6.1f} {mem_text:>7} {task['name'][:14]:14} {short_command(task, max(5, cols - 68))}"
                    )
                else:
                    line = f"{task['id'][:9]:9} {pid:>7} {status[:9]:9} {cpu:6.1f} {task['name']}"
                attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
                if task.get("state_name") in {"stopped", "tracing"} or status == "paused":
                    attr |= curses.A_BOLD
                elif task.get("state_name") in {"idle", "sleeping"} and index != selected:
                    attr |= curses.A_DIM
                stdscr.addnstr(screen_y, 0, line.ljust(cols - 1), cols - 1, attr)

            if tasks:
                task = tasks[selected]
                if task.get("external"):
                    action_hint = "i=adopt/output" if task.get("adoptable") else "no TTY: signals only"
                    if task.get("protected"):
                        action_hint = "protected"
                    detail = (
                        f"{action_hint} · PPID={task.get('ppid')} PGRP={task.get('pgrp')} threads={task.get('threads')} "
                        f"tty={task.get('tty')} cwd={task.get('cwd')}"
                    )
                else:
                    detail = f"managed · log={task['log_path']} · cwd={task['cwd']} · started={now_text(task.get('started_at'))}"
            else:
                detail = "No matching processes. Tab changes view, / searches, c clears the filter."
            stdscr.addnstr(rows - 2, 0, detail, cols - 1, curses.A_DIM)
            stdscr.addnstr(rows - 1, 0, message, cols - 1)
            stdscr.refresh()
        except curses.error:
            pass

        key = stdscr.getch()
        if key == -1:
            continue
        message = ""
        if key in (ord("q"), 27):
            return
        if key in (ord("?"), ord("h")):
            view_help(stdscr)
            continue
        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
            selected_token = tasks[selected]["id"] if tasks else None
        elif key == curses.KEY_DOWN:
            selected = min(max(0, len(tasks) - 1), selected + 1)
            selected_token = tasks[selected]["id"] if tasks else None
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - max(1, rows - 9))
            selected_token = tasks[selected]["id"] if tasks else None
        elif key == curses.KEY_NPAGE:
            selected = min(max(0, len(tasks) - 1), selected + max(1, rows - 9))
            selected_token = tasks[selected]["id"] if tasks else None
        elif key == curses.KEY_HOME:
            selected = 0
            selected_token = tasks[0]["id"] if tasks else None
        elif key == curses.KEY_END:
            selected = max(0, len(tasks) - 1)
            selected_token = tasks[selected]["id"] if tasks else None
        elif key == 9:
            view_mode = view_modes[(view_modes.index(view_mode) + 1) % len(view_modes)]
            selected = 0
            selected_token = None
            message = f"view: {view_mode.upper()}"
        elif key == ord("s"):
            sort_mode = sort_modes[(sort_modes.index(sort_mode) + 1) % len(sort_modes)]
            selected = 0
            selected_token = None
            message = f"sort: {sort_mode}"
        elif key == ord("/"):
            value = prompt(stdscr, "filter (pid/user/name/command): ", search_text)
            if value is not None:
                search_text = value
                selected = 0
                selected_token = None
        elif key == ord("c"):
            search_text = ""
            selected = 0
            selected_token = None
            message = "filter cleared"
        elif key == ord("n"):
            line = prompt(stdscr, "command: ")
            if line:
                try:
                    argv = shlex.split(line)
                    result = request({"cmd": "start", "argv": argv, "cwd": os.getcwd()})
                    message = f"started {result['task']['id']}"
                except Exception as exc:
                    message = str(exc)
        elif key == ord("i") and tasks:
            task = tasks[selected]
            if not task.get("external"):
                message = "task is already managed"
            elif task.get("protected"):
                message = "this pman/system process is protected"
            elif not task.get("adoptable"):
                message = "cannot adopt: process has no accessible controlling TTY"
            else:
                try:
                    result = request({"cmd": "adopt", "pid": task["pid"], "name": task["name"]})["task"]
                    message = f"adopted {task['pid']} as {result['id']} -> {result['log_path']}"
                    selected_token = result["id"]
                except Exception as exc:
                    message = str(exc)
        elif key in (10, 13, ord("a")) and tasks:
            task = tasks[selected]
            if task.get("external"):
                if task.get("protected") or not task.get("adoptable"):
                    message = "process must have an accessible TTY before it can be attached"
                    continue
                try:
                    task = request({"cmd": "adopt", "pid": task["pid"], "name": task["name"]})["task"]
                except Exception as exc:
                    message = str(exc)
                    continue
            if task["status"] not in {"running", "paused"}:
                message = "finished tasks cannot be attached"
            else:
                curses.def_prog_mode()
                curses.endwin()
                try:
                    attach(task["id"])
                except Exception as exc:
                    message = str(exc)
                finally:
                    curses.reset_prog_mode()
                    stdscr.refresh()
        elif key == ord(" ") and tasks:
            task = tasks[selected]
            if task.get("protected"):
                message = "protected process: signal refused"
                continue
            try:
                if task.get("external"):
                    action = "cont" if task.get("state_name") in {"stopped", "tracing"} else "stop"
                    request({"cmd": "signal_external", "pid": task["pid"], "signal": action})
                else:
                    action = "cont" if task["status"] == "paused" else "stop"
                    request({"cmd": "signal", "id": task["id"], "signal": action})
                message = "continued" if action == "cont" else "paused"
                last_scan = 0
            except Exception as exc:
                message = str(exc)
        elif key == ord("o") and tasks:
            task = tasks[selected]
            if task.get("external") and (task.get("protected") or not task.get("adoptable")):
                message = "output capture requires an accessible controlling TTY"
                continue
            default_log = "" if task.get("external") else task["log_path"]
            value = prompt(stdscr, "new log path: ", default_log)
            if value:
                try:
                    path = str(Path(value).expanduser().resolve())
                    if task.get("external"):
                        result = request({"cmd": "adopt", "pid": task["pid"], "name": task["name"], "log_path": path})["task"]
                        message = f"adopted {task['pid']} as {result['id']} -> {path}"
                    else:
                        request({"cmd": "set_log", "id": task["id"], "path": path})
                        message = f"future output -> {path}"
                except Exception as exc:
                    message = str(exc)
        elif key == ord("l") and tasks:
            if tasks[selected].get("external"):
                message = "external output is unavailable until the process is adopted"
            else:
                view_log(stdscr, tasks[selected])
        elif key == ord("t") and tasks:
            task = tasks[selected]
            if task.get("protected"):
                message = "protected process: SIGTERM refused"
                continue
            try:
                if task.get("external"):
                    answer = prompt(stdscr, f"send SIGTERM to PID {task['pid']} ({task['name']})? [y/N] ")
                    if not answer or answer.lower() != "y":
                        message = "cancelled"
                        continue
                    request({"cmd": "signal_external", "pid": task["pid"], "signal": "term"})
                else:
                    request({"cmd": "signal", "id": task["id"], "signal": "term"})
                message = "SIGTERM sent"
                last_scan = 0
            except Exception as exc:
                message = str(exc)
        elif key == ord("k") and tasks:
            task = tasks[selected]
            if task.get("protected"):
                message = "protected process: SIGKILL refused"
                continue
            answer = prompt(stdscr, f"kill PID {task['pid']} ({task['name']}) with SIGKILL? [y/N] ")
            if answer and answer.lower() == "y":
                try:
                    if task.get("external"):
                        request({"cmd": "signal_external", "pid": task["pid"], "signal": "kill"})
                    else:
                        request({"cmd": "signal", "id": task["id"], "signal": "kill"})
                    message = "SIGKILL sent"
                    last_scan = 0
                except Exception as exc:
                    message = str(exc)
        elif key == ord("d") and tasks:
            if tasks[selected].get("external"):
                message = "external processes have no pman record"
            else:
                try:
                    request({"cmd": "remove", "id": tasks[selected]["id"]})
                    message = "task record removed; log file kept"
                except Exception as exc:
                    message = str(exc)


def print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No managed jobs.")
        return
    print(f"{'ID':8}  {'STATUS':8} {'PID':>7}  {'NAME':18} COMMAND")
    for task in tasks:
        print(f"{task['id']:8}  {task['status']:8} {str(task.get('pid') or '-'):>7}  {task['name'][:18]:18} {task['command']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Headless PTY process manager with a curses TUI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="action")
    run = sub.add_parser("run", help="start a managed process")
    run.add_argument("-n", "--name")
    run.add_argument("-l", "--log")
    run.add_argument("-C", "--cwd", default=os.getcwd())
    run.add_argument("-a", "--attach", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    adopt = sub.add_parser("adopt", help="adopt an existing terminal process by PID")
    adopt.add_argument("pid", type=int)
    adopt.add_argument("-n", "--name")
    adopt.add_argument("-l", "--log")
    adopt.add_argument("-a", "--attach", action="store_true")
    sub.add_parser("list", help="list managed processes").add_argument("--json", action="store_true")
    sub.add_parser("tui", help="open the TUI")
    sub.add_parser("attach", help="attach a task in the foreground").add_argument("id")
    command_help = {
        "pause": "pause a task with SIGSTOP",
        "resume": "continue a paused task with SIGCONT",
        "stop": "request task shutdown with SIGTERM",
        "kill": "force task shutdown with SIGKILL",
        "interrupt": "send SIGINT to a task",
        "remove": "remove a finished task record",
    }
    for name, help_text in command_help.items():
        sub.add_parser(name, help=help_text).add_argument("id")
    log = sub.add_parser("redirect", help="redirect future output to another file")
    log.add_argument("id")
    log.add_argument("path")
    logs = sub.add_parser("logs", help="print a task log")
    logs.add_argument("id")
    logs.add_argument("-n", "--lines", type=int, default=100)
    signal_pid = sub.add_parser("signal-pid", help="signal an unmanaged process by PID")
    signal_pid.add_argument("pid", type=int)
    signal_pid.add_argument("signal", choices=("stop", "cont", "term", "kill", "int", "hup"))
    sub.add_parser("doctor", help="show runtime paths and daemon status")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv == ["_daemon"]:
        PmanDaemon().run()
        return 0
    if raw_argv == ["_shutdown"]:
        if daemon_alive():
            raw_request({"cmd": "shutdown"})
        return 0
    args = build_parser().parse_args(raw_argv)
    action = args.action or "tui"
    try:
        if action == "doctor":
            home, sock_path, state_path, logs_dir = prepare_paths()
            info = daemon_info()
            if info:
                print(f"daemon: running (protocol {info.get('protocol', 'legacy')}, client {PROTOCOL_VERSION})")
            else:
                print(f"daemon: stopped (client protocol {PROTOCOL_VERSION})")
            print(f"reptyr: {reptyr_binary() or 'not installed'}")
            try:
                ptrace_scope = Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip()
            except OSError:
                ptrace_scope = "unavailable"
            print(f"ptrace_scope: {ptrace_scope}")
            print(f"state:  {state_path}")
            print(f"socket: {sock_path}")
            print(f"logs:   {logs_dir}")
            return 0
        if action == "tui":
            shell_pid = os.getppid()
            ensure_daemon()
            curses.wrapper(tui, shell_pid)
            return 0
        if action == "run":
            command = list(args.command)
            if command and command[0] == "--":
                command.pop(0)
            if not command:
                raise RuntimeError("missing command (example: pman run -- python3 server.py)")
            payload = {
                "cmd": "start",
                "argv": command,
                "cwd": str(Path(args.cwd).expanduser().resolve()),
                "name": args.name,
                "log_path": str(Path(args.log).expanduser().resolve()) if args.log else None,
            }
            task = request(payload)["task"]
            print(f"started {task['id']} pid={task['pid']} log={task['log_path']}")
            if args.attach:
                attach(task["id"])
            return 0
        if action == "adopt":
            payload = {
                "cmd": "adopt",
                "pid": args.pid,
                "name": args.name,
                "log_path": str(Path(args.log).expanduser().resolve()) if args.log else None,
            }
            task = request(payload)["task"]
            print(f"adopted pid={args.pid} as {task['id']} log={task['log_path']}")
            if args.attach:
                attach(task["id"])
            return 0
        if action == "list":
            tasks = request({"cmd": "list"})["tasks"]
            if args.json:
                print(json.dumps(tasks, ensure_ascii=False, indent=2))
            else:
                print_tasks(tasks)
            return 0
        if action == "attach":
            attach(args.id)
            return 0
        if action in {"pause", "resume", "stop", "kill", "interrupt"}:
            task_id = resolve_task_id(args.id)
            sig = {"pause": "stop", "resume": "cont", "stop": "term", "kill": "kill", "interrupt": "int"}[action]
            result = request({"cmd": "signal", "id": task_id, "signal": sig})["task"]
            print(f"{result['id']}: {action} requested")
            return 0
        if action == "remove":
            task_id = resolve_task_id(args.id)
            request({"cmd": "remove", "id": task_id})
            print(f"removed task record {task_id}; log file kept")
            return 0
        if action == "redirect":
            task_id = resolve_task_id(args.id)
            path = str(Path(args.path).expanduser().resolve())
            request({"cmd": "set_log", "id": task_id, "path": path})
            print(f"{task_id}: future output -> {path}")
            return 0
        if action == "logs":
            task_id = resolve_task_id(args.id)
            task = next(t for t in request({"cmd": "list"})["tasks"] if t["id"] == task_id)
            try:
                lines = Path(task["log_path"]).read_bytes().decode(errors="replace").splitlines()
            except OSError as exc:
                raise RuntimeError(str(exc))
            print("\n".join(lines[-max(0, args.lines) :]))
            return 0
        if action == "signal-pid":
            result = request({"cmd": "signal_external", "pid": args.pid, "signal": args.signal})["process"]
            print(f"pid={result['pid']} ({result['name']}): {result['signal']} sent")
            return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"pman: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
