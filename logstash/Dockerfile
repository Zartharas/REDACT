# Standard Logstash image doesn't ship the OpenSearch output plugin by
# default (Elastic's own output plugins target Elasticsearch); this adds it
# per OpenSearch's own documented approach (docs.opensearch.org/latest/tools/logstash),
# rather than reaching for the older opensearchproject/logstash-oss-with-opensearch-output-plugin
# image, which as of this writing is pinned to Logstash 7.16.2 and doesn't
# track current Logstash releases.
#
# CAVEAT, same as logstash/redact-pipeline.conf: this Dockerfile was updated
# based on live testing against a real Docker/OpenSearch stack (see project
# history) after the plugin-installation step below was verified working.
# The RUN rm step here was added in response to a real bug found during that
# testing: the base logstash:8.15.0 image ships its own default
# /usr/share/logstash/pipeline/logstash.conf (a beats-input,
# stdout-rubydebug sample pipeline). Docker's official Logstash image loads
# every *.conf file in that directory as ONE combined pipeline unless told
# otherwise, so without this removal, our redact-pipeline.conf ran merged
# with that default -- opening an unused beats listener on 5044, and, more
# seriously, printing every event's pre-anonymization raw content
# (including PII, via [event][original]) to the container's own logs via
# the default file's unconditional stdout output. Removing it is required
# for correctness of the "no PII leaves the pipeline unredacted" claim this
# chapter makes, not just cleanup.

FROM logstash:8.15.0

RUN bin/logstash-plugin install logstash-output-opensearch

# Added 2026-08-10 (ROADMAP item 12, the queue-decoupled ingestion path):
# logstash-output-redis, used by logstash-pipeline-queued.conf's `redis`
# output block. Confirmed via elastic.co's own plugin documentation and
# the logstash-plugins GitHub org (the official maintainer, same as
# logstash-output-opensearch above) that this is NOT bundled in the base
# logstash:8.15.0 image by default and needs this same explicit install
# step. Only used by the "queued" Compose profile's logstash-queued
# service -- the default logstash service (redact-pipeline.conf) never
# calls the redis output block, so installing this plugin doesn't change
# that service's behavior, just adds it to the same built image both
# services share.
RUN bin/logstash-plugin install logstash-output-redis

# Added 2026-08-11 (ROADMAP item 13, the Kafka-shaped alternative queue
# path): logstash-output-kafka, used by redact-pipeline-kafka.conf's
# `kafka` output block. UNLIKE logstash-output-redis and
# logstash-output-opensearch above, logstash-integration-kafka (which
# bundles both the Kafka input and output plugins) is widely documented
# as one of Logstash's DEFAULT bundled plugins in the standard (non-OSS)
# image, not a third-party add-on -- but that has NOT been independently
# confirmed against a live logstash:8.15.0 container in this sandbox (no
# Docker daemon here), unlike the redis/opensearch plugin gaps above,
# which WERE confirmed via each plugin's own official documentation.
# Running the install command explicitly here is deliberately safe either
# way: if the plugin is already bundled, `logstash-plugin install`
# against an already-present plugin is idempotent (updates/no-ops rather
# than erroring), so this line costs nothing if the "bundled by default"
# understanding turns out to be right, and closes the gap outright if it
# turns out to be wrong. Only used by the "kafka-queued" Compose
# profile's logstash-kafka service -- same shared-image reasoning as the
# redis plugin above.
RUN bin/logstash-plugin install logstash-integration-kafka

RUN rm -f /usr/share/logstash/pipeline/logstash.conf

COPY redact-pipeline.conf /usr/share/logstash/pipeline/redact-pipeline.conf
