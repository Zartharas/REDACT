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
import re, json, random, sys, os, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
import detect
import fields
from faker import Faker

IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

# Engineering upgrade, 2026-08-09 (Task #10): extends this script to also
# measure the field-gated condition against real data, not just naive.
# Checking what fields.py would actually see on the real, unmodified
# Loghub files (not a synthetic guess) surfaced a real gap two ways:
#
# 1. A raw Loghub line has a leading RFC3164 "Mon DD HH:MM:SS host "
#    timestamp/hostname prefix before the syslog tag (confirmed against
#    the actual OpenSSH_2k.log/Linux_2k.log content). fields.py's
#    extract_fields_syslog is anchored at the start of the string, so
#    this prefix means fields.py returns {} for essentially every RAW
#    line -- field-gating never engages, and detect_all_field_gated()
#    correctly, safely falls back to running NER on the full original
#    line (see detect.build_ner_candidate's documented fallback), same
#    as naive would. This is the honest, as-deployed-today result: THIS
#    PROJECT'S OWN logstash/redact-pipeline.conf reads whole raw lines
#    via a plain `file` input with no grok/timestamp-stripping filter
#    before calling redact-service, so this raw-line condition is what a
#    real deployment against unmodified syslog would actually see right
#    now, not a worst-case strawman.
# 2. Separately (found by fetching Linux_2k.log specifically), some
#    daemons there use a PAM-decorated tag ("sshd(pam_unix)[19939]:")
#    that _SYSLOG_TAG_RE didn't handle at all even once the header is
#    stripped -- fixed in fields.py the same day (see
#    validation/syslog_coverage_extension_round4_test.py for the
#    dedicated regression coverage).
#
# _strip_syslog_header below simulates what a syslog-aware ingestion step
# (e.g. Logstash's own grok SYSLOGTIMESTAMP+SYSLOGHOST patterns) WOULD
# produce, so the field-gated condition can also be measured under that
# more favorable, commonly-used real-world setup -- clearly labeled as a
# simulation of a preprocessing step this project doesn't currently have
# wired in, not a claim that it already does.
_RFC3164_HEADER_RE = re.compile(r'^[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+')


def strip_syslog_header(line: str) -> tuple[int, str]:
    """Returns (prefix_length, remainder) if `line` starts with a
    standard RFC3164 "Mon DD HH:MM:SS host " header, else (0, line)
    unchanged. Thunderbird's raw format does NOT match this (it uses a
    much longer, dataset-specific custom header -- "- <epoch> <date>
    <host> Mon DD HH:MM:SS <host>/<host> tag[pid]: ...", not standard
    syslog), so this deliberately returns the line unchanged for
    Thunderbird rather than guessing at a bespoke parser for one
    dataset's own format -- the same "targeted, not general-purpose"
    scoping this project's other fields.py patterns already use."""
    m = _RFC3164_HEADER_RE.match(line)
    if not m:
        return 0, line
    return m.end(), line[m.end():]


def field_gate_engagement(entries, log_type: str, strip_header: bool = False) -> tuple[int, int]:
    """Diagnostic, not scoring: how many lines does fields.py actually
    recognize structure in at all (the precondition for field-gating to
    do anything other than fall back to naive-equivalent behavior)? This
    is the single most honest number for whether field-gating is doing
    anything on a given real dataset, independent of whatever the
    eventual recall/precision/timing numbers say."""
    engaged = 0
    for e in entries:
        text = e['log']
        _, body = strip_syslog_header(text) if strip_header else (0, text)
        if fields.extract_fields(log_type, body):
            engaged += 1
    return engaged, len(entries)

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


# windows_event real-data validation, 2026-08-10 (ROADMAP item, "windows_event
# and cloudtrail have NOT been checked against any real (non-synthetic) data
# at all"). No Loghub dataset covers this -- Loghub's own "Windows" dataset
# is CBS (Component-Based Servicing, i.e. Windows Update/servicing
# subsystem) log text, an entirely different subsystem with no
# EventID=/TargetUserName=-shaped fields at all; this project's own
# inject_and_evaluate.py module docstring already excludes Loghub's Windows
# dataset for exactly this reason ("none of them contains a field that
# would plausibly carry a person's name... in production use"). A real
# Windows SECURITY-channel dataset was sourced instead: 36 real
# nxlog-exported Microsoft-Windows-Security-Auditing events (logon,
# process creation, Kerberos ticket, file/registry/share access, group
# membership, etc.), captured from a real (lab, not production) Windows
# domain environment, from the public repo
# github.com/d4rk-d4nph3/Windows-Event-Samples (WinEvents.log) --
# real captured field VALUES (hostnames like DC01.corp.local/
# ACC01.prod.corp.local, account names, IP addresses, EventIDs), not
# fabricated. See datasets/WindowsEventSamples_raw.jsonl's own header for
# exactly which fields were kept per record (large free-text fields --
# Message, TaskContent, EventData, PrivilegeList, GroupMembership -- were
# dropped when transcribing from the source to keep the file a manageable
# size; every field VALUE that IS present is copied verbatim from the
# real source, nothing invented).
WINDOWS_EVENT_RAW_PATH = os.path.join('datasets', 'WindowsEventSamples_raw.jsonl')

