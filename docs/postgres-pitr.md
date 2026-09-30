# Postgres point-in-time recovery (pgBackRest)

The bundled Postgres can archive its WAL and take backups with pgBackRest,
so the database can be restored to any moment in the retention window
(7 days by default), not just to last night's dump. The feature is **off by
default**: with `postgresql.pgbackrest.enabled: false` nothing below
renders, and the Postgres pod is exactly as it is without it.

It doesn't replace the nightly age-encrypted `pg_dump` (Ranma-Config's
`distant-signal-postgres-backup`). Keep that running. It is independent of
pgBackRest, portable across Postgres versions, and encrypted to a key that
never touches the cluster.

- Design: [the backup design](superpowers/specs/2026-09-30-backup-and-observability-gaps-design.md), item 1.
- Chart: `postgresql.pgbackrest.*` and `metrics.prometheusRule.pgbackrest`
  in `charts/distant-signal/values.yaml`; `templates/_pgbackrest.tpl`,
  `templates/postgres-pgbackrest.yaml`,
  `templates/pgbackrest-prometheusrule.yaml`.
- Image: `docker/postgres-pgbackrest.Dockerfile`, with the daily check
  script `docker/pgbackrest/pgbackrest-daily-check.sh`.

## How it works

| Piece | What it does |
| --- | --- |
| Image | `postgres-pgbackrest`: the exact `postgres:16.15-trixie` image the chart pins, plus pgBackRest from the PGDG repository and the `pgbackrest-daily-check` script. The chart swaps it in for the stock image. |
| WAL archiving | `archive_mode=on`, `archive_command='pgbackrest --stanza=ds archive-push %p'` and `archive_timeout=60` are added to the Postgres `-c` args. Archiving is asynchronous, through a spool at `/var/lib/postgresql/data/pgbackrest-spool` on the data volume, beside `PGDATA`. |
| Repository | S3 (Thoth on mine-bringer), under `repo.path` in `repo.s3.bucket`. Every file is encrypted client-side with AES-256-CBC before it leaves the pod, and compressed with zstd. |
| Disk-full guard | `archive-push-queue-max` (`archive.queueMax`, 8 GB). If the repository is unreachable for long enough that 8 GB of WAL waits in the queue, pgBackRest **drops** WAL instead of letting `pg_wal` fill the node's disk. Postgres stays up; the dropped WAL is a gap that point-in-time recovery can't cross. |
| Backups | CronJobs `<release>-pgbackrest-full` (Sunday 03:30 UTC) and `-diff` (Monday–Saturday 03:30 UTC). Each backup then runs `expire`, which keeps the full backups and WAL needed to restore to any point in the last `repo.retentionFull` days. |
| Daily check | CronJob `<release>-pgbackrest-check` (05:00 UTC) runs `pgbackrest-daily-check`: `pgbackrest check` (archiving works end to end), `pgbackrest verify` (every file in the repository decrypts and matches its checksum), and a WAL gap check (every segment since the oldest backup on the current timeline is in the repository). |
| How the CronJobs run | `kubectl exec` into `<release>-postgres-0`, container `postgres`, so pgBackRest runs next to the data directory with the container's own `PGBACKREST_*` env. Their ServiceAccount's Role allows `get` on that one pod and `pods/exec` on it, and nothing else. They are the only pods in the chart that mount a ServiceAccount token. |
| Secrets | From existing Secrets only: the S3 key pair (`repo.s3.existingSecret`) and the cipher passphrase (`repo.cipher.existingSecret`, defaulting to the same Secret). |

All CronJobs run in `Etc/UTC`, clear of the nightly schedule ingest
(22:00–01:30). See the chart README, "Scheduled jobs and time zones".

### The cipher passphrase

**Keep an offline copy of the passphrase, next to the age identity for the
logical dumps.** The repository can't be read without it. A copy that exists
only in the cluster (a Secret, or a SealedSecret whose sealing key lives in
the cluster) is lost with the cluster, and so is every pgBackRest backup.

The passphrase can't be changed for an existing repository. To rotate it,
create a new repository (a new `repo.path`) with the new passphrase, run
`stanza-create` and a full backup there, and delete the old path once its
backups have aged past the retention window.

## Enabling it

1. **Measure WAL.** Take a 24-hour `pg_stat_wal.wal_bytes` delta with the
   current settings, to size the repository:

   ```sql
   SELECT wal_bytes, now() FROM pg_stat_wal;  -- again 24 h later; subtract
   ```

   Expect several times less than the 35 GB/day measured before
   `wal_compression` and the larger `max_wal_size`, and plan on 2–8 GB/day
   stored after zstd. Confirm the repository has room for 7 days of that
   plus two full backups.

