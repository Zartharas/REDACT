# Builds the redact-service container (src/service.py) -- the tested
# detection/anonymization/audit logic behind a Flask API. This is a
# standard Python/Flask container; nothing here is version-sensitive the
# way the OpenSearch or Logstash images are, so it's the one piece of this
# Docker setup closest to "should just work" without further verification.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_lg

COPY src/ ./src/

ENV REDACT_TOKEN_STORE_PATH=/app/output/token_store.json
# REDACT_PSEUDO_KEY and REDACT_AUDIT_KEY are intentionally NOT baked in here.
# Set them via docker-compose environment or a secrets mechanism -- shipping
# a default key in an image is exactly the kind of thing this chapter's own
# audit-trail section argues against.

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
# --workers $(nproc): matched to the container's available CPU cores, per
# the standard gunicorn sync-worker sizing guidance for CPU-bound work (the
# NER call is CPU-bound and GIL-serialized per process, so more workers
# than cores buys nothing but memory pressure). Requires shell form (not
# JSON-array exec form) so $(nproc) actually expands; sh -c is the standard
# way to get that inside an exec-form-only CMD.
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
CMD ["sh", "-c", "gunicorn --chdir src --workers $(nproc) --bind 0.0.0.0:8080 --timeout 60 service:app"]
