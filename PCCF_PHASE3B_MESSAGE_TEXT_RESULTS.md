# PCCF T7: names in log message text (fix attempt for the T3 caveat)

Pre-registered in `PCCF_PHASE3_PREREGISTRATION.md` (T7) before any run.
Harness: `validation/real_data/pccf_phase3b_message_text.py`. Output:
`pccf_phase3b_message_results.{txt,json}`. Rows tagged *exploratory* were
added after the pre-registered verdicts were computed; they are reported,
not judged.

**Setup:** same 5 log datasets, 3 seeds and leave-one-dataset-out protocol
as T3, with identity-field injection kept. On top of that:

- 15% of lines get an appended message sentence naming a person (spaced
  or flattened).
- 10% get a distractor sentence naming a software product instead.
- Held-out datasets use a disjoint template/product set.
- Message text holds 3,601 gold names; identity fields hold 3,540.

## Results (pooled; α = 0.05)

| configuration | P | R | message-text R |
|---|---|---|---|
| union | 0.813 | 0.754 | 0.769 |
| production field-gated (raw) | 0.819 | 0.754 | 0.769 |
| **E2: the T3 model as deployed, calibrated on field-only names** | 0.974 | 0.366 | **0.009** |
| S1: KV rule, mask groups, recalibrated on data with message names | 0.874 | 0.724 | 0.712 |
| **S2: KV rule, mask × key groups** | 0.854 | 0.738 | 0.738 |
| S3: hybrid, learned word features | 0.880 | 0.579 | 0.431 |
| S4: one LR over all features | 0.864 | 0.390 | 0.429 |
| E3: hybrid without word features (exploratory) | 0.844 | 0.739 | 0.740 |

Candidate recall in the no-key groups (the recall floor for unkeyed
names):

| config | ner, no key | flat, no key |
|---|---|---|
| S2 | 0.936 | 1.000 |
| E3 | 0.941 | 0.998 |
| S3 | **0.522** | **0.524** |

**Verdicts:**

- **H12 NOT SUPPORTED:** once calibration data includes message names, even
  mask-only groups keep 93% of union's message recall.
- **H13 NOT SUPPORTED:** S3's floor collapses to 0.52. S2 misses the
  tolerance narrowly (0.936 vs 0.940).
- **H14 NOT SUPPORTED:** S3 loses too much recall.

## What this means

1. **The T3 caveat was real and severe.** The T3 model, calibrated only on
   names in identity fields, **drops 99% of names in message text** (recall
   0.009), while its precision (0.97) looks excellent. That is exactly the
   kind of silent GDPR Art. 32 / HIPAA §164.312 failure the project warns
   about. The certificate was valid for the calibration population and
   misleading outside it.
2. **The fix is mainly the calibration data, and only secondarily the
   partition.** Once calibration includes names in every context where they
   occur in production, the recall floor comes back even with mask-only
   groups. Adding key status to the groups (S2) gives each context its own
   floor, which makes a missing context visible (fail-open) rather than
   silently dropped. It also holds recall to within about 1.5 points of
   union.
3. **Learned word features broke the guarantee.** The hybrid's text LR
   (S3) learned the calibration templates' wording, so on held-out wording
   its scores shifted and the floor fell to 0.52. The same hybrid without
   word features (E3) holds the floor at about 0.94. The rule: **keep
   nonconformity features structural** (key status, shape, layer
   agreement, model confidence), never raw vocabulary. Conformal validity
   depends on exchangeability, and wording differs between log sources.
4. **The T3 precision gain was mostly an artifact of field-only
   injection.** With names possible anywhere, the valid configurations
   gain **+3 to +4 points of precision** (S2 +0.040 at −0.016 recall; E3
   +0.030 at −0.015), not +20. Text context in log messages barely
   separates person names from product names. The paper must report the
   message-text number as the realistic one. The +20 applies only where
   names are known to sit in identity fields.
5. Flattened names are unchanged (about 0.50–0.54 in both locations).
   Detection, not fusion, is the bottleneck.

## Revised recommendation
- For deployment: use **S2** (the fit-free KV rule, mask × key groups).
  Calibrate on data containing names in every location seen in
  production, and alert whenever a group's calibration set is below 19
  true names (fail-open is recall-safe but uncertified).
- For the paper, the redaction-tier claim becomes:
  - large gains when PII location is structured (MEDDOCAN fields, identity
    fields)
  - modest gains in unstructured log text
  - two stated preconditions: calibration coverage of every context, and
    structural-only features

  E2 belongs in the paper as the cautionary example.