2. **Smoke-test the S3 target** from a tailnet machine with pgBackRest
   installed, against a throwaway path (e.g. `/mine-bringer/pgbackrest/smoke`)
   and a throwaway local Postgres: `stanza-create`, `archive-push` (via a
   running server), `backup`, `expire` with `--repo1-retention-full=1` after
   a second full backup (this proves DeleteObjects works), and `restore`. If
   `expire` fails, stop: the repository would grow without bound.

3. **Create the Secret** holding `access-key-id`, `secret-access-key` and
   `cipher-pass` (key names configurable). Generate the passphrase fresh,
   for example `openssl rand -base64 48`, and store it offline first.

4. **Allow egress from the Postgres pod to the S3 endpoint**, and from the
   CronJob pods to the Kubernetes API server, if the namespace has a
   default-deny egress policy. The chart renders neither (its NetworkPolicies
   give the Postgres pod no egress policy).

5. **Build and pin the image.** `containers.yml` publishes
   `postgres-pgbackrest` tagged `pg<postgres>-pgbackrest<version>` (e.g.
   `pg16.15-pgbackrest2.59.1`); every push to main re-points that tag at a
   fresh build. Set `postgresql.pgbackrest.image.tag` to the tag **with a
   digest**, read from
   `docker buildx imagetools inspect ghcr.io/fasterspeeding/distant-signal/postgres-pgbackrest:pg16.15-pgbackrest2.59.1`.
   The chart refuses to render without it, because a per-release default
   would restart Postgres on every deploy. Moving to a newer digest (a
   Postgres patch release, or a pgBackRest bump) restarts Postgres once, so
   do it on purpose. Keep it on the same Postgres version as
   `postgresql.image.tag`.

6. **Turn it on:** `postgresql.pgbackrest.enabled: true`, plus `repo.path`,
   `repo.s3.endpoint`, `repo.s3.port`, `repo.s3.bucket` and
   `repo.s3.existingSecret`. Postgres restarts once, for `archive_mode`.

7. **Create the stanza straight away.** Until it exists every
   `archive-push` fails and WAL stays in `pg_wal`:

   ```sh
   kubectl -n distant-signal exec distant-signal-postgres-0 -c postgres -- \
     pgbackrest --log-level-console=info stanza-create
   kubectl -n distant-signal exec distant-signal-postgres-0 -c postgres -- \
     pgbackrest --log-level-console=info check
   ```

8. **Take the first full backup** rather than waiting for Sunday, then run
   the check job:

   ```sh
   kubectl -n distant-signal create job --from=cronjob/distant-signal-pgbackrest-full pgbackrest-full-initial
   kubectl -n distant-signal logs -f job/pgbackrest-full-initial
   kubectl -n distant-signal create job --from=cronjob/distant-signal-pgbackrest-check pgbackrest-check-initial
   kubectl -n distant-signal logs -f job/pgbackrest-check-initial
   ```

   A backup starts at the next regular checkpoint (up to
   `checkpoint_timeout`, 15 minutes), so the log is quiet for a while at
   first.

9. **Watch it for 7 days**, then run the first drill (below).

`pgbackrest info` shows the backups and the archived WAL range at any time:

```sh
kubectl -n distant-signal exec distant-signal-postgres-0 -c postgres -- pgbackrest info
```

## Alerts

With `metrics.prometheusRule.enabled`, the chart renders a
`<release>-pgbackrest` PrometheusRule. The Job alerts read kube-state-metrics;
the archiver alerts read postgres_exporter's `pg_stat_archiver_*` series
(`stat_archiver` collector, on by default), which this chart doesn't deploy.

| Alert | Severity | Meaning | What to do |
| --- | --- | --- | --- |
| `DistantSignalPgBackRestCheckFailed` | critical | The daily check Job failed. | Read the Job's log (`kubectl logs job/<name>`). A failed `check` means archiving is broken: see "Archiving is failing". A failed `verify` names the bad file: take a full backup, and find out what damaged it. "WAL gap": see below. |
| `DistantSignalPgBackRestBackupFailed` | warning | A full or diff backup Job failed. | Read the Job's log. Re-run it with `kubectl create job --from=cronjob/...`. A rerun resumes a partial backup. |
| `DistantSignalPgBackRestBackupStale` | warning | No full or diff backup has succeeded for 30 h. | As above; also check the CronJob isn't suspended. |
| `DistantSignalPgBackRestFullBackupStale` | warning | No full backup for 8 days. | Run the full CronJob by hand. |
| `DistantSignalPgBackRestArchiveFailing` | warning | `archive_command` keeps failing. | See "Archiving is failing". |
| `DistantSignalPgBackRestArchiveStalled` | warning | No WAL segment archived for 15 minutes, although `archive_timeout` switches segments every minute while anything writes. | As for failing. |

