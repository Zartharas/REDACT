"""
Background poller that re-reads the policy file on an interval and applies
changes via anonymize.load_policy(), without needing a container restart.
Phase 2 of DETECTION_POLICY_DECOUPLING_SCOPING.md ("Engineering upgrade 19",
BUGS_AND_FIXES.md).

WHY POLLING, NOT inotify/watchdog: the scoping doc's Phase 2 description
offered two options -- "watch the policy file for changes... or expose an
authenticated admin endpoint." This module does both, but polling (not an
inotify-based watch) is the PRIMARY propagation mechanism, for a reason the
scoping doc didn't fully resolve: config/policy.json is bind-mounted from
the HOST filesystem (docker-compose.yml), and Docker bind mounts over
common storage drivers/OS combinations do not reliably deliver inotify
events for host-side edits into the container the way a native filesystem
does -- this is a well-documented Docker/inotify gap across mount types
(overlay2, various host OSes), not something this project's own testing
can fully characterize without matrices of host OS x storage driver this
sandbox has no access to. Polling the file's mtime+content hash costs
nothing to trust across that gap: it works identically whether inotify
delivery happens to work on a given host or not.

WHY EVERY GUNICORN WORKER RUNS ITS OWN POLLER, rather than one shared
reload happening once for the whole deployment: gunicorn's default
(non-`--preload`) worker model means each worker is a SEPARATE OS process
with its own independent copy of anonymize.py's module-level
PSEUDONYMIZE_TYPES/TOKENIZE_TYPES/REDACT_TYPES (same reason each worker
warms its own copy of the spaCy/Presidio model -- see
detect._get_analyzer()'s docstring in service.py). A single admin API call
hitting ONE worker (through a load balancer or directly) can only ever
update that one worker's own in-memory policy; there is no shared-memory
mechanism in this deployment for one worker to push new policy into its
siblings. Each worker running its own poller against the SAME shared
bind-mounted file is what makes every worker independently converge to the
same on-disk policy within one poll interval, without needing any new
inter-process communication this project would have to build, test, and
verify working (Redis pub/sub, a shared IPC channel) just for this.
Disclosed cost: workers converge within REDACT_POLICY_POLL_INTERVAL_S
seconds of each other, not atomically -- a request landing on Worker A
milliseconds after a file edit and a request landing on Worker B a few
seconds later can, briefly, be processed under different policies. This is
the real, disclosed tradeoff of "no new infrastructure," not a claim that
Phase 2 gives atomic cluster-wide policy changes.
"""
import hashlib
import os
import threading
import time
import traceback

import anonymize
import policy_audit


