#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate the support-matrix contract, r11 pass sets, README claims and fixtures."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

OUTCOMES = {
    "hardware_pass",
    "software_fallback",
    "expected_rejection",
    "wrong_output",
    "untested",
}
TIERS = {"supported", "experimental", "unsupported", "untested"}
CRITICALITY = {"critical", "optional"}
LAYERS = {"decode", "encode", "demux", "drm", "tone_mapping", "client", "display"}
ROW_REQUIRED = (
    "id",
    "backend",
    "device",
    "codec",
    "va_profile",
    "bit_depth",
    "chroma",
    "coded_picture",
    "client",
    "api",
    "kernel",
    "library",
    "tier",
    "criticality",
    "outcome",
    "layer",
)
SUPPORTED_PROVENANCE = ("machine", "kernel_package", "driver_version", "date")
PRIMARY_SUITES = {
    "JCT-VC-HEVC_V1": (144, 147),
    "JVT-AVC_V1": (73, 135),
    "JVT-FR-EXT": (27, 69),
    "VP9-TEST-VECTORS": (216, 305),
}
README_FRACTIONS = ("144/147", "73/135", "27/69", "216/305")
HEVC_CLIENT_COMMIT = "bf1b838f2ab88b4f8fd83443325c782ea0e0f7fa"
HEVC_CLIENT_PATCH_SHA256 = (
    "a598cbf599060ff2bb105bb95a24c5dada122b58363ad9d24337cfb1f307f26b"
)
HEVC_CLIENT_EVIDENCE = (
    "https://github.com/iconidentify/omarchy-m1-video/blob/main/"
    "docs/evidence/issue15/hevc-parameter-sets-2026-09-19/README.md"
)
PROFILE_ARRAY = re.compile(
    r"static const VAProfile \w+_profiles\[\] = \{(.*?)\};", re.S
)
PROFILE_NAME = re.compile(r"VAProfile[A-Za-z0-9_]+")


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "docs" / "support-matrix.json").is_file() and (
            candidate / "meson.build"
        ).is_file():
            return candidate
    raise SystemExit("could not locate repository root from tests/support-matrix.py")


class Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def extend(self, messages: list[str]) -> None:
        self.items.extend(messages)

    def check(self, cond: bool, message: str) -> None:
        if not cond:
            self.add(message)


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def advertised_profiles_from_source(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted((root / "src").glob("codec_*.c")):
        text = path.read_text()
        for block in PROFILE_ARRAY.findall(text):
            found.extend(PROFILE_NAME.findall(block))
    found.append("VAProfileNone")
    # Preserve order, drop duplicates.
    unique: list[str] = []
    for name in found:
        if name not in unique:
            unique.append(name)
    return unique


def validate_row(row: dict, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"rows[{index}] id={row.get('id', '<missing>')}"
    if not isinstance(row, dict):
        return [f"rows[{index}] is not an object"]
    for key in ROW_REQUIRED:
        if key not in row:
            errors.append(f"{prefix} missing {key}")
    tier = row.get("tier")
    if tier not in TIERS:
        errors.append(f"{prefix} invalid tier {tier!r}")
    if row.get("outcome") not in OUTCOMES:
        errors.append(f"{prefix} invalid outcome {row.get('outcome')!r}")
    if row.get("criticality") not in CRITICALITY:
        errors.append(f"{prefix} invalid criticality {row.get('criticality')!r}")
    if row.get("layer") not in LAYERS:
        errors.append(f"{prefix} invalid layer {row.get('layer')!r}")
    va_profile = row.get("va_profile")
    if not isinstance(va_profile, str) or not va_profile.startswith("VAProfile"):
        errors.append(f"{prefix} va_profile must be a VAProfile* name")

    if tier == "supported":
        prov = row.get("hardware_provenance")
        if not isinstance(prov, dict):
            errors.append(
                f"{prefix} supported row missing hardware_provenance object"
            )
        else:
            for key in SUPPORTED_PROVENANCE:
                value = prov.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix} supported row missing hardware_provenance.{key}"
                    )
        artifact = row.get("result_artifact")
        if not isinstance(artifact, str) or not artifact.strip():
            errors.append(f"{prefix} supported row missing result_artifact")

    if row.get("outcome") == "hardware_pass":
        scope = row.get("outcome_scope")
        if scope not in ("full_suite", "exact_pass_set", "generated_matrix"):
            errors.append(
                f"{prefix} hardware_pass requires outcome_scope "
                "full_suite, exact_pass_set or generated_matrix"
            )
    return errors


