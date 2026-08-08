"""
ROADMAP item 10, the originally-planned test finally unblocked (2026-08-08):
does the flattened-username dictionary (src/flattened_names.py, built from
Faker's en_US first_names/last_names lists) generalize to an actual US name
population, and not only to Faker's non-US locales
(validation/non_us_name_test.py, which answered a related but different
question -- see that script's own docstring for why real data wasn't used
there)?

Data sources, both official US government aggregate statistics, public
domain, and each publisher's own page states the data does not identify
individuals (see raw/README.md for full citations):
  - Given names: SSA "Popular Baby Names," national data, 1880-2025.
  - Surnames: US Census Bureau, 2010 Census, all surnames occurring 100+
    times (162,253 of them, one row of which -- "ALL OTHER NAMES" -- is an
    aggregate bucket, not a real name, and is excluded here).

Methodology: unlike non_us_name_test.py's uniform sampling over each
locale's full name list, this test samples names weighted by their actual
real-world frequency -- given names from the last 10 SSA years (2016-2025,
both sexes combined), surnames by their actual 2010 Census count. This is
a deliberate methodological choice, not an oversight. The question this
test asks is how the dictionary performs against a realistic production
username population, and a realistic population is Zipfian (a small number
of names account for a large share of real people) rather than uniform
over every name that ever cleared SSA's 5-occurrence privacy floor in 145
years of data. Sampling uniformly over 162,253 surnames (the vast majority
of which are individually rare) would understate how the dictionary
performs on the names a real system would actually see most often.

Run: python validation/real_name_frequency/build_real_name_test.py
(no network access needed once raw/*.zip exist locally -- see
raw/README.md and download_name_data.sh)
"""
import sys
import os
import csv
import io
import random
import zipfile
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import flattened_names  # noqa: E402

RAW_DIR = os.path.join(os.path.dirname(__file__), "raw")
CENSUS_ZIP = os.path.join(RAW_DIR, "census_surnames.zip")
SSA_ZIP = os.path.join(RAW_DIR, "ssa_given_names.zip")
SSA_YEARS = range(2016, 2026)  # last 10 years of data in the zip (through 2025)


