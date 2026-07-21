"""
Consolidated validation run. Answers one question: does REDACT actually work,
end to end, right now, on this codebase? Not "was this tested at some point
in the conversation" but "does it still pass if you run it fresh."

Every check here is a real assertion against real output, not a print
statement that looks like a test. A check that fails makes the script exit
non-zero and print exactly what failed, rather than silently continuing.

Run with: python validate.py
"""
import sys
import os
import json
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

RESULTS = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def section(title: str):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # ------------------------------------------------------------------
    section("1. Synthetic corpus generation and ground-truth integrity")
    # ------------------------------------------------------------------
    import generate_logs
    generate_logs.main(n=3000, out_path="data/validate_corpus.jsonl", dirty_ratio=0.3)

    with open("data/validate_corpus.jsonl") as f:
        entries = [json.loads(l) for l in f]

    check("corpus size matches request", len(entries) == 3000, f"got {len(entries)}")

    offset_errors = 0
    for e in entries:
        for span in e["pii"]:
            extracted = e["log"][span["start"]:span["end"]]
            if not extracted or span["start"] < 0 or span["end"] > len(e["log"]):
                offset_errors += 1
    check("every ground-truth offset extracts a non-empty, in-range substring",
          offset_errors == 0, f"{offset_errors} bad offsets out of "
          f"{sum(len(e['pii']) for e in entries)} spans")

    dirty_count = sum(1 for e in entries if e["pii"])
    dirty_ratio = dirty_count / len(entries)
    check("dirty ratio lands near the requested 30%",
          0.25 <= dirty_ratio <= 0.35, f"actual ratio {dirty_ratio:.1%}")

    # ------------------------------------------------------------------
    section("2. Detection: does it find real PII, and only real PII?")
    # ------------------------------------------------------------------
    import evaluate
    per_type, elapsed = evaluate.run_evaluation(entries, use_ner=True, use_entropy_gate=False)
    metrics = evaluate.summarize(per_type, len(entries), elapsed, "Full ensemble")

    check("micro-average recall exceeds 60% (the ensemble finds most real PII)",
          metrics["micro_recall"] > 0.60, f"recall = {metrics['micro_recall']:.3f}")
    check("micro-average precision exceeds 50% (it isn't flagging mostly noise)",
          metrics["micro_precision"] > 0.50, f"precision = {metrics['micro_precision']:.3f}")

    # entity types with rigid formats should be caught essentially perfectly;
    # this is the floor, not a stretch goal
    for entity_type in ("EMAIL", "SSN", "CREDIT_CARD"):
        c = per_type.get(entity_type, {"tp": 0, "fp": 0, "fn": 0})
        recall = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else 0
        check(f"{entity_type} recall is at or near 100% (rigid regex-matchable format)",
              recall >= 0.98, f"recall = {recall:.3f}")

    # ------------------------------------------------------------------
    section("3. Anonymization: correlation, reversibility, and JSON validity")
    # ------------------------------------------------------------------
    import anonymize
    key = "validation-run-key"
    store = anonymize.TokenStore("output/validate_token_store.json")

    # correlation-preservation: same IP -> same pseudonym, every time
    ip_tokens = {}
    correlation_ok = True
    for e in entries[:1000]:
        if not e["pii"]:
            continue
        out = anonymize.pseudonymize(e["log"], e["pii"], key=key)
        for span in e["pii"]:
            if span["type"] != "IP":
                continue
            original = e["log"][span["start"]:span["end"]]
            start = out.find("ip_")
            token = out[start:start+15] if start != -1 else None
            if original in ip_tokens and token and ip_tokens[original] != token:
                correlation_ok = False
            elif token:
                ip_tokens[original] = token
    check("pseudonymization is correlation-preserving (same input -> same token, always)",
          correlation_ok, f"checked {len(ip_tokens)} distinct IPs")

    # tokenize/detokenize round trip
    round_trip_ok = True
    checked = 0
    for e in entries[:500]:
        if not e["pii"]:
            continue
        tokenized = anonymize.tokenize(e["log"], e["pii"], store=store)
        restored = anonymize.detokenize(tokenized, store=store)
        checked += 1
        if restored != e["log"]:
            round_trip_ok = False
    check("tokenize -> detokenize reproduces the original string exactly",
          round_trip_ok, f"checked {checked} entries")

    # JSON validity after transformation (the bug that was found and fixed)
    broken_json = 0
    checked_json = 0
    for e in entries:
        if e["log_type"] != "cloudtrail" or not e["pii"]:
            continue
        checked_json += 1
        spans = anonymize.dedup_spans(e["pii"])
        out = anonymize.anonymize_by_policy(e["log"], spans, key=key, store=store)
        try:
            json.loads(out)
        except json.JSONDecodeError:
            broken_json += 1
    check("anonymized CloudTrail output remains valid JSON (regression check for the "
          "overlapping-span bug)", broken_json == 0,
          f"{broken_json} broken out of {checked_json} checked")

    # ------------------------------------------------------------------
    section("4. Audit trail: signatures verify, and tampering is caught")
    # ------------------------------------------------------------------
    import audit
    audit_key = "validation-audit-key"
    events = []
    for e in entries[:200]:
        for span in e["pii"]:
            original = e["log"][span["start"]:span["end"]]
            events.append(audit.build_audit_event(
                field_type=span["type"], method="tokenize", policy_version="v-test",
                original_value=original, audit_key=audit_key))

    all_valid = all(audit.verify_audit_event(ev, audit_key) for ev in events)
    check("every genuine audit event verifies correctly", all_valid, f"{len(events)} events")

    if events:
        tampered = dict(events[0])
        tampered["method"] = "redact"
        tamper_caught = not audit.verify_audit_event(tampered, audit_key)
        check("a tampered event is correctly rejected", tamper_caught)

        wrong_key_caught = not audit.verify_audit_event(events[0], "wrong-key")
        check("verification against the wrong key is correctly rejected", wrong_key_caught)

    # ------------------------------------------------------------------
    section("5. Taxonomy drift detection: catches a real injected failure")
    # ------------------------------------------------------------------
    import drift
    import random
    from faker import Faker

    half = len(entries) // 2
    baseline_entries = entries[:half]
    current_stable = entries[half:]

    baseline_stats = drift.field_stats(baseline_entries)
    current_stable_stats = drift.field_stats(current_stable)
    flagged_stable, _ = drift.compare(baseline_stats, current_stable_stats, threshold=0.05)
    check("no false positives when comparing a stable corpus against itself",
          len(flagged_stable) == 0, f"{len(flagged_stable)} fields incorrectly flagged")

    fake = Faker()
    Faker.seed(4242)
    random.seed(4242)
    current_drifted = []
    injected = 0
    for e in current_stable:
        e = dict(e)
        if e["log_type"] == "cloudtrail" and '"eventName": "GetPatientRecord"' in e["log"]:
            if random.random() < 0.6:
                name = fake.name()
                e["log"] = e["log"].replace(
                    '"reason": "billing reconciliation"',
                    f'"reason": "contact {name} re: billing"')
                injected += 1
        current_drifted.append(e)

    current_drifted_stats = drift.field_stats(current_drifted)
    flagged_drifted, _ = drift.compare(baseline_stats, current_drifted_stats, threshold=0.05)
    drifted_field_names = {f"{f_['log_type']}.{f_['field']}" for f_ in flagged_drifted}
    check("the injected drift is caught",
          "cloudtrail.requestParameters.reason" in drifted_field_names,
          f"{injected} entries drifted, flagged fields: {drifted_field_names or 'none'}")
    check("nothing else is falsely flagged alongside the real drift",
          len(flagged_drifted) == 1, f"{len(flagged_drifted)} fields flagged total")

    # ------------------------------------------------------------------
    section("6. HTTP service: real requests, real responses")
    # ------------------------------------------------------------------
    # run in-process rather than spawning the Flask dev server, to keep this
    # script self-contained; exercises the same detect -> dedup -> anonymize
    # -> audit path service.py wraps
    import detect
    sample = entries[0]
    for e in entries:
        if e["pii"]:
            sample = e
            break
    spans = detect.detect_all(sample["log"], use_ner=True)
    typed_spans = anonymize.dedup_spans([s for s in spans if s["type"] != "HIGH_ENTROPY"])
    anonymized = anonymize.anonymize_by_policy(sample["log"], typed_spans, key=key, store=store)
    check("service-equivalent path (detect -> dedup -> anonymize) runs without error "
          "and changes the input when PII is present",
          anonymized != sample["log"] and len(typed_spans) > 0)

    # ------------------------------------------------------------------
    section("Summary")
    # ------------------------------------------------------------------
    passed = sum(1 for _, status, _ in RESULTS if status == "PASS")
    failed = sum(1 for _, status, _ in RESULTS if status == "FAIL")
    print(f"\n{passed} passed, {failed} failed, {len(RESULTS)} total checks\n")

    if failed:
        print("FAILED CHECKS:")
        for name, status, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
        sys.exit(1)
    else:
        print("All checks passed. This reflects the code as it exists right now, "
              "run fresh — not a historical claim.")
        sys.exit(0)


if __name__ == "__main__":
    main()