def validate_suites(matrix: dict) -> list[str]:
    errors: list[str] = []
    suites = matrix.get("suites")
    if not isinstance(suites, dict) or not suites:
        return ["suites must be a non-empty object"]
    for key, suite in suites.items():
        prefix = f"suites[{key}]"
        if not isinstance(suite, dict):
            errors.append(f"{prefix} is not an object")
            continue
        if "eligible_percent" in suite or "eligible_total" in suite:
            errors.append(
                f"{prefix} eligible-subset fields cannot replace raw passed/total"
            )
        passed = suite.get("passed")
        total = suite.get("total")
        if not isinstance(passed, int) or not isinstance(total, int):
            errors.append(f"{prefix} passed and total must be integers")
            continue
        if total < 1 or passed < 0 or passed > total:
            errors.append(f"{prefix} invalid passed/total {passed}/{total}")
        if isinstance(suite.get("percent"), (int, float)) and "total" not in suite:
            errors.append(f"{prefix} percentage without raw total")
    for name, (passed, total) in PRIMARY_SUITES.items():
        suite = suites.get(name)
        if not isinstance(suite, dict):
            errors.append(f"primary suite {name} missing")
            continue
        if suite.get("passed") != passed or suite.get("total") != total:
            errors.append(
                f"primary suite {name} must remain {passed}/{total}, "
                f"got {suite.get('passed')}/{suite.get('total')}"
            )
        if not suite.get("primary"):
            errors.append(f"primary suite {name} must set primary=true")
    return errors


def validate_pass_sets(matrix: dict, pass_sets: dict) -> list[str]:
    errors: list[str] = []
    suites = pass_sets.get("suites")
    if not isinstance(suites, dict):
        return ["r11-pass-sets.json missing suites"]
    for key, expected in matrix.get("suites", {}).items():
        pkey = expected.get("pass_set_key")
        if not pkey:
            errors.append(f"suites[{key}] missing pass_set_key")
            continue
        recorded = suites.get(pkey)
        if not isinstance(recorded, dict):
            errors.append(f"pass-set key {pkey} missing")
            continue
        passing = recorded.get("passing_vectors") or []
        failing = recorded.get("failing_vectors") or []
        if recorded.get("passed") != expected.get("passed"):
            errors.append(
                f"{pkey} passed {recorded.get('passed')} != matrix {expected.get('passed')}"
            )
        if recorded.get("total") != expected.get("total"):
            errors.append(
                f"{pkey} total {recorded.get('total')} != matrix {expected.get('total')}"
            )
        if len(passing) != expected.get("passed"):
            errors.append(
                f"{pkey} passing_vectors has {len(passing)} names, expected {expected.get('passed')}"
            )
        if len(passing) + len(failing) != expected.get("total"):
            errors.append(
                f"{pkey} pass+fail {len(passing)+len(failing)} != total {expected.get('total')}"
            )
    hevc_fail = set(suites.get("hevc", {}).get("failing_vectors") or [])
    for vector in (
        "RPS_E_qualcomm_5",
        "TSUNEQBD_A_MAIN10_Technicolor_2",
        "VPSSPSPPS_A_MainConcept_1",
    ):
        if vector not in hevc_fail:
            errors.append(f"HEVC failing vector {vector} missing from pinned pass sets")
    return errors


