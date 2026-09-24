# PCCF phase 10B: throughput and cost per layer (descriptive; author's Docker run, 2026-09-24)

Pre-registered as descriptive in `PCCF_PHASE10_PREREGISTRATION.md`.

- **Setup:** 2,000 T7 telemetry lines, single process, **single thread**
  (OMP/torch = 1), in the `redact-pccf-multilang` image on the author's Mac.
- **Raw output:** `validation/real_data/phase10/throughput/`.

| layer | lines/s | ms/line | vCPU-s per 1M lines | USD per 1M lines* |
|---|---|---|---|---|
| regex / flattened dictionary (detect.scan_flattened) | 98542.9 | 0.01 | 10 | 0.00 |
| spaCy en_core_web_lg NER via Presidio (detect.scan_ner) | 145.1 | 6.89 | 6,890 | 0.08 |
| spaCy NER + beam confidence (as used by PCCF) | 123.7 | 8.08 | 8,085 | 0.09 |
| GLiNER multi v2.1 (zero-shot, threshold 0.3) | 2.9 | 348.75 | 348,752 | 3.88 |
| XLM-R token classification (Davlan/xlm-roberta-base-ner-hrl) | 8.5 | 117.82 | 117,818 | 1.31 |
| PCCF filter (candidates + KV features + rule score + threshold) | 509106.4 | 0.00 | 2 | 0.00 |

\*At an assumed $0.04 per vCPU-hour; an illustration only.

**Reading:**

1. **The PCCF filter itself is essentially free.** Candidate building, KV
   features, rule scoring and the threshold together cost about 2
   vCPU-seconds per million lines. That is under 0.03 % of the spaCy NER
   layer it filters.
2. **Detection dominates cost.** spaCy `en_core_web_lg` (via Presidio)
   handles about 145 lines/s per thread, or about 124 lines/s with the beam
   confidence PCCF uses. That is roughly $0.09 per million lines at the
   assumed price.
3. **Transformer NER is 17–50× slower per thread.** XLM-R runs at 8.5
   lines/s and GLiNER at 2.9 lines/s (about $1.31 and $3.88 per million
   lines). The GLiNER quality gain (amendment 7) therefore has a large
   throughput price. A practical deployment would use it for languages with
   no usable spaCy model, not everywhere.
4. **Regex and dictionary layers** run at about 98k lines/s, which is
   negligible.

**Caveats:**

- Single-thread numbers on one laptop CPU inside Docker Desktop. Absolute
  rates vary by host, and multi-threading and batching would raise
  transformer throughput.
- The comparison is the useful part, not the absolute rates.
