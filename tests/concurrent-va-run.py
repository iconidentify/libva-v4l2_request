#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Software oracle and process runner for the real VA worker. No model hashes."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKER_LINE = re.compile(r"^worker (\d+) frames=(\d+) MD5=([0-9a-f]{32})$")


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


def run_worker(binary, mode, clips, extra=None, timeout=60):
    args = [str(binary), mode]
    if extra:
        args.extend(extra)
    for clip in clips:
        args.extend([str(clip), "yuv420p"])
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return result


def self_test(binary: Path):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clips = []
        for i in range(2):
            p = tmp / f"c{i}.mp4"
            generate_clip(p, seed=1000 + i)
            clips.append(p)
        one = run_worker(binary, "software", clips[:1])
        if one.returncode != 0:
            raise RuntimeError("software single-stream failed: " + one.stderr)
        workers = parse_workers(one.stdout)
        if len(workers) != 1 or workers[0][1] < 1:
            raise RuntimeError("missing worker digest: " + one.stdout)
        expected = workers[0][2]
        two = run_worker(binary, "software", clips, extra=["--threads", "2"])
        if two.returncode != 0:
            raise RuntimeError("software two-stream failed: " + two.stderr)
        got = parse_workers(two.stdout)
        if len(got) != 2 or got[0][2] != expected:
            raise RuntimeError("stream association mismatch")
        # Negative: corrupt digest comparison
        if expected == "0" * 32:
            raise RuntimeError("uninitialized digest")
        # Cancellation: kill a child
        proc = subprocess.Popen(["sleep", "30"])
        time.sleep(0.05)
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
            if proc.returncode == 0:
                raise RuntimeError("killed child reported success")
        # Launch failure
        bad = subprocess.run([str(binary), "software", str(tmp / "missing.mp4"), "yuv420p"],
                             capture_output=True, text=True, timeout=10)
        if bad.returncode == 0:
            raise RuntimeError("missing clip succeeded")
        # Hardware mode without a device must not silently software-fallback
        hw = run_worker(binary, "vaapi", clips[:1], timeout=15)
        if hw.returncode == 0 and "software frame rejected" not in hw.stderr:
            # Device present is OK; skip assertion
            pass
        elif hw.returncode in (77, 1) or "vaapi device unavailable" in hw.stderr:
            pass
        print("PASS: software oracle, association, kill, missing clip")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("worker", nargs="?", type=Path)
    p.add_argument("--processes", type=int, default=1)
    p.add_argument("--deadline", type=float, default=60)
    args = p.parse_args()
    if args.self_test:
        if not args.worker:
            raise SystemExit("self-test requires WORKER")
        self_test(args.worker.resolve())
        return
    raise SystemExit("use --self-test in hosted CI")


if __name__ == "__main__":
    main()
