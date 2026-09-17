# Concurrent API stress schedules

**AI disclosure:** This document, including its title and all prose, was generated
by AI at the repository owner's request.

This is the schedule and evidence record for
[libva-v4l2_request#36](https://github.com/iconidentify/libva-v4l2_request/issues/36)
(*Stress concurrent API calls, context teardown and frame access*). It covers the
**offline** schedules: what runs, with which seeds, what the exact expected results
are, and what is deliberately **not** claimed here. The guarded-hardware campaign
(ten repetitions of each schedule on a qualified M1 device through
`tests/hwguard.py`) is the separate hardware gate of #36 and is documented as a
runbook below; it has not been run as part of the offline work.

## What the offline harness proves — and what it cannot

`tests/concurrent-stress.c` links the real driver sources against an in-memory
model V4L2 device. There is no decoder or kernel module:
the model completes queued requests after a seeded number of model-time ticks,
and time only advances when the driver polls or reads its monotonic clock, so
the schedules advance model waits without a real decoder.

**The offline part of in-driver overlap (AC2) is measured inside the driver.**
On one VA display, `api_mutex` serializes locked entrypoints. The hook counts
outer unlocked calls that enter while another thread holds a locked section;
unlocked calls can also overlap each other, which is outside this metric.
Nested image/buffer helpers and calls inside the same thread's locked section
are excluded. The global counters require a quiescent, single-display test;
they are disabled by default, with one relaxed enabled-flag load per bracket.

The `overlap` and `overlap-actor` cases hold stream 0's first model request.
Other decoders wait for a latched release signal that remains set, so a delayed
worker cannot miss the entire close/open interval and deadlock. The model poll
confirms the selected reader is inside `vaSyncSurface` holding `api_mutex` before
collecting evidence. The model clock stays frozen and short real sleeps pace
this held window. A thread-local counter delta then requires at least eight
outer unlocked calls from the selected decoder or failure actor during that
window, excluding earlier traffic. The actor must also perform an invalid buffer
map with the exact expected error. Only then may the gate release.

`overlap-late` deliberately delays a decoder until after release; it reproduces
the missed-pulse ordering that hung the original gate. `overlap-counters` checks
that nested helpers and same-thread calls cannot inflate the evidence. Output
includes `driver_overlap locked=… unlocked=… over=… max_locked=… max_unl=…
gated_thread_events=…`; the last field is the selected worker's held-window delta.

The schedules exercise the real public API surface the same way a threaded
client does:

* locked entrypoints from `v4l2r_lock_surface_api()` — `vaCreateContext`,
  `vaBeginPicture`, `vaRenderPicture`, `vaEndPicture`, `vaSyncSurface`,
  `vaQuerySurfaceStatus`, `vaExportSurfaceHandle`, `vaDeriveImage`,
  `vaGetImage`, `vaDestroyContext`, `vaDestroySurfaces` — called from many
  threads at once;
* deliberately interleaved with the **unlocked** entrypoints
  (`vaCreateSurfaces2`, `vaCreateBuffer`, `vaMapBuffer`, `vaUnmapBuffer`,
  `vaDestroyBuffer`, `vaCreateImage`, `vaDestroyImage`), the way FFmpeg splits
  decode and filter threads over one VA display;
* context teardown concurrent with decode and readback of other streams, and
  surviving derived images/surfaces read back through the preserved state
  after `vaDestroyContext`.

The model device is the decode oracle: the slice bytes a stream renders into a
picture are hashed (FNV-1a) when the request is queued, and the completion writes
a byte pattern derived from that hash into the target CAPTURE plane. Readers
recompute the same pattern from the frame bytes, so a lost frame, cross-stream
pixels or a stale buffer reuse all fail an exact byte comparison. The decoder
waits for a surface's previous frame to be read back before decoding into that
surface again, matching a real decoder surface pool (the kernel contract covers
decode completion, not client readback).

What this harness cannot prove: anything about a real AVD decoder, the kernel's
own serialization, real dma-buf fences, or throughput. Those need the guarded
hardware campaign. Offline schedules are seeded and deterministic in their
results, including the mid-decode teardown victim's verified frame count
(exactly half its frames, byte-exact): OS thread interleaving still varies
between runs (that is the point), but no pass/fail outcome or recorded hash
depends on it — the forced boundaries (first-call rendezvous, midpoint
teardown, actor handshake) make the proven events happen every repetition.

## Schedules and seeds

Registered Meson cases (10 repetitions each; per-repetition seeds are derived
deterministically from the recorded base seed and the repetition index and are
printed in each `rep` line):

| Meson test | Schedule | Base seed | Frames/stream |
| --- | --- | --- | --- |
| `concurrent-threads-1/2/4` | `threads N 12 10` | `549203187` | 12 |
| `concurrent-teardown-1/2/4` | `teardown N 12 10` | `812734691` | 12 |
| `concurrent-failure-4` | `failure 4 12 10` | `3372110043` | 12 |
| `concurrent-overlap-2` | `overlap 2 6 10` (in-driver gate) | `73204115` | 6 |
| `concurrent-overlap-actor-2` | `overlap-actor 2 6 10` (actor fires into the gate) | `1946285037` | 6 |
| `concurrent-processes-1/2/4` | `concurrent-process.py` (3 reps) | `0xC0FFEE` | 12 |
| `concurrent-overlap-late-2` | `overlap-late 2 6 10` (late waiter) | `73204115` | 6 |
| `concurrent-overlap-counters` | outer-call and thread-ownership oracle | none | none |
| `concurrent-tsan` | TSan build, 9 schedules × 3 reps plus counter oracle | per schedule | 12/6 |

Each stream uses its own mixed profile (H.264/HEVC/VP9 model codecs), surface
dimensions (64x48, 64x64, 128x96, 96x64), a seeded surface rotation and seeded
readback modes (client image with pitch-strided verification, or exported
dma-buf read directly through the model plane storage). Per-stream results are
printed as `stream <id> frames=<n> MD5=<hex>` where the MD5 covers the verified
frame sequence.

Deterministic lifetime events and remaining overlap evidence:

* The teardown victim holds a staged picture at frame FRAMES/2 until context
  destruction completes. Its subsequent buffer creation must return exactly
  `INVALID_CONTEXT`; any other failure aborts the test. All preceding frames are
  verified, and survivor reads wait for completed teardown.
* The first-call rendezvous coordinates callers **outside** the driver. The earlier
  `overlap=` metric counted that barrier and has been removed. It did not prove #36
  AC2. The held-window instrumentation above now covers the offline part; real
  hardware overlap remains unqualified.
* The failure actor checks a foreign surface while its owner's context is alive.
  Its handshake proves lifetime ordering only: an unfinished reader may be waiting
  for teardown, so it cannot establish concurrent active API work.

`concurrent-process.py` runs the same single-stream worker (`concurrent-stress
worker ID FRAMES SEED`, where the worker id selects the stream recipe: model
codec, dimensions and content) in 1/2/4 separate processes behind a start
barrier (one stdin byte). It derives every worker's expected digest
independently from the declared seed/frame recipe (splitmix64/FNV-1a/MD5, the
same arithmetic as the harness) and compares exactly — a syntactically valid
but wrong digest fails. The runner validates its arguments (bounded process,
frame and repetition counts, finite positive deadline), the deadline covers
process creation and the barrier release as well as the run, and cleanup
kills each owned worker group before reaping its leader, with bounded waits and
one-time ownership release. Output uses temporary files, so an escaped descendant
cannot keep pipe EOF pending. SIGTERM/SIGINT request cancellation and cleanup. Its `--self-test` runs real negative
fixtures with actual child processes (stalled worker, inherited stdout
holder, launch failure), plus parent interruption and escaped-output fixtures and, against a real schedule, confirms the derived
digests match the workers' output. Offline this proves per-process integrity
and clean teardown with no shared state; contention for one real decoder is
the hardware gate.

`concurrent-tsan.sh` builds the stress harness with `-Db_sanitize=thread` in a
fresh build directory and runs the threaded, teardown and failure schedules.
It uses the same compiler Meson would (`CC`, default `cc`) for its runtime
probe, and classifies probe failures: known runtime-unavailable startup
signatures skip with exit 77 and the printed reason, while any other nonzero
probe outcome — including a real ThreadSanitizer diagnostic — fails the
check; the executed/skip outcome is printed as evidence. Any report in the
schedules fails the check; reports name their frames so driver-scope findings
(both accesses inside `src/`) can be turned into regressions, while harness-
or uninstrumented-library-scope reports are classified during review rather
than silently accepted.

## Offline results

Historical contributor results on `94cffec` (superseded by maintainer integration
validation for the final source; these counts do not describe the integrated suite).
Recorded from the validation runs of this work (Ubuntu 24.04.5 LTS, aarch64,
GCC 13.3.0, meson 1.3.2, ninja 1.11.1, libva 1.20.0, libdrm 2.4.125,
`-Db_sanitize=address,undefined`; commands and raw output retained in the
evidence log):

* Baseline on the base commit before any change: **127/127** Meson cases,
  `tests/frame-check.sh` and `tests/shared-contexts.sh` software checks pass.
* With the new schedules (after the review remediation): **139/139** Meson
  cases — the baseline set plus `concurrent-threads-1/2/4`,
  `concurrent-teardown-1/2/4`, `concurrent-failure-4`,
  `concurrent-processes` (self-test with real negative fixtures),
  `concurrent-processes-1/2/4` and `concurrent-tsan` (which ran, not skipped).
  The `threads` and `failure` schedules verify **12/12 frames per stream in
  every repetition**; the `teardown` victims verify **exactly 6/12**
  (the forced midpoint), byte-exact in every case. The earlier reported overlap
  maximum is withdrawn because it counted a pre-call barrier. Flakiness check: 10 consecutive full
  runs each of `failure 4 12 10` and `teardown 4 12 10` (100 repetitions per
  schedule) with zero failures; the reviewer's two deterministic mutation
  windows (destroy between BeginPicture and buffer creation; a late-arriving
  actor) are now forced or made safe by construction.
* Per-configuration registered counts (re-measured, including the new cases):
  139 on 6.8 UAPI with all codecs, 132 on 22.04/5.15, 123 on 20.04/5.4,
  128 with all codecs disabled, 135 with HEVC forced and VP9 disabled.
* Clang 18.1.3: the harness compiles warning-free, links and passes a
  threaded schedule run (the local image lacks clang's ASan runtime files,
  so the clang sanitizer combination itself is left to the CI matrix where
  the runtime is installed).
* ThreadSanitizer (GCC 13.3, `-Db_sanitize=thread`, fresh build): the
  threaded, teardown and failure schedules run with **zero reports** in the
  driver or the harness on this host.

## Guarded-hardware runbook (not run offline)

The hardware repetitions of these schedules need an exclusive `tests/hwguard.py`
lease on a qualified device and are not claimed by the offline work. The
offline-equivalent steps for a device owner:

1. Build an unsanitized driver (`meson setup build && meson compile -C build`).
2. Take the hardware guard lease with a finite deadline and a journal
   since-stamp; close all other video clients first.
3. Run the process schedule against the real driver with
   `LIBVA_DRIVERS_PATH=<build>/src`. **Note: this still needs a VA-API
   process worker to be implemented first** — the offline worker links the
   driver directly in-process and cannot be switched to hardware by setting
   `LIBVA_DRIVERS_PATH`; a worker that decodes through a real VA display
   (frame-check-style) is a separate follow-up item before this campaign.
   Record per-worker hashes, deadlines, guard logs and kernel journal deltas.
4. Ten repetitions of each declared schedule, comparing the exact passing sets
   and per-stream hashes against the software references; any kernel fault,
   stuck task or abandoned decoder holder fails the criterion and stops the
   campaign.
5. Historical HEVC corruption findings are reported to their dedicated ticket
   (#39/#43), never hidden inside this one.

## Limits

* No hardware was opened, installed, rebooted or kernel-tested for the offline
  work; the hardware criteria of #36 remain open until the guarded campaign
  above runs on a qualified device.
* The model decoder implements the V4L2 M2M request flow the driver uses
  (CREATE_BUFS/QUERYBUF/QBUF/DQBUF/requests/EXPBUF) — it does not model kernel
  scheduling, fences, or format conversion; converter paths are covered by
  other offline cases and the hardware matrix.
* TSan coverage depends on the runtime being permitted in the environment;
  the skip is loud (exit 77 with the reason), never silent.

## Maintainer integration

Process cleanup, exact teardown status and TSan startup classification were fixed
in the maintainer integration. Unknown TSan failures now fail; only enumerated
startup incompatibilities skip. Compiler commands with arguments are parsed as an
argument vector and used consistently for probe and build. Hermetic regressions
exercise startup classification and actual cancellation/cleanup. #36 stays open for
the real VA-API worker and guarded hardware runs. The subsequent #86 integration
adds the offline held-window overlap evidence above.


### Adversarial review of #86

The contributor gate could miss its close/open pulse and wait forever, reproduced
with a deliberately delayed worker and consistent with the failing oldest-UAPI
CI job. Whole-repetition counters could also satisfy the gate with earlier or
other-thread calls, and nested image/buffer helpers inflated call counts. The
latched release, identified sync waiter, per-worker delta, exact actor error and
counter oracle address these findings. Maintainer corrections were self-reviewed;
this is not a claim of an independent second review of those corrections.

The original mixed-codec CI failure also exposed a resource-test timing assumption:
50 ms did not ensure an early-exit fixture had exited. The fixture now establishes
that condition explicitly. During review of that path, the existing resource
sampler was found to reap its owned group leader before the final group signal.
It now observes exit without reaping, reserves the group ID through the final
signal, and tests successful/failed leaders plus a surviving descendant. No
hardware evidence or codec support count is changed by these offline fixes.
