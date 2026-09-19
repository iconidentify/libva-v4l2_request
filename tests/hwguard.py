#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exclusive hardware lease and deadline runner for decoder tests.

Never opens the decoder to check health, never waits on a child without a
deadline, and never loads or unloads kernel modules.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LEASE_ENV = "LIBVA_HW_GUARD_LEASE"
IDENTITY_ENV = "LIBVA_HW_GUARD_IDENTITY"
BUSY_EXIT = 75
AVD_FAULT_PHRASES = (
    "h2 error",
    "h3 error",
    "h2 timeout",
    "h3 timeout",
    "frame processing timed out",
    "avd firmware",
    "avd_timeout",
    "unable to handle kernel",
    "internal error: oops",
    "kernel bug at",
)
# Keep the old name for tests that mention journal markers.
AVD_JOURNAL_MARKERS = AVD_FAULT_PHRASES


def is_avd_fault(line: str) -> bool:
    lowered = line.lower()
    taint = "taints kernel" in lowered or "tainting kernel" in lowered
    if any(phrase in lowered for phrase in AVD_FAULT_PHRASES):
        return True
    if taint:
        return False
    if ("apple_avd:" in lowered or ".avd:" in lowered) and "error" in lowered:
        return True
    return False
DECODER_WCHANS = ("video_do_ioctl", "v4l2_", "vb2_", "m2m", "avd_", "media_request")
REDACT = re.compile(r"(https?://\S+)|(/\S+)|([A-Za-z]:\\[^\s]+)")
WEDGE_GRACE_S = 10.0


class GuardError(RuntimeError):
    """Preflight or monitor failed; stop without touching the decoder."""


def redact_text(value: str) -> str:
    if not value:
        return value
    return REDACT.sub("<redacted>", value)


def redact_argv(argv: Iterable[str]) -> list[str]:
    return [redact_text(part) for part in argv]


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "meson.build").is_file() and (candidate / "tests").is_dir():
            return candidate
    raise SystemExit("could not locate repository root from tests/hwguard.py")


def default_lock_dir() -> Path:
    # Host-wide, independent of XDG_RUNTIME_DIR so two agents cannot both lease.
    return Path("/tmp") / "libva-v4l2-hwguard"


def journalctl_cmd(since: str | None = None) -> list[str]:
    cmd = ["journalctl", "-k", "--no-pager", "-o", "cat"]
    if since:
        cmd.extend(["--since", since])
    else:
        cmd.append("-b")
    return cmd


def process_group(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def read_fd_links(fd_dir: Path) -> list[str] | None:
    """Resolve a process's open fds, tolerating individual fds vanishing.

    Returns None only when the process itself is gone. Reading the fds as one
    comprehension made a single fd closing mid-scan discard the whole process,
    which matters because that process may hold the decoder on a different fd:
    losing it reports the decoder idle while it is in use, and the guard then
    starts a run against someone else's decoder. Busy processes close fds
    constantly, so this needs no exit to trigger.
    """
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return None
    links: list[str] = []
    for fd in entries:
        try:
            links.append(os.readlink(fd))
        except OSError:
            continue
    return links


def pid_alive(pid: int) -> bool:
    """Whether a pid still exists. A task that has exited is not stuck."""
    return pid > 0 and Path(f"/proc/{pid}").exists()


def exited_pid() -> int:
    """A pid that has certainly exited, for exercising the stuck-task check.

    Retries because a reaped pid is immediately reusable; without this the
    check races the allocator on a busy machine.
    """
    proc = subprocess.Popen(["true"])
    for _ in range(10):
        proc.wait(timeout=10)
        if not pid_alive(proc.pid):
            return proc.pid
        proc = subprocess.Popen(["true"])
    return proc.pid


def is_owned_holder(holder_pid: int, child_pid: int) -> bool:
    if holder_pid in (child_pid, os.getpid()):
        return True
    group = process_group(holder_pid)
    if group is not None and group in (child_pid, process_group(child_pid) or -1):
        return True
    seen: set[int] = set()
    pid = holder_pid
    while pid and pid not in seen:
        if pid in (child_pid, os.getpid()):
            return True
        if process_group(pid) == child_pid:
            return True
        seen.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[-1].split()[1])
        except (OSError, IndexError, ValueError):
            return False
    return False


