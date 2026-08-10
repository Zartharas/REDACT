"""
Extracts (field_name, value) pairs from structured or semi-structured log
lines, so drift detection (drift.py) can track PII hit rate per field rather
than per whole log line.

Scope: this only works where a log format has an identifiable field
structure.
  - cloudtrail: real JSON, flattened to dotted paths.
  - windows_event: key=value pairs, extracted with a regex that tolerates
    values containing spaces (real Windows Event exports frequently have
    multi-word values like a person's name in TargetUserName).
  - syslog: PARTIALLY supported (added 2026-08-07, see
    extract_fields_syslog's own docstring for exactly what is and isn't
    covered). Earlier versions of this module treated syslog as
    unsupported outright, since most syslog message bodies are free text
    with no generic field boundary a parser could locate -- that's still
    true for message shapes this extractor doesn't recognize, and those
    still fall through to zero extracted fields (message-level blindness
    for that line), not a fabricated field structure that isn't really
    there.
"""
import json
import re

_KV_KEY_RE = re.compile(r"(\w+)=")


def _extract_kv_pairs(text: str) -> dict[str, str]:
    """Tolerant key=value extractor, shared by the windows_event and
    syslog (KV-shaped messages) extractors below. A value runs from after
    '=' up to the start of the next recognized key=, or end of string --
    this is what lets 'TargetUserName=Timothy Wong FailureReason=...'
    correctly assign 'Timothy Wong' to TargetUserName instead of splitting
    on every space."""
    matches = list(_KV_KEY_RE.finditer(text))
    fields = {}
    for i, m in enumerate(matches):
        key = m.group(1)
        value_start = m.end()
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[value_start:value_end].strip()
        fields[key] = value
    return fields


def extract_fields_windows_event(text: str) -> dict[str, str]:
    return _extract_kv_pairs(text)


