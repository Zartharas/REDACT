# Real-data validation

Everything else in this repository is tested against REDACT's own synthetic corpus. This directory answers the question that synthetic-only testing cannot: does the detection ensemble behave the same way on real log data nobody here wrote?

```bash
bash download_loghub.sh
python inject_and_evaluate.py
```

Pulls five real, unmodified log datasets from [Loghub](https://github.com/logpai/loghub) (Zhu et al., ISSRE 2023), the same benchmark source used by [SDLog](https://arxiv.org/abs/2505.14976) (Aghili et al., 2025), a closely related but methodologically different study (fine-tuned deep learning classifier, broader sensitivity definition covering usernames/paths/IDs, no format-specific breakdown). Three of the five (OpenSSH, Linux, Thunderbird) contain a real authentication field, into which a synthetic identity is injected using the exact same methodology as the main synthetic corpus, replacing the existing attacker-guessed or fixed test username, never a real person's data. Two (OpenStack, Zookeeper) contain real IP addresses used directly as ground truth, no injection.

Eleven of Loghub's sixteen datasets are deliberately excluded (Android, Apache, BGL, Hadoop, HDFS, HealthApp, HPC, Mac, Proxifier, Spark, Windows), none of them has a field that would plausibly carry a person's name, email, SSN, credit card, or medical record number in production. Injecting PII into them would test a fabricated scenario, not a real one.

One methodological detail worth knowing if you're comparing your own run against the paper's reported numbers: PAM logs the fixed string `unknown`, not the attacker's actual input, when an authentication attempt targets a nonexistent account. Lines matching that fixed status label are excluded from injection eligibility, since replacing `unknown` with a synthetic name would inject into a field that never held a username to begin with.

Same integrity discipline as the synthetic generator: every injected offset is verified against the actual injected text before evaluation, and the script asserts on any mismatch rather than silently continuing.

## Confirmed results (corrected 2026-08-07 — see Bug 10 in `BUGS_AND_FIXES.md`)

**The table originally here (precision 0.34–0.51 across all five real datasets) was substantially wrong, not just imprecise.** `inject_and_evaluate.py` lacked the prediction-dedup step `evaluate.py` already has; whenever regex and NER independently agreed on the same real span (routine for IP addresses), the second correct detection was counted as a false positive instead of a harmless duplicate. Recall was never affected — only precision. Fixed and re-run against all five datasets, corrected numbers below; full quantified before/after and root cause in `BUGS_AND_FIXES.md` Bug 10.

| Dataset | Precision | Recall | PERSON spaced | PERSON flat (regex+NER) | PERSON flat (+ Layer 4) | IP recall |
|---|---|---|---|---|---|---|
| Synthetic corpus (baseline, naive regex+NER) | 0.588 | 0.706 | 98.8% | 4.9% | 68.1%* | 100.0% |
| OpenSSH (real) | **0.974** | 0.945 | 99.1% | 0.0% | 45.5% | 99.8% |
| Linux (real) | **0.920** | 0.961 | 98.4% | 3.4% | 50.0% | 100.0% |
| Thunderbird (real) | **0.701** | 0.982 | 100.0% | 0.0% | 75.0%† | 100.0% |
| OpenStack (real, IP only) | **0.989** | 1.000 | n/a | n/a | n/a | 100.0% |
| Zookeeper (real, IP only) | **0.476** | 1.000 | n/a | n/a | n/a | 100.0% |

\* Synthetic PERSON-flat recall with Layer 4 is the full-ensemble number from the main `README.md` (2,038/2,993 overall PERSON, i.e. 68.1% including both formats) — kept here for reference, not a like-for-like flat-only figure; see the main README for the flat-only 50.3% number.
† Thunderbird's flat-name sample is small (n=12 injected), so 0.0% → 75.0% is a real but noisy result — treat the direction (Layer 4 helps) as more reliable than the exact percentage at this sample size.

**Revised interpretation:** the format-sensitivity gap (near-total success on spaced names, sharply lower on flattened ones) still replicates across all three PERSON-bearing real sources — that finding holds. What does **not** hold anymore is the previously stated "precision is consistently lower on real data than synthetic": three of five real datasets (OpenSSH, Linux, OpenStack) now show *higher* precision than the synthetic baseline's 0.588, and only Zookeeper and Thunderbird show real degradation, driven by the same private/internal-IP-range false positives documented as Finding 1 in the main `README.md` — a genuine detector limitation on datasets with heavier internal-IP traffic, not a synthetic-vs-real generalization gap. **The flattened-username layer (Layer 4) also generalizes to real, unmodified log text**: recall gains of similar magnitude to the synthetic corpus's 50.3% (OpenSSH 0.0%→45.5%, Linux 3.4%→50.0%, Thunderbird 0.0%→75.0% on a small n=12 sample) show up on real Loghub lines, at effectively unchanged precision (e.g. OpenSSH 0.974→0.975, Linux 0.920→0.921).

**What this does and doesn't prove, stated plainly:** the injected PERSON values in these three datasets are still drawn from Faker (`fake.user_name()` / `fake.name()`), the same generator whose name lists the flattened-username layer's own dictionary is built from — see `flattened_names.py`'s own documented limitation. This test validates the layer against **real surrounding log text** (genuine Loghub lines this project didn't write), which is a real and useful generalization check, but it does **not** validate the dictionary against a name population outside Faker's own list — the dictionary-matches-itself concern from the synthetic-corpus result isn't resolved by this test alone. **Update, 2026-08-07 (ROADMAP item 10):** `validation/non_us_name_test.py` addresses the dictionary-vs-real-population question directly, though on the synthetic corpus rather than these real-text datasets — it tests the en_US dictionary against flattened names from Faker's German/French/Spanish/Italian providers (largely disjoint from en_US, 0.5–13.3% surname overlap depending on locale) and finds recall collapses to 1.4% (28/2,000) outside the dictionary's own population, confirming the concern directly. That test is still Faker-sourced, not real name-frequency data — a genuine US Census-based version remains blocked in this project's dev sandbox (no network route to census.gov, and the one real-world alternative on PyPI was rejected because its data traces to a 2021 Facebook breach) — so combining a truly independent name source with these real Loghub datasets specifically remains open if such a source becomes available later.

Reproduce with `bash download_loghub.sh && python inject_and_evaluate.py`. Requires `faker`, `presidio_analyzer`, `presidio_anonymizer`, and a spaCy English model (`python -m spacy download en_core_web_lg`) installed in whichever Python environment runs the script.