def validate_hevc_client_qualification(matrix: dict) -> list[str]:
    """Keep the selected client result distinct from the shipped r11 baseline."""
    errors: list[str] = []
    rows = [
        row for row in matrix.get("rows", [])
        if isinstance(row, dict) and row.get("id") == "avd-m1-hevc-ffmpeg-param-sets"
    ]
    if len(rows) != 1:
        return ["delayed-parameter-set client row must appear exactly once"]
    row = rows[0]
    client = row.get("client") or ""
    if HEVC_CLIENT_COMMIT not in client:
        errors.append("delayed-parameter-set row missing exact FFmpeg commit")
    if HEVC_CLIENT_PATCH_SHA256 not in client:
        errors.append("delayed-parameter-set row missing exact client patch SHA-256")
    if row.get("result_artifact") != HEVC_CLIENT_EVIDENCE:
        errors.append("delayed-parameter-set row must link the issue #15 hardware evidence")
    if row.get("tier") != "experimental" or row.get("outcome") != "hardware_pass":
        errors.append("selected delayed-parameter-set client must remain an experimental hardware pass")
    if row.get("outcome_scope") != "exact_pass_set" or row.get("layer") != "client":
        errors.append("delayed-parameter-set result must remain a client-layer exact pass set")
    hevc = matrix.get("suites", {}).get("JCT-VC-HEVC_V1", {})
    if (hevc.get("passed"), hevc.get("total")) != (144, 147):
        errors.append("selected client result must not replace the packaged r11 144/147 baseline")
    notes = row.get("notes") or ""
    if "145/147" not in notes or "not installed" not in notes:
        errors.append("delayed-parameter-set notes must retain the 145/147 and not-installed limits")
    return errors


def validate_profile_coverage(matrix: dict, source_profiles: list[str]) -> list[str]:
    errors: list[str] = []
    advertised = matrix.get("advertised_va_profiles")
    if not isinstance(advertised, list) or not advertised:
        return ["advertised_va_profiles missing"]
    advertised_names = []
    for entry in advertised:
        if not isinstance(entry, dict) or "va_profile" not in entry:
            errors.append("advertised_va_profiles entry missing va_profile")
            continue
        advertised_names.append(entry["va_profile"])
    for name in source_profiles:
        if name not in advertised_names:
            errors.append(f"source profile {name} has no advertised_va_profiles entry")
    for name in advertised_names:
        if name not in source_profiles:
            errors.append(f"advertised profile {name} is not in codec profile tables or VAProfileNone")
    rows = matrix.get("rows") or []
    row_profiles = {
        row.get("va_profile")
        for row in rows
        if isinstance(row, dict)
    }
    for name in advertised_names:
        if name not in row_profiles:
            errors.append(f"advertised profile {name} has no matrix row")
    return errors


def validate_matrix_object(matrix: dict) -> list[str]:
    errors = Errors()
    if not isinstance(matrix, dict):
        return ["matrix is not an object"]
    for key in (
        "schema_version",
        "contract_id",
        "driver_version",
        "layers",
        "outcomes",
        "tiers",
        "promotion_rules",
        "demotion_rules",
        "proposed_production_gate",
        "platforms",
        "advertised_va_profiles",
        "suites",
        "rows",
    ):
        errors.check(key in matrix, f"missing {key}")
    errors.check(matrix.get("schema_version") == "1.0.0", "schema_version must be 1.0.0")
    errors.check(
        matrix.get("contract_id") == "libva-v4l2_request-support-matrix",
        "contract_id mismatch",
    )
    layers = matrix.get("layers")
    if isinstance(layers, dict):
        for key in ("decode", "encode", "demux", "drm", "tone_mapping"):
            errors.check(key in layers, f"layers missing {key}")
    else:
        errors.add("layers must be an object")
    if set(matrix.get("outcomes") or []) != OUTCOMES:
        errors.add("outcomes must be the five distinct contract results")
    tiers = matrix.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != TIERS:
        errors.add("tiers must define supported, experimental, unsupported, untested")
    rows = matrix.get("rows")
    if not isinstance(rows, list) or not rows:
        errors.add("rows must be a non-empty array")
        return errors.items
    ids = []
    for index, row in enumerate(rows):
        errors.extend(validate_row(row, index))
        if isinstance(row, dict) and "id" in row:
            ids.append(row["id"])
    if len(ids) != len(set(ids)):
        errors.add("row ids must be unique")
    advertised = matrix.get("advertised_va_profiles") or []
    advertised_names = [
        entry.get("va_profile")
        for entry in advertised
        if isinstance(entry, dict)
    ]
    row_profiles = {
        row.get("va_profile") for row in rows if isinstance(row, dict)
    }
    for name in advertised_names:
        if name not in row_profiles:
            errors.add(f"advertised profile {name} has no matrix row")
    errors.extend(validate_suites(matrix))
    gate = matrix.get("proposed_production_gate") or {}
    supported = sum(1 for row in rows if isinstance(row, dict) and row.get("tier") == "supported")
    if "stable_rows_at_r11" in gate and gate.get("stable_rows_at_r11") != supported:
        errors.add("proposed_production_gate.stable_rows_at_r11 must match supported row count")
    return errors.items


