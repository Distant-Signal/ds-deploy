# Scheduled jobs

How the chart's CronJobs behave, what alerts on them, and what to do when
one goes wrong. Alerts: [scheduled jobs](alerts.md#scheduled-jobs).

| CronJob | What it runs | Values |
|---|---|---|
| `<fullname>-api-maintenance` | The api image's `maintenance` binary: one pass of the session, dead-link, retention and delegated-access sweeps. | `apiMaintenance` |
| `<fullname>-pgbackrest-{full,diff,check,verify}` | `kubectl exec` of pgBackRest in the Postgres pod. | `postgresql.pgbackrest.backup` ([Postgres PITR](postgres-pitr.md)) |
| `<fullname>-poller-tocs` (only with `pollers.tocs.mode: cronjob`) | poller-tocs, once: one fetch of the TOC list onto `ds:ingest:reference`. 04:40 UTC. | `pollers.tocs.cron` |
| `<fullname>-poller-stations` (only with `pollers.stations.mode: cronjob`) | poller-stations, once: one fetch of the stations feed, written to Postgres in one transaction. 04:20 UTC. | `pollers.stations.cron` |
| `<fullname>-poller-transit-schedule-tfl` (only with `pollerTransitSchedule.networks.tfl.timetableMode: cronjob`) | poller-transit-schedule for TfL, once: one timetable download, parse and publish onto `ds:ingest:transit-tfl`. 04:00 UTC. | `pollerTransitSchedule.networks.tfl.timetableCron` |
| `<fullname>-poller-transit-schedule-metrolink` (only with `pollerTransitSchedule.networks.metrolink.timetableMode: cronjob`) | poller-transit-schedule for Metrolink's timetable, once, onto `ds:ingest:transit-metrolink`. 03:30 UTC. Its situations loop stays in the always-on Deployment of the same name. | `pollerTransitSchedule.networks.metrolink.timetableCron` |
| `<fullname>-poller-tfl-daily` (only with `pollers.tfl.stops.mode: cronjob`) | poller-tfl's daily stops and route-sequences jobs, once each, onto `ds:ingest:tfl-stops`. 05:40 UTC. The status poll stays in the poller-tfl Deployment. | `pollers.tfl.stops.cron` |
| `<fullname>-schedule-reference` (only with `scheduleFeed.reference.mode: cronjob`) | schedule-reference, once: publishes the newest CIF delivery if it is new. Every 30 minutes, Europe/London. | `scheduleFeed.reference.cron` ([below](#schedule-reference)) |

## How a run behaves

Every CronJob sets:

- **`timeZone`**, always explicit (CI fails a CronJob without one). Without
  it the schedule follows the node's local zone. Maintenance and backups use
  `Etc/UTC`; anything tied to the GB rail day uses `Europe/London` and stays
  out of 01:00-02:59 local, where DST skips or repeats a run.
- **`concurrencyPolicy: Forbid`.** A run still going when the next is due
  makes the controller skip that one; it is not queued and not doubled.
- **`startingDeadlineSeconds`.** A start missed by more than this (the
  control plane was down, the node rebooted) is skipped instead of run late.
  It also keeps a long outage from hitting the controller's "more than 100
  missed starts" refusal, which would otherwise stop the CronJob for good.
- **`activeDeadlineSeconds`** on the Job: a run that hangs, or that waits
  Pending for room on the node, is killed and the Job fails with
  `DeadlineExceeded`. This replaces a Deployment's liveness probe.
- **History and TTL.** `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`
  keep the last runs to read logs from, and `ttlSecondsAfterFinished`
  deletes each finished Job and its pod after a day (pgBackRest: three days,
  longer than its alerts' window).

The Jobs of this chart's own binaries also set (`distant-signal.scheduledJobFailurePolicy`):

- **`restartPolicy: Never`** and **`podReplacementPolicy: Failed`**: a retry
  is a new pod, started only once the failed one has fully stopped, so two
  pods of one run never overlap and each attempt keeps its own log.
- **A pod failure policy**:

  | The pod ended with | What the Job does |
  |---|---|
  | exit 137 (OOMKilled at its memory limit) | fails at once: the same input would OOM again |
  | exit 3 (the run refused its input) | fails at once |
  | a `DisruptionTarget` condition (evicted, preempted, node drained) | retries, without counting it |
  | anything else: 1 (a transient error), 143 (SIGTERM, as the host's earlyoom kills) | retries, up to `backoffLimit` |

  A failed Job is not retried until the next scheduled run.
- **Resources**, requests and limits, ephemeral storage included. A pod with
  none is BestEffort: the first the kubelet evicts and earlyoom kills.

**Runs must be idempotent.** Kubernetes creates a CronJob's Job "about once"
per schedule: rarely twice, rarely not at all. `api-maintenance`'s steps are
all idempotent, pgBackRest's own lock refuses a second backup, and a poller's
once mode skips a run whose data is still fresh (below).

**Running alongside deploys.** A Helm upgrade changes only the Jobs created
after it; a run already going finishes on its old image. Nothing needs to be
suspended for an ordinary deploy.

## Workers on a schedule

A worker with a `mode` value runs either as its always-on Deployment
(`deployment`, the default) or as a CronJob (`cronjob`); the chart renders
exactly one of them per loop, and CI checks it. Five pollers have one,
each a daily job that spends the rest of the day idle: `pollers.tocs.mode`,
`pollers.stations.mode`, poller-tfl's daily jobs (`pollers.tfl.stops.mode`)
and, for poller-transit-schedule's TfL and Metrolink networks,
`pollerTransitSchedule.networks.<network>.timetableMode`.
schedule-reference has `scheduleFeed.reference.mode`
([below](#schedule-reference)). The CronJob comes from
`distant-signal.cronWorkerSpec` with the defaults above, and its pods keep
the component's labels, so its NetworkPolicies, Redis ACL user, database
role and Secrets apply exactly as they did to the Deployment.

Each run sets `RUN_ONCE=true`, which every poller's loop
(`common::poller_loop`) understands:

1. It reads the same last-fetch cursor the Deployment does (for tocs, the
   newest `tocs/1` entry on its stream; for stations, its `ingest_freshness`
   row; for a transit timetable, the newest commit on its stream, unless a
   `timetableRefresh` token is unanswered). If the data landed less than
   `cron.skipIfFresherThanSecs` ago (half a day for tocs), it logs "data
   still fresh; skipping this run" and exits 0 without calling upstream. A
   doubled Job, a manual run or a retry soon after a success therefore
   costs no RDM request.
2. Otherwise it runs one cycle. A stream producer then waits (up to two
   minutes) for its snapshot to be written to Redis before it exits; the
   writer applies it as before. poller-stations writes Postgres itself, in
   one awaited transaction inside the cycle (retrying a transient failure
   for up to 15 minutes), so its data is committed before the exit. If the write doesn't finish in time, the run
   exits 1 and the Job retries.
3. Exit codes: 0 for success or a skip; 1 for a transient failure (the
   upstream, Redis or the database), which the Job retries up to
   `cron.backoffLimit`; 3 for refused data, which fails the Job at once.

A run serves no `/metrics` (`METRICS_ENABLED=false`): it would be gone
before Prometheus scraped it. So DistantSignalPollerStale has no rule for a
poller on a schedule, and DistantSignalPollerFailing can't see it. It is
covered instead by:

- the scheduled-job alerts below, which add its CronJob to their
  failure and staleness rules;
- `DistantSignalIngestSourceStale` for its `ingest_freshness` source
  (`tocs` and `stations` older than 48 hours, `transit-tfl-timetable`
  older than 36 hours), which the always-on ingest-writer exports.

A poller only gets a `cronjob` mode once its binary waits for its writes
before a once-mode exit (`common::poller_loop::finish_once`, or a write
awaited inside the cycle); until then the chart refuses it.

### The transit timetable

poller-transit-schedule runs two loops in one process, the daily timetable
and the five-minute situations (SIRI-SX). `TRANSIT_LOOPS` picks them:
`both` (the default, one Deployment), `timetable` or `situations`. Once mode
needs `timetable` alone, or whichever loop returned first would end the
run. TfL has no situations loop, so its timetable CronJob is all of it.

Metrolink's situations loop runs every five minutes, so with
`timetableMode: cronjob` and `situationsEnabled` the network is two
workloads:

| Workload | Runs | Resources |
|---|---|---|
| CronJob `<fullname>-poller-transit-schedule-metrolink` | the timetable, once a day at 03:30 UTC (`TRANSIT_LOOPS=timetable`, `RUN_ONCE`) | `timetableCron.resources`: requests `128Mi`, limit `512Mi`, while it runs |
| Deployment `<fullname>-poller-transit-schedule-metrolink` (the same Deployment, changed in place) | the situations loop alone (`TRANSIT_LOOPS=situations`), always on | `situationsResources`: requests `32Mi`, limit `256Mi` |

Each loop runs in exactly one of them, and CI checks that for every render.
So two pods never XADD the same schema: the CronJob's `Forbid` and
`podReplacementPolicy: Failed` keep its timetable runs apart, and the
situations pod produces no timetable. They share the network's stream and
Redis user, as the two loops did in one pod. The situations Deployment
keeps its `/metrics`, its DistantSignalPollerStale rule
(`transit-situations`) and the `transit-metrolink-situations` freshness
source (30 minutes). The timetable is covered by the scheduled-job alerts
and the `transit-metrolink-timetable` source (36 hours).

A Metrolink run downloads the BODS North West GTFS (about 159 MB) into its
`/work` emptyDir, streams the parse (a 48 MiB peak) and publishes. It runs
at 03:30 UTC, after the 03:00 UTC dump and before TfL (04:00), stations
(04:20), tocs (04:40) and the 05:00 UTC backups.

The chart refuses `timetableMode: cronjob` for `net`. It also refuses it for
a network with `situationsEnabled` but no `situationsResources` (only
Metrolink has them).

A TfL run downloads the Journey Planner export (141 MB on 2026-10-26) into
its `/work` emptyDir, parses it (12 s, a 332 MiB peak), and publishes 61
parts. It requests the Deployment's 448Mi, but only for the run's 20
seconds or so, not all day. It runs at 04:00 UTC, after the 03:00 UTC dump,
before the 05:00 UTC pgBackRest backups, and apart from the other scheduled
pollers, so its peak meets no other. A refused timetable (the feed guard:
a sharp drop, no service today, ...) exits 3: the previous timetable stays
active, the Job fails without downloading again, and
DistantSignalScheduledJobFailed fires (DistantSignalTransitFeedRefused
can't see a run that exports no metrics). A refusal now waits for the next
interval in the always-on loop too, instead of re-downloading the export
on the failed-cycle backoff.

**Capacity.** A Job's requests are only reserved while it runs, so the
448Mi the TfL Deployment held all day (and the 160Mi Metrolink's gives
back) is free between runs, and other pods can take it. If the node has no
room for a run's requests at its time (448Mi at 04:00, 128Mi at 03:30),
the pod waits Pending:
DistantSignalScheduledJobPending fires after 15 minutes, and the run fails
at `activeDeadlineSeconds` (30 minutes). The next day's run, or a manual
one, catches up within the 36-hour freshness allowance.

### poller-tfl's daily jobs

poller-tfl runs a five-minute line-status poll and, while
`pollers.tfl.stops.enabled`, two daily jobs: the stops job and (with
`stops.routes`) the route-sequences job, about 110 TfL calls in all.
`TFL_JOBS` picks them: `all` (the default, one Deployment), `status` or
`daily`. With `pollers.tfl.stops.mode: cronjob` the Deployment runs
`status` and a CronJob `<fullname>-poller-tfl-daily` runs `daily` once a day
at 05:40 UTC. Each daily job skips if its own snapshot on the stream is
fresher than half a day, then waits for its snapshot to reach the stream.
Both jobs run even if one fails. A transient failure of either exits 1, so
the Job retries and the job that succeeded skips. A refused snapshot (the
feed guard) exits 3.

Why 05:40 UTC, when the Deployment ran them in the evening: it is clear of
the 03:00 dump, the 03:30–04:40 scheduled pollers and the 05:00 backups (a
diff takes about 3 minutes). The routes are also fresh before the morning
peak. TfL's rate limit is per key per minute, not per time of day; the
status poll shares the key but makes one call per 5 minutes. A 429 waits
what TfL asks (`Retry-After`), up to six attempts a call, which the
45-minute `activeDeadlineSeconds` covers.

The status poll keeps its `/metrics` and DistantSignalPollerStale
(`tfl`). The daily jobs' `tfl-stops` and `tfl-routes` stale rules drop. They
are covered by the scheduled-job alerts and by DistantSignalIngestSourceStale
for the `tfl-stops` and `tfl-line-routes` sources (2 days). That alert is
on in both modes.

### schedule-reference

schedule-reference publishes the CIF timetable's derived tables once a
night, after the delivery lands (22:00-01:30 London), and sits idle the
rest of the day: about 5Mi between publishes, a 1429Mi peak during one,
against the 1792Mi the `reference` container requested all day in
production. With `scheduleFeed.reference.mode: cronjob` it runs as
`<fullname>-schedule-reference` instead of in the schedulefeed pod.

**The pod.** Its own CronJob (component label `schedule-reference`:
Postgres admits it, and its egress policy allows Postgres and DNS only),
mounting the schedulefeed PVC **read-only** at `/data/schedule-feed`, as
the container did. The PVC is ReadWriteOnce, which binds it to one node,
not one pod: the schedulefeed-sftp pod already shares it that way. In
production it is `local-path` on the single node `mine-bringer`, its PV
pinned there by node affinity (checked 2026-11-01), so the CronJob's pods
land beside the others. On a multi-node cluster pin them together
(`scheduleFeed.nodeSelector`/`affinity` apply to the CronJob too).

**The schedule.** `*/30 * * * *` in Europe/London, the Deployment's
1800-second poll, so a delivery is picked up as quickly as before (from
extraction to a completed publish took 18-38 minutes, 2026-10-20 to
10-31). The 01:00-02:59 rule above is about heavy runs that DST skips or
repeats; here every run is the same cheap check, and only the one that
finds a new delivery is heavy. On the spring night the 01:00 and 01:30
runs don't exist, so a delivery landing then waits up to an hour more; on
the autumn night a repeated run finds the delivery published (or the lock
held) and exits.

**A run** (`RUN_ONCE=true`, `crates/schedule-reference`'s `run_once`):

1. Takes the run lock, `pg_try_advisory_lock` on a fixed key
   (`common::advisory_locks::SCHEDULE_REFERENCE_RUN_KEY`, ASCII
   `dsschref`), on a connection of its own, held for the whole run. If
   another run holds it (a doubled Job, a manual run) it exits 0. The
   Deployment takes the same lock for each cycle, so while the mode
   switches the two never publish at once.
2. Reads its completion marker (`schedule_reference_publishes`), up to 7
   times over about a minute (the Deployment waits forever), and records
   `seeded` in `schedule_reference_signals`. Unreadable: exit 1.
3. Finds the newest complete delivery on the PVC. None, the one the marker
   names, or one an earlier run settled (below): exit 0. That is all most
   runs do, a few seconds.
4. Otherwise publishes every product, with the same in-run retries as the
   Deployment's cycle, and writes the marker: exit 0.

Exits:

| Exit | When | The Job |
|---|---|---|
| 0 | nothing new, lock held, or a complete publish | succeeds |
| 1 | the marker unreadable, or a product failed in a way a retry could fix | retries up to `backoffLimit` (2), each attempt a **full republish**; then the next scheduled run tries again |
| 3 | a product refused as bad data (SQLSTATE class 22/23), or input it can't publish (an ALF with no links, a window with no trains) | fails at once; the delivery is **settled** (`kind = 'settled'`), so later runs skip it until a newer delivery lands |

**No resume.** The Deployment remembered, in memory, which products of a
failed delivery had gone through and redid only the rest. A run keeps
nothing: the next one republishes the whole delivery. That is safe because
every product's publish is idempotent: the crosswalks upsert and prune,
`tiploc_locations` and `fixed_links` replace the table, the line
populations and network departures upsert, the per-date products are diff
publishes under a new publish id (whose first chunk discards whatever an
abandoned attempt staged), and the marker is an upsert. A DB test
(`a_full_republish_leaves_the_tables_as_one_publish_does`) publishes the
same delivery twice and finds the tables as one publish leaves them. The
cost is a heavier retry: the full publish again, 20-odd minutes and the
1.4GiB peak.

**Settled deliveries.** As a Deployment, a delivery with a permanent
failure advanced the in-memory marker (not the durable one), so it was
not attempted again until a restart. A run has no memory, so it records
the delivery as settled instead; otherwise every run would republish it
every 30 minutes until the next delivery. To try it again after a fix:
`DELETE FROM schedule_reference_signals WHERE kind = 'settled'` (as the
owner), then start a run by hand.

**Metrics and alerts.** A run serves no `/metrics`. Its four alerts read
the always-on ingest-writer's gauges of its database rows instead, with
the same windows (docs/alerts.md):

| Alert | As a Deployment | As a CronJob |
|---|---|---|
| DistantSignalScheduleReferenceNotSeeded | `schedule_reference_seeded` 0 for 30m | no `seeded` signal for 1h (`cronNotSeededAfter`: a 30-minute interval plus 30m), not while suspended |
| DistantSignalScheduleReferencePublishStale | `schedule_reference_last_published_delivery_timestamp_seconds` older than 30h | `schedule_reference_recorded_published_delivery_timestamp_seconds` (the newest `schedule_reference_publishes` row) older than 30h |
| DistantSignalSchedulePublishStagedMismatch | `increase(store_schedule_publish_staged_mismatch_total[6h]) > 0` | a `staged_mismatch` signal within 6h |
| DistantSignalScheduleReferencePublishRejected | `increase(schedule_reference_publishes_total{outcome="rejected"}[6h]) > 0` | a `rejected` signal within 6h |

The ingest-writer reads `schedule_reference_signals` and
`schedule_reference_publishes` every minute
(`distant_signal_schedule_reference_recorded_signal_timestamp_seconds{kind, product}`
and `..._recorded_published_delivery_timestamp_seconds`). schedule-reference
records the signals in either mode; only the rules change with the mode.
The scheduled-job alerts cover its runs as for the pollers.

**Capacity.** The Job requests 1536Mi (the 1429Mi peak) and 250m CPU, with
a 4Gi memory limit and 64Mi/512Mi ephemeral storage, only while a run is
going: seconds every half hour, 20-40 minutes once a night. The budget is
to **leave about 1.8GiB of memory requests unclaimed** on the node, the
1792Mi the container held all day, so that each run finds room. Nothing
reserves it (no PriorityClass, no preemption): it is a budget kept by
whoever sizes the node's other workloads (Ranma-Config,
`docs/specs/resource-tuning-2026-10.md`). If it is spent, the nightly run
waits Pending: DistantSignalScheduledJobPending fires after 15 minutes,
the run fails at `activeDeadlineSeconds` (90 minutes), and the next run
30 minutes later tries again; DistantSignalScheduleReferencePublishStale
follows at 30 hours.

**Connections.** The run lock's session is one connection beside the pool
(3), so the role's computed limit is now pool + 2 (5), in either mode.

**Switching.** Set `scheduleFeed.reference.mode: cronjob`; the release
recreates the schedulefeed pod without `reference` (schedule-ingest
restarts with it; SFTP is in its own pod) and creates the CronJob, which
first runs at the next :00 or :30. A manual run
(`kubectl create job --from=cronjob/<fullname>-schedule-reference ...`)
should log "no new delivery to publish" and exit 0 within seconds.
`scheduleFeed.reference.resources` no longer applies; the run uses
`scheduleFeed.reference.cron.resources`. **Rollback:** set it back to
`deployment` (or remove it). The container returns, seeds from the same
marker and does not republish a published delivery. The tables, the
marker and the signals are the same either way; a delivery settled by a
run is attempted again by the Deployment (it doesn't read `settled`).

### Switching a worker over, and back

The same for each (shown for tocs; stations uses `pollers.stations.mode`,
a transit timetable `pollerTransitSchedule.networks.<network>.timetableMode`;
for Metrolink the release changes the Deployment to situations only rather
than deleting it). To
switch, set `pollers.tocs.mode: cronjob` in the deploy values. The
release deletes the Deployment and creates the CronJob, which first runs at
`cron.schedule` (04:40 UTC). To check it before then, start a run by hand
(`kubectl create job --from=cronjob/<fullname>-poller-tocs ...`). Within
half a day of the Deployment's last fetch, that run logs the skip and
exits 0.

To roll back, set `pollers.tocs.mode: deployment` (or remove the value). The
Deployment returns and waits out what is left of its day from the same
cursor, so it doesn't re-fetch. Nothing else changes: the stream, the
writer, the Redis user and the database are the same either way.

## Alerts

From kube-state-metrics, in the `<fullname>-scheduled-jobs` PrometheusRule
(`metrics.prometheusRule.scheduledJobs`):

- `DistantSignalScheduledJobFailed`: a run failed, with the reason.
- `DistantSignalScheduledJobStale`: no success for 1.5 times the schedule
  interval (and at least 30 minutes), while no run is going (a long run
  ends by `activeDeadlineSeconds`; if it hits that, it fails).
- `DistantSignalScheduledJobOOMKilled`: a Job pod was killed at its memory
  limit. The cluster's restart-based OOM alert can't see this: nothing
  restarts under `restartPolicy: Never`.
- `DistantSignalScheduledJobPending`: a Job pod can't be scheduled, or its
  image won't pull.

A short-lived pod is gone before Prometheus scrapes it, so these alerts read
the Job and pod objects, not the run's own `/metrics`; a run's details are in
its log.

## When one fails

1. `kubectl get jobs -n distant-signal --sort-by=.metadata.creationTimestamp`
   and `kubectl describe job <job>`: the Failed condition's reason, and for
   a pod failure policy, which rule matched.
2. `kubectl logs -n distant-signal job/<job>` (kept until the TTL).
3. Run it again now, if the next run is far off:
   `kubectl create job -n distant-signal --from=cronjob/<cronjob> <cronjob>-manual-$(date +%s)`.
4. To stop it for a while, set the CronJob's `suspend` value (or
   `kubectl patch cronjob <cronjob> -p '{"spec":{"suspend":true}}'`, which the
   next Helm upgrade reverts). A run already going is not stopped.

**OOMKilled:** raise the workload's `resources.limits.memory` (the run's peak
plus half again) and release.

**Pending:** a scheduled pod reserves its requests only while it runs, so the
room it needs at its time can have gone to other pods. `kubectl describe pod`
says why it can't be placed.

## Rollback

Each CronJob has its own switch (`apiMaintenance.enabled`,
`postgresql.pgbackrest.backup.suspend`, `pollers.tocs.mode`,
`pollers.stations.mode`, `pollers.tfl.stops.mode`,
`pollerTransitSchedule.networks.<network>.timetableMode`,
`scheduleFeed.reference.mode`).
Setting a transit network back to `deployment` returns its Deployment to
both loops; the timetable waits out its day from the stream cursor. The
failure policy, the replacement policy and the TTLs need no rollback of
their own: removing them only makes a Job retry an OOM kill, count
evictions, and keep its pods until the history limits.