# Fields rendered into the flat "Key=Value Key2=Value2 ..." KV line shape
# fields.py's extract_fields_windows_event() (-> _extract_kv_pairs) expects
# -- this project's own synthetic windows_event corpus already uses this
# exact shape (EventID=4624 TargetUserName=... SourceIP=...), and this is
# also a standard, real-world SIEM-normalized representation of a Windows
# Security event (this is genuinely how many production log pipelines
# flatten EventLog/EVTX data for text-based ingestion), not an artificial
# shape invented just to make this test pass. Ordered roughly by how a
# real analyst would expect to scan a line: identity fields first, then
# the network/object fields most likely to matter for detection.
_WINEVENT_FIELD_ORDER = [
    'EventID', 'Hostname', 'Channel', 'Category',
    'SubjectUserName', 'SubjectDomainName',
    'TargetUserName', 'TargetDomainName',
    'AccountName', 'Domain',
    'IpAddress', 'IpPort',
    'ServiceName', 'ObjectName', 'ObjectType', 'ShareName',
    'ProcessName', 'TaskName', 'RuleName', 'ServerName',
]


def _flatten_windows_event(obj: dict) -> str:
    parts = []
    for key in _WINEVENT_FIELD_ORDER:
        val = obj.get(key)
        if val is None or val == '':
            continue
        parts.append(f"{key}={val}")
    return ' '.join(parts)


