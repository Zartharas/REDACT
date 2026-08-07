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

RUN rm -f /usr/share/logstash/pipeline/logstash.conf

COPY redact-pipeline.conf /usr/share/logstash/pipeline/redact-pipeline.conf
