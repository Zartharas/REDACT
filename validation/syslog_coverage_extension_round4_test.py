"""
ROADMAP item 8, fourth follow-on (2026-08-09), found while extending the
real-data validation (Task #10, validation/real_data/inject_and_evaluate.py)
to cover the field-gated NER strategy. Before writing that extension,
fetched the actual, unmodified Loghub OpenSSH/Linux/Thunderbird files
(validation/real_data/download_loghub.sh's own source) to check what
fields.py would really see -- not a synthetic guess at real-world shape.

Found: `_SYSLOG_TAG_RE`'s own comment claimed it already covered "the
Loghub OpenSSH/Linux datasets" tag shape. Checked directly, that claim
was wrong for Linux and Thunderbird: PAM-backed daemons there commonly
log as "sshd(pam_unix)[19939]:" or "crond(pam_unix)[2915]:", not
"sshd[19939]:" -- real examples, pulled verbatim from
raw.githubusercontent.com/logpai/loghub/master/Linux/Linux_2k.log:

    Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure;
    logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=218.188.2.4

The parenthesized PAM module name has no path through the old
`_SYSLOG_TAG_RE` at all -- it fails to match "(" immediately after the
tag, so `extract_fields_syslog` silently returned {} for every line
shaped this way, not just declined to recognize the message body (the
tag-prefix match itself failed, same class of bug the systemd-logind
hyphen fix and the ordering fix in earlier rounds both were).

Fixed: `_SYSLOG_TAG_RE` gained an optional `(?P<module>[\\w-]+)`
parenthesized group between the tag and the optional `[pid]`. The module
name itself isn't used for anything -- once the tag and message body
split out correctly, the existing KV fallback extractor handles the
message body ("authentication failure; logname= uid=0 ... rhost=X
user=Y" is KV-shaped) the same way it already does for any other daemon.

Still NOT covered by this fix, disclosed rather than silently assumed
fixed: a raw, unmodified real syslog line almost always has a LEADING
"Mon DD HH:MM:SS host " (or, for Thunderbird, an even longer custom
prefix) before the tag -- `_SYSLOG_TAG_RE` is and remains anchored at
the start of the string, so that header still has to be stripped by
something upstream before this function sees the line at all. See
validation/real_data/inject_and_evaluate.py's own comments for the
honest, disclosed state of that separate gap, including the fact that
this project's own logstash/redact-pipeline.conf does not currently
strip it either.

Run: python validation/syslog_coverage_extension_round4_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import fields  # noqa: E402
from faker import Faker  # noqa: E402

fake = Faker()
Faker.seed(271828)  # distinct from every prior syslog test seed

NEW_DIRTY_TEMPLATES = [
    # The exact real shape found in Linux_2k.log, parameterized.
    ("sshd(pam_unix)[{pid}]: authentication failure; logname= uid=0 euid=0 "
     "tty=NODEVssh ruser= rhost={ip}  user={user}",
     "sshd.rhost", "sshd.user"),
    # A different daemon, same PAM-decorated tag shape, confirming the fix
    # is general to the tag format and not special-cased to sshd.
    ("crond(pam_unix)[{pid}]: authentication failure; logname= uid=0 "
     "euid=0 tty=cron ruser= rhost={ip}  user={user}",
     "crond.rhost", "crond.user"),
]

EXISTING_DIRTY_TEMPLATES = [
    # Unchanged shapes from the original extractor and all three prior
    # extension rounds -- must still work identically after this round's
    # tag-regex change (an additive, optional group should never affect
    # lines that don't have a parenthesized module in the tag).
    ("sshd[{pid}]: Failed password for {user} from {ip} port {port} ssh2",
     "sshd.user", "sshd.src_ip"),
    ("sshd[{pid}]: Accepted publickey for {user} from {ip} port {port} ssh2",
     "sshd.user", "sshd.src_ip"),
    ("sshd[{pid}]: Disconnected from user {user} {ip} port {port}",
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
    ("systemd-logind[{pid}]: New session {num} of user {user}.",
     "systemd-logind.user", None),
    ("CRON[{pid}]: ({user}) CMD (/home/{user}/backup.sh --verbose)",
     "CRON.user", "CRON.command"),
    ("cron[{pid}]: ({user}) RELOAD (crontabs/{user})",
     "cron.user", "cron.crontab"),
    ("NetworkManager[{pid}]: <info>  [{epoch_ts}] dhcp4 (eth0): address {ip}",
     "NetworkManager.src_ip", "NetworkManager.iface"),
    ("systemd[1]: {service}.service: Failed with result 'exit-code'.",
     "systemd.unit", "systemd.result"),
]

CLEAN_TEMPLATES = [
    # Deliberately still-uncovered shapes -- must produce zero fields.
    "sshd[{pid}]: Connection closed by {ip} port {port} [preauth]",
    "cron[{pid}]: (CRON) INFO (Reloading configuration)",
    "systemd[1]: Starting Daily apt download activities...",
    # New this round: a malformed/unsupported tag decoration -- a bracket
    # where a paren is expected -- must still fail cleanly (return {}),
    # not partially match and silently produce a wrong field. Confirms
    # the new module group didn't loosen the regex in a way that lets
    # garbage through.
    "sshd[pam_unix][{pid}]: authentication failure; user={user}",
]


def build_line(template: str) -> str:
    return template.format(
        pid=fake.random_int(1000, 9999),
        user=fake.user_name(),
        ip=fake.ipv4_public(),
        port=fake.random_int(1024, 65535),
        ts=fake.iso8601(),
        epoch_ts=f"{fake.unix_time():.4f}",
        num=fake.random_int(1, 9999),
        service=fake.word(),
    )


def main():
    n_per_template = 100

    print("=== New syslog patterns (added this round: PAM-decorated tag) ===")
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
          f"({covered_new / total_new:.1%})")

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
          f"({covered_existing / total_existing:.1%})")

    print("\n=== Clean/malformed lines (false-extraction check) ===")
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

    # Real-data spot check: the exact line pulled from Linux_2k.log in
    # this file's own docstring, verbatim (not templated/regenerated),
    # confirming the fix works on the actual real text, not just a
    # Faker-parameterized approximation of it.
    print("\n=== Verbatim real-data spot check (from Linux_2k.log) ===")
    real_line = ("sshd(pam_unix)[19939]: authentication failure; logname= "
                  "uid=0 euid=0 tty=NODEVssh ruser= rhost=218.188.2.4")
    result = fields.extract_fields_syslog(real_line)
    real_ok = result.get("sshd.rhost") == "218.188.2.4"
    print(f"  {real_line!r}\n  -> {result}")
    print(f"  sshd.rhost correctly extracted: {real_ok}")

    print("\n=== Summary ===")
    ok = (covered_new == total_new and covered_existing == total_existing
          and false_positives == 0 and real_ok)
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
