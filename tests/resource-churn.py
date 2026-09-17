#!/usr/bin/env python3
"""Record process-local resource bounds without inspecting other applications."""

import argparse
import json
import math
import mmap
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time


STATUS_KEYS = ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem")


def _read_identity(pid):
    fields = Path("/proc").joinpath(str(pid), "stat").read_text().rsplit(") ", 1)[1].split()
    return fields[0], fields[19]


def _read_status(pid):
    values = {key: None for key in STATUS_KEYS}
    state = None
    for line in Path("/proc").joinpath(str(pid), "status").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator and key in values:
            fields = value.split()
            values[key] = int(fields[0]) if fields else None
        elif separator and key == "State":
            state = value.strip().split()[0]
    result = {key.lower() + "_kib": value for key, value in values.items()}
    result["process_state"] = state
    return result


def _read_maps(pid):
    count = 0
    mapped_bytes = 0
    for line in Path("/proc").joinpath(str(pid), "maps").read_text().splitlines():
        address = line.split(None, 1)[0]
        start, end = address.split("-", 1)
        count += 1
        mapped_bytes += int(end, 16) - int(start, 16)
    return count, mapped_bytes


def _read_dmabufs(pid, fds):
    references = []
    readable = 0
    unresolved_candidate = False
    for fd in fds:
        info_path = Path("/proc").joinpath(str(pid), "fdinfo", fd)
        target_path = Path("/proc").joinpath(str(pid), "fd", fd)
        try:
            target = os.readlink(target_path)
            inode = os.stat(target_path).st_ino
            fields = {}
            for line in info_path.read_text().splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    fields[key.strip()] = value.strip()
            if os.readlink(target_path) != target or os.stat(target_path).st_ino != inode:
                unresolved_candidate = True
                continue
            readable += 1
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            unresolved_candidate = True
            continue
        if "exp_name" not in fields:
            if "dmabuf" in target.lower():
                unresolved_candidate = True
            continue
        size = fields.get("size")
        inode = fields.get("ino") or fields.get("inode")
        references.append({
            "fd": int(fd),
            "exporter": fields.get("exp_name"),
            "size_bytes": int(size) if size and size.isdigit() else None,
            "identity": inode,
        })

    if (fds and not readable) or unresolved_candidate:
        return {"status": "unavailable", "references": None,
                "unique_objects": None, "unique_bytes": None}
    identities = {}
    known_identities = all(reference["identity"] for reference in references)
    for reference in references:
        identity = reference["identity"]
        identities.setdefault(identity, reference["size_bytes"])
    known_sizes = [size for size in identities.values() if size is not None]
    return {
        "status": "observed" if references else "none-observed",
        "references": len(references),
        "unique_objects": len(identities) if known_identities else None,
        "unique_bytes": sum(known_sizes) if known_identities and len(known_sizes) == len(identities) else None,
    }


def snapshot(pid):
    proc = Path("/proc").joinpath(str(pid))
    state, start_time = _read_identity(pid)
    if state == "Z":
        raise ProcessLookupError("process {} is a zombie".format(pid))
    status = _read_status(pid)
    if status["process_state"] == "Z":
        raise ProcessLookupError("process {} is a zombie".format(pid))
    fds = sorted(entry.name for entry in proc.joinpath("fd").iterdir())
    map_count, mapped_bytes = _read_maps(pid)
    sample = {
        "type": "sample",
        "timestamp_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "pid": pid,
        "process_start_time": start_time,
        "fd_count": len(fds),
        "map_count": map_count,
        "mapped_bytes": mapped_bytes,
        "dmabuf": _read_dmabufs(pid, fds),
    }
    sample["dmabuf_references"] = sample["dmabuf"]["references"]
    sample["dmabuf_objects"] = sample["dmabuf"]["unique_objects"]
    sample["dmabuf_bytes"] = sample["dmabuf"]["unique_bytes"]
    sample.update(status)
    final_state, final_start_time = _read_identity(pid)
    if final_state == "Z" or final_start_time != start_time:
        raise ProcessLookupError("process {} changed while sampled".format(pid))
    return sample


