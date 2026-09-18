# M1 real-client concurrency evidence — 2026-09-18

**Result: all 180 guarded groups passed**, covering 2,560 decoded frames checked
against independent software pixels and 200 retained-frame cases with 400 extra
before/after-teardown readbacks. The original kernel module remained loaded;
there were no faults, timeouts, wedges, foreign clients or abandoned holders.
The decoder ended healthy and idle. No installation, module operation, shipped
patch edit, reboot or codec-support promotion occurred.

AI-assisted maintainer execution, verification and self-review; no independent
reviewer is claimed. This follows z23's real worker in PR96/#94 and preserves
grudev's earlier model, forced in-driver overlap and TSan contributions.

## What ran

[The pre-execution plan](PLAN.md) was committed as
`44cf9a3116d6a2d04575cf7361156e12b0f1a5b8` before the device run. Compiled source:
`c77e7b566f7baf9c7a2aad797e62c9aa578d9687`. [Identities](identities.json) record
helper/worker/driver hashes, toolchain, kernel, boot ID and the original module's
installed SHA-256 and matching loaded GNU build-ID note. This matching note and
recorded restoration provenance identify the loaded original; the pathname alone
would not establish that.

Fresh public inputs comprise eight frames each of H.264, HEVC, VP9 8-bit and VP9
10-bit. [Inputs](inputs.json) preserve exact media/reference identities, dimensions
and recipes. Independent FFmpeg-CLI packed raw pixels supply the expected hashes;
the client never generates its own oracle. Ten software repetitions passed on the
same built worker and inputs before hardware execution.

| Measured execution | Groups | Child processes | Decoded frames | Retained frames | Extra retained readbacks |
| --- | ---: | ---: | ---: | ---: | ---: |
| Software preparation | 180 | 300 | 2,560 | 200 | 400 |
| Guarded VAAPI / M1 | 180 | 300 | 2,560 | 200 | 400 |

Each mode runs ten repetitions of 1/2/4 shared-device threads and 1/2/4 independent
processes, for normal output, midpoint teardown and live-client EOF rejection.
Seeds, per-process IDs, counts, full/prefix stream digests, every individual frame
hash and held frame hashes match. Codec assignments rotate by repetition.
The hardware logs identify actual AVD contexts; hardware mode rejects software
frames. Victim frame readback remains valid after its decoder is destroyed;
other live contexts subsequently complete their outputs.

Guard `2258b641-2057-4f0c-ae5c-6de52b1b51c4` ran from **00:37:49 to 00:38:03 UTC**,
with a 6,600-second outer cap and 30-second group deadlines. It reported success,
exit 0 and final idle state. Separate whole-boot preflight/final snapshots show
no faults, holders or stuck tasks. Final installed module hash and loaded note
match the pre-run original, with the same boot ID. All children and the lease
are released. This was a short finite correctness campaign, not a soak or a
throughput benchmark; the recorded interval is not a decode-speed measurement.

## Reproduce the evidence check without hardware

```sh
python3 docs/concurrency-2026-09-18/verify.py --self-test
```

The standalone verifier uses Python's standard library and does not import the
worker runner. It validates the archive and all 980 members, recomputes reference
hashes directly from the archived raw pixels, then checks every software and
hardware log/result, exact group inventory, schedule, stream/held-frame identity,
binary/source/lease identity and preflight/final health. A complete-summary flag
alone cannot pass. Sixteen semantic mutations cover missing groups, incorrect
counts/digests/stream/frame/seed/retention, duplicate/missing worker records,
changed reference pixels, forged completion/driver/source/lease and bad final
module/holder/guard state. Mutations update both copies of a worker log so a mere
text-copy mismatch cannot masquerade as semantic validation.

[evidence.tar.xz](evidence.tar.xz) contains normalized worker logs/results, guard,
health and identity records plus the public generated media/raw references.
[archive-index.json](archive-index.json) binds every member's bytes and the
archive hash. Local integration paths are aliased. Its input manifest is the
pre-run normalized public manifest; the exact original manifest hash remains in
the plan and execution identity, since path aliasing changes serialized bytes.
Raw originals remain in the private execution root. This is recorded maintainer
provenance, not independently attested hardware output.

## Acceptance scope and remaining limits

This supplies #36's ten-repetition real-client output and kernel/holder health
evidence. Its earlier offline overlap, survivor-image, invalid-VA-client recovery
and TSan evidence stays attributed to the earlier contributions. The current
hardware teardown pauses other live contexts' API calls until victim teardown
completes; the client failure is FFmpeg's documented EOF rejection. These are
not measurements of simultaneous in-driver destruction, an injected VA driver
failure, or independently held VAImage/exported-DMABUF lifetime on hardware.

The selected finite campaign does not qualify all possible schedules, browsers,
displays, strict codec suites, boot stability, throughput or new platforms.
HEVC RPS_E corruption remains in #42; this generated corpus does not exercise
that conformance vector or establish a fix. Previously published strict pass sets are HEVC144/147, AVC73/135 and VP9216/305;
this campaign did not rerun those suites, and adds no support-count claim. Reconcile
#36's original criteria with these declared limits before closing the ticket;
related codec, client and release parents remain open.

Final local integration validation: fresh GCC ASan/UBSan Meson suite **196 passed,
zero failed**, including the new archive/semantic verifier and existing TSan.
Software frame-check and shared-contexts pass. The latter are separate from the
archived hardware execution and do not reopen the decoder.