def load_census_surnames() -> Counter:
    """Returns {lowercase surname: 2010 Census count}, excluding the
    'ALL OTHER NAMES' aggregate row (rank 0 -- not a real name)."""
    counts = Counter()
    with zipfile.ZipFile(CENSUS_ZIP) as z:
        with z.open("Names_2010Census.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
            for row in reader:
                name = row["name"].strip()
                if not name or name == "ALL OTHER NAMES":
                    continue
                try:
                    count = int(row["count"])
                except ValueError:
                    continue
                counts[name.lower()] += count
    return counts


def load_ssa_given_names(years=SSA_YEARS) -> Counter:
    """Returns {lowercase given name: summed SSA count across `years` and
    both sexes}. SSA's own file already excludes names with fewer than 5
    occurrences in a given year/state ('to safeguard privacy' -- SSA's own
    wording), so this is already a privacy-conscious aggregate, not raw
    individual records."""
    counts = Counter()
    with zipfile.ZipFile(SSA_ZIP) as z:
        for year in years:
            fname = f"yob{year}.txt"
            if fname not in z.namelist():
                continue
            with z.open(fname) as f:
                for line in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
                    parts = line.strip().split(",")
                    if len(parts) != 3:
                        continue
                    name, _sex, count = parts
                    counts[name.lower()] += int(count)
    return counts


def weighted_sample(counter: Counter, n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    names = list(counter.keys())
    weights = list(counter.values())
    return rng.choices(names, weights=weights, k=n)


def measure_recall(tokens: list[str]) -> tuple[int, int, list[str]]:
    hit = 0
    misses = []
    for tok in tokens:
        text = f"sudo[1234]: {tok} : USER=root ; COMMAND=/usr/bin/true"
        hits = flattened_names.scan_flattened_names(text)
        found = any(h["start"] == 12 for h in hits)  # position of tok
        if found:
            hit += 1
        elif len(misses) < 8:
            misses.append(tok)
    return hit, len(tokens), misses


def dictionary_coverage(counter: Counter, dictionary: set[str]) -> tuple[float, float]:
    """Returns (unique-name coverage %, frequency-weighted coverage %) --
    what fraction of the real population's distinct names, and what
    fraction of real PEOPLE (weighted by count), are actually present in
    the en_US dictionary. These can diverge a lot: a dictionary can cover
    a small % of distinct names but a much larger % of the population if
    it happens to include the most common ones (or vice versa)."""
    names = set(counter.keys())
    unique_cov = len(names & dictionary) / len(names) if names else 0.0
    total_mass = sum(counter.values())
    covered_mass = sum(c for n, c in counter.items() if n in dictionary)
    weighted_cov = covered_mass / total_mass if total_mass else 0.0
    return unique_cov, weighted_cov


def main(n: int = 2000, seed: int = 20260808):
    if not (os.path.exists(CENSUS_ZIP) and os.path.exists(SSA_ZIP)):
        print(f"Missing source data. Run: bash "
              f"{os.path.join(os.path.dirname(__file__), 'download_name_data.sh')}")
        sys.exit(1)

    print("Loading real US name-frequency data...")
    surnames = load_census_surnames()
    given_names = load_ssa_given_names()
    print(f"  {len(surnames):,} distinct surnames (2010 Census, 100+ occurrences)")
    print(f"  {len(given_names):,} distinct given names (SSA, {min(SSA_YEARS)}-{max(SSA_YEARS)})")

    given_unique_cov, given_weighted_cov = dictionary_coverage(
        given_names, flattened_names.FIRST_NAMES)
    sur_unique_cov, sur_weighted_cov = dictionary_coverage(
        surnames, flattened_names.LAST_NAMES)
    print(f"\nDictionary coverage (en_US Faker list vs. real population):")
    print(f"  Given names: {given_unique_cov:.1%} of distinct real names, "
          f"{given_weighted_cov:.1%} of real population (weighted by SSA count)")
    print(f"  Surnames:    {sur_unique_cov:.1%} of distinct real names, "
          f"{sur_weighted_cov:.1%} of real population (weighted by Census count)")

    print(f"\nSampling {n} flattened <given><surname> tokens, weighted by "
          f"real frequency (seed={seed})...")
    given_sample = weighted_sample(given_names, n, seed)
    surname_sample = weighted_sample(surnames, n, seed + 1)
    tokens = [(g + s) for g, s in zip(given_sample, surname_sample)]

    hit, total, misses = measure_recall(tokens)
    print(f"\n=== Overall recall on real, frequency-weighted US name population ===")
    print(f"{hit}/{total} = {hit/total:.1%}")
    print(f"Example misses: {misses}")
    print(f"\n(Compare: 50.3% on the en_US-Faker-derived synthetic corpus "
          f"this layer was built from, README.md; 1.4% on Faker's non-US "
          f"locales, validation/non_us_name_test.py)")

    # Subset analysis: recall split by whether the sampled given name and
    # surname are in the dictionary in the specific role segment_match()
    # checks at the true split point (left=given must be in FIRST_NAMES or
    # LAST_NAMES with a matching right=surname role -- see below). A first
    # pass at this used a looser "name is in the dictionary at all" check
    # and got a confusing 82.2%-not-100% result for the "both in
    # dictionary" bucket. The actual cause, found by inspecting the
    # mismatches directly, turned out to be worth reporting on its own:
    # a name like "foster" or "kennedy" sits in LAST_NAMES (a real,
    # well-known surname) but not in FIRST_NAMES, even though modern SSA
    # data shows plenty of real people are given that name as a first
    # name today (the "surname as first name" naming trend -- Mason,
    # Hunter, Cooper, Foster, Kennedy, etc.). When that happens on both
    # halves of a token, segment_match() correctly finds no valid
    # <first><last> or <last><first> split, because from the dictionary's
    # perspective both halves fill the same role (last-name-shaped) --
    # not because the algorithm is broken.
    print(f"\n=== Recall split by whether each sampled name is in the "
          f"dictionary in the specific role segment_match() needs ===")
    role_ok = role_hit = role_total = 0       # (given in FIRST, surname in LAST) --
                                                # or the reverse-order match
    wrong_role = wrong_role_hit = wrong_role_total = 0  # both names known,
                                                          # but same role
    missing = missing_hit = missing_total = 0  # at least one name entirely
                                                 # absent from the dictionary
    for g, s, tok in zip(given_sample, surname_sample, tokens):
        text = f"sudo[1234]: {tok} : USER=root ; COMMAND=/usr/bin/true"
        found = any(h["start"] == 12 for h in flattened_names.scan_flattened_names(text))
        forward = g in flattened_names.FIRST_NAMES and s in flattened_names.LAST_NAMES
        reverse = g in flattened_names.LAST_NAMES and s in flattened_names.FIRST_NAMES
        g_known = g in flattened_names.FIRST_NAMES or g in flattened_names.LAST_NAMES
        s_known = s in flattened_names.FIRST_NAMES or s in flattened_names.LAST_NAMES
        if forward or reverse:
            role_total += 1
            role_hit += found
        elif g_known and s_known:
            wrong_role_total += 1
            wrong_role_hit += found
        else:
            missing_total += 1
            missing_hit += found
    if role_total:
        print(f"  Correct role for a valid split: {role_hit}/{role_total} = {role_hit/role_total:.1%} "
              f"(should be ~100% modulo length bounds -- the one confirmed exception in "
              f"this script's own dev run was 'sarahli' ('li' is a real, common surname "
              f"but only 2 characters, below MIN_PART_LEN=3, so no split point in "
              f"_segment_match() ever considers a 2-character half -- a length-bound "
              f"edge case, not a dictionary-coverage gap)")
    if wrong_role_total:
        print(f"  Both names known, wrong role:   {wrong_role_hit}/{wrong_role_total} = "
              f"{wrong_role_hit/wrong_role_total:.1%} (e.g. both are known surnames, "
              f"like 'fostercastaneda' -- 'foster' is a real modern first name per SSA "
              f"but only in LAST_NAMES per Faker's dictionary)")
    if missing_total:
        print(f"  At least one name entirely unknown: {missing_hit}/{missing_total} = "
              f"{missing_hit/missing_total:.1%}")
    print(f"\nThis is the actual mechanism behind the headline recall number: "
          f"segment_match() requires the two halves of a token to fill "
          f"complementary FIRST_NAMES/LAST_NAMES roles, not merely both be "
          f"'in the dictionary somewhere.' Two real, distinct gaps compound "
          f"to produce the 15% headline figure -- coverage (most real given "
          f"names/surnames aren't in Faker's ~700/~1,000-name lists at all) "
          f"and role rigidity (a name Faker classifies as surname-only "
          f"can't fill the first-name role even if real usage has drifted, "
          f"per the 'surname as first name' trend measured above).")


if __name__ == "__main__":
    main()