def infer_driver_path(cmd: list[str]) -> str | None:
    for index, part in enumerate(cmd):
        if part.endswith(".sh") and index + 1 < len(cmd) and not cmd[index + 1].startswith("-"):
            return cmd[index + 1]
    return None


def foreign_holders(state: DecoderState, child_pid: int) -> list[dict[str, Any]]:
    return [
        holder for holder in state.holders
        # A short vector can finish between the fd scan and classification.
        # Preserve ownership observed during the scan instead of treating a
        # vanished /proc entry as evidence of a foreign decoder client.
        if holder.get("owner_root") != child_pid
        and holder.get("pgrp") != child_pid
        and not is_owned_holder(int(holder.get("pid") or 0), child_pid)
    ]


@dataclass
class DecoderState:
    module_loaded: bool
    video_node: str | None
    media_node: str | None
    holders: list[dict[str, Any]] = field(default_factory=list)
    stuck_tasks: list[dict[str, Any]] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.holders)

    @property
    def wedged(self) -> bool:
        # Stuck tasks are sampled in an earlier pass. A task that has since
        # exited is not wedging anything, and preflight() consumes this too,
        # so an unrelated process transiently in a decoder wchan must not
        # refuse the run with "decoder already wedged".
        return any(task.get("state") == "D"
                   and pid_alive(int(task.get("pid") or 0))
                   for task in self.stuck_tasks)


