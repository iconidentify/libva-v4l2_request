# Development roadmap

The GitHub roadmap is the live source of ownership, blockers and progress: [iconidentify/libva-v4l2_request#7](https://github.com/iconidentify/libva-v4l2_request/issues/7).
The initial plan contains **54 leaf tickets in eight workstreams**, plus the roadmap and
shared workflow issues, across both repositories. This document is an index, not a second
status database. Check each ticket's current labels, comments and native dependencies.

## Next contributor wave

Use [the contributor entry point](CONTRIBUTOR_START.md) for bounded offline tasks,
public inputs and the live ready queues. It routes remaining real H.264 parser/admission work and a coherent HEVC
reference-content client adapter. The reference traces, VP9 model, field-feasibility
research and reference-memory audit are completed inputs. Allocation retry/startup
has an isolated candidate; capture-DMA coherence and runtime qualification remain
explicit gates. Check live claims before starting.
The current GitHub roadmap records the accepted resource/qualification evidence,
active claims and remaining hardware gates; historical r11 figures below are a
starting snapshot. Research acceptance never promises a new hardware capability.

## Agent entry point

Read [the claim and execution workflow](AGENT_WORKFLOW.md) before editing. Its GitHub copy
is [iconidentify/libva-v4l2_request#8](https://github.com/iconidentify/libva-v4l2_request/issues/8). Claim one ready leaf with a unique session ID, use an isolated worktree,
and coordinate shared files and exclusive hardware access. GitHub assignment alone does not
identify an agent when sessions share an account.

## Product target

Qualify a dependable Linux VA-API decoder across explicit codec/profile, bit-depth/chroma,
backend/device and client combinations. Preserve all current passing vectors; require
independent reference matches for every mandatory vector in a claimed supported row.
Keep full-suite denominators, unsupported formats, software fallback and untested hardware
visible. Compiled-in codecs, correct decoder pixels and a successful boot are different
kinds of evidence.

Historical M1 r11 starting snapshot: 35 sanitizer cases; 72,000 generated parser inputs within three
cases; HEVC 144/147, AVC 73/135, opt-in High 10 FRExt 27/69 and VP9 216/305; 864 additional
generated hardware frame comparisons. These results use the companion kernel patchset.
[Complete record](https://github.com/iconidentify/omarchy-m1-video/blob/main/docs/codec-validation-r11-2026-09-15.json).

## Delivery stages

| Stage | Exit direction |
|---|---|
| M0 - Reproducible foundation | Make support claims, test inputs, results and agent coordination reproducible. Exit: baseline schema/corpus/runner and build gates are usable from a fresh checkout. No calendar commitment. |
| M1 - Correctness and isolation | Eliminate reproducible corruption, memory-safety and lifecycle failures in declared supported paths. Preserve the exact r11 pass sets. Exit requires evidence for each closed defect. |
| M2 - Broader playback compatibility | Qualify additional formats, clients and devices against the support matrix. Hardware-dependent rows stay experimental until their acceptance evidence exists. |
| M3 - Production release readiness | Prove stress behavior, measured performance, maintainability, packaging and recovery. A stable claim applies only to qualified platform rows with no open release-blocking faults. |
| M4 - Hardware-dependent expansion | Discovery and implementation of kernel/firmware-dependent or presently unverified codec features. This is a gated research backlog, not a promise that all devices can decode every format. |

There are no invented deadlines. Hardware availability and explicit kernel/boot authorization
are real dependencies. Research tickets deliver reviewed decisions and implementation
contracts; closing them does not mean the feature is supported. The production qualification
[iconidentify/omarchy-m1-video#27](https://github.com/iconidentify/omarchy-m1-video/issues/27) distinguishes userspace qualification from boot-enabled distribution readiness.
An unresolved reset/corruption defect prevents a stable claim for the affected configuration.

## First wave: independently claimable scopes

These seven tickets were ready when the roadmap was created. Check their live status before claiming.

| Ticket | Initial owner skill | Work |
|---|---|---|
| [iconidentify/libva-v4l2_request#15](https://github.com/iconidentify/libva-v4l2_request/issues/15) | Product/support contract | Define codec, device and client support tiers with explicit release gates |
| [iconidentify/libva-v4l2_request#16](https://github.com/iconidentify/libva-v4l2_request/issues/16) | Corpus/tooling | Publish a licensed, checksum-pinned codec regression corpus manifest |
| [iconidentify/libva-v4l2_request#17](https://github.com/iconidentify/libva-v4l2_request/issues/17) | Hardware harness | Ship a portable hardware guard with an exclusive device lease and durable logs |
| [iconidentify/libva-v4l2_request#18](https://github.com/iconidentify/libva-v4l2_request/issues/18) | Build/CI | Expand build and sanitizer CI across supported compilers, architectures and API versions |
| [iconidentify/libva-v4l2_request#19](https://github.com/iconidentify/libva-v4l2_request/issues/19) | Core diagnostics | Add actionable decoder diagnostics without leaking media or user data |
| [iconidentify/omarchy-m1-video#9](https://github.com/iconidentify/omarchy-m1-video/issues/9) | Installer reliability | Test installer upgrades, failure recovery and uninstall in disposable environments |
| [iconidentify/libva-v4l2_request#20](https://github.com/iconidentify/libva-v4l2_request/issues/20) | Maintenance/docs | Establish maintenance, contribution and security reporting procedures |

## Workstreams and tickets

### Establish a reproducible support contract and test infrastructure

Parent: [iconidentify/libva-v4l2_request#9](https://github.com/iconidentify/libva-v4l2_request/issues/9). Every advertised support claim has a device/client/version boundary and reproducible evidence. Fresh-checkout agents can run offline checks, obtain legal test data and compare results without private lab state.

- [iconidentify/libva-v4l2_request#15](https://github.com/iconidentify/libva-v4l2_request/issues/15) — Define codec, device and client support tiers with explicit release gates (P0, M0). Contract: [SUPPORT.md](SUPPORT.md).
- [iconidentify/libva-v4l2_request#16](https://github.com/iconidentify/libva-v4l2_request/issues/16) — Publish a licensed, checksum-pinned codec regression corpus manifest (P1, M0).
- [iconidentify/libva-v4l2_request#21](https://github.com/iconidentify/libva-v4l2_request/issues/21) — Make conformance results schema-validated and compare exact passing vectors (P0, M0). Shipped: `python3 tests/compare-results.py` ([PR #52](https://github.com/iconidentify/libva-v4l2_request/pull/52)).
- [iconidentify/libva-v4l2_request#17](https://github.com/iconidentify/libva-v4l2_request/issues/17) — Ship a portable hardware guard with an exclusive device lease and durable logs (P0, M0). Guard: `python3 tests/hwguard.py`.
- [iconidentify/libva-v4l2_request#18](https://github.com/iconidentify/libva-v4l2_request/issues/18) — Expand build and sanitizer CI across supported compilers, architectures and API versions (P1, M0).
- [iconidentify/libva-v4l2_request#22](https://github.com/iconidentify/libva-v4l2_request/issues/22) — Add coverage-guided parser and VA-API sequence fuzzing with replayable failures (P1, M1).

### Prove lifecycle safety, fault recovery and predictable resource use

Parent: [iconidentify/libva-v4l2_request#10](https://github.com/iconidentify/libva-v4l2_request/issues/10). Malformed input, API failures and multiple clients must not corrupt unrelated pictures, leak resources without bound, or silently succeed. Measure performance before changing synchronization.

- [iconidentify/libva-v4l2_request#23](https://github.com/iconidentify/libva-v4l2_request/issues/23) — Audit public VA-API state transitions and reject invalid lifetimes consistently (P0, M1).
- [iconidentify/libva-v4l2_request#35](https://github.com/iconidentify/libva-v4l2_request/issues/35) — Make request and buffer cleanup correct at every injected failure point (P0, M1).
- [iconidentify/libva-v4l2_request#36](https://github.com/iconidentify/libva-v4l2_request/issues/36) — Stress concurrent API calls, context teardown and frame access (P1, M1).
- [iconidentify/libva-v4l2_request#41](https://github.com/iconidentify/libva-v4l2_request/issues/41) — Prove bounded memory, dma-buf and file-descriptor use under playback churn (P1, M3).
- [iconidentify/libva-v4l2_request#19](https://github.com/iconidentify/libva-v4l2_request/issues/19) — Add actionable decoder diagnostics without leaking media or user data (P1, M1).
- [iconidentify/libva-v4l2_request#24](https://github.com/iconidentify/libva-v4l2_request/issues/24) — Establish reproducible throughput, latency, CPU and memory benchmarks (P1, M3).
- [iconidentify/libva-v4l2_request#47](https://github.com/iconidentify/libva-v4l2_request/issues/47) — Optimize measured synchronization or copy bottlenecks while preserving ownership (P2, M3).

### Expand and qualify H.264 profiles and coding features

Parent: [iconidentify/libva-v4l2_request#11](https://github.com/iconidentify/libva-v4l2_request/issues/11). Progressively qualify High 10, chroma formats and profile handling while exposing kernel-dependent interlacing and advanced syntax as explicit work. Do not advertise unsupported profiles to force client fallback.

- [iconidentify/libva-v4l2_request#37](https://github.com/iconidentify/libva-v4l2_request/issues/37) — Qualify progressive Baseline and Extended streams without blanket profile overrides (P1, M2).
- [iconidentify/libva-v4l2_request#25](https://github.com/iconidentify/libva-v4l2_request/issues/25) — Qualify H.264 High 10 across FFmpeg versions and native VA clients (P1, M2).
- [iconidentify/libva-v4l2_request#26](https://github.com/iconidentify/libva-v4l2_request/issues/26) — Qualify H.264 monochrome and 4:2:2 paths through VA-API (P2, M2).
- [iconidentify/omarchy-m1-video#10](https://github.com/iconidentify/omarchy-m1-video/issues/10) — Design and establish feasibility of H.264 field and MBAFF decoding on AVD (P2, M4).
- [iconidentify/omarchy-m1-video#14](https://github.com/iconidentify/omarchy-m1-video/issues/14) — Implement and qualify AVD interlaced H.264 from the approved field-decoding design (P2, M4).
- [iconidentify/libva-v4l2_request#27](https://github.com/iconidentify/libva-v4l2_request/issues/27) — Decide and implement the supported boundary for FMO, ASO and partitioned H.264 (P2, M4).

### Resolve remaining HEVC correctness and client parser gaps

Parent: [iconidentify/libva-v4l2_request#12](https://github.com/iconidentify/libva-v4l2_request/issues/12). Explain and fix the remaining reference corruption and intermittent parallel mismatches; qualify parameter-set changes and format boundaries without per-vector hacks.

- [iconidentify/libva-v4l2_request#38](https://github.com/iconidentify/libva-v4l2_request/issues/38) — Isolate the HEVC RPS_E reference corruption with frame and command evidence (P0, M1). Record: [RPS_E.md](RPS_E.md).
- [iconidentify/libva-v4l2_request#42](https://github.com/iconidentify/libva-v4l2_request/issues/42) — Correct HEVC long-term reference handling for RPS_E without regressions (P0, M1).
- [iconidentify/libva-v4l2_request#39](https://github.com/iconidentify/libva-v4l2_request/issues/39) — Make intermittent multi-process HEVC corruption reproducible (P0, M1). Record: [HEVC_CONCURRENCY.md](HEVC_CONCURRENCY.md).
- [iconidentify/libva-v4l2_request#43](https://github.com/iconidentify/libva-v4l2_request/issues/43) — Fix the isolated HEVC concurrency defect and lock in its regression (P0, M1).
- [iconidentify/omarchy-m1-video#15](https://github.com/iconidentify/omarchy-m1-video/issues/15) — Preserve delayed HEVC parameter sets and all pictures in the FFmpeg client path (P1, M2).
- [iconidentify/libva-v4l2_request#28](https://github.com/iconidentify/libva-v4l2_request/issues/28) — Specify and enforce HEVC range-extension and bit-depth capability boundaries (P2, M4).

### Complete VP9 state, resizing and format coverage

Parent: [iconidentify/libva-v4l2_request#13](https://github.com/iconidentify/libva-v4l2_request/issues/13). Preserve the 216/305 baseline passes and extend correct decoding across reference/size changes where the backend permits. Rejecting an unsupported stream is not a new conformance pass.

- [iconidentify/omarchy-m1-video#11](https://github.com/iconidentify/omarchy-m1-video/issues/11) — Design preservation of AVD VP9 reference metadata across size changes (P1, M4).
- [iconidentify/omarchy-m1-video#16](https://github.com/iconidentify/omarchy-m1-video/issues/16) — Implement the approved AVD VP9 state-preserving resize contract (P1, M4).
- [iconidentify/libva-v4l2_request#44](https://github.com/iconidentify/libva-v4l2_request/issues/44) — Decode VP9 inter-frame resize streams correctly through VA-API (P1, M4).
- [iconidentify/omarchy-m1-video#12](https://github.com/iconidentify/omarchy-m1-video/issues/12) — Resolve VP9 sub-64 dimension limits with a verified backend decision (P2, M4).
- [iconidentify/libva-v4l2_request#29](https://github.com/iconidentify/libva-v4l2_request/issues/29) — Qualify VP9 scalable streams, reference refresh and high-bit-depth boundaries (P2, M2).

### Qualify additional codec backends and dynamic playback

Parent: [iconidentify/libva-v4l2_request#14](https://github.com/iconidentify/libva-v4l2_request/issues/14). Validate VP8, MPEG-2, AV1 and generic pixel-format paths only on capable backends; distinguish compiled-in code from qualified hardware support.

- [iconidentify/libva-v4l2_request#30](https://github.com/iconidentify/libva-v4l2_request/issues/30) — Qualify the AV1 backend on hardware that actually exposes AV1 decode (P2, M2).
- [iconidentify/libva-v4l2_request#31](https://github.com/iconidentify/libva-v4l2_request/issues/31) — Add VP8 control, reference and hardware conformance coverage (P2, M2).
- [iconidentify/libva-v4l2_request#32](https://github.com/iconidentify/libva-v4l2_request/issues/32) — Qualify MPEG-2 picture ordering, fields and malformed-input handling (P2, M2).
- [iconidentify/libva-v4l2_request#40](https://github.com/iconidentify/libva-v4l2_request/issues/40) — Audit NV12, P010 and converter-backed surface contracts across backends (P1, M2).
- [iconidentify/libva-v4l2_request#45](https://github.com/iconidentify/libva-v4l2_request/issues/45) — Validate drain, seek, EOS and legal format changes across codecs (P1, M2).
- [iconidentify/libva-v4l2_request#33](https://github.com/iconidentify/libva-v4l2_request/issues/33) — Publish a feasibility map for remaining codecs and advanced profiles (P2, M4).

### Deliver correct playback through real clients and display paths

Parent: [iconidentify/omarchy-m1-video#7](https://github.com/iconidentify/omarchy-m1-video/issues/7). Connect decoder correctness to ordinary media playback, seek/adaptation, sandboxed browsers, colour and presentation. Container parsing and rendering dependencies remain assigned to their actual layers.

- [iconidentify/libva-v4l2_request#48](https://github.com/iconidentify/libva-v4l2_request/issues/48) — Build an end-to-end container and streaming playback matrix (P1, M2).
- [iconidentify/omarchy-m1-video#22](https://github.com/iconidentify/omarchy-m1-video/issues/22) — Qualify Chrome and Chromium hardware playback with normal sandboxing (P1, M2).
- [iconidentify/omarchy-m1-video#23](https://github.com/iconidentify/omarchy-m1-video/issues/23) — Validate Firefox hardware decoding inside its normal sandbox (P1, M2).
- [iconidentify/omarchy-m1-video#21](https://github.com/iconidentify/omarchy-m1-video/issues/21) — Qualify mpv playback, readback and OpenGL presentation for NV12 and P010 (P1, M2).
- [iconidentify/omarchy-m1-video#24](https://github.com/iconidentify/omarchy-m1-video/issues/24) — Validate and package a scoped Mesa Vulkan dma-buf plane-offset correction (P1, M2).
- [iconidentify/omarchy-m1-video#26](https://github.com/iconidentify/omarchy-m1-video/issues/26) — Fix Chrome full-range H.264 colour handling when the colour description is absent (P1, M2).
- [iconidentify/omarchy-m1-video#25](https://github.com/iconidentify/omarchy-m1-video/issues/25) — Define and qualify SDR/HDR metadata and 10-bit presentation boundaries (P2, M2).
- [iconidentify/libva-v4l2_request#49](https://github.com/iconidentify/libva-v4l2_request/issues/49) — Verify clean client fallback and recovery for unsupported or damaged video (P1, M2).

### Qualify platforms, boot behavior, packaging and releases

Parent: [iconidentify/omarchy-m1-video#8](https://github.com/iconidentify/omarchy-m1-video/issues/8). Earn production claims with platform evidence, reproducible packages, tested recovery and explicit release gates. An unexplained reset prevents a stable boot-enabled claim for the affected configuration.

- [iconidentify/omarchy-m1-video#13](https://github.com/iconidentify/omarchy-m1-video/issues/13) — Investigate unexplained boot resets with a consented, reproducible test matrix (P0, M1).
- [iconidentify/omarchy-m1-video#17](https://github.com/iconidentify/omarchy-m1-video/issues/17) — Correct the identified reset cause before qualifying boot-enabled installation (P0, M1).
- [iconidentify/omarchy-m1-video#20](https://github.com/iconidentify/omarchy-m1-video/issues/20) — Qualify suspend/resume and interrupted playback recovery (P1, M3).
- [iconidentify/omarchy-m1-video#18](https://github.com/iconidentify/omarchy-m1-video/issues/18) — Establish repeatable qualification for additional Apple Silicon machines (P1, M2).
- [iconidentify/libva-v4l2_request#46](https://github.com/iconidentify/libva-v4l2_request/issues/46) — Qualify a non-AVD V4L2 backend and isolate Apple-specific behavior (P1, M2).
- [iconidentify/libva-v4l2_request#34](https://github.com/iconidentify/libva-v4l2_request/issues/34) — Document and test portable builds, ABI compatibility and clean userspace installs (P1, M3).
- [iconidentify/omarchy-m1-video#9](https://github.com/iconidentify/omarchy-m1-video/issues/9) — Test installer upgrades, failure recovery and uninstall in disposable environments (P1, M3).
- [iconidentify/omarchy-m1-video#19](https://github.com/iconidentify/omarchy-m1-video/issues/19) — Automate reproducible package provenance and kernel compatibility checks (P1, M3).
- [iconidentify/libva-v4l2_request#20](https://github.com/iconidentify/libva-v4l2_request/issues/20) — Establish maintenance, contribution and security reporting procedures (P1, M3).
- [iconidentify/omarchy-m1-video#27](https://github.com/iconidentify/omarchy-m1-video/issues/27) — Publish a release qualification report with enforceable correctness and support gates (P0, M3).

## Updating the plan

Keep prerequisites as native GitHub blocked-by links and preserve parent/sub-issue relationships.
Document discoveries as linked children with concrete acceptance criteria; do not silently
widen a claim, delete a failing vector or close an implementation ticket after analysis alone.
The issue bodies contain starting points, implementation steps, acceptance checks, validation,
resource gates and handoff instructions. Final release claims require linked evidence.

All roadmap issues and this documentation were generated by AI at the owner's request.
Original upstream/contributor code and test authorship remain credited in the repositories.
