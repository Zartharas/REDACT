# PCCF phase 6 results (split-shift vs chance)

## H25: validity under exchangeable (E1) re-splits, 50 seeds

| lang | scorer | group | mean n_cal / n_test | mean recall | seed SD | floor (0.95 − 2·SE) | ok |
|---|---|---|---|---|---|---|---|
| fr | LR-UD | {dict+ner} | 14 / 25 | 0.997 | 0.020 | 0.944 | ok |
| fr | LR-UD | {ner} | 413 / 824 | 0.950 | 0.015 | 0.946 | ok |
| fr | rule | {dict+ner} | 14 / 25 | 1.000 | 0.000 | 0.950 | ok |
| fr | rule | {ner} | 413 / 824 | 1.000 | 0.000 | 0.950 | ok |
| ru | LR-UD | {dict} | 11 / 22 | 1.000 | 0.000 | 0.950 | ok |
| ru | LR-UD | {dict+ner} | 86 / 168 | 0.957 | 0.026 | 0.943 | ok |
| ru | LR-UD | {ner} | 35 / 69 | 0.967 | 0.042 | 0.938 | ok |
| ru | rule | {dict} | 11 / 22 | 1.000 | 0.000 | 0.950 | ok |
| ru | rule | {dict+ner} | 86 / 168 | 0.971 | 0.021 | 0.944 | ok |
| ru | rule | {ner} | 35 / 69 | 0.988 | 0.023 | 0.943 | ok |
| de | LR-UD | {dict+ner} | 30 / 60 | 0.972 | 0.029 | 0.942 | ok |
| de | LR-UD | {ner} | 491 / 979 | 0.950 | 0.012 | 0.947 | ok |
| de | rule | {dict+ner} | 30 / 60 | 0.996 | 0.027 | 0.942 | ok |
| de | rule | {ner} | 491 / 979 | 1.000 | 0.000 | 0.950 | ok |
| it | LR-UD | {ner} | 423 / 852 | 0.951 | 0.013 | 0.946 | ok |
| it | rule | {ner} | 423 / 852 | 1.000 | 0.000 | 0.950 | ok |
| nl | LR-UD | {dict+ner} | 11 / 21 | 1.000 | 0.000 | 0.950 | ok |
| nl | LR-UD | {ner} | 382 / 756 | 0.953 | 0.012 | 0.946 | ok |
| nl | rule | {dict+ner} | 11 / 21 | 1.000 | 0.000 | 0.950 | ok |
| nl | rule | {ner} | 382 / 756 | 1.000 | 0.000 | 0.950 | ok |
| pt | LR-UD | {dict+ner} | 9 / 20 | 1.000 | 0.000 | 0.950 | ok |
| pt | LR-UD | {ner} | 556 / 1109 | 0.952 | 0.012 | 0.947 | ok |
| pt | rule | {dict+ner} | 9 / 20 | 1.000 | 0.000 | 0.950 | ok |
| pt | rule | {ner} | 556 / 1109 | 0.970 | 0.024 | 0.943 | ok |
| fi | LR-UD | {ner} | 496 / 995 | 0.952 | 0.010 | 0.947 | ok |
| fi | rule | {ner} | 496 / 995 | 0.972 | 0.009 | 0.947 | ok |
| tr | LR-UD | {dict} | 72 / 143 | 0.970 | 0.025 | 0.943 | ok |
| tr | LR-UD | {dict+ner} | 483 / 975 | 0.952 | 0.013 | 0.946 | ok |
| tr | LR-UD | {ner} | 1450 / 2899 | 0.950 | 0.009 | 0.947 | ok |
| tr | rule | {dict} | 72 / 143 | 0.993 | 0.007 | 0.948 | ok |
| tr | rule | {dict+ner} | 483 / 975 | 0.990 | 0.004 | 0.949 | ok |
| tr | rule | {ner} | 1450 / 2899 | 0.982 | 0.002 | 0.949 | ok |
| hi | LR-UD | {dict+ner} | 100 / 197 | 0.968 | 0.017 | 0.945 | ok |
| hi | LR-UD | {ner} | 635 / 1274 | 0.951 | 0.010 | 0.947 | ok |
| hi | rule | {dict+ner} | 100 / 197 | 1.000 | 0.000 | 0.950 | ok |
| hi | rule | {ner} | 635 / 1274 | 1.000 | 0.000 | 0.950 | ok |
| te | LR-UD | {dict} | 9 / 20 | 1.000 | 0.000 | 0.950 | ok |
| te | LR-UD | {dict+ner} | 90 / 178 | 0.962 | 0.022 | 0.944 | ok |
| te | LR-UD | {ner} | 732 / 1435 | 0.954 | 0.009 | 0.947 | ok |
| te | rule | {dict} | 9 / 20 | 1.000 | 0.000 | 0.950 | ok |
| te | rule | {dict+ner} | 90 / 178 | 1.000 | 0.000 | 0.950 | ok |
| te | rule | {ner} | 732 / 1435 | 1.000 | 0.000 | 0.950 | ok |
| ar_wiki | LR-UD | {dict+ner} | 121 / 244 | 0.956 | 0.022 | 0.944 | ok |
| ar_wiki | LR-UD | {ner} | 272 / 536 | 0.949 | 0.021 | 0.944 | ok |
| ar_wiki | rule | {dict+ner} | 121 / 244 | 1.000 | 0.000 | 0.950 | ok |
| ar_wiki | rule | {ner} | 272 / 536 | 0.999 | 0.001 | 0.950 | ok |
| ja | LR-UD | {dict} | 66 / 134 | 0.961 | 0.029 | 0.942 | ok |
| ja | LR-UD | {dict+ner} | 220 / 450 | 0.953 | 0.017 | 0.945 | ok |
| ja | LR-UD | {ner} | 345 / 693 | 0.953 | 0.012 | 0.947 | ok |
| ja | rule | {dict} | 66 / 134 | 1.000 | 0.000 | 0.950 | ok |
| ja | rule | {dict+ner} | 220 / 450 | 0.985 | 0.026 | 0.943 | ok |
| ja | rule | {ner} | 345 / 693 | 0.970 | 0.006 | 0.948 | ok |
| id | LR-UD | {dict} | 82 / 162 | 1.000 | 0.000 | 0.950 | ok |
| id | LR-UD | {dict+ner} | 233 / 472 | 0.960 | 0.017 | 0.945 | ok |
| id | LR-UD | {ner} | 370 / 736 | 0.952 | 0.013 | 0.946 | ok |
| id | rule | {dict} | 82 / 162 | 1.000 | 0.000 | 0.950 | ok |
| id | rule | {dict+ner} | 233 / 472 | 1.000 | 0.000 | 0.950 | ok |
| id | rule | {ner} | 370 / 736 | 1.000 | 0.000 | 0.950 | ok |
| id_hf | LR-UD | {dict} | 20 / 41 | 0.989 | 0.046 | 0.937 | ok |
| id_hf | LR-UD | {dict+ner} | 294 / 591 | 0.953 | 0.015 | 0.946 | ok |
| id_hf | LR-UD | {ner} | 527 / 1049 | 0.948 | 0.016 | 0.945 | ok |
| id_hf | rule | {dict} | 20 / 41 | 1.000 | 0.000 | 0.950 | ok |
| id_hf | rule | {dict+ner} | 294 / 591 | 1.000 | 0.000 | 0.950 | ok |
| id_hf | rule | {ner} | 527 / 1049 | 1.000 | 0.000 | 0.950 | ok |
| ko | LR-UD | {dict} | 25 / 52 | 0.955 | 0.055 | 0.934 | ok |
| ko | LR-UD | {dict+ner} | 66 / 133 | 0.957 | 0.032 | 0.941 | ok |
| ko | LR-UD | {ner} | 273 / 535 | 0.953 | 0.018 | 0.945 | ok |
| ko | rule | {dict} | 25 / 52 | 0.975 | 0.034 | 0.940 | ok |
| ko | rule | {dict+ner} | 66 / 133 | 0.995 | 0.036 | 0.940 | ok |
| ko | rule | {ner} | 273 / 535 | 1.000 | 0.000 | 0.950 | ok |
| zh | LR-UD | {dict} | 373 / 757 | 0.954 | 0.013 | 0.946 | ok |
| zh | LR-UD | {dict+ner} | 419 / 828 | 0.953 | 0.014 | 0.946 | ok |
| zh | LR-UD | {ner} | 197 / 395 | 0.956 | 0.020 | 0.944 | ok |
| zh | rule | {dict} | 373 / 757 | 1.000 | 0.000 | 0.950 | ok |
| zh | rule | {dict+ner} | 419 / 828 | 1.000 | 0.000 | 0.950 | ok |
| zh | rule | {ner} | 197 / 395 | 1.000 | 0.000 | 0.950 | ok |
| in | LR-UD | {dict+ner} | 16 / 32 | 0.974 | 0.060 | 0.933 | ok |
| in | LR-UD | {ner} | 282 / 565 | 0.951 | 0.012 | 0.946 | ok |
| in | rule | {dict+ner} | 16 / 32 | 1.000 | 0.000 | 0.950 | ok |
| in | rule | {ner} | 282 / 565 | 0.978 | 0.019 | 0.945 | ok |
| ko_kdpii | LR-UD | {dict+ner} | 25 / 50 | 0.969 | 0.038 | 0.939 | ok |
| ko_kdpii | LR-UD | {ner} | 419 / 835 | 0.953 | 0.012 | 0.946 | ok |
| ko_kdpii | rule | {dict+ner} | 25 / 50 | 1.000 | 0.000 | 0.950 | ok |
| ko_kdpii | rule | {ner} | 419 / 835 | 0.970 | 0.018 | 0.945 | ok |

