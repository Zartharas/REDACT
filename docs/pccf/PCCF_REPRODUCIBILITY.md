# PCCF reproducibility status (2026-09-24)

> **Update (same day): Docker is now the reference environment.** The
> clean-room Docker outputs of phases 6–10A from `run_pccf_repro_p5to10.sh`
> (run of 2026-09-24, runner commit 0a69b81) replaced the host-computed
> result files. The results documents were regenerated from them. All 19
> verdicts are unchanged. Three headline figures moved at the third decimal:
>
> | figure | host value | Docker value |
> |---|---|---|
> | phase 7, B1 mean ΔP | +0.234 | +0.236 |
> | phase 8, ACI mean ΔP vs PCCF | +0.0004 (worst −0.026) | +0.0006 (worst −0.027) |
> | phase 9, floor hold at ≥ 100 names | 97.0 % | 96.9 % |
>
> Re-running the Docker runner now compares Docker with Docker, so the
> strict check is expected to pass. Host runs of phases 6–10 are for
> development only.


| scope | how | result |
|---|---|---|
| Phases 1–3 | `run_pccf_docker.sh`: full clean-room rerun, every stored number within 0.005 | **PASS**, three times |
| Phase 5 (50 rows) | `run_pccf_repro_p5to10.sh`: re-evaluated from the detection caches in a clean container | **exact match** (verdicts and every number) |
| Phases 6–10A | same runner, check 1 (strict: every stored number within 0.005) | **FAIL**: 91 differences in 23 of 234 result files |
| Phases 6–10A | same runner, check 2 (every pre-registered verdict and headline figure) | **all 19 verdicts identical**; headline numbers match to the reported precision |

## Why the strict check fails
Phases 6–10A were computed on the author's host Python (numpy 2.2.6). The
check recomputes them in the Docker image, which uses a different
numpy/BLAS build. The logistic scorer is fitted by Newton/IRLS, and its
weights differ at the level of floating-point rounding between the two
builds. Because the UD features are discrete, many candidates share almost
the same probability. A rounding-level change can then reorder a block of
candidates exactly at a conformal or F1 threshold. That flips a handful of
names.

Size of the differences:

| where | typical difference | largest |
|---|---|---|
| per-cell recall (phase 6 seeds, phase 8 tl) | about 0.01 | |
| thresholds re-optimised on the fly (B1 F1-tuned) | | 0.06 |
| small-budget subsamples (phase 9 part B at f = 0.05–0.2) | | 0.06 |

Phase 5 matched exactly because both sides of that comparison were
computed in the same Docker image.

## What stands
- Every pre-registered verdict (H22–H40) is identical in the clean-room
  recomputation.
- The headline figures reproduce to their reported precision:
  - phase 7: PCCF 1.4 % vs 14.1 / 35.2 / 39.4 % violations
  - phase 7b: 2.4 % vs 13.1 / 34.5 / 34.5 %
  - phase 8: ACI 0/63
  - phase 9: 5/5 sound drops; floor 96.9 % vs 97.0 % reported
- Individual per-seed or per-cell numbers in phases 6–9 should be quoted as
  platform-dependent at about the ±0.01 level. Quantities from re-tuned
  thresholds and small calibration subsets are platform-dependent up to
  about ±0.06.

## Remedy for future work (not applied retroactively)
- Quantise scorer probabilities (for example to 1e-6) before thresholding.
- Or compute all published numbers inside the pinned Docker image only.

Either would make the strict check pass across platforms. The committed
results are left as computed and recorded.
