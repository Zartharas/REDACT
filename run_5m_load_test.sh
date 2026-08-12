#!/usr/bin/env bash
# ROADMAP item 9 stretch goal (Task #47): push the local-scale load test
# beyond the 1,000,000-line high-water mark, to narrow the gap toward real
# production volume incrementally. Terabytes/day stays honestly out of
# reach without real cloud spend/hardware (see ROADMAP.md's item 12
# closing paragraph) -- this does NOT claim to close that gap, only to
# find out where this project's single-machine ceiling actually sits
# between 1,000,000 lines and that real target.
#
# Same harness as run_1m_load_test.sh (validation/load_test/run_load_test.sh,
# unmodified logic) at 5x the previous scale, not a rewrite -- no reason to
# duplicate something that already works and has already found three real
# bugs (Bug 12, 13, 15, 16 in BUGS_AND_FIXES.md) along the way.
#
# WHAT CHANGED SPECIFICALLY TO MAKE THIS SAFE TO RUN, not just "the same
# script with a bigger number": run_load_test.sh's poll-loop deadline
# (REDACT_LOAD_TEST_MAX_WAIT_SECONDS) used to default to a fixed 14400s
# (4 hours), sized for the 1,000,000-line runs this project has actually
# completed. At 5,000,000 lines, even the FASTER of the two measured
# 1,000,000-line rates (~439 lines/sec, 2026-08-10) implies ~3.2 hours,
# and the SLOWER one (~224 lines/sec, 2026-08-08) implies ~6.2 hours --
# past the old fixed deadline, which would have falsely reported a
# deadline-exceeded failure on a pipeline that was still healthy and
# converging (exactly Bug 15's iteration-count version of this same
# problem, for a different mechanism). Fixed at the source in
# run_load_test.sh itself (now scales its default deadline with N), not
# worked around here by just passing a bigger env var -- so this same
# fix protects any future run at any scale, not just this one.
#
# DISK SPACE, estimated (not directly measured in this sandbox -- no
# Docker daemon here to run this against): the canonical 10,000-line
# corpus (data/synthetic_logs.jsonl) is 1.9MB, ~190 bytes/line. At
# 5,000,000 lines that's roughly:
#   - ~950MB for the generated corpus (data/load_test_corpus_5000000.jsonl)
#   - a comparable amount again for the exported raw per-source log files
#     (data/raw/) Logstash actually reads
#   - the persisted token store growing to roughly 465,000 entries,
#     extrapolating the 1,000,000-line run's measured 93,279 (9.3% of
#     lines producing a new reversible token) -- a few tens of MB, not a
#     major factor on its own
#   - OpenSearch holding roughly 5,000,000 anonymized/quarantine documents
#     plus ~4,465,000 audit-trail documents (extrapolating the 1,000,000-
#     line run's measured 89.3% audit fan-out), each a JSON document with
#     the original field structure plus redaction metadata -- the actual
#     on-disk index size depends on OpenSearch's own storage overhead
#     (inverted indices, replicas) that this project has not measured
#     directly at any scale, so no total GB figure is given here as
#     anything more than "plan for several GB of headroom beyond the
#     ~2GB of flat files above, and check `docker system df` /
#     `df -h` partway through a first attempt rather than assuming."
# Recommend at least 15-20GB of free disk before starting, as a
# conservative margin given that uncertainty -- not a number this project
# has confirmed is sufficient, since it has never actually run at this
# scale.
#
# Run from the repo root:
#   chmod +x run_5m_load_test.sh
#   ./run_5m_load_test.sh
#
# Needs Docker Desktop running, and takes a while -- plan for several
# hours based on the 1,000,000-line runs' own measured rates (see the
# deadline math above). Not yet run anywhere; syntax-checked only
# (bash -n) in this sandbox, same disclosed limitation as
# run_floci_kafka_test.sh and run_floci_elbv2_test.sh.
set -euo pipefail

git push origin main   # publish everything from this session first

# Same five required .env keys run_1m_load_test.sh already checks for --
# duplicated here rather than factored into a shared helper, matching
# that script's own standalone structure so either can be run
# independently without the other.
touch .env
for key in REDACT_PSEUDO_KEY REDACT_AUDIT_KEY REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done

echo "--- .env now has these keys set (values redacted) ---"
sed 's/=.*/=<redacted>/' .env

echo
echo "--- Pre-flight: validating Logstash pipeline config syntax ---"
docker compose build logstash

# Real bug, found 2026-08-11 from a live run: `docker compose run --rm
# logstash ...` only starts the DIRECT dependencies logstash itself
# declares in docker-compose.yml (opensearch-node1, redact-service,
# redact-lb) -- it does NOT start opensearch-node2/opensearch-node3, since
# nothing in that dependency graph names them. That was fine back when
# OpenSearch was a single-node service, but since ROADMAP item 12 made it
# a real 3-node cluster, opensearch-node1's own healthcheck specifically
# requires all 3 nodes to have joined ("number_of_nodes":3) -- so node1
# can never pass health in this isolated context, and `docker compose run`
# fails with "dependency failed to start: container ... is unhealthy"
# before Logstash's config is ever actually tested. This exact OpenSearch
# config already proved it forms a healthy cluster fine (confirmed live,
# same day) under run_floci_kafka_test.sh's explicitly-scoped
# `docker compose up -d --build redact-service redact-lb opensearch-node1
# opensearch-node2 opensearch-node3` -- the difference is that invocation
# names all 3 nodes explicitly and this pre-flight step didn't. Fixed by
# bringing up the full dependency set (matching that proven invocation)
# BEFORE the `docker compose run` call, so by the time it runs, node1 can
# actually become healthy.
echo "--- Bringing up Logstash's full dependency set first (all 3 OpenSearch"
echo "    nodes, not just node1 -- see comment above for why) ---"
docker compose up -d --build redact-service redact-lb \
  opensearch-node1 opensearch-node2 opensearch-node3

docker compose run --rm logstash \
  bin/logstash --config.test_and_exit -f /usr/share/logstash/pipeline/redact-pipeline.conf
echo "Logstash config syntax OK."

echo
echo "--- Checking free disk space (informational only -- see the header"
echo "    comment above for why no hard minimum is enforced here) ---"
df -h . 2>/dev/null || true

echo
echo "--- Running the 5,000,000-line load test ---"
echo "--- (run_load_test.sh's own deadline now auto-scales with N -- see"
echo "--- that script's REDACT_LOAD_TEST_MAX_WAIT_SECONDS comment) ---"
./validation/load_test/run_load_test.sh 5000000