**H25 (LR-UD): SUPPORTED**; rule-scorer control: all ok

## H26: split shift (E2 original protocol vs E1 pooled), LR-UD {ner}

| lang | shifted split | E1 mean ± SE | E2 mean ± SE | E2 − E1 | beyond 2 SE |
|---|---|---|---|---|---|
| fr | yes | 0.950 ± 0.002 | 0.952 ± 0.001 | +0.003 | no |
| ru | no | 0.967 ± 0.006 | 0.967 ± 0.006 | +0.000 | no |
| de | yes | 0.950 ± 0.002 | 0.959 ± 0.001 | +0.009 | no |
| it | yes | 0.951 ± 0.002 | 0.929 ± 0.002 | -0.023 | **yes** |
| nl | yes | 0.953 ± 0.002 | 0.950 ± 0.001 | -0.003 | no |
| pt | yes | 0.952 ± 0.002 | 0.951 ± 0.001 | -0.001 | no |
| fi | yes | 0.952 ± 0.001 | 0.956 ± 0.001 | +0.004 | no |
| tr | no | 0.950 ± 0.001 | 0.950 ± 0.001 | +0.000 | no |
| hi | yes | 0.951 ± 0.001 | 0.944 ± 0.001 | -0.007 | **yes** |
| te | yes | 0.954 ± 0.001 | 0.955 ± 0.001 | +0.001 | no |
| ar_wiki | yes | 0.949 ± 0.003 | 0.938 ± 0.002 | -0.012 | **yes** |
| ja | yes | 0.953 ± 0.002 | 0.953 ± 0.001 | -0.000 | no |
| id | yes | 0.952 ± 0.002 | 0.967 ± 0.001 | +0.016 | no |
| id_hf | yes | 0.948 ± 0.002 | 0.940 ± 0.001 | -0.008 | **yes** |
| ko | no | 0.953 ± 0.003 | 0.953 ± 0.003 | +0.000 | no |
| zh | no | 0.956 ± 0.003 | 0.956 ± 0.003 | +0.000 | no |
| in | no | 0.951 ± 0.002 | 0.951 ± 0.002 | +0.000 | no |
| ko_kdpii | yes | 0.953 ± 0.002 | 0.951 ± 0.002 | -0.002 | no |

**H26: SUPPORTED** (3/3 of it, hi, id_hf lower beyond 2 SE)

## H27: was the phase-5 tolerance too tight? (E1, LR-UD, all eligible groups × 50 seeds)

checks 2100; observed miss rate 0.084; nominal 0.023; combined-variance prediction 0.125 (± 0.014)

**H27: SUPPORTED**

## Sensitivity only (NOT a re-scoring of phase 5): phase-5 misses under the combined-variance floor

- ru {ner}: recall 0.859; combined floor 0.860 (n_cal≈35, n_test=71) → still misses
- it {ner}: recall 0.931; combined floor 0.924 (n_cal≈499, n_test=698) → would pass
- hi {ner}: recall 0.932; combined floor 0.929 (n_cal≈704, n_test=1146) → would pass
- id_hf {ner}: recall 0.930; combined floor 0.927 (n_cal≈596, n_test=896) → would pass

=== Verdicts (mechanical) ===
  H25: SUPPORTED
  H26: SUPPORTED
  H27: SUPPORTED
