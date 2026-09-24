> **Summary (pre-registered in `PCCF_PHASE8_PREREGISTRATION.md`; mechanical verdicts at the end).**
>
> **H35 NOT SUPPORTED: clustering small groups does not recover the fail-open cost.**
> CLUST was identical to PCCF in all 37 rows with a fail-open group. The reason
> is structural. Even when every small group is merged, the cluster never
> reaches 19 true calibration names; the maximum is 16, in ro, and most
> clusters hold 0–8. So the merged cluster still fails open. The precision
> cost comes from groups with *many false and almost no true* candidates, and
> a recall guarantee cannot certify a group that holds almost no positives.
> Cutting those false positives means accepting recall risk. Across the
> originally small groups, pooled recall is:
>
> | method | pooled recall in small groups (n = 342) |
> |---|---|
> | PCCF / CLUST | 1.000 |
> | global fallback | 0.629 |
>
> Fail-open remains the right redaction default. The natural next idea (not
> tested here) is a *precision*-mode decision for such groups: certify that
> a group is almost all noise, then drop it knowingly and log that decision.
>
> **H36 NOT SUPPORTED: weighted conformal (WCP) does not fix the shift.**
> The domain classifier over candidate features does not capture it. The it
> cell improves (0.931 → 0.943), but hi and id_hf get slightly worse
> (0.929, 0.924). This fits a shift in *label-given-features*, which
> covariate reweighting cannot correct.
>
> **H37 SUPPORTED: adaptive conformal (ACI, γ = 0.005) fixes it at no measurable precision cost.**
>
> - **Shifted cells:** 0/63 violations, vs 1/63 for PCCF.
> - **Known-miss cells ({ner} recall):**
>
>   | row | PCCF | ACI |
>   |---|---|---|
>   | it | 0.931 | 0.944 |
>   | hi | 0.932 | 0.949 |
>   | id_hf | 0.930 | 0.944 |
>   | tl_gl | 0.915 | 0.949 |
>
> - **Precision cost:** mean ΔP vs PCCF is +0.0006, and the worst row is −0.027.
>
> **Honest caveats:**
>
> 1. **H37's "≥ 3 of 4 restored" criterion was weak.** Under the combined
>    floor, PCCF itself already met 3 of the 4 cells; only tl_gl missed. The
>    more telling (descriptive, post-hoc) check is the stricter phase-5
>    floor: ACI meets it in **4/4** cells and PCCF in **0/4**.
> 2. **ACI assumes online ground truth** for true candidates, meaning analyst
>    confirmation of redacted names. That is realistic for a SOC review loop,
>    but it is an extra operational requirement.
> 3. **ACI's guarantee is long-run average coverage,** not the finite-sample
>    split-conformal guarantee.

# PCCF phase 8 results: clustered small groups and shift correction

## Part A: CLUST vs fail-open (PCCF) vs global fallback

Rows with at least one fail-open group: 37. CLUST gains ≥ +0.03 P in 0. Cluster-level floors: 52/53 cells hold.