def _growth(samples, key):
    baseline = samples[0].get(key)
    values = [sample.get(key) for sample in samples]
    if baseline is None or any(value is None for value in values):
        return None
    return max(values) - baseline


def _monotonic(samples, key):
    values = [sample.get(key) for sample in samples]
    if any(value is None for value in values):
        return None
    return values[-1] > values[0] and all(a <= b for a, b in zip(values, values[1:]))


def summarize(samples, limits, acceptance=False):
    if not samples:
        raise ValueError("no resource samples")
    if acceptance and limits.get("vmrss_kib") is None:
        raise ValueError("acceptance mode requires --max-rss-growth-kib")
    keys = {
        "fd_count": "fds",
        "map_count": "maps",
        "mapped_bytes": "mapped_bytes",
        "vmrss_kib": "vmrss_kib",
        "dmabuf_references": "dmabuf_references",
        "dmabuf_objects": "dmabuf_objects",
        "dmabuf_bytes": "dmabuf_bytes",
    }
    growth = {name: _growth(samples, key) for key, name in keys.items()}
    monotonic = {name: _monotonic(samples, key) for key, name in keys.items()}
    violations = []
    for name, value in growth.items():
        limit = limits.get(name)
        if limit is not None and value is None:
            violations.append("{} growth is unavailable".format(name))
        elif limit is not None and value > limit:
            violations.append("{} growth {} exceeds {}".format(name, value, limit))
    if any(sample["dmabuf"]["status"] == "unavailable" for sample in samples):
        dmabuf_status = "unavailable"
    elif any(sample["dmabuf"]["status"] == "observed" for sample in samples):
        dmabuf_status = "observed"
    else:
        dmabuf_status = "none-observed"
    if acceptance and dmabuf_status == "unavailable":
        violations.append("dma-buf fdinfo accounting is unavailable")
    return {
        "type": "summary",
        "samples": len(samples),
        "limits": limits,
        "growth": growth,
        "monotonic_growth": monotonic,
        "dmabuf_status": dmabuf_status,
        "violations": violations,
        "passed": not violations,
    }


