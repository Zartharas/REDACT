"""
Downloads and prepares a manageable, real CloudTrail sample for
validation/real_data/inject_and_evaluate.py's build_cloudtrail_corpus(),
completing the "windows_event and cloudtrail have never been checked
against real data" gap alongside windows_event (see
WindowsEventSamples_raw.jsonl and inject_and_evaluate.py's
build_windows_event_corpus()).

MUST be run on a machine with real internet access, not this project's
sandbox -- the sandbox's network is allowlist-restricted and blocks the
raw.githubusercontent.com / summitroute.com binary downloads this needs
(confirmed live, 2026-08-10: `curl -sI` against a raw.githubusercontent.com
.zip returned "403 Forbidden ... X-Proxy-Error: blocked-by-allowlist").

SOURCE, AND A REAL, DISCLOSED LIMITATION OF THIS SPECIFIC DATASET: the
flaws.cloud CloudTrail logs (Scott Rodriguez / Summit Route, released
2020, https://summitroute.com/blog/2020/10/09/public_dataset_of_cloudtrail_logs_from_flaws_cloud/)
-- 1,939,207 real events from a real (if intentionally vulnerable) AWS
training environment, 2017-2020. NOT anonymized in every field: real IAM
usernames used by the game (backup, Level6, flaws) and the real AWS API
vocabulary (eventName, requestParameters shapes) are genuine. IS
anonymized in the field this project would most want to test directly:
source IP addresses and account IDs were run through a format-preserving
substitution tool (Latacora's wernicke) before release, specifically so
real player IPs couldn't be recovered from the public data. That means
this dataset can validate PERSON-type detection (injected into
userIdentity.userName, same methodology as the syslog/windows_event
datasets) against real CloudTrail JSON structure and real API call
patterns, but CANNOT validate IP detection against genuinely real,
unmodified IP addresses -- only against real-SHAPED (dotted-quad),
publisher-substituted ones. Disclosed here and again in
build_cloudtrail_corpus()'s own docstring, not glossed over.

Usage:
    python3 validation/real_data/prepare_cloudtrail_dataset.py [--n 2000]

Downloads ~240MB, extracts one 100,000-line chunk, and writes a trimmed,
uniformly sampled subset (default 2,000 records) to
validation/real_data/datasets/CloudTrailFlaws_raw.jsonl -- one compact
JSON object per line (event field names lowercased to match this
project's own generate_logs.py CLOUDTRAIL_TEMPLATES shape), with large
nested blobs (responseElements, additionalEventData, full
requestParameters when very large) dropped to keep the file a manageable
size. Every field VALUE that IS kept is copied verbatim from the real
(if partially anonymized, see above) source -- nothing invented.
"""
import argparse
import gzip
import json
import os
import random
import tarfile
import urllib.request

DOWNLOAD_URL = "https://summitroute.com/downloads/flaws_cloudtrail_logs.tar"
TAR_PATH = "flaws_cloudtrail_logs.tar"
OUT_PATH = os.path.join("datasets", "CloudTrailFlaws_raw.jsonl")

# Fields worth keeping per record -- mirrors this project's own
# CLOUDTRAIL_TEMPLATES in src/generate_logs.py (eventName,
# sourceIPAddress, userIdentity.userName, requestParameters), plus a few
# more real top-level fields that are small and potentially PII-adjacent
# (userAgent can carry identifying tool/version strings; awsRegion and
# eventTime are structural, kept for readability, not PII).
KEEP_TOP_LEVEL = [
    "eventTime", "eventName", "eventSource", "awsRegion",
    "sourceIPAddress", "userAgent", "errorCode", "errorMessage",
]
KEEP_USERIDENTITY = ["type", "principalId", "arn", "accountId", "userName"]


def trim_record(record: dict) -> dict:
    trimmed = {k: record[k] for k in KEEP_TOP_LEVEL if k in record}
    ui = record.get("userIdentity")
    if isinstance(ui, dict):
        trimmed_ui = {k: ui[k] for k in KEEP_USERIDENTITY if k in ui}
        if trimmed_ui:
            trimmed["userIdentity"] = trimmed_ui
    # requestParameters kept only if small (real CloudTrail events can
    # have deeply nested, huge requestParameters for some API calls --
    # e.g. IAM policy documents -- dropped here to keep each sample line
    # a reasonable size, same "manageable file" tradeoff
    # WindowsEventSamples_raw.jsonl's own header comment discloses for
    # Message/TaskContent/EventData).
    rp = record.get("requestParameters")
    if isinstance(rp, dict) and len(json.dumps(rp)) < 300:
        trimmed["requestParameters"] = rp
    return trimmed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    os.makedirs("datasets", exist_ok=True)

    if not os.path.exists(TAR_PATH):
        print(f"Downloading {DOWNLOAD_URL} (~240MB, this will take a while)...")
        urllib.request.urlretrieve(DOWNLOAD_URL, TAR_PATH)
    else:
        print(f"{TAR_PATH} already present, skipping download.")

    print("Reading tar index...")
    with tarfile.open(TAR_PATH) as tf:
        gz_members = [m for m in tf.getmembers() if m.name.endswith(".gz")]
        if not gz_members:
            raise RuntimeError(
                f"No .gz members found in {TAR_PATH} -- the archive's internal "
                f"layout may have changed since this script was written "
                f"(2026-08-10). Run `tar tvf {TAR_PATH} | head -20` and adjust "
                f"the .endswith('.gz') filter above to match."
            )
        # First chunk only -- 100,000 events is already far more than
        # needed for a 2,000-line sample, and downloading the whole
        # 240MB archive is already the slow part; no reason to also
        # decompress every chunk when one suffices.
        member = gz_members[0]
        print(f"Extracting and decompressing {member.name}...")
        f = tf.extractfile(member)
        raw_bytes = gzip.decompress(f.read())

    data = json.loads(raw_bytes)
    records = data.get("Records", data if isinstance(data, list) else [])
    print(f"Chunk contains {len(records)} events.")

    random.seed(args.seed)
    sample = random.sample(records, min(args.n, len(records)))
    print(f"Sampling {len(sample)} events (seed={args.seed}).")

    with open(OUT_PATH, "w", encoding="utf-8") as out:
        for record in sample:
            trimmed = trim_record(record)
            out.write(json.dumps(trimmed) + "\n")

    print(f"Wrote {OUT_PATH}.")
    print()
    print("Next step: run validation/real_data/inject_and_evaluate.py -- it will")
    print("pick this file up automatically (see build_cloudtrail_corpus()) alongside")
    print("the existing OpenSSH/Linux/Thunderbird/windows_event conditions.")


if __name__ == "__main__":
    main()
