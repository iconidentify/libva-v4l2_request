# Resource churn and ownership bounds

Issue [#41](https://github.com/iconidentify/libva-v4l2_request/issues/41)
separates exact userspace ownership from real-device resource measurements. The
offline model proves that the driver releases what it owns while the initialized
VA display remains alive. It does not prove that a kernel releases video buffers,
that firmware remains healthy, or that RSS has a universal platform-independent
limit.

The [2026-09-17 M1 acceptance](resource-churn-2026-09-17/README.md) records fresh
normal and early-export 1,000-cycle campaigns and an uninterrupted
3600.168-second early-export soak (16,298 measured lifecycles),
followed by intentional-client-exit/recovery checks and exact selected-build codec
pass-set preservation. Completed acceptance campaigns satisfy the unchanged resource bounds;
the report retains actual ranges rather than assuming every curve is flat.
The [earlier evidence](resource-churn-2026-09-16/README.md) preserves both aborted
soaks and calibration failures as failed/incomplete runs. This qualifies the fixed
M1 workload, not browser rendering, concurrent API use, other resolutions or boot
stability; the installed driver package is unchanged.

## Offline ownership model

`tests/failure-cleanup.c` wraps allocation, mapping, descriptor and fake V4L2
operations. Its resource snapshot records:

- live model descriptors by video, media, request, dma-buf and converter kind;
- mapping count and mapped bytes;
- non-handle heap allocation count and bytes;
- live config, context, surface, buffer and image objects; and
- handle-table backing arrays and bytes as a separate bounded high-water cache.

The initialized-driver baseline contains one config and the five initial handle
tables. Each table starts with 32 slots. Tables may grow geometrically to the
peak concurrent object count and retain that capacity until `vaTerminate`; live
objects are still required to return to zero. Per-context OUTPUT/CAPTURE storage,
cached exports and mappings must disappear when that context lifecycle ends.

Three sanitizer cases each run 1,000 complete lifecycles in one initialized
driver and assert exact ownership balance after every cycle:

- `failure-churn-normal`: decode, synchronize, export, close caller-owned export
  descriptors, destroy the surface and destroy the context;
- `failure-churn-early-export`: export and close a standalone backing before the
  first decode, decode into it, then destroy the lifecycle; and
- `failure-churn-held-image`: derive and map an image, destroy the context, prove
  the surviving image remains readable, then destroy the image and surface.

Each case prints checkpoints at cycles 100, 500 and 1,000. Model descriptors are
generation-tagged and closed slots are reused, so the campaign cannot hide a
stale or double close and does not exhaust a monotonically increasing fixture ID.
The existing operation-index failure sweeps also destroy all contexts, images,
buffers and surfaces without terminating the driver, assert lifecycle balance,
complete a fresh decode, and assert balance again. Final `vaTerminate` accounting
remains a separate zero-resource check rather than the only cleanup proof.

```sh
meson setup build-resource -Db_sanitize=address,undefined
meson test -C build-resource --print-errorlogs \
  failure-churn-normal failure-churn-early-export failure-churn-held-image
```

## Process-local measurements

`tests/resource-churn.py` reads only `/proc/<pid>` for the selected process. It
records descriptor count, mapping count and virtual bytes, `VmRSS`, `VmHWM`, the
RSS components reported by the kernel, and dma-buf fdinfo when present. It never
reads global debugfs dma-buf data or another application's media. Duplicate
dma-buf descriptors are counted as references and deduplicated by fdinfo identity
where the kernel exposes one. Missing identity makes unique-object/byte metrics
unavailable; it is not converted into a made-up identity per descriptor.

The dma-buf result is `observed`, `none-observed`, or `unavailable`; unavailable
accounting is never reported as zero and fails acceptance mode. Reference,
unique-object and byte growth have separate thresholds. Every JSONL metadata,
sample and summary record is flushed and fsynced. Acceptance mode requires an
explicit RSS-growth threshold because libc and sanitizer retention make a
universal threshold misleading. It also fails when the target exits before every
requested sample is recorded, so a short run cannot stand in for a soak.

The campaign binds to the process start time before warmup and rejects identity
changes across samples. Timing options must be finite, with positive intervals
and at least two samples; invalid options fail before a workload starts. Sample
records include wall and monotonic clocks, and the summary records the elapsed
sample span. `/proc` reads are not an atomic process snapshot: detected races fail,
but exact resource checkpoints still require the workload to be quiescent.
Only the executable basename is recorded, not command arguments that might contain
media paths or credentials. Preserve reviewed workload/input provenance separately.

```sh
python3 tests/resource-churn.py self-test

V4L2R_SOURCE_COMMIT=$(git rev-parse HEAD) \
python3 tests/resource-churn.py monitor \
  --pid "$PID" --samples 11 --interval 1 --acceptance \
  --warmup-seconds <declared-warmup> \
  --max-fd-growth 0 --max-map-growth 0 --max-mapped-growth-kib 0 \
  --max-dmabuf-reference-growth 0 --max-dmabuf-object-growth 0 \
  --max-dmabuf-growth-kib 0 \
  --max-rss-growth-kib <reviewed-bound> \
  --output resource-churn.jsonl
```

For a long-lived command, `run` starts the workload and records its process until
the requested sample count is reached. Its explicit warmup precedes the baseline,
so expected one-time process and decoder allocations are separated from measured
steady-state growth. Choose the interval and sample count to cover the declared
duration; an early or nonzero workload exit fails the summary. The command must
exit after its bounded campaign. A workload still running after the final sample
and `--exit-timeout` is terminated and the result fails rather than hanging the
sampler. `run` owns a new process group and cleans up that group on logging or
sampling exceptions as well as on timeout; forced cleanup fails acceptance. SIGINT
and SIGTERM propagate through bounded cleanup, including a guard's stop signal.
An interrupted campaign is incomplete even if it has no final summary.
`monitor --pid` only observes and never terminates its target. Descendant resource
usage is not sampled, so the measured decoder must be the selected process, not a
wrapper that runs the decoder in a child. If output cannot be written, the command
fails and cleans up, but cannot promise a durable summary on that broken output.

```sh
V4L2R_SOURCE_COMMIT=$(git rev-parse HEAD) \
python3 tests/resource-churn.py run \
  --samples 61 --interval 60 --acceptance \
  --max-fd-growth <reviewed-bound> --max-map-growth <reviewed-bound> \
  --max-mapped-growth-kib <reviewed-bound> \
  --max-rss-growth-kib <reviewed-bound> \
  --output soak.jsonl -- <workload> <arguments>
```

## Hardware boundary

The acceptance campaign still requires a capable M1, an exclusive lease through
`tests/hwguard.py`, an unsanitized selected userspace build, pinned valid media,
correct sample hashes, checkpoints at cycles 100/500/1,000, and a separate
60-minute soak. Record the driver commit, device, kernel/package/module identity,
guard log, raw resource JSONL and exact hashes. Stop at the first new decoder
fault or wedge and leave the decoder idle; do not reopen it as automatic
recovery.

Offline exact ownership is not hardware qualification. Process exit reclaim is
not accepted as proof of in-process cleanup, and RSS alone is not proof that
dma-bufs or kernel request descriptors were released.

## Synchronized real-device campaign

`resource-workload.c` keeps one initialized VA display alive across all cycles.
Four synthetic, redistributable clips cover H.264, HEVC, VP9 8-bit and VP9 10-bit
at 640x360, 24 frames each. Each lifecycle opens a decoder, decodes/drains the
clip, seeks to its beginning, flushes and decodes/drains it again. Every output
frame and aggregate digest must match a separate software decoder. Software
fallback in hardware mode is fatal. Each decoded frame is synchronized and
exported; caller-owned export FDs are closed.

A derived image survives decoder destruction in every hardware lifecycle. The
workload observes a successful `vaDestroyContext`, then requires the mapped image
bytes to remain identical. It retains the FFmpeg surface pool until after
`vaDestroyImage`, because destroying a surface with a live derived image is
correctly rejected. An initial smoke fixture got this ordering wrong and leaked
one client-owned surface per lifecycle; the checkpoint recorder detected it.
That fixture failure is not a production driver leak.

The C process pauses on a pipe before warmup and after every complete lifecycle
following 20 warmup cycles (five per codec). Python samples that exact decoder
PID while it is quiescent, verifies its process identity, writes and fsyncs the
record, then allows the next cycle. The VA display is not terminated at these
checkpoints. FD/dma-buf counts and bytes must equal the initial display state,
so retained allocations cannot be hidden in the warmup baseline. Mapping count
must not grow after warmup. Mapped-byte growth defaults to zero; the M1 campaign
explicitly allows at most 4 MiB of additional allocator arena capacity, with
two additional checks: every added mapped byte must be explained by glibc
arena/mmap accounting, and allocator-accounted allocations (including tcache)
must remain within 64 KiB of warmup. Unexplained mappings fail even below the
4 MiB ceiling. On allocators without mallinfo accounting, any mapping growth
fails. RSS has a separate 4 MiB allowance and its trend remains reviewable.

These are bounds for this fixed 640x360 workload: its largest 10-bit download,
planar conversion and packed hash buffers total approximately 2 MiB, and the
allocator may retain freed capacity after those temporary buffers disappear.
The short instrumented M1 campaign measured about 10 KiB of allocation/cache
growth that plateaued, with flat FD/dma-buf/mapping counts. This is consistent
with bounded allocator retention, not proof about every allocation in every
client. glibc's [dynamic mmap/trim thresholds](https://sourceware.org/glibc/manual/latest/html_node/Memory-Allocation-Tunables.html)
explain why freeing temporary buffers need not immediately shrink an arena.
Raw mallinfo arena/allocated/free/mmap values accompany every checkpoint.

The original zero-growth early-export campaign stopped at cycle 909 after a
2 MiB mapping increase. It had no allocator breakdown, so that event alone
cannot identify the retained allocation. Preserve it as a failed campaign;
do not relabel it as a pass. The added accounting and separately declared bounds
must be applied in a fresh complete campaign, and they do not establish a
universal bound for other streams/platforms.

`early` additionally uses the existing early-export interposer and requires one
successful dma-buf identity/layout check for every decoded frame. Both `normal`
and `early` require 1,000 measured lifecycles, with raw checkpoints including
100/500/1,000. A soak uses the same continuous mixed-codec work with a large finite
cycle cap and a minimum 3,600-second measured interval. It stops at a complete
lifecycle, not on a timer that could hide partial output. A guard provides an
outer finite deadline and watches faults and foreign clients throughout.

Example (prepare is entirely offline):

```sh
python3 tests/resource-campaign.py prepare --directory /absolute/campaign/media
python3 tests/resource-campaign-check.py

export LIBVA_DRIVERS_PATH=/absolute/selected-build/src
export LIBVA_DRIVER_NAME=v4l2_request
export V4L2R_SOURCE_COMMIT=$(git rev-parse HEAD)
python3 tests/hwguard.py --identity avd --deadline 600 \
  --log /absolute/campaign/normal-guard.jsonl -- \
  python3 tests/resource-campaign.py run --mode normal --cycles 1000 --max-mapped-growth-kib 4096 \
  --directory /absolute/campaign/media --output /absolute/campaign/normal.jsonl
# Repeat with --mode early and fresh output/guard log paths.
python3 tests/hwguard.py --identity avd --deadline 60 \
  --log /absolute/campaign/interrupt-guard.jsonl -- \
  python3 tests/resource-interrupt.py --directory /absolute/campaign/media \
  --output /absolute/campaign/interrupt.jsonl
python3 tests/hwguard.py --identity avd --deadline 3900 \
  --log /absolute/campaign/soak-guard.jsonl -- \
  python3 tests/resource-campaign.py run --mode early --cycles 1000000 --seconds 3600 --max-mapped-growth-kib 4096 \
  --directory /absolute/campaign/media --output /absolute/campaign/soak.jsonl
```

Output paths must be fresh. `inputs.json` retains generated input checksums,
generation commands, independent per-frame references, helper identities and
tool versions. The VP9 10-bit clip is a checksum-pinned CC0 fixture under
`tests/fixtures/resource/`; this preserves coverage on older libvpx builds that
cannot encode 10-bit VP9. Keep the manifest and actual clips with JSONL, workload and stderr
logs. Encoders/containers may produce different bytes across versions or runs;
the recorded files, not a promise of byte-identical regeneration, identify the
tested media. Recorder metadata identifies the selected driver binary. Guard
logs distinguish selected userspace from the unknown loaded kernel-module hash.
These measurements cover the decoder process, not global kernel memory or
another application's allocations. A result does not qualify browser rendering,
concurrent decoder calls, other dimensions or boot behavior.

The interrupted-client check requires Linux pidfds and Python's `os.pidfd_open`
and `signal.pidfd_send_signal` APIs. It waits for completed in-process checkpoints,
binds the recorded decoder child by pidfd plus start time and parent identity,
then sends SIGKILL to that exact process. Its expected result is a failed campaign
and a healthy, idle outer guard. A following normal campaign establishes continued
decode usability. Neither this process-exit case nor its reclaimed resources count
as in-process cleanup evidence. A stop signal to the interruption wrapper also
runs bounded coordinator cleanup; offline fixtures exercise responsive and stalled
coordinators without opening any decoder.

The offline campaign regression executes the real software workload, verifies
checkpoints/pixels, rejects a wrong reference and changed input, refuses unguarded
hardware, and interrupts a live client to verify bounded child cleanup. Meson
registers it when FFmpeg development dependencies are available; it requires
the libx264/libx265 and 8-bit libvpx encoders used by existing software frame checks.
