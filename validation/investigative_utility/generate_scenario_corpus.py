"""
A small, purpose-built supplementary corpus for the privacy-vs-investigative-
utility experiment (see README.md in this directory).

Why this corpus exists and data/synthetic_logs.jsonl (the main 10,000-line
corpus) doesn't already answer this question: that corpus's own generator
(src/generate_logs.py) calls make_slots() fresh, independently, for every
single record -- every IP, SSN, credit-card number, and email is Faker-
generated per-record and essentially never repeats (confirmed directly:
2,327 IP mentions -> 2,327 unique values, 0 appearing twice; same for SSN,
CREDIT_CARD, MRN). Only PERSON has any real recurrence (353 of 2,629 unique
names appear more than once, accounting for 717 of 2,993 mentions), because
Faker's name pool is small enough to coincidentally repeat, not because the
generator intended it to.

That's a real limitation, not a detail: an investigator's actual task is
"has this IP/user/card shown up before, and can I get back to the real
value" -- a question about RECURRENCE. A corpus where 4 of 5 entity types
essentially never recur can't honestly support measuring that. This
generator exists to fix that gap directly: a small number of "actor" values
per type are deliberately reused across many records (simulating the same
source IP, user, or card appearing repeatedly across an incident timeline),
mixed with one-off "noise" values of the same types (simulating unrelated
background activity an investigator needs to NOT conflate with the actors
under investigation).

This is deliberately small (a few hundred lines) and deliberately synthetic
-- it exists only to generate a measurable recurrence pattern for the
correlation/reversibility experiment in measure_investigative_utility.py,
not to serve as a second general-purpose validation corpus. It reuses this
project's existing template/slot machinery (src/generate_logs.py) rather
than reinventing it.
"""
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import generate_logs as gl  # noqa: E402

random.seed(1337)
gl.fake.seed_instance(1337)

# Actor pools: values deliberately reused across many records. Sizes are
# small and uneven on purpose -- a real incident timeline involves a handful
# of actors of interest, not dozens, and different types plausibly recur at
# different rates (an IP tied to a scanning/brute-force source recurs far
# more often over a short window than, say, a credit card number tied to a
# single fraud ring testing a batch of stolen numbers).
N_RECORDS = 400
RECURRING_IPS = [gl.fake.ipv4_public() for _ in range(8)]
RECURRING_PERSON_NAMES = [gl.fake.name() for _ in range(6)]
RECURRING_PERSON_FLAT = [gl.fake.user_name() for _ in range(6)]
RECURRING_EMAILS = [gl.fake.email() for _ in range(4)]
RECURRING_SSNS = [gl.fake.ssn() for _ in range(3)]
RECURRING_CREDIT_CARDS = [gl.fake.credit_card_number() for _ in range(3)]
RECURRING_MRNS = [f"MRN-{gl.fake.random_number(digits=7, fix_len=True)}" for _ in range(3)]

# Probability that a given PII-bearing slot draws from the recurring pool
# rather than generating a fresh one-off noise value. 0.75 means roughly
# three-quarters of mentions belong to a recurring actor -- deliberately the
# INVERSE of the main corpus's ~0%, since this corpus's whole purpose is
# giving the correlation metric something real to measure. The remaining
# noise fraction still matters: it's what tests whether a method creates
# FALSE correlations between genuinely unrelated one-off entities.
POOL_PROBABILITY = 0.75

POOLS = {
    "IP": RECURRING_IPS,
    "PERSON_name": RECURRING_PERSON_NAMES,
    "PERSON_flat": RECURRING_PERSON_FLAT,
    "EMAIL": RECURRING_EMAILS,
    "SSN": RECURRING_SSNS,
    "CREDIT_CARD": RECURRING_CREDIT_CARDS,
    "MRN": RECURRING_MRNS,
}

# Track a running count of *fresh* noise values minted per pool-key so noise
# values are guaranteed unique (never accidentally re-drawing the same noise
# value twice, which would silently and wrongly count as a "recurring
# actor" when scoring).
_noise_counters: dict[str, int] = {}