def extract_fields_cloudtrail(text: str) -> dict[str, str]:
    """Flattens nested JSON into dotted field paths. Only leaf string values
    are returned -- numbers, booleans, and nulls carry no PII risk in this
    framework's taxonomy and are excluded so they don't dilute hit-rate
    denominators with fields that could never be flagged."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    fields: dict[str, str] = {}

    def walk(obj, prefix: str):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, str):
            fields[prefix] = obj
        # ints/floats/bools/None/lists of non-strings: intentionally skipped

    walk(data, "")
    return fields


# Tag prefix common to syslog lines this project's generator uses:
# "process[pid]: rest" or "process: rest" (no pid). Captured separately
# from the message body so the two known message shapes below (KV-style,
# and the sshd preposition-style auth line) can each parse just the body,
# not have to also account for the tag prefix in their own patterns.
#
# [\w-]+, not \w+: found while adding the systemd-logind pattern below
# (2026-08-08) -- a plain \w+ doesn't match the hyphen in real daemon tags
# like "systemd-logind", "systemd-resolved", "systemd-networkd", so the
# whole tag-prefix match would fail on those lines and the function would
# return {} for every one of them instead of merely declining to
# recognize the message body shape. Widening the tag character class
# fixes this for any hyphenated daemon name, not only the one that
# surfaced it.
#
# Optional (?P<module>[\w-]+) parenthesized group added 2026-08-09
# (ROADMAP item 8 follow-on, Task #10's real-data validation): the
# earlier comment here claimed this regex already covered "the Loghub
# OpenSSH/Linux datasets" tag shape -- checked directly against the real,
# unmodified Loghub files (validation/real_data/download_loghub.sh) while
# extending the field-gate validation to real data, and that claim was
# WRONG for Linux and Thunderbird: PAM-backed daemons commonly log as
# "sshd(pam_unix)[19939]:" or "crond(pam_unix)[2915]:", not
# "sshd[19939]:" -- the parenthesized PAM module name has no path through
# the old regex at all (fails to match "(" after the tag), so
# extract_fields_syslog silently returned {} for every line shaped this
# way, not just declined to recognize the message body. The module name
# itself isn't used for anything (matched and discarded, same as the pid
# group when a caller doesn't need it) -- what matters is that the tag
# and message body still split out correctly when it's present.
#
# Still does NOT handle a leading RFC3164 "Mon DD HH:MM:SS host " (or,
# for Thunderbird, an even longer custom multi-field) prefix before the
# tag -- this regex is and remains anchored at the start of the STRING,
# so a raw, unmodified real syslog line (which almost always has that
# header) still returns {} until something upstream strips it first. See
# validation/real_data/inject_and_evaluate.py's own comments on
# _strip_syslog_header for the honest, disclosed state of that gap,
# including the fact that this project's OWN logstash/redact-pipeline.conf
# does not currently do that stripping either -- this project's synthetic
# syslog templates (generate_logs.py) never included a timestamp/host
# prefix, so this gap was invisible to every synthetic-only test run
# before this one.
_SYSLOG_TAG_RE = re.compile(
    r"^(?P<tag>[\w-]+)(?:\((?P<module>[\w-]+)\))?(?:\[(?P<pid>\d+)\])?:\s*(?P<rest>.*)$"
)

# sshd auth-message shapes. Order matters: "Failed password for invalid
# user X" must be tried before the plain "Failed password for X" pattern,
# since the plain pattern would otherwise greedily (and wrongly) capture
# "invalid" as part of the username.
_SYSLOG_SSHD_PATTERNS = [
    re.compile(r"^Failed password for invalid user (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Failed password for (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Accepted password for (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    # Added alongside the su/useradd/dhclient patterns below (ROADMAP item
    # 8 follow-on): key-based auth is at least as common as password auth
    # in real sshd logs, and the original pattern list only covered
    # "Accepted password for", never "Accepted publickey for" -- an
    # oversight, not a deliberate scope decision, so it belongs in this
    # same list rather than a new one.
    re.compile(r"^Accepted publickey for (?P<user>\S+) from (?P<src_ip>\S+) port (?P<port>\d+)"),
    re.compile(r"^Invalid user (?P<user>\S+) from (?P<src_ip>\S+)"),
    re.compile(r"^User (?P<user>\S+) from (?P<src_ip>\S+) not allowed"),
    # Added 2026-08-08 (round 3, ROADMAP item 8's "any auth wording not in
    # the sshd list" gap): session-teardown messages are at least as common
    # in real sshd logs as the login-attempt messages above, and OpenSSH
    # logs a username in both the normal-disconnect and preauth-disconnect
    # cases. "authenticating " is optional (present when the disconnect
    # happens before auth completes, e.g. a scanner or brute-force attempt
    # that never got past the username prompt).
    re.compile(r"^Disconnected from (?:authenticating )?user (?P<user>\S+) (?P<src_ip>\S+) port (?P<port>\d+)"),
]

# A second batch of message shapes added 2026-08-07 (ROADMAP item 8
# follow-on, closing part of the "other daemons, other wording" gap the
# original extract_fields_syslog docstring named as still open). Each
# targets one specific, common real-world syslog message shape rather
# than attempting a general-purpose parser -- the same scoped-but-honest
# approach the original sshd patterns above already took, just extended
# to a few more daemons: su (privilege escalation -- who ran su, and as
# whom), and dhclient (which host was handed which IP address, relevant
# to the same IP-address PII category regex/NER already detect
# elsewhere). useradd/usermod deliberately are NOT handled by a new
# pattern here -- see the KV-separator fix below instead, since their
# real-world message shape is comma-separated key=value pairs, not a
# preposition-based sentence, and the existing tolerant KV extractor
# already covers that shape once it knows to treat ',' as a pair
# separator the same way it already treats ';'.
_SYSLOG_SU_PATTERN = re.compile(
    r"^pam_unix\(su:session\): session opened for user \S+ by (?P<user>\S+)\(uid=\d+\)"
)
_SYSLOG_DHCLIENT_PATTERNS = [
    re.compile(r"^DHCPACK from (?P<src_ip>\S+)"),
    re.compile(r"^DHCPOFFER from (?P<src_ip>\S+)"),
]

# Third batch, added 2026-08-08, closing three more of the specific gaps
# extract_fields_syslog's own docstring had named as still open:
# NetworkManager, systemd, and cron. Same scoped approach as the su/
# dhclient patterns above -- one hand-written pattern per common real
# message shape, not a general-purpose parser.
#
# systemd-logind: "New session 42 of user donaldgarcia." -- logged once
# per login session (SSH, console, or otherwise), so this is a genuinely
# common source of a username landing in syslog outside sshd's own
# messages. Tag is "systemd-logind", which needed the _SYSLOG_TAG_RE fix
# above to even reach this pattern at all.
_SYSLOG_LOGIND_PATTERN = re.compile(
    r"^New session (?P<session_id>\d+) of user (?P<user>\S+)\.$"
)

# cron: "(donaldgarcia) CMD (/home/donaldgarcia/backup.sh --verbose)" --
# the parenthesized username before "CMD" is cron's own standard log
# format (every distribution's cron logs this way), and is exactly the
# "cron with PII-bearing arguments" gap the docstring named, at least for
# the username itself; the command's own arguments remain free text this
# pattern doesn't attempt to parse further (see the docstring update
# below for why: an argument could be anything, unlike a fixed-position
# username).
_SYSLOG_CRON_PATTERN = re.compile(
    r"^\((?P<user>\S+)\)\s+CMD\s+\((?P<command>.+)\)$"
)

# cron's own reload/startup message ("(root) RELOAD (crontabs/donaldgarcia)",
# logged once whenever crond notices a crontab file changed), added
# 2026-08-08 (round 3) -- closes the "cron's own non-job-execution
# messages" gap the docstring named as still open. Same parenthesized-
# username shape as the CMD pattern above, just a different keyword and a
# crontab path (which itself frequently embeds a username, e.g.
# "crontabs/donaldgarcia") instead of a command.
_SYSLOG_CRON_RELOAD_PATTERN = re.compile(
    r"^\((?P<user>\S+)\)\s+RELOAD\s+\((?P<crontab>.+)\)$"
)

# NetworkManager: "<info>  [1620000000.1234] dhcp4 (eth0): address
# 192.168.1.105" -- NetworkManager's own DHCP client logs the leased
# address the same way dhclient does above, just wrapped in
# NetworkManager's severity-tag/timestamp prefix instead of dhclient's
# bare message body. Deliberately narrow: only the dhcp4-address shape is
# covered, not NetworkManager's many other message types (interface state
# changes, policy decisions, Wi-Fi SSID changes -- the last of which can
# itself carry PII-adjacent data, e.g. a network named after a person, but
# isn't covered here; a real deployment logging that would need its own
# pattern, same disclosure as everything else in this function).
_SYSLOG_NETWORKMANAGER_PATTERN = re.compile(
    r"^<info>\s+\[[\d.]+\]\s+dhcp4\s+\((?P<iface>[\w.]+)\):\s+address\s+(?P<src_ip>\S+)"
)

# NetworkManager Wi-Fi connection messages ("<info> [1620000000.1234]
# device (wlan0): Activation: (wifi) connected to 'HomeNetwork-JSmith'"),
# added 2026-08-08 (round 3) -- closes the "Wi-Fi SSID changes" gap the
# original NetworkManager pattern's own comment explicitly named as still
# open, and the docstring called out as an example of PII-adjacent data
# this extractor didn't yet reach (a network named after a person, e.g. a
# home router's default or user-chosen SSID). Extracts the SSID as `ssid`,
# same field-level exposure the dhcp4-address pattern above already gives
# the leased IP.
_SYSLOG_NETWORKMANAGER_WIFI_PATTERN = re.compile(
    r"^<info>\s+\[[\d.]+\]\s+device\s+\((?P<iface>[\w.]+)\):\s+Activation:\s+\(wifi\)\s+connected to '(?P<ssid>[^']+)'"
)

# systemd unit-failure messages ("myapp.service: Failed with result
# 'exit-code'."), added 2026-08-08 (round 3) -- closes the "other systemd
# unit-failure messages" gap the docstring named as still open, alongside
# the systemd-logind session pattern above (a different systemd
# subsystem's message shape entirely -- logind logs sessions, this is the
# service manager itself logging a unit's failure). Extracts the unit
# name and failure result. In practice a unit name rarely carries PII on
# its own (unlike systemd-logind's username or NetworkManager's SSID
# above), so this pattern closes the documented coverage gap more than it
# adds new PII-detection value -- included for completeness and because a
# custom unit name COULD embed a person's name in an unusual deployment
# (e.g. a per-user systemd timer unit).
_SYSLOG_SYSTEMD_UNIT_FAILURE_PATTERN = re.compile(
    r"^(?P<unit>\S+\.service): Failed with result '(?P<result>[\w-]+)'\.$"
)


def extract_fields_syslog(text: str) -> dict[str, str]:
    """Partial syslog field extraction, added 2026-08-07 (ROADMAP item 8).

    Scope, consistent with the rest of this module: syslog message bodies
    are free text in general, and there is no generic parser that reliably
    locates field boundaries across arbitrary syslog content -- that
    limitation from this module's original docstring still holds.
    What changed is that a meaningful fraction of real syslog traffic
    (this project's own three syslog templates in generate_logs.py, and
    common real sshd auth-log lines in datasets like Loghub's OpenSSH) DOES
    have a recognizable, stable shape, and it's worth extracting fields
    from exactly those shapes rather than treating all syslog as opaque.

    Ten shapes are recognized, checked in this order after stripping the
    leading "tag[pid]: " or "tag: " prefix:

    1. KV-style bodies (this project's `sudo` and `kernel` templates, e.g.
       "PWD=/home/donaldgarcia ; USER=root ; COMMAND=..." or
       "IN=eth0 SRC=1.2.3.4 DST=10.0.0.1 PROTO=TCP"; also useradd/usermod-
       style messages using ',' as the pair separator instead of ';',
       added 2026-08-07): reuses the same tolerant key=value extractor
       windows_event already uses, since the underlying shape is
       identical once the syslog tag prefix is stripped off.
    2. sshd authentication and session messages ("Failed password for X
       from Y port Z", "Accepted password for...", "Accepted
       publickey for..." (added 2026-08-07 -- key-based auth is at least
       as common as password auth in real sshd logs and was an oversight,
       not a scope decision), "Invalid user X from Y", "Disconnected from
       user X Y port Z" / "Disconnected from authenticating user X Y port
       Z" (added 2026-08-08, round 3 -- session-teardown messages are at
       least as common as login-attempt messages in real sshd logs, and
       still carry a username), etc.): these have no `=` characters at
       all, so they'd never match the KV extractor, but they DO have a
       stable preposition-based structure ("for X from Y") that a small
       set of hand-written patterns can reliably parse into named fields
       (user, src_ip, port).
    3. su privilege-escalation messages (added 2026-08-07): "pam_unix
       (su:session): session opened for user root by donaldgarcia(uid=0)"
       -- who ran su, extracted as `user`.
    4. dhclient DHCP lease messages (added 2026-08-07): "DHCPACK from
       192.168.1.1", "DHCPOFFER from 192.168.1.1" -- the IP address a
       lease was granted by, extracted as `src_ip` (the same IP-address
       PII category regex/NER already detect elsewhere in this
       framework's taxonomy, just reachable at the field level here too).
    5. systemd-logind session messages (added 2026-08-08): "New session
       42 of user donaldgarcia." -- logged once per login session
       regardless of how the user authenticated, so this is a second,
       independent source of a username landing in syslog structure,
       not just sshd's own auth messages. Required widening
       `_SYSLOG_TAG_RE` to accept hyphens in the tag itself
       ("systemd-logind"), which the original tag regex didn't.
    6. cron job execution messages (added 2026-08-08): "(donaldgarcia)
       CMD (/home/donaldgarcia/backup.sh --verbose)" -- cron's own
       standard log format across distributions, parenthesized username
       before "CMD". Extracts `user`; the command's own arguments are
       captured as `command` but not parsed further (an argument's shape
       is arbitrary, unlike a fixed-position username).
    7. NetworkManager DHCP lease messages (added 2026-08-08): "<info>
       [1620000000.1234] dhcp4 (eth0): address 192.168.1.105" --
       NetworkManager's own DHCP client logging the same kind of leased
       address dhclient does above, just wrapped in NetworkManager's own
       severity-tag/timestamp prefix.
    8. cron reload/startup messages (added 2026-08-08, round 3): "(root)
       RELOAD (crontabs/donaldgarcia)" -- logged whenever crond notices a
       crontab file changed, the parenthesized username's counterpart to
       shape 6's job-execution message; the crontab path itself frequently
       embeds a username too, extracted as `crontab`.
    9. NetworkManager Wi-Fi connection messages (added 2026-08-08, round
       3): "<info> [1620000000.1234] device (wlan0): Activation: (wifi)
       connected to 'HomeNetwork-JSmith'" -- closes the "Wi-Fi SSID
       changes" gap shape 7's own pattern explicitly named as still open;
       an SSID is PII-adjacent when a network is named after a person.
    10. systemd unit-failure messages (added 2026-08-08, round 3):
       "myapp.service: Failed with result 'exit-code'." -- a unit name
       rarely carries PII on its own (unlike shape 5's username or shape
       9's SSID), so this one is included mainly to close the documented
       coverage gap rather than for strong new PII-detection value.

    Message shapes that match none of the above -- most notably the
    free-text content of a sudo COMMAND value or cron CMD argument, other
    NetworkManager message types beyond DHCP leases and Wi-Fi connection
    (interface state changes, policy decisions), other systemd message
    types beyond unit-failure and login sessions (unit start/stop,
    timers), or any auth-message wording not in the sshd pattern list --
    fall through to zero extracted fields for that line, same as before
    this function existed. This is a deliberate, honest partial fix: it
    closes the gap for the message shapes actually present in this
    project's own corpus, common real sshd logs, and a handful of other
    common daemons (su, dhclient, useradd, systemd-logind, systemd unit
    failures, cron, NetworkManager), not a claim that syslog is now fully
    covered the way windows_event/cloudtrail are. A production deployment
    logging different syslog message shapes would need its own additional
    patterns here, following the same approach.
    """
    m = _SYSLOG_TAG_RE.match(text)
    if not m:
        return {}
    tag, rest = m.group("tag"), m.group("rest")

    # Preposition/sentence-shaped patterns are tried BEFORE the generic
    # KV fallback below, not after. Found live while testing the su and
    # dhclient patterns added 2026-08-07: both message shapes legitimately
    # contain a bare "=" somewhere in a non-KV context ("by
    # jsmith(uid=0)", "(xid=0x12345678)"), which used to trigger the KV
    # branch's `if "=" in rest` check first and produce a wrong,
    # nonsensical field (e.g. `su.uid: "0)"`) instead of ever reaching
    # these more specific patterns. Trying the specific shapes first and
    # falling back to the generic KV extractor only if none of them match
    # fixes this without weakening the KV extractor itself for the shapes
    # that actually need it (sudo, kernel, useradd).
    for pattern in _SYSLOG_SSHD_PATTERNS:
        sm = pattern.match(rest)
        if sm:
            return {f"{tag}.{k}": v for k, v in sm.groupdict().items() if v is not None}

    su_match = _SYSLOG_SU_PATTERN.match(rest)
    if su_match:
        return {f"{tag}.{k}": v for k, v in su_match.groupdict().items() if v is not None}

    for pattern in _SYSLOG_DHCLIENT_PATTERNS:
        dm = pattern.match(rest)
        if dm:
            return {f"{tag}.{k}": v for k, v in dm.groupdict().items() if v is not None}

    logind_match = _SYSLOG_LOGIND_PATTERN.match(rest)
    if logind_match:
        return {f"{tag}.{k}": v for k, v in logind_match.groupdict().items() if v is not None}

    # Tried before the KV fallback below, same reason as su/dhclient
    # above: cron's own command field can legitimately contain "=" inside
    # an argument (e.g. "CMD (/usr/bin/backup.sh --verbose=true)"), which
    # would otherwise wrongly trigger the generic KV branch first and
    # produce a nonsensical field instead of ever reaching this pattern.
    cron_match = _SYSLOG_CRON_PATTERN.match(rest)
    if cron_match:
        # .strip() on the command group specifically: real crontab logs
        # commonly pad the command with leading whitespace for column
        # alignment ("CMD (   /usr/lib/php/sessionclean)"), which isn't
        # part of the actual command and shouldn't leak into the field
        # value.
        return {f"{tag}.{k}": (v.strip() if k == "command" else v)
                for k, v in cron_match.groupdict().items() if v is not None}

    cron_reload_match = _SYSLOG_CRON_RELOAD_PATTERN.match(rest)
    if cron_reload_match:
        return {f"{tag}.{k}": v for k, v in cron_reload_match.groupdict().items() if v is not None}

    nm_match = _SYSLOG_NETWORKMANAGER_PATTERN.match(rest)
    if nm_match:
        return {f"{tag}.{k}": v for k, v in nm_match.groupdict().items() if v is not None}

    nm_wifi_match = _SYSLOG_NETWORKMANAGER_WIFI_PATTERN.match(rest)
    if nm_wifi_match:
        return {f"{tag}.{k}": v for k, v in nm_wifi_match.groupdict().items() if v is not None}

    unit_failure_match = _SYSLOG_SYSTEMD_UNIT_FAILURE_PATTERN.match(rest)
    if unit_failure_match:
        return {f"{tag}.{k}": v for k, v in unit_failure_match.groupdict().items() if v is not None}

    if "=" in rest:
        kv = _extract_kv_pairs(rest)
        if kv:
            # Unlike windows_event's space-separated key=value pairs,
            # syslog KV-style messages use different pair separators
            # depending on the daemon: this project's `sudo` template
            # uses ';' ("PWD=/home/donaldgarcia ; USER=root ; ..."), and
            # useradd/usermod-style messages (added 2026-08-07, ROADMAP
            # item 8 follow-on) use ',' ("name=X, UID=1005, GID=1005,
            # ..."). _extract_kv_pairs's next-key lookahead doesn't know
            # to stop at either -- neither is a key=value token itself, so
            # the trailing separator ends up tacked onto the previous
            # value. Strip both here rather than complicate the shared KV
            # extractor for syslog-specific separator conventions.
            return {f"{tag}.{k}": v.rstrip(" ;,") for k, v in kv.items()}

    return {}


def extract_fields(log_type: str, text: str) -> dict[str, str]:
    if log_type == "windows_event":
        return extract_fields_windows_event(text)
    if log_type == "cloudtrail":
        return extract_fields_cloudtrail(text)
    if log_type == "syslog":
        return extract_fields_syslog(text)
    return {}  # unrecognized log_type
