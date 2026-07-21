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

CMD ["python", "src/service.py"]
