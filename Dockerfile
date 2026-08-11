# Builds the redact-service container (src/service.py) -- the tested
# detection/anonymization/audit logic behind a Flask API. This is a
# standard Python/Flask container; nothing here is version-sensitive the
# way the OpenSearch or Logstash images are, so it's the one piece of this
# Docker setup closest to "should just work" without further verification.

# Engineering upgrade: split into a builder stage and a final runtime
# stage. requirements.txt (presidio-analyzer/anonymizer, spaCy underneath
# them) pulls in pip's own build machinery and the en_core_web_lg model
# download -- none of that needs to exist in the image that actually runs
# in production. Building into a venv here and copying only the finished
# venv into the final stage keeps pip's build artifacts, any transient
# build-time files, and this stage's own base-image layer history out of
# what ships. Both stages still use the same python:3.12-slim base, so
# this is not a distroless/Alpine-style minimal-base change (spaCy's own
# compiled dependencies are the reason a slim Debian base was chosen over
# Alpine's musl libc in the first place -- untested here, not claimed).
FROM python:3.12-slim AS builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY requirements.txt requirements-redis.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-redis.txt \
    && python -m spacy download en_core_web_lg
# requirements-redis.txt added to the base install, 2026-08-10 (ROADMAP
# item 12): this same image now also runs src/queue_consumer.py
# (docker-compose.yml's queue-consumer service, `command:
# ["python3", "src/queue_consumer.py"]`), which needs the redis package
# unconditionally -- not "only where you're actually configuring Redis"
# the way requirements-redis.txt's own header comment describes for
# RedisStorageProvider, since queue-consumer's whole job is reading from
# Redis. Checked while writing this comment, worth stating plainly since
# it's a real, separate, pre-existing gap this change doesn't touch:
# src/service.py has no env-var-driven way to select RedisStorageProvider
# at all right now -- it always calls
# `anonymize.TokenStore(TOKEN_STORE_PATH, ...)` with a bare path, which
# anonymize.py's own TokenStore.__init__ auto-wraps in FileStorageProvider
# for backward compatibility (see anonymize.py). RedisStorageProvider is
# fully implemented and verified (Bug 14/ROADMAP item 6, live multi-
# process testing) but only ever exercised directly by validation
# scripts, never wired up as a redact-service runtime option. Installing
# the redis package here does NOT change that -- it's needed for
# queue_consumer.py only. Wiring an actual env-var switch into
# service.py is a separate, real, not-yet-done task, not implied by
# anything in this Dockerfile change.

FROM python:3.12-slim

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY src/ ./src/

ENV REDACT_TOKEN_STORE_PATH=/app/output/token_store.json
# REDACT_PSEUDO_KEY and REDACT_AUDIT_KEY are intentionally NOT baked in here.
# Set them via docker-compose environment or a secrets mechanism -- shipping
# a default key in an image is exactly the kind of thing this chapter's own
# audit-trail section argues against.

# Engineering upgrade: this container ran as root by default before this,
# despite service.py never actually needing root privileges for anything
# it does (bind to a non-privileged port, read/write one output
# directory). Standard container-hardening practice, and a real finding
# from reviewing this Dockerfile with a security-engineering eye rather
# than only a "does it work" one.
#
# -r for a system account (no login shell needed, no interactive-user
# baggage), fixed UID/GID (1000) rather than letting useradd pick one, so
# this is reproducible across rebuilds and matches a predictable UID a
# host-side bind mount or CI scanner might expect. /app/output is created
# and chowned here, before the USER switch below, since the redact-output
# named volume docker-compose.yml mounts there needs to inherit this
# ownership on first creation for the non-root process to be able to
# write token_store.json into it -- Docker copies a fresh named volume's
# initial content (including ownership) from whatever already exists at
# that path in the image at container start. An EXISTING volume created
# by an earlier, root-owned version of this image will NOT retroactively
# get these permissions -- `docker compose down -v` (a fresh volume) or a
# manual `chown -R 1000:1000` on the existing volume's data is needed
# when upgrading a running deployment across this change, not implied
# automatically by rebuilding the image alone.
RUN groupadd -r -g 1000 redact \
    && useradd -r -u 1000 -g redact -d /app -s /usr/sbin/nologin redact \
    && mkdir -p /app/output \
    && chown -R redact:redact /app

