"""
Adversarial evasion-testing experiment (manuscript-revision follow-up,
task #24 of the "all three, in that order" plan: detection tradeoffs
synthesis -> taxonomy reference -> this).

Goal: craft inputs designed to evade REDACT's real detection layers
(src/detect.py's regex, Presidio NER, entropy, and flattened-name
dictionary layers), measure the evasion rate per technique, and report
it honestly -- including where evasion succeeds completely.

Method: for each PII span in the REAL ground-truth corpus
(data/synthetic_logs.jsonl, the same corpus DETECTION_PERFORMANCE_
TRADEOFFS.md's headline numbers were measured against), substitute the
original matched value with a transformed variant designed to evade one
specific detection mechanism, and record where in the line it now sits.
This is substitution against real log lines and real ground-truth spans,
not synthetic evasion strings assembled in isolation -- the surrounding
log structure (field names, punctuation, adjacent tokens) stays exactly
as this project's own corpus generator produced it.

Every technique here targets a SPECIFIC, DOCUMENTED mechanism in
src/detect.py (cited in each function's docstring below) -- this is not
a generic fuzzer. Techniques that don't correspond to a real mechanism
in the shipped code are not included, per this session's "don't build
speculative features unrelated to the asks" constraint.
"""
import json
import os
import random
import re
import sys
from pathlib import Path

random.seed(20260911)

_HERE = os.path.dirname(__file__)
CORPUS_PATH = Path(os.path.join(_HERE, "..", "..", "data", "synthetic_logs.jsonl"))
OUT_PATH = Path(_HERE) / "evasion_corpus.jsonl"


# --------------------------------------------------------------------------
# Evasion techniques, one function per (type, technique) pair. Each takes
# the original matched value and returns a transformed value, or None if
# the technique doesn't apply to this particular value (e.g. an IP octet
# pattern that requires all-decimal octets < 256).
#
# Every technique is annotated with WHICH detect.py mechanism it targets
# and WHY it's expected to matter, not left to speak for itself.
# --------------------------------------------------------------------------

def ssn_no_dashes(v):
    # Targets: REGEX_PATTERNS["SSN"] = r"\b\d{3}-\d{2}-\d{4}\b" -- hyphens
    # are literal, not optional. A 9-digit run with no separators does not
    # match this pattern at all. Presidio's US_SSN recognizer is a
    # separate, independent mechanism (own context/pattern rules) so this
    # tests the regex layer specifically, not the whole ensemble by
    # assumption.
    digits = re.sub(r"\D", "", v)
    return digits if len(digits) == 9 else None


def ssn_spaced(v):
    # Same target as ssn_no_dashes, different literal separator.
    digits = re.sub(r"\D", "", v)
    return f"{digits[0:3]} {digits[3:5]} {digits[5:9]}" if len(digits) == 9 else None


def ssn_dotted(v):
    digits = re.sub(r"\D", "", v)
    return f"{digits[0:3]}.{digits[3:5]}.{digits[5:9]}" if len(digits) == 9 else None


def credit_card_spaced_groups(v):
    # Targets: REGEX_PATTERNS["CREDIT_CARD"] = r"\b\d{12,19}\b" -- requires
    # one CONTIGUOUS digit run. Splitting into human-readable 4-digit
    # groups (how card numbers are actually written/typed in free text)
    # breaks the \b...\b contiguous-run match entirely -- no partial credit
    # for regex, since each individual group of 4 digits is far short of
    # the 12-digit floor.
    digits = re.sub(r"\D", "", v)
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return " ".join(groups)


def credit_card_dashed_groups(v):
    digits = re.sub(r"\D", "", v)
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return "-".join(groups)


