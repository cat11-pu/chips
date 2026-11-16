#!/usr/bin/env python3
"""Run AYT malformed-corpus and legal-reference property tests."""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import ayt_reference


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SAMPLE_RATE = 44100
FRAME_RATE = 50
SAMPLES_PER_FRAME = SAMPLE_RATE // FRAME_RATE


def read_manifest(path: Path) -> List[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


def read_pcm(path: Path) -> Tuple[int, List[float]]:
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("PCM file is shorter than its count field")
    count = struct.unpack_from("<I", data, 0)[0]
    expected = 4 + count * 4
    if len(data) != expected:
        raise ValueError(f"PCM length {len(data)} != {expected}")
    values = struct.unpack_from(f"<{count}f", data, 4)
    return count, list(values)


def sanitizer_report(stderr: str) -> str:
    if "LeakSanitizer: detected memory leaks" in stderr:
        return "leak"
    if "AddressSanitizer" in stderr or "SEGV" in stderr or "DEADLYSIGNAL" in stderr:
        return "out_of_bounds_or_crash"
    if "runtime error:" in stderr:
        return "undefined_behavior"
    return ""


def run_command(args: Sequence[str], env: dict, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_register_stress(harness: Path, env: dict) -> dict:
    result = run_command([str(harness), "stress", "10000", "3735928559"], env)
    observed = None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        try:
            observed = json.loads(lines[-1])
        except json.JSONDecodeError:
            pass
    sanitizer = sanitizer_report(result.stderr)
    failed = result.returncode != 0 or sanitizer or not observed
    return {
        "frames": 10000,
        "status": "fail" if failed else "pass",
        "sanitizer": sanitizer,
        "observed": observed,
        "stderr": result.stderr.strip()[-1200:],
    }


def test_malformed(
    harness: Path,
    manifest: Path,
    env: dict,
) -> Tuple[dict, List[dict]]:
    rows = read_manifest(manifest)
    by_kind = Counter(row["kind"] for row in rows)
    passed = 0
    counterexamples: List[dict] = []
    crashes = 0
    sanitizer_findings = Counter()
    offset_mismatches = 0
    byte_mismatches = 0
    report_rows = []

    for row in rows:
        path = manifest.parent / row["path"]
        expected_kind = row["kind"]
        expected_frame = int(row["frame"])
        expected_byte = int(row["byte"])
        result = run_command(
            [str(harness), "check-corrupt", str(path), expected_kind, str(expected_frame)],
            env,
        )
        reason = ""
        observed = None
        sanitizer = sanitizer_report(result.stderr)
        if result.returncode < 0:
            reason = "killed_by_signal"
            crashes += 1
        elif sanitizer:
            reason = sanitizer
            sanitizer_findings[sanitizer] += 1
        else:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            if lines:
                try:
                    observed = json.loads(lines[-1])
                except json.JSONDecodeError:
                    reason = "bad_harness_json"
            if observed is None and not reason:
                reason = "missing_harness_result"
            elif observed and observed.get("status") == "accepted":
                reason = "unexpected_accept"
            elif observed and observed.get("kind") != expected_kind:
                reason = "wrong_error_kind"
            elif observed and observed.get("frame") != expected_frame:
                reason = "wrong_frame_offset"
                offset_mismatches += 1
            elif observed and observed.get("byte") != expected_byte:
                reason = "wrong_byte_offset"
                byte_mismatches += 1
            elif result.returncode != 0:
                reason = "nonzero_exit"

        if reason:
            counterexample = {
                "file": str(path.relative_to(REPO_ROOT)),
                "kind": expected_kind,
                "expected_frame": expected_frame,
                "expected_byte": expected_byte,
                "reason": reason,
                "observed": observed,
                "last_frame_hint": (
                    observed.get("frame") if isinstance(observed, dict) else expected_frame
                ),
                "stderr": result.stderr.strip()[-1200:],
            }
            counterexamples.append(counterexample)
        else:
            passed += 1
        report_rows.append({
            "file": row["path"],
            "kind": expected_kind,
            "expected_frame": expected_frame,
            "expected_byte": expected_byte,
            "observed_kind": observed.get("kind") if observed else sanitizer or "crash",
            "observed_frame": observed.get("frame") if observed else None,
            "passed": not bool(reason),
        })

    summary = {
        "total": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "by_kind": dict(sorted(by_kind.items())),
        "crashes": crashes,
        "sanitizer_findings": dict(sanitizer_findings),
        "offset_mismatches": offset_mismatches,
        "byte_mismatches": byte_mismatches,
        "counterexamples": counterexamples,
    }
    return summary, report_rows


def test_valid(
    harness: Path,
    manifest: Path,
    env: dict,
) -> Tuple[dict, List[dict], List[dict]]:
    rows = read_manifest(manifest)
    counterexamples: List[dict] = []
    report_rows = []
    total_samples = 0
    exact_matches = 0
    worst_diff = 0.0
    failed = 0

    with tempfile.TemporaryDirectory(prefix="ayt-pcm-") as tmp:
        tmp_path = Path(tmp)
        for row in rows:
            path = manifest.parent / row["path"]
            stem = path.stem
            c_path = tmp_path / f"{stem}.c.pcm"
            result = run_command([str(harness), "render", str(path), str(c_path)], env)
            reason = sanitizer_report(result.stderr)
            if not reason and result.returncode != 0:
                reason = "render_failed"
            try:
                c_count, c_samples = read_pcm(c_path) if not reason else (0, [])
                ref_file = ayt_reference.parse_ayt(path)
                ref_samples = []
                ay = ayt_reference.Ay(ref_file.clock_hz)
                samples_per_frame = SAMPLE_RATE // ref_file.frame_rate
                for frame_index in range(ref_file.play_frames):
                    ayt_reference._apply_frame(ay, ref_file.frames, frame_index)
                    for _ in range(samples_per_frame):
                        while not ay.tick():
                            pass
                        ref_samples.append(ay.sample)
            except Exception as exc:
                reason = reason or f"reference_failed:{type(exc).__name__}"
                c_count, c_samples, ref_samples = 0, [], []

            max_diff = 0.0
            first_bad_sample = None
            first_bad_frame = None
            mismatched = 0
            if not reason:
                if c_count != len(ref_samples):
                    reason = "sample_count_mismatch"
                else:
                    for sample_index, (actual, expected) in enumerate(zip(c_samples, ref_samples)):
                        diff = abs(actual - expected)
                        if diff > max_diff:
                            max_diff = diff
                        if actual != expected:
                            mismatched += 1
                        if diff > 1e-6 and first_bad_sample is None:
                            first_bad_sample = sample_index
                            first_bad_frame = sample_index // samples_per_frame
                    if first_bad_sample is not None:
                        reason = "sample_mismatch"

            total_samples += min(c_count, len(ref_samples))
            exact_matches += max(0, min(c_count, len(ref_samples)) - mismatched)
            worst_diff = max(worst_diff, max_diff)
            if reason:
                failed += 1
                counterexamples.append({
                    "file": str(path.relative_to(REPO_ROOT)),
                    "reason": reason,
                    "frame": first_bad_frame,
                    "sample": first_bad_sample,
                    "max_abs_diff": max_diff,
                    "mismatched_samples": mismatched,
                    "stderr": result.stderr.strip()[-1200:],
                })
            report_rows.append({
                "file": row["path"],
                "samples": c_count,
                "mismatched_samples": mismatched,
                "max_abs_diff": max_diff,
                "first_bad_sample": first_bad_sample,
                "first_bad_frame": first_bad_frame,
                "passed": not bool(reason),
            })

    summary = {
        "total": len(rows),
        "passed": len(rows) - failed,
        "failed": failed,
        "samples_compared": total_samples,
        "exact_samples": exact_matches,
        "mismatched_samples": total_samples - exact_matches,
        "max_abs_diff": worst_diff,
        "tolerance": 1e-6,
        "counterexamples": counterexamples,
    }
    return summary, report_rows, counterexamples


def write_reports(
    output_dir: Path,
    malformed: dict,
    valid: dict,
    register_stress: dict,
    malformed_rows: List[dict],
    valid_rows: List[dict],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "sample_rate": SAMPLE_RATE,
            "frame_rate": FRAME_RATE,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "sanitizers": "address,undefined,leak",
            "malformed_expected_outcome": "reject with matching frame offset",
            "legal_comparison": "all samples, abs tolerance 1e-6",
        },
        "malformed": malformed,
        "valid": valid,
        "register_stress": register_stress,
        "counterexamples": malformed["counterexamples"] + valid["counterexamples"],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    md = [
        "# AYT audio property test summary",
        "",
        f"- Generated: `{result['generated_at_utc']}`",
        f"- Invalid corpus: **{malformed['passed']}/{malformed['total']}** rejected with the expected frame",
        f"- Crashes: **{malformed['crashes']}**",
        f"- ASan/UBSan findings: **{sum(malformed['sanitizer_findings'].values())}**",
        f"- Leak findings: **{malformed['sanitizer_findings'].get('leak', 0)}**",
        f"- Wrong frame offsets: **{malformed['offset_mismatches']}**",
        f"- Wrong byte offsets: **{malformed['byte_mismatches']}**",
        f"- Valid corpus: **{valid['passed']}/{valid['total']}** files within tolerance",
        f"- Samples compared: **{valid['samples_compared']}**",
        f"- Exact sample matches: **{valid['exact_samples']}/{valid['samples_compared']}**",
        f"- Maximum absolute sample difference: **{valid['max_abs_diff']:.9g}**",
        f"- Counterexamples: **{len(result['counterexamples'])}**",
        f"- Chip register stress: **{register_stress['status']}** "
        f"({register_stress['frames']} frames, raw 8-bit addresses)",
        "",
        "## Malformed coverage",
        "",
        "| corruption | files | passed |",
        "|---|---:|---:|",
    ]
    passed_by_kind = Counter(
        row["kind"] for row in malformed_rows if row["passed"]
    )
    for kind, count in malformed["by_kind"].items():
        md.append(f"| `{kind}` | {count} | {passed_by_kind[kind]} |")
    if result["counterexamples"]:
        md.extend(("", "## Counterexamples", ""))
        for item in result["counterexamples"][:20]:
            md.append(
                f"- `{item['file']}`: `{item['reason']}`"
                + (f", frame `{item.get('frame')}`" if item.get("frame") is not None else "")
            )
    else:
        md.extend(("", "No failing cases were observed in the generated corpus.", ""))
    (output_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with (output_dir / "malformed_report.tsv").open("w", encoding="utf-8") as fp:
        fp.write("file\tkind\texpected_frame\texpected_byte\tobserved_kind\tobserved_frame\tpassed\n")
        for row in malformed_rows:
            fp.write(
                f"{row['file']}\t{row['kind']}\t{row['expected_frame']}\t"
                f"{row['expected_byte']}\t{row['observed_kind']}\t"
                f"{row['observed_frame']}\t{int(row['passed'])}\n"
            )
    with (output_dir / "valid_report.tsv").open("w", encoding="utf-8") as fp:
        fp.write("file\tsamples\tmismatched_samples\tmax_abs_diff\tfirst_bad_sample\tfirst_bad_frame\tpassed\n")
        for row in valid_rows:
            fp.write(
                f"{row['file']}\t{row['samples']}\t{row['mismatched_samples']}\t"
                f"{row['max_abs_diff']:.9g}\t{row['first_bad_sample']}\t"
                f"{row['first_bad_frame']}\t{int(row['passed'])}\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts/audio-property")
    parser.add_argument("--harness", type=Path, default=HERE / "ayt_harness_san")
    parser.add_argument("--skip-generate", action="store_true")
    args = parser.parse_args()

    output_dir = args.output.resolve()
    corpus_dir = output_dir / "corpus"
    if not args.skip_generate:
        subprocess.run(
            [
                sys.executable,
                str(HERE / "gen_corpus.py"),
                str(corpus_dir),
                "--counterexamples",
                str(HERE / "counterexamples"),
            ],
            check=True,
            cwd=REPO_ROOT,
        )

    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "detect_leaks=1:abort_on_error=1:strict_string_checks=1"
    env["UBSAN_OPTIONS"] = "print_stacktrace=1:halt_on_error=1"
    malformed, malformed_rows = test_malformed(
        args.harness,
        corpus_dir / "malformed_manifest.tsv",
        env,
    )
    register_stress = test_register_stress(args.harness, env)
    valid, valid_rows, _ = test_valid(
        args.harness,
        corpus_dir / "valid_manifest.tsv",
        env,
    )
    write_reports(output_dir, malformed, valid, register_stress, malformed_rows, valid_rows)
    print(json.dumps({
        "malformed": {k: malformed[k] for k in (
            "total", "passed", "failed", "crashes", "sanitizer_findings",
            "offset_mismatches", "byte_mismatches"
        )},
        "valid": {k: valid[k] for k in (
            "total", "passed", "failed", "samples_compared", "exact_samples",
            "mismatched_samples", "max_abs_diff"
        )},
        "register_stress": register_stress["status"],
        "counterexamples": len(malformed["counterexamples"]) + len(valid["counterexamples"]),
    }, indent=2))
    return 0 if (
        malformed["failed"] == 0
        and valid["failed"] == 0
        and register_stress["status"] == "pass"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
