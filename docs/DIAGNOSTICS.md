# Driver diagnostics

The driver writes diagnostics to standard error. Each message belongs to a
stable category, so a report can separate an unsupported stream from a client
bug, an out-of-memory condition or a kernel/decoder failure without depending
on the message wording.

## Output modes

| `LIBVA_V4L2_DIAG` | Output |
|---|---|
| unset or `text` | Historic lines, `libva-v4l2request: <message>`. Error, warning and info records only. |
| `json` | One JSON object per line with the fields below, including debug records and a driver capability summary. |

Any other value selects `text` and prints one warning without echoing the
value. `LIBVA_V4L2_TRACE` is separate and unchanged: it enables verbose
per-buffer lifecycle tracing and is off by default.

Selecting a mode never changes VA return codes or decoded output; the offline
`diag-categories` test runs every failure scenario in both modes and compares
the status returned to the client.

## Categories

The `category` names are part of the schema. New categories may be added;
existing names are not renamed or reused.

| Category | Meaning | Typical client status |
|---|---|---|
| `info` | Lifecycle and capability decisions (decoder chosen, CAPTURE format, converter). Not a failure. | — |
| `unsupported` | The profile, entrypoint, RT format or stream format cannot be driven by any detected decoder or converter. | `VA_STATUS_ERROR_UNSUPPORTED_*`, `OPERATION_FAILED` at context creation |
| `client` | VA calls in the wrong order, invalid IDs or parameters, or a surface layout the client fixed by exporting it first. | `INVALID_*`, `OPERATION_FAILED`, `SURFACE_BUSY` |
| `bitstream` | Malformed, contradictory or oversized coded data. | `INVALID_BUFFER` |
| `reference` | A picture needs a reference that is missing, belongs to another context, was detached or failed to decode. | `INVALID_BUFFER`, `INVALID_SURFACE` |
| `allocation` | Memory, dma-buf, request or V4L2 buffer allocation failed or reached a limit. | `ALLOCATION_FAILED` |
| `timeout` | A wait for the decoder, a request, the converter or a GPU reader of an exported buffer expired (2 s). | `OPERATION_FAILED` |
| `kernel` | A V4L2 or media ioctl was rejected. | `OPERATION_FAILED` |
| `decoder` | The decoder completed a frame but flagged it as failed (`V4L2_BUF_FLAG_ERROR`). | `DECODING_ERROR` |
| `device` | The device node reported an error, hang-up or invalid descriptor. | `OPERATION_FAILED` |

A `timeout` or `device` record means the decoder did not respond. Check the
kernel log before retrying, and do not repeatedly reopen a wedged decoder
(see `tests/README.md`).

## JSON records

Schema identifier: `libva-v4l2request.diag/1`. Validated by
`tests/diag-schema.py` against `tests/fixtures/diagnostics/schema.json`.

| Field | Always | Meaning |
|---|---|---|
| `schema` | yes | `libva-v4l2request.diag/1` |
| `run` | yes | 16 hex digits, random per process. Separates interleaved logs from several processes. |
| `seq` | yes | Record number within the run, increasing from 1. |
| `t_ms` | yes | Milliseconds since diagnostics started in this process (monotonic clock). |
| `level` | yes | `error`, `warning`, `info` or `debug`. |
| `category` | yes | See above. |
| `op` | no | Short stable name of the operation, e.g. `request-wait`, `capture-alloc`, `set-controls`. |
| `errno` | no | Symbolic errno of the failed call, e.g. `ETIMEDOUT`; `errno-<n>` for unlisted values. |
| `ctx` | no | Process-unique decoder context serial. VA context IDs are reused by later contexts and repeat across VA displays; `ctx` never is, so use it to group one decode session's records. |
| `va_context` | no | The VA context ID the client used, hexadecimal. |
| `codec`, `profile` | no | Codec backend and VA profile of the context. |
| `suppressed` | no | On `op: suppressed` records, the number of dropped records. |
| `msg` | yes | Human-readable text. Not stable; do not parse it. |

The `op: driver-init`, `level: debug` record also carries the driver `version`,
the libva `va_api` version it was built against, the `h264_high10` mode and a
`decoders` array with each decoder's `card`, accepted coded `formats` and 10-bit
capability decisions.

Example:

```json
{"schema":"libva-v4l2request.diag/1","run":"2789fe06fd81220f","seq":7,"t_ms":2014,"level":"error","category":"timeout","op":"request-wait","errno":"ETIMEDOUT","ctx":3,"va_context":"0x02000000","codec":"h264","profile":"VAProfileH264High","msg":"failed waiting on request 41"}
```

## Privacy and size limits

- No record contains bitstream or pixel bytes. Messages include only sizes,
  indices, formats, dimensions, VA IDs and device node names.
- Before output, absolute paths outside `/dev/` and `/sys/` (or containing
  `/..`) become `<path>`, and URLs (`scheme://…`, `file:…`) become `<url>`.
  This also applies to a device path set with `LIBVA_V4L2_REQUEST_MEDIA_PATH`
  outside `/dev/`. Control bytes become `?`; JSON mode also masks bytes above
  ASCII so records stay valid UTF-8, while text mode keeps them for localized
  error strings. This is a safeguard; no call site passes file names or
  process arguments.
- `msg` is at most 256 bytes (longer text ends in `...`), and a record is at
  most 1024 bytes (the `driver-init` summary is at most 4096).
- Each category allows 20 error or warning records, and separately 20 debug
  records, per 10 seconds. Further records are counted and reported as a single
  `suppressed` record when that category next logs after the window, or when
  the driver terminates. `info` records mark lifecycle events (a few per
  context, such as `decoding h264 via ...`) and are never limited, so scripts
  that look for them keep working.
- Debug records are never written in `text` mode.

Measured on the offline test host (Ubuntu 24.04 aarch64, GCC 13.3, `meson
setup` default `debugoptimized`, output to `/dev/null`): about 0.5 µs per
written JSON record, and about 6 ns for a rate-limited record or a debug record
in text mode. Run `build/tests/diagnostics overhead` to measure a build.

## Related opt-in traces

`LIBVA_V4L2_HEVC_REFTRACE` writes a separate per-request record of the HEVC
reference controls as submitted (schema `libva-v4l2request.hevc-refs/1`,
sharing this run identifier); see [HEVC_REFTRACE.md](HEVC_REFTRACE.md). It is
off by default and does not change these diagnostics.

## Reporting a problem

Reproduce with `LIBVA_V4L2_DIAG=json`, keep the records for the failing `ctx`,
and include them with the driver version from the `driver-init` record. Review
the log before posting it: client applications print their own messages to
the same stream, and those are not redacted by the driver.


## M1 regression evidence

The [16 September validation record](diagnostics-validation-2026-09-16.json)
identifies the tested driver, guard window, full-suite denominators, exact r11
pass sets and generated normal/early-export comparisons with JSON diagnostics.
It retains the known failures: in particular, FM1_FT_E refuses a software frame
with zero accepted output on both the preceding and diagnostic builds. The
strict full AVC comparison therefore remains non-green even though no prior
hardware pass is lost. This is regression evidence, not expanded support or
boot/display qualification.
