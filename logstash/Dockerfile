# Standard Logstash image doesn't ship the OpenSearch output plugin by
# default (Elastic's own output plugins target Elasticsearch); this adds it
# per OpenSearch's own documented approach (docs.opensearch.org/latest/tools/logstash),
# rather than reaching for the older opensearchproject/logstash-oss-with-opensearch-output-plugin
# image, which as of this writing is pinned to Logstash 7.16.2 and doesn't
# track current Logstash releases.
#
# CAVEAT, same as logstash/redact-pipeline.conf: this Dockerfile has not
# been built or run -- no Docker runtime was available in the environment
# this chapter's code was developed in. It reflects OpenSearch's documented
# plugin-installation approach, not a verified build.

FROM logstash:8.15.0

RUN bin/logstash-plugin install logstash-output-opensearch

COPY redact-pipeline.conf /usr/share/logstash/pipeline/redact-pipeline.conf
