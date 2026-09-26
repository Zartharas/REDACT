"""Compare recomputed PCCF result JSON files against the committed ones.
Floats must agree within --tol (default 0.005); booleans, strings, ints and
structure must match exactly. Usage: compare_repro_json.py <committed_root> <recomputed_root> <relpath>...
"""
import json, os, sys

TOL = 0.005


def cmp(a, b, path, bad):
    if isinstance(a, bool) or isinstance(b, bool):
        if a != b:
            bad.append((path, a, b))
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            if a != b:
                bad.append((path, a, b))
        elif abs(float(a) - float(b)) > TOL:
            bad.append((path, a, b))
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                bad.append((f"{path}/{k}", "missing" if k not in a else "present", "missing" if k not in b else "present"))
            else:
                cmp(a[k], b[k], f"{path}/{k}", bad)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            bad.append((path, f"len {len(a)}", f"len {len(b)}"))
        for i, (x, y) in enumerate(zip(a, b)):
            cmp(x, y, f"{path}[{i}]", bad)
    elif a != b:
        bad.append((path, a, b))


def main():
    ref, new, rels = sys.argv[1], sys.argv[2], sys.argv[3:]
    total = 0
    for rel in rels:
        bad = []
        pa, pb = os.path.join(ref, rel), os.path.join(new, rel)
        if not os.path.exists(pb):
            print(f"MISSING {rel}"); total += 1; continue
        cmp(json.load(open(pa)), json.load(open(pb)), rel, bad)
        total += len(bad)
        print(f"{'ok  ' if not bad else 'DIFF'} {rel}" + (f"  ({len(bad)} differences; first: {bad[0]})" if bad else ""))
    print(f"compared {len(rels)} files, {total} differences\n{'PASS' if total == 0 else 'FAIL'}")
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    main()
