"""Compare two directories of pccf_*_results.json files (T6).
PASS = every boolean verdict identical and every float within +/-0.005.
Integer count differences are listed but do not fail the check on their
own, because their effect shows up in the float metrics."""
import glob
import json
import os
import sys

TOL = 0.005


def walk(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out["missing"].append(f"{path}/{k}")
            else:
                walk(a[k], b[k], f"{path}/{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out["missing"].append(f"{path} (length {len(a)} vs {len(b)})")
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{path}[{i}]", out)
    elif isinstance(a, bool) or isinstance(b, bool):
        if a != b:
            out["verdict"].append(f"{path}: {a} vs {b}")
    elif isinstance(a, int) and isinstance(b, int):
        if a != b:
            out["count"].append(f"{path}: {a} vs {b}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a - b) > TOL:
            out["float"].append(f"{path}: {a:.4f} vs {b:.4f}")
    elif a != b:
        out["other"].append(f"{path}: {str(a)[:60]} vs {str(b)[:60]}")


def main(ref, new):
    out = {"verdict": [], "float": [], "count": [], "missing": [], "other": []}
    files = sorted(glob.glob(os.path.join(ref, "pccf_*_results.json")))
    for f in files:
        g = os.path.join(new, os.path.basename(f))
        if not os.path.exists(g):
            out["missing"].append(os.path.basename(f))
            continue
        walk(json.load(open(f)), json.load(open(g)), os.path.basename(f), out)
    print(f"compared {len(files)} result files")
    for k, v in out.items():
        print(f"{k}: {len(v)}")
        for x in v[:25]:
            print("   ", x)
    ok = not out["verdict"] and not out["float"] and not out["missing"]
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
