Synthetic data lives here after you run the generator. Nothing in this directory is committed to version control, since it's all reproducibly generated, not hand-authored.

```bash
python src/generate_logs.py --n 10000 --out data/synthetic_logs.jsonl --dirty-ratio 0.3
```

Deterministic given the fixed seed in `generate_logs.py` (`Faker.seed(42)`, `random.seed(42)`), so this always regenerates the exact same corpus described in the README's measured results.
