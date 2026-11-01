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
exactly one, and CI checks it. Three have one, each a daily job that
spends the rest of the day idle: `pollers.tocs.mode`,
`pollers.stations.mode` and, for poller-transit-schedule's TfL network,
`pollerTransitSchedule.networks.tfl.timetableMode`. The CronJob comes from
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
run. TfL has no situations loop, so its timetable CronJob is all of it. A
network with situations on (Metrolink) would need its situations loop
moved to a Deployment of its own (`TRANSIT_LOOPS=situations`) beside the
timetable CronJob. The binary supports that, but the chart doesn't render
it yet, so it refuses `timetableMode: cronjob` for any network but tfl, and
for tfl with `situationsEnabled`.

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
448Mi the Deployment held all day is free between runs, and other pods can
take it. If the node has no room for 448Mi at 04:00, the pod waits Pending:
DistantSignalScheduledJobPending fires after 15 minutes, and the run fails
at `activeDeadlineSeconds` (30 minutes). The next day's run, or a manual
one, catches up within the 36-hour freshness allowance.

### Switching a worker over, and back

The same for each (shown for tocs; stations uses `pollers.stations.mode`,
the TfL timetable `pollerTransitSchedule.networks.tfl.timetableMode`). To
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
  interval (and at least 30 minutes).
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
`pollers.stations.mode`, `pollerTransitSchedule.networks.tfl.timetableMode`). The failure policy, the replacement
policy and the TTLs need no rollback of their own: removing them only makes a
Job retry an OOM kill, count evictions, and keep its pods until the history
limits.
