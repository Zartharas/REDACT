"""
ROADMAP item 8 follow-on. The original extract_fields_syslog() (src/fields.py,
2026-08-07) covered sudo/kernel KV-style bodies and a handful of sshd
password-auth message shapes, explicitly flagged as partial: "message shapes
outside the pattern list still get zero fields... a production deployment
with different syslog traffic needs its own additional patterns." This
script measures a second round of exactly that -- new patterns for a few
more common real-world syslog message shapes -- against a small, dedicated,
Faker-seeded corpus (deliberately NOT the canonical data/synthetic_logs.jsonl,
so this doesn't perturb any of the many numbers already tied to that
corpus's exact content and gold-span count).

New shapes covered, added the same day as this test:
  - sshd "Accepted publickey for X from Y port Z" (key-based auth -- the
    original patterns only covered "Accepted password for", an oversight).
  - su "session opened for user root by X(uid=N)" (privilege escalation).
  - useradd/usermod "name=X, UID=N, GID=N, home=X, shell=X" (comma-separated
    KV, not semicolon-separated like sudo's).
  - dhclient "DHCPACK from X" / "DHCPOFFER from X" (DHCP lease IP).

Found and fixed live while building this test: a real ordering bug, not
just missing coverage. The su and dhclient message shapes both legitimately
contain a bare "=" in a non-KV context ("by jsmith(uid=0)",
"(xid=0x12345678)"), which used to trigger extract_fields_syslog's generic
KV-fallback branch FIRST (since that branch only checked `if "=" in rest`)
and produce a wrong, nonsensical field (e.g. su.uid = "0)") instead of ever
reaching the more specific su/dhclient patterns. Fixed by trying the
specific preposition-shaped patterns before the generic KV fallback, not
after -- this also protects any future daemon pattern added the same way.

Run: python validation/syslog_coverage_extension_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import fields  # noqa: E402
from faker import Faker  # noqa: E402

fake = Faker()
Faker.seed(4242)  # distinct from generate_logs.py's seed 42, deliberately --
                   # this is a separate, small, throwaway test corpus, not
                   # meant to be confused with or compared 1:1 against the
                   # canonical corpus's own numbers.

NEW_DIRTY_TEMPLATES = [
    ("sshd[{pid}]: Accepted publickey for {user} from {ip} port {port} ssh2: "
     "RSA SHA256:abcd1234efgh5678", "sshd.user", "sshd.src_ip"),
    ("su[{pid}]: pam_unix(su:session): session opened for user root by "
     "{user}(uid=0)", "su.user", None),
    ("useradd[{pid}]: new user: name={user}, UID=1005, GID=1005, "
     "home=/home/{user}, shell=/bin/bash", "useradd.name", None),
    ("dhclient[{pid}]: DHCPACK from {ip} (xid=0x{hexid})", "dhclient.src_ip", None),
    ("dhclient[{pid}]: DHCPOFFER from {ip}", "dhclient.src_ip", None),
]

EXISTING_DIRTY_TEMPLATES = [
    # Unchanged shapes from the original extension -- must still work
    # identically after the ordering fix above.
    ("sshd[{pid}]: Failed password for {user} from {ip} port {port} ssh2",
     "sshd.user", "sshd.src_ip"),
    ("sudo[{pid}]: {user} : TTY=pts/0 ; PWD=/home/{user} ; USER=root ; "
     "COMMAND=/usr/bin/systemctl restart nginx", "sudo.PWD", "sudo.USER"),
    ("kernel: [{ts}] iptables: DROP IN=eth0 SRC={ip} DST=10.0.0.1 PROTO=TCP",
     "kernel.SRC", None),
]

CLEAN_TEMPLATES = [
    "systemd[1]: Started Session {num} of user root.",
    "kernel: [{ts}] CPU0: Package temperature above threshold, cpu clock throttled",
    "cron[{pid}]: (root) CMD (/usr/bin/backup.sh)",
]


def build_line(template: str) -> str:
    return template.format(
        pid=fake.random_int(1000, 9999),
        user=fake.user_name(),
        ip=fake.ipv4_public(),
        port=fake.random_int(1024, 65535),
        ts=fake.iso8601(),
        num=fake.random_int(1, 9999),
        hexid=fake.hexify("^^^^^^^^", upper=False),
    )


def main():
    n_per_template = 100

    print("=== New syslog patterns (added this session) ===")
    total_new = 0
    covered_new = 0
    for template, required_field, _ in NEW_DIRTY_TEMPLATES:
        hits = 0
        for _ in range(n_per_template):
            line = build_line(template)
            result = fields.extract_fields_syslog(line)
            if required_field in result:
                hits += 1
        total_new += n_per_template
        covered_new += hits
        pct = hits / n_per_template * 100
        print(f"  {template[:50]!r:52} {hits}/{n_per_template} ({pct:.0f}%)")

    print(f"\nNew shapes total: {covered_new}/{total_new} "
          f"({covered_new/total_new:.1%})")

    print("\n=== Existing syslog patterns (regression check) ===")
    total_existing = 0
    covered_existing = 0
    for template, field_a, field_b in EXISTING_DIRTY_TEMPLATES:
        hits = 0
        for _ in range(n_per_template):
            line = build_line(template)
            result = fields.extract_fields_syslog(line)
            required = [f for f in (field_a, field_b) if f]
            if all(f in result for f in required):
                hits += 1
        total_existing += n_per_template
        covered_existing += hits
        pct = hits / n_per_template * 100
        print(f"  {template[:50]!r:52} {hits}/{n_per_template} ({pct:.0f}%)")

    print(f"\nExisting shapes total: {covered_existing}/{total_existing} "
          f"({covered_existing/total_existing:.1%})")

    print("\n=== Clean lines (false-extraction check) ===")
    total_clean = 0
    false_positives = 0
    for template in CLEAN_TEMPLATES:
        fps = 0
        for _ in range(n_per_template):
            line = build_line(template)
            result = fields.extract_fields_syslog(line)
            if result:
                fps += 1
                print(f"    FALSE EXTRACTION on clean line: {line!r} -> {result}")
        total_clean += n_per_template
        false_positives += fps

    print(f"\nClean lines total: {false_positives}/{total_clean} falsely extracted")

    print("\n=== Summary ===")
    ok = (covered_new == total_new and covered_existing == total_existing
          and false_positives == 0)
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
