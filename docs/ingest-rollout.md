# Ingest-affecting deploys: the rollout check

A deploy that restarts an ingest workload may leave **duplicates**, which
are harmless. It must never leave a **gap**: TRUST movements or Darwin
messages that were skipped, or trimmed from a Redis stream before anyone
read them. `scripts/check-ingest-rollout.py` checks for gaps before and
after such a deploy, reading only from Prometheus. Ranma runs it around
every ingest-affecting deploy.

## When

Run it for any deploy whose render changes a pod template in the chart's
`ingest` or `pollers` groups ([`charts/distant-signal/components.yaml`](../charts/distant-signal/components.yaml)):

- the relays: movement-relay, darwin-relay;
- the movement-events consumers: trust-consumer, trust-backlog-consumer,
  full-coverage-consumer;
- the ingest-writer;
- any poller.

That includes an image bump of any of them, a change to their values, and
a Redis restart, since every one of them depends on Redis. The deploy notes
of a ds-deploy PR say whether it rolls pods. When in doubt, run it.

## How

From a machine that can reach the cluster's Prometheus (for example
through `kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090`):

```sh
export PROMETHEUS_URL=http://127.0.0.1:9090

# 1. Just before approving the deploy.
uv run scripts/check-ingest-rollout.py baseline --out /tmp/ingest-baseline.json

# 2. Once Flux has applied it.
uv run scripts/check-ingest-rollout.py verify --baseline /tmp/ingest-baseline.json
```

`verify` polls every 30 s (`--interval-seconds`) until every check passes
or 15 minutes have passed (`--wait-minutes`). If nobody took a baseline,
`--before <time>` reads one from Prometheus as it was at that time (a unix
timestamp, or ISO 8601 in UTC, e.g. `--before 2026-11-04T19:05:00`). Use a
time shortly before the deploy started.

If Prometheus needs authentication, pass `--bearer-token-file FILE` (or set
`PROMETHEUS_BEARER_TOKEN`) or `--basic-auth-file FILE` (holding
`user:password`). `--ca-file` sets a CA bundle for https. The script sends
only `GET /api/v1/query`, so it can't change anything. `--record FILE`
saves every query and answer; attach that file when reporting a failure.

## What it checks

Gap signals come first. A gap fails at once, without waiting, because
waiting won't undo it:

| Check | Fails when |
|---|---|
| stream gaps | trust-consumer, full-coverage-consumer or trust-backlog-consumer counted a stream gap since the baseline (`*_stream_gap_detected_total`): entries were trimmed before that consumer read them |
| group recreated | a movement-events consumer group was recreated (`movement_feed_group_recreated_total`): Redis lost the stream or the group |
| darwin offset reset | darwin-relay reset its Kafka offset (`darwin_relay_offset_resets_total`): its committed offset was gone |
| dead letters | new dead letters since the baseline: movement-feed's (`movement_feed_deadlettered_total`), the ingest streams' (`ingest_stream_dead_lettered_total`), or a dead-letter stream longer than before |
| cronjob (a poller's) | a poller CronJob in the `pollers` group missed a scheduled start, its `kube_cronjob_next_schedule_time` over 10 minutes in the past with no run going, so that run never happened. Or a run of it started since the baseline failed (`kube_job_failed`). Its data waits for the next run. Suspended CronJobs are only reported; a late start with a run still going waits |

Recovery checks may need time. A check that hasn't passed by the deadline
fails:

| Check | Passes when |
|---|---|
| deployments | every `ingest` and `pollers` Deployment has exactly 1 pod, which is available and updated, and its new spec has been observed (kube-state-metrics). A Deployment scaled to 0 is reported, not failed. |
| poller | every poller with a pod created since the baseline (its `poller_cycle_total` series and `kube_pod_created`) has a successful cycle in that pod. A poller that ran fewer than 6 cycles an hour before the baseline (the baseline records each one's cadence) may not be due within the wait, so it is only reported. A poller's upstream can't be replayed, so a new pod that never completes a cycle is a growing gap. |
| movement-relay ready | `movement_relay_ready` is 1 |
| movement-relay publishing | `movement_relay_events_published_total` rose over the last 5 minutes (05:00 to 23:00 UTC; overnight a flat counter is only reported) |
| movement-relay partitions, Kafka lag | both TRUST partitions held (`movement_relay_assigned_partitions`, summed over pods), and Kafka lag (`movement_relay_consumer_lag`) within 5000 records of the baseline |
| group lag | each movement-events group's lag is within the larger of 2000 entries and 50% of its baseline, and its pending entries within 100 (or 50%) of theirs |
| full-coverage replay | `full_coverage_consumer_startup_complete` is 1 (it replays the rail day before consuming; 15 minutes is ample) |
| ingest streams pending | the oldest pending entry on every `ds:ingest:*` stream is under 5 minutes old |
| darwin lag | `darwin_relay_consumer_lag` is at most 50000 messages (the alert's threshold) |

Duplicates are reported as `INFO`, never failed: the entries
full-coverage-consumer replayed and the re-delivered entries since the
baseline.

A check whose metric has no series prints `SKIP ... not available`
instead of failing. That covers a workload that is off (darwin-relay
outside production), and a metric the running image doesn't export yet.
movement-relay's partitions and Kafka lag gauges and darwin-relay's
offset-reset counter are exported from ds-ingest 662fe534 on, so with an
older image those three checks skip.

Exit status: 0 when every check passed or was skipped; 1 on a gap, or when
something hadn't recovered within the wait; 2 on a usage error or an
unreachable Prometheus.

## When it fails

- **A gap** (stream gaps, group recreated, offset reset): data was lost
  for the window the counter covers. Note the time range from the
  consumers' logs. The movement-events alerts in [alerts.md](alerts.md)
  (`DistantSignalStreamGap`, `DistantSignalMovementGroupRecreated`,
  `DistantSignalDarwinOffsetReset`) carry the runbooks. Record it on the
  deploy request.
- **Dead letters:** see `DistantSignalDeadLetterGrowing` and
  `DistantSignalIngestDeadLetters` in [alerts.md](alerts.md). Dead letters
  are kept, not lost. Re-inject them once the cause is fixed.
- **Not recovered** (a Deployment, lag, the replay, pending entries): the
  data is still in Kafka or the Redis streams, so this isn't a gap yet.
  It becomes one when the Kafka retention or the stream's `MAXLEN` runs
  out, so fix it now, don't wait. Start with `DistantSignalIngestRolloutStuck`
  and `DistantSignalMovementRelayNotReady` in [alerts.md](alerts.md), then
  roll back the image if the new version is at fault.

## Pollers

Unlike the TRUST and Darwin feeds, no poller's upstream can be replayed:
what a poller misses while no pod is polling is lost (ds-pollers
`docs/redeploy-safety.md` has the per-poller review). For incidents and
TfL line status, that loss is permanent history. So the frequent pollers
replace a pod without stopping the old one first
(`pollers.<name>.rollout`, `pollerIrishRailLive.rollout`):

| Rollout | Pollers (default) | What a deploy does |
|---|---|---|
| `surge` | incidents, ldbws, tfl, irish-rail-live | RollingUpdate, maxSurge 1, maxUnavailable 0. The new pod is Ready (`/healthz`) only after its first successful cycle, or after `workerHealth.pollerReadyFallbackSecs` (480 s) if it hasn't had one; then the old pod stops. The new pod's first cycle comes when the old schedule says (its startup cursor), so a deploy costs at most one duplicate poll. |
| `recreate` | stations, tocs, irish-rail-gtfs, nir-stations; transit-schedule (fixed) | The old pod stops, then the new one starts. Their data is daily reference, healed by the next run. transit-schedule must never run two timetable producers at once. |

With `surge`, a new pod that crashes, waits on the schema gate or fails
every cycle never replaces the old one. The rollout waits instead, and
`DistantSignalIngestRolloutStuck` (`extra_pods`) fires if a pod never
becomes Ready. The fallback also makes a new pod Ready during an upstream
outage, when the old pod fails too, so such a rollout still finishes
inside Flux's timeout. A surge poller's Postgres role must admit both
pods. The chart computes its CONNECTION LIMIT that way and refuses an
explicit limit below it. `scripts/check-poller-rollout-chart.py` (CI)
checks every poller Deployment's strategy, probe and fallback.

The surge needs a poller image with ds-pollers' `poller-lifecycle`. With an
older image `/healthz` is ready at once, so the old pod stops as soon as
the new one starts. That is the old behaviour minus the image pull, never
worse.
