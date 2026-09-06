# Getting real PHI ground truth: n2c2 (formerly i2b2) de-identification corpus

REDACT's PHI claim currently rests on MRN-shaped regex alone (`\bMRN-\d{7}\b`)
-- there is no real, PHI-labeled ground truth anywhere in this project's
validation suite, unlike PERSON/IP/EMAIL/SSN/CREDIT_CARD, which all now have
at least one real-data condition (OpenSSH, Linux, Thunderbird, CloudTrail,
windows_event, OpenStack, Zookeeper -- see `inject_and_evaluate.py`). The
n2c2 2014 de-identification track corpus is the actual gold-standard
benchmark for this specific gap: real clinical notes, authentic PHI (all 18
HIPAA Safe Harbor categories, a strict superset of what REDACT currently
detects) replaced with realistic surrogates, with the original PHI spans
and their categories preserved as ground-truth annotations.

## Why this isn't a script this project can run for you

Every other real-data source this project uses (Loghub, flaws.cloud
CloudTrail) is a direct, anonymous download -- no registration, no
agreement, just a URL. n2c2 is different: it requires an individual data
use agreement (DUA) through Harvard DBMI, tied to your own identity/
affiliation. That's not something an assistant can complete on your
behalf -- account creation and entering personal/identifying information
into third-party forms are both outside what this project's tooling (or
this assistant) does automatically, by design, regardless of convenience.

## How to start it (as of this research pass, 2026-09)

1. Portal: <https://portal.dbmi.hms.harvard.edu/projects/n2c2-nlp/>
2. Data Use Agreement terms: <https://n2c2.dbmi.hms.harvard.edu/data-use-agreement>
3. Register on the DBMI portal, request access to the n2c2 NLP data sets
   project specifically (the 2014 de-identification track is what you
   want -- there are several years/tracks under the same portal umbrella;
   don't grab the wrong one).
4. **Known caveat, found during research:** at least one source reported
   the n2c2 datasets as "temporarily unavailable" via this portal as
   recently as mid-2026. If the portal shows the dataset as unavailable
   when you check, that's a real, disclosed platform issue, not something
   wrong with these instructions -- check back periodically, and consider
   watching the DBMI program page (<https://dbmi.hms.harvard.edu/programs/national-nlp-clinical-challenges-n2c2>)
   for status updates.
5. DUA review/approval is not instant. Start this now if you want it in
   hand before resubmission -- it is a "days to weeks" item, not a
   same-session one.

## What happens once you have it

Do NOT hand the raw files directly into this project's pipeline yet. The
right next step, once a real sample file is in hand, is to:
1. Read one real annotated record's actual structure (the i2b2/n2c2 2014
   format is documented publicly as BRAT-standoff or inline XML tags
   depending on release year/track -- but this project's own discipline is
   to confirm the *actual* file in hand rather than build a parser against
   a remembered/assumed shape, the same lesson Bug 17 and the Azure Bug
   (e) both reinforced the hard way).
2. Write `prepare_n2c2_dataset.py` (mirroring `prepare_cloudtrail_dataset.py`'s
   existing pattern: trim to a manageable sample, preserve real PHI-span
   ground truth, write to `validation/real_data/datasets/`) against that
   confirmed real structure.
3. Add a new `build_n2c2_corpus()` to `inject_and_evaluate.py`, evaluating
   REDACT's detection layer against real PHI spans directly (no injection
   needed, unlike the PERSON conditions -- the ground truth here is
   already real, authentic PHI, not synthetic values injected into a real
   field the way OpenSSH/Linux/Thunderbird/CloudTrail's PERSON conditions
   work).

This deliberately mirrors how OpenStack/Zookeeper's `IP_ONLY_DATASETS`
condition works (real ground truth used as-is, no injection) rather than
the injection-based conditions -- PHI surrogates in n2c2 are already real
substitutions of real PHI, exactly the kind of ground truth injection
exists to approximate for the *other* real datasets that don't have it
natively.
