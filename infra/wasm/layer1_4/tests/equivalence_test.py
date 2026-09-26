"""
Task #48's actual verification step: confirms the compiled WebAssembly
module (build/layer1_4.wasm, built from assembly/index.ts) produces the
SAME detection hits as the real Python implementations it ports --
src/detect.py's scan_regex() (Layer 1) and src/flattened_names.py's
scan_flattened_names() (Layer 4) -- rather than just asserting the port
"should" be equivalent from reading the code.

Usage:
    python3 wasm/layer1_4/tests/equivalence_test.py [N]
        N = number of lines from data/synthetic_logs.jsonl to test
            (default: all 10,000)

Requires Node.js (to run the compiled Wasm module via
tests/run_wasm_over_corpus.mjs) and the module already built
(`cd wasm/layer1_4 && npm run build`).
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


def normalize(hits, keep_method=False):
    """(type, start, end[, method]) tuples, sorted -- order-independent
    comparison, since neither implementation guarantees the same
    iteration order across pattern types for hits at different spans
    (both DO preserve order within a single pattern type, but that's not
    what's being verified here -- overall span/type correctness is)."""
    if keep_method:
        return sorted((h["type"], h["start"], h["end"], h["method"]) for h in hits)
    return sorted((h["type"], h["start"], h["end"]) for h in hits)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None

    corpus_path = REPO_ROOT / "data" / "synthetic_logs.jsonl"
    lines = corpus_path.read_text().splitlines()
    if n is not None:
        lines = lines[:n]

    logs = []
    for line in lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        logs.append(rec["log"])

    # One raw log per line -- these are single-line generated strings, so
    # this straightforward newline-joined format round-trips cleanly.
    plain_path = WASM_DIR / "tests" / "_equivalence_corpus.txt"
    plain_path.write_text("\n".join(logs) + "\n")

    print(f"Running {len(logs)} lines through the compiled Wasm module (Node)...")
    result = subprocess.run(
        ["node", str(WASM_DIR / "tests" / "run_wasm_over_corpus.mjs"), str(plain_path)],
        capture_output=True, text=True, cwd=str(WASM_DIR),
    )
    if result.returncode != 0:
        print("Wasm runner FAILED:", result.stderr, file=sys.stderr)
        sys.exit(1)
    wasm_results = json.loads(result.stdout)

    print(f"Running the same {len(logs)} lines through src/detect.py + src/flattened_names.py...")
    mismatches = []
    regex_match_count = 0
    flat_match_count = 0
    total_regex_hits_py = 0
    total_flat_hits_py = 0

    for i, log in enumerate(logs):
        py_regex = detect.scan_regex(log)
        py_flat = flattened_names.scan_flattened_names(log)
        total_regex_hits_py += len(py_regex)
        total_flat_hits_py += len(py_flat)

        wasm_rec = wasm_results[i] if i < len(wasm_results) else {"regex": [], "flattened": []}
        wasm_regex = wasm_rec["regex"]
        wasm_flat = wasm_rec["flattened"]

        py_regex_n = normalize(py_regex)
        wasm_regex_n = normalize(wasm_regex)
        py_flat_n = normalize(py_flat)
        wasm_flat_n = normalize(wasm_flat)

        if py_regex_n == wasm_regex_n:
            regex_match_count += 1
        else:
            mismatches.append({
                "line": i, "layer": "regex", "log": log,
                "python": py_regex_n, "wasm": wasm_regex_n,
            })

        if py_flat_n == wasm_flat_n:
            flat_match_count += 1
        else:
            mismatches.append({
                "line": i, "layer": "flattened_names", "log": log,
                "python": py_flat_n, "wasm": wasm_flat_n,
            })

    print()
    print(f"=== Layer 1 (regex): {regex_match_count}/{len(logs)} lines byte-identical "
          f"({total_regex_hits_py} total Python hits) ===")
    print(f"=== Layer 4 (flattened names): {flat_match_count}/{len(logs)} lines byte-identical "
          f"({total_flat_hits_py} total Python hits) ===")

    if mismatches:
        print(f"\n{len(mismatches)} line-level mismatches found. First 10:")
        for m in mismatches[:10]:
            print(f"  line {m['line']} [{m['layer']}]: {m['log'][:120]!r}")
            print(f"    python: {m['python']}")
            print(f"    wasm:   {m['wasm']}")
        sys.exit(1)
    else:
        print("\nAll lines byte-identical between Python and the compiled Wasm module.")


if __name__ == "__main__":
    main()
