# PCCF phase 11 results: real documents

## H41 / H42 (phase-5 harness; combined floor)

| row | union P / R | rule ΔP (floor) | LR-UD ΔP / ΔR (floor) | status |
|---|---|---|---|---|
| en_tab_redact | 0.891 / 0.817 | +0.000 (ok) | +0.068 / -0.031 (ok) | useful+valid |
| en_tab_spacy | 0.225 / 0.851 | +0.004 (ok) | +0.096 / -0.027 (ok) | useful+valid |
| en_btc_redact | 0.714 / 0.163 | +0.000 (ok) | +0.046 / -0.006 (ok) | useful+valid |
| en_btc_spacy | 0.327 / 0.172 | +0.003 (ok) | +0.089 / -0.010 (ok) | useful+valid |
| en_wnut_redact | 0.736 / 0.623 | +0.000 (ok) | +0.043 / -0.067 (miss) | floor miss |
| en_wnut_spacy | 0.477 / 0.731 | -0.000 (ok) | +0.106 / -0.055 (ok) | useful+valid |
| de_germeval | 0.744 / 0.881 | -0.002 (ok) | +0.042 / -0.031 (ok) | useful+valid |
| ru_factrueval | 0.946 / 0.583 | +0.006 (ok) | +0.012 / -0.036 (ok) | gain < 0.03 |
| ar_anercorp | 0.632 / 0.891 | -0.002 (ok) | +0.017 / -0.076 (miss) | floor miss |
| en_enron_redact | 0.468 / 0.384 | +0.015 (ok) | +0.058 / -0.021 (ok) | descriptive |
| en_enron_spacy | 0.258 / 0.521 | +0.003 (ok) | +0.046 / -0.022 (ok) | descriptive |

## H43: TAB direct identifiers kept by LR-UD PCCF

| row | idtype | gold | covered by a candidate | kept | kept / covered | floor |
|---|---|---|---|---|---|---|
| en_tab_redact | DIRECT | 515 | 497 | 486 | 0.978 | 0.930 (judged) |
| en_tab_redact | QUASI | 1737 | 1479 | 1432 | 0.968 | 0.939 |
| en_tab_redact | NO_MASK | 229 | 54 | 35 | 0.648 | 0.891 |
| en_tab_spacy | DIRECT | 515 | 502 | 492 | 0.980 | 0.931 (judged) |
| en_tab_spacy | QUASI | 1737 | 1531 | 1494 | 0.976 | 0.939 |
| en_tab_spacy | NO_MASK | 229 | 81 | 61 | 0.753 | 0.902 |

## Descriptive: ACI vs PCCF (phase-8 method) and pooled-split diagnostic

| row | shifted | PCCF P/R | ACI P/R | PCCF / ACI violations |
|---|---|---|---|---|
| en_tab_redact | True | 0.959/0.786 | 0.968/0.780 | 0 / 0 |
| en_tab_spacy | True | 0.321/0.824 | 0.320/0.813 | 0 / 0 |
| en_btc_redact | True | 0.760/0.157 | 0.760/0.157 | 0 / 0 |
| en_btc_spacy | True | 0.416/0.161 | 0.383/0.161 | 0 / 0 |
| en_wnut_redact | True | 0.779/0.556 | 0.771/0.581 | 1 / 0 |
| en_wnut_spacy | True | 0.582/0.675 | 0.532/0.687 | 0 / 0 |
| de_germeval | True | 0.786/0.850 | 0.786/0.850 | 0 / 0 |
| ru_factrueval | False | 0.958/0.547 | 0.956/0.555 | 0 / 0 |
| ar_anercorp | True | 0.649/0.815 | 0.647/0.846 | 1 / 0 |
| en_enron_redact | False | 0.526/0.362 | 0.520/0.363 | 0 / 0 |
| en_enron_spacy | False | 0.303/0.499 | 0.304/0.496 | 0 / 0 |
- ar_anercorp pooled (E1) vs original (E2) LR-UD recall: {dict} 1.000 vs nan; {dict+ner} 0.954 vs 0.925; {ner} 0.948 vs 0.891
- de_germeval pooled (E1) vs original (E2) LR-UD recall: {dict} 1.000 vs nan; {dict+ner} 0.956 vs 0.960; {ner} 0.957 vs 0.943
- en_btc_redact pooled (E1) vs original (E2) LR-UD recall: {dict} 1.000 vs 1.000; {ner} 0.955 vs 0.957
- en_btc_spacy pooled (E1) vs original (E2) LR-UD recall: {dict} 0.983 vs 0.969; {dict+ner} 0.963 vs 0.962; {ner} 0.953 vs 0.925
- en_tab_redact pooled (E1) vs original (E2) LR-UD recall: {ner} 0.951 vs 0.970
- en_tab_spacy pooled (E1) vs original (E2) LR-UD recall: {dict} 0.951 vs 0.973; {dict+ner} 0.950 vs 0.962; {ner} 0.950 vs 0.972
- en_wnut_redact pooled (E1) vs original (E2) LR-UD recall: {ner} 0.949 vs 0.890
- en_wnut_spacy pooled (E1) vs original (E2) LR-UD recall: {dict} 0.966 vs 0.958; {dict+ner} 0.959 vs 0.924; {ner} 0.953 vs 0.914

=== Verdicts (mechanical) ===
  H41: SUPPORTED
  H42: SUPPORTED
  H43: SUPPORTED
