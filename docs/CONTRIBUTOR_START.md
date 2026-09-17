# Contribute to the next codec and AVD phase

Help make more videos decode correctly on Linux. The next work combines focused
client fixes with Apple Video Decoder (AVD) source analysis, reference-control
tracing and executable checks of proposed state transitions. We build on the
Asahi Linux AVD driver, [eiln's original reverse engineering](https://github.com/eiln/avd),
libva-v4l2_request and the contributors credited in both repository READMEs.
This is an unofficial project; keep reports and coordination in these forks.

## Start without Apple hardware

The offline tasks below can be completed without a decoder, privileged access,
installation or private lab data. Linux is needed for C/FFmpeg builds; source
analysis and standalone Python tools can use any suitable host. A synthetic
model is not hardware validation. New codec support still needs its separate
reviewed hardware evidence.

The [live roadmap](https://github.com/iconidentify/libva-v4l2_request/issues/7)
controls current status. These are entry points, not permanent reservations:

Updated after the 2026-09-17 review round. Start from the current default branch;
merged prerequisites are already there. Re-read live claims before choosing.

| Scope | Next concrete deliverable | State at this handoff |
| --- | --- | --- |
| [HEVC reference-content adapter #82](https://github.com/iconidentify/omarchy-m1-video/issues/82) | Connect actual client pause/drain/retention and verified coherent allocation identity; retain rejection where the APIs cannot prove them | Offline implementation available after the synthetic observer in [PR #83](https://github.com/iconidentify/omarchy-m1-video/pull/83); no real copy path yet |
| [H.264 actual admission #79](https://github.com/iconidentify/omarchy-m1-video/issues/79) | Exercise real Annex-B/AVCC parser-to-issue behavior, rejected suffixes and config/flush transitions before any profile remap | Offline implementation available after the partial guards in [PR #80](https://github.com/iconidentify/omarchy-m1-video/pull/80) |
| [HEVC parameter sets #44](https://github.com/iconidentify/omarchy-m1-video/issues/44) | Local FFmpeg parser fix and full-output software regressions | Claimed by SarthakU; coordinate, do not duplicate |
| [AVD allocation retry #81](https://github.com/iconidentify/omarchy-m1-video/issues/81) | Actual allocator/startup failure regressions and isolated candidate module build | [PR #84](https://github.com/iconidentify/omarchy-m1-video/pull/84) owns the candidate; later guarded runtime qualification remains separate |
| [Capture backing #52](https://github.com/iconidentify/omarchy-m1-video/issues/52) / [driver #90](https://github.com/iconidentify/libva-v4l2_request/issues/90) | Establish allocation/export/import/CPU coherence before changing cache policy, then qualify Chromium | Blocked on that reviewed contract; respect #90's existing contributor claim |

Completed research/tools are inputs, not new assignments: client-selection #41/#73,
HEVC control traces driver #84, VP9 state validator companion #42, field-feasibility
#43 and reference-memory audit #77. Their hardware/feature parents remain open.
Driver [#22](https://github.com/iconidentify/libva-v4l2_request/issues/22) fuzzing and
[#36](https://github.com/iconidentify/libva-v4l2_request/issues/36) concurrency retain
existing claims. The live queue can change after this dated snapshot.

Check the **open, ready** queues in the
[driver](https://github.com/iconidentify/libva-v4l2_request/issues?q=is%3Aissue%20is%3Aopen%20label%3Astatus%3Aready)
and [companion](https://github.com/iconidentify/omarchy-m1-video/issues?q=is%3Aissue%20is%3Aopen%20label%3Astatus%3Aready)
before choosing. Existing claims and PRs take precedence over this table. A ticket
marked blocked is not available for its whole implementation; claim its ready
offline child instead. No task here promises a specific increase in passing videos.

## Claim one bounded task

1. Read the ticket, all comments, native dependencies, repository `AGENTS.md`,
   [CONTRIBUTING.md](../CONTRIBUTING.md) and the
   [shared workflow](https://github.com/iconidentify/libva-v4l2_request/issues/8).
2. Fork the owning repository and branch from its current default: driver
   `avd-fixes`, companion `main`. Record the actual fetched base SHA. Keep changes
   in an isolated checkout/worktree.
3. Post a unique-session claim with files, a concrete plan and a lease of at most
   24 hours. Re-read comments/open PRs before editing; an assignment alone is not
   a claim. Coordinate shared test registration and common driver files.
4. Open a small draft PR early. Link the parent with `Refs`, keep the child's
   acceptance table current, and preserve original authorship and source/fixture
   provenance. People and coding agents follow the same review requirements.
5. Run meaningful positive and negative tests and required CI. Record what was
   not run. Close only the child after its own evidence and code are merged;
   parent hardware/feature gates remain open.

If you cannot edit labels, your claim comment is still useful: ask a maintainer
to reconcile the status. Do not create a duplicate ticket just to obtain an
assignment. Review routing goes through `iconidentify`; a named routing contact
is not a claim that independent technical review has already happened.

A useful starting prompt for a coding agent:

> Work on <full ready-child issue URL>. Read its complete discussion, native
> dependencies, AGENTS.md, CONTRIBUTING.md and the live shared workflow first.
> Confirm the ticket is unclaimed, post a unique-session claim, and use an isolated
> branch from the current default SHA. Implement only its bounded offline scope.
> Preserve existing behavior and other contributors' work. Add tests that detect
> the claimed failure, record exact results and limitations, and open a draft PR.
> Do not promote hardware support from a model or close the parent. Coordinate
> shared files and release or renew the claim at handoff. Follow the repository's
> separate authorization rules before any hardware or system action.

## What the next phase can establish

The selected M1 build retains **HEVC 144/147, AVC 73/135 and VP9 216/305** strict
hardware passes. Its resource acceptance includes an uninterrupted one-hour
soak and **882,336 exact frame comparisons across the accepted campaigns**.
[Published evidence](https://github.com/iconidentify/libva-v4l2_request/blob/b9803ed5290ecb4b48c09482cbc6e943aee08b63/docs/resource-churn-2026-09-17/README.md).
These are conformance/workload results, not everyday-video success percentages,
universal stability or counts of unique videos. The M2 Max contribution is
[limited reported smoke evidence](https://github.com/iconidentify/omarchy-m1-video/blob/f50a4a4c88b03ea1e10acf734a53c3a0ee9b2519/docs/evidence/issue18/t6021-review.md),
not full multi-device qualification.

Paired HEVC captures now show matching selected commands and controls while RPS_E
still produces wrong pixels. The next discriminating observation is coherent
reference content with proven writer identity and quiescence; equal commands do not
prove firmware guilt. The source-proven allocation retry/startup defects have an
isolated candidate, but accepted RPS_E runs had no allocation errors, so that candidate
is not an established corruption fix. VP9 resizing still needs its validated
state-preservation contract. H.264 interlacing is a feasibility
question: the original reverse engineer reports hardware limitations. Do not
promise to recover all 49 failing Main-profile vectors or assume a missing
kernel implementation proves either firmware capability or impossibility.

Device owners can coordinate qualification through the owning tickets. An issue
claim is not a decoder lease. Kernel instrumentation, module operations,
installation, suspend and boot testing retain their explicit authorization and
recovery gates. Hardware evidence must retain the exact source/build identity,
corpus lock, command/environment, guard run ID and fixed journal boundary. Keep
original failures and unknowns; never reconstruct historical records as measurements.

AI-assisted contribution planning; original source and contributor credits remain intact.
