"""
Layer 4: flattened-username name detection via dictionary segmentation.

Built directly in response to the project's single most important measured
finding (README, Section 4): Presidio's NER catches 98.8% of space-separated
names ("Timothy Wong") but only 5.9% of flattened username-style tokens with
no whitespace ("donaldgarcia"), because general-purpose NER expects sentence
structure that log-format usernames don't have.

Approach: rather than trying to make a sentence-level NER model understand a
structureless token, treat it as a compound-word segmentation problem. Given
a token, try every split point and check whether the left part is a known
first name and the right part a known last name (or vice versa). This is a
standard technique for compound segmentation against a name corpus, not a
sentence-understanding problem, so it doesn't need spaCy/Presidio at all.

Known, stated limitation, not hidden: the name dictionary here is Faker's own
first_names/last_names provider data, the same generator used to build this
project's synthetic corpus. That makes any recall measured against the
synthetic corpus optimistic in a way that will not transfer 1:1 to real
production usernames drawn from a different population -- someone named
"Zhiwei Tan" or "Aoife O'Sullivan" is not in Faker's default en_US list and
this layer will miss them exactly as regex/NER already do. This layer is a
real improvement, not a solved problem: swap in a larger, more representative
name corpus (e.g. US Census surname/given-name frequency lists, or a
locale-appropriate list for the deployment's actual user population) before
trusting this measurement to generalize past this specific synthetic dataset.
Validating this layer against the real Loghub datasets already used
elsewhere in this project (see validation/real_data/) is the concrete next
step to check whether the gain here is real or a dictionary-matches-itself
artifact -- not yet done as of this commit.
"""
import re

from faker.providers.person.en_US import Provider as _EnUSPersonProvider

FIRST_NAMES = {n.lower() for n in _EnUSPersonProvider.first_names}
LAST_NAMES = {n.lower() for n in _EnUSPersonProvider.last_names}

MIN_PART_LEN = 3
MIN_TOKEN_LEN = 6
MAX_TOKEN_LEN = 30

# Candidate tokens: runs of letters, optionally with a separator (. _ -)
# or trailing digits, the shapes Faker's own user_name() provider and real
# username conventions both actually produce.
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9._-]{5,29}\b")
_TRAILING_DIGITS_RE = re.compile(r"\d+$")


def _strip_trailing_digits(token: str) -> str:
    return _TRAILING_DIGITS_RE.sub("", token)


def _segment_match(token: str) -> bool:
    """True if token splits cleanly into <first name><last name> (either
    order) with no separator, both parts meeting the minimum length."""
    lower = token.lower()
    n = len(lower)
    for split in range(MIN_PART_LEN, n - MIN_PART_LEN + 1):
        left, right = lower[:split], lower[split:]
        if (left in FIRST_NAMES and right in LAST_NAMES) or \
           (left in LAST_NAMES and right in FIRST_NAMES):
            return True
    return False


def _separator_match(token: str) -> bool:
    """True if a separator-delimited token (firstname.lastname,
    firstname_lastname) has parts that are both known name parts."""
    parts = re.split(r"[._-]", token.lower())
    if len(parts) != 2:
        return False
    a, b = parts
    if len(a) < MIN_PART_LEN or len(b) < MIN_PART_LEN:
        return False
    return (a in FIRST_NAMES and b in LAST_NAMES) or (a in LAST_NAMES and b in FIRST_NAMES)


def scan_flattened_names(text: str) -> list[dict]:
    """Returns PERSON hits for flattened/compound username-style tokens that
    regex and whitespace-dependent NER both structurally cannot catch."""
    hits = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        if not (MIN_TOKEN_LEN <= len(raw) <= MAX_TOKEN_LEN):
            continue

        # A name-shaped token immediately followed by '@' is an email
        # local-part (fake.email() derives from a name, so this fires
        # constantly), not a standalone username -- the EMAIL regex layer
        # already owns that span with its own type. Skip it here rather
        # than double-flagging it as PERSON and colliding with the actual
        # EMAIL gold span. Found empirically: this was the single largest
        # source of false positives before this check was added (see
        # BUGS_AND_FIXES.md).
        if text[m.end():m.end() + 1] == "@":
            continue

        if "." in raw or "_" in raw or "-" in raw:
            matched = _separator_match(raw)
        else:
            core = _strip_trailing_digits(raw)
            if len(core) < MIN_TOKEN_LEN:
                continue
            matched = _segment_match(core)
            if matched:
                # report only the matched core span, not any trailing digits
                end = m.start() + len(core)
                hits.append({"type": "PERSON", "start": m.start(), "end": end,
                             "method": "flattened_name_dict"})
                continue

        if matched:
            hits.append({"type": "PERSON", "start": m.start(), "end": m.end(),
                         "method": "flattened_name_dict"})
    return hits
