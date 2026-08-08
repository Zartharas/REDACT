"""
ROADMAP item 10: test the flattened-username layer (src/flattened_names.py)
against a name population it was NOT built from, to check whether its
50.3%-on-the-synthetic-corpus recall is a real, generalizable capability or
an artifact of the dictionary and the corpus sharing a source (Faker's
en_US first_names/last_names list, in both cases).

Before the results below, here is what this test actually establishes and
where it falls short:

The original plan (see flattened_names.py's own docstring and ROADMAP.md
item 10) was to swap in real-world name-frequency data (e.g. US Census
surname/given-name frequency lists) for this test. That wasn't possible in
this environment: this sandbox's network access is allowlisted to a small
set of hosts (PyPI, GitHub repo cloning) and does not reach census.gov,
nltk's data mirrors, or raw.githubusercontent.com (the same restriction
documented elsewhere in this project, e.g. BUGS_AND_FIXES.md's notes on the
spaCy model download). One real-world name dataset was available on PyPI
(`names-dataset`), but it was rejected on inspection: its own documentation
states it is "extracted from the Facebook massive dump (533M users)," i.e.
built from a documented 2021 data breach. Using breach-derived personal
data to validate a PII-protection tool would directly contradict this
project's own stated principles (no real organizational or personal data
anywhere in this repository), so it was left out deliberately rather than
overlooked.

What this test does instead: it uses Faker's own non-US locale name
providers (already an installed dependency, no network access needed) --
German, French, Spanish, and Italian -- as a name population that is
largely, though not entirely, disjoint from the en_US Faker list
flattened_names.py's dictionary is built from. Measured overlap before
running this test: DE/FR/ES/IT surnames overlap with the en_US surname
list at 4.5%, 13.3%, 12.0%, and 0.5% respectively (some overlap is expected
and correct, since a number of given names, especially, are shared across
Western naming traditions). This is a genuine test of whether the
dictionary generalizes past its own source list, and it directly exercises
the exact failure mode the docstring already names as a known risk
("someone named Zhiwei Tan or Aoife O'Sullivan is not in Faker's default
en_US list"). Still, it is Faker-sourced data in both the dictionary and
(for this test) the injected names -- just not the SAME Faker locale -- so
it does not fully settle the broader "real production username population"
question the original US Census plan targeted. That remains open.
"""
import sys
import random

sys.path.insert(0, "src")
import flattened_names  # noqa: E402

from faker.providers.person.de_DE import Provider as DE
from faker.providers.person.fr_FR import Provider as FR
from faker.providers.person.es_ES import Provider as ES
from faker.providers.person.it_IT import Provider as IT

LOCALES = {"de_DE": DE, "fr_FR": FR, "es_ES": ES, "it_IT": IT}


def build_flattened_tokens(provider, seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    first_names = list(provider.first_names)
    last_names = list(provider.last_names)
    tokens = []
    for _ in range(n):
        first = rng.choice(first_names)
        last = rng.choice(last_names)
        # Matches the flattened shape generate_logs.py's PERSON_name_flat
        # slot produces: lowercase, no separator, first+last concatenated.
        # This approximates what fake.user_name() does for the synthetic
        # corpus, but it's reproduced directly here from the locale's raw
        # name lists so the test doesn't depend on user_name()'s own
        # locale-specific formatting quirks.
        tokens.append((first + last).lower())
    return tokens


def main(n_per_locale: int = 500, seed: int = 20260807):
    print(f"Testing flattened_names.py's en_US dictionary against {n_per_locale} "
          f"flattened tokens per locale, {len(LOCALES)} non-US locales "
          f"({n_per_locale * len(LOCALES)} tokens total)\n")

    overall_hit = overall_total = 0
    ascii_hit = ascii_total = nonascii_hit = nonascii_total = 0
    for locale_name, provider in LOCALES.items():
        tokens = build_flattened_tokens(provider, seed=seed, n=n_per_locale)
        hit = 0
        misses = []
        for tok in tokens:
            # Embed in a minimal log-line shape so scan_flattened_names'
            # token-boundary regex behaves the same way it does against
            # real log text, not a bare word.
            text = f"sudo[1234]: {tok} : USER=root ; COMMAND=/usr/bin/true"
            hits = flattened_names.scan_flattened_names(text)
            found = any(h["start"] == 12 for h in hits)  # position of tok
            if found:
                hit += 1
            elif len(misses) < 5:
                misses.append(tok)
            if tok.isascii():
                ascii_total += 1
                ascii_hit += found
            else:
                nonascii_total += 1
                nonascii_hit += found
        rate = hit / len(tokens)
        print(f"{locale_name}: {hit}/{len(tokens)} recall = {rate:.1%}")
        print(f"  example misses: {misses}")
        overall_hit += hit
        overall_total += len(tokens)

    print(f"\nOverall non-US-locale recall: {overall_hit}/{overall_total} "
          f"= {overall_hit/overall_total:.1%}")
    print(f"(compare to 50.3% recall on the en_US-Faker-derived synthetic "
          f"corpus this layer was originally measured against, README.md)")
    print(f"\nBreakdown by whether the accented characters some of these "
          f"names contain (é, ö, í, etc.) even reach the dictionary lookup "
          f"at all -- scan_flattened_names' token regex is ASCII-only "
          f"([A-Za-z0-9._-]), so a non-ASCII name is doubly disadvantaged "
          f"(tokenization gap AND dictionary gap), not just a dictionary miss:")
    print(f"  ASCII-only tokens:     {ascii_hit}/{ascii_total} = {ascii_hit/ascii_total:.1%}")
    print(f"  Non-ASCII tokens:      {nonascii_hit}/{nonascii_total} = {nonascii_hit/nonascii_total:.1%}"
          f"  (partly a tokenization gap, not purely a dictionary gap)")


if __name__ == "__main__":
    main()
