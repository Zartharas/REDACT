"""
ROADMAP item 8, second follow-on (2026-08-08). The previous extension
(validation/syslog_coverage_extension_test.py, same day as this file's
predecessor) closed sshd-publickey/su/useradd/dhclient and left three
gaps explicitly named in extract_fields_syslog's own docstring: "other
daemons, other wording -- NetworkManager, systemd unit failures, cron
with PII-bearing arguments." This test measures closing three of those,
against a fresh, dedicated, Faker-seeded corpus kept deliberately separate
from data/synthetic_logs.jsonl, for the same reason as the previous
extension test: this doesn't perturb any of the many numbers already tied
to that corpus's exact content.

New shapes covered, added the same day as this test:
  - systemd-logind "New session N of user X." -- logged once per login
    session regardless of auth method, a second independent source of a
    username in syslog structure beyond sshd's own messages.
  - cron "(X) CMD (command)" -- the parenthesized username cron logs
    before every job execution, standard across distributions.
  - NetworkManager "<info> [timestamp] dhcp4 (iface): address X" -- the
    same DHCP-lease-IP shape dhclient covers, under NetworkManager's own
    tag and message wrapping.

Found and fixed live while building this test: `_SYSLOG_TAG_RE` used a
plain `\\w+` for the tag, which doesn't match the hyphen in
"systemd-logind" -- the whole tag-prefix match failed on every
systemd-logind line, not just the message-body pattern, so the function
returned {} unconditionally for this daemon until the tag regex was
widened to `[\\w-]+`. Same class of bug as the ordering issue the
previous round found: something that looked like "just add a pattern"
actually needed a small fix to code the new pattern depends on.

Run: python validation/syslog_coverage_extension_round2_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import fields  # noqa: E402
from faker import Faker  # noqa: E402

fake = Faker()
Faker.seed(9191)  # distinct from both the canonical corpus's seed (42) and
                   # the first syslog extension test's seed (4242) --
                   # another separate, small, throwaway test corpus.

NEW_DIRTY_TEMPLATES = [
    ("systemd-logind[{pid}]: New session {num} of user {user}.",
     "systemd-logind.user", None),
    ("CRON[{pid}]: ({user}) CMD (/home/{user}/backup.sh --verbose)",
     "CRON.user", "CRON.command"),
    ("NetworkManager[{pid}]: <info>  [{epoch_ts}] dhcp4 (eth0): address {ip}",
     "NetworkManager.src_ip", "NetworkManager.iface"),
]

EXISTING_DIRTY_TEMPLATES = [
    # Unchanged shapes from both the original extractor and the first
    # extension round -- must still work identically after the tag-regex
    # widening and the new pattern insertions above.
    ("sshd[{pid}]: Failed password for {user} from {ip} port {port} ssh2",
     "sshd.user", "sshd.src_ip"),
    ("sshd[{pid}]: Accepted publickey for {user} from {ip} port {port} ssh2",
     "sshd.user", "sshd.src_ip"),
    ("su[{pid}]: pam_unix(su:session): session opened for user root by "
     "{user}(uid=0)", "su.user", None),
    ("useradd[{pid}]: new user: name={user}, UID=1005, GID=1005, "
     "home=/home/{user}, shell=/bin/bash", "useradd.name", None),
    ("dhclient[{pid}]: DHCPACK from {ip}", "dhclient.src_ip", None),
    ("sudo[{pid}]: {user} : TTY=pts/0 ; PWD=/home/{user} ; USER=root ; "
     "COMMAND=/usr/bin/systemctl restart nginx", "sudo.PWD", "sudo.USER"),
    ("kernel: [{ts}] iptables: DROP IN=eth0 SRC={ip} DST=10.0.0.1 PROTO=TCP",
     "kernel.SRC", None),
]

CLEAN_TEMPLATES = [
    # Deliberately still-uncovered shapes -- must produce zero fields,
    # same as any message shape outside the pattern list.
    "systemd[1]: Started Session {num} of user root.",  # different
        # message shape than systemd-logind's "New session N of user X."
        # above -- this one is left uncovered on purpose, to prove the new
        # pattern isn't accidentally over-broad.
    "kernel: [{ts}] CPU0: Package temperature above threshold, cpu clock throttled",
    "NetworkManager[{pid}]: <info>  [{epoch_ts}] Wi-Fi P2P device controlled by wpa_supplicant",  # different
        # NetworkManager message type than the dhcp4-address shape above.
    "cron[{pid}]: (CRON) INFO (Reloading configuration)",  # cron's own
        # startup/reload message, not a job-execution "(user) CMD (...)"
        # line -- structurally different, should not match the cron
        # pattern above.
]


def build_line(template: str) -> str:
    return template.format(
        pid=fake.random_int(1000, 9999),
        user=fake.user_name(),
        ip=fake.ipv4_public(),
        port=fake.random_int(1024, 65535),
        ts=fake.iso8601(),
        epoch_ts=f"{fake.unix_time():.4f}",  # NetworkManager's own real
            # timestamp format is Unix epoch seconds with a fractional
            # part ("[1620000000.1234]"), not ISO8601 -- using the wrong
            # shape here would test a message this daemon doesn't
            # actually produce.
        num=fake.random_int(1, 9999),
    )


def main():
    n_per_template = 100

    print("=== New syslog patterns (added this session) ===")
    total_new = 0
    covered_new = 0
    for template, field_a, field_b in NEW_DIRTY_TEMPLATES:
        hits = 0
        for _ in range(n_per_template):
            line = build_line(template)
            result = fields.extract_fields_syslog(line)
            required = [f for f in (field_a, field_b) if f]
            if all(f in result for f in required):
                hits += 1
        total_new += n_per_template
        covered_new += hits
        pct = hits / n_per_template * 100
        print(f"  {template[:55]!r:57} {hits}/{n_per_template} ({pct:.0f}%)")

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
        print(f"  {template[:55]!r:57} {hits}/{n_per_template} ({pct:.0f}%)")

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
