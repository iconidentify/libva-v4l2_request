#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Software oracle and process runner for the real VA worker. Never opens VA."""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKER_LINE = re.compile(r"^worker (\d+) frames=(\d+) MD5=([0-9a-f]{32})$", re.M)
KEPT_LINE = re.compile(r"^worker (\d+) kept=(\d+)x(\d+)$", re.M)


def generate_clip(path: Path, seed: int, frames: int = 4):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc=size={64 + 2 * (seed % 4)}x64:rate=2:duration={frames / 2}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "1", "-preset", "ultrafast",
        str(path),
    ]
    subprocess.run(cmd, check=True, timeout=30)


def parse_workers(text: str):
    out = []
    for line in text.splitlines():
        m = WORKER_LINE.match(line)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return out


def run_worker(binary, clips, extra=None, timeout=60):
    args = [str(binary), "software"]
    if extra:
        args.extend(extra)
    for clip in clips:
        args.extend([str(clip), "yuv420p"])
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def expect_ok(result, label):
    if result.returncode != 0:
        raise RuntimeError(label + " failed: " + result.stderr + result.stdout)
    return parse_workers(result.stdout)


def self_test(binary: Path, processes: int, deadline: float):
    if processes < 1 or processes > 4:
        raise SystemExit("invalid --processes")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clips = []
        for i in range(2):
            p = tmp / f"c{i}.mp4"
            generate_clip(p, seed=1000 + i)
            clips.append(p)
        oracle = [expect_ok(run_worker(binary, [clip]), "oracle " + str(i))[0]
                  for i, clip in enumerate(clips)]
        two = expect_ok(run_worker(binary, clips, extra=["--threads", "2"]), "two-stream")
        if len(two) != 2:
            raise RuntimeError("expected two workers: " + str(two))
        for i in range(2):
            if two[i][1] != oracle[i][1] or two[i][2] != oracle[i][2]:
                raise RuntimeError("worker %d digest/count mismatch" % i)
        if two[0][2] == two[1][2]:
            raise RuntimeError("distinct clips produced identical digests")
        flipped = two[0][2][-1] + two[0][2][:-1]
        if flipped == two[0][2] or flipped == oracle[0][2]:
            raise RuntimeError("digest mutation collapsed")
        # --invalid must fail via an unopened-context send, not UAF.
        inv = run_worker(binary, clips[:1], extra=["--invalid"])
        if inv.returncode == 0:
            raise RuntimeError("invalid API succeeded")
        if "invalid API succeeded" in inv.stderr:
            raise RuntimeError(inv.stderr)
        tear_raw = run_worker(binary, clips[:1], extra=["--teardown"])
        if tear_raw.returncode != 0 or not KEPT_LINE.search(tear_raw.stdout):
            raise RuntimeError("teardown did not keep a decoded frame: " + tear_raw.stdout)
        tear = parse_workers(tear_raw.stdout)
        if not tear or tear[0][1] < 1:
            raise RuntimeError("teardown produced no hashed frames")
        missing = run_worker(binary, [tmp / "missing.mp4"])
        if missing.returncode == 0:
            raise RuntimeError("missing clip succeeded")
        # Cancel the actual worker, not an unrelated process.
        fifo = tmp / "blocked.h264"
        os.mkfifo(fifo)
        proc = subprocess.Popen([str(binary), "software", str(fifo), "yuv420p"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        if proc.poll() is not None:
            raise RuntimeError("worker exited before cancel: %s" % proc.returncode)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        if proc.returncode == 0:
            raise RuntimeError("killed worker reported success")
        # --processes / --deadline: independent software oracles in parallel.
        deadline_end = time.monotonic() + deadline
        kids = []
        try:
            for i in range(processes):
                kids.append(subprocess.Popen(
                    [str(binary), "software", str(clips[i % 2]), "yuv420p"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for kid in kids:
                remaining = deadline_end - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("process deadline")
                out, err = kid.communicate(timeout=remaining)
                if kid.returncode != 0:
                    raise RuntimeError("process worker failed: " + err)
                got = parse_workers(out)
                if len(got) != 1 or got[0][1] < 1:
                    raise RuntimeError("process worker missing digest")
        finally:
            for kid in kids:
                if kid.poll() is None:
                    kid.kill()
        print("PASS: independent software oracles, invalid API, teardown keep, kill, processes")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("worker", nargs="?", type=Path)
    p.add_argument("--processes", type=int, default=2)
    p.add_argument("--deadline", type=float, default=60)
    args = p.parse_args()
    if args.self_test:
        if not args.worker:
            raise SystemExit("self-test requires WORKER")
        self_test(args.worker.resolve(), args.processes, args.deadline)
        return
    raise SystemExit("use --self-test; vaapi is not a software path")


if __name__ == "__main__":
    main()