class EventLog:
    def __init__(self, path: Path, *, verbose: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.verbose = verbose
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def write(self, **record: Any) -> None:
        record.setdefault("time", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        if not self.verbose:
            record.pop("cmd", None)
            record.pop("argv", None)
            for key in ("path", "url", "media"):
                if key in record and isinstance(record[key], str):
                    record[key] = redact_text(record[key])
            if "holders" in record:
                cleaned = []
                for holder in record["holders"]:
                    item = dict(holder)
                    if "cmd" in item:
                        item["cmd"] = "<redacted>"
                    cleaned.append(item)
                record["holders"] = cleaned
        os.write(self._fd, (json.dumps(record) + "\n").encode())
        os.fsync(self._fd)

    def close(self) -> None:
        os.close(self._fd)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


class LinuxBackend:
    def __init__(self, preflight_since: str | None = None) -> None:
        self.preflight_since = preflight_since
        self.owner_pid: int | None = None

    def state(self) -> DecoderState:
        video = media = None
        sys_v4l = Path("/sys/class/video4linux")
        if sys_v4l.is_dir():
            for dev in sorted(sys_v4l.glob("video*")):
                if _read(dev / "name") == "avd":
                    video = f"/dev/{dev.name}"
                    for node in (dev / "device").glob("media*"):
                        media = f"/dev/{node.name}"
        nodes = {n for n in (video, media) if n}
        holders: list[dict[str, Any]] = []
        if nodes:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                pgrp = process_group(int(pid_dir.name))
                # timeout(1) and other wrappers may create a separate group.
                # Observe the ancestry while the fd holder still exists too.
                owner_root = self.owner_pid if self.owner_pid and is_owned_holder(
                    int(pid_dir.name), self.owner_pid
                ) else None
                fds = read_fd_links(pid_dir / "fd")
                if fds is None:
                    continue
                if nodes.intersection(fds):
                    cmdline = (_read(pid_dir / "cmdline") or "").replace("\0", " ").strip()
                    holders.append({"pid": int(pid_dir.name), "pgrp": pgrp,
                                    "owner_root": owner_root,
                                    "cmd": cmdline[:160]})
        stuck: list[dict[str, Any]] = []
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            task_root = pid_dir / "task"
            if not task_root.is_dir():
                continue
            try:
                tasks = list(task_root.iterdir())
            except OSError:
                # The process exited between is_dir() and the scan. The holder
                # loop above already tolerates this; without it a short-lived
                # client can kill the guard mid-run with an unhandled
                # FileNotFoundError, skipping the normal abort path.
                continue
            for task in tasks:
                wchan = _read(task / "wchan") or ""
                if not any(token in wchan for token in DECODER_WCHANS):
                    continue
                stat = _read(task / "stat") or ""
                state = stat.rsplit(")", 1)[-1].split()[0] if ")" in stat else "?"
                stuck.append({
                    "pid": int(pid_dir.name),
                    "tid": int(task.name),
                    "state": state,
                    "wchan": wchan,
                })
        return DecoderState(
            module_loaded=Path("/sys/module/apple_avd").exists(),
            video_node=video,
            media_node=media,
            holders=holders,
            stuck_tasks=stuck,
            faults=self.journal_since(self.preflight_since),
        )

    def journal_since(self, since: str | None = None) -> list[str]:
        try:
            proc = subprocess.run(
                journalctl_cmd(since), capture_output=True, text=True, timeout=15,
            )
        except OSError as exc:
            raise GuardError(f"journalctl is not available: {exc}") from exc
        if proc.returncode != 0:
            raise GuardError(f"journalctl failed ({proc.returncode})")
        return [line for line in proc.stdout.splitlines() if is_avd_fault(line)]


class FakeBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "journal").write_text("")
        (self.root / "holders.json").write_text("[]")
        (self.root / "stuck.json").write_text("[]")
        (self.root / "module").write_text("1")
        (self.root / "video").write_text("/dev/video-fake")
        (self.root / "media").write_text("/dev/media-fake")

    def state(self) -> DecoderState:
        holders = json.loads((self.root / "holders.json").read_text() or "[]")
        stuck = json.loads((self.root / "stuck.json").read_text() or "[]")
        faults = [
            line for line in (self.root / "journal").read_text().splitlines()
            if is_avd_fault(line)
        ]
        return DecoderState(
            module_loaded=(self.root / "module").read_text().strip() == "1",
            video_node=(self.root / "video").read_text().strip(),
            media_node=(self.root / "media").read_text().strip(),
            holders=holders,
            stuck_tasks=stuck,
            faults=faults,
        )

    def journal_since(self, since: str | None = None) -> list[str]:
        del since
        return [
            line for line in (self.root / "journal").read_text().splitlines()
            if is_avd_fault(line)
        ]

    def inject_fault(self, line: str) -> None:
        with (self.root / "journal").open("a") as handle:
            handle.write(line + "\n")

    def inject_holder(self, pid: int = 99999) -> None:
        (self.root / "holders.json").write_text(json.dumps([{"pid": pid, "cmd": "/usr/bin/mpv /secret/clip.mkv"}]))

    def inject_stuck(self, pid: int = 1) -> None:
        (self.root / "stuck.json").write_text(json.dumps([
            {"pid": pid, "tid": pid, "state": "D", "wchan": "avd_submit_job"}
        ]))


class Lease:
    def __init__(self, lock_dir: Path, identity: str, run_id: str) -> None:
        self.lock_dir = lock_dir
        self.identity = identity
        self.run_id = run_id
        self.lock_path = lock_dir / f"{identity}.lock"
        self.meta_path = lock_dir / f"{identity}.lease.json"
        self._fd: int | None = None

    def acquire(self) -> dict[str, Any] | None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = {}
            try:
                owner = json.loads(self.meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                owner = {"run_id": "unknown"}
            os.close(self._fd)
            self._fd = None
            return owner
        meta = {
            "run_id": self.run_id,
            "identity": self.identity,
            "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta) + "\n")
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp.replace(self.meta_path)
        dirfd = os.open(self.lock_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if self.meta_path.is_file():
                self.meta_path.unlink()
        except OSError:
            pass
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def preflight(backend: LinuxBackend | FakeBackend) -> DecoderState:
    state = backend.state()
    if not (state.module_loaded and state.video_node):
        raise GuardError("decoder is not present")
    if state.wedged:
        raise GuardError("decoder already wedged")
    if state.busy:
        raise GuardError("decoder already in use")
    if state.faults:
        raise GuardError("existing decoder/kernel faults on this boot")
    return state


def userspace_identity(driver_path: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "libva_drivers_path": redact_text(driver_path or os.environ.get("LIBVA_DRIVERS_PATH") or ""),
        "libva_driver_name": os.environ.get("LIBVA_DRIVER_NAME") or "v4l2_request",
        "loaded_module_limit": (
            "sysfs shows whether apple_avd is present; this guard cannot certify "
            "which module binary was loaded earlier or that every patch is active"
        ),
    }
    try:
        info["source_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        info["source_commit"] = None
    return info


@dataclass
class RunStatus:
    status: str
    returncode: int | None
    timed_out: bool = False
    wedged: bool = False
    abort_reason: str | None = None
    busy_owner: dict[str, Any] | None = None


def _kill_group(proc: subprocess.Popen) -> bool:
    """Return True if the child survived SIGKILL (kernel-stuck)."""
    if proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    try:
        proc.wait(timeout=2)
        return False
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    try:
        proc.wait(timeout=3)
        return False
    except subprocess.TimeoutExpired:
        return True


def run_guarded(
    cmd: list[str],
    *,
    identity: str = "avd",
    deadline: float = 60.0,
    poll: float = 0.2,
    fake: bool = False,
    fake_root: Path | None = None,
    lock_dir: Path | None = None,
    log_path: Path | None = None,
    verbose: bool = False,
    inject: str | None = None,
    env: dict[str, str] | None = None,
    journal_since: str | None = None,
) -> RunStatus:
    run_id = str(uuid.uuid4())
    lock_dir = lock_dir or default_lock_dir()
    log_path = log_path or (lock_dir / f"{identity}-{run_id}.jsonl")
    log = EventLog(log_path, verbose=verbose)
    backend: LinuxBackend | FakeBackend
    if fake:
        backend = FakeBackend(fake_root or (lock_dir / "fake" / run_id))
        if inject == "preflight-busy":
            backend.inject_holder()
        if inject == "preflight-fault":
            backend.inject_fault("apple_avd: firmware timeout H3")
    else:
        backend = LinuxBackend(preflight_since=journal_since)
    lease = Lease(lock_dir, identity, run_id)
    busy = lease.acquire()
    if busy:
        log.write(event="busy", identity=identity, owner=busy)
        log.close()
        return RunStatus(status="busy", returncode=BUSY_EXIT, busy_owner=busy)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    inferred = infer_driver_path(cmd)
    if inferred and not child_env.get("LIBVA_DRIVERS_PATH"):
        child_env["LIBVA_DRIVERS_PATH"] = inferred
    child_env[LEASE_ENV] = run_id
    child_env[IDENTITY_ENV] = identity
    child_env["LIBVA_HW_GUARD"] = "1"
    abort_reason: str | None = None
    stop = False

    def request_stop(reason: str) -> None:
        nonlocal abort_reason, stop
        if not stop:
            abort_reason = reason
            stop = True

    def handle_sigint(signum, frame) -> None:
        del signum, frame
        request_stop("signal")

    previous_int = signal.signal(signal.SIGINT, handle_sigint)
    previous_term = signal.signal(signal.SIGTERM, handle_sigint)
    proc: subprocess.Popen | None = None
    timed_out = False
    wedged = False
    returncode: int | None = None
    try:
        state = preflight(backend)
        log.write(
            event="preflight",
            run_id=run_id,
            identity=identity,
            idle=not state.busy,
            module_loaded=state.module_loaded,
            userspace=userspace_identity(child_env.get("LIBVA_DRIVERS_PATH")),
        )
        started = time.monotonic()
        journal_origin = time.strftime("%Y-%m-%d %H:%M:%S")
        start_record = {"event": "start", "run_id": run_id}
        if verbose:
            start_record["cmd"] = cmd
        log.write(**start_record)
        proc = subprocess.Popen(cmd, env=child_env, start_new_session=True)
        if isinstance(backend, LinuxBackend):
            backend.owner_pid = proc.pid
        deadline_at = started + deadline
        while proc.poll() is None:
            if inject == "timeout":
                request_stop("timeout")
                timed_out = True
            elif inject == "avd-error" and fake:
                backend.inject_fault("avd 269080000.avd: H3 error")
            elif inject == "foreign" and fake:
                backend.inject_holder(99999)
            elif inject == "owned-holder" and fake:
                backend.inject_holder(proc.pid)
            elif inject == "stuck-child" and fake:
                backend.inject_stuck()
            elif inject == "stuck-exited" and fake:
                backend.inject_stuck(exited_pid())
            now = time.monotonic()
            if now >= deadline_at:
                timed_out = True
                request_stop("timeout")
            current = backend.state()
            try:
                new_faults = backend.journal_since(journal_origin)
            except GuardError as exc:
                request_stop(str(exc))
                new_faults = []
            if current.faults or new_faults:
                request_stop("avd-error")
            foreign = foreign_holders(current, proc.pid)
            if foreign:
                request_stop("foreign-client")
                log.write(event="abort", reason="foreign-client", holders=foreign)
            if current.wedged:
                # Stuck tasks are collected in an earlier pass. A short-lived
                # owned client sampled in D state can exit before ownership is
                # resolved, and the ancestry walk then reports it foreign. A
                # task that no longer exists is not wedging the decoder.
                other = [
                    task for task in current.stuck_tasks
                    if pid_alive(int(task.get("pid") or 0))
                    and not is_owned_holder(int(task.get("pid") or 0), proc.pid)
                ]
                if other:
                    wedged = True
                    request_stop("wedged")
            if stop:
                break
            try:
                proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                continue
        if proc.poll() is None:
            wedged = _kill_group(proc) or wedged
            if timed_out and abort_reason is None:
                abort_reason = "timeout"
        returncode = proc.poll()
        final = backend.state()
        leftover = foreign_holders(final, proc.pid)
        if leftover and abort_reason is None:
            abort_reason = "leftover-holders"
        status = "ok"
        if abort_reason == "signal":
            status = "signal"
        elif timed_out:
            status = "timeout"
        elif wedged:
            status = "wedged"
        elif abort_reason:
            status = "abort"
        elif returncode not in (0, None):
            status = "child-error"
        log.write(
            event="final",
            run_id=run_id,
            status=status,
            returncode=returncode,
            timed_out=timed_out,
            wedged=wedged,
            abort_reason=abort_reason,
            idle=not leftover,
            holders=leftover,
        )
        return RunStatus(
            status=status,
            returncode=returncode,
            timed_out=timed_out,
            wedged=wedged,
            abort_reason=abort_reason,
        )
    except GuardError as exc:
        log.write(event="final", run_id=run_id, status="preflight-error", abort_reason=str(exc))
        return RunStatus(status="preflight-error", returncode=2, abort_reason=str(exc))
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
        lease.release()
        log.close()


def run_self_test() -> int:
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    root = repo_root()
    check((root / "tests" / "hwguard.py").is_file(), "hwguard.py missing")
    source = (root / "tests" / "hwguard.py").read_text()
    home = chr(47) + "home" + chr(47)
    check(home not in source, "hwguard.py contains an absolute home path")

    work = Path(os.environ.get("TMPDIR") or "/tmp") / f"hwguard-selftest-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    lock_dir = work / "locks"
    fake_root = work / "fake"

    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]

    # Reaped vector children no longer have a queryable process group. The
    # group observed alongside their open fd still proves lease ownership.
    gone_pid = 999999999
    snapshot = DecoderState(True, "/dev/video-fake", None, holders=[
        {"pid": gone_pid, "pgrp": os.getpid()},
        {"pid": gone_pid, "pgrp": gone_pid, "owner_root": os.getpid()},
        {"pid": gone_pid, "pgrp": gone_pid},
        {"pid": gone_pid},
    ])
    check(foreign_holders(snapshot, os.getpid()) == snapshot.holders[2:],
          "exited owned holder must stay owned; foreign/unknown must not")

    holder_script = work / "holder.py"
    holder_script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, os.environ['HWGUARD_DIR'])\n"
        "import hwguard\n"
        "lease = hwguard.Lease(Path(os.environ['LOCK_DIR']), 'avd', 'holder')\n"
        "busy = lease.acquire()\n"
        "print('ACQUIRED' if busy is None else 'BUSY', flush=True)\n"
        "time.sleep(8)\n"
        "lease.release()\n"
    )
    env = os.environ.copy()
    env["HWGUARD_DIR"] = str(root / "tests")
    env["LOCK_DIR"] = str(lock_dir / "two")
    proc1 = subprocess.Popen(
        [sys.executable, str(holder_script)], env=env,
        stdout=subprocess.PIPE, text=True,
    )
    line = proc1.stdout.readline() if proc1.stdout else ""
    check("ACQUIRED" in line, f"first locker did not acquire: {line!r}")
    second = run_guarded(
        [sys.executable, "-c", "print('should-not-run')"],
        fake=True, fake_root=fake_root / "b", lock_dir=lock_dir / "two",
        deadline=2, log_path=work / "busy.jsonl",
    )
    check(second.status == "busy" and second.returncode == BUSY_EXIT,
          f"second process was not busy: {second}")
    check(second.busy_owner and second.busy_owner.get("run_id") == "holder",
          "busy result missing owner run_id")
    proc1.terminate()
    proc1.wait(timeout=5)

    check(not is_avd_fault("apple_avd: loading out-of-tree module taints kernel."),
          "module taint line must not count as a decoder fault")
    check(is_avd_fault("avd 269080000.avd: H3 error"),
          "documented H3 error must count as a decoder fault")
    check(is_avd_fault("avd 269080000.avd: Frame processing timed out!"),
          "documented frame timeout must count as a decoder fault")
    check(is_avd_fault("avd 269080000.avd: H2 error"),
          "documented H2 error must count as a decoder fault")
    check(is_avd_fault("apple_avd: H3 error while taints kernel"),
          "taint plus a real error must still count as a decoder fault")
    check(journalctl_cmd()[-1] == "-b", "boot journal query must use journalctl -b")
    check("--since" not in journalctl_cmd(), "boot journal query must not use --since")
    since_cmd = journalctl_cmd("2026-09-16 00:00:00")
    check("--since" in since_cmd and "2026-09-16 00:00:00" in since_cmd,
          "journalctl_cmd(since) must pass --since")
    check(infer_driver_path(["sh", "tests/hwdownload.sh", "/tmp/build/src"]) == "/tmp/build/src",
          "driver path not inferred from hardware script argv")

    xdg_ident = f"selftest-{os.getpid()}"
    xdg_holder = work / "xdg-holder.py"
    xdg_holder.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, os.environ['HWGUARD_DIR'])\n"
        "import hwguard\n"
        "lease = hwguard.Lease(hwguard.default_lock_dir(), os.environ['IDENT'], 'holder')\n"
        "busy = lease.acquire()\n"
        "print('ACQUIRED' if busy is None else 'BUSY', flush=True)\n"
        "time.sleep(8)\n"
        "lease.release()\n"
    )
    xdg_a = os.environ.copy()
    xdg_a["HWGUARD_DIR"] = str(root / "tests")
    xdg_a["IDENT"] = xdg_ident
    xdg_a["XDG_RUNTIME_DIR"] = str(work / "xdg-a")
    (work / "xdg-a").mkdir()
    (work / "xdg-b").mkdir()
    xdg_proc = subprocess.Popen(
        [sys.executable, str(xdg_holder)], env=xdg_a,
        stdout=subprocess.PIPE, text=True,
    )
    xdg_line = xdg_proc.stdout.readline() if xdg_proc.stdout else ""
    check("ACQUIRED" in xdg_line, f"host-wide first locker failed: {xdg_line!r}")
    xdg_second = run_guarded(
        [sys.executable, "-c", "print('should-not-run')"],
        fake=True, fake_root=fake_root / "xdg", identity=xdg_ident,
        deadline=2, log_path=work / "xdg-busy.jsonl",
        env={"XDG_RUNTIME_DIR": str(work / "xdg-b")},
    )
    check(xdg_second.status == "busy", f"different XDG_RUNTIME_DIR still leased: {xdg_second}")
    xdg_proc.terminate()
    xdg_proc.wait(timeout=5)

    timed = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "t", lock_dir=lock_dir / "t",
        deadline=0.4, poll=0.05, inject="timeout", log_path=work / "timeout.jsonl",
    )
    check(timed.status == "timeout" and timed.timed_out, f"timeout inject: {timed}")
    check(timed.abort_reason == "timeout", "timeout missing abort_reason")

    avd = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "e", lock_dir=lock_dir / "e",
        deadline=8, poll=0.05, inject="avd-error", log_path=work / "avd.jsonl",
    )
    check(avd.status == "abort" and avd.abort_reason == "avd-error", f"avd-error inject: {avd}")

    foreign = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "f", lock_dir=lock_dir / "f",
        deadline=8, poll=0.05, inject="foreign", log_path=work / "foreign.jsonl",
    )
    check(foreign.status == "abort" and foreign.abort_reason == "foreign-client",
          f"foreign inject: {foreign}")
    owned = run_guarded(
        [sys.executable, "-c", "import time; time.sleep(0.4)"],
        fake=True, fake_root=fake_root / "o", lock_dir=lock_dir / "o",
        deadline=5, poll=0.05, inject="owned-holder", log_path=work / "owned.jsonl",
    )
    check(owned.status == "ok" and owned.abort_reason is None,
          f"owned descendant holder aborted: {owned}")

    stuck = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "s", lock_dir=lock_dir / "s",
        deadline=8, poll=0.05, inject="stuck-child", log_path=work / "stuck.jsonl",
    )
    check(stuck.status == "wedged" and stuck.wedged, f"stuck-child inject: {stuck}")
    check(stuck.abort_reason == "wedged", f"stuck-child abort_reason: {stuck.abort_reason}")
    stuck_final = json.loads((work / "stuck.jsonl").read_text().strip().splitlines()[-1])
    check(stuck_final.get("event") == "final", "stuck-child missing final event")
    check(stuck_final.get("wedged") is True, "stuck-child final wedged flag")
    check("returncode" in stuck_final, "stuck-child final missing returncode")
    check("idle" in stuck_final, "stuck-child final missing idle")

    # A stuck task whose pid already exited must not abort the run. Stuck
    # tasks are collected in one pass and their ownership resolved in a later
    # one, so a short-lived owned client sampled in D state can be gone by the
    # time the ancestry walk runs; the walk then reports it foreign. Before
    # this check that raced into a spurious "wedged" abort under sustained
    # client churn while the decoder was healthy.
    exited = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "x", lock_dir=lock_dir / "x",
        deadline=8, poll=0.05, inject="stuck-exited", log_path=work / "exited.jsonl",
    )
    check(not exited.wedged and exited.abort_reason != "wedged",
          f"exited stuck task must not abort as wedged: {exited}")
    check(not pid_alive(exited_pid()), "exited pid must read as gone")
    check(pid_alive(os.getpid()), "live pid must read as alive")

    # DecoderState.wedged is consumed by preflight() as well as the monitor
    # loop, so cover both directions directly: a live foreign stuck task still
    # reads as wedged, an exited one does not.
    def _state(pid: int) -> DecoderState:
        return DecoderState(
            module_loaded=True, video_node="/dev/video-fake", media_node=None,
            stuck_tasks=[{"pid": pid, "tid": pid, "state": "D",
                          "wchan": "avd_submit_job"}],
        )
    # One unreadable fd must not discard the whole process from the holder
    # scan: it may hold the decoder on another fd, and losing it reports the
    # decoder idle while in use, so the guard would start against a foreign
    # client. A regular file stands in for an fd that cannot be readlink'd.
    fd_dir = work / "fddir"
    fd_dir.mkdir(parents=True, exist_ok=True)
    (fd_dir / "0").symlink_to("/dev/video-fake")
    (fd_dir / "1").write_text("not a symlink")
    links = read_fd_links(fd_dir)
    check(links is not None and "/dev/video-fake" in links,
          f"unreadable fd must not discard the holder: {links}")
    check(read_fd_links(work / "nonexistent-fd-dir") is None,
          "a vanished process must read as None, not an empty holder")

    check(_state(1).wedged, "live stuck task must still read as wedged")
    check(not _state(exited_pid()).wedged,
          "exited stuck task must not read as wedged (preflight consumer)")

    # SIGINT: child sleeps, parent handler via inject=signal using a subprocess sending SIGINT
    sig_script = work / "sig.py"
    sig_script.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, os.environ['HWGUARD_DIR'])\n"
        "import hwguard\n"
        "from pathlib import Path\n"
        "status = hwguard.run_guarded(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    fake=True, fake_root=Path(os.environ['FAKE']), lock_dir=Path(os.environ['LOCK']),\n"
        "    deadline=20, poll=0.05, log_path=Path(os.environ['LOG']))\n"
        "print(status.status, status.abort_reason or '', flush=True)\n"
    )
    sig_env = os.environ.copy()
    sig_env["HWGUARD_DIR"] = str(root / "tests")
    sig_env["FAKE"] = str(fake_root / "sig")
    sig_env["LOCK"] = str(lock_dir / "sig")
    sig_env["LOG"] = str(work / "sig.jsonl")
    sig_proc = subprocess.Popen(
        [sys.executable, str(sig_script)], env=sig_env,
        stdout=subprocess.PIPE, text=True,
    )
    time.sleep(0.4)
    sig_proc.send_signal(signal.SIGINT)
    out, _ = sig_proc.communicate(timeout=10)
    check("signal" in out, f"SIGINT did not record signal status: {out!r}")

    foreign_log = (work / "foreign.jsonl").read_text()
    check("holders" in foreign_log, "foreign abort did not record holders")
    check("/secret/clip.mkv" not in foreign_log, "publishable log leaked media path")
    check("clip.mkv" not in foreign_log, "publishable log leaked media basename")
    check("https://" not in foreign_log, "publishable log leaked URL")
    check('"cmd"' not in foreign_log.split('"holders"')[0], "publishable start event kept argv")
    verbose_foreign = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "fv", lock_dir=lock_dir / "fv",
        deadline=8, poll=0.05, inject="foreign", log_path=work / "foreign-verbose.jsonl",
        verbose=True,
    )
    check(verbose_foreign.abort_reason == "foreign-client", "verbose foreign inject failed")
    verbose_log = (work / "foreign-verbose.jsonl").read_text()
    check("/secret/clip.mkv" in verbose_log, "verbose log omitted holder command")

    # Scripts refuse unguarded hardware
    require = root / "tests" / "require-hw-guard.sh"
    check(require.is_file(), "missing require-hw-guard.sh")
    refused = subprocess.run(
        ["sh", "-c", f". {require}; require_hw_guard"],
        capture_output=True, text=True,
    )
    check(refused.returncode == 2, "require_hw_guard did not refuse unguarded mode")
    allowed = subprocess.run(
        ["sh", "-c", f". {require}; require_hw_guard"],
        capture_output=True, text=True,
        env={**os.environ, LEASE_ENV: "test-lease"},
    )
    check(allowed.returncode == 0, "require_hw_guard refused a valid lease")

    for script in (
        "hwdownload.sh", "early-export.sh", "h264-high10.sh", "vp9-matrix.sh",
        "frame-check.sh", "shared-contexts.sh", "hevc-concurrent.py",
    ):
        text = (root / "tests" / script).read_text()
        check("require-hw-guard.sh" in text or "LIBVA_HW_GUARD_LEASE" in text,
              f"{script} does not route through the guard")

    conf = (root / "tests" / "conformance.py").read_text()
    check("LIBVA_HW_GUARD_LEASE" in conf, "conformance.py --driver is not guard-gated")
    ung = subprocess.run(
        [sys.executable, str(root / "tests" / "hevc-concurrent.py"),
         "--schedule", "pair-bd", "--resources", "/tmp",
         "--frame-check", "/bin/true", "--output", "/tmp/hevc-conc-ungarded"],
        capture_output=True, text=True,
    )
    check(ung.returncode == 2, "hevc-concurrent.py did not refuse unguarded hardware")

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
    try:
        check(is_owned_holder(sleeper.pid, sleeper.pid), "pid must own itself")
        check(not is_owned_holder(1, sleeper.pid), "init must not count as the child")
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)

    if errors:
        print(f"{len(errors)} hwguard self-test error(s):", file=sys.stderr)
        print("\n".join(f"- {item}" for item in errors), file=sys.stderr)
        return 1
    print(
        "hwguard ok: exclusive lease, timeout/AVD-error/foreign/SIGINT/stuck-child "
        "stop with final status, logs redacted, unguarded hardware refused"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    parser.add_argument("--identity", default="avd")
    parser.add_argument("--deadline", type=float, default=120.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--fake-root", type=Path)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--verbose-log", action="store_true",
                        help="include unredacted local command detail")
    parser.add_argument(
        "--inject",
        choices=("timeout", "avd-error", "foreign", "owned-holder", "stuck-child",
                 "stuck-exited"),
    )
    parser.add_argument(
        "--journal-since",
        help="preflight journalctl --since instead of -b (new faults during the run still abort)",
    )
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("pass a command after --, or --self-test")
    result = run_guarded(
        cmd,
        identity=args.identity,
        deadline=args.deadline,
        poll=args.poll,
        fake=args.fake,
        fake_root=args.fake_root,
        lock_dir=args.lock_dir,
        log_path=args.log,
        verbose=args.verbose_log,
        inject=args.inject,
        journal_since=args.journal_since,
    )
    payload = {
        "status": result.status,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "wedged": result.wedged,
        "abort_reason": result.abort_reason,
        "busy": result.status == "busy",
        "owner": result.busy_owner,
    }
    if result.status == "busy":
        print(json.dumps({"busy": True, "owner": result.busy_owner}))
        return BUSY_EXIT
    print(json.dumps(payload), file=sys.stderr)
    if result.status == "ok":
        return int(result.returncode or 0)
    return 1


if __name__ == "__main__":
    sys.exit(main())
