> **Summary (pre-registered in `PCCF_PHASE9_PREREGISTRATION.md`).**
>
> **H38 NOT SUPPORTED. Part (c) failed; (a) and (b) held.**
>
> - **(a) The certificate is sound.** All 5/5 dropped groups really were
>   noise on test (0/155, 0/150, 3/564, 2/457, 13/338 true).
> - **(b) Recall cost is negligible:** −0.0018 on average.
> - **(c) Coverage of the gain fails.** The certificate fires in only 5 of
>   the 36 rows with a fail-open group; 9 were needed. Most small groups are
>   too small (< 30 calibration candidates) or too uncertain to certify as
>   noise.
>
> Where it does fire, it removes most of the fail-open cost, with recall
> unchanged to ≤ 0.005:
>
> | row | PCCF precision | NOISE-DROP precision |
> |---|---|---|
> | pt | 0.619 | 0.893 |
> | in | 0.489 | 0.945 |
> | el | 0.730 | 0.883 |
> | hu_gl | 0.818 | 0.901 |
> | vi_gl | 0.738 | 0.889 |
>
> These are the rows where fail-open hurt most (phase 7). Recommendation: an
> opt-in, logged precision mode. It is honest about its limited reach; ro,
> for example, is not certified.
>
> **H39 SUPPORTED: the labelling budget.**
>
> - **Floor:** it holds in 96.9 % of draws once a group has ≥ 100 true
>   calibration names. Because the combined floor already accounts for
>   calibration size, it holds at about 97 % in every bin, even 19–49.
> - **Tightness improves with more labels.** The 5th-percentile recall rises
>   with n_cal_true:
>
>   | true calibration names | 5th-percentile recall |
>   |---|---|
>   | 19–49 | 0.886 |
>   | 100–199 | 0.916 |
>   | 400+ | 0.932 |
>
> - **Gain:** 34 of 38 rows reach ≥ 80 % of their full precision gain once
>   the main group has about 100 true names.
> - **Exceptions:** tr, tr_mit, cs and lv. In tr/tr_mit a *second* large
>   group ({dict}) also needs labels before it stops failing open.
>
> **Practitioner rule:** label about 100 true names per active layer-agreement
> group; 400+ if you want the worst-case recall draw above 0.93.

# PCCF phase 9 results: certified noise-group dropping and labelling budget

## Part A: NOISE-DROP

Rows with a fail-open group: 36; drop decisions: 5 in 5 rows.
(a) dropped groups with test precision ≤ 0.10: 5/5
(b) mean recall change vs PCCF over affected rows: -0.0018
(c) rows with ≥ +0.03 precision gain: 5/36

| row | dropped group(s) (cal n / true; CP upper) | test precision of dropped | PCCF P/R | NOISE-DROP P/R |
|---|---|---|---|---|
| el | {dict} 99/0; u=0.037 | 0/155 | 0.730/0.278 | 0.883/0.278 |
| hu_gl | {dict} 83/0; u=0.043 | 0/150 | 0.818/0.530 | 0.901/0.530 |
| in | {dict} 310/1; u=0.015 | 3/564 | 0.489/0.564 | 0.945/0.562 |
| pt | {dict} 320/3; u=0.027 | 2/457 | 0.619/0.393 | 0.893/0.393 |
| vi_gl | {dict} 184/2; u=0.034 | 13/338 | 0.738/0.546 | 0.889/0.541 |

## Part B: labelling budget (pooled splits, seeds 1-10)

| true calibration names in the group | draws | floor holds | mean recall | recall 5th pct |
|---|---|---|---|---|
| 19–49 | 932 | 97.2% | 0.964 | 0.886 |
| 50–99 | 692 | 97.4% | 0.961 | 0.908 |
| 100–199 | 578 | 96.9% | 0.955 | 0.916 |
| 200–399 | 460 | 96.5% | 0.954 | 0.925 |
| 400–+ | 443 | 97.3% | 0.953 | 0.932 |

| row | full-calibration ΔP (median) | smallest f with main group ≥ 100 true | ΔP there (median) | ratio |
|---|---|---|---|---|
| fr | +0.133 | 0.4 | +0.129 | 0.97 |
| it | +0.096 | 0.4 | +0.094 | 0.98 |
| nl | +0.073 | 0.4 | +0.077 | 1.07 |
| pt | +0.113 | 0.2 | +0.110 | 0.98 |
| fi | +0.187 | 0.2 | +0.185 | 0.99 |
| tr | +0.175 | 0.1 | +0.040 | 0.23 |
| ar_wiki | +0.033 | 0.4 | +0.034 | 1.03 |
| ja | +0.084 | 0.4 | +0.078 | 0.92 |
| id_hf | +0.050 | 0.2 | +0.045 | 0.91 |
| ko | +0.128 | 0.4 | +0.131 | 1.02 |
| zh | +0.114 | 0.4 | +0.111 | 0.97 |
| in | +0.257 | 0.4 | +0.258 | 1.00 |
| tr_mit | +0.156 | 0.05 | +0.043 | 0.28 |
| bg | +0.105 | 0.2 | +0.104 | 1.00 |
| pl | +0.212 | 0.2 | +0.207 | 0.97 |
| cs | +0.099 | 0.2 | +0.071 | 0.71 |
| lt | +0.164 | 0.2 | +0.145 | 0.88 |
| et | +0.107 | 0.2 | +0.100 | 0.94 |
| sv | +0.108 | 0.4 | +0.089 | 0.83 |
| sk | +0.099 | 0.2 | +0.096 | 0.98 |
| lv | +0.100 | 0.2 | +0.079 | 0.79 |
| hu | +0.147 | 0.2 | +0.118 | 0.80 |
| ro | +0.247 | 0.4 | +0.245 | 0.99 |
| el | +0.109 | 0.4 | +0.110 | 1.00 |
| da | +0.151 | 0.2 | +0.161 | 1.06 |
| sl | +0.211 | 0.2 | +0.202 | 0.96 |
| hr | +0.216 | 0.2 | +0.204 | 0.94 |
| sr | +0.062 | 0.2 | +0.072 | 1.16 |
| bg_gl | +0.154 | 0.2 | +0.164 | 1.06 |
| cs_gl | +0.146 | 0.2 | +0.136 | 0.93 |
| et_gl | +0.160 | 0.2 | +0.159 | 1.00 |
| sk_gl | +0.129 | 0.2 | +0.142 | 1.11 |
| lv_gl | +0.169 | 0.2 | +0.174 | 1.03 |
| hu_gl | +0.132 | 0.2 | +0.133 | 1.01 |
| sr_gl | +0.136 | 0.2 | +0.136 | 1.00 |
| vi_gl | +0.100 | 0.2 | +0.092 | 0.92 |
| ms_gl | +0.194 | 0.2 | +0.175 | 0.91 |
| tl_gl | +0.085 | 0.2 | +0.077 | 0.91 |

Floor holds for n_cal_true ≥ 100: 1435/1481 (96.9%); rows reaching ≥ 80 % of full gain at the ≥ 100-name budget: 34/38

=== Verdicts (mechanical) ===
  H38: NOT SUPPORTED (a=True, b=True, c=False)
  H39: SUPPORTED
