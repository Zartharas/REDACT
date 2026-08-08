# Real US name-frequency data (ROADMAP item 10 — DONE, 2026-08-08)

This directory holds the raw source files for what was originally the
still-open half of item 10: `validation/non_us_name_test.py` had already
confirmed the flattened-username dictionary collapses to 1.4% recall
outside its own (Faker en_US) name population, but that test used Faker's
*non-US* locales, not a real US name population — so it never answered
the actual originally-planned question, how does the dictionary do
against the real US given/surname distribution it claims to represent?

**That question is now answered.** `validation/real_name_frequency/build_real_name_test.py`
samples 2,000 flattened tokens from the real SSA/Census data below,
weighted by real frequency, and measures **15.2% recall (305/2,000)**
against `scan_flattened()` — landing between the 50.3% Faker-matched
figure and the 1.4% non-US-locale figure, right where a real population
should. See `README.md`'s Layer 4 section for the full result and what
drives that number (dictionary coverage and a real surname-as-first-name
naming trend, not an algorithm defect). This README's rest is kept as
written for the data-provenance record; only this intro reflects the
final status.

## What goes here (not committed — run the download script instead)

Both sources below are official US government aggregate statistics, public
domain, and each one's own publisher explicitly states the data does not
identify individuals — the same ethical bar that got `names-dataset`
(PyPI, sourced from a 2021 Facebook breach) rejected for this project
earlier.

**Don't hand-download and commit these.** Run
`bash validation/real_name_frequency/download_name_data.sh` instead — it
fetches both zips into this directory. This mirrors
`validation/real_data/download_loghub.sh`'s own pattern in this project:
raw external data is fetched fresh by a script, not checked into git
(`raw/*.zip` is gitignored; only this README and the download script are
tracked). The ingestion script (once written) reads directly out of the
zip via Python's `zipfile` module, so there's no need to extract by hand
either.

1. **`ssa_given_names.zip`** — Social Security Administration, "Popular
   Baby Names," national data, all applications for a Social Security
   card by year, 1880–present.
   Source page: <https://www.ssa.gov/oact/babynames/limits.html>
   Direct download: <https://www.ssa.gov/oact/babynames/names.zip> (~7 MB)
   Format: one `yob<YEAR>.txt` file per year inside the zip, each a plain
   CSV with columns `name,sex,count` (names with fewer than 5 occurrences
   in a given year are excluded by SSA itself, "to safeguard privacy").

2. **`census_surnames.zip`** — US Census Bureau, "Frequently Occurring
   Surnames from the 2010 Census," all surnames occurring 100+ times in
   the 2010 Census (162,253 names).
   Source page: <https://www.census.gov/topics/population/genealogy/data/2010_surnames.html>
   Direct download: <https://www2.census.gov/topics/genealogy/2010surnames/names.zip> (<1 MB)
   Format: `Names_2010Census.csv` inside the zip, columns include `name`,
   `rank`, `count`, plus demographic proportion columns not needed here.
   The source page states plainly: "the data do not in any way identify
   any specific individuals."

## What the script does (`validation/real_name_frequency/build_real_name_test.py`)

Mirrors `validation/non_us_name_test.py`'s methodology: builds flattened-
username tokens from real SSA given names and real Census surnames (e.g.
`donaldgarcia`-style, no separator), injects them into the same syslog
`sudo` template shape used elsewhere in this project, and measures
`scan_flattened()`'s recall against this real population instead of a
Faker-generated one — weighted by real name frequency, not sampled
uniformly, since a realistic username population is Zipfian. Fully
regex/dictionary-based (`src/flattened_names.py`), no spaCy or Docker
required, so it ran cleanly in this project's dev sandbox once the source
data existed locally.

**Scope note, stated up front:** even this test won't be a perfect
substitute for "real production username data," since it's still
first-name x last-name combinations drawn from frequency tables, not
usernames actually observed in a real system (which might use initials,
nicknames, numbers, or other conventions Faker's `user_name()` doesn't
model either). It closes the specific "is the dictionary just US-biased
Faker names" question, not the broader "does this generalize to any real
production identity system" question.
