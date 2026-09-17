#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline differential checker for HEVC reference-control traces (issue #84).

Compares two trace files written by the driver with LIBVA_V4L2_HEVC_REFTRACE
(schema ``libva-v4l2request.hevc-refs/1``) picture by picture, in decode
order, and reports the first attributable difference in the submitted
reference controls. It never guesses: malformed, truncated, incompatible or
ambiguous inputs are rejected with exit status 2 rather than compared.

    hevc-reftrace-check.py validate FILE
    hevc-reftrace-check.py compare A B [--context CTX_A CTX_B] [--expected-pictures N] [--json]
    hevc-reftrace-check.py --self-test [hevc-reftrace test binary]
    hevc-reftrace-check.py --write-fixtures

Exit status: 0 equal (or valid), 1 a difference was found, 2 rejected input.

DPB entries are correlated by the picture that wrote the referenced CAPTURE
buffer (the earlier record whose ``target`` is that ``buf``), not by the raw
buffer index, so two clients with different buffer allocation can still be
compared. Fields this version does not know are preserved and compared last.
"""
import copy
import json
import pathlib
import re
import subprocess
import sys
import tempfile

SCHEMA = "libva-v4l2request.hevc-refs/1"
ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "hevc-reftrace"
RUN_RE = re.compile(r"[0-9a-f]{16}")
MAX_RECORD_BYTES = 4096
MAX_TRACE_BYTES = 64 * 1024 * 1024

TOP_INT = ("seq", "ctx", "pic", "req", "first", "last", "target", "poc", "irap",
           "idr", "ltr_sps", "reorder", "total_curr")
TOP_LIST = ("st_before", "st_after", "lt_curr")
DPB_INT = ("i", "buf", "poc", "lt", "field")
SLICE_INT = ("i", "nal", "tmvp")
KNOWN_TOP = set(TOP_INT) | set(TOP_LIST) | {"schema", "run", "va_context", "dpb",
                                            "slices", "slices_omitted", "error"}
KNOWN_SLICE = set(SLICE_INT) | {"type", "l0", "l1", "col_l0", "col"}


class Reject(Exception):
    """Input that must not be compared."""


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def int_list(value):
    return isinstance(value, list) and all(is_int(v) for v in value)


# --- loading and validation --------------------------------------------------

def load(path):
    """Parse one trace file; reject anything the comparison cannot trust."""
    path = pathlib.Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRACE_BYTES + 1)
        if len(raw) > MAX_TRACE_BYTES:
            raise Reject(f"{path}: trace exceeds {MAX_TRACE_BYTES} bytes")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Reject(f"{path}: cannot read: {error}")
    if not text:
        raise Reject(f"{path}: empty file")
    if not text.endswith("\n"):
        raise Reject(f"{path}: truncated: last record has no newline")
    records = []
    run = None
    for number, line in enumerate(text.splitlines(), 1):
        if len(line.encode("utf-8")) + 1 > MAX_RECORD_BYTES:
            raise Reject(f"{path}:{number}: record exceeds byte bound")
        try:
            record = json.loads(line, object_pairs_hook=unique_object,
                                parse_constant=reject_constant)
        except (json.JSONDecodeError, RecursionError) as error:
            raise Reject(f"{path}:{number}: truncated or malformed record ({error})")
        if not isinstance(record, dict):
            raise Reject(f"{path}:{number}: record is not an object")
        if record.get("schema") != SCHEMA:
            raise Reject(f"{path}:{number}: incompatible schema {record.get('schema')!r}, "
                         f"expected {SCHEMA!r}")
        if not isinstance(record.get("run"), str) or not RUN_RE.fullmatch(record["run"]):
            raise Reject(f"{path}:{number}: missing run identity")
        if run is None:
            run = record["run"]
        elif record["run"] != run:
            raise Reject(f"{path}:{number}: records from two runs ({run}, {record['run']})")
        if not is_int(record.get("seq")):
            raise Reject(f"{path}:{number}: missing seq")
        if record["seq"] != number:
            raise Reject(f"{path}:{number}: seq {record['seq']} breaks the sequence "
                         f"(expected {number}): truncated or reordered capture")
        if "_line" in record or "src" in record:
            raise Reject(f"{path}:{number}: reserved checker field")
        if "error" in record:
            raise Reject(f"{path}:{number}: unavailable record content: {record['error']}")
        check_full_record(path, number, record)
        record["_line"] = number
        records.append(record)
    return records


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Reject(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def reject_constant(value):
    raise Reject(f"non-JSON numeric constant {value}")


def check_full_record(path, number, record):
    where = f"{path}:{number}"

    def bounded(obj, key, low, high):
        value = obj.get(key)
        if not is_int(value) or not low <= value <= high:
            raise Reject(f"{where}: invalid {key!r}: expected integer {low}..{high}")

    for key in TOP_INT:
        if not is_int(record.get(key)):
            raise Reject(f"{where}: missing or non-integer {key!r}")
    for key in ("seq", "ctx", "pic", "req"):
        bounded(record, key, 1, (1 << (64 if key == "seq" else 32)) - 1)
    bounded(record, "target", 0, (1 << 32) - 1)
    bounded(record, "poc", -(1 << 31), (1 << 31) - 1)
    bounded(record, "total_curr", 0, 16)
    for key in ("first", "last", "irap", "idr", "ltr_sps", "reorder"):
        bounded(record, key, 0, 1)
    if not isinstance(record.get("va_context"), str):
        raise Reject(f"{where}: missing va_context")
    dpb = record.get("dpb")
    if not isinstance(dpb, list) or len(dpb) > 16:
        raise Reject(f"{where}: missing or oversized dpb")
    for entry in dpb:
        if not isinstance(entry, dict) or not all(is_int(entry.get(k)) for k in DPB_INT):
            raise Reject(f"{where}: malformed dpb entry")
        if "src" in entry or "_line" in entry:
            raise Reject(f"{where}: reserved checker field")
        bounded(entry, "buf", -1, (1 << 32) - 1)
        bounded(entry, "poc", -(1 << 31), (1 << 31) - 1)
        for key in ("lt", "field"):
            bounded(entry, key, 0, 1)
    if [e["i"] for e in dpb] != list(range(len(dpb))):
        raise Reject(f"{where}: dpb slots are not 0..n-1 in order")

    def refs(value):
        if (not int_list(value) or len(value) > 16 or
                any(i < 0 or i >= len(dpb) for i in value)):
            raise Reject(f"{where}: malformed or out-of-range reference list")

    for key in TOP_LIST:
        refs(record.get(key))
    slices = record.get("slices")
    if not isinstance(slices, list) or len(slices) > 16:
        raise Reject(f"{where}: missing or oversized slices")
    for item in slices:
        if (not isinstance(item, dict) or not all(is_int(item.get(k)) for k in SLICE_INT)
                or item.get("type") not in ("B", "P", "I")):
            raise Reject(f"{where}: malformed slice")
        bounded(item, "nal", 0, 63)
        bounded(item, "tmvp", 0, 1)
        refs(item.get("l0")); refs(item.get("l1"))
        if ((item["type"] == "I" and (item["l0"] or item["l1"])) or
                (item["type"] == "P" and item["l1"])):
            raise Reject(f"{where}: reference list disagrees with slice type")
        if item["tmvp"]:
            bounded(item, "col_l0", 0, 1)
            # I-slice headers can retain temporal MVP enablement, but never
            # select a collocated picture. Preserve their raw u8 metadata.
            if item["type"] == "I":
                bounded(item, "col", 0, 255)
            else:
                collocated = item["l0"] if item["type"] == "P" or item["col_l0"] else item["l1"]
                bounded(item, "col", 0, len(collocated) - 1)
        elif "col" in item or "col_l0" in item:
            raise Reject(f"{where}: inactive collocated reference")
    if [item["i"] for item in slices] != list(range(len(slices))):
        raise Reject(f"{where}: slice indices are not 0..n-1 in order")
    if "slices_omitted" in record:
        bounded(record, "slices_omitted", 0, (1 << 32) - 1)
        if record["slices_omitted"]:
            raise Reject(f"{where}: omitted slices make comparison incomplete")


# --- correlation -------------------------------------------------------------

def select_context(records, path, wanted):
    contexts = sorted({r["ctx"] for r in records})
    if wanted is None:
        if len(contexts) != 1:
            raise Reject(f"{path}: ambiguous frame correlation: {len(contexts)} decode "
                         f"contexts {contexts}; pass --context to choose one per file")
        wanted = contexts[0]
    elif wanted not in contexts:
        raise Reject(f"{path}: context {wanted} not present (have {contexts})")
    return wanted, [r for r in records if r["ctx"] == wanted]


def pictures_of(records, path):
    """Group a context's records into pictures in decode order, resolving each
    DPB slot to the picture that wrote its buffer."""
    pictures = []
    wrote = {}          # CAPTURE buffer index -> (pic ordinal, poc)
    last_pic, last_req = 0, 0
    closed = True
    for r in records:
        where = f"{path}:{r['_line']}"
        if r["pic"] < last_pic or r["req"] != last_req + 1:
            raise Reject(f"{where}: pic/req ordinals out of order")
        if r["pic"] > last_pic:
            if r["pic"] != last_pic + 1:
                raise Reject(f"{where}: picture ordinal gap ({last_pic} -> {r['pic']})")
            if not closed or not r["first"]:
                raise Reject(f"{where}: incomplete picture or missing first batch")
            pictures.append({"pic": r["pic"], "poc": r["poc"], "target": r["target"], "requests": []})
        elif closed or r["first"]:
            raise Reject(f"{where}: duplicate first batch or request after last batch")
        last_pic, last_req = r["pic"], r["req"]
        pic = pictures[-1]
        if pic["poc"] != r["poc"] or pic["target"] != r["target"]:
            raise Reject(f"{where}: picture {r['pic']} changes POC/target mid-picture")
        resolved = []
        for entry in r["dpb"]:
            src = wrote.get(entry["buf"])
            if src is None:
                raise Reject(f"{where}: unresolved reference buffer {entry['buf']}")
            resolved.append(dict(entry, src=list(src)))
        pic["requests"].append(dict(r, dpb=resolved))
        closed = bool(r["last"])
        if closed:
            wrote[r["target"]] = (r["pic"], r["poc"])
    if not closed:
        raise Reject(f"{path}: incomplete final picture (missing last batch)")
    return pictures


# --- comparison --------------------------------------------------------------

def diff_value(path, a, b):
    if a != b:
        return {"field": path, "a": a, "b": b}
    return None


def diff_list(path, a, b, item):
    for i, (x, y) in enumerate(zip(a, b)):
        d = item(f"{path}[{i}]", x, y)
        if d:
            return d
    if len(a) != len(b):
        return {"field": f"{path}.length", "a": len(a), "b": len(b)}
    return None


def diff_dpb_entry(path, a, b):
    for key in ("src", "poc", "lt", "field"):
        d = diff_value(f"{path}.{key}", a.get(key), b.get(key))
        if d:
            if key == "src" and (a.get("src") is None or b.get("src") is None):
                d["note"] = "reference buffer not written by an earlier traced picture"
            return d
    return diff_unknown(path, a, b, set(DPB_INT) | {"src"})


def diff_slice(path, a, b):
    for key in ("type", "nal", "l0", "l1", "tmvp", "col_l0", "col"):
        d = diff_value(f"{path}.{key}", a.get(key), b.get(key))
        if d:
            return d
    return diff_unknown(path, a, b, KNOWN_SLICE)


def diff_unknown(path, a, b, known):
    for key in sorted((set(a) | set(b)) - known - {"_line"}):
        if (key in a) != (key in b):
            return {"field": f"{path}.{key}", "a": a.get(key), "b": b.get(key),
                    "unknown_field": True, "note": "field present on only one side"}
        d = diff_value(f"{path}.{key}", a.get(key), b.get(key))
        if d:
            d["unknown_field"] = True
            return d
    return None


def diff_request(path, a, b):
    if "error" in a or "error" in b:
        if a.get("error") != b.get("error"):
            return {"field": f"{path}.error", "a": a.get("error"), "b": b.get("error"),
                    "note": "record content unavailable on one side"}
        return None
    for key in ("first", "last", "irap", "idr", "ltr_sps", "total_curr"):
        d = diff_value(f"{path}.{key}", a[key], b[key])
        if d:
            return d
    d = diff_list(f"{path}.dpb", a["dpb"], b["dpb"], diff_dpb_entry)
    if d:
        return d
    for key in TOP_LIST:
        d = diff_value(f"{path}.{key}", a[key], b[key])
        if d:
            return d
    d = diff_list(f"{path}.slices", a["slices"], b["slices"], diff_slice)
    if d:
        return d
    d = diff_value(f"{path}.slices_omitted", a.get("slices_omitted", 0), b.get("slices_omitted", 0))
    if d:
        return d
    return diff_unknown(path, a, b, KNOWN_TOP)


def compare(a_records, b_records, a_path, b_path, ctx_a=None, ctx_b=None, expected_pictures=None):
    ctx_a, a_records = select_context(a_records, a_path, ctx_a)
    ctx_b, b_records = select_context(b_records, b_path, ctx_b)
    a_pics = pictures_of(a_records, a_path)
    b_pics = pictures_of(b_records, b_path)
    if expected_pictures is not None:
        if expected_pictures < 1 or len(a_pics) != expected_pictures or len(b_pics) != expected_pictures:
            raise Reject("trace picture count does not match the independently expected count")
    result = {"equal": True, "scope": "recorded reference fields only",
              "extent": "expected picture count" if expected_pictures is not None else "captured prefix only", "context_a": ctx_a, "context_b": ctx_b,
              "pictures_a": len(a_pics), "pictures_b": len(b_pics), "pictures_compared": 0}
    for pa, pb in zip(a_pics, b_pics):
        result["pictures_compared"] += 1
        head = {"pic": pa["pic"], "poc_a": pa.get("poc"), "poc_b": pb.get("poc")}
        # A picture whose records all overflowed has no POC; the request
        # comparison below reports that as unavailable content instead.
        if "poc" in pa and "poc" in pb and pa["poc"] != pb["poc"]:
            result.update(equal=False, difference=dict(head, field="poc", a=pa.get("poc"), b=pb.get("poc")))
            return result
        for i, (ra, rb) in enumerate(zip(pa["requests"], pb["requests"])):
            d = diff_request(f"req[{i}]", ra, rb)
            if d:
                result.update(equal=False, difference=dict(head, **d, line_a=ra["_line"], line_b=rb["_line"]))
                return result
        if len(pa["requests"]) != len(pb["requests"]):
            result.update(equal=False, difference=dict(head, field="requests", a=len(pa["requests"]), b=len(pb["requests"])))
            return result
    if len(a_pics) != len(b_pics):
        short = "B" if len(b_pics) < len(a_pics) else "A"
        n = min(len(a_pics), len(b_pics))
        result.update(equal=False, difference={"pic": n + 1, "field": "missing",
                                               "note": f"trace {short} ends after picture {n}"})
    return result


def format_result(result):
    if result["equal"]:
        return (f"equal: {result['pictures_compared']} pictures "
                f"(context A={result['context_a']}, B={result['context_b']}; "
                f"{result['extent']}; {result['scope']})")
    d = result["difference"]
    text = f"first difference at picture {d['pic']}"
    if d.get("poc_a") is not None:
        text += f" (poc {d['poc_a']})"
    text += f": {d['field']}: {d.get('a')!r} != {d.get('b')!r}"
    if d.get("note"):
        text += f" [{d['note']}]"
    if d.get("unknown_field"):
        text += " [field unknown to this checker version]"
    if "line_a" in d:
        text += f" (A line {d['line_a']}, B line {d['line_b']})"
    return text


# --- synthetic fixtures ------------------------------------------------------

def synth(run, ctx, buf_base=0, va_context="0x02000000"):
    """Four synthetic pictures: IDR, P, B, P with a three-slot DPB. Values are
    invented for the checker; they are not from any hardware capture."""
    def rec(seq, pic, req, target, poc, dpb, before, after, lt, slices,
            first=1, last=1, irap=0, idr=0, ltr_sps=0, reorder=1):
        return {"schema": SCHEMA, "run": run, "seq": seq, "ctx": ctx, "va_context": va_context,
                "pic": pic, "req": req, "first": first, "last": last, "target": target + buf_base,
                "poc": poc, "irap": irap, "idr": idr, "ltr_sps": ltr_sps, "reorder": reorder,
                "total_curr": len(before) + len(after) + len(lt),
                "dpb": [{"i": i, "buf": b + buf_base, "poc": p, "lt": l, "field": 0}
                        for i, (b, p, l) in enumerate(dpb)],
                "st_before": before, "st_after": after, "lt_curr": lt,
                "slices": [{"i": i, "type": t, "nal": n, "l0": l0, "l1": l1, "tmvp": 0}
                           for i, (t, n, l0, l1) in enumerate(slices)]}
    return [
        rec(1, 1, 1, 0, 0, [], [], [], [], [("I", 19, [], [])], irap=1, idr=1),
        rec(2, 2, 2, 1, 4, [(0, 0, 0)], [0], [], [], [("P", 1, [0], [])]),
        rec(3, 3, 3, 2, 2, [(0, 0, 0), (1, 4, 0)], [0], [1], [], [("B", 1, [0], [1])]),
        rec(4, 4, 4, 3, 8, [(0, 0, 0), (1, 4, 0), (2, 2, 0)], [1, 0], [], [],
            [("P", 1, [1, 0], [])]),
    ]


def dumps(records):
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)


def fixtures():
    a = synth("00000000000000aa", 3)
    b = synth("00000000000000bb", 7, buf_base=2, va_context="0x02000001")
    out = {"synthetic-equal-a.jsonl": dumps(a), "synthetic-equal-b.jsonl": dumps(b)}

    v = synth("00000000000000bb", 7, buf_base=2)
    v[3]["dpb"][0], v[3]["dpb"][1] = dict(v[3]["dpb"][1], i=0), dict(v[3]["dpb"][0], i=1)
    v[3]["st_before"] = [0, 1]
    v[3]["slices"][0]["l0"] = [0, 1]
    out["synthetic-dpb-order-b.jsonl"] = dumps(v)

    v = synth("00000000000000bb", 7, buf_base=2)
    v[2]["dpb"][1]["lt"] = 1
    v[2]["st_after"] = []
    v[2]["lt_curr"] = [1]
    v[2]["ltr_sps"] = 1
    v[2]["reorder"] = 0
    out["synthetic-ltr-flag-b.jsonl"] = dumps(v)

    v = synth("00000000000000bb", 7, buf_base=2)
    v[1]["poc"] = 6
    out["synthetic-poc-b.jsonl"] = dumps(v)

    v = synth("00000000000000bb", 7, buf_base=2)
    v[3]["slices"][0]["l0"] = [0, 1]
    out["synthetic-slice-refs-b.jsonl"] = dumps(v)

    out["synthetic-short-b.jsonl"] = dumps(synth("00000000000000bb", 7, buf_base=2)[:3])

    text = dumps(a)
    out["synthetic-truncated.jsonl"] = text[:-40]

    v = synth("00000000000000aa", 3)
    del v[2]["run"]
    out["synthetic-missing-run.jsonl"] = dumps(v)

    v = synth("00000000000000aa", 3)
    v[3]["seq"] = 5
    out["synthetic-gap.jsonl"] = dumps(v)

    v = synth("00000000000000aa", 3)
    v[0]["schema"] = "libva-v4l2request.hevc-refs/2"
    out["synthetic-wrong-schema.jsonl"] = dumps(v)

    v = synth("00000000000000aa", 3)
    for i, r in enumerate(v):
        r["seq"] = i + 1
        r["pic"] = r["req"] = i + 1
    out["synthetic-picture-gap.jsonl"] = dumps([v[0], dict(v[2], seq=2, req=2)])

    x = synth("00000000000000cc", 3)
    y = synth("00000000000000cc", 4, buf_base=4)
    mixed = []
    for i, (p, q) in enumerate(zip(x, y)):
        mixed.append(dict(p, seq=2 * i + 1))
        mixed.append(dict(q, seq=2 * i + 2))
    out["synthetic-two-contexts.jsonl"] = dumps(mixed)

    v = synth("00000000000000bb", 7, buf_base=2)
    v[3] = {"schema": SCHEMA, "run": v[3]["run"], "seq": 4, "ctx": 7, "pic": 4, "req": 4,
            "error": "record-overflow"}
    out["synthetic-overflow-b.jsonl"] = dumps(v)

    v = synth("00000000000000bb", 7, buf_base=2)
    v[2]["future_field"] = {"x": 1}
    out["synthetic-unknown-field-b.jsonl"] = dumps(v)
    return out


FIXTURE_README = """# Synthetic HEVC reference-trace fixtures