USER redact

EXPOSE 8080

# Production WSGI server, not the Flask dev server. src/service.py's own
# `if __name__ == "__main__": app.run(..., threaded=True)` path is documented
# in-code as a stopgap (see BUGS_AND_FIXES.md Bug 3): threaded=True only
# buys overlapping I/O, since the NER call inside detect.detect_all() is
# CPU-bound and still serializes on the GIL within a single process. This
# is the multi-process fix that comment always pointed at.
#
# --chdir src, then `service:app`: src/ has no __init__.py (not a package),
# so gunicorn needs to import service.py as a top-level module from inside
# src/, not as src.service from /app. service.py's own
# `sys.path.insert(0, os.path.dirname(__file__))` line still runs at import
# time regardless of gunicorn's cwd, so detect.py/anonymize.py/audit.py's
# bare `import detect` etc. keep working unchanged.
#
# --workers ${GUNICORN_WORKERS:-$(nproc)}: originally a bare `$(nproc)`,
# matched to the container's available CPU cores per the standard gunicorn
# sync-worker sizing guidance for CPU-bound work (the NER call is CPU-bound
# and GIL-serialized per process, so more workers than cores buys nothing
# but memory pressure). Made configurable, 2026-08-10 (ROADMAP item 12
# follow-up), after a live `docker compose up --scale redact-service=3`
# run OOM-killed OpenSearch (exit 137) -- root-caused to `nproc` workers
# PER REPLICA, each warming its own full spaCy/Presidio model copy (see
# the model-memory comment below), so replica_count x nproc x model-memory
# is what actually has to fit in the host's Docker memory budget, not just
# nproc x model-memory as the un-scaled, single-replica case always was.
# `docker compose --scale` has no way to divide worker count by replica
# count on its own -- this env var is what lets docker-compose.yml (or a
# .env override) size workers-per-replica DOWN as replica count goes UP,
# instead of every additional replica silently multiplying total memory
# demand by a full `nproc`. See docker-compose.yml's redact-service
# environment block for the default this project now ships with (2), and
# BUGS_AND_FIXES.md / ROADMAP.md item 12 for the OOM finding this fixes.
# Still shell form (not JSON-array exec form), same reason as before: both
# `$(nproc)` and `${GUNICORN_WORKERS:-...}` need real shell expansion, and
# sh -c is the standard way to get that inside an exec-form-only CMD.
#
# --timeout 60: gunicorn's default worker timeout is 30s, sized for typical
# web request latency, not headroom for anything unusual under load. This
# service's own measured per-request cost is milliseconds (NER-bound
# throughput was ~128-135 events/sec against the full 10,000-entry corpus,
# see README.md), so 60s is deliberate slack, not a number this project has
# needed to hit in testing -- lower it if you've verified your own
# deployment's actual p99 request latency and want tighter failure
# detection instead.
#
# Each gunicorn worker imports this module independently after forking
# (gunicorn's default, non---preload behavior) and therefore warms its own
# copy of the spaCy/Presidio model at startup (see the module-level
# detect._get_analyzer() call in service.py and its docstring for why
# --preload was deliberately not used here). This means worker_count x
# model-memory at steady state -- size the container's memory limit
# accordingly, not just for one copy of the model.
# --access-logfile - added 2026-08-10 (ROADMAP item 12): with
# container_name no longer fixed (see docker-compose.yml's redact-service
# comment -- required to allow --scale), Compose auto-names each replica
# distinctly (e.g. redact-..._redact-service-1, -2, -3), and each
# replica's own `docker compose logs` output now shows a gunicorn access
# log line per request it actually handled. That's the direct,
# verifiable evidence for "did redact-lb's nginx proxy actually
# distribute requests across all replicas, or did they all land on one"
# -- without this flag there was no way to answer that question short of
# adding new instrumentation.
CMD ["sh", "-c", "gunicorn --chdir src --workers ${GUNICORN_WORKERS:-$(nproc)} --bind 0.0.0.0:8080 --timeout 60 --access-logfile - service:app"]