def build_windows_event_corpus(inject_prob=0.7, seed=42):
    """Mirrors build_user_field_corpus's injection methodology (same two
    formats -- flat username vs. spaced full name, same Faker seed
    convention) for consistency with the syslog datasets above, applied
    to TargetUserName (falling back to SubjectUserName when TargetUserName
    isn't present on a given real event -- both are genuine identity
    fields in the Security-Auditing schema, see MS-EVEN6/Windows Security
    Auditing documentation) instead of relying on the REAL account names
    already in this data, which are overwhelmingly machine/service
    accounts (DC01$, LOCAL SERVICE, SYSTEM) rather than human names --
    exactly the same reason OpenSSH/Linux/Thunderbird's existing test
    values needed replacing rather than reused as-is. IpAddress values
    ARE used directly as real, unmodified ground truth (same as
    OpenStack/Zookeeper below) wherever a record actually has one --
    genuinely real IPs from the source dataset, no injection needed."""
    random.seed(seed)
    fake = Faker()
    Faker.seed(seed)

    entries, injected, flat_n, spaced_n = [], 0, 0, 0
    with open(WINDOWS_EVENT_RAW_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            text = _flatten_windows_event(obj)
            pii = []

            for field in ('TargetUserName', 'SubjectUserName'):
                val = obj.get(field)
                if not val or val in ('-',) or val.endswith('$'):
                    # '-' is Windows Security Auditing's own "not
                    # applicable" placeholder (e.g. an anonymous/system
                    # logon's SubjectUserName); a trailing '$' is a real
                    # machine-account naming convention (DC01$), not a
                    # human name -- neither is a plausible injection
                    # target, matching EXCLUDE_VALUES's role above for
                    # the syslog datasets.
                    continue
                if random.random() >= inject_prob:
                    continue
                key_eq = f"{field}="
                idx = text.find(key_eq)
                if idx == -1:
                    continue
                start = idx + len(key_eq)
                # Values here never contain a space (real account names
                # in this dataset are single tokens like "Administrator"
                # or "LOCAL SERVICE" -- the latter DOES contain a space,
                # already excluded above via the '-'/'$' check only
                # catching the two placeholder shapes, not this one; a
                # third check keeps this simple and correct: stop at the
                # next " Key=" boundary or end of string, same tolerant
                # rule _extract_kv_pairs itself uses).
                next_field_m = re.search(r' [A-Za-z]+=', text[start:])
                end = start + next_field_m.start() if next_field_m else len(text)
                flat = random.random() < 0.5
                value = fake.user_name() if flat else fake.name()
                flat_n += flat
                spaced_n += (not flat)
                text = text[:start] + value + text[end:]
                pii.append({'type': 'PERSON', 'start': start, 'end': start + len(value),
                            'injected_value': value})
                injected += 1
                break  # inject into at most one identity field per line,
                       # matching the syslog datasets' one-injection-per-line rate

            ip_val = obj.get('IpAddress')
            if ip_val and IP_RE.fullmatch(ip_val):
                # Real, unmodified IP -- direct ground truth, same as
                # build_ip_only_corpus below. Only exact dotted-quad
                # matches (fullmatch, not search) are used, since some
                # real records here have '::1' or '::ffff:192.168.2.108'
                # (IPv6/IPv4-mapped forms) that this project's own IP
                # regex (src/detect.py's REGEX_PATTERNS['IP'], dotted-
                # quad only) was never designed to catch -- correctly
                # excluded from ground truth rather than counted as a
                # detector miss for a format it never claimed to support.
                m = IP_RE.search(text[text.find('IpAddress='):])
                if m:
                    ip_start = text.find('IpAddress=') + len('IpAddress=')
                    pii.append({'type': 'IP', 'start': ip_start,
                                'end': ip_start + len(ip_val)})

            entries.append({'log': text, 'pii': pii})

    mismatches = sum(
        1 for e in entries for p in e['pii']
        if p['type'] == 'PERSON' and e['log'][p['start']:p['end']] != p['injected_value']
    )
    assert mismatches == 0, f"windows_event: {mismatches} offset integrity failures"
    return entries, injected, flat_n, spaced_n


# cloudtrail real-data validation. See validation/real_data/
# prepare_cloudtrail_dataset.py for how CloudTrailFlaws_raw.jsonl gets
# produced (must be run separately, on a machine with real internet
# access -- not available in this project's sandbox) and for the
# disclosed limitation this dataset carries: sourceIPAddress values were
# anonymized (format-preserving substitution) by the dataset's own
# publisher before release, so IP ground truth here is real-SHAPED but
# not genuinely real -- only the PERSON-injection condition (into
# userIdentity.userName, same methodology as build_windows_event_corpus)
# tests against truly unmodified real data.
CLOUDTRAIL_RAW_PATH = os.path.join('datasets', 'CloudTrailFlaws_raw.jsonl')


def build_cloudtrail_corpus(inject_prob=0.7, seed=42):
    random.seed(seed)
    fake = Faker()
    Faker.seed(seed)

    entries, injected, flat_n, spaced_n = [], 0, 0, 0
    with open(CLOUDTRAIL_RAW_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # extract_fields_cloudtrail() flattens nested JSON into
            # dotted paths itself (src/fields.py) -- unlike windows_event,
            # no custom flattening is needed here; the real JSON record,
            # unmodified in shape, IS the "log" text this project's own
            # generate_logs.py CLOUDTRAIL_TEMPLATES already produces (a
            # single compact JSON object per line, not
            # {"Records": [...]}-wrapped).
            text = json.dumps(obj)
            pii = []

            user_name = obj.get('userIdentity', {}).get('userName')
            if user_name and random.random() < inject_prob:
                # Real values here are legitimate game-account names
                # (backup, Level6, flaws) or IAM role/session identifiers
                # -- not human names -- same reasoning as
                # build_windows_event_corpus's TargetUserName/
                # SubjectUserName injection: replace with a synthetic
                # identity rather than testing against a real value that
                # was never PII-shaped to begin with.
                key_eq = f'"userName": "{user_name}"'
                idx = text.find(key_eq)
                if idx != -1:
                    start = idx + len('"userName": "')
                    end = start + len(user_name)
                    flat = random.random() < 0.5
                    value = fake.user_name() if flat else fake.name()
                    flat_n += flat
                    spaced_n += (not flat)
                    text = text[:start] + value + text[end:]
                    pii.append({'type': 'PERSON', 'start': start, 'end': start + len(value),
                                'injected_value': value})
                    injected += 1

            # sourceIPAddress: included as ground truth for regex-recall
            # purposes (it IS a real, dotted-quad-shaped value the regex
            # layer should catch), but see this function's own module-
            # level comment above -- these values are publisher-
            # anonymized, not genuinely real IPs, so this only tests
            # "does the IP regex fire on a real-shaped value embedded in
            # real JSON structure," not "does this work against a real
            # adversary's real IP."
            ip_val = obj.get('sourceIPAddress')
            if ip_val and IP_RE.fullmatch(ip_val):
                idx = text.find(f'"sourceIPAddress": "{ip_val}"')
                if idx != -1:
                    start = idx + len('"sourceIPAddress": "')
                    pii.append({'type': 'IP', 'start': start, 'end': start + len(ip_val)})

            entries.append({'log': text, 'pii': pii})

    mismatches = sum(
        1 for e in entries for p in e['pii']
        if p['type'] == 'PERSON' and e['log'][p['start']:p['end']] != p['injected_value']
    )
    assert mismatches == 0, f"cloudtrail: {mismatches} offset integrity failures"
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


def evaluate(entries, label, use_flattened=False, field_gate_log_type=None,
             strip_syslog_header_flag=False):
    """field_gate_log_type: if set, uses detect.detect_all_field_gated()
    instead of the naive scan_regex()+scan_ner() path -- the production
    detection function src/service.py and src/pipeline.py now call by
    default (see src/detect.py's detect_all_field_gated). strip_syslog_header_flag:
    if True, strips a standard RFC3164 header (see strip_syslog_header
    above) from each line before field-gating, and shifts the resulting
    hit offsets back by the same constant amount before scoring against
    gold spans recorded in the ORIGINAL (unstripped) line's coordinates --
    a simple constant-offset shift is correct here because header
    stripping removes one fixed-length CONTIGUOUS prefix, unlike
    build_ner_candidate's own per-segment remapping, which handles
    multiple, possibly non-adjacent excised spans."""
    tp = fp = fn = 0
    person_tp = person_fn = 0
    person_flat_tp = person_flat_total = 0
    person_spaced_tp = person_spaced_total = 0
    ip_tp = ip_fn = 0

    t0 = time.perf_counter()
    for e in entries:
        gold = e['pii']
        if field_gate_log_type is not None:
            text = e['log']
            if strip_syslog_header_flag:
                prefix_len, body = strip_syslog_header(text)
            else:
                prefix_len, body = 0, text
            hits = detect.detect_all_field_gated(
                body, log_type=field_gate_log_type, use_flattened=use_flattened
            )
            if prefix_len:
                hits = [{**h, 'start': h['start'] + prefix_len,
                         'end': h['end'] + prefix_len} for h in hits]
            # detect_all_field_gated() always includes scan_entropy()
            # internally (matching detect_all()'s own default), unlike
            # the naive branch below, which -- in THIS script specifically
            # -- never calls scan_entropy() at all. Gold spans here only
            # ever carry PERSON/IP types, so an unfiltered HIGH_ENTROPY
            # hit can never match anything and would just inflate FP,
            # unfairly penalizing field-gated's precision against a naive
            # baseline that was never exposed to that layer in this
            # script. Filtered out here so the comparison stays scoped to
            # what both conditions are actually being judged on.
            preds = [h for h in hits if h['type'] != 'HIGH_ENTROPY']
        else:
            preds = detect.scan_regex(e['log']) + detect.scan_ner(e['log'])
            if use_flattened:
                preds = preds + detect.scan_flattened(e['log'])
        # De-duplicate overlapping same-type hits from different layers
        # (mirrors evaluate.py's run_evaluation): without this, two layers
        # correctly agreeing on the same real span gets the second one
        # counted as a false positive instead of a harmless duplicate.
        dedup = []
        for p in preds:
            if not any(p['type'] == d['type'] and p['start'] < d['end'] and d['start'] < p['end']
                       for d in dedup):
                dedup.append(p)
        preds = dedup
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

    elapsed = time.perf_counter() - t0

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
    rate = len(entries) / elapsed if elapsed else float('inf')
    print(f"  timing: {len(entries)} lines in {elapsed:.2f}s -> {rate:.1f} events/sec")
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
        # Layer 4 validation: does the flattened-username dictionary layer's
        # 50.3%-recall gain (measured on the Faker-derived synthetic corpus,
        # where the name dictionary and the corpus's own name generator share
        # a source) hold up on real log data with the same Faker-generated
        # injected names? This does NOT test the dictionary against names
        # outside Faker's list (that's a separate, harder generalization
        # question this script can't answer, since injection itself uses
        # Faker) -- it tests whether the layer still works correctly when
        # the surrounding log text is real, unmodified Loghub data rather
        # than this project's own synthetic templates.
        evaluate(entries, f"{name} + flattened-username layer", use_flattened=True)

        # Engineering upgrade, 2026-08-09 (Task #10): field-gated
        # condition, using detect.detect_all_field_gated() -- the same
        # function src/service.py and src/pipeline.py now call by
        # default in production, not a separate research-only path.
        # Two sub-conditions, both honestly labeled:
        engaged_raw, total = field_gate_engagement(entries, 'syslog', strip_header=False)
        print(f"  field-gate engagement (RAW, as this project's current "
              f"logstash/redact-pipeline.conf would actually send it -- no "
              f"header-stripping step exists there today): {engaged_raw}/{total} "
              f"lines got real field structure ({engaged_raw/total:.1%}); the "
              f"rest fall back to naive-equivalent full-line NER, per "
              f"detect.build_ner_candidate's documented fallback")
        evaluate(entries, f"{name} + field-gated (RAW, log_type=syslog)",
                 field_gate_log_type='syslog')

        engaged_stripped, total = field_gate_engagement(entries, 'syslog', strip_header=True)
        if engaged_stripped != engaged_raw:
            print(f"  field-gate engagement (RFC3164 header stripped, "
                  f"SIMULATING a syslog-aware ingestion step this project "
                  f"does not currently have wired in): {engaged_stripped}/{total} "
                  f"lines got real field structure ({engaged_stripped/total:.1%})")
            evaluate(entries, f"{name} + field-gated (header-stripped simulation, log_type=syslog)",
                     field_gate_log_type='syslog', strip_syslog_header_flag=True)
        else:
            # Thunderbird's raw format doesn't match the standard RFC3164
            # header shape strip_syslog_header looks for (it uses a much
            # longer, dataset-specific custom prefix -- see that
            # function's own docstring), so stripping had no effect here;
            # skip the redundant second run rather than print two
            # identical results as if they were independent findings.
            print(f"  field-gate engagement (header-stripped): identical to RAW "
                  f"({engaged_stripped}/{total}) -- {name}'s header doesn't match "
                  f"the standard RFC3164 shape strip_syslog_header looks for, "
                  f"so stripping had no effect; not re-run as a separate condition")

    if os.path.exists(WINDOWS_EVENT_RAW_PATH):
        entries, injected, flat_n, spaced_n = build_windows_event_corpus()
        print(f"windows_event: {len(entries)} lines, {injected} PERSON injected "
              f"(flat={flat_n}, spaced={spaced_n}), "
              f"{sum(1 for e in entries for p in e['pii'] if p['type']=='IP')} real IP spans")
        evaluate(entries, "windows_event (naive)")
        # No RFC3164-header-stripping condition here -- that's specific to
        # syslog's own header shape (see strip_syslog_header's own
        # docstring); windows_event's KV lines have no equivalent prefix
        # to strip, so only the RAW condition applies.
        engaged, total = field_gate_engagement(entries, 'windows_event', strip_header=False)
        print(f"  field-gate engagement: {engaged}/{total} lines got real field "
              f"structure ({engaged/total:.1%})")
        evaluate(entries, "windows_event + field-gated", field_gate_log_type='windows_event')
    else:
        print(f"Skipping windows_event: {WINDOWS_EVENT_RAW_PATH} not found.")

    if os.path.exists(CLOUDTRAIL_RAW_PATH):
        entries, injected, flat_n, spaced_n = build_cloudtrail_corpus()
        print(f"cloudtrail: {len(entries)} lines, {injected} PERSON injected "
              f"(flat={flat_n}, spaced={spaced_n}), "
              f"{sum(1 for e in entries for p in e['pii'] if p['type']=='IP')} "
              f"IP spans (real-SHAPED, publisher-anonymized -- see "
              f"build_cloudtrail_corpus's own docstring)")
        evaluate(entries, "cloudtrail (naive)")
        engaged, total = field_gate_engagement(entries, 'cloudtrail', strip_header=False)
        print(f"  field-gate engagement: {engaged}/{total} lines got real field "
              f"structure ({engaged/total:.1%})")
        evaluate(entries, "cloudtrail + field-gated", field_gate_log_type='cloudtrail')
    else:
        print(f"Skipping cloudtrail: {CLOUDTRAIL_RAW_PATH} not found -- run "
              f"prepare_cloudtrail_dataset.py first (needs real internet access, "
              f"not available in this project's sandbox).")

    for name in IP_ONLY_DATASETS:
        entries = build_ip_only_corpus(name)
        print(f"{name}: {len(entries)} lines, IP-only ground truth, "
              f"{sum(len(e['pii']) for e in entries)} IP spans")
        evaluate(entries, name)
        # Field-gating deliberately NOT run here: these two datasets'
        # ground truth is IP-only, and IP is a regex hit (scan_regex),
        # never NER-dependent -- field-gating only changes what NER sees,
        # so it cannot change this condition's result at all. Running it
        # anyway would just reprint identical numbers under a different
        # label, not a real additional data point.
