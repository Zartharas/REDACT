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

## Confirmed results

Run twice independently (fixed seed 42, deterministic), against the synthetic corpus baseline for comparison:

| Dataset | Precision | Recall | PERSON spaced | PERSON flat | IP recall |
|---|---|---|---|---|---|
| Synthetic corpus (baseline) | 0.588 | 0.745 | 98.8% | 5.9% | 100.0% |
| OpenSSH (real) | 0.507 | 0.945 | 99.1% | 0.0% | 99.8% |
| Linux (real) | 0.498 | 0.961 | 98.4% | 3.4% | 100.0% |
| Thunderbird (real) | 0.414 | 0.982 | 100.0% | 0.0% | 100.0% |
| OpenStack (real, IP only) | 0.497 | 1.000 | n/a | n/a | 100.0% |
| Zookeeper (real, IP only) | 0.340 | 1.000 | n/a | n/a | 100.0% |

The format-sensitivity gap central to this project, near-total success on spaced names, near-total failure on flattened ones, replicates across three independent real sources, not just the synthetic corpus. Precision is lower on every real dataset than on synthetic (0.34–0.51 vs. 0.588), an honest sign that synthetic-only evaluation likely overstates real-world precision. IP recall stays near-perfect everywhere, consistent with IP detection being a comparatively solved, format-based problem next to name detection.

Reproduce with `bash download_loghub.sh && python inject_and_evaluate.py`. Requires `faker`, `presidio_analyzer`, `presidio_anonymizer`, and a spaCy English model (`python -m spacy download en_core_web_lg`) installed in whichever Python environment runs the script.