def _file_hash(path: str) -> "str | None":
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class PolicyWatcher:
    """One instance per worker process (created and .start()'d once at
    service.py module-import time, same lifecycle as anonymize.load_policy()
    itself). Not usable from pipeline.py -- that script is a one-shot batch
    job with no long-running process for a background thread to live in;
    Phase 2 is service.py-only, disclosed here rather than left implicit."""

    def __init__(self, resolved_path: str, audit_key: str,
                 audit_log_path: str, poll_interval_s: float = 10.0,
                 on_result=None):
        self.resolved_path = resolved_path
        self.audit_key = audit_key
        self.audit_log_path = audit_log_path
        self.poll_interval_s = poll_interval_s
        # Optional callable(outcome: str) -> None, invoked at the end of
        # every check_now() call regardless of outcome. This exists so
        # service.py can increment its own Prometheus Counter (labeled by
        # outcome) at the exact moment an outcome happens, without this
        # module needing to import prometheus_client itself -- keeping
        # PolicyWatcher testable and usable (tests/test_policy_hot_reload.py)
        # with no Flask/Prometheus dependency in play at all. Wrapped in
        # its own try/except below so a broken callback can never take
        # down policy checking itself -- the metric is a nice-to-have
        # observability signal, not something check_now()'s own
        # correctness should ever depend on.
        self._on_result = on_result

        self._lock = threading.Lock()
        self._last_known_hash = _file_hash(resolved_path)
        self._last_check_ts: "float | None" = None
        self._last_reload_ts: "float | None" = None
        self._last_outcome: "str | None" = None
        self._last_error: "str | None" = None
        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        # daemon=True: this thread must never keep the process alive on its
        # own -- a gunicorn worker shutting down (SIGTERM, restart, scale-
        # down) should not be blocked waiting for this loop to notice and
        # exit on its own schedule.
        self._thread = threading.Thread(
            target=self._run, name="policy-watcher", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Broad except, deliberately: if check_now() itself raised
            # something check_now() didn't already catch (a bug in THIS
            # module, not a bad policy file -- that case is handled inside
            # check_now() and never reaches here as an exception), the
            # alternative is this thread dying silently. A dead poller
            # thread with no crash log and no metric would leave a worker
            # silently frozen on whatever policy it last had -- worse than
            # a policy that's merely stale, since staleness is at least
            # bounded by the poll interval and a dead poller's staleness is
            # unbounded and invisible. Logging to stderr here is a last-
            # resort backstop, not the primary observability path (that's
            # get_status()'s timestamps, surfaced via /admin/policy and the
            # Prometheus gauges in service.py).
            try:
                self.check_now(triggered_by="poller")
            except Exception:
                print(
                    f"policy_watcher: unexpected error in poll loop, "
                    f"continuing (see traceback below):\n{traceback.format_exc()}",
                )
            self._stop_event.wait(self.poll_interval_s)

    def check_now(self, triggered_by: str = "manual") -> dict:
        """Check the resolved path's current content hash against what's
        already loaded; if changed, attempt anonymize.load_policy(). Safe
        to call from any thread (guarded by self._lock) and safe to call
        concurrently with the background poller (used by service.py's
        POST /admin/policy/reload to short-circuit the wait for the next
        scheduled tick on THIS worker specifically).

        Returns a dict describing what happened -- never raises
        PolicyConfigError itself (that's the whole point: a running
        worker must survive a bad edit, unlike the module-import-time
        load_policy() call in service.py's own startup path, which is
        SUPPOSED to crash a worker that's still starting up rather than
        ever come up with a bad policy)."""
        with self._lock:
            self._last_check_ts = time.time()
            new_hash = _file_hash(self.resolved_path)

            if new_hash == self._last_known_hash:
                self._last_outcome = "unchanged"
                self._last_error = None
                result = {"outcome": "unchanged", "path": self.resolved_path}
            else:
                old_hash = self._last_known_hash
                try:
                    anonymize.load_policy(path=self.resolved_path)
                except anonymize.PolicyConfigError as e:
                    # Deliberately do NOT update self._last_known_hash here
                    # -- if the same broken edit is still on disk next
                    # poll, this method compares against the ORIGINAL
                    # last-good hash again (not the broken one), so the
                    # file keeps registering as "changed" and keeps
                    # retrying every poll rather than silently giving up
                    # on ever detecting a subsequent fix. anonymize.load_policy()
                    # itself guarantees it never mutates
                    # PSEUDONYMIZE_TYPES/TOKENIZE_TYPES/REDACT_TYPES before
                    # finishing validation (see that function's own
                    # comment) -- so the live policy this worker is
                    # actively using for real requests is untouched by
                    # this failure.
                    self._last_outcome = "rejected"
                    self._last_error = str(e)
                    event = policy_audit.build_policy_audit_event(
                        outcome="rejected", resolved_path=self.resolved_path,
                        old_policy_hash=old_hash, new_policy_hash=new_hash,
                        triggered_by=triggered_by, audit_key=self.audit_key,
                        error=str(e),
                    )
                    policy_audit.append_policy_audit_event(event, self.audit_log_path)
                    result = {"outcome": "rejected", "path": self.resolved_path, "error": str(e)}
                else:
                    self._last_known_hash = new_hash
                    self._last_reload_ts = time.time()
                    self._last_outcome = "reloaded"
                    self._last_error = None
                    event = policy_audit.build_policy_audit_event(
                        outcome="reloaded", resolved_path=self.resolved_path,
                        old_policy_hash=old_hash, new_policy_hash=new_hash,
                        triggered_by=triggered_by, audit_key=self.audit_key,
                    )
                    policy_audit.append_policy_audit_event(event, self.audit_log_path)
                    result = {"outcome": "reloaded", "path": self.resolved_path}

            if self._on_result is not None:
                try:
                    self._on_result(result["outcome"])
                except Exception:
                    print(
                        f"policy_watcher: on_result callback raised, "
                        f"ignoring (outcome was {result['outcome']!r}):\n"
                        f"{traceback.format_exc()}",
                    )
            return result

    def get_status(self) -> dict:
        with self._lock:
            return {
                "resolved_path": self.resolved_path,
                "poll_interval_s": self.poll_interval_s,
                "worker_pid": os.getpid(),
                "current_policy_hash": self._last_known_hash,
                "last_check_ts": self._last_check_ts,
                "last_reload_ts": self._last_reload_ts,
                "last_outcome": self._last_outcome,
                "last_error": self._last_error,
                "pseudonymize_types": sorted(anonymize.PSEUDONYMIZE_TYPES),
                "tokenize_types": sorted(anonymize.TOKENIZE_TYPES),
                "redact_types": sorted(anonymize.REDACT_TYPES),
            }