**Provenance: synthetic.** Every file here is generated by
`tests/hevc-reftrace-check.py --write-fixtures` from invented values; none of
them comes from a hardware capture, a conformance stream or a real decoder.
They exist only to prove the checker's accept/reject/first-difference
behaviour. The self-test rejects a checkout whose fixtures drift from the
generator.

| File | Expected result |
|---|---|
| `synthetic-equal-a.jsonl` vs `synthetic-equal-b.jsonl` | equal (different run, context and buffer indices) |
| `synthetic-dpb-order-b.jsonl` | first difference: picture 4 `req[0].dpb[0].src` |
| `synthetic-ltr-flag-b.jsonl` | first difference: picture 3 `req[0].ltr_sps` |
| `synthetic-poc-b.jsonl` | first difference: picture 2 `poc` |
| `synthetic-slice-refs-b.jsonl` | first difference: picture 4 `req[0].slices[0].l0` |
| `synthetic-short-b.jsonl` | first difference: trace B ends after picture 3 |
| `synthetic-overflow-b.jsonl` | rejected: unavailable record content |
| `synthetic-unknown-field-b.jsonl` | first difference: picture 3 unknown field `future_field` |
| `synthetic-truncated.jsonl` | rejected: truncated record |
| `synthetic-missing-run.jsonl` | rejected: missing run identity |
| `synthetic-gap.jsonl` | rejected: sequence gap |
| `synthetic-picture-gap.jsonl` | rejected: picture ordinal gap |
| `synthetic-wrong-schema.jsonl` | rejected: incompatible schema |
| `synthetic-two-contexts.jsonl` | rejected without `--context` (ambiguous); compared with itself as `--context 3 4` it is equal, since both contexts carry the same pictures in different buffers |
"""


def write_fixtures():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, text in fixtures().items():
        (FIXTURES / name).write_text(text, encoding="utf-8")
    (FIXTURES / "README.md").write_text(FIXTURE_README, encoding="utf-8")
    print(f"wrote {len(fixtures())} fixtures to {FIXTURES}")


# --- self-test ---------------------------------------------------------------

def expect_reject(path, needle, **kw):
    try:
        records = load(path)
        compare(records, records, path, path, **kw)
    except Reject as error:
        if needle not in str(error):
            raise AssertionError(f"{path.name}: rejected for the wrong reason: {error}")
        return
    raise AssertionError(f"{path.name}: was not rejected")


def expect_difference(a, b, pic, field):
    result = compare(load(a), load(b), a, b)
    if result["equal"]:
        raise AssertionError(f"{b.name}: no difference found")
    d = result["difference"]
    if (d["pic"], d["field"]) != (pic, field):
        raise AssertionError(f"{b.name}: first difference {d['pic']}/{d['field']}, "
                             f"expected {pic}/{field}: {format_result(result)}")
    return result


def adversarial_tests():
    base = synth("00000000000000aa", 3)
    mutations = [
        ("missing first", lambda r: r[-1].update(first=0), "missing first"),
        ("missing last", lambda r: r[-1].update(last=0), "incomplete final"),
        ("omitted slices", lambda r: r[-1].update(slices_omitted=2), "omitted slices"),
        ("unresolved reference", lambda r: r[1]["dpb"][0].update(buf=99), "unresolved reference"),
        ("bad slot", lambda r: r[1]["slices"][0].update(l0=[15]), "out-of-range"),
        ("bad flag", lambda r: r[0].update(first=9), "invalid 'first'"),
        ("zero picture", lambda r: r[0].update(pic=0), "invalid 'pic'"),
        ("bad POC", lambda r: r[0].update(poc=1 << 31), "invalid 'poc'"),
        ("forged source", lambda r: r[1]["dpb"][0].update(src=[1, 0]), "reserved checker"),
        ("bad collocated", lambda r: r[1]["slices"][0].update(tmvp=1, col_l0=1, col=2), "invalid 'col'"),
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "trace.jsonl"
        for name, mutate, message in mutations:
            records = copy.deepcopy(base)
            mutate(records)
            path.write_text(dumps(records))
            expect_reject(path, message)
        for text, message in [
            (dumps(base).replace('"seq":1', '"seq":1,"seq":1', 1), "duplicate JSON key"),
            (dumps(base).replace('"poc":0', '"poc":NaN', 1), "non-JSON"),
            (dumps(base).replace('"poc":0', '"extra":"' + 'x' * 4096 + '","poc":0', 1), "byte bound"),
        ]:
            path.write_text(text)
            expect_reject(path, message)
        # A second batch cannot change its destination, reopen a completed
        # picture, or advance to another picture before closing the first.
        for change, message in [("target", "changes POC/target"),
                                ("first", "duplicate first"),
                                ("last", "incomplete picture")]:
            records = copy.deepcopy(base)
            records[1]["last"] = 0
            second = dict(records[1], first=0, last=1, req=3, seq=3)
            records.insert(2, second)
            for i, row in enumerate(records): row.update(seq=i + 1, req=i + 1)
            if change == "target": second["target"] += 1
            elif change == "first": second["first"] = 1
            else: second["last"] = 0
            path.write_text(dumps(records))
            expect_reject(path, message)
        path.write_text(dumps(base[:3]))
        rows = load(path)
        assert compare(rows, rows, path, path)["extent"] == "captured prefix only"
        try:
            compare(rows, rows, path, path, expected_pictures=4)
        except Reject:
            pass
        else:
            raise AssertionError("two equally shortened traces claimed full coverage")
        path.write_text(dumps(base))
        rows = load(path)
        other = copy.deepcopy(rows)
        other[0]["reorder"] ^= 1
        assert compare(rows, other, path, path, expected_pictures=4)["equal"]
        other[0]["future_field"] = None
        assert not compare(rows, other, path, path)["equal"]
        records = copy.deepcopy(base)
        records[1]["slices"][0].update(tmvp=1, col_l0=0, col=0)
        path.write_text(dumps(records))
        rows = load(path)
        assert compare(rows, rows, path, path)["equal"]  # P slices implicitly use L0.
        records = copy.deepcopy(base)
        records[0]["slices"][0].update(tmvp=1, col_l0=0, col=255)
        path.write_text(dumps(records))
        rows = load(path)
        assert compare(rows, rows, path, path)["equal"]  # I has no active collocated reference.
        other = copy.deepcopy(rows)
        other[0]["slices"][0]["col"] = 0
        assert not compare(rows, other, path, path)["equal"]  # Retain raw differences.
        records[0]["slices"][0]["col"] = 256
        path.write_text(dumps(records))
        expect_reject(path, "invalid 'col'")
    print("hevc-reftrace-check adversarial regressions: PASS (23 cases)")


def self_test(binary):
    adversarial_tests()
    generated = fixtures()
    for name, text in generated.items():
        on_disk = (FIXTURES / name).read_text(encoding="utf-8")
        if on_disk != text:
            raise AssertionError(f"fixture {name} differs from the generator; "
                                 "run hevc-reftrace-check.py --write-fixtures")
    a = FIXTURES / "synthetic-equal-a.jsonl"
    result = compare(load(a), load(FIXTURES / "synthetic-equal-b.jsonl"), a, "b")
    assert result["equal"] and result["pictures_compared"] == 4, result
    expect_difference(a, FIXTURES / "synthetic-dpb-order-b.jsonl", 4, "req[0].dpb[0].src")
    expect_difference(a, FIXTURES / "synthetic-ltr-flag-b.jsonl", 3, "req[0].ltr_sps")
    expect_difference(a, FIXTURES / "synthetic-poc-b.jsonl", 2, "poc")
    expect_difference(a, FIXTURES / "synthetic-slice-refs-b.jsonl", 4, "req[0].slices[0].l0")
    r = expect_difference(a, FIXTURES / "synthetic-short-b.jsonl", 4, "missing")
    assert "ends after picture 3" in r["difference"]["note"]
    expect_reject(FIXTURES / "synthetic-overflow-b.jsonl", "unavailable record content")
    r = expect_difference(a, FIXTURES / "synthetic-unknown-field-b.jsonl", 3, "req[0].future_field")
    assert r["difference"].get("unknown_field")
    expect_reject(FIXTURES / "synthetic-truncated.jsonl", "truncated")
    expect_reject(FIXTURES / "synthetic-missing-run.jsonl", "missing run identity")
    expect_reject(FIXTURES / "synthetic-gap.jsonl", "breaks the sequence")
    expect_reject(FIXTURES / "synthetic-picture-gap.jsonl", "picture ordinal gap")
    expect_reject(FIXTURES / "synthetic-wrong-schema.jsonl", "incompatible schema")
    two = FIXTURES / "synthetic-two-contexts.jsonl"
    expect_reject(two, "ambiguous frame correlation")
    result = compare(load(two), load(two), two, two, ctx_a=3, ctx_b=4)
    assert result["equal"], result
    try:
        compare(load(two), load(two), two, two, ctx_a=3, ctx_b=9)
    except Reject as error:
        assert "not present" in str(error)
    else:
        raise AssertionError("unknown context accepted")

    checked = "fixtures only"
    if binary:
        result = subprocess.run([binary, "emit-samples"], capture_output=True, text=True,
                                timeout=30, check=False)
        if result.returncode:
            raise AssertionError(f"emit-samples failed ({result.returncode}): {result.stderr}")
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(result.stdout)
            sample = pathlib.Path(handle.name)
        try:
            records = load(sample)
            contexts = sorted({r["ctx"] for r in records})
            assert len(contexts) == 2, contexts
            expect_reject(sample, "ambiguous frame correlation")
            for ctx in contexts:
                r = compare(records, records, sample, sample, ctx_a=ctx, ctx_b=ctx)
                assert r["equal"], r
            # The writer's own long-term picture is attributable to its flag.
            first = [r for r in records if r["ctx"] == contexts[0]]
            assert first[4]["ltr_sps"] == 1 and first[4]["lt_curr"] == [1]
            assert any(e["lt"] == 1 for e in first[4]["dpb"])
            assert first[4]["dpb"][1]["buf"] == 1
            second = [r for r in records if r["ctx"] == contexts[1]]
            assert [(r["pic"], r["req"], r["first"], r["last"]) for r in second] == \
                [(1, 1, 1, 1), (2, 2, 1, 0), (2, 3, 0, 1)], second
            # Nothing path-like anywhere but the schema tag.
            assert all("/" not in json.dumps({k: v for k, v in r.items() if k != "schema"})
                       for r in records)
            checked = f"fixtures and {len(records)} writer records"
        finally:
            sample.unlink()
    print(f"hevc-reftrace-check self-test: PASS ({checked})")


# --- command line ------------------------------------------------------------

def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--self-test":
        self_test(argv[1] if len(argv) > 1 else None)
        return 0
    if argv[0] == "--write-fixtures":
        write_fixtures()
        return 0
    if argv[0] == "validate" and len(argv) == 2:
        records = load(argv[1])
        contexts = sorted({r["ctx"] for r in records})
        for ctx in contexts:
            pictures_of([r for r in records if r["ctx"] == ctx], argv[1])
        print(f"valid: {len(records)} records, run {records[0]['run']}, contexts {contexts}")
        return 0
    if argv[0] == "compare" and len(argv) >= 3:
        args = argv[1:]
        as_json = "--json" in args
        args = [x for x in args if x != "--json"]
        expected_pictures = None
        if "--expected-pictures" in args:
            i = args.index("--expected-pictures")
            try:
                expected_pictures = int(args[i + 1])
            except (IndexError, ValueError):
                raise Reject("--expected-pictures needs a positive integer")
            if expected_pictures < 1:
                raise Reject("--expected-pictures needs a positive integer")
            del args[i:i + 2]
        ctx_a = ctx_b = None
        if "--context" in args:
            i = args.index("--context")
            try:
                ctx_a, ctx_b = int(args[i + 1]), int(args[i + 2])
            except (IndexError, ValueError):
                raise Reject("--context needs two integer context serials")
            del args[i:i + 3]
        if len(args) != 2:
            raise Reject("compare needs exactly two trace files")
        result = compare(load(args[0]), load(args[1]), args[0], args[1], ctx_a, ctx_b, expected_pictures)
        print(json.dumps(result, indent=1) if as_json else format_result(result))
        return 0 if result["equal"] else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Reject as error:
        print(f"hevc-reftrace-check: rejected: {error}", file=sys.stderr)
        sys.exit(2)
    except AssertionError as error:
        print(f"hevc-reftrace-check: self-test failed: {error}", file=sys.stderr)
        sys.exit(1)