def email_obfuscated_at_dot(v):
    # Targets: REGEX_PATTERNS["EMAIL"] requires a literal "@" and ".".
    # "[at]"/"[dot]" obfuscation is a real, well-documented technique
    # (originally used by humans to defeat spam-harvesting bots posting to
    # public forums) that also defeats a literal-character regex, and
    # would defeat Presidio's EMAIL_ADDRESS recognizer for the same
    # structural reason -- neither layer has a semantic notion of "at"
    # meaning "@".
    local, _, domain = v.partition("@")
    domain_dotted = domain.replace(".", " [dot] ")
    return f"{local} [at] {domain_dotted}"


def email_zero_width_split(v):
    # Targets the same regex/NER literal-character dependency, via a
    # different real-world technique: inserting a zero-width space
    # (U+200B, invisible when rendered/printed, does not change what a
    # human reading a terminal or log viewer sees) between the "@" and the
    # domain. \b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b requires "@" to be
    # immediately followed by a `[\w-]` character; a zero-width space in
    # between breaks that adjacency for the regex while remaining
    # visually identical to the original address in any terminal, log
    # viewer, or text editor that renders it (confirmed: U+200B has no
    # visible glyph in standard rendering).
    local, _, domain = v.partition("@")
    return f"{local}@​{domain}"


def ip_defanged(v):
    # Targets: REGEX_PATTERNS["IP"] = r"\b(?:\d{1,3}\.){3}\d{1,3}\b" --
    # requires literal "." between octets. "Defanging" (replacing "." with
    # "[.]") is a REAL, widely-used convention in security/threat-intel
    # writing specifically so IOCs don't render as clickable links or
    # trigger automated tools -- meaning this isn't a contrived evasion,
    # it's a documented convention this project's own target domain
    # (SOC/security telemetry) already uses routinely, sometimes for
    # entirely benign reasons (an analyst pasting an IOC into a ticket),
    # which makes false-negative risk here a real operational concern,
    # not just an adversarial one.
    return v.replace(".", "[.]")


def ip_octal_padded(v):
    # Targets the same regex's literal decimal-octet assumption: an IP
    # address parsed by most OS network stacks accepts zero-padded octets
    # (leading zeros), which some parsers additionally interpret as octal.
    # \d{1,3} still matches a 3-digit zero-padded octet numerically (e.g.
    # "192" -> "192" is unchanged), so this technique specifically needs
    # 1-2 digit octets padded to 3 to test whether padding alone (without
    # changing digit COUNT) has any effect -- included as a documented
    # near-miss test even though \d{1,3} is expected to still match it;
    # the point is to confirm that expectation empirically rather than
    # assume it.
    parts = v.split(".")
    if len(parts) != 4:
        return None
    return ".".join(p.zfill(3) for p in parts)


def mrn_lowercase_no_dash(v):
    # Targets: REGEX_PATTERNS["MRN"] = r"\bMRN-\d{7}\b" -- both the
    # literal "MRN-" prefix casing and the hyphen are exact-match
    # requirements with no case-insensitivity flag and no separator
    # flexibility.
    m = re.match(r"MRN-(\d{7})", v)
    return f"mrn{m.group(1)}" if m else None


def mrn_spelled_out(v):
    m = re.match(r"MRN-(\d{7})", v)
    return f"medical record number {m.group(1)}" if m else None


def person_leetspeak(v):
    # Targets: Presidio's NER model expects natural-language capitalized
    # tokens; character substitution (a->4, e->3, i->1, o->0) is a
    # long-documented technique against keyword/pattern filters and is
    # expected to also disrupt tokenization/embedding lookup for a name
    # the model has never seen in this exact spelling, without affecting
    # scan_regex (names were never regex-matched) or scan_entropy (a short
    # substituted name is well under entropy's 12-char min_len floor in
    # most cases -- flagged per-record below, not assumed).
    table = str.maketrans("aeioAEIO", "43104310")
    return v.translate(table)


def person_unicode_homoglyph(v):
    # Targets the same NER tokenization dependency via a different real
    # technique: substituting Cyrillic lookalike characters (а=U+0430,
    # е=U+0435, о=U+043E -- visually near-identical to Latin a/e/o in most
    # fonts, a real, documented technique used in phishing/typosquatting)
    # for their Latin counterparts. Confirmed distinct codepoints, not a
    # cosmetic no-op: Python's str equality and spaCy's tokenizer both
    # operate on codepoints, not rendered glyphs.
    table = str.maketrans("aeoAEO", "а е о А Е О".replace(" ", ""))
    return v.translate(table)


