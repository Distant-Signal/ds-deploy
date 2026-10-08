# Alert runbook

One section per `DistantSignal*` alert in the chart's PrometheusRules
(`charts/distant-signal/templates/prometheusrule.yaml` and
`pgbackrest-prometheusrule.yaml`). Each alert's `runbook_url` links to its
section here. The chart README's "Alerts" table lists what each one fires on,
with the default thresholds.

## Why the alerts themselves are terse

On 2026-10-01 ntfy refused notifications for a 3-alert
`DistantSignalPollerFailing` group: Alertmanager's webhook was 5.8 KB of JSON,
and ntfy rejects anything over 4,096 bytes. Most of each alert's ~2 KB was its
`generatorURL` (the URL-encoded PromQL, 953 characters for that rule) and a
multi-sentence description. So every alert now has:

- a one-line `summary` (at most 80 characters) and a `description` (at most
  150), with the explanation here instead;
- an `expr` of at most about 300 characters. A longer expression is a
  recording rule named `distant_signal:<what>:<how>`, placed before the alert
  in the same rule group. Prometheus evaluates a group's rules in order, so
  the alert reads the value recorded in the same evaluation and fires exactly
  as the inline expression did.

`scripts/check-alert-payloads.py` (CI's scripts-lint job) renders the chart
with every alert on and fails when an alert breaks those limits, or when a
3-alert group's estimated webhook JSON is over 4,096 bytes. With
`--download-promtool DIR` it also runs `promtool check rules` and the unit
tests in `scripts/alert-rules-tests/`.

Metric names below omit the `distant_signal_` prefix.

## health

### DistantSignalPostgresDown

The Distant Signal database is down: postgres_exporter's `pg_up` is 0, or the
bundled Postgres StatefulSet (`<release>-postgres`) has no ready replica. On
2026-10-01 it was down for six hours (10:16-16:10 UTC) with only a warning
about the exporter. Every writer stops: api answers 5xx on its `/private`
ingest routes, the consumers keep their batches pending, the aggregator and
notifier cycles fail, and movement-events lag grows.

1. `kubectl -n distant-signal get pod distant-signal-postgres-0` and
   `kubectl logs distant-signal-postgres-0 -c postgres --previous`: crash
   loop, OOM, `could not write` / `No space left on device`, or a failed
   recovery.
2. Disk: `kubectl -n distant-signal exec distant-signal-postgres-0 -c postgres -- df -h /var/lib/postgresql/data`.
   A full volume needs resizing (or WAL that failed to archive cleared, see
   [Postgres PITR](postgres-pitr.md#archiving-is-failing)).
3. Connections: `max_connections` reached shows as `too many clients` in
   api's logs while `pg_up` stays 1 (that is
   [DistantSignalApiDatabaseDown](#distantsignalapidatabasedown) instead).
4. Once it is back, watch movement-events lag drain and the consumers'
   error counters stop rising. Kafka and the Redis stream hold the TRUST
   backlog only up to their retention and MAXLEN.

### DistantSignalStatefulImagePullFailing

A container or init container of the bundled Postgres (`<release>-postgres-0`)
or Redis (`<release>-redis-<hash>-<suffix>`) pod has been waiting in
`ImagePullBackOff`, `ErrImagePull` or `InvalidImageName` for 5m
(kube-state-metrics' `kube_pod_[init_]container_status_waiting_reason`). Both
are single pods replaced without overlap, so nothing is serving: for Postgres
DS is down (see [DistantSignalPostgresDown](#distantsignalpostgresdown)), for
Redis the movement-events stream and live TRUST movements stop. On 2026-10-01
`postgres-0` was recreated on an image from a new private registry and sat in
`ImagePullBackOff` for about six hours. A pull never succeeds on its own.

1. `kubectl -n distant-signal describe pod <pod>`: the Events name the image
   and the registry's answer (`unauthorized`, `not found`, `manifest unknown`,
   a DNS or TLS error).
2. Pull secrets: the pod's `imagePullSecrets` (chart value `imagePullSecrets`)
   must name a Secret that exists in the namespace and holds credentials for
   that registry (`kubectl -n distant-signal get secret <name>`; check which
   registry its `.dockerconfigjson` covers without printing it). Check the
   image reference itself (`postgresql.image`, `postgresql.pgbackrest.image`,
   `redis.image`): repository, tag and digest.
3. Pre-pull to confirm the fix before the pod retries: on the node,
   `crictl pull <image>` (with the same credentials), or run a throwaway pod
   with the same image and pull secret. Once the image is on the node, delete
   the stuck pod (or wait for the back-off) and it starts.
4. Roll back if it cannot be fixed quickly: `helm rollback` (or revert the
   image change in the Flux/Git values) to the last image that ran. Kafka and
   the Redis stream hold the TRUST backlog only up to their retention and
   MAXLEN, so restore service first and debug the new image after.

### DistantSignalApiDatabaseDown

api's own probe has failed for `apiDatabaseDown.for`:
`distant_signal_api_db_up` is 0 on some replica. Every 15s api runs
`SELECT 1` through its request pool, with a 10s limit; each failure also
counts in `distant_signal_api_db_probe_failures_total`, and api logs the
first failure ("api cannot query its database") and the recovery.

While it fires, api answers 5xx on most routes, including the `/private`
ingest routes the consumers and pollers POST to. Those keep their batches
pending and retry, so watch movement-events lag.

1. If [DistantSignalPostgresDown](#distantsignalpostgresdown) fires too, the
   database itself is down: start there.
2. Otherwise read api's log for the error: `password authentication failed`
   (the database Secret changed without an api restart), `too many clients`
   (`max_connections`; see the chart's Postgres connection budget),
   `pool timed out` (every pooled connection busy: a slow query or a lock),
   or a connection refused/timed out (NetworkPolicy, DNS, an external
   database's firewall).
3. `SELECT state, count(*) FROM pg_stat_activity GROUP BY 1;` shows whether
   connections are piling up.

Severity is critical because api is the only writer for every ingest path:
while it cannot query, nothing lands, and the queues in front of it (Kafka
retention, the Redis stream's MAXLEN) are finite.

### DistantSignalSchemaGateWaiting

A service has been waiting at its schema gate for `schemaGateWaiting.for`:
`distant_signal_db_schema_ready` is 0 on some pod of the component named in
the alert. At startup the aggregator, enricher, notifier and ingest-writer
(and the api, when `api.migrateOnStartup` is false) wait, before their
loops, readiness and listener, until the database has the newest migration
built into their image and their own role holds every grant
`charts/distant-signal/files/db-grants.yaml` gives it (spec §12.2). They
poll every 5s and exit after 15 minutes, so the pod restarts into the gate
again. Liveness stays up while they wait; readiness does not, so a rollout
stalls on the new pods and the old ones keep serving.

The service logs each poll as "schema gate: waiting for the schema" with
`applied_migration`, `required_migration` and `missing` (the grants it
lacks, as `PRIVILEGE on table[.column]`).

1. `applied_migration` below `required_migration`: the migrations have not
   run. With `migrate.job.enabled`, check the migrate hook Job
   (`kubectl -n distant-signal get jobs` and its pod's log); a Helm upgrade that timed out before
   the Job finished needs the HelmRelease `timeout` raised (about 20
   minutes). Without the Job, the api migrates at startup: check the api's
   log.
2. `missing` is not empty: the role lacks grants. The chart's
   postgres-setup Job applies `files/postgres-grants.sql`; check it ran
   for this release, and that `db-grants.yaml` lists the table for this
   service (a new table needs a grant there, `scripts/gen-db-grants.py`).
3. `ds-migrate wait --role <role>` (in the api image), run with that
   service's `DATABASE_URL`, makes the same check from outside the service
   and exits once it passes.

### DistantSignalConsumerApiCallsFailing

A TRUST consumer keeps failing its calls to api: at least
`consumerApiErrors.minErrors` failures within `window`, continuously for
`for` (`distant_signal:consumer_api_errors:increase`, one series per
consumer). The consumers' `*_ready` gauges only follow their Redis
connection, so on 2026-10-01 trust-consumer failed
`GET /private/tracked-trains` about 23,600 times, and trust-backlog-consumer
about 2,200 POSTs, without an alert.

The operations counted (`distant_signal_<consumer>_errors_total{operation}`):

| Consumer | Operations |
| --- | --- |
| trust-consumer | `reload_tracked_trains`, `post_train_events`, `reload_stanox_crs`, `startup_reference_load`; `db_write` with `trustConsumer.ingest.sink: db` |
| trust-backlog-consumer | `post_batch`, `post_train_reasons`, `reload_stanox_crs`; `db_write`, `db_write_reasons` with `trustBacklogConsumer.ingest.sink: db` |
| full-coverage-consumer | `reload_line_population_fetch`, `post_line_stats`, `post_station_samples`, `reload_stanox_crs` |

Data rejections (`post_rejected`) are not counted: those are poison entries,
isolated by the dead-letter path. full-coverage-consumer's
`post_window_stats` has its own alert,
[DistantSignalFullCoverageWindowPostErrors](#distantsignalfullcoveragewindowposterrors).

1. Check [DistantSignalApiDatabaseDown](#distantsignalapidatabasedown) and
   [DistantSignalPostgresDown](#distantsignalpostgresdown): a database
   outage is the usual cause.
2. Otherwise `sum by (operation) (increase(distant_signal_<consumer>_errors_total[10m]))`
   shows which call fails, and the consumer's error log has api's status and
   body. A 401/403 points at the internal OAuth client (Authentik); a 5xx at
   api's own log. `db_write*` (ingest plan 3b, `ingest.sink: db`) are the
   consumer's own Postgres writes: its log has the SQLSTATE; a
   `permission denied` means its role lacks a grant (the schema gate
   should have caught it; check the roles setup Job ran).
3. Failed POSTs leave their batches pending in movement-events and are
   retried, so nothing is lost until the stream's MAXLEN trims them: watch
   [DistantSignalMovementLagHigh](#distantsignalmovementlaghigh).

### DistantSignalApiPublic5xx

More than `api5xx.public.ratio` (5%) of api's public requests answered 5xx
over `api5xx.window` (5m), with at least `minErrors` (3) of them, for `for`
(10m). Public means every route except `/private/*`, `/public/health`, `/public/ready` and
unmatched paths (`/{unmatched}`, scanner noise): what the frontend, the MCP
and anyone else calling the API sees. The recording rules
`distant_signal:api_requests:rate`, `distant_signal:api_5xx:rate` and
`distant_signal:api_5xx:increase` carry a `scope` label (`public`,
`ingest`).

The cluster's own `DistantSignalApi5xx` (Ranma-Config) is one ratio over
every route, so on 2026-10-01 the ingest retry storm (about 27 5xx a second
on `/private/*`) hid what the public saw: about 20% of their few requests a
minute failing for six hours.

A 503 with `{"error":"service_unavailable"}` means a route could not reach
the database (or Redis, or the IdP), see the 2026-10-06 entry in
[the API changelog](api-changelog.md); a 500 is anything else.

1. Check [DistantSignalApiDatabaseDown](#distantsignalapidatabasedown) and
   [DistantSignalPostgresDown](#distantsignalpostgresdown) first.
2. Which routes and codes:
   `sum by (exported_endpoint, status) (increase(distant_signal_http_requests_total{status=~"5.."}[10m]))`.
   Mostly 503: a dependency (api's log says "a dependency is unavailable;
   answering 503"). Mostly 500: a bug or a failing query; api's error log
   names the route's operation.
3. A single route's 503s with "too many trip plans are being computed" in
   the body is `/Trips/plan` shedding load, not an outage.

### DistantSignalApiIngest5xx

The same for the `/private/*` ingest routes, with at least `minErrors` (30)
5xx in the window. The pollers and consumers retry with backoff (honouring
`Retry-After` on a 503), so their data is late, not lost, until the
movement-events stream's MAXLEN or a poller's retry budget runs out. See
[DistantSignalConsumerApiCallsFailing](#distantsignalconsumerapicallsfailing)
for the consumers' view and the same first steps as above.

### Querying api request metrics

- The route is the `exported_endpoint` label, not `endpoint`:
  axum-prometheus names it `endpoint`, and the PodMonitor scrape sets its
  own `endpoint` (`metrics`), so Prometheus renames api's to
  `exported_endpoint`. The value is the route template
  (`/Train/by-uid/{train_uid}/{date}`), never a concrete path.
- api registers some series at 0 when it starts (`api::route_metrics`):
  `http_requests_total` for the key public routes (`/Trips/plan`,
  `/Train/by-uid/...`, line status, stations, trains search and resolve,
  freshness, incidents, session) and every `/private` route, each with
  status 200, 500 and 503; `http_requests_duration_seconds` for the key
  public routes' 200s; and `api_trip_plan_graph_cache_total` for `hit` and
  `miss`. Without that, a series appeared at 1 on its first request and
  `increase()`/`rate()` never counted that request, so a rare route like
  `/Trips/plan` read as zero across api's frequent restarts.
- For any other route or status code the first request after a restart is
  still invisible to `increase()`. For a rare route, sum over a window
  longer than the restarts and read the raw counters
  (`max_over_time(distant_signal_http_requests_total{exported_endpoint="/Trips/plan"}[1d])`
  per pod) rather than trusting one `increase()`.

### DistantSignalAggregatorCycleFailing

No aggregation cycle has succeeded for `cycleStalled.maxAgeSeconds`:
`time() - distant_signal_aggregator_last_success_timestamp_seconds{cycle="aggregate"}`.
The gauge holds the last successful cycle's time, or the process start
until one succeeds, so an aggregator that restarts into a failing database
still fires. `distant_signal_aggregator_cycles_total{result}` counts both
outcomes. On 2026-10-01 every cycle failed for six hours with no alert:
`aggregator_cycle_duration_seconds_count` rises whether a cycle succeeds or
not.

While it fires, line statuses, line stats and the full-coverage window
verdicts stop updating (the site shows stale data). Retention runs on its own
task and is not covered.

1. The aggregator's log: "aggregation cycle failed" with the error.
2. Usually the database:
   [DistantSignalPostgresDown](#distantsignalpostgresdown). Otherwise a
   statement timeout on a slow query, or a migration the aggregator's build
   expects but api has not run yet.

### DistantSignalNotifierCycleFailing

One of the notifier's loops (`cycle` label: `line_status`, `forward_queue`
or `skip_check`) has not succeeded for `cycleStalled.maxAgeSeconds`
(`distant_signal_notifier_last_success_timestamp_seconds{cycle}`, the process
start until the first success; `distant_signal_notifier_cycles_total` counts
both outcomes). The hourly `template_sweep` loop is left out.

While it fires, line-status, train and skipped-stop push notifications for
that loop are not decided. Delivery is at-most-once within a grace window, so
some users miss them for good. Read the notifier's log ("notifier ... cycle
failed") and check the database.

## movement-events

### DistantSignalMovementLagHigh

A consumer group's unread entries (`movement_relay_stream_lag`) plus its
delivered-but-unACKed ones (`movement_relay_stream_pending`) are above
`movementLag.warningRatio` of the stream's MAXLEN cap
(`movement_relay_stream_maxlen`, or `movementRelay.streamMaxLen` before a
relay exports it). Recorded as `distant_signal:movement_events_behind:ratio`.

A consumer whose downstream (api) is failing keeps reading and leaves each
failed batch pending, so its lag stays near 0 while pending grows; MAXLEN trims
pending entries all the same. Once lag plus pending reaches the cap, entries
are trimmed before the group reads them (a stream gap).

1. Read the consumer's logs: is it failing to POST to api, or is it slow?
2. Check api's health.

Design: [movement-relay design](superpowers/specs/2026-09-04-movement-relay-design.md).

### DistantSignalMovementLagCritical

As [DistantSignalMovementLagHigh](#distantsignalmovementlaghigh), above
`movementLag.criticalRatio`. A stream gap (data loss) is close. Fix the
consumer, or raise `movementRelay.streamMaxLen` together with Redis
`maxmemory` to buy time.

### DistantSignalMovementLagGrowing

A group's lag has a positive `deriv` over `movementLagGrowing.window` and
grew by more than `minIncrease` entries net (recorded as
`distant_signal:movement_events_lag:deriv` and `:delta`). Lag never reads 0
(the steady-state baseline is a few hundred entries), so this needs both a
rising trend and a real increase. The group consumes slower than
movement-relay publishes; left alone, lag reaches the cap and entries are
trimmed unread. Look for a slow downstream (api latency, DB load) or a
consumer that is CPU-bound.

### DistantSignalStreamGap

trust-consumer, full-coverage-consumer or trust-backlog-consumer found that
movement-events entries were trimmed before it read them
(`<consumer>_stream_gap_detected_total`, recorded per consumer as
`distant_signal:movement_stream_gaps:increase`). Those TRUST events are lost
to that consumer: treat the rail day's derived data as not clean. It is
usually preceded by `DistantSignalMovementLagCritical`.

### DistantSignalDeadLetterGrowing

A consumer group moved records to `movement-events-deadletter`
(`movement_feed_deadlettered_total{group,reason}`). This should stay at 0.
Inspect the records, then re-inject or delete them:
[movement-events-deadletter](movement-events-deadletter.md).

### DistantSignalDeadLetterNearFull

The dead-letter stream holds more than `deadLetter.nearFullRatio` of its
10,000-record cap (`movement_relay_deadletter_length`, falling back to the
consumers' `movement_feed_deadletter_length`). It is never trimmed by count;
once full, new poison records stay pending in their group instead. Drain it:
[movement-events-deadletter](movement-events-deadletter.md#capacity-never-trimmed-by-count).

### DistantSignalDeadLetterFull

A consumer tried to dead-letter records but the stream is at its cap
(`movement_feed_deadletter_full_total`). Those records stay pending and are
retried forever until the stream is drained:
[movement-events-deadletter](movement-events-deadletter.md#deleting).

### DistantSignalMovementRelayPublishFailing

movement-relay failed every XADD (`movement_relay_errors_total{operation=~"publish_event|redis_oom"}`)
and published nothing over `relayPublishFailing.window`
(`distant_signal:movement_relay_publish_failing:bool` is 1). TRUST ingestion
has stopped, and no lag alert fires because the stream simply stops growing.
The relay's `/livez` stays 200 (a failed cycle is progress), so nothing
restarts it. Kafka holds the backlog only for as long as the topic retains it.

- OOM: Redis is at `maxmemory`.
- `MISCONF` or an AOF error: see
  [DistantSignalRedisPersistenceFailing](#distantsignalredispersistencefailing).
- `NOAUTH`: the Redis password changed.
- Redis down: check its pod.

Design: [Redis message queue evaluation](superpowers/specs/2026-09-30-redis-message-queue-evaluation-design.md).

### DistantSignalRedisPersistenceFailing

From movement-relay's `INFO persistence` gauges
(`distant_signal:redis_aof_health:min` is 0): Redis's last AOF write
(`redis_aof_last_write_ok`) or rewrite (`redis_aof_last_bgrewrite_ok`) failed,
or, for the bundled Redis with `redis.persistence.enabled`, AOF is off
(`redis_aof_enabled`).

- A failed AOF write makes Redis refuse every write, so movement-relay stops
  publishing.
- A failed rewrite lets the AOF grow until the disk fills.
- With AOF off, a restart loses movement-events and every consumer group.

Check `INFO persistence`, the Redis logs and the disk behind `/data`.

### DistantSignalMovementGroupRecreated

A consumer found its group missing (`NOGROUP`) and recreated it
(`movement_feed_group_recreated_total{group}`), or movement-relay found the
stream itself missing and recreated it with every group
(`movement_relay_stream_created_total`, shown as `group="(all)"`); recorded
as `distant_signal:movement_group_recreations:increase`. Redis lost its data
(AOF lost or unreadable, volume replaced) or the group was deleted. Its
pending entries, and after an empty restart every entry it had not read, are
gone, and the stream-gap check cannot see this. Treat that rail day as
partial.

### DistantSignalDeadLetterExpiring

The oldest dead letter (`movement_relay_deadletter_oldest_age_seconds`) is
within `deadLetterExpiring.warnBeforeTrimSecs` of
`movementRelay.deadLetterMaxAgeSecs`, after which movement-relay deletes it
(the TRUST 1-day retention safeguard). Re-inject it, or deliberately discard
it, before then:
[movement-events-deadletter](movement-events-deadletter.md#retention-deleted-after-24-hours).

### DistantSignalMovementFeedLongPending

A group re-read entries already delivered more than 240 times
(`movement_feed_long_pending_total{group}`). They are retried forever and
never dead-lettered: a long downstream outage, or a failure the consumer
cannot classify as a data rejection. See
[long-pending entries](movement-events-deadletter.md#long-pending-entries),
and act before the stream cap trims them.

### DistantSignalTrustEnvelopeParseDrops

A consumer dropped TRUST envelopes whose body did not match the known shape
(`<consumer>_errors_total{operation="parse_envelope",msg_type}`, recorded as
`distant_signal:trust_envelope_parse_drops:increase`). A steady rate for one
`msg_type` means the feed's schema changed, which otherwise looks like trains
no longer moving. See
[envelope parse drops](movement-events-deadletter.md#envelope-parse-drops).

## enricher

### DistantSignalEnricherErrors

More than `enricherErrors.errorRatio` of an LLM call site's calls failed over
the window, with at least `minErrors` failures
(`distant_signal:enricher_llm_call_failures:ratio` and `:increase`, from
`enricher_llm_call_total{outcome!="success"}`). Incidents are not being
enriched. Check the LLM endpoint (`enricher.llm.baseUrl`), its credentials
and rate limits, and enricher's logs. The `outcome` label narrows it down:
`quota_exhausted` means the provider account is out of credit or at its
billing limit (top it up; no retry helps), and `refused` means the model
declined the text on safety grounds. Failure log lines carry the
provider's `request_id` where it sends one (OpenAI does); see
[enricher-openai.md](enricher-openai.md). With keyless auth
(`enricher.llm.auth`), `auth_error` means no access token could be minted
(see DistantSignalEnricherTokenExchangeFailing below) and `unauthorized`
means OpenAI answered 401 even to a freshly exchanged token: check that the
OpenAI service account, its project and the mapping still exist and are
enabled.

### DistantSignalEnricherTokenExchangeFailing

Only rendered with keyless auth (`enricher.llm.auth` is `openaiWifAuthentik`
or `openaiWifKubernetes`). At least `enricherTokenExchange.minFailures`
token requests to one `stage` failed over the window and none succeeded
(`distant_signal:enricher_llm_token_exchange_failures:increase` and
`:successes:increase`, from `enricher_llm_token_exchange_total`). The
enricher keeps using its cached OpenAI token until it expires (at most an
hour; `enricher_llm_token_remaining_seconds` shows what is left), so this
can fire before extractions fail with `outcome="auth_error"`.

The `outcome` label of `enricher_llm_token_exchange_total` and enricher's
`LLM token request rejected` log lines (`stage`, `status`, `error_code`,
`error_description`; never a token) narrow it down:

- `token_file_error`: the projected token is missing or empty. Check the
  pod's `openai-identity-token` volume and that the pod runs as
  `enricher.serviceAccount`.
- `stage="authentik"`, `invalid_client` or `invalid_grant`: Authentik
  refused the k8s token. The k3s signing key rotated (update the Generic
  OAuth Source's JWKS), the token's audience no longer matches the
  application's expression policy, or the generated service account left
  the `<ds-openai-enricher>` group.
- `stage="openai"`, `invalid_subject_token` or `invalid_grant`: OpenAI
  refused the subject token or found no mapping: an issuer/audience change,
  a signing-key rotation (in `openaiWifKubernetes` mode, upload the new
  JWKS first), or a mapping that no longer matches the `sub` or the group
  attribute.
- `timeout`, `error`, `http_error`: the endpoint is unreachable or failing
  (NetworkPolicy egress, DNS, an Authentik or OpenAI outage).

The checklist and rotation runbook are in
[enricher-openai.md](enricher-openai.md#keyless-auth-workload-identity-federation).
To switch from Authentik to the fallback, follow "Switching to the fallback"
there.

## full-coverage windows

Design: [windowed full-coverage stats](superpowers/specs/2026-09-27-full-coverage-windowed-stats-design.md).

### DistantSignalFullCoverageWindowFeedStale

full-coverage-consumer has marked its windows `feed_stale`
(`full_coverage_consumer_window_feed_stale`): too few TRUST Activations in the
last hour, or no movement event recently. No window influences severity and
no train is presumed cancelled meanwhile. Expected during a known TRUST
outage; otherwise check movement-relay and the consumer's lag.

### DistantSignalFullCoverageWindowPostErrors

At least 3 POSTs to `/private/full-coverage-window-stats` failed
(`full_coverage_consumer_errors_total{operation="post_window_stats"}`) within
5 minutes, continuously for 10 minutes (`postErrorsThreshold`,
`postErrorsWindow`, `postErrorsFor`), the same shape as
[DistantSignalConsumerApiCallsFailing](#distantsignalconsumerapicallsfailing).
The consumer posts about once a minute, so the one POST that fails while an
api rollout is down does not fire it; a failure on every POST does. An api
without that route (not yet deployed, or its migrations not run) answers 404.
Check api's logs and the consumer's warn log.

### DistantSignalFullCoverageWindowStatsStalled

No window rows posted (`full_coverage_consumer_window_rows_posted_total`)
while windowed stats are on. The consumer posts on every stats write (about a
minute) once its startup replay is done, so look for a stuck replay, a
population that never loaded, or failing POSTs
([DistantSignalFullCoverageWindowPostErrors](#distantsignalfullcoveragewindowposterrors)).

## resources

### DistantSignalComponentMemoryHigh

A container in this release's pods has a working set (cadvisor) above
`componentMemory.ratio` of its memory limit (kube-state-metrics), recorded as
`distant_signal:container_memory_working_set:limit_ratio`. It is at risk of
an OOMKill: raise its `resources.limits.memory`, or find the growth.

## notifier

### DistantSignalNotifierPushDropped

The notifier dropped decided notifications
(`notifier_push_dropped_total{reason}`). Delivery is at-most-once by decision
(triage DQ9), so these users may never get them.

- `queue_full` / `user_queue_full`: raise the notifier's push queue capacity
  or worker count.
- `send_failed`: push endpoints are failing; check
  `notifier_push_send_total` by `outcome`.
- `error`: the notifier could not load subscriptions; check the database.

Design: [line-status notifications](superpowers/specs/2026-09-02-line-status-notifications-design.md#delivery-guarantee-at-most-once).

## ingest-writer

### DistantSignalIngestWriterDown

The ingest-writer (`ingestWriter.enabled`) has been down for
`ingestWriterDown.for`: its scrape target's `up` is 0, or its Deployment
(`<release>-ingest-writer`) has no available replica. It owns the
train-domain sweeps once `ingestWriter.loops.enabled` is on (schedule-match,
reconciliation, backlog-match, the CORPUS crosswalk), and from phase 3 it
applies the ingest streams. While it is down, new trains stay unmatched to
their schedules and the TRUST backlog is not matched; nothing is lost, the
sweeps catch up when it is back.

1. `kubectl -n distant-signal get pods -l app.kubernetes.io/component=ingest-writer`
   and `kubectl -n distant-signal logs deploy/distant-signal-ingest-writer --previous`:
   a crash loop, an OOM, or a failed start.
2. Not ready but running: it waits at the schema gate until the migrations
   it was built with are applied (the migrate hook Job's logs), then needs
   Postgres (see [DistantSignalPostgresDown](#distantsignalpostgresdown)).
   A `permission denied` (42501) means its role lacks a grant
   (`files/db-grants.yaml`, the role setup Job).
3. Restarting: `/livez` fails when a loop makes no progress for
   `ingestWriter.progressStallSecs`; the log names the loop.
4. While the api still runs its own loops (`API_BACKGROUND_LOOPS` true, the
   default), they keep sweeping; if the api's are already off and the
   writer cannot be fixed quickly, set `API_BACKGROUND_LOOPS=true` on the
   api (the advisory locks keep the two from sweeping at once).

Design: [ingest architecture](superpowers/specs/2026-10-06-ingest-architecture-design.md#10-the-ingest-writer).

### DistantSignalTrainEventOutboxRejected

With `trustConsumer.ingest.sink: db`, trust-consumer cannot write
`train_subscriptions`: a resolution, cancellation or reinstatement (and the
later events of the same subscription) waits in `train_event_outbox` for the
ingest-writer's `train_event_outbox` loop (ingest plan 3b.3). The loop
refused one for a data error (SQLSTATE class 22/23) within
`trainEventOutbox.rejected.window`
(`distant_signal_store_train_event_outbox_total{outcome="rejected"}`). The
row stays, with `rejected_at` and `rejection`; that subscription's change did
not land, and later events of it go ahead.

1. `SELECT id, tracked_train_id, dedup_key, rejected_at, rejection, event
   FROM train_event_outbox WHERE rejected_at IS NOT NULL ORDER BY id;` and
   the writer's log (`train-event outbox row refused`).
2. Fix the cause (usually a migration or a constraint the event breaks),
   then re-queue the row: `UPDATE train_event_outbox SET rejected_at = NULL,
   rejection = NULL WHERE id = ...;` (as the writer or the owner). The next
   tick re-applies it; every write is idempotent.
3. A row that can never apply: delete it, and check the subscription by
   hand (`train_subscriptions.resolution_status`, `trains_id`).

### DistantSignalTrainEventOutboxStuck

The oldest pending `train_event_outbox` row is older than
`trainEventOutbox.stuck.maxAgeSeconds` (120 s) for `stuck.for`
(`time() - distant_signal_train_event_outbox_oldest_pending_timestamp_seconds`,
which the writer sets each tick; 0 when empty). The loop runs every 5 s, so
the loop is not applying rows: subscriptions stop resolving, cancelling and
reopening, and the queued trains' movements do not land. Critical.

1. [DistantSignalIngestWriterDown](#distantsignalingestwriterdown): the writer
   must be up with `ingestWriter.loops.enabled`.
2. The writer's log for `train-event outbox apply failed`: a transient error
   (Postgres, a lock timeout) rolls the tick back and retries. A
   `permission denied` (42501) means the writer role lacks SELECT, UPDATE
   or DELETE on `train_event_outbox` (`files/db-grants.yaml`, the role setup
   Job).
3. If the writer cannot be fixed quickly, set `trustConsumer.ingest.sink:
   http`: the api then applies everything inline again; the rows already
   queued are applied once the loop runs.

## ingest streams

The `ds:ingest:*` Redis streams (ingest spec §7, §14.2; plan 3a.4): the
pollers and full-coverage-consumer XADD snapshots, and the ingest-writer
applies them to Postgres through one consumer group, `ingest-writer`. The
metrics are `crates/ingest-stream`'s (`src/metrics.rs`, labelled `stream`).
These alerts render with `ingestWriter.enabled` and their
`metrics.prometheusRule.ingest*` toggles, and stay silent until a stream is
on: their series do not exist before. Dead letters, inspection and
re-injection: [ingest-streams-deadletter](ingest-streams-deadletter.md).

The usual first steps: the writer's log (`kubectl -n distant-signal logs
deploy/distant-signal-ingest-writer`), filtered on the stream; and the
group's view in Redis (`XINFO GROUPS <stream>`, `XPENDING <stream>
ingest-writer`).

### DistantSignalIngestStreamBacklog

A stream's oldest pending entry
(`ingest_stream_oldest_pending_age_seconds`) is older than
`ingestStreamBacklog.oldestPendingAgeSecs` (600), or its unread
(`ingest_stream_lag`) plus delivered-but-unACKed (`ingest_stream_pending`)
entries are over `warningRatio` (0.5) of its `MAXLEN ~` cap (warning) or
over `criticalRatio` (0.8) (critical), for `for`. Recorded as
`distant_signal:ingest_stream_behind:ratio`, with each stream's cap from
`ingestStreamBacklog.maxlen` (keep in step with
`crates/ingest-stream/src/budget.rs`).

At the cap, the oldest entries are trimmed before they are applied
(`ingest_stream_consumed_total{outcome="trimmed"}` counts the pending ones).
For the snapshot streams only the newest snapshot matters for correctness,
so a trim loses history granularity, not current state.

1. A transient failure retries the same entry forever, in order: the log
   says "failed transiently; left pending". Usually the database (see
   [DistantSignalPostgresDown](#distantsignalpostgresdown)); a
   `permission denied` is a missing grant.
2. An entry of an unknown schema version also stays pending: see
   [DistantSignalIngestUnsupportedSchema](#distantsignalingestunsupportedschema).
3. A slow writer (high `ingest_stream_handler_seconds`): look at the
   database's load.
4. No consumer at all: [DistantSignalIngestWriterDown](#distantsignalingestwriterdown),
   or that stream is `off` in the writer while its producer writes.

### DistantSignalIngestStreamStalled

The producers' XADDs to a stream succeeded within
`ingestStreamStalled.stallAfterSecs` (about 3x the stream's cadence), but
the writer last applied an entry of it longer ago than that
(`time() - ingest_stream_last_applied_timestamp_seconds`, recorded as
`distant_signal:ingest_stream_unapplied:seconds` while over), for `for`.
Data is reaching Redis and not Postgres: pages go stale.

Shadow mode counts as applying (`skipped`), and so does a duplicate. A
writer that has applied nothing since it started has no last-applied
series and does not fire this; the backlog alert covers it. Work through
[DistantSignalIngestStreamBacklog](#distantsignalingeststreambacklog)'s
steps; the writer's `/livez` also fails once a stream task makes no
progress for `ingestWriter.progressStallSecs`.

### DistantSignalIngestDeadLetters

The writer moved entries to the stream's dead-letter stream
(`ds:dlq:<domain>`; `ingest_stream_dead_lettered_total{stream,reason}`)
within `ingestDeadLetters.window`. Reasons: `poison` (the handler refused
the whole entry: an unknown schema name, or a data error), `undecodable`
(a broken envelope or body), `oversize` (a body over 1 MiB),
`rejected_rows` (the entry was applied, but some rows were refused and only
those were dead-lettered). This should stay at 0. Inspect, fix, re-inject
or delete: [ingest-streams-deadletter](ingest-streams-deadletter.md).

### DistantSignalIngestDeadLetterExpiring

A dead-letter stream's oldest entry (`ingest_stream_dlq_oldest_age_seconds`)
is within `ingestDeadLetterExpiring.warnBeforeSecs` (4 h) of
`retentionSecs` (7 days), after which the writer's hourly `MINID` trim
deletes it. Re-inject it or decide it can go, before then:
[ingest-streams-deadletter](ingest-streams-deadletter.md#retention).

### DistantSignalIngestUnsupportedSchema

The writer left an entry pending because it does not know its schema
*version* (`ingest_stream_consumed_total{outcome="unsupported_schema"}`): a
producer was rolled out before the writer that understands its new
schema. The stream is blocked behind that entry (entries apply in order),
so this is critical. Roll the ingest-writer forward to a build that has the
schema (spec §13.3: writer first, then producers), or roll the producer
back. Nothing is lost while the entry stays pending, up to the stream's
`MAXLEN`. Do not `XACK` it by hand.

### DistantSignalIngestProducerXaddFailing

Every XADD to a stream failed over `ingestProducerXaddFailing.window`
(`ingest_stream_produce_total`: some `outcome!="ok"`, none `ok`). The
outcome label says why: `down` (Redis unreachable or loading), `oom`
(Redis at `maxmemory`), `noauth` or `noperm` (the producer's ACL user or
password, `redis.acl`), `misconf` (a failed RDB/AOF write), `error`. A
snapshot producer keeps only its newest snapshot, retries with backoff,
and reports readiness `stream_unavailable`.

1. The producer's log (the poller named by the failing pod).
2. Redis: `INFO memory`, `INFO persistence`, and `ACL LOG` for a refused
   user.
3. With `oom`, see also
   [DistantSignalIngestStreamMemoryHigh](#distantsignalingeststreammemoryhigh).

### DistantSignalIngestStreamMemoryHigh

The ingest streams and their dead-letter streams together use over
`ingestStreamMemoryHigh.ratio` (0.75) of `budgetBytes` (512 MiB, D5) of
Redis (`sum(ingest_stream_bytes)`, the `MEMORY USAGE` of each stream and
its dead-letter stream). Every stream at its cap is about 285 MB worst
case, so a breach means a cap or an entry size is larger than budgeted
(`crates/ingest-stream/src/budget.rs`). Look at the per-stream values, then
fix the backlog or drain dead letters
([ingest-streams-deadletter](ingest-streams-deadletter.md)). Redis's
`maxmemory` is shared with movement-events: at it, every XADD fails with
OOM.

### DistantSignalIngestClockSkew

The writer clamped an observed time to its own `now() + 2 min`
(`ingest_stream_observed_at_clamped_total{stream,schema}`; D13, spec §7.8)
within `ingestClockSkew.window`. A producer stamped `produced_at` (or a row
time) more than 2 minutes in the future: its node's clock is ahead of the
writer's. The clamp keeps the ordering guard working (a row stamped in the
future is overwritten by the next snapshot), so data is not blocked, but
that producer's freshness and history times are off. Compare the clocks
(`date -u` in the producer and writer pods; NTP on the nodes). The cluster
is a single node today, so this should not fire.

### DistantSignalIngestShadowMismatch

The rollout's compare step (plan 3a, spec §13.1) disagrees: while a
producer is on `http+shadow`, the rows the ingest-writer handled for one
schema (`ingest_stream_rows_total`, `shadow` or `apply`) differ from the
rows the api accepted (`ingest_stream_sink_rows_total{sink="http"}`) by
more than `ingestShadowMismatch.ratio` over its `window`
(`distant_signal:ingest_stream_rows_vs_http:ratio`, 1 when they agree).
Do not flip that producer to `stream` until it is quiet again.

- **0** (the copy never arrives): the producer's XADDs fail (see
  `DistantSignalIngestProducerXaddFailing`, the producer's log, its Redis
  ACL user and NetworkPolicy), or the writer's stream is `off`.
- **Below 1**: copies are lost between the POST and the writer: XADD
  failures, entries superseded while Redis was down
  (`ingest_stream_produce_dropped_total{reason="superseded"}`), dead
  letters (`DistantSignalIngestDeadLetters`), or an encoding failure (the
  producer logs it; `reason="oversize"`).
- **Above 1**: the writer saw rows the api did not count: more than one
  producer pod, or a dead-letter re-injection during the window.

A short blip around a deploy is expected (counters restart); the `for`
covers it.

## users

### DistantSignalUserSignupSpike

api created more than `userSignupSpike.threshold` user accounts within the
window. Sign-up is open to any account the IdP admits (in production, any
Discord account through Authentik's Discord source), so a burst is real growth
or scripted sign-ups (spam, abuse of `/chat` or the MCP). Look at the newest
rows in `users` and the IdP's enrolment log. To stop it, restrict the IdP's
enrolment (e.g. bind the application to a group), then revoke the unwanted
sessions: [session revocation](session-revocation.md).

## cold archive

Details: [cold archive](cold-archive.md).

### DistantSignalArchiveUploadFailures

The aggregator failed cold-archive batch uploads or verifications
(`aggregator_archive_upload_failures_total`). With `failurePolicy: retain` the
`trains` table grows past its retention window while this fires; with
`delete`, rows are pruned without an archive. Check the object store and the
aggregator's "archiving a trains batch failed" logs.

### DistantSignalArchiveExpiryErrors

Cold-archive expiry hit errors (`aggregator_archive_expiry_errors_total{stage}`):
a failed LIST or DELETE, or a key refused by the pre-delete check. Expired
objects stay until a run succeeds. Check the object store and the
aggregator's "archive expiry" logs. See
[object expiry](cold-archive.md#object-expiry).

### DistantSignalArchiveExpiryUnmatchedKeys

Expiry listed keys under an archive table prefix that do not match the archive
layout (`aggregator_archive_expiry_skipped_unmatched_total{table}`). They are
never deleted. Something other than the aggregator writes there, or the prefix
is shared; the aggregator logs the first keys each run.

### DistantSignalArchiveExpiryCapReached

An expiry run found more expired objects than
`archive.expiry.maxDeletesPerRun` and stopped at the cap (in dry-run: would
have). Expected once after a long outage or a retention cut; otherwise check
`aggregator_archive_expiry_candidates` before raising the cap.

### DistantSignalArchiveExpiryOverdue

The oldest archived `service_date`
(`aggregator_archive_oldest_service_date_seconds{table}`) is older than
`archive.expiry.retentionDays` plus `overdueGraceDays`. Expiry is not running,
is failing, or is still in dry-run (`aggregator_archive_expiry_dry_run` is 1):
switch `archive.expiry.dryRun` off.

## schedule pipeline

### DistantSignalScheduleReferenceNotSeeded

schedule-reference has been waiting to read its last completed publish from
api (`GET /private/schedule-reference-publishes`), so it publishes nothing
(`schedule_reference_seeded` is 0). Check api and the internal OAuth token
endpoint; the container's warn log names the error.

### DistantSignalScheduleFeedZipRejected

schedule-ingest refused to extract an uploaded CIF delivery zip: over
`MAX_EXTRACTED_BYTES` or `MAX_ZIP_ENTRIES`, or an entry that inflates past
what it declares (PL-5). The timetable is not refreshed until a new upload
replaces it. The ingest container's error log names the reason.

### DistantSignalScheduleFeedIngestRejected

schedule-ingest extracted a CIF delivery but api answered 400/413/422 to its
ingest record, so schedule-reference never sees it. It is not retried until a
new upload replaces it. The ingest container's error log and api's log name
the reason.

### DistantSignalCorpusRejected

schedule-ingest refused a CORPUS extract (not gzip, not the TIPLOCDATA JSON,
too few rows, a row without an NLC, or over the decompression cap) and moved
it to `/data/schedule-feed/corpus/rejected/`. `corpus_locations` keeps the
previous delivery. The ingest container's error log names the reason.

### DistantSignalCorpusStale

The newest loaded Network Rail CORPUS extract was delivered over
`corpusStaleAfterDays` ago (`api_corpus_last_delivered_at_seconds`). CORPUS is
published monthly. Either RDM stopped pushing it (check the SFTP corpus
folder) or schedule-ingest cannot load it (check
[DistantSignalCorpusRejected](#distantsignalcorpusrejected) and the ingest
container's error log). The previous extract stays in use.

### DistantSignalScheduleReferencePublishStale

schedule-reference last fully published a delivery timestamped over
`staleAfterHours` ago
(`schedule_reference_last_published_delivery_timestamp_seconds`). Either no
new delivery arrived (check schedule-ingest and the SFTP upload) or one keeps
failing to publish (check `schedule_reference_publishes_total` by `outcome`,
and the reference container's error log).

### DistantSignalSchedulePublishStagedMismatch

A publish finished with a staged key count that did not match the
publisher's total, so the rows that publish left out were not deleted. They
stay until the next complete publish. Counted by api
(`api_schedule_publish_staged_mismatch_total{product}`) or, with
schedule-reference's db sink (`scheduleFeed.reference.ingest.sink: db`), by
schedule-reference (`store_schedule_publish_staged_mismatch_total{product}`);
the warn log of whichever counted it has the `publish_id` and both counts.

### DistantSignalScheduleReferencePublishRejected

api refused one of schedule-reference's products with 400, 413 or 422. It is
not retried for this delivery and the delivery is not recorded as published,
so that product keeps its previous rows (a restart tries the delivery again).
Usually a schema skew between the two services or an oversized chunk; the
reference container's error log has api's response body.

### DistantSignalLinePopulationMissing

After 06:00 London, full-coverage-consumer still has no schedule line
population for today's rail day on some lines
(`full_coverage_consumer_population_missing_past_deadline_lines`), so their
full-coverage stats stay Pending. schedule-reference publishes each line's
population for today and tomorrow, so both last night's and the previous
delivery failed to provide it. Check
[DistantSignalScheduleReferencePublishStale](#distantsignalschedulereferencepublishstale)
and the consumer's population reload errors.

## schedule SFTP

These read SFTPGo's own telemetry (`scheduleFeed.sftp.telemetry`), so they
render only with `scheduleFeed.enabled` and telemetry on. Failed logins and
anomalous `dtd-push` logins are Loki rules, not metrics: the counters carry no
username or source IP. The full runbook is
[schedule-feed-sftp.md](schedule-feed-sftp.md).

### DistantSignalSftpNoUpload

SFTPGo received no upload in `noUploadWindow` (30h): DTD's daily push did not
arrive. Quiet until the counter has a full window of history. Uploads are not
per file type, so a CORPUS upload can mask a missing CIF;
DistantSignalScheduleReferencePublishStale stays authoritative for the
timetable. Check the `sftp` container's log for DTD's `login` and `Upload`
lines, and the defender for a ban on DTD's address.

### DistantSignalSftpUploadErrors

An upload failed or was interrupted in the last hour (`sftpgo_upload_errors_total`).
The `Upload` log line's `error` field says why: a size cap, a disconnect, or a
full volume. DTD normally retries; the next delivery replaces a partial file.

### DistantSignalSftpUserStoreDown

SFTPGo's user store is unavailable, so every login fails. The `sftp` container
loads `dtd-push` from its entrypoint at start; restart the pod and read its
startup log (`--loaddata-from` errors, or the password policy refusing a short
password).

## schedule bucket

These read schedule-ingest's bucket source (`scheduleFeed.bucket`, metrics
with `source="bucket"`), so they render only with `scheduleFeed.bucket.enabled`;
`DistantSignalScheduleFeedSourcesDisagree` needs SFTP on as well. The bucket,
its IAM and the kill switch are `charts/ds-ingest-bucket`'s and Ranma's. The
full runbook is [schedule-feed-bucket.md](schedule-feed-bucket.md).

### DistantSignalScheduleBucketAccessRevoked

Every call to the bucket answered 401/403, or the reader has no usable key,
for 10 minutes. The SFTP source is unaffected and carries on. The likely
causes are the kill-switch trips (see
[schedule-feed-bucket.md](schedule-feed-bucket.md#kill-switch)): the budget
or a usage alert removed the reader's bindings, Ranma paused the
`kill-switch-group: reader` bindings, or the reader's service account was
disabled. DS backs off to `scheduleFeed.bucket.maxBackoffSecs` and logs once
per state change; nothing in DS restarts it and nothing needs to.

1. Check the bucket's audit log for a reader loop (repeated `objects.get` of
   one generation). If there is one, fix it before anything else.
2. Ask Ranma for a deliberate reapply, which removes `crossplane.io/paused`
   from the `kill-switch-group: reader` bindings. The source recovers on its
   next retry.

A missing or rotated key raises this too: check that the Secret named by
`scheduleFeed.bucket.existingSecret` exists (`kubectl get secret <name>`),
without reading it.

### DistantSignalScheduleBucketNoNewObject

No new expected object has reached the bucket for 30 hours, once one has been
seen. SFTP may still be delivering:
[DistantSignalScheduleReferencePublishStale](#distantsignalschedulereferencepublishstale)
is authoritative for the timetable. Causes are on the publisher's side
(a late or failed push, or a publisher kill-switch trip) or a reader that
cannot list (see the other bucket alerts).

### DistantSignalScheduleBucketReadErrors

The bucket source keeps failing; `kind` on
`schedule_feed_source_errors_total` says where: `list`, `get` (the download),
`verify` (size or CRC32C mismatch: the object is not used and is retried),
`delete`, or `size`. Revoked access (`auth`) is its own alert. The reader backs
off and SFTP is unaffected. Read schedule-ingest's log for the error.

### DistantSignalScheduleBucketUnexpectedObject

An object with a name outside `scheduleFeed.bucket.expectedKeys`, over
`maxObjectBytes`, or not routable to CIF or CORPUS was flagged; it is deleted
unread after `deleteMinAgeSecs` and stays recoverable from soft delete for
7 days. To inspect it, restore it as in
[schedule-feed-bucket.md](schedule-feed-bucket.md#restoring-a-deleted-object),
and check the audit log for who wrote it.

### DistantSignalScheduleBucketDownloadBudget

The reader's hourly or daily download cap stopped it: a download loop or an
attack, stopped by the reader itself. Don't raise the caps before finding the
cause: check the audit log and schedule-ingest's log for repeated downloads
of one object or many new objects.

### DistantSignalScheduleFeedSourcesDisagree

SFTP and the bucket delivered different content of one `kind` (`cif` or
`corpus`) within `scheduleFeed.disagreementWindowMinutes`. The bucket copy won
(decision D5, `scheduleFeed.sourcePrecedence`). Compare the two SHA-256s in
schedule-ingest's audit lines, and ask the publisher which is right.

## pollers

### DistantSignalPollerFailing

`distant_signal:poller_failing:bool` is 1 for a poller: it completed no
successful cycle and at least one failed one over `pollerFailures.window`, or
more than `failureRatio` of its cycles over `ratioWindow` failed
(`poller_cycle_total{result}`), for `for` (10m). Its product is going
stale. With the defaults (15m ratio window, 10m `for`) a poller that fails
every cycle fires after about 18 minutes; the former 1h window with no `for`
took 40-55 minutes on 2026-10-01. Daily pollers
(stations, tocs) run one cycle a day, so one failed daily cycle is enough.
Check the poller's logs: its upstream feed (National Rail, TfL, Irish Rail),
its credentials or an expired key, and api's `/private` ingest route.

### DistantSignalPollerStale

A poller's last successful cycle
(`poller_last_success_timestamp_seconds{cycle}`, recorded by
`common::poller_loop` for every poller; the process start until a cycle
succeeds) is older than `pollerStale.intervalMultiple` (2) times its own
`pollIntervalSecs`, and at least `minAgeSeconds` (1800), for `for` (10m):
30 minutes for ldbws and incidents, two days for the daily pollers. Unlike
DistantSignalPollerFailing it needs no failed cycle, so it also fires for a
poller that has stopped completing cycles at all (a wedged loop, a schedule
that never comes due). If DistantSignalPollerFailing is firing too, start
there. Otherwise check the poller's log for its last "poll cycle" line and
whether the pod is running. poller-ldbws holds undelivered samples while api
is down (`ldbws_pending_samples`) and sends them on the first successful
POST.

### DistantSignalLdbwsStationStale

The least recently sampled LDBWS station
(`ldbws_stalest_station_age_seconds`) is older than `maxAgeSeconds`. The
rotation (SVC-04) has stopped reaching part of the station list, e.g. every
cycle runs out of budget first; compare `ldbws_stations_attempted_per_cycle`
with `ldbws_stations_total`. Stations LDBWS rejects as an invalid CRS are
excluded (see the next alert).

### DistantSignalLdbwsInvalidCrs

LDBWS answers "Invalid crs code supplied" for a sample station
(`ldbws_invalid_crs_station{crs}`), so no line gets LDBWS data from it. It is
almost certainly a typo in a `lines/*.toml` `sample_stations` entry (e.g.
"ANV" for Andover's "ADV"): fix the file, run
`cargo run -p line-catalogue-validator` and redeploy api. poller-ldbws
excludes the station from the stale-station gauge and re-probes it hourly;
the alert clears once api stops listing it or LDBWS accepts it.

### DistantSignalIncidentRemovalStalled

Over the last window (2h, 24 polls), api applied its "Ended (no longer
listed)" inference to no incidents snapshot, and skipped at least one as
`incomplete`, `empty` or a `shrink`
(`api_incident_removal_inference_total{outcome}`; `too_soon` and
`no_baseline` skips are benign and not counted). While this lasts,
an incident that leaves the Knowledgebase feed without RDM clearing it stays
"active" on the archive and in line status, the bug the inference exists to
fix. See
[the design](superpowers/specs/2026-10-06-incident-source-removal-design.md).

- `incomplete`: poller-incidents skipped malformed `<PtIncident>` elements
  (its "skipping malformed" warnings and
  `poller_incidents_skipped_elements_total`), the feed body was cut short,
  or an older poller image is sending the bare array. Find the bad element
  in the poller's log; an old image needs redeploying.
- `empty`: the feed returned no incidents at all, which looks like an RDM
  outage or a changed URL or key. Check the poller's fetch.
- `shrink`: every snapshot was more than 50% smaller than the one before.
  One poll of that is expected after a large purge; the next becomes the
  baseline. Repeated halving is an upstream problem.

Nothing is lost while it fires: once a snapshot passes the guard, rows that
are still missing are counted again, and two complete polls later they show
as ended.

## pgBackRest

Details and procedures: [Postgres PITR](postgres-pitr.md#alerts). The Job
alerts read kube-state-metrics; the archiver alerts read postgres_exporter's
`pg_stat_archiver_*` series.

### DistantSignalPgBackRestCheckFailed

The daily check Job or the weekly verify Job failed within `recentJobWindow`
(`distant_signal:pgbackrest_job_failed:recent`). Either WAL archiving is
broken (the repository is unreachable, or its credentials or stanza are
wrong), `pgbackrest verify` found a bad file, or WAL is missing from the
repository, so point-in-time recovery can't cross that gap. Read the Job's log
to see which: [archiving is failing](postgres-pitr.md#archiving-is-failing),
[WAL gap](postgres-pitr.md#wal-gap).

### DistantSignalPgBackRestBackupFailed

A full or diff backup Job failed within `recentJobWindow`. WAL archiving is
separate and may still be working; the restore window keeps growing from the
last good backup. Read the Job's log and re-run it with
`kubectl create job --from=cronjob/...`.

### DistantSignalPgBackRestBackupStale

Neither the full nor the diff CronJob has succeeded for over `backupMaxAge`
(`kube_cronjob_status_last_successful_time`). Check the Jobs' logs and that
the CronJobs aren't suspended.

### DistantSignalPgBackRestFullBackupStale

The weekly full backup hasn't succeeded for over `fullBackupMaxAge`. Diffs
grow until the next full, and time-based retention keeps the old full (and
its WAL) until a newer one exists. Run the full CronJob by hand.

### DistantSignalPgBackRestArchiveFailing

`archive_command` (pgBackRest `archive-push`) has kept failing
(`pg_stat_archiver_failed_count`). WAL piles up in the spool or `pg_wal`; past
`archive.queueMax` pgBackRest drops it, leaving a PITR gap. See
[archiving is failing](postgres-pitr.md#archiving-is-failing).

### DistantSignalPgBackRestArchiveStalled

No WAL segment was archived (`pg_stat_archiver_archived_count`) over
`archiveStallWindow`, although `archive_timeout` switches segments every
minute while anything writes. Check the Postgres log, and
`pg_stat_archiver`'s `last_failed_wal`.
