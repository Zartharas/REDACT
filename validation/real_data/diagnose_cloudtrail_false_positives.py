"""
Root-causes the cloudtrail false-positive spike found 2026-08-10
(P=0.310, FP=4627 on 2000 real flaws.cloud events -- naive and
field-gated came back nearly identical, FP 4627 vs 4562, which already
rules out anything field-gating's excision logic would touch).

HYPOTHESIS, confirmed mechanically (not yet confirmed against the actual
data volume): AWS account IDs are always exactly 12 digits, which sits
squarely inside src/detect.py's CREDIT_CARD regex range (`\\d{12,19}`).
A real CloudTrail event's userIdentity.accountId field, AND the same
12-digit account ID embedded inside the arn field (colons count as word
boundaries for \\b), both independently trigger a false CREDIT_CARD
match -- a systematic collision between a real AWS identifier format and
this project's regex, not a diffuse NER weakness. This is exactly the
same shape as the "rhost=" dangling-key bug found earlier this project
(a specific, mechanical, fixable cause), just a different collision.

Run from validation/real_data/ (same convention as inject_and_evaluate.py):
    python3 diagnose_cloudtrail_false_positives.py

No spaCy needed -- pure regex counting, confirms or refutes the
hypothesis's MAGNITUDE before anything gets "fixed" based on a plausible-
sounding but unconfirmed theory.
"""
import json
import os
import re

CLOUDTRAIL_RAW_PATH = os.path.join('datasets', 'CloudTrailFlaws_raw.jsonl')
CREDIT_CARD_RE = re.compile(r'\b\d{12,19}\b')
ACCOUNT_ID_RE = re.compile(r'^\d{12}$')


def main():
    if not os.path.exists(CLOUDTRAIL_RAW_PATH):
        print(f"{CLOUDTRAIL_RAW_PATH} not found -- run prepare_cloudtrail_dataset.py first.")
        return

    total_lines = 0
    total_cc_hits = 0
    lines_with_account_id_field = 0
    cc_hits_matching_a_real_account_id = 0

    with open(CLOUDTRAIL_RAW_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            obj = json.loads(line)
            account_id = obj.get('userIdentity', {}).get('accountId')
            if account_id and ACCOUNT_ID_RE.match(account_id):
                lines_with_account_id_field += 1

            text = json.dumps(obj)
            cc_hits = CREDIT_CARD_RE.findall(text)
            total_cc_hits += len(cc_hits)
            if account_id:
                cc_hits_matching_a_real_account_id += sum(1 for h in cc_hits if h == account_id)

    print(f"Lines: {total_lines}")
    print(f"Lines with a userIdentity.accountId field: {lines_with_account_id_field} "
          f"({lines_with_account_id_field/total_lines:.1%})")
    print(f"Total CREDIT_CARD-shaped (12-19 digit) matches across all lines: {total_cc_hits}")
    print(f"...of which exactly equal a real accountId value on that same line: "
          f"{cc_hits_matching_a_real_account_id}")
    print()
    print("If cc_hits_matching_a_real_account_id is a large fraction of the FP count")
    print("reported by inject_and_evaluate.py (4627 naive), that confirms the AWS")
    print("account-ID / CREDIT_CARD regex collision as the dominant false-positive")
    print("source -- both directly (the accountId field) and via the arn field")
    print("(same 12-digit ID, colon-delimited, also matches \\b\\d{12,19}\\b).")


if __name__ == '__main__':
    main()
