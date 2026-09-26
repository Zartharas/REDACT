# Layer 1 + Layer 4 WebAssembly port (Task #48)

Standalone WebAssembly build of REDACT's Layer 1 (regex detectors,
`src/detect.py`) and Layer 4 (flattened-username name segmentation,
`src/flattened_names.py`) detection logic. Scoped deliberately narrow,
per the ROADMAP: this is the "does a real, correct, standalone Wasm
module even work" step. Vector/Fluent Bit/Envoy edge-agent integration
is explicitly **not** attempted here — see Task #49/`ROADMAP.md`.

## Why AssemblyScript, not Rust

Disclosed plainly, not glossed over: the original external-review memo
this ROADMAP item responds to proposed Rust targeting `wasm32`. This
development sandbox has no Rust toolchain (`rustc`/`cargo` not
installed, `rustup`'s installer is unreachable from here, and there's no
root/sudo to install one via `apt`). npm/Node are available, and
[AssemblyScript](https://www.assemblyscript.org/) — a TypeScript-like
language that compiles directly to standard `.wasm` via `npx asc` — let
this actually be built and tested end-to-end in this sandbox instead of
shipping untested Rust source. If a real Rust/wasm32 toolchain becomes
available, porting `assembly/index.ts`'s logic to Rust is a mechanical
exercise — the code is deliberately written in a manual-character-
scanning style with no regex engine, since neither AssemblyScript nor a
from-scratch Rust port would have one available offline either.

## What's actually verified, not just claimed

Every regex pattern below is a hand-written character scanner, not a
real regex engine — AssemblyScript doesn't have one. Each one is
reasoned through by hand against Python's actual `\b`/backtracking
semantics (see `assembly/index.ts`'s per-function comments), and then
**directly checked**, not just argued, against the real Python
implementations it replaces:

```bash
cd wasm/layer1_4
npm install
npm run build
python3 tests/equivalence_test.py            # full 10,000-line synthetic corpus
python3 tests/real_data_equivalence_test.py   # 12,033 real log lines (Loghub + CloudTrailFlaws)
```

**Results, both confirmed in this sandbox:**

- **10,000/10,000 lines byte-identical** against `src/detect.py`'s
  `scan_regex()` and `src/flattened_names.py`'s `scan_flattened_names()`
  over the canonical synthetic corpus (`data/synthetic_logs.jsonl`).
- **12,033/12,033 lines byte-identical** against the same real-world
  datasets `validation/real_data/` already uses (Linux/OpenSSH/
  OpenStack/Thunderbird/Zookeeper Loghub logs, plus real CloudTrail and
  Windows Event samples), re-serialized identically to how
  `validation/real_data/inject_and_evaluate.py` actually builds the text
  `detect.py` scans.

**A real bug was found and fixed by the real-data test, not the
synthetic one:** the first version of `scanCreditCardDigitRuns()` only
checked digit-run length (12-19), reasoning correctly that shrinking a
match within an over-long run never helps satisfy `\d{12,19}\b`'s
trailing boundary — but never separately verified the LEADING boundary.
`Linux_2k.log` line 198 contains `n219076184117.netvigator.com`: a
12-digit run immediately preceded by `n` (a `\w` character, so no `\b`
exists there), which Python correctly excludes and the first Wasm
version incorrectly flagged as `CREDIT_CARD`. Fixed by adding the same
`boundaryAt()` check every other scanner in this file already had — see
the fix's own comment in `assembly/index.ts` and `BUGS_AND_FIXES.md`.
This is exactly why the equivalence test exists against real data and
not just this project's own synthetic corpus (same rationale as
`validation/real_data/`'s own README).

## Known, disclosed gap

`scanEmail()`'s local-part boundary handling is reasoned through for the
realistic email shapes this project's corpora actually contain (both
the synthetic Faker-generated ones and the real CloudTrail/Windows
samples all pass byte-identical) — see its own comment in
`assembly/index.ts` for why fully pathological inputs (local parts
mixing `\w`/non-`\w` transitions in ways real email generators don't
produce) aren't proven equivalent to true regex backtracking here.
Disclosed rather than assumed correct, matching this project's standard
elsewhere.

## Performance: not a demonstrated win as measured here

A quick, honestly-reported comparison (10,000 lines, this sandbox, Node
`performance.now()` vs. Python `time.time()`) through the JSON-string
interface both `tests/*.py` scripts and `tests/run_wasm_over_corpus.mjs`
use: **Python ran the same detection logic slightly faster (~27,400
lines/sec) than calling the compiled Wasm module through Node via that
JSON-string interface (~21,800 lines/sec).** This is not a claim that
Wasm is slower at the actual character-scanning work — the JSON
`stringify`/`parse` round-trip and JS-string-to-Wasm-linear-memory
marshalling on every call are real, measurable overhead this interface
pays on every single scan, and dominate at this small a workload. A real
edge-deployment performance case (the external-review memo's original
motivation) would need a typed, allocation-light interface — spans
returned as flat `i32` arrays over shared Wasm memory instead of a JSON
string round-trip through JS — which this task deliberately did not
build, since the point of this step was proving detection-logic
correctness, not chasing a throughput number this task never actually
measured honestly until now. Stated here plainly rather than left
implied or oversold, matching every other performance claim in this
project's `BUGS_AND_FIXES.md`.

## Files

- `assembly/index.ts` — the port itself (Layer 1 + Layer 4).
- `assembly/names_data.ts` — auto-generated from Faker's
  `en_US.Provider.first_names`/`last_names`, the same dictionary source
  `src/flattened_names.py` uses. Regenerate via:
  ```bash
  python3 -c "
  from faker.providers.person.en_US import Provider as P
  fn = sorted({n.lower() for n in P.first_names})
  ln = sorted({n.lower() for n in P.last_names})
  # ...see the exact generation script used in this task's own history
  # for the file-writing logic if this ever needs regenerating.
  "
  ```
- `tests/equivalence_test.py` — synthetic-corpus equivalence check.
- `tests/real_data_equivalence_test.py` — real-data equivalence check,
  the one that found the CREDIT_CARD boundary bug above.
- `tests/run_wasm_over_corpus.mjs` — Node helper both equivalence
  scripts shell out to.
- `build/` and `node_modules/` are gitignored (regenerate via
  `npm install && npm run build`), matching this project's existing
  policy on generated/regenerable artifacts.
