"""
Supplementary to equivalence_test.py: runs the same Python-vs-Wasm
comparison against this project's REAL log datasets
(validation/real_data/datasets/), not just the synthetic corpus --
matters specifically for Layer 1's AWS-account-ID-in-ARN exclusion logic
(scanEmail/scanCreditCard's context check in assembly/index.ts), which
was ported as a direct hand-written scanner rather than a real regex
engine and therefore benefits most from being checked against real,
messy CloudTrail JSON and real OpenSSH/Linux/OpenStack/Thunderbird/
Zookeeper log lines this project didn't generate itself.

CloudTrail lines are re-serialized via json.dumps(obj), matching exactly
how validation/real_data/inject_and_evaluate.py builds the text detect.py
actually scans (see that file's own CloudTrail-handling block) -- not the
raw file line, which would test a string this project's own pipeline
never actually processes.

Usage: python3 wasm/layer1_4/tests/real_data_equivalence_test.py
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WASM_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import detect  # noqa: E402
import flattened_names  # noqa: E402

DATASETS_DIR = REPO_ROOT / "validation" / "real_data" / "datasets"


def normalize(hits):
    return sorted((h["type"], h["start"], h["end"]) for h in hits)


def load_lines():
    lines = []

    for name in ["Linux_2k", "OpenSSH_2k", "OpenStack_2k", "Thunderbird_2k", "Zookeeper_2k"]:
        path = DATASETS_DIR / f"{name}.log"
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.rstrip("\n")
                if raw:
                    lines.append((f"{name}.log", raw))

    cloudtrail_path = DATASETS_DIR / "CloudTrailFlaws_raw.jsonl"
    if cloudtrail_path.exists():
        with open(cloudtrail_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                text = json.dumps(obj)  # matches inject_and_evaluate.py's own construction
                lines.append(("CloudTrailFlaws_raw.jsonl", text))

    windows_path = DATASETS_DIR / "WindowsEventSamples_raw.jsonl"
    if windows_path.exists():
        with open(windows_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                text = obj.get("text") if isinstance(obj, dict) and "text" in obj else json.dumps(obj)
                lines.append(("WindowsEventSamples_raw.jsonl", text))

    return lines


def main():
    tagged_lines = load_lines()
    if not tagged_lines:
        print("No real-data datasets found under validation/real_data/datasets/ -- nothing to test.")
        sys.exit(0)

    plain_path = WASM_DIR / "tests" / "_real_data_equivalence_corpus.txt"
    # Real log lines can legitimately contain characters that would
    # collide with a naive newline-joined format if they ever embedded a
    # literal newline -- none of these datasets' lines do (confirmed:
    # each source file is itself one-record-per-line), so the same
    # simple format equivalence_test.py uses is safe here too.
    plain_path.write_text("\n".join(text for _, text in tagged_lines) + "\n")

    print(f"Running {len(tagged_lines)} real-data lines through the compiled Wasm module (Node)...")
    result = subprocess.run(
        ["node", str(WASM_DIR / "tests" / "run_wasm_over_corpus.mjs"), str(plain_path)],
        capture_output=True, text=True, cwd=str(WASM_DIR),
    )
    if result.returncode != 0:
        print("Wasm runner FAILED:", result.stderr, file=sys.stderr)
        sys.exit(1)
    wasm_results = json.loads(result.stdout)

    mismatches = []
    regex_match_count = 0
    flat_match_count = 0

    for i, (source, text) in enumerate(tagged_lines):
        py_regex_n = normalize(detect.scan_regex(text))
        py_flat_n = normalize(flattened_names.scan_flattened_names(text))

        wasm_rec = wasm_results[i] if i < len(wasm_results) else {"regex": [], "flattened": []}
        wasm_regex_n = normalize(wasm_rec["regex"])
        wasm_flat_n = normalize(wasm_rec["flattened"])

        if py_regex_n == wasm_regex_n:
            regex_match_count += 1
        else:
            mismatches.append({"line": i, "source": source, "layer": "regex", "text": text,
                                "python": py_regex_n, "wasm": wasm_regex_n})

        if py_flat_n == wasm_flat_n:
            flat_match_count += 1
        else:
            mismatches.append({"line": i, "source": source, "layer": "flattened_names", "text": text,
                                "python": py_flat_n, "wasm": wasm_flat_n})

    print()
    print(f"=== Layer 1 (regex): {regex_match_count}/{len(tagged_lines)} lines byte-identical ===")
    print(f"=== Layer 4 (flattened names): {flat_match_count}/{len(tagged_lines)} lines byte-identical ===")

    if mismatches:
        print(f"\n{len(mismatches)} line-level mismatches found. First 15:")
        for m in mismatches[:15]:
            print(f"  [{m['source']}] line {m['line']} [{m['layer']}]: {m['text'][:150]!r}")
            print(f"    python: {m['python']}")
            print(f"    wasm:   {m['wasm']}")
        sys.exit(1)
    else:
        print("\nAll real-data lines byte-identical between Python and the compiled Wasm module.")


if __name__ == "__main__":
    main()
