"""
Engineering upgrade 16 (2026-09, BUGS_AND_FIXES.md). Found via
Zookeeper's real-IP validation condition, run for the first time after
download_loghub.sh's missing-file gap was fixed (Engineering upgrade 14):
with the Luhn fix (Engineering upgrade 15) already closing the dominant
CREDIT_CARD collision, 140 false positives remained -- ALL type PERSON,
130 of them (93%) the exact same recurring string,
"QuorumPeer[myid=1]/0:0:0:0:0:0:0:0:2181" -- a Log4j-style bracketed
logger/thread context tag (class name + config + an IPv6 zero-address +
a port), not natural language, that spaCy's NER misreads as a person's
name. Root-caused via a dedicated diagnostic
(validation/real_data/diagnose_zookeeper_false_positives.py, no
fabricated hypothesis -- ran the real ensemble and counted).

Fix: src/detect.py's scan_ner() now discards a PERSON hit if its matched
text contains any of `[ ] / : $ @` -- punctuation that never appears
inside a real name or username under any of this project's own supported
conventions (a spaced "First Last", or a flat concatenated "firstlast",
including Faker-generated usernames that sometimes append digits).
Digits were deliberately NOT added to this exclusion set: a username
containing a digit is a real, legitimate case this project's own
synthetic corpus produces (Faker's user_name() provider), and this
project's sandbox had no way to re-verify that empirically when this fix
was written -- unlike the six punctuation characters here, which carry
no equivalent risk.

This test checks, with no live corpus/spaCy-avoidance tricks (spaCy IS
required, same as production scan_ner()):
  1. The exact real collision found live is now excluded.
  2. A structurally similar but different bracketed logger-context tag
     is also excluded (guards against an overfit fix that only matches
     the one literal string).
  3. A normal, real spaced person name is NOT affected by this filter
     (the actual regression risk: over-broad exclusion swallowing real
     PERSON hits, not just failing to swallow the bug).

Run: python validation/person_structural_exclusion_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import detect  # noqa: E402


def person_hits(text):
    return [(h["start"], h["end"], text[h["start"]:h["end"]])
            for h in detect.scan_ner(text) if h["type"] == "PERSON"]


def main():
    checks = []

    # 1. The exact real collision found live against Zookeeper_2k.log.
    line = ("2015-08-18 16:09:15,099 - INFO  "
            "[QuorumPeer[myid=1]/0:0:0:0:0:0:0:0:2181:Environment@100] - "
            "Server environment:os.name=Linux")
    hits = person_hits(line)
    ok = not any("QuorumPeer" in text for _, _, text in hits)
    checks.append(("real Zookeeper QuorumPeer context tag excluded", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] real Zookeeper QuorumPeer context tag excluded -> {hits}")

    # 2. A different, but structurally similar, bracketed logger-context
    # tag -- guards against a fix narrow enough to only match the one
    # literal string this bug happened to surface.
    line = "[WorkerSender[myid=2]/10.0.0.5:2888:QuorumCnxManager$Listener@493] - Received connection"
    hits = person_hits(line)
    ok = not any("WorkerSender" in text for _, _, text in hits)
    checks.append(("different bracketed logger-context tag also excluded", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] different bracketed logger-context tag also excluded -> {hits}")

    # 3. A real, ordinary spaced person name -- must NOT be affected.
    # This is the actual regression risk: an over-broad structural filter
    # swallowing legitimate PERSON hits, not just failing to swallow the
    # bug it was written for.
    line = "User John Smith logged in from the admin console."
    hits = person_hits(line)
    ok = any(text == "John Smith" for _, _, text in hits)
    checks.append(("ordinary spaced person name still detected", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] ordinary spaced person name still detected -> {hits}")

    print("\n=== Summary ===")
    ok_all = all(ok for _, ok in checks)
    for name, ok in checks:
        if not ok:
            print(f"  FAILED: {name}")
    print("ALL CHECKS PASSED" if ok_all else "SOME CHECKS FAILED")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