def validate_readme(readme: str, matrix: dict) -> list[str]:
    errors: list[str] = []
    for fraction in README_FRACTIONS:
        if fraction not in readme:
            errors.append(f"README.md missing raw suite total {fraction}")
    if "docs/SUPPORT.md" not in readme:
        errors.append("README.md must point at docs/SUPPORT.md")
    lowered = readme.lower()
    if re.search(r"av1 is (fully )?supported", lowered):
        errors.append("README.md appears to claim AV1 hardware support")
    matrix_suites = matrix.get("suites") or {}
    for name, (passed, total) in PRIMARY_SUITES.items():
        suite = matrix_suites.get(name) or {}
        if suite.get("passed") != passed or suite.get("total") != total:
            errors.append(f"matrix suite {name} drifted from README r11 totals")
    return errors


def mutate_supported(matrix: dict) -> dict:
    clone = copy.deepcopy(matrix)
    row = clone["rows"][0]
    row["tier"] = "supported"
    row["outcome"] = "hardware_pass"
    row["outcome_scope"] = "exact_pass_set"
    row["hardware_provenance"] = {
        "machine": "Apple M1 T8103 / MacBookPro17,1",
        "kernel_package": "linux-asahi 7.1.13.asahi3-1",
        "driver_version": "1.3.r11",
        "date": "2026-09-15",
    }
    row["result_artifact"] = matrix["evidence_record"]
    clone["proposed_production_gate"]["stable_rows_at_r11"] = 1
    return clone


def run_fixtures(live: dict) -> list[str]:
    errors: list[str] = []

    valid = mutate_supported(live)
    unexpected = validate_matrix_object(valid)
    if unexpected:
        errors.append("positive supported-row fixture failed: " + "; ".join(unexpected))

    missing_prov = mutate_supported(live)
    missing_prov["rows"][0].pop("hardware_provenance")
    missing_prov["proposed_production_gate"]["stable_rows_at_r11"] = 1
    got = validate_matrix_object(missing_prov)
    if not any("hardware_provenance" in item for item in got):
        errors.append("missing-provenance fixture did not fail on hardware_provenance")

    missing_artifact = mutate_supported(live)
    missing_artifact["rows"][0]["result_artifact"] = ""
    got = validate_matrix_object(missing_artifact)
    if not any("result_artifact" in item for item in got):
        errors.append("missing-artifact fixture did not fail on result_artifact")

    subset = copy.deepcopy(live)
    subset["suites"]["JVT-AVC_V1"]["passed"] = 73
    subset["suites"]["JVT-AVC_V1"]["total"] = 73
    subset["suites"]["JVT-AVC_V1"]["eligible_percent"] = 100
    got = validate_suites(subset)
    if not any("73/135" in item or "eligible-subset" in item for item in got):
        errors.append("subset-percentage fixture did not reject hidden 73/135 denominator")

    advertised_only = copy.deepcopy(live)
    advertised_only["rows"] = [
        row for row in live["rows"] if row.get("va_profile") != "VAProfileAV1Profile0"
    ]
    got = validate_matrix_object(advertised_only)
    if not any("VAProfileAV1Profile0" in item for item in got):
        errors.append("missing-profile fixture did not require an AV1 row")

    inferred = copy.deepcopy(live)
    for row in inferred["rows"]:
        if row.get("va_profile") == "VAProfileAV1Profile0":
            row["tier"] = "supported"
            row["outcome"] = "hardware_pass"
            row["outcome_scope"] = "full_suite"
            row["hardware_provenance"] = {"note": "compiled in codec_av1.c"}
            row["result_artifact"] = ""
    inferred["proposed_production_gate"]["stable_rows_at_r11"] = 1
    got = validate_matrix_object(inferred)
    if not any("hardware_provenance" in item or "result_artifact" in item for item in got):
        errors.append("inferred-AV1-supported fixture was not rejected")

    extrapolated = mutate_supported(live)
    extrapolated["rows"][0]["device"] = "apple-avd-m2"
    extrapolated["rows"][0]["hardware_provenance"]["machine"] = "copied from M1 without a run"
    # Still has provenance keys, so schema allows it; contract test: platforms.extrapolate_to
    # must stay empty and other-device rows must remain untested.
    other = [row for row in live["rows"] if row["id"] == "avd-other-apple-silicon"]
    if not other or other[0]["tier"] != "untested" or other[0]["outcome"] != "untested":
        errors.append("other Apple Silicon row must remain untested")
    if live["platforms"][0].get("extrapolate_to") not in ([], None):
        errors.append("M1 platform must not extrapolate to other devices")
    return errors


