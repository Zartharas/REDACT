"""
Background async re-checker for schema-trust-skipped fields -- the
MANDATORY mitigation DETECTION_POLICY_DECOUPLING_SCOPING.md's Phase 3
devil's-advocate section requires before shipping any "skip detection,
trust the schema" mechanism: a monitored, not just assumed, trust
boundary. Same daemon-thread-per-worker shape as policy_watcher.py's
PolicyWatcher (Phase 2), reused deliberately for consistency rather than
invented fresh.

WHY A QUEUE, NOT A DIRECT SYNCHRONOUS CALL from detect.py: the whole point
of schema-trust (src/schema_trust.py) is to skip the (comparatively
expensive) detection call for a declared field ON THE REQUEST HOT PATH.
Immediately re-running that same detection synchronously to "double-check"
would defeat the purpose entirely -- every request would pay the full
detection cost anyway, just relabeled. maybe_sample() below is a
near-zero-cost, NEVER-BLOCKING enqueue (a full queue drops the sample and
increments a counter -- backpressure-safe, never stalls a request); the
actual detection re-run happens later, off the hot path, in this module's
own background thread.
"""
import queue
import random
import threading
import time
import traceback

import detect
import schema_trust_audit

# Bounded so a sampler that falls behind (detection re-checks slower than
# they're being enqueued) can never grow this queue -- and therefore this
# worker's memory -- without limit. Dropping the oldest excess sample under
# sustained overload is an acceptable, disclosed cost: missing some
# samples during a burst is far better than an unbounded queue turning a
# traffic spike into an OOM.
_DEFAULT_MAXSIZE = 1000

# Types scan_entropy() can produce that aren't a real canonical PII type --
# excluded from "actual_types_found" the same way pipeline.py's own
# comment already excludes HIGH_ENTROPY from anonymize_by_policy() routing
# ("entropy hits carry type HIGH_ENTROPY... a review signal, not an
# auto-anonymization trigger").
_NON_CANONICAL_HIT_TYPES = {"HIGH_ENTROPY"}


class SchemaTrustSampler:
    """One instance per worker process, started once at service.py
    module-import time (same lifecycle as PolicyWatcher)."""

    def __init__(self, audit_key: str, audit_log_path: str,
                 sample_rate: float = 0.01, maxsize: int = _DEFAULT_MAXSIZE,
                 on_result=None):
        self.audit_key = audit_key
        self.audit_log_path = audit_log_path
        self.sample_rate = sample_rate
        # Optional callable(outcome: str) -> None, invoked at the end of
        # every processed sample -- same reasoning and same pattern as
        # policy_watcher.PolicyWatcher's own on_result hook: lets
        # service.py increment its own Prometheus Counter at the moment an
        # outcome happens without this module needing to import
        # prometheus_client itself. Wrapped in its own try/except so a
        # broken callback can never affect sample processing.
        self._on_result = on_result

        self._queue: "queue.Queue[tuple[str, str, str, str]]" = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

        self._lock = threading.Lock()
        self._samples_enqueued = 0
        self._samples_dropped = 0
        self._samples_processed = 0
        self._outcome_counts = {"consistent": 0, "no_signal": 0, "drift_detected": 0}
        self._last_processed_ts: "float | None" = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="schema-trust-sampler", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def maybe_sample(self, log_type: str, field_name: str,
                      declared_type: str, field_text: str) -> bool:
        """Called synchronously from the request path (service.py, right
        after detect.detect_all_schema_aware() returns a schema-trust
        span) -- MUST be cheap and MUST NEVER block. With probability
        self.sample_rate, enqueues (log_type, field_name, declared_type,
        field_text) for this worker's background thread to independently
        re-check later. Returns True if enqueued, False if skipped by the
        sample-rate roll or dropped because the queue is full (both are
        normal, expected outcomes, not errors)."""
        if random.random() >= self.sample_rate:
            return False
        try:
            self._queue.put_nowait((log_type, field_name, declared_type, field_text))
        except queue.Full:
            with self._lock:
                self._samples_dropped += 1
            return False
        with self._lock:
            self._samples_enqueued += 1
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_one(*item)
            except Exception:
                # Same reasoning as PolicyWatcher._run()'s identical guard:
                # a dead sampler thread is worse than one that occasionally
                # logs an unexpected error and keeps going, since a dead
                # thread silently stops providing the ONE safety net this
                # whole feature's devil's-advocate section requires.
                print(
                    f"schema_trust_sampler: unexpected error processing a "
                    f"sample, continuing:\n{traceback.format_exc()}",
                )

    def _process_one(self, log_type: str, field_name: str,
                      declared_type: str, field_text: str) -> None:
        # log_type=None deliberately: field_text is an isolated field VALUE
        # (already excised from its line by detect_all_schema_aware()), not
        # a full structured log line -- running log_type-aware field-gating
        # on a bare value doesn't apply, and would need fields.py to
        # recognize the isolated value as its own valid line, which it
        # won't. Plain detect_all_field_gated(text, log_type=None) is
        # exactly detect_all(use_ner=True)'s behavior (see that function's
        # own docstring), the correct "give it every detector, no
        # log-type-specific help" treatment for a bare value under
        # independent re-check.
        hits = detect.detect_all_field_gated(field_text, log_type=None)
        actual_types = {h["type"] for h in hits if h["type"] not in _NON_CANONICAL_HIT_TYPES}

        if declared_type in actual_types:
            outcome = "consistent"
        elif not actual_types:
            outcome = "no_signal"
        else:
            outcome = "drift_detected"

        with self._lock:
            self._samples_processed += 1
            self._outcome_counts[outcome] += 1
            self._last_processed_ts = time.time()

        event = schema_trust_audit.build_schema_trust_audit_event(
            outcome=outcome, log_type=log_type, field_name=field_name,
            declared_type=declared_type, actual_types_found=list(actual_types),
            audit_key=self.audit_key,
        )
        schema_trust_audit.append_schema_trust_audit_event(event, self.audit_log_path)

        if self._on_result is not None:
            try:
                self._on_result(outcome)
            except Exception:
                print(
                    f"schema_trust_sampler: on_result callback raised, "
                    f"ignoring (outcome was {outcome!r}):\n{traceback.format_exc()}",
                )

    def get_status(self) -> dict:
        with self._lock:
            return {
                "sample_rate": self.sample_rate,
                "queue_depth": self._queue.qsize(),
                "samples_enqueued": self._samples_enqueued,
                "samples_dropped": self._samples_dropped,
                "samples_processed": self._samples_processed,
                "outcome_counts": dict(self._outcome_counts),
                "last_processed_ts": self._last_processed_ts,
            }
