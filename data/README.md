Synthetic data lives here after you run the generator. `synthetic_logs.jsonl` — the canonical 10,000-entry corpus every measured number in the top-level README is derived from — is committed, so anyone cloning the repo has the exact corpus without needing to run the generator first. Everything else generated into this directory (custom `--n`/`--out` runs, `data/raw/` from `export_raw_logs.py`, ad hoc validation corpora) is gitignored, since it's all reproducibly regenerated from the fixed seed below, not hand-authored.

```bash
python src/generate_logs.py --n 10000 --out data/synthetic_logs.jsonl --dirty-ratio 0.3
```

Deterministic given the fixed seed in `generate_logs.py` (`Faker.seed(42)`, `random.seed(42)`), so this always regenerates the exact same corpus described in the README's measured results.
