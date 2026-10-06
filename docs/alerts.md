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
| trust-consumer | `reload_tracked_trains`, `post_train_events`, `reload_stanox_crs`, `startup_reference_load` |
| trust-backlog-consumer | `post_batch`, `post_train_reasons`, `reload_stanox_crs` |
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
   api's own log.
3. Failed POSTs leave their batches pending in movement-events and are
   retried, so nothing is lost until the stream's MAXLEN trims them: watch
   [DistantSignalMovementLagHigh](#distantsignalmovementlaghigh).

### DistantSignalApiPublic5xx

More than `api5xx.public.ratio` (5%) of api's public requests answered 5xx
over `api5xx.window` (5m), with at least `minErrors` (3) of them, for `for`
(10m). Public means every route except `/private/*`, `/public/health` and
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

api finished a publish whose staged key count did not match the publisher's
total (`api_schedule_publish_staged_mismatch_total{product}`), so it did not
delete the rows that publish left out. They stay until the next complete
publish. api's warn log has the `publish_id` and both counts.

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