def validate_fixture_files(root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = root / "tests" / "fixtures" / "support-matrix" / "manifest.json"
    if not manifest_path.is_file():
        return ["missing tests/fixtures/support-matrix/manifest.json"]
    manifest = load_json(manifest_path)
    for entry in manifest:
        path = manifest_path.parent / entry["file"]
        matrix = load_json(path)
        got = validate_matrix_object(matrix)
        expect_ok = entry.get("ok", False)
        if expect_ok and got:
            errors.append(f"{path.name} should pass: " + "; ".join(got))
        if not expect_ok:
            needle = entry.get("error", "")
            if not any(needle in item for item in got):
                errors.append(
                    f"{path.name} should fail matching {needle!r}, got {got}"
                )
    return errors


def main() -> int:
    root = repo_root()
    matrix_path = root / "docs" / "support-matrix.json"
    pass_path = root / "docs" / "r11-pass-sets.json"
    schema_path = root / "docs" / "support-matrix.schema.json"
    readme_path = root / "README.md"
    errors = Errors()
    for path in (matrix_path, pass_path, schema_path, readme_path):
        errors.check(path.is_file(), f"missing {path.relative_to(root)}")
    if errors.items:
        print("\n".join(errors.items), file=sys.stderr)
        return 1

    matrix = load_json(matrix_path)
    pass_sets = load_json(pass_path)
    schema = load_json(schema_path)
    errors.check(isinstance(schema, dict) and "$defs" in schema, "schema missing $defs")
    errors.extend(validate_matrix_object(matrix))
    errors.extend(validate_pass_sets(matrix, pass_sets))
    errors.extend(validate_hevc_client_qualification(matrix))
    errors.extend(
        validate_profile_coverage(matrix, advertised_profiles_from_source(root))
    )
    errors.extend(validate_readme(readme_path.read_text(), matrix))
    errors.extend(run_fixtures(matrix))
    errors.extend(validate_fixture_files(root))

    if matrix.get("driver_version") != "1.3.r11":
        errors.add("live matrix driver_version must match r11 while this evidence is current")
    supported = sum(1 for row in matrix["rows"] if row.get("tier") == "supported")
    if supported != 0:
        errors.add("r11 contract must not declare supported rows")

    if errors.items:
        print(f"{len(errors.items)} support-matrix error(s):", file=sys.stderr)
        print("\n".join(f"- {item}" for item in errors.items), file=sys.stderr)
        return 1
    print(
        "support-matrix ok: "
        f"{len(matrix['rows'])} rows, "
        f"{len(matrix['advertised_va_profiles'])} advertised profiles, "
        "r11 totals 144/147 73/135 27/69 216/305, "
        "zero supported rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