def write_record(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def validate_options(count, interval, limits, acceptance, warmup, exit_timeout):
    if count < 2:
        raise ValueError("recording requires at least two samples")
    for name, value, allow_zero in (("interval", interval, False),
                                     ("warmup", warmup, True),
                                     ("exit timeout", exit_timeout, False)):
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError("{} must be finite and {}".format(
                name, "nonnegative" if allow_zero else "positive"))
    if any(value is not None and value < 0 for value in limits.values()):
        raise ValueError("growth limits must be nonnegative")
    if acceptance and limits.get("vmrss_kib") is None:
        raise ValueError("acceptance mode requires --max-rss-growth-kib")


def workload_status(process, process_group=False):
    """Keep a group leader waitable until all signals to its group are done."""
    if not process_group or process.returncode is not None:
        return process.poll()
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


def wait_workload(process, timeout, process_group=False):
    if not process_group:
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        status = workload_status(process, True)
        if status is not None:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(min(remaining, 0.01))


def group_has_live_members(group):
    # killpg(group, 0) also sees our unreaped zombie leader. Ignore zombies
    # when deciding whether cleanup was forced, but retain the leader's PID.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[2]) == group and fields[0] not in ("Z", "X"):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            return True  # Fail closed if live descendants cannot be ruled out.
    return False


def stop_workload(process, timeout, process_group=False):
    """Bounded cleanup of a workload we spawned; never used for monitor --pid."""
    # record() and its caller both have exception cleanup. Once this group is
    # released, its numeric ID could be reused; never inspect or signal it again.
    if process_group and getattr(process, "_resource_group_released", False):
        return False

    if process_group:
        # A reaped leader no longer reserves its numeric process-group ID.
        # Never inspect or signal that ID, even on the first cleanup attempt.
        try:
            if process.returncode is not None:
                raise ChildProcessError()
            workload_status(process, True)
        except ChildProcessError:
            process._resource_group_released = True
            return False
        forced = group_has_live_members(process.pid)
        if forced:
            os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while group_has_live_members(process.pid) and time.monotonic() < deadline:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        # Signal before reaping, including when the leader exited naturally.
        # The waitable leader reserves this ID through our final signal.
        os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass  # Never hang on an unkillable task or signal this group again.
        process._resource_group_released = True
        return forced

    def send(sig):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass

    if process.poll() is not None:
        return False
    send(signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        send(signal.SIGKILL)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass  # The result is already a failure; never hang on an unkillable task.
    return True


def record(pid, output, count, interval, limits, acceptance=False, process=None,
           warmup=0.0, exit_timeout=1.0, process_group=False):
    # Cover output-open, sampling and JSONL-write exceptions as well as normal exit.
    cleanup_timeout = exit_timeout if math.isfinite(exit_timeout) and exit_timeout > 0 else 1.0
    try:
        validate_options(count, interval, limits, acceptance, warmup, exit_timeout)
        return _record(pid, output, count, interval, limits, acceptance, process,
                       warmup, exit_timeout, process_group)
    finally:
        if process is not None:
            # A guard may give us only a short SIGTERM grace period. On any
            # interrupted/failed recording, do not spend the normal exit timeout.
            timeout = min(cleanup_timeout, 0.25) if sys.exc_info()[0] else cleanup_timeout
            stop_workload(process, timeout, process_group)


def _record(pid, output, count, interval, limits, acceptance, process,
            warmup, exit_timeout, process_group):
    samples = []
    errors = []
    expected_identity = None
    try:
        state, expected_identity = _read_identity(pid)
        if state == "Z":
            errors.append("target is already a zombie")
    except (OSError, ValueError, IndexError) as error:
        errors.append("cannot establish target identity: {}".format(type(error).__name__))
    metadata = {
        "type": "metadata",
        "source_commit": os.environ.get("V4L2R_SOURCE_COMMIT"),
        "kernel": platform.release(),
        "platform": platform.platform(),
        "pid": pid,
        "process_start_time": expected_identity,
        "interval_seconds": interval,
        "requested_samples": count,
        "warmup_seconds": warmup,
        "exit_timeout_seconds": exit_timeout if process is not None else None,
        # Arguments may contain media paths or credentials. Store reviewed provenance
        # separately; the recorder does not copy raw command arguments into JSONL.
        "workload_executable": Path(process.args[0]).name if process is not None else None,
    }
    with output.open("w") as stream:
        write_record(stream, metadata)
        if warmup and not errors:
            time.sleep(warmup)
        for index in range(count):
            if errors or (process is not None and workload_status(process, process_group) is not None):
                break
            try:
                sample = snapshot(pid)
                if sample["process_start_time"] != expected_identity:
                    errors.append("target process identity changed during campaign")
                    break
            except (OSError, ValueError, IndexError) as error:
                errors.append("sample unavailable: {}".format(type(error).__name__))
                break
            sample["index"] = index
            samples.append(sample)
            write_record(stream, sample)
            if index + 1 < count:
                time.sleep(interval)
        try:
            result = summarize(samples, limits, acceptance)
        except ValueError as error:
            result = {
                "type": "summary", "samples": len(samples), "limits": limits,
                "violations": [str(error)], "passed": False,
            }
        result["violations"].extend(errors)
        result["elapsed_sample_seconds"] = (
            (samples[-1]["monotonic_ns"] - samples[0]["monotonic_ns"]) / 1e9
            if len(samples) > 1 else 0.0)
        if len(samples) != count:
            result["violations"].append(
                "recorded {} of {} requested samples".format(len(samples), count))
        if process is not None:
            try:
                result["exit_status"] = wait_workload(process, exit_timeout, process_group)
            except subprocess.TimeoutExpired:
                result["exit_status"] = None
                result["violations"].append(
                    "workload remained live after the sampling window")
            if stop_workload(process, exit_timeout, process_group):
                result["violations"].append("owned workload required forced cleanup")
            if result["exit_status"] not in (None, 0):
                result["violations"].append(
                    "workload exited with status {}".format(result["exit_status"]))
        result["passed"] = not result["violations"]
        write_record(stream, result)
    return result


def _child():
    descriptors = []
    mapping = None
    print("READY", flush=True)
    for command in sys.stdin:
        command = command.strip()
        if command == "allocate":
            descriptors = [os.open("/dev/null", os.O_RDONLY) for _ in range(4)]
            mapping = mmap.mmap(-1, 2 * mmap.PAGESIZE)
            print("ALLOCATED", flush=True)
        elif command == "release":
            mapping.close()
            mapping = None
            for descriptor in descriptors:
                os.close(descriptor)
            descriptors = []
            print("RELEASED", flush=True)
        elif command == "exit":
            return 0
        else:
            return 2
    return 0


def self_test():
    child = subprocess.Popen(
        [sys.executable, __file__, "_child"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "READY"
    baseline = snapshot(child.pid)
    child.stdin.write("allocate\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "ALLOCATED"
    allocated = snapshot(child.pid)
    assert allocated["fd_count"] >= baseline["fd_count"] + 4
    assert allocated["map_count"] >= baseline["map_count"] + 1
    child.stdin.write("release\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "RELEASED"
    released = snapshot(child.pid)
    assert released["fd_count"] == baseline["fd_count"]
    assert released["map_count"] == baseline["map_count"]
    child.stdin.write("exit\n")
    child.stdin.flush()
    assert child.wait() == 0

    bounded = summarize([baseline, allocated, released], {
        "fds": allocated["fd_count"] - baseline["fd_count"],
        "maps": allocated["map_count"] - baseline["map_count"],
        "mapped_bytes": allocated["mapped_bytes"] - baseline["mapped_bytes"],
        "vmrss_kib": max(0, allocated["vmrss_kib"] - baseline["vmrss_kib"]),
    }, acceptance=True)
    assert bounded["passed"]
    leaking = [dict(baseline), dict(baseline), dict(baseline)]
    for index, sample in enumerate(leaking):
        sample["fd_count"] += index
    assert not summarize(leaking, {"fds": 0}, acceptance=False)["passed"]
    try:
        summarize([baseline], {"fds": 0}, acceptance=True)
    except ValueError:
        pass
    else:
        raise AssertionError("acceptance summary allowed a missing RSS threshold")
    unavailable = dict(baseline)
    unavailable["dmabuf"] = {"status": "unavailable", "references": None,
                              "unique_objects": None, "unique_bytes": None}
    unavailable["dmabuf_references"] = None
    unavailable["dmabuf_objects"] = None
    unavailable["dmabuf_bytes"] = None
    result = summarize([unavailable], {"fds": 0}, acceptance=False)
    assert result["dmabuf_status"] == "unavailable"
    assert not summarize([unavailable], {"fds": 0, "vmrss_kib": 0},
                         acceptance=True)["passed"]

    with tempfile.TemporaryDirectory() as directory:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        # A fixed 50 ms warmup does not guarantee that Python has exited on
        # a loaded CI runner. Establish the early-exit condition explicitly.
        assert process.wait(timeout=5) == 0
        partial = record(process.pid, Path(directory) / "partial.jsonl", 2, 0.01,
                         {"fds": 0, "maps": 0, "mapped_bytes": 0,
                          "vmrss_kib": 1024, "dmabuf_references": 0,
                          "dmabuf_objects": 0, "dmabuf_bytes": 0},
                         acceptance=True, process=process, warmup=0.05)
        assert not partial["passed"]
    regression_tests(baseline)
    print("resource-churn self-test: PASS")
    return 0


def regression_tests(baseline):
    from types import SimpleNamespace
    from unittest import mock

    limits = {"fds": 0, "maps": 0, "mapped_bytes": 0, "vmrss_kib": 1024,
              "dmabuf_references": 0, "dmabuf_objects": 0, "dmabuf_bytes": 0}
    module = sys.modules[__name__]
    # The same cleanup runs from nested finally blocks. A vanished group's ID
    # must not be consulted again even if the OS has since recycled that number.
    completed = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    assert completed.wait(timeout=3) == 0
    with mock.patch.object(os, "killpg", side_effect=AssertionError("signalled a reaped leader")):
        assert not stop_workload(completed, 0.05, True)
    with mock.patch.object(os, "killpg", side_effect=AssertionError("revisited a released group")):
        assert not stop_workload(completed, 0.05, True)

    # Successful and failed leaders must still be waitable at EVERY group
    # signal, including the final kill. No timing assumption or recycled PID.
    real_killpg = os.killpg
    for status in (0, 3):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit({})".format(status)],
                                 start_new_session=True)
        signals = []

        def owned_signal(group, sig):
            assert group == child.pid and child.returncode is None
            assert os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            signals.append(sig)
            real_killpg(group, sig)

        try:
            assert wait_workload(child, 3, True) == status
            with mock.patch.object(os, "killpg", side_effect=owned_signal):
                assert not stop_workload(child, 0.05, True)
            assert signals == [signal.SIGKILL] and child.returncode == status
        finally:
            stop_workload(child, 0.05, True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        changed = dict(baseline, process_start_time="different-process")
        with mock.patch.object(module, "_read_identity", return_value=("S", baseline["process_start_time"])), \
             mock.patch.object(module, "snapshot", side_effect=[baseline, changed]):
            result = record(123, root / "identity.jsonl", 2, 0.001, limits, True)
        assert not result["passed"] and result["samples"] == 1
        assert any("identity changed" in error for error in result["violations"])

        with mock.patch.object(module, "_read_identity", return_value=("S", baseline["process_start_time"])), \
             mock.patch.object(module, "snapshot", side_effect=PermissionError()):
            result = record(123, root / "permission.jsonl", 2, 0.001, limits, True)
        assert not result["passed"] and result["samples"] == 0

        # Output-open and mid-record write failures must both reap the owned child.
        for fail_write in (False, True):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                     start_new_session=True)
            output = root / "write.jsonl" if fail_write else root / "missing" / "out"
            try:
                with mock.patch.object(module, "write_record", side_effect=OSError("disk full")):
                    try:
                        record(child.pid, output, 2, 0.001, limits, True,
                               child, exit_timeout=0.05, process_group=True)
                    except OSError:
                        pass
                    else:
                        raise AssertionError("expected logging failure")
                assert child.poll() is not None
            finally:
                stop_workload(child, 0.05, True)

        # A command must never launch when timing or acceptance options are invalid.
        marker = root / "launched"
        for extra in (("--interval", "nan"), ("--interval", "inf"),
                      ("--interval", "0"), ("--samples", "1"),
                      ("--warmup-seconds", "-1"), ("--exit-timeout", "0"),
                      ("--max-fd-growth", "-1"), ("--acceptance",)):
            result = subprocess.run([sys.executable, __file__, "run", "--output", str(root / "invalid.jsonl"),
                                     *extra, "--", sys.executable, "-c",
                                     "from pathlib import Path; Path({!r}).touch()".format(str(marker))],
                                    capture_output=True, timeout=3)
            assert result.returncode == 2 and not marker.exists()

        # A still-running command is bounded and fails, even if resources were stable.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        try:
            result = record(child.pid, root / "bounded.jsonl", 2, 0.005, limits,
                            process=child, warmup=0.05, exit_timeout=0.02,
                            process_group=True)
            assert not result["passed"] and child.poll() is not None
            assert any("sampling window" in error for error in result["violations"])
        finally:
            stop_workload(child, 0.05, True)

        # A hardware guard's SIGTERM must propagate to the owned workload group.
        sampler = subprocess.Popen([sys.executable, __file__, "run", "--output",
            str(root / "signal.jsonl"), "--warmup-seconds", "10", "--", sys.executable,
            "-c", "import os,time; print(os.getpid(),flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True)
        try:
            target = int(sampler.stdout.readline())
            sampler.terminate()
            assert sampler.wait(timeout=3) == 128 + signal.SIGTERM
            try:
                assert _read_identity(target)[0] == "Z"
            except (FileNotFoundError, ProcessLookupError):
                pass
        finally:
            if sampler.poll() is None:
                sampler.kill()
                sampler.wait(timeout=1)
            sampler.stdout.close()

        # A wrapper may exit first; terminate only the new workload's remaining group.
        child = subprocess.Popen([sys.executable, "-c",
            "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(1,flush=True); time.sleep(30)'],"
            "stdout=subprocess.PIPE); p.stdout.readline(); print(p.pid,flush=True)"], stdout=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            descendant = int(child.stdout.readline())
            assert wait_workload(child, 3, True) == 0
            with mock.patch.object(os, "killpg", side_effect=owned_signal):
                assert stop_workload(child, 0.05, True)
            assert child.poll() is not None
            for _ in range(50):
                try:
                    if _read_identity(descendant)[0] == "Z":
                        break
                except ProcessLookupError:
                    break
                except FileNotFoundError:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("owned workload descendant remained live")
        finally:
            stop_workload(child, 0.05, True)
            child.stdout.close()

    # Missing dma-buf identity cannot be called a known number of unique objects.
    with mock.patch.object(os, "readlink", return_value="/dmabuf"), \
         mock.patch.object(os, "stat", return_value=SimpleNamespace(st_ino=1)), \
         mock.patch.object(Path, "read_text", return_value="exp_name: fake\nsize: 4096\n"):
        dma = _read_dmabufs(123, ["3", "4"])
    assert dma["references"] == 2 and dma["unique_objects"] is None and dma["unique_bytes"] is None
    unknown = dict(baseline, dmabuf=dma, dmabuf_references=2,
                   dmabuf_objects=None, dmabuf_bytes=None)
    assert not summarize([unknown, unknown], limits, acceptance=True)["passed"]
    print("resource-churn adversarial regressions: PASS")


def limits_from_args(args):
    return {
        "fds": args.max_fd_growth,
        "maps": args.max_map_growth,
        "mapped_bytes": args.max_mapped_growth_kib * 1024,
        "vmrss_kib": args.max_rss_growth_kib,
        "dmabuf_references": args.max_dmabuf_reference_growth,
        "dmabuf_objects": args.max_dmabuf_object_growth,
        "dmabuf_bytes": args.max_dmabuf_growth_kib * 1024,
    }


def add_record_options(parser):
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-fd-growth", type=int, default=0)
    parser.add_argument("--max-map-growth", type=int, default=0)
    parser.add_argument("--max-mapped-growth-kib", type=int, default=0)
    parser.add_argument("--max-rss-growth-kib", type=int)
    parser.add_argument("--max-dmabuf-reference-growth", type=int, default=0)
    parser.add_argument("--max-dmabuf-object-growth", type=int, default=0)
    parser.add_argument("--max-dmabuf-growth-kib", type=int, default=0)
    parser.add_argument("--warmup-seconds", type=float, default=0.0)
    parser.add_argument("--acceptance", action="store_true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("self-test")
    subparsers.add_parser("_child")
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--pid", type=int, required=True)
    monitor_parser = subparsers.add_parser("monitor")
    monitor_parser.add_argument("--pid", type=int, required=True)
    add_record_options(monitor_parser)
    run_parser = subparsers.add_parser("run")
    add_record_options(run_parser)
    run_parser.add_argument("--exit-timeout", type=float, default=1.0)
    run_parser.add_argument("workload", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "_child":
        return _child()
    if args.command == "snapshot":
        print(json.dumps(snapshot(args.pid), sort_keys=True))
        return 0
    try:
        validate_options(args.samples, args.interval, limits_from_args(args),
                         args.acceptance, args.warmup_seconds,
                         getattr(args, "exit_timeout", 1.0))
    except ValueError as error:
        parser.error(str(error))
    if args.command == "monitor":
        result = record(args.pid, args.output, args.samples, args.interval,
                        limits_from_args(args), args.acceptance,
                        warmup=args.warmup_seconds)
        return 0 if result["passed"] else 1
    if not args.workload:
        parser.error("run requires a workload after --")
    workload = args.workload[1:] if args.workload[0] == "--" else args.workload
    if not workload:
        parser.error("run requires a workload after --")
    def interrupted(signum, frame):
        del frame
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    process = None
    try:
        process = subprocess.Popen(workload, start_new_session=True)
        result = record(process.pid, args.output, args.samples, args.interval,
                        limits_from_args(args), args.acceptance, process,
                        args.warmup_seconds, args.exit_timeout, process_group=True)
        return 0 if result["passed"] else 1
    finally:
        # Also cover an interrupt immediately after Popen returns.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if process is not None:
                stop_workload(process, 0.25, True)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
