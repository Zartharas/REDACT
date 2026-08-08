"""
ROADMAP item 11. A dedicated small corpus of the exact category the entropy
detection layer (src/detect.py's scan_entropy()) is actually built for --
API keys, session/bearer tokens, and opaque hashes -- so it can be measured
against its real intended use case instead of the main synthetic corpus,
which (as README.md already says plainly) doesn't contain this category at
all. The main corpus's entropy numbers (2.3% unique recall, 34.8%
false-alarm rate at the most permissive threshold tested) are a real
measurement, but of the wrong target: structured fields like EventID=,
TargetUserName=, IP addresses, none of which are what entropy-based
detection is meant to catch.

Two kinds of lines, same JSONL shape as generate_logs.py ({"log": str,
"pii": [{"start", "end", "type": "HIGH_ENTROPY"}]}):

  - "dirty": contains a real secret-shaped token (Bearer/JWT-style token,
    an AWS-style access-key/secret pair, a GitHub-style PAT, a session
    cookie value, or a hex-encoded hash), embedded in a realistic
    surrounding log line (an HTTP access log, an auth log, a config dump).
    Gold span type is "HIGH_ENTROPY" -- matching what scan_entropy() itself
    emits, so evaluate_entropy.py in this directory can score against it
    directly without a type-translation table.
  - "clean": a realistic long-but-NOT-secret token in a similar surrounding
    context -- a UUID (this is the deliberately hard/borderline case:
    UUIDs are long and look random but are far lower entropy than a true
    secret, given their fixed hyphen positions and version/variant nibbles),
    a long English phrase, a URL, a file path, a hostname, a timestamp.
    These exist specifically to measure the false-alarm rate honestly, the
    same way the main corpus's CLEAN_TEMPLATES do.

All tokens are generated with a fixed seed (Random(42), matching this
project's existing convention) from `random`/`string`, not from any real
credential, key, or service -- no real API keys, no real tokens, nothing
that could accidentally be a live, working secret.
"""
import json
import random
import string
import argparse

SEED = 42


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _b64ish(rng: random.Random, n: int) -> str:
    alphabet = string.ascii_letters + string.digits + "+/"
    return "".join(rng.choice(alphabet) for _ in range(n))


def _urlsafe_b64ish(rng: random.Random, n: int) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(rng.choice(alphabet) for _ in range(n))


def make_jwt(rng: random.Random) -> str:
    # Real JWTs are three base64url segments (header.payload.signature).
    # Not decodable to valid JSON here -- shape and entropy are what
    # matters for this test, not semantic validity.
    return ".".join(_urlsafe_b64ish(rng, n) for n in (36, 64, 43))


def make_aws_access_key(rng: random.Random) -> str:
    return "AKIA" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16))


def make_aws_secret_key(rng: random.Random) -> str:
    return _b64ish(rng, 40)


def make_github_pat(rng: random.Random) -> str:
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def make_session_token(rng: random.Random) -> str:
    return _hex(rng, 64)


def make_sha256_hash(rng: random.Random) -> str:
    return _hex(rng, 64)


def make_api_key_generic(rng: random.Random) -> str:
    return "sk_live_" + _b64ish(rng, 32)


def make_uuid(rng: random.Random) -> str:
    # Real UUIDv4 shape: fixed hyphens, version nibble fixed to '4',
    # variant nibble constrained to 8/9/a/b -- structurally lower entropy
    # per character than a fully random hex/base64 token of the same
    # length, which is exactly why it's included here as the deliberately
    # hard negative case.
    h = _hex(rng, 12)
    return f"{_hex(rng, 8)}-{_hex(rng, 4)}-4{_hex(rng, 3)}-{rng.choice('89ab')}{_hex(rng, 3)}-{h}"


SECRET_MAKERS = {
    "jwt": make_jwt,
    "aws_access_key": make_aws_access_key,
    "aws_secret_key": make_aws_secret_key,
    "github_pat": make_github_pat,
    "session_token": make_session_token,
    "sha256_hash": make_sha256_hash,
    "api_key_generic": make_api_key_generic,
}

DIRTY_TEMPLATES = [
    ('GET /api/v1/orders HTTP/1.1" 200 Authorization: Bearer {secret}', "jwt"),
    ('POST /oauth/token HTTP/1.1" 200 access_token={secret}', "jwt"),
    ('config: aws_access_key_id={secret}', "aws_access_key"),
    ('config: aws_secret_access_key={secret}', "aws_secret_key"),
    ('git: X-GitHub-Token: {secret}', "github_pat"),
    ('session middleware: session_id={secret}; Path=/; HttpOnly', "session_token"),
    ('auth: password_hash=sha256:{secret}', "sha256_hash"),
    ('billing: X-Api-Key: {secret}', "api_key_generic"),
    ('webhook delivery: X-Hub-Signature-256=sha256={secret}', "sha256_hash"),
]

CLEAN_TEMPLATES = [
    'GET /api/v1/orders/{uuid} HTTP/1.1" 200 request_id={uuid}',
    'nginx: upstream connect to {host} failed, trying next',
    'app: user preferences saved successfully for account settings page',
    'cron: scheduled backup job completed at {ts} with no errors reported',
    'lb: health check passed for instance i-0a1b2c3d4e5f67890',
    'app: rendering template dashboard/overview.html took 42ms',
    'file: reading configuration from /etc/redact/service-settings.yaml',
    'db: connection pool resized to 20 after sustained load increase',
]

HOSTS = ["api-internal.example", "cache-03.example", "db-replica-2.example"]


def build_entry(rng: random.Random, dirty: bool) -> dict:
    if dirty:
        template, kind = rng.choice(DIRTY_TEMPLATES)
        secret = SECRET_MAKERS[kind](rng)
        prefix = f'{rng.choice(["10.0.1.4", "10.0.2.9", "10.0.3.1"])} - - "'
        text = prefix + template.format(secret=secret)
        start = text.index(secret)
        return {"log": text, "pii": [{"start": start, "end": start + len(secret), "type": "HIGH_ENTROPY"}]}
    else:
        template = rng.choice(CLEAN_TEMPLATES)
        text = template.format(
            uuid=make_uuid(rng),
            host=rng.choice(HOSTS),
            ts=f"2026-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}T{rng.randint(0,23):02d}:00:00Z",
        )
        return {"log": text, "pii": []}


def main(n: int, out_path: str, dirty_ratio: float = 0.5):
    rng = random.Random(SEED)
    with open(out_path, "w") as f:
        for _ in range(n):
            dirty = rng.random() < dirty_ratio
            f.write(json.dumps(build_entry(rng, dirty)) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--out", type=str, default="validation/entropy_fair_test/secrets_corpus.jsonl")
    parser.add_argument("--dirty-ratio", type=float, default=0.5)
    args = parser.parse_args()
    main(args.n, args.out, args.dirty_ratio)
    print(f"Wrote {args.n} entries to {args.out}")
