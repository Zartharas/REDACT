# PCCF phase 10: SOC operations: an analyst-feedback loop and the throughput cost (pre-registration)

Written 2026-09-24, **before any phase-10 number was computed.**

## Part A: an adaptive threshold with realistic analyst feedback (log telemetry)
- **Data:** the phase-3b T7 telemetry corpora (5 log sources × 3 seeds;
  names in fields *and* message text; the held-out source uses template
  set B), from the existing cache.
- **Scorer:** the pre-registered S2 configuration (KV rule, mask × key
  groups). The static baseline is S2 calibrated on the other sources, as in
  phase 3b.
- **Streaming:** each held-out source is processed as a stream in line order.
- **ACI:** per group, α starts at 0.05 and γ = 0.005. It updates only from
  *audited* lines.
  - **Audit rate ρ:** each line is audited with probability ρ (a fixed seed).
    An audited line reveals every true name in it: redacted ones are
    confirmed, missed ones are noticed.
  - **Feedback delay:** the update arrives after D lines.
  - **Settings:** (ρ, D) ∈ {(1.0, 0), (0.1, 0), (0.1, 500)}.
- **Cells:** (held-out source, seed, group) with n_test_true ≥ 19.
- **Floor:** the phase-6 combined floor, with n_cal from the static
  calibration set.

**H40.** At (ρ = 0.1, D = 500), ACI has fewer floor violations than static
S2 across the cells, AND its pooled precision is ≥ static precision − 0.02.
Descriptive: all three settings, recall in the first vs second half of each
stream, and recall by location (field / message).

## Part B: throughput and cost (descriptive; the author's Docker run)
- **What:** lines per second on 2,000 T7 telemetry lines, single process,
  CPU, in the `redact-pccf-multilang` image, for each layer:
  - regex/flattened dictionary
  - spaCy `en_core_web_lg` NER
  - GLiNER multi v2.1
  - XLM-R token-classification (Davlan)
  - the PCCF filter (candidate building + KV scoring + threshold)
- **Cost:** reported per million lines as vCPU-seconds. The dollar figure
  uses a stated, adjustable assumption of **$0.04 per vCPU-hour**. It is an
  illustration only.
- **Hypothesis:** none. This part is descriptive.
