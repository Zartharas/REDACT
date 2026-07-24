"""
Real-data generalization validation (paper Section 5.7).

Every other result in this repository is measured against REDACT's own
synthetic corpus (see generate_logs.py). This script answers a direct
question that synthetic-only evaluation cannot: does the format-sensitivity
finding (98.8% recall on space-separated names, 5.9% on flattened ones)
hold on log data this project's authors had no part in creating?

It evaluates against five real, independently collected, unmodified log
datasets from Loghub (Zhu et al., ISSRE 2023), the same benchmark source
used by a closely related study, SDLog (Aghili et al., 2025). Run
download_loghub.sh first.

Injection methodology, applied only where a real field could plausibly
carry the entity type in production:
  - OpenSSH, Linux, Thunderbird: contain a real "user X" or "for user X"
    authentication field. The existing test value in that field (an
    attacker-guessed username, or a fixed system account) is replaced with
    a synthetic identity, in one of the same two formats central to this
    paper's finding, with the exact offset recorded and verified.
  - OpenStack, Zookeeper: contain real, unmodified IP addresses in
    connection/API logs. These are used directly as ground truth, no
    injection performed.

Eleven of Loghub's sixteen datasets (Android, Apache, BGL, Hadoop, HDFS,
HealthApp, HPC, Mac, Proxifier, Spark, Windows) are deliberately excluded:
none of them contains a field that would plausibly carry a person's name,
email address, SSN, credit card number, or medical record number in
production use. Injecting PII into them would test a fabricated scenario,
not a real one, and the paper says so explicitly rather than padding the
result count.
"""
import re, json, random, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
import detect
from faker import Faker

IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

USER_FIELD_DATASETS = {
    'OpenSSH': re.compile(r'\buser (\w+)'),
    'Linux': re.compile(r'\buser (\w+)'),
    'Thunderbird': re.compile(r'\bfor user (\w+)'),
}
# Fixed phrases that look like a username field match but aren't a real
# identity (PAM's "no such account" message), excluded from injection.
EXCLUDE_VALUES = {'unknown'}

IP_ONLY_DATASETS = ['OpenStack', 'Zookeeper']


def build_user_field_corpus(name, pattern, inject_prob=0.5, seed=42):
    random.seed(seed)
    fake = Faker()
    Faker.seed(seed)
    path = os.path.join('datasets', f'{name}_2k.log')
    with open(path, encoding='utf-8', errors='replace') as f:
        raw_lines = [l.rstrip('\r\n') for l in f]

    entries, injected, flat_n, spaced_n = [], 0, 0, 0
    for line in raw_lines:
        pii = [{'type': 'IP', 'start': m.start(), 'end': m.end()}
               for m in IP_RE.finditer(line)]
        m = pattern.search(line)
        if m and m.group(1) not in EXCLUDE_VALUES and random.random() < inject_prob:
            flat = random.random() < 0.5
            value = fake.user_name() if flat else fake.name()
            flat_n += flat
            spaced_n += (not flat)
            start, end = m.start(1), m.end(1)
            line = line[:start] + value + line[end:]
            pii.append({'type': 'PERSON', 'start': start, 'end': start + len(value),
                        'injected_value': value})
            injected += 1
        entries.append({'log': line, 'pii': pii})

    # integrity check: recorded offset must extract exactly the injected value
    mismatches = sum(
        1 for e in entries for p in e['pii']
        if p['type'] == 'PERSON' and e['log'][p['start']:p['end']] != p['injected_value']
    )
    assert mismatches == 0, f"{name}: {mismatches} offset integrity failures"
    return entries, injected, flat_n, spaced_n


def build_ip_only_corpus(name):
    path = os.path.join('datasets', f'{name}_2k.log')
    with open(path, encoding='utf-8', errors='replace') as f:
        raw_lines = [l.rstrip('\r\n') for l in f]
    return [
        {'log': line, 'pii': [{'type': 'IP', 'start': m.start(), 'end': m.end()}
                               for m in IP_RE.finditer(line)]}
        for line in raw_lines
    ]


def evaluate(entries, label):
    tp = fp = fn = 0
    person_tp = person_fn = 0
    person_flat_tp = person_flat_total = 0
    person_spaced_tp = person_spaced_total = 0
    ip_tp = ip_fn = 0

    for e in entries:
        gold = e['pii']
        preds = detect.scan_regex(e['log']) + detect.scan_ner(e['log'])
        matched = set()
        for p in preds:
            hit = False
            for i, g in enumerate(gold):
                if i in matched:
                    continue
                if g['type'] == p['type'] and p['start'] < g['end'] and g['start'] < p['end']:
                    matched.add(i)
                    hit = True
                    tp += 1
                    if g['type'] == 'PERSON':
                        person_tp += 1
                        if ' ' in e['log'][g['start']:g['end']]:
                            person_spaced_tp += 1
                        else:
                            person_flat_tp += 1
                    elif g['type'] == 'IP':
                        ip_tp += 1
                    break
            if not hit:
                fp += 1
        for i, g in enumerate(gold):
            if i not in matched:
                fn += 1
                if g['type'] == 'PERSON':
                    person_fn += 1
                elif g['type'] == 'IP':
                    ip_fn += 1
        for g in gold:
            if g['type'] == 'PERSON':
                if ' ' in e['log'][g['start']:g['end']]:
                    person_spaced_total += 1
                else:
                    person_flat_total += 1

    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0
    print(f"=== {label} ===")
    print(f"  overall: P={prec:.3f} R={rec:.3f} (TP={tp} FP={fp} FN={fn})")
    if person_spaced_total:
        print(f"  PERSON spaced:  {person_spaced_tp}/{person_spaced_total} = "
              f"{person_spaced_tp/person_spaced_total:.1%}")
    if person_flat_total:
        print(f"  PERSON flat:    {person_flat_tp}/{person_flat_total} = "
              f"{person_flat_tp/person_flat_total:.1%}")
    if ip_tp + ip_fn:
        print(f"  IP recall:      {ip_tp}/{ip_tp+ip_fn} = {ip_tp/(ip_tp+ip_fn):.1%}")
    print()


if __name__ == '__main__':
    if not os.path.isdir('datasets'):
        print("Run download_loghub.sh first.")
        sys.exit(1)

    for name, pattern in USER_FIELD_DATASETS.items():
        entries, injected, flat_n, spaced_n = build_user_field_corpus(name, pattern)
        print(f"{name}: {len(entries)} lines, {injected} PERSON injected "
              f"(flat={flat_n}, spaced={spaced_n})")
        evaluate(entries, name)

    for name in IP_ONLY_DATASETS:
        entries = build_ip_only_corpus(name)
        print(f"{name}: {len(entries)} lines, IP-only ground truth, "
              f"{sum(len(e['pii']) for e in entries)} IP spans")
        evaluate(entries, name)
