"""
Bug 17 (2026-08-10). Found while extending real-data validation (see
validation/real_data/prepare_cloudtrail_dataset.py and
diagnose_cloudtrail_false_positives.py) to a real, publicly released
CloudTrail dataset: running the naive/field-gated ensemble against 2,000
real flaws.cloud events measured P=0.310 (FP=4627) -- a precision
collapse far worse than any other real-data condition tested this
project (OpenSSH FP=49, Linux FP=122).

Root cause, confirmed via diagnose_cloudtrail_false_positives.py against
the real data: AWS account IDs are always exactly 12 digits, sitting at
the low end of src/detect.py's CREDIT_CARD regex range (\\d{12,19}). A
real CloudTrail event's userIdentity.accountId field, AND the same
12-digit ID embedded in the arn field (colons satisfy \\b same as
whitespace), each independently trigger a false CREDIT_CARD match.
Confirmed live: 3,775 of 4,627 false positives (81.6%) were exact
matches of a real accountId value on the same line.

Fix: NOT a blanket \\d{12,19} -> \\d{13,19} narrowing -- confirmed via
Faker.credit_card_number() (2,000 seeded calls) that this project's own
synthetic corpus generator (src/generate_logs.py's CREDIT_CARD_num slot)
legitimately produces exactly-12-digit values sometimes, so narrowing
the range would silently regress the project's own already-measured
synthetic CREDIT_CARD recall to fix a real-data problem. Instead added a
narrow, context-aware exclusion in src/detect.py's scan_regex(): a
12-digit CREDIT_CARD match is suppressed ONLY when immediately preceded
by an AWS ARN's account-ID position (arn:aws...::<12digits>:) or a
"[recipientA]ccountId": JSON key -- both structurally specific enough
that a real credit card number could not coincidentally match. 13-19
digit matches, and any 12-digit match NOT in one of these two contexts
(including Faker's own synthetic values), are completely unaffected.

This test checks, with no spaCy/Docker/live data required:
  1. Both real collision shapes (arn field, accountId JSON key) are
     suppressed.
  2. A bare 12-digit number with NO AWS context is still detected
     (guards against the exclusion silently over-matching and eating
     Faker's own synthetic 12-digit CREDIT_CARD values).
  3. 13-19 digit numbers in an AWS context are NOT suppressed (AWS
     account IDs are never that length; a real card number embedded
     near "arn:aws" text should not be swallowed by this fix).
  4. A verbatim real accountId/arn pair, structured the way
     prepare_cloudtrail_dataset.py's trimmed real records actually are.

Run: python validation/aws_account_id_credit_card_exclusion_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import detect  # noqa: E402


def types_at(text):
    return {(h["type"], text[h["start"]:h["end"]]) for h in detect.scan_regex(text)}


def main():
    checks = []

    # 1a. Real collision shape: accountId JSON key (verbatim structure,
    # not a made-up account number, matching a real trimmed CloudTrail
    # record as prepare_cloudtrail_dataset.py writes it).
    line = '{"eventName": "ConsoleLogin", "userIdentity": {"accountId": "811596193553", "userName": "backup"}}'
    hits = types_at(line)
    ok = ("CREDIT_CARD", "811596193553") not in hits
    checks.append(("accountId JSON key suppressed", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] accountId JSON key suppressed -> {hits}")

    # 1b. Real collision shape: same account ID embedded in an arn field.
    line = '{"userIdentity": {"arn": "arn:aws:iam::811596193553:user/Level6"}}'
    hits = types_at(line)
    ok = ("CREDIT_CARD", "811596193553") not in hits
    checks.append(("arn field suppressed", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] arn field suppressed -> {hits}")

    # 1c. recipientAccountId key (the other real CloudTrail field name
    # that carries a 12-digit account ID in some event types).
    line = '{"recipientAccountId": "811596193553"}'
    hits = types_at(line)
    ok = ("CREDIT_CARD", "811596193553") not in hits
    checks.append(("recipientAccountId key suppressed", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] recipientAccountId key suppressed -> {hits}")

    # 2. Bare 12-digit number, no AWS context at all -- must still be
    # detected. This is the regression guard for Faker's own synthetic
    # CREDIT_CARD_num values (confirmed to sometimes be 12 digits).
    line = "Card on file: 411111111111 expires 12/28"
    hits = types_at(line)
    ok = ("CREDIT_CARD", "411111111111") in hits
    checks.append(("bare 12-digit number still detected", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] bare 12-digit number still detected -> {hits}")

    # 3. A longer (16-digit) number sitting right next to AWS-looking
    # text -- must NOT be suppressed, since AWS account IDs are never
    # anything but exactly 12 digits.
    line = '{"arn": "arn:aws:iam::4111111111111111:user/x"}'
    hits = types_at(line)
    ok = ("CREDIT_CARD", "4111111111111111") in hits
    checks.append(("16-digit number near arn context still detected", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] 16-digit number near arn context still detected -> {hits}")

    # 4. A 12-digit number that merely follows some unrelated JSON key
    # (not accountId/recipientAccountId, not an arn) -- must still be
    # detected; the exclusion must not be so loose it swallows any
    # 12-digit value near a quote.
    line = '{"transactionId": "811596193553"}'
    hits = types_at(line)
    ok = ("CREDIT_CARD", "811596193553") in hits
    checks.append(("12-digit number under unrelated JSON key still detected", ok))
    print(f"  [{'OK' if ok else 'FAIL'}] 12-digit number under unrelated JSON key still detected -> {hits}")

    print("\n=== Summary ===")
    ok_all = all(ok for _, ok in checks)
    for name, ok in checks:
        if not ok:
            print(f"  FAILED: {name}")
    print("ALL CHECKS PASSED" if ok_all else "SOME CHECKS FAILED")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
