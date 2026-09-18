# M1 real-client concurrency qualification plan

AI-assisted maintainer plan and self-review; no independent reviewer is claimed.
Execution has not occurred at this plan commit. Owning ticket: #36; prior #94
worker implementation is merged and complete. Preserve grudev's earlier model,
forced-overlap and TSan evidence separately.

Use the unmodified client/runner/guard from merged source
`c77e7b566f7baf9c7a2aad797e62c9aa578d9687`. [Identities](identities.json) pins
source, built driver/worker, helper files, original installed module and the
matching currently loaded GNU build-ID note. The four [inputs](inputs.json)
record exact bytes and independent FFmpeg-CLI raw-pixel expectations. The raw
local manifest differs only in its absolute path/recipe fields and is separately
hash-pinned. Files were generated offline; ten software repetitions must pass
on these exact binaries/inputs before opening the device.

Execution follows [the merged runbook](../CONCURRENT_VA_HARDWARE.md): ten
repetitions × 1/2/4 threads or processes × normal/teardown/live-client EOF rejection
= 180 groups, 2,560 selected decoded frames. Software fallback cannot pass.
Per-group timeout 30 seconds, cleanup at most five seconds, outer guard 6,600
seconds. The existing kernel module stays loaded. No installation, module
unload/reload, boot setting, package or shipped-patch change is part of this plan.

The read-only preflight at 00:34:47 UTC found the original M1 module loaded,
video0/media0 idle, no holders, stuck tasks or whole-boot faults. Acquire a NEW
exclusive portable guard immediately around the actual matrix; preflight is
repeated by the guard and this snapshot is not a lease. The machine owner's
standing testing authorization is recorded in the private session; it is not
transferable permission for other contributors' devices.

Run with `LIBVA_DRIVER_NAME=v4l2_request`, the recorded build's `src` directory
in `LIBVA_DRIVERS_PATH`, and `V4L2R_SOURCE_COMMIT` set to the compiled source SHA.
The local private root is named `runtime36`; keep input bytes/manifest, software
results, identities, guard JSONL, each worker log/result and accepted/complete
records there. Every output root is fresh and single-use. The guard command is:

```text
python3 tests/hwguard.py --identity avd --deadline 6600 --log <root>/guard.jsonl --
  python3 tests/concurrent-va-run.py <build>/tests/concurrent-va-worker
    --manifest <root>/inputs/manifest.json --output <root>/hardware
    --mode vaapi --repetitions 10 --deadline 30
```

Stop on the first failed group/output mismatch, kernel fault, wedge, foreign
holder or guard error. Preserve partial logs and final state. Do not retry a
failed campaign, reset the decoder or shorten the journal window. A failed run
becomes an offline investigation before any separately reviewed next plan.

Acceptance requires all 180 groups, exact per-stream count/digest/retained-frame
checks, guard success, healthy idle final state and unchanged loaded/installed
module identity. Verify the recorded outputs independently against the sealed
manifest and count every group; a `complete.json` string alone is insufficient.
Publish normalized evidence with hashes and precise limitations.

This matrix exercises actual shared-device contexts and multiple clients, but
its victim teardown pauses other live contexts' API calls until teardown
completes. It does not prove real in-driver overlap, exported-DMABUF/independent
VAImage lifetime, injected VA driver recovery, strict conformance changes,
browser/display qualification or full release readiness. Existing offline
criteria and any remaining hardware criteria must be reconciled individually;
#36 is not automatically closed by a successful selected matrix.