def _fresh_noise(pool_key: str) -> str:
    _noise_counters[pool_key] = _noise_counters.get(pool_key, 0) + 1
    n = _noise_counters[pool_key]
    if pool_key == "IP":
        return f"10.{200 + (n % 50)}.{n % 256}.{(n * 7) % 256}"
    if pool_key == "PERSON_name":
        return f"{gl.fake.first_name()} Noise{n}"
    if pool_key == "PERSON_flat":
        return f"noiseuser{n}"
    if pool_key == "EMAIL":
        return f"noise.contact{n}@example.org"
    if pool_key == "SSN":
        return f"900-{n:02d}-{(n * 3) % 10000:04d}"
    if pool_key == "CREDIT_CARD":
        return f"4{(1000000000000 + n):012d}"[:16]
    if pool_key == "MRN":
        return f"MRN-9{n:06d}"
    raise ValueError(pool_key)


def make_scenario_slots() -> dict:
    """Same shape as generate_logs.make_slots(), but the PII-bearing values
    are drawn from a fixed recurring pool (POOL_PROBABILITY of the time) or
    minted as a guaranteed-unique noise value otherwise, instead of being
    fresh Faker output every call."""
    slots = gl.make_slots()  # keeps num/pid/port/ts filler exactly as-is

    def pick(pool_key: str) -> str:
        if random.random() < POOL_PROBABILITY:
            return random.choice(POOLS[pool_key])
        return _fresh_noise(pool_key)

    slots["IP_addr"] = pick("IP")
    slots["PERSON_name"] = pick("PERSON_name")
    slots["PERSON_name_flat"] = pick("PERSON_flat")
    slots["EMAIL_addr"] = pick("EMAIL")
    slots["SSN_num"] = pick("SSN")
    slots["CREDIT_CARD_num"] = pick("CREDIT_CARD")
    slots["MRN_id"] = pick("MRN")
    return slots


def build_entry(log_type: str, dirty: bool) -> dict:
    slots = make_scenario_slots()
    if not dirty:
        template = random.choice(gl.CLEAN_TEMPLATES[log_type])
        text = template.format(**slots)
        return {"log": text, "log_type": log_type, "pii": []}

    if log_type == "windows_event":
        template, used_keys = random.choice(gl.WINDOWS_TEMPLATES)
    elif log_type == "syslog":
        template, used_keys = random.choice(gl.SYSLOG_TEMPLATES)
    else:
        template, used_keys = random.choice(gl.CLOUDTRAIL_TEMPLATES)

    text, spans = gl.render(template, slots, used_keys)
    return {"log": text, "log_type": log_type, "pii": spans}


def main(n: int, out_path: str, dirty_ratio: float = 0.85):
    # dirty_ratio is much higher than generate_logs.py's default (0.3):
    # this corpus's only job is producing scoreable recurring/noise PII
    # mentions, so mostly-clean filler lines add nothing here.
    log_types = ["windows_event", "syslog", "cloudtrail"]
    with open(out_path, "w") as f:
        for i in range(n):
            log_type = random.choice(log_types)
            dirty = random.random() < dirty_ratio
            entry = build_entry(log_type, dirty)
            entry["record_id"] = i
            f.write(json.dumps(entry) + "\n")

    pools_path = os.path.join(os.path.dirname(out_path), "actor_pools.json")
    with open(pools_path, "w") as f:
        json.dump({
            "IP": RECURRING_IPS,
            "PERSON": RECURRING_PERSON_NAMES + RECURRING_PERSON_FLAT,
            "EMAIL": RECURRING_EMAILS,
            "SSN": RECURRING_SSNS,
            "CREDIT_CARD": RECURRING_CREDIT_CARDS,
            "MRN": RECURRING_MRNS,
        }, f, indent=2)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "scenario_corpus.jsonl")
    main(N_RECORDS, out)
    print(f"Wrote {N_RECORDS} entries to {out}")
