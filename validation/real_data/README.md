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