Ranma-Config's `CronJobNotSucceeding` also covers the three CronJobs (their
names don't end in `-backup`).

### Archiving is failing

1. Look for pgBackRest's error in the Postgres log:
   `kubectl logs distant-signal-postgres-0 -c postgres | grep -i -E 'pgbackrest|archive'`.
2. Common causes: the S3 endpoint is down or unreachable (the NetworkPolicy,
   DNS, TLS), the credentials changed, or the stanza doesn't exist.
3. While it fails, WAL waits in the spool and `pg_wal`. Watch the data
   volume. At `archive.queueMax` pgBackRest starts dropping WAL (a WAL gap).
4. Once it's fixed, archiving catches up by itself. Run the check job by
   hand to confirm.

### WAL gap

The check job fails with `WAL gap: N of M segments since the oldest backup
are missing` when WAL was dropped at `queueMax` (or lost some other way).
Point-in-time recovery works up to the start of the gap, and again from the
first backup taken after it, but can't cross it.

1. Fix whatever stopped archiving (above).
2. Take a full backup now:
   `kubectl create job --from=cronjob/distant-signal-pgbackrest-full pgbackrest-full-after-gap`.
3. The check keeps failing until `expire` removes the backups from before
   the gap, i.e. for up to `retentionFull` days. Silence
   `DistantSignalPgBackRestCheckFailed` until then, but only after reading
   each day's log to confirm that the gap is the only failure.

## Point-in-time restore (a real incident)

This replaces the live database. Decide the target time first (UTC), just
before the damage. A time later than the newest archived commit fails
recovery with "recovery ended before configured recovery target was
reached"; to restore to the latest point instead, use `--type=default`
without `--target`/`--target-action`.

1. **Stop Flux** from undoing the changes below:
   `flux suspend helmrelease distant-signal -n distant-signal`.

2. **Stop the writers and Postgres:**

   ```sh
   for d in api aggregator enricher notifier; do
     kubectl -n distant-signal scale deploy "distant-signal-${d}" --replicas=0
   done
   kubectl -n distant-signal scale statefulset distant-signal-postgres --replicas=0
   ```

   Scale down every other workload that writes too (the consumers,
   movement-relay, the pollers and schedulefeed) if the target is far enough
   back that their replays would matter.

3. **Start a restore pod** from the StatefulSet's own pod template, so it
   has the same image, env, Secrets and security context. It gets its own
   component label so the Postgres Service doesn't select it (the Thoth
   egress policy must select it too, see the deploy notes), mounts the data
   volume, and sleeps:

   ```sh
   kubectl -n distant-signal get statefulset distant-signal-postgres -o json | jq '
     .spec.template
     | .metadata.labels["app.kubernetes.io/component"] = "pgbackrest-restore"
     | {apiVersion: "v1", kind: "Pod",
        metadata: {name: "pgbackrest-restore", labels: .metadata.labels},
        spec: (.spec
          | .containers = [.containers[0]
              | .command = ["sleep", "infinity"] | .args = []
              | del(.readinessProbe, .livenessProbe, .startupProbe)]
          | .volumes = ((.volumes // []) + [{name: "data",
              persistentVolumeClaim: {claimName: "data-distant-signal-postgres-0"}}]))}
   ' | kubectl apply -f -
   kubectl -n distant-signal wait --for=condition=Ready pod/pgbackrest-restore
   ```

4. **Keep the damaged data directory** and restore into a fresh one. The
   rename doubles the disk use for a while (local-path has room):

   ```sh
   kubectl -n distant-signal exec pgbackrest-restore -- sh -c \
     'mv "$PGDATA" "$PGDATA.damaged-$(date -u +%Y%m%dT%H%M%SZ)" && mkdir -m 0700 "$PGDATA"'
   kubectl -n distant-signal exec pgbackrest-restore -- \
     pgbackrest --log-level-console=info --type=time \
       --target="2026-10-01 12:34:00+00" --target-action=promote restore
   ```

   (If disk is short, skip the rename and add `--delta` to restore over the
   damaged directory in place. Nothing of it is kept then.)

5. **Delete the restore pod and start Postgres.** It replays WAL from the
   repository up to the target, then promotes onto a new timeline and
   carries on archiving:

   ```sh
   kubectl -n distant-signal delete pod pgbackrest-restore
   kubectl -n distant-signal scale statefulset distant-signal-postgres --replicas=1
   kubectl -n distant-signal logs -f distant-signal-postgres-0 -c postgres
   ```

   Wait for `database system is ready to accept connections`.

