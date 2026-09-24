"""PCCF phase 10 part B (descriptive): per-layer throughput on T7 telemetry lines.
Single process, single thread (OMP/torch threads = 1) so that vCPU-seconds are comparable.
Run inside redact-pccf-multilang via run_phase10_throughput.sh. Writes throughput.json/.md to --out.
"""
import argparse, json, os, sys, time

os.environ.setdefault("OMP_NUM_THREADS", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
_argv, sys.argv = sys.argv, [sys.argv[0]]
import pccf_phase3_telemetry as P3  # noqa: E402
sys.argv = _argv
import pccf  # noqa: E402
import pccf_context as ctx  # noqa: E402

USD_PER_VCPU_HOUR = 0.04  # stated assumption (adjustable); illustration only


def lines(n):
    corp, _ = P3.corpora(42)
    out = [e["log"] for ds in sorted(corp) for e in corp[ds]]
    return out[:n]


def bench(name, fn, data, warm=20):
    for t in data[:warm]:
        fn(t)
    t0 = time.perf_counter()
    for t in data:
        fn(t)
    dt = time.perf_counter() - t0
    return {"layer": name, "lines": len(data), "seconds": dt, "lines_per_s": len(data) / dt,
            "ms_per_line": 1000 * dt / len(data), "vcpu_s_per_million": 1e6 * dt / len(data),
            "usd_per_million": 1e6 * dt / len(data) / 3600 * USD_PER_VCPU_HOUR}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import torch
    torch.set_num_threads(1)
    import detect
    import ner_confidence
    import lang_layers as LL
    data = lines(a.n)
    res = []
    res.append(bench("regex / flattened dictionary (detect.scan_flattened)", detect.scan_flattened, data))
    res.append(bench("spaCy en_core_web_lg NER via Presidio (detect.scan_ner)", detect.scan_ner, data))

    def ner_beam(t):
        h = [x for x in detect.scan_ner(t) if x["type"] == "PERSON"]
        return ner_confidence.annotate(h, t, "en_core_web_lg", "PERSON") if h else []
    res.append(bench("spaCy NER + beam confidence (as used by PCCF)", ner_beam, data))
    res.append(bench("GLiNER multi v2.1 (zero-shot, threshold 0.3)", LL.scan_ner_gliner, data))
    res.append(bench("XLM-R token classification (Davlan/xlm-roberta-base-ner-hrl)",
                     lambda t: LL.scan_ner_hf("hi", t), data))
    pre = {t: {"ner": [dict(x, confidence=0.9) for x in detect.scan_ner(t) if x["type"] == "PERSON"],
               "flat": [x for x in detect.scan_flattened(t) if x["type"] == "PERSON"]} for t in data}
    model = pccf.PCCF(mode="coverage", alpha=0.05)
    model.thresholds = {}  # threshold lookup cost only; unseen groups fail open (same code path)

    def filt(t):
        cands = pccf.build_candidates(pre[t], "PERSON")
        if cands:
            ctx.apply_scores(cands, ctx.KVRuleScorer().predict([ctx.kv_features(t, c) for c in cands]))
        return [c for c in cands if model.accept(c)]
    res.append(bench("PCCF filter (candidates + KV features + rule score + threshold)", filt, data))
    os.makedirs(a.out, exist_ok=True)
    json.dump({"assumption_usd_per_vcpu_hour": USD_PER_VCPU_HOUR, "threads": 1, "results": res},
              open(os.path.join(a.out, "throughput.json"), "w"), indent=1)
    md = ["| layer | lines/s | ms/line | vCPU-s per 1M lines | USD per 1M lines* |", "|---|---|---|---|---|"]
    md += [f"| {r['layer']} | {r['lines_per_s']:.1f} | {r['ms_per_line']:.2f} | {r['vcpu_s_per_million']:,.0f} | "
           f"{r['usd_per_million']:.2f} |" for r in res]
    md.append(f"\n*Assumes ${USD_PER_VCPU_HOUR}/vCPU-hour, single thread, {a.n} T7 telemetry lines. Illustration only.")
    open(os.path.join(a.out, "throughput.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