| row | PCCF P/R | CLUST P/R | GLOBAL-FB P/R | small-group recall PCCF / CLUST / GLOBAL-FB |
|---|---|---|---|---|
| fr | 0.874/0.450 | 0.874/0.450 | 0.899/0.448 | 1.00 / 1.00 / 0.89 |
| ru | 0.732/0.390 | 0.732/0.390 | 0.747/0.390 | 1.00 / 1.00 / 1.00 |
| de | 0.951/0.536 | 0.951/0.536 | 0.970/0.534 | 1.00 / 1.00 / 0.50 |
| it | 0.897/0.436 | 0.897/0.436 | 0.927/0.434 | 1.00 / 1.00 / 0.86 |
| nl | 0.895/0.396 | 0.895/0.396 | 0.921/0.394 | 1.00 / 1.00 / 0.83 |
| pt | 0.619/0.393 | 0.619/0.393 | 0.911/0.390 | 1.00 / 1.00 / 0.55 |
| fi | 0.855/0.346 | 0.855/0.346 | 0.926/0.344 | 1.00 / 1.00 / 0.62 |
| hi | 0.849/0.736 | 0.849/0.736 | 0.910/0.728 | 1.00 / 1.00 / 0.00 |
| te | 0.827/0.637 | 0.827/0.637 | 0.851/0.628 | 1.00 / 1.00 / 0.11 |
| ar_wiki | 0.546/0.819 | 0.546/0.819 | 0.583/0.806 | 1.00 / 1.00 / 0.06 |
| in | 0.489/0.564 | 0.489/0.564 | 0.945/0.562 | 1.00 / 1.00 / 0.00 |
| ko_kdpii | 0.437/0.455 | 0.437/0.455 | 0.437/0.455 | 1.00 / 1.00 / 1.00 |
| ko_klue | 0.901/0.790 | 0.901/0.790 | 0.901/0.790 | 1.00 / 1.00 / 1.00 |
| bg | 0.607/0.429 | 0.607/0.429 | 0.638/0.429 | – / – / – |
| pl | 0.859/0.454 | 0.859/0.454 | 0.868/0.454 | 1.00 / 1.00 / 1.00 |
| cs | 0.495/0.424 | 0.495/0.424 | 0.498/0.423 | 1.00 / 1.00 / 0.67 |
| lt | 0.833/0.393 | 0.833/0.393 | 0.833/0.393 | 1.00 / 1.00 / 1.00 |
| et | 0.532/0.395 | 0.532/0.395 | 0.541/0.394 | 1.00 / 1.00 / 0.60 |
| sv | 0.814/0.338 | 0.814/0.338 | 0.889/0.336 | 1.00 / 1.00 / 0.00 |
| sk | 0.504/0.405 | 0.504/0.405 | 0.526/0.403 | 1.00 / 1.00 / 0.38 |
| lv | 0.471/0.412 | 0.471/0.412 | 0.471/0.412 | 1.00 / 1.00 / 1.00 |
| hu | 0.450/0.420 | 0.450/0.420 | 0.481/0.420 | – / – / – |
| ro | 0.672/0.344 | 0.672/0.344 | 0.886/0.343 | 1.00 / 1.00 / 0.86 |
| el | 0.730/0.278 | 0.730/0.278 | 0.928/0.278 | – / – / – |
| da | 0.845/0.363 | 0.845/0.363 | 0.931/0.361 | 1.00 / 1.00 / 0.64 |
| sl | 0.915/0.418 | 0.915/0.418 | 0.926/0.418 | 1.00 / 1.00 / 1.00 |
| hr | 0.840/0.454 | 0.840/0.454 | 0.871/0.452 | 1.00 / 1.00 / 0.69 |
| sr | 0.396/0.378 | 0.396/0.378 | 0.395/0.377 | 1.00 / 1.00 / 0.86 |
| bg_gl | 0.863/0.576 | 0.863/0.576 | 0.911/0.576 | – / – / – |
| cs_gl | 0.927/0.585 | 0.927/0.585 | 0.943/0.584 | 1.00 / 1.00 / 0.67 |
| et_gl | 0.904/0.556 | 0.904/0.556 | 0.925/0.555 | 1.00 / 1.00 / 0.80 |
| sk_gl | 0.867/0.571 | 0.867/0.571 | 0.917/0.571 | 1.00 / 1.00 / 1.00 |
| lv_gl | 0.929/0.582 | 0.929/0.582 | 0.929/0.582 | 1.00 / 1.00 / 1.00 |
| hu_gl | 0.818/0.530 | 0.818/0.530 | 0.905/0.530 | – / – / – |
| sr_gl | 0.913/0.580 | 0.913/0.580 | 0.919/0.580 | 1.00 / 1.00 / 1.00 |
| vi_gl | 0.738/0.546 | 0.738/0.546 | 0.889/0.541 | 1.00 / 1.00 / 0.00 |
| ms_gl | 0.867/0.568 | 0.867/0.568 | 0.941/0.565 | 1.00 / 1.00 / 0.00 |

Pooled recall inside originally-small groups: PCCF 1.000 (n=342), CLUST 1.000 (n=342), GLOBAL-FB 0.629 (n=342)

## Part B: shift correction on shifted rows (original protocol)

| method | violations / shifted cells | known misses restored (of 4) | mean ΔP vs PCCF | mean ΔR vs PCCF |
|---|---|---|---|---|
| PCCF | 1/63 | 3 | +0.000 | +0.000 |
| WCP | 2/63 | 2 | +0.000 | +0.000 |
| ACI | 0/63 | 4 | +0.001 | -0.000 |

Known-miss cells ({ner} recall): hi: PCCF 0.932, WCP 0.929, ACI 0.949; id_hf: PCCF 0.930, WCP 0.924, ACI 0.944; it: PCCF 0.931, WCP 0.943, ACI 0.944; tl_gl: PCCF 0.915, WCP 0.915, ACI 0.949

=== Verdicts (mechanical) ===
  H35: NOT SUPPORTED
  H36: NOT SUPPORTED
  H37: SUPPORTED
