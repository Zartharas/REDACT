# Drift alert source-attribution: design doc (Task #50)

Per this task's own description: a design/dependency doc, written
before committing to a specific integration, not an implementation.
No code changes were made for this task — `monitoring/alert_rules.yml`
and `monitoring/alertmanager.yml` (Engineering upgrade 2,
`BUGS_AND_FIXES.md`) are unchanged.

## The actual problem, stated precisely

`RedactFieldDriftDetected` (`monitoring/alert_rules.yml`) fires with
`log_type`/`field` labels (e.g. `log_type="cloudtrail", field="notes"`)
but `monitoring/alertmanager.yml` is a deliberate stub — one receiver,
no routing tree (see that file's own comment). Every drift alert, for
every log type, goes to the same undifferentiated place. The goal: route
a drift alert for `cloudtrail`-sourced PII to whichever team actually
owns the CloudTrail ingestion pipeline, not to a single catch-all.

## Real gap found while scoping this, worth stating before anything else

**REDACT's pipeline does not currently carry any source metadata finer
than `log_type` — and `log_type` today is only three fixed values**
(`windows_event`, `syslog`, `cloudtrail`), assigned in
`logstash/redact-pipeline.conf` purely by which file path a raw log
line was read from (see that file's `if [path] =~ ...` block). There is
no hostname, service name, team tag, or any other identifying field
captured or forwarded anywhere in this pipeline today. "Map log source
metadata back to an owning team," as this task's own description
phrases it, implicitly assumes source metadata finer-grained than
`log_type` already exists to map FROM — it doesn't, in REDACT's current
scope. This is a precondition gap, not just an integration detail: any
real deployment of this project across an actual multi-team
organization would need its log sources tagged with something like a
service/application identifier (however that org's real log shipping
already does it — Fluent Bit/Vector both support this natively via
static tags or metadata enrichment) BEFORE source-attribution has
anything more specific than "the whole cloudtrail log type" to route
on. Scoped honestly around what exists today (`log_type`-level
attribution only), with this gap flagged as a real precondition for
anything finer.

## Why this needs an external dependency, not something built in REDACT

Task #50's own description already states this correctly: REDACT has no
concept of "teams," "ownership," or an organizational directory
anywhere in its scope, and building one from scratch would be
duplicating infrastructure that, in any real organization deploying
this, already exists somewhere else (an HR system, an IAM/SSO group
directory, or a service catalog). The right design is to treat
ownership data as **external, and REDACT as a consumer of it**, not
REDACT growing its own ownership database that would need independent
maintenance and would inevitably drift out of sync with whatever the
organization's real source of truth is — the exact "two things that
should be one but aren't" failure shape this project's own drift
detector exists to catch elsewhere (`src/service.py`'s module
docstring), worth not reproducing here.

## Two real options, with a recommended default

### Option A: static YAML mapping (recommended as the near-term default)

A single file, e.g. `monitoring/source_ownership.yml`:

```yaml
# Illustrative example only -- not wired into anything yet. Real values
# (team names, receiver names) would need to match whatever this
# project's actual Alertmanager receivers/PagerDuty services/Slack
# channels are configured with in a real deployment; there are none
# configured today (alertmanager.yml is a deliberate stub).
log_type_owners:
  cloudtrail: team-cloud-platform
  windows_event: team-endpoint-security
  syslog: team-infrastructure
```

Consumed by Alertmanager's own native routing tree (a `route.routes`
list matching on the `log_type` label, each entry pointing at a
different `receiver`) — **no REDACT code changes needed at all** for
this option; it's purely an `alertmanager.yml` configuration change,
since `log_type` is already an alert label today. This is the cheapest,
lowest-risk path, and the right default given `log_type` only has three
possible values right now — a 3-line routing tree, not a system.

**Real limitation, disclosed:** this file has no source of truth beyond
itself. If team ownership changes (a re-org, a service migrating
teams), someone has to remember to update this file — the same
staleness risk any hand-maintained mapping has, and exactly the
"invisible drift" pattern this project's whole `drift.py` mechanism
exists to catch for PII fields. Acceptable at the current 3-log-type
scale (low enough surface area that staleness would likely be noticed
quickly), genuinely risky if this ever grows to dozens or hundreds of
real source identifiers.

### Option B: a real service catalog (Backstage-style), consumed via API

A real internal developer/service catalog (Backstage is the most common
open-source example — an entity model where each service/component
declares its owning team in a `catalog-info.yaml`, queryable via a live
API) is the organizationally-correct long-term answer: ownership data
lives in ONE real place or already-adopted org, and REDACT's Airflow
DAG queries it at drift-check time instead of maintaining its own copy.

**Real, disclosed cost this project cannot resolve on its own:**
REDACT has no existing dependency on any service catalog today, and
introducing one is an organizational decision (which catalog, if any,
the deploying organization already uses or is willing to stand up) that
this project cannot make on its behalf — exactly why Task #50's own
description calls this "a dependency that doesn't exist in REDACT's
scope today." Building Option B speculatively, against a catalog this
project doesn't actually have access to or a confirmed target for,
would mean writing an integration this sandbox cannot test against
anything real and that may not even match whatever catalog a real
deploying organization actually runs (Backstage isn't the only one).

**Recommendation: ship Option A now** (it requires zero new
dependencies and works today, however thin), **and treat Option B as an
explicit "if/when this project has a real target service catalog to
integrate against" follow-on**, not a default to build toward
speculatively. If Option B is ever pursued, the integration point would
be a new, optional lookup step inside
`src/airflow_tasks.py`'s existing `push_drift_metrics_to_prometheus()`
(Engineering upgrade 2) — attach a `team` label (resolved via the
catalog's API, called once per DAG run and cached, not per-metric) to
the pushed Prometheus metrics, so Alertmanager can route on `team`
directly instead of needing its own copy of the `log_type` -> team
mapping. This keeps the catalog dependency isolated to one function,
optional (falls back to no `team` label / Option A's static routing if
unconfigured), and consistent with this project's existing pattern of
optional, env-var-gated integrations (Vault, Redis Sentinel, Kafka all
follow this same "off by default, real when configured" shape).

## What this design doc deliberately does not do

No `alertmanager.yml` routing tree was written, and no
`source_ownership.yml` file was created as a real, consumed artifact —
both are illustrative here only. Per this task's own instruction ("write
the design/dependency doc first before committing to a specific
integration"), committing to Option A's exact receiver names/routing
tree needs a real answer to "what are this deployment's actual
receivers" (Slack webhook, PagerDuty service, email) that
`alertmanager.yml`'s own stub status shows this project doesn't have
yet — building the routing tree before that exists would mean
inventing receiver names with nothing real behind them.
