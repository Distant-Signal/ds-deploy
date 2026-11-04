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

Recovery checks may need time. A check that hasn't passed by the deadline
fails:

| Check | Passes when |
|---|---|
| deployments | every `ingest` and `pollers` Deployment has exactly 1 pod, which is available and updated, and its new spec has been observed (kube-state-metrics). A Deployment scaled to 0 is reported, not failed. |
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