6. **Check the app role's password.** A physical restore brings back
   `pg_authid` as of the target. If the password in the Secret changed
   since then (a rotation), set it again:
   `ALTER ROLE distant_signal PASSWORD ...` through `kubectl exec ... psql`,
   reading the password from the Secret without echoing it.

7. **Run the verification queries** (below), then scale the workloads back
   up and resume Flux:
   `flux resume helmrelease distant-signal -n distant-signal`. Migrations are
   a no-op: the schema is at the target point, and the api applies anything
   newer on start.

8. **Take a full backup** so the new timeline has one:
   `kubectl create job --from=cronjob/distant-signal-pgbackrest-full pgbackrest-full-after-restore`.
   Delete the kept `pgdata.damaged-*` directory once nobody needs it.

### Verification queries

Run after every restore and every drill:

```sql
-- Should match the migrations the running api image ships.
SELECT max(version) FROM _sqlx_migrations WHERE success;
-- Within the expected change from the source.
SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM journeys) AS journeys,
       (SELECT count(*) FROM train_subscriptions) AS subscriptions, (SELECT count(*) FROM trains) AS trains;
-- Close to the target time: movement events arrive every few seconds.
SELECT max(received_at) FROM train_movement_events;
SELECT max(created_at) FROM trains;
SELECT max(created_at) FROM journeys;
-- Should return no rows.
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

## Drill

Monthly for the first quarter, then quarterly. **Never touch production for
a drill**: restore into a scratch pod with an `emptyDir`, never into the
real data volume, and start it with **archiving off**, so it can't push a
new timeline into the production repository.

1. Start a scratch pod from the StatefulSet's pod template, with an
   `emptyDir` of about twice the database size instead of the data volume,
   its own component label (the Thoth egress policy must select it), and
   small requests, because the node has little CPU or memory left to
   request:

   ```sh
   kubectl -n distant-signal get statefulset distant-signal-postgres -o json | jq '
     .spec.template
     | .metadata.labels["app.kubernetes.io/component"] = "pgbackrest-restore"
     | {apiVersion: "v1", kind: "Pod",
        metadata: {name: "pgbackrest-drill", labels: .metadata.labels},
        spec: (.spec
          | .containers = [.containers[0]
              | .command = ["sleep", "infinity"] | .args = []
              | .resources = {requests: {cpu: "100m", memory: "512Mi"}, limits: {memory: "2Gi"}}
              | del(.readinessProbe, .livenessProbe, .startupProbe)]
          | .volumes = ((.volumes // []) + [{name: "data", emptyDir: {sizeLimit: "15Gi"}}]))}
   ' | kubectl apply -f -
   kubectl -n distant-signal wait --for=condition=Ready pod/pgbackrest-drill
   ```

2. Restore to about an hour ago, and time it:

   ```sh
   kubectl -n distant-signal exec pgbackrest-drill -- sh -c '
     mkdir -m 0700 "$PGDATA" && date -u &&
     pgbackrest --log-level-console=info --type=time \
       --target="$(date -u -d "1 hour ago" "+%Y-%m-%d %H:%M:%S+00")" \
       --target-action=promote restore && date -u'
   ```

3. Start Postgres with archiving off, on the local socket only, with a small
   `shared_buffers` (the pod keeps the production memory limit), and time
   the WAL replay:

   ```sh
   kubectl -n distant-signal exec pgbackrest-drill -- sh -c '
     date -u && pg_ctl -D "$PGDATA" -l /tmp/drill.log -w -t 3600 \
       -o "-c archive_mode=off -c listen_addresses= -c shared_buffers=256MB" start && date -u'
   ```

4. Run the verification queries:
   `kubectl -n distant-signal exec -it pgbackrest-drill -- psql -U distant_signal -d distant_signal`.
   `max(created_at)` should be close to the target.

5. Record the RTO: restore time plus replay time. The earlier estimate is
   30–90 minutes; the first drill replaces it with a measurement.

6. `kubectl -n distant-signal delete pod pgbackrest-drill`.

Keep drilling the logical dump too: once a quarter, restore the latest
`.sql.gz.age` with the procedure in Ranma-Config's `backup-encryption.md`.
That drill needs the offline age identity, so it stays manual.

## Turning it off

Set `postgresql.pgbackrest.enabled: false`. Postgres restarts once, on the
stock image with archiving off, and the CronJobs, Role and alerts are
removed. The repository is left as it is: delete `repo.path` from the
bucket by hand once its backups are no longer wanted. They hold personal
data until they're deleted (see [personal-data-retention.md](personal-data-retention.md)).
