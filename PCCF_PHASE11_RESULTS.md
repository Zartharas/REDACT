# PCCF phase 11 results: real documents

## Summary (added 2026-09-24; figures are the Docker reference run)

**All three pre-registered hypotheses are SUPPORTED on real, human-annotated
documents** (court judgments, tweets, news; English, German, Russian).

- **H41 (rule floors): 8/8 rows hold.** The rule scorer never cuts recall
  below its floor on real text.
- **H42 (LR-UD useful and valid): 6/8.** The two non-passes are different in
  kind:
  - `en_wnut_redact` is a floor miss (ΔR −0.067 against the floor). The
    split diagnostic attributes it to train→test shift, not to the method:
    the {ner} group's recall is 0.949 under pooled (exchangeable) splits
    versus 0.890 on the original split. ACI removes the violation (1 → 0)
    at equal precision, the same pattern as phases 6 and 8.
  - `ru_factrueval` is valid but not useful (ΔP +0.012 < 0.03). The union is
    already at P 0.946, so there is little noise left to filter.
- **H43 (TAB direct identifiers): 0.978 / 0.980 kept of those covered,**
  against floors of 0.930 / 0.931.

### Devil's advocate (read before citing)
1. **The guarantee is conditional on a candidate.** End-to-end, TAB DIRECT
   recall is 486/515 = 0.944 (REDACT layers) and 492/515 = 0.955 (spaCy
   layers). 18 and 13 direct identifiers are missed by every layer, and 11
   and 10 more are dropped by PCCF. A privacy claim must quote the
   end-to-end figure, not 0.98.
2. **The layers dominate the outcome.** BTC union recall is 0.16–0.17: the
   detectors miss most tweet names, and no threshold can recover them
   (practitioner rule 6).
3. **TAB NO_MASK PERSON (not judged)** keeps only 0.648 / 0.753. These are
   names the annotators chose not to mask (e.g. public officials), so the
   low figure is expected. It is also why they are excluded from H43.
4. **Shift again.** 7 of 8 rows test as shifted. Static calibration held
   here, but the only miss came from shift. For a real deployment, use ACI
   on analyst feedback (rule 3).
5. **Not run:** Enron (descriptive; the download returned a non-zip) and
   ANERcorp (manual licence download not supplied). Neither affects the
   verdicts, which were pre-registered over the rows that have data.
6. **Minor:** there was one cache error each in `en_btc_redact` and
   `en_btc_spacy` (1 of 3,500 docs). That document is excluded, which is
   negligible.

---

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
| ar_anercorp | no data | | | |
| en_enron_redact | no data | | | |
| en_enron_spacy | no data | | | |

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