def person_all_caps(v):
    # Targets NER's reliance on capitalization patterns as a feature:
    # standard English NER training data overwhelmingly represents names
    # in Title Case; ALL CAPS is a real, common format in log output
    # (many Windows Event Log and mainframe-style fields render usernames
    # upper-cased) that removes the Title Case signal entirely without
    # changing the string's information content at all.
    return v.upper()


def flattened_name_digit_insertion(v):
    # Targets: src/flattened_names.py's dictionary segmentation, which
    # tries every split point of a flattened token and checks whether
    # each side is a known first/last name. Inserting a single digit
    # between the two name halves (a real, common convention for
    # disambiguating username collisions, e.g. "johnsmith2") breaks the
    # exact-split assumption: neither "johnsmith2"'s prefix "johnsmith2"
    # (whole token, digit included) nor any split point around the
    # inserted digit corresponds to two valid dictionary words, since the
    # digit isn't a valid character in either name.
    if " " in v or len(v) < 4:
        return None
    mid = len(v) // 2
    return v[:mid] + str(random.randint(0, 9)) + v[mid:]


TECHNIQUES = {
    "SSN": [("no_dashes", ssn_no_dashes), ("spaced", ssn_spaced), ("dotted", ssn_dotted)],
    "CREDIT_CARD": [("spaced_groups", credit_card_spaced_groups),
                     ("dashed_groups", credit_card_dashed_groups)],
    "EMAIL": [("at_dot_obfuscation", email_obfuscated_at_dot),
              ("zero_width_split", email_zero_width_split)],
    "IP": [("defanged", ip_defanged), ("octal_padded", ip_octal_padded)],
    "MRN": [("lowercase_no_dash", mrn_lowercase_no_dash), ("spelled_out", mrn_spelled_out)],
    "PERSON": [("leetspeak", person_leetspeak),
               ("unicode_homoglyph", person_unicode_homoglyph),
               ("all_caps", person_all_caps),
               ("flattened_digit_insertion", flattened_name_digit_insertion)],
}


def main():
    if not CORPUS_PATH.exists():
        print(f"ERROR: ground-truth corpus not found at {CORPUS_PATH}", file=sys.stderr)
        sys.exit(1)

    records = [json.loads(line) for line in open(CORPUS_PATH)]
    print(f"Loaded {len(records)} ground-truth records from {CORPUS_PATH}")

    out = []
    record_id = 0
    skipped_not_applicable = 0
    for rec in records:
        text = rec["log"]
        log_type = rec.get("log_type")
        for span in rec.get("pii", []):
            ptype = span["type"]
            if ptype not in TECHNIQUES:
                continue
            original_value = text[span["start"]:span["end"]]
            for technique_name, fn in TECHNIQUES[ptype]:
                variant_value = fn(original_value)
                if variant_value is None:
                    skipped_not_applicable += 1
                    continue
                new_text = text[:span["start"]] + variant_value + text[span["end"]:]
                new_start = span["start"]
                new_end = span["start"] + len(variant_value)
                out.append({
                    "record_id": record_id,
                    "log_type": log_type,
                    "pii_type": ptype,
                    "technique": technique_name,
                    "original_value": original_value,
                    "variant_value": variant_value,
                    "log": new_text,
                    "span": {"start": new_start, "end": new_end, "type": ptype},
                })
                record_id += 1

    with open(OUT_PATH, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")

    print(f"Wrote {len(out)} evasion-variant records to {OUT_PATH}")
    print(f"Skipped {skipped_not_applicable} (original value not shaped in a way "
          f"the technique applies to -- e.g. a non-4-digit-aligned card number)")


if __name__ == "__main__":
    main()
