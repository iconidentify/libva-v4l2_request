# M1 resource acceptance — 2026-09-17

The fresh early-export soak completed **3600.168 measured
seconds** (16,298 complete measured lifecycles), satisfying the uninterrupted
60-minute gate in [#41](https://github.com/iconidentify/libva-v4l2_request/issues/41).
Both fresh 1,000-cycle campaigns, the interrupted-client check and subsequent decode,
and the exact selected-build codec comparison also completed. All outer guards ended
healthy and idle, with no new decoder fault, deadline, wedge or foreign holder.

This qualifies the declared resource workload on the selected M1 build. It is not a
new codec support, concurrency, browser/display, performance or boot-stability claim.
The installed package and package pin are unchanged.

## Identity and recovery

- Tested production source: `299d29333bc215d9d045130a0e0c6b8558a6654a` (merged #80).
- Production `src` tree: `2b48a40943abe31554c3cc8fbf9ce5ed83033b2a`.
- Selected release driver SHA-256:
  `ce5c4513e72d1178f3bcb695f8acd76ad298564df8077eb408722cc447fb6f63`.
- Publication base: `27da69dd5fcb438deab970061a2edcc68a9e1d93` (merged #81).
  Its production source and resource harness are identical to the tested source.
  Guard logs record checkout HEAD: `299d293` for the two short campaigns and
  `27da69d` for the soak/follow-ups after #81 was integrated. Campaign metadata
  records the selected binary's build revision, `299d293`, throughout. The audit
  verifies the unchanged production source and every recorded harness-file hash;
  neither raw revision is rewritten to conceal the distinction.
- Apple M1 T8103 / `apple,j293`, kernel `7.1.13-3-1-ARCH`, linux-asahi
  `7.1.13.asahi3-1`, libva `2.24.1-1`, libdrm `2.4.134-1`, FFmpeg `2:9.0.1-4`.
- Installed `libva-v4l2_request-avd 1.3.r11-2` was not replaced. Tests explicitly select
  the separate release build with `LIBVA_DRIVERS_PATH`.
- Selected on-disk module SHA-256:
  `e50540e1d0fc48c7e0bf9b21ff028758bdb35f94aa7070af00de88e61e5c70e9`.
  This records the file selected for loading, not a measured in-memory module hash.

An earlier firmware timeout at `2026-09-17T01:25:32.746612+00:00` blocked preflight.
Its cause remains unknown. The user closed video apps, saved work and expressly
authorized one unload/reload of the existing module. Both operations succeeded at
02:16:40 UTC. Every accepted run uses the fixed `2026-09-17 02:16:40 UTC` journal
boundary tied to that recovery. No further reset or cutoff advancement occurred.
The [dimension evidence](../dimension-hardware-2026-09-17/README.md) retains the earlier
fault/preflight records; this archive also includes `authorized-recovery.json`.

## Completed campaigns

Each lifecycle decodes/drains a 24-frame clip, seeks/flushes and decodes it again.
The four fixed synthetic 640x360 clips cover H.264, HEVC, VP9 8-bit and VP9 10-bit.
One initialized VA display survives every checkpoint in each campaign. Twenty
warmup lifecycles precede measurement; frame/held-image totals include warmup.

| Run | Measured cycles | Measured seconds | Exact frames | Held-image checks | Peak mapped growth, bytes | Peak RSS growth, KiB | Peak allocator-accounted growth, bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| normal-1000 | 1,000 | 211.648 | 48,960 | 1,020 | 0 | 48 | 10016 |
| early-1000 | 1,000 | 221.385 | 48,960 | 1,020 | 0 | 0 | 9536 |
| soak | 16,298 | 3600.168 | 783,264 | 16,318 | 0 | 0 | 9536 |
| post-interrupt | 4 | 0.844 | 1,152 | 24 | 0 | 0 | 592 |

The completed campaigns contain **882,336 exact frame comparisons** and
**18,382 surviving-image checks**. Each early-export frame additionally has a
successful dma-buf identity/layout check. The report builder independently replays
the ordered raw frame hashes, aggregate digests, checkpoint/held-image sequences and
early-export records against the independent software references; it does not rely
only on the runner's pass flag.

FD count is exactly four and process dma-buf references/objects/bytes return to the
initialized-display state at every accepted checkpoint. Mapping count does not grow.
The normal 1,000-cycle run's mapped bytes decrease by 2 MiB alongside a 2 MiB allocator
arena decrease; RSS peaks 48 KiB above baseline and ends 1,328 KiB below it. These
decreases are preserved, so the report does not call every curve flat. All other
numeric ranges and allocator windows are retained in [validation.json](validation.json).

The reviewed limits are unchanged: no FD/dma-buf/mapping-count growth, at most 4 MiB
mapped/RSS growth, at most 64 KiB allocator-accounted growth, and every positive
mapped-byte change explained by allocator arena/mmap accounting. These are bounds
for this workload, not universal client limits. `none-observed` dma-bufs means no
process descriptors at quiescent checkpoints, not zero global kernel allocations.
The sampler does not measure descendants or global kernel memory.

## Interrupted client and codec checks

After the soak, the interruption wrapper bound the recorded decoder child by pidfd,
verified its start time and parent, and intentionally sent SIGKILL after completed
checkpoints. That inner campaign remains **failed by design**. The wrapper verified
failure and child exit, its outer guard passed healthy/idle, and a fresh four-cycle
normal run passed. Process-exit reclamation is not counted as in-process ownership
evidence.

The same selected binary then preserved every r11 passing vector:

| Suite | Hardware passes / tested | Scope |
| --- | ---: | --- |
| HEVC | 144/147 | Full suite; known wrong output retained |
| AVC | 73/135 | Full suite; no fallback counted |
| FRExt | 27/69 | Full suite with explicit FFmpeg High 10 opt-in |
| VP9 | 216/305 | Full suite |
| VP9 high depth | 1/1 | Selected 10-bit 4:2:0 vector; five other suite entries unqualified |
| AVC profile override | 5/5 | Explicit subset, separate from default AVC count |

The strict AVC comparator remains non-green for the known `FM1_FT_E` software fallback.
It is explicitly recorded and contributes no hardware pass. HEVC category changes
from coarse historical `decode_error` to observed `checksum_mismatch` remain visible;
they do not erase the known wrong-output failures. Complete summaries, per-vector
results, frame hashes, diagnostics, comparisons and pinned corpus identities are
archived. External corpus media is not redistributed.

## Prior failures, review and reproduction

The previous 616.048-second and 1,426.146-second foreign-client-aborted soaks remain
failed/incomplete in the [prior report](../resource-churn-2026-09-16/README.md) and its
immutable archive. Their durations are not combined with this fresh hour. Earlier
calibration/fixture failures are likewise retained without reclassification.

The selected release build previously passed 177 offline cases with actual TSan;
#80 supplies ASan/UBSan and software-check evidence for the identical production
source. This evidence-only change does not modify production code, resource harnesses
or thresholds. Review is maintainer self-review, not an independent review.

The [measurement contract](../RESOURCE_CHURN.md) documents commands and thresholds.
The [archive](raw-evidence.tar.xz) includes `run-campaign.sh`, `codec-suites.py`,
`make-evidence.py`, the media manifest, exact synthetic inputs/software references,
every resource checkpoint and workload/stderr record, guards and post-soak codec
output. Run commands require a freshly claimed exclusive guarded window and fresh
output paths; do not replay an existing output directory or bypass a fault preflight.
Helper binaries are represented by hashes rather than redistributed.

The [archive manifest](archive-manifest.json) records original and published member
SHA-256 values. Only the maintainer home-directory prefix is replaced with
`<workspace>` in public text; synthetic media bytes are unchanged. All
1,394 members were re-read and hash-verified after creation.
The archive SHA-256 is `cf42950f269ba3392d6795ddf4197f7def08a052283119d30d9cff1ac78c5d7d`.

No kernel edit, installation, reboot, client/display configuration or support-tier
promotion occurred. Concurrency, fuzz-duration, performance and broader platform
release gates remain in their owning open tickets.
