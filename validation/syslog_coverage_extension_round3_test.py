"""
ROADMAP item 8, third follow-on (2026-08-08). Round 2
(validation/syslog_coverage_extension_round2_test.py, same day) closed
systemd-logind/cron-CMD/NetworkManager-dhcp4 and left four gaps
explicitly named in extract_fields_syslog's own docstring: "other systemd
unit-failure messages, other NetworkManager message types like Wi-Fi SSID
changes, cron's own startup/reload messages as opposed to job execution,
and any auth wording not in the sshd list." This test measures closing
all four, against a fresh, dedicated, Faker-seeded corpus (same reasoning
as both previous extension tests: deliberately NOT
data/synthetic_logs.jsonl, so this doesn't perturb any number already
tied to that corpus's exact content).

New shapes covered, added the same day as this test:
  - sshd "Disconnected from (authenticating) user X Y port Z" -- a
    session-teardown message at least as common as the login-attempt
    messages already covered, still carrying a username.
  - cron "(X) RELOAD (crontab-path)" -- cron's own configuration-reload
    message, the counterpart to the already-covered "(X) CMD (...)"
    job-execution message.
  - NetworkManager "<info> [timestamp] device (iface): Activation:
    (wifi) connected to 'SSID'" -- closes the Wi-Fi SSID gap the
    round-2 dhcp4 pattern's own comment explicitly left open.
  - systemd "unit.service: Failed with result 'exit-code'." -- closes
    the "other systemd unit-failure messages" gap (distinct from
    systemd-logind's session messages, a different systemd subsystem).

Run: python validation/syslog_coverage_extension_round3_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import fields  # noqa: E402
from faker import Faker  # noqa: E402

fake = Faker()
Faker.seed(31337)  # distinct from every prior syslog test seed (42, 4242, 9191)

NEW_DIRTY_TEMPLATES = [
    ("sshd[{pid}]: Disconnected from user {user} {ip} port {port}",
     "sshd.user", "sshd.src_ip"),
    ("sshd[{pid}]: Disconnected from authenticating user {user} {ip} port {port} [preauth]",
     "sshd.user", "sshd.src_ip"),
    ("cron[{pid}]: ({user}) RELOAD (crontabs/{user})",
     "cron.user", "cron.crontab"),
    ("NetworkManager[{pid}]: <info>  [{epoch_ts}] device (wlan0): Activation: "
     "(wifi) connected to 'HomeNetwork-{user}'",
     "NetworkManager.ssid", "NetworkManager.iface"),
    ("systemd[1]: {service}.service: Failed with result 'exit-code'.",
     "systemd.unit", "systemd.result"),
]

EXISTING_DIRTY_TEMPLATES = [
    # Unchanged shapes from the original extractor and both prior
    # extension rounds -- must still work identically after this round's
    # pattern insertions.
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
    ("systemd-logind[{pid}]: New session {num} of user {user}.",
     "systemd-logind.user", None),
    ("CRON[{pid}]: ({user}) CMD (/home/{user}/backup.sh --verbose)",
     "CRON.user", "CRON.command"),
    ("NetworkManager[{pid}]: <info>  [{epoch_ts}] dhcp4 (eth0): address {ip}",
     "NetworkManager.src_ip", "NetworkManager.iface"),
]

CLEAN_TEMPLATES = [
    # Deliberately still-uncovered shapes -- must produce zero fields,
    # same as any message shape outside the pattern list.
    "sshd[{pid}]: Connection closed by {ip} port {port} [preauth]",  # no
        # "user" in this specific wording variant -- distinct from the
        # "Disconnected from user X" shape just added, on purpose, to
        # prove that pattern isn't accidentally over-broad.
    "cron[{pid}]: (CRON) INFO (Reloading configuration)",  # cron's
        # generic startup-info message, structurally different from both
        # "(X) CMD (...)" and the new "(X) RELOAD (...)" -- "CRON" here is
        # a literal daemon-internal token, not a real username, and this
        # message shape has no username-bearing structure this extractor
        # should be matching.
    "systemd[1]: Starting Daily apt download activities...",  # a systemd
        # unit *starting*, not *failing* -- different message shape than
        # the new unit-failure pattern, should not match it.
    "NetworkManager[{pid}]: <info>  [{epoch_ts}] Wi-Fi P2P device controlled by wpa_supplicant",  # still
        # a different NetworkManager message type than the new Wi-Fi
        # connection pattern.
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

    print("=== New syslog patterns (added this session, round 3) ===")
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
