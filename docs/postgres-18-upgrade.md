# Postgres 16 to 18 upgrade (pg_upgrade)

Production's bundled Postgres moves from 16.15 to 18.6 in one maintenance
window, with `pg_upgrade --copy` run as a Kubernetes Job on the stopped
database's volume. This is the runbook for that window: pre-checks, the
steps, verification and rollback. The Ranma session runs it; the user
approves the window and each production change.

Decisions (2026-10-26):

- **Postgres 18.6 on Debian trixie**, `postgres:18.6-trixie@sha256:edb0c08d…`.
  Same Debian release and glibc (2.41) as 16.15-trixie, so libc
  collations (`en_US.utf8`) don't change and text indexes stay valid.
- **`pg_upgrade --copy`**, not `--link`: the 16 data directory is left
  untouched, so a rollback is a directory rename. It needs free disk for a
  second copy of the data (not of `pg_wal`).
- **Data checksums come on in the window (decided 2026-11-02).** Production
  runs with `data_checksums=off` and `wal_log_hints=off`, so nothing
  detects a page that the disk or filesystem silently corrupted;
  `pgbackrest verify` only proves the repository's files match what was
  backed up. The new cluster is still initialised with
  `initdb --no-data-checksums` (18's initdb turns them on by default, and
  `pg_upgrade` refuses to upgrade a cluster without checksums into one
  with them). After `pg_upgrade --copy`, while the new 18 cluster is
  still cleanly shut down and has never started, the Job runs
  `pg_checksums --enable` on it. That rewrites every relation block in
  place, inside the existing downtime. See
  [Data checksums](#data-checksums) for why this ordering.
- **pgBackRest moves to 2.59.3** (still the 2.59 line, which supports 18;
  pgdg's trixie repo dropped 2.59.1, so every 0.2.0 image, the rebuilt
  pg16 one included, ships 2.59.3, which reads the existing 2.59.1 repo).
  The stanza (`ds`) follows
  the new cluster with `pgbackrest stanza-upgrade`, then a new full backup.
- The images come from `lucy/postgres-pgbackrest` (Forgejo):
  `pg18.6-pgbackrest2.59.3-tini0.19.0` (Postgres 18 + pgBackRest + tini)
  and the one-off `upgrade-pg16.15-to-pg18.6` (Postgres 18 plus the exact
  16.15 server binaries, `postgresql-16=16.15-1.pgdg13+2`, installed at
  build time). Its CI upgrades a 16 cluster with the same commands on
  every build.

## What changes

| Piece | Before | After |
| --- | --- | --- |
| Postgres image (`postgresql.pgbackrest.image.tag`, Ranma-Config `clusters/base/apps/distant-signal.yaml`) | `pg16.15-pgbackrest2.59.1-tini0.19.0@sha256:dafa2a90…` | `pg18.6-pgbackrest2.59.3-tini0.19.0@sha256:<Forgejo digest>` |
| Chart default `postgresql.image.tag` (used only with pgBackRest off) | `16.15-trixie` | `18.6-trixie` (this repo, already on `main`) |
| Nightly `pg_dump` CronJob `distant-signal-postgres-backup` (Ranma-Config `clusters/mine-bringer/apps/distant-signal.yaml`) | `postgres:16@sha256:1a6ab3f5…` | `postgres:18.6-trixie@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6`. A 16 `pg_dump` refuses an 18 server ("server version mismatch"), so this moves in the same change. |
| pgBackRest repository | stanza `ds`, archive id `16-1` | same stanza, plus db 2 / archive id `18-2` after `stanza-upgrade`. The 16 backups and WAL stay until retention expires them. |
| Data checksums (`SHOW data_checksums`) | `off` | `on` (enabled on the new 18 cluster by `pg_checksums --enable` before its first start; the 16 directory stays `off`) |
| Data directory | `/var/lib/postgresql/data/pgdata` (16) | `/var/lib/postgresql/data/pgdata` (18); the 16 one kept as `/var/lib/postgresql/data/pgdata16-old` for 7 days |
| Grafana, Ranma-Config `monitoring-dashboards.yaml` lines 692-696 | `pg_stat_bgwriter_buffers_backend_total`, `…_buffers_backend_fsync_total` | Those columns were removed in Postgres 17 (moved to `pg_stat_io`); the panel goes blank. Fix in Ranma-Config separately. |

Note that merging this repo's 18 change to `main` does **not** move
production: with pgBackRest on, the Postgres pod runs
`postgresql.pgbackrest.image`, which Ranma-Config pins. Between that merge
and the window, CI's DB tests run on 18 while production runs 16: keep new
migrations to SQL that Postgres 16 also accepts.

## Rehearsal (2026-10-26, local, no production data)

A 16.15 cluster in the production image's build
(`pg16.15-pgbackrest2.59.1-tini0.19.0`), with the chart's `-c` settings
and pgBackRest env (a posix scratch repository instead of S3, the same
cipher and async archiving), loaded with every DS migration (195), the
e2e `seed.sql`, and a 4,000,000-row filler table with a text index:
104 tables, 4,000,309 rows, 1.4 GB without `pg_wal`. Then the steps below,
with the Job's script exactly as in the manifest, run in the upgrade image
as uid 999, read-only root filesystem, no network, all capabilities
dropped.

| Step | Time |
| --- | --- |
| Drain archiving (`CHECKPOINT`, `pg_switch_wal()`, wait for `pg_stat_archiver`) | 4 s |
| Stop 16 (SIGINT) | 6 s |
| Upgrade Job, total | 92 s |
| of which: preconditions + `initdb` | 2 s |
| of which: `pg_upgrade --check` | 2 s |
| of which: `pg_upgrade --copy` (1.4 GB) | 87 s |
| Start 18 until ready | 2 s |
| `pgbackrest stanza-upgrade` | 0.2 s |
| `pgbackrest check` | 0.5 s |
| Full backup on 18 (`--start-fast`, 1.4 GB, posix repo) | 39 s |
| `ALTER EXTENSION pg_stat_statements UPDATE` (1.10 to 1.12) | 0.1 s |
| `vacuumdb --all --analyze-in-stages --missing-stats-only` | 0.7 s |
| `REINDEX INDEX CONCURRENTLY incidents_search_document` | 0.1 s |
| amcheck `bt_index_check` on every btree index | 3 s |
| `vacuumdb --all --analyze-only` | 4 s |

Results: Postgres 18.6, `data_checksums` off, the same row count in all
104 tables, every btree index passes amcheck. The node was heavily loaded
(load average 60 to 90 on 6 cores), so `--copy` ran at about 17 MB/s.

The rehearsal predates the checksums decision: it ran without the
`pg_checksums --enable` step, hence `data_checksums` off. Rehearse the
amended Job once more before the window (see Before the window).

**Production estimate.** About 11 to 13 GB to copy (PGDATA is 15 GB,
less `pg_wal`). At the rehearsal's rate that is 11 to 13 minutes; budget
20. `pg_checksums --enable` then reads and rewrites the same 11 to 13 GB
of relation files in place and fsyncs them, about the same I/O as the
copy: expect 10 to 15 minutes, budget 20. The manual full backups to S3
(Thoth) take as long as a Sunday `distant-signal-pgbackrest-full` run:
check the last one's duration
(`kubectl -n distant-signal get jobs -l app.kubernetes.io/component=pgbackrest`)
before picking the window. Expect 75 to 110 minutes with the app down
(60 to 90 without checksums).

Findings that shaped this runbook:

- **`delete_old_cluster.sh` would delete the new cluster.** pg_upgrade
  writes it with `rm -rf '/var/lib/postgresql/data/pgdata'`, the old
  cluster's path, which after the rename is the 18 cluster. The Job deletes
  the script; never run it. Delete `pgdata16-old` by name.
- **The daily check's WAL gap check reported a false gap** after
  `stanza-upgrade` ("104 of 112 segments missing"): pg_upgrade starts the
  new cluster's WAL past the old one's last segment, and the check merged
  the 16 and 18 archives. Fixed in `lucy/postgres-pgbackrest` (the check
  now uses only the current database's backups and archive id), so the
  pg18 image must be a build with that fix. With an older build,
  `DistantSignalPgBackRestCheckFailed` fires daily until the 16 backups
  expire.
- **A manual backup without `--start-fast` waits for the next scheduled
  checkpoint** (`checkpoint_timeout` is 15 min): 14 minutes in the
  rehearsal. Every manual backup below uses `--start-fast`.
- Between 18's start and `stanza-upgrade`, every archive-push fails
  (tini keeps that harmless); WAL waits in `pg_wal` and is pushed once
  the stanza is upgraded.
- pg_upgrade 18 carries planner statistics over, but not extended
  statistics: `--missing-stats-only` fills those in seconds.
- Rollback was rehearsed too (see Rollback): 16 starts on the renamed
  directory with exactly its pre-upgrade rows; the upgraded stanza refuses
  it (`ERROR: [028]: backup and archive info files exist but do not match
  the database`); a new repository path works at once.

## Before the window

Days before:

1. **Images.** `lucy/postgres-pgbackrest` 0.2.0 merged and published:
   note the digests of `pg18.6-pgbackrest2.59.3-tini0.19.0` and
   `upgrade-pg16.15-to-pg18.6` from the Containers run's job summary. The
   pg18 build must include the daily-check fix (CHANGELOG 0.2.0, "Fixed").
2. **Ranma-Config change, prepared but not merged:**
   `postgresql.pgbackrest.image.tag` to the pg18 tag and digest
   (`clusters/base/apps/distant-signal.yaml`), and the
   `distant-signal-postgres-backup` CronJob's image to
   `postgres:18.6-trixie@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6`
   (`clusters/mine-bringer/apps/distant-signal.yaml`). Its comment about
   the client image matching the server's major changes with it.
3. **Pull access.** The upgrade Job pulls from Forgejo with the
   `forgejo-distant-signal-pull` Secret, which the Postgres pod already
   uses.
4. **Rehearse the checksums step.** Run the amended Job script (with
   the `pg_checksums --enable` step) once on a local 16 cluster, as in the
   rehearsal above, and note the `pg_checksums` time and rate next to the
   copy's. Expect `data_checksums` on after the start, the same row
   counts, and a clean `pgbackrest check` and full backup. The
   `lucy/postgres-pgbackrest` upgrade image's CI still runs the old
   commands (see Follow-ups).
5. **Window.** Avoid 19:30 to 01:30 UTC (the evening rail peak and the
   schedule delivery and publish) and 03:00 to 05:30 UTC (the `pg_dump`
   at 03:00 and pgBackRest backups at 05:00). Prefer a weekday mid-morning
   or early afternoon (e.g. 10:00 UTC Tuesday to Thursday), after the
   07:00 daily check; not Sunday (full backup 05:00, verify 08:00).

On the day, before stopping anything (read-only):

```sh
ns=distant-signal
pg=distant-signal-postgres-0
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -Atc \
  "select version(); show data_checksums; show lc_collate;
   select pg_size_pretty(pg_database_size('distant_signal'));
   select extname, extversion from pg_extension order by 1;
   select count(*) from pg_prepared_xacts;"
# Expect 16.15, off, en_US.utf8, ~13 GB, plpgsql + pg_stat_statements, 0.
kubectl -n $ns exec $pg -c postgres -- sh -c \
  'du -sh --exclude=pg_wal /var/lib/postgresql/data/pgdata; du -sh /var/lib/postgresql/data/pgdata/pg_wal; df -h /var/lib/postgresql/data'
# Free space must exceed the first figure plus 2 GiB, with room left for
# the node (local-path doesn't enforce the PVC size: this is the node's disk).
# pg_checksums rewrites blocks in place and needs no extra space.
kubectl -n $ns exec $pg -c postgres -- pgbackrest --log-level-console=info check
kubectl -n $ns exec $pg -c postgres -- pgbackrest info
kubectl -n $ns get deploy,statefulset,cronjob -o wide > ds-before-pg18.txt
```

## The window

### 1. Backups (app still up)

```sh
kubectl -n $ns exec $pg -c postgres -- \
  pgbackrest --log-level-console=info --type=full --start-fast backup
kubectl -n $ns create job --from=cronjob/distant-signal-postgres-backup ds-pre-pg18-dump
kubectl -n $ns wait --for=condition=complete job/ds-pre-pg18-dump --timeout=60m
```

The dump lands where the nightly ones do (age-encrypted, Thoth), with the
16 client.

### 2. Stop the app

```sh
flux -n $ns suspend helmrelease distant-signal
kubectl -n $ns get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.replicas}{"\n"}{end}' > ds-replicas.txt
kubectl -n $ns get deploy -o name | grep -v -- '-redis$' | xargs kubectl -n $ns scale --replicas=0
kubectl -n $ns get pods
```

Redis stays up. Wait until only `distant-signal-postgres-0`, Redis and
any finished Job pods are left.

### 3. Stop the CronJobs, drain archiving, stop Postgres

```sh
for cj in $(kubectl -n $ns get cronjob -o name); do
  kubectl -n $ns patch "$cj" --type merge -p '{"spec":{"suspend":true}}'
done
kubectl -n $ns get jobs   # nothing still running
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -Atc \
  "checkpoint; select pg_walfile_name(pg_current_wal_lsn()); select pg_switch_wal();"
# Repeat until last_archived_wal >= the file name printed above:
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -Atc \
  "select last_archived_wal, last_failed_wal, failed_count from pg_stat_archiver"
kubectl -n $ns exec $pg -c postgres -- ls /var/lib/postgresql/data/pgbackrest-spool/archive/ds/out
kubectl -n $ns scale statefulset distant-signal-postgres --replicas=0
kubectl -n $ns wait --for=delete pod/$pg --timeout=300s
```

The Ranma `distant-signal-postgres-backup` CronJob doesn't set `suspend`,
so the patch survives Flux; the chart's CronJobs are held by the suspended
HelmRelease.

### 4. Run the upgrade Job

Fill in the upgrade image's digest, then `kubectl apply -f` it. The
script is the one rehearsed plus the "enable data checksums" step (rehearse
that once more before the window). It refuses to start unless the 16
cluster was shut down cleanly, has no data checksums, nothing is running
on it, no earlier attempt left `pgdata18` or `pgdata16-old`, and the
volume has room for the copy.

The checksums step runs on `pgdata18` after `pg_upgrade` and before the
directory swap. pg_upgrade leaves the new cluster cleanly shut down, which
`pg_checksums` requires, and nothing starts it until step 5. If
`pg_checksums` is interrupted, the cluster's checksum setting is left
unchanged, the swap hasn't happened, and the "fails before swap" path
below applies. The step checks that the system identifier is the same
before and after (pg_checksums only rewrites blocks and the control
file's checksum version), so the `stanza-upgrade` in step 6 records the
identifier the cluster keeps.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: distant-signal-postgres-upgrade-18
  namespace: distant-signal
  labels:
    app.kubernetes.io/name: distant-signal
    app.kubernetes.io/component: postgres-upgrade
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 604800
  template:
    metadata:
      labels:
        app.kubernetes.io/name: distant-signal
        app.kubernetes.io/component: postgres-upgrade
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      imagePullSecrets:
        - name: forgejo-distant-signal-pull
      securityContext:
        runAsNonRoot: true
        runAsUser: 999
        runAsGroup: 999
        fsGroup: 999
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: upgrade
          image: git-bringer.fox-prometheus.ts.net/lucy/postgres-pgbackrest:upgrade-pg16.15-to-pg18.6@sha256:<digest>
          command: ["bash", "-c"]
          args:
            - |
              set -euo pipefail
              step() { printf '\n== %s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
              DATA=/var/lib/postgresql/data
              OLD="${DATA}/pgdata"
              NEW="${DATA}/pgdata18"
              KEEP="${DATA}/pgdata16-old"
              cd "${DATA}"

              step "preconditions"
              test "$(cat "${OLD}/PG_VERSION")" = 16
              test ! -e "${OLD}/postmaster.pid"
              test ! -e "${NEW}"
              test ! -e "${KEEP}"
              "${PG_OLD_BINDIR}/pg_controldata" "${OLD}" >"${DATA}/pg_controldata-16.txt"
              grep -E '^(Database cluster state|Data page checksum version|Latest checkpoint location|Latest checkpoint.s REDO WAL file):' "${DATA}/pg_controldata-16.txt"
              grep -Eq '^Database cluster state: +shut down$' "${DATA}/pg_controldata-16.txt"
              grep -Eq '^Data page checksum version: +0$' "${DATA}/pg_controldata-16.txt"
              need_kb=$(du -sk --exclude=pg_wal "${OLD}" | cut -f1)
              free_kb=$(df -Pk "${DATA}" | awk 'NR == 2 { print $4 }')
              echo "old cluster without pg_wal: ${need_kb} KiB; free: ${free_kb} KiB"
              # --copy writes a second copy of every relation file; keep 2 GiB spare.
              test "${free_kb}" -gt $((need_kb + 2097152))

              step "initdb 18 (no data checksums, as the 16 cluster)"
              initdb -D "${NEW}" --username="${PGUSER}" --encoding=UTF8 --locale=en_US.utf8 --no-data-checksums
              grep -Eq "^listen_addresses = '\*'" "${NEW}/postgresql.conf"

              step "pg_upgrade --check"
              pg_upgrade --check --copy -b "${PG_OLD_BINDIR}" -B "${PG_NEW_BINDIR}" -d "${OLD}" -D "${NEW}" -U "${PGUSER}"

              step "pg_upgrade --copy"
              pg_upgrade --copy -b "${PG_OLD_BINDIR}" -B "${PG_NEW_BINDIR}" -d "${OLD}" -D "${NEW}" -U "${PGUSER}"

              step "carry pg_hba.conf and postgresql.auto.conf over"
              cp -p "${OLD}/pg_hba.conf" "${NEW}/pg_hba.conf"
              cp -p "${OLD}/postgresql.auto.conf" "${NEW}/postgresql.auto.conf"
              diff <(grep -Ev '^[[:space:]]*(#|$)' "${OLD}/postgresql.conf") <(grep -Ev '^[[:space:]]*(#|$)' "${NEW}/postgresql.conf") || true

              step "enable data checksums on the new 18 cluster (never started yet)"
              "${PG_NEW_BINDIR}/pg_controldata" "${NEW}" >"${DATA}/pg_controldata-18-before-checksums.txt"
              grep -Eq '^Database cluster state: +shut down$' "${DATA}/pg_controldata-18-before-checksums.txt"
              grep -Eq '^Data page checksum version: +0$' "${DATA}/pg_controldata-18-before-checksums.txt"
              "${PG_NEW_BINDIR}/pg_checksums" --enable --progress -D "${NEW}"
              "${PG_NEW_BINDIR}/pg_controldata" "${NEW}" >"${DATA}/pg_controldata-18.txt"
              grep -E '^(Database system identifier|Database cluster state|Data page checksum version):' "${DATA}/pg_controldata-18.txt"
              grep -Eq '^Database cluster state: +shut down$' "${DATA}/pg_controldata-18.txt"
              grep -Eq '^Data page checksum version: +1$' "${DATA}/pg_controldata-18.txt"
              # pgBackRest's stanza-upgrade (step 6) records this identifier: it must not move.
              test "$(grep '^Database system identifier:' "${DATA}/pg_controldata-18-before-checksums.txt")" = \
                "$(grep '^Database system identifier:' "${DATA}/pg_controldata-18.txt")"

              step "swap directories: ${OLD} becomes the 18 cluster, the 16 one is kept as ${KEEP}"
              mv "${OLD}" "${KEEP}"
              mv "${NEW}" "${OLD}"
              # pg_upgrade's delete_old_cluster.sh runs `rm -rf '${OLD}'`, which is now
              # the 18 cluster. Never run it: delete ${KEEP} by name, after 7 days.
              rm -f "${DATA}/delete_old_cluster.sh"
              test ! -e "${DATA}/delete_old_cluster.sh"
              cat "${OLD}/PG_VERSION" "${KEEP}/PG_VERSION"
              ls -la "${DATA}"
              du -sh "${OLD}" "${KEEP}"
              step "done"
          env:
            # The 16 cluster's bootstrap superuser (POSTGRES_USER): pg_upgrade
            # requires the same install user in the new cluster.
            - name: PGUSER
              value: distant_signal
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              memory: 2Gi
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
            - name: tmp
              mountPath: /tmp
            - name: run
              mountPath: /var/run/postgresql
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: data-distant-signal-postgres-0
        - name: tmp
          emptyDir: {}
        - name: run
          emptyDir: {}
```

```sh
kubectl -n $ns logs -f job/distant-signal-postgres-upgrade-18
kubectl -n $ns wait --for=condition=complete job/distant-signal-postgres-upgrade-18 --timeout=90m
```

The log prints the 16 cluster's control data, the free space, both
`pg_upgrade` reports, a diff of the non-comment `postgresql.conf` lines
(expected: only `autovacuum_worker_slots`, new in 18), `pg_checksums`
progress and its summary (`Checksums enabled in cluster`), the 18
cluster's control data with `Data page checksum version: 1`, and the
directory listing with `pgdata` (18) and `pgdata16-old` (16). The copy
and the checksums pass each take about 10 to 20 minutes on production. If it fails before
"swap directories", nothing was renamed: fix the cause, delete
`pgdata18` (a throwaway pod on the PVC, or `rm -rf` from a debug
container), and run it again. The PV's node affinity puts the Job on the
node that holds the data.

### 5. Start Postgres 18

With the HelmRelease still suspended, start the StatefulSet on the pg18
image by hand, so the app stays down until the checks pass:

```sh
kubectl -n $ns set image statefulset/distant-signal-postgres \
  postgres=git-bringer.fox-prometheus.ts.net/lucy/postgres-pgbackrest:pg18.6-pgbackrest2.59.3-tini0.19.0@sha256:<digest>
kubectl -n $ns scale statefulset distant-signal-postgres --replicas=1
kubectl -n $ns wait --for=condition=ready pod/$pg --timeout=600s
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -Atc \
  "select version(); show data_checksums;"
```

Expect 18.6 and `on`. If it says `off`, the Job's checksums step didn't
run (the Job asserts it, so this means an edited script). The app is
still down, so fix it now: scale the StatefulSet to 0 (a clean
shutdown), run `pg_checksums --enable --progress -D
/var/lib/postgresql/data/pgdata` as uid 999 in a pod on the PVC with the
upgrade image, and start it again. Archive-push fails until the next step (`archive
command failed` in the log; WAL waits in `pg_wal`).

### 6. pgBackRest: stanza-upgrade, check, full backup

```sh
kubectl -n $ns exec $pg -c postgres -- pgbackrest --log-level-console=info stanza-upgrade
kubectl -n $ns exec $pg -c postgres -- pgbackrest --log-level-console=info check
kubectl -n $ns exec $pg -c postgres -- \
  pgbackrest --log-level-console=info --type=full --start-fast backup
kubectl -n $ns exec $pg -c postgres -- pgbackrest info
kubectl -n $ns exec $pg -c postgres -- pgbackrest-daily-check --no-verify
```

`pgbackrest info` lists db 1 (16) and db 2 (18) and the new full backup.
The daily check must end with "no WAL gap".

With checksums on, pgBackRest turns `checksum-page` on by itself and
validates every data page's checksum as it backs it up, so this full
backup is the first whole-cluster checksum check. A bad page doesn't fail
the backup: pgBackRest logs a warning (`invalid page checksum`, with the
file and block) and records the page in the backup manifest. Read the
backup's console output for such warnings; there should be none. Every
later backup, full or incremental, repeats the check on the files it
copies.

### 7. Post-upgrade steps, then the app

```sh
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -v ON_ERROR_STOP=1 -c \
  "ALTER EXTENSION pg_stat_statements UPDATE;"
kubectl -n $ns exec $pg -c postgres -- vacuumdb -U distant_signal --all --analyze-in-stages --missing-stats-only
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -v ON_ERROR_STOP=1 -c \
  "REINDEX INDEX CONCURRENTLY incidents_search_document;"
```

The reindex is insurance: the libc collation is the same (same Debian
release and glibc), but it is the one index built over free text through
`to_tsvector`, and it is quick.

Then make the change durable and bring the app back:

1. Merge the prepared Ranma-Config change (pg18 pgBackRest image,
   `postgres:18.6-trixie` `pg_dump` CronJob); approve its deploy with the
   passkey. The StatefulSet already runs that image, so the HelmRelease's
   upgrade doesn't restart Postgres.
2. `flux -n $ns resume helmrelease distant-signal`. Helm restores the
   Deployments' replicas; check against `ds-replicas.txt` and
   `kubectl -n $ns scale` any that didn't come back.
3. Resume the CronJobs:
   `for cj in $(kubectl -n $ns get cronjob -o name); do kubectl -n $ns patch "$cj" --type merge -p '{"spec":{"suspend":false}}'; done`
4. Once the app is up: `kubectl -n $ns exec $pg -c postgres -- vacuumdb -U distant_signal --all --analyze-only`
   (refreshes all planner statistics, as pg_upgrade recommends; a few
   minutes on production, harmless with traffic).

Don't resume the HelmRelease before step 1 lands: it would roll the
StatefulSet back to the 16 image, which can't open the 18 directory
(`database files are incompatible with server`) and crash-loops until
fixed. Nothing is lost, but the app stays down.

### 8. Afterwards

- Same day: the api, writers and pollers log no database errors; the
  status page and a train lookup work; `pg_stat_archiver.failed_count`
  stops rising; the next 03:00 `pg_dump` succeeds with the 18 client;
  `pg_stat_database.checksum_failures` stays 0 (see Verification). WAL
  volume is somewhat higher than before: with checksums on, the first
  hint-bit change to a page after each checkpoint writes a full-page image,
  as `wal_log_hints=on` would. Watch `pg_wal` and the archive rate for the
  first day.
- Next morning: the 07:00 `distant-signal-pgbackrest-check` passes.
- After 7 days, delete the 16 directory by name (never with
  `delete_old_cluster.sh`, which the Job removed):
  `kubectl -n $ns exec $pg -c postgres -- rm -rf /var/lib/postgresql/data/pgdata16-old`.
  From then on the 16 rollback is a restore (see below).
- Delete the Job: `kubectl -n $ns delete job distant-signal-postgres-upgrade-18`
  (its TTL does it after 7 days anyway).

## Verification

```sh
kubectl -n $ns exec $pg -c postgres -- psql -U distant_signal -d distant_signal -Atc "
  select version();
  show data_checksums;                                               -- on
  select extname, extversion from pg_extension order by 1;           -- pg_stat_statements 1.12
  select count(*) from pg_index where not indisvalid;                -- 0
  select last_archived_wal, failed_count from pg_stat_archiver;
  select coalesce(datname, '<shared>'), checksum_failures, checksum_last_failure
    from pg_stat_database;                                           -- all 0, no timestamps"
```

`checksum_failures` counts pages that failed verification when a backend
or a backup read them, per database (the `<shared>` row is the shared
catalogs). It is only non-null with checksums on. Recent postgres_exporter
releases export it through the `stat_database` collector (on by default)
as `pg_stat_database_checksum_failures`; confirm Ranma-Config's exporter
shows the series once 18 is up. Nothing alerts on it yet (see
Follow-ups).

Row counts: before step 3, save `select relname, n_live_tup from
pg_stat_user_tables order by 1` (an estimate, fine for a sanity check),
or exact `count(*)` of the tables you care about, and compare after step 5.
The rehearsal compared exact counts of all 104 tables, and checked every
btree index with amcheck (`CREATE EXTENSION amcheck` then `bt_index_check`
per index; drop the extension afterwards); on production that reads every
index once, so run it only if something looks wrong.

## Rollback

Enabling checksums doesn't change any of this. `--copy` leaves the 16
directory untouched, and `pg_checksums` runs only on the new 18 directory,
so `pgdata16-old` is the 16 cluster exactly as it stopped, checksums still
off. 16 starts on it as before; nothing has to be turned back off.

**Before 18 has started** (the Job failed, or you stop after step 4): the
16 directory is untouched (`--copy`). If the Job got as far as the swap,
rename back: `pgdata` to `pgdata18-failed`, `pgdata16-old` to `pgdata`
(a pod on the PVC as uid 999, or a debug container). Start the
StatefulSet on the 16 image (still in the suspended HelmRelease's values),
resume the HelmRelease, CronJobs and Deployments. The stanza was never
upgraded, so archiving and backups carry on.

**After 18 has started**: everything written on 18 is lost by going back.
Rename as above and start 16. The stanza now belongs to the 18 cluster:
16's archive-push and `check` fail with `ERROR: [028]: backup and archive
info files exist but do not match the database`. Point 16 at a new
repository path before starting it (Ranma-Config ConfigMap,
`postgresql.pgbackrest.repo.path`, e.g. `…/distant-signal-rollback`), then
`pgbackrest stanza-create` and a full backup. Rehearsed: works at once.
The old path keeps both 16 and 18 backups until retention expires them.

**Last resort**: a restore. The pre-window full backup is in the old
stanza as db 1 (`pgbackrest restore --set=<label>` into an empty 16 data
directory, with the 16 image; docs/postgres-pitr.md, "Point-in-time
restore"), or the step 1 `pg_dump` into a fresh 16 cluster. After 7 days,
once `pgdata16-old` is deleted, these are the only way back to 16.
A restore of the db 1 backup gives a 16 cluster without checksums, as
before the window.

## Data checksums

Why enable them here, and why on the new 18 cluster rather than on 16
first.

**What they catch.** With checksums on, Postgres writes a checksum into
every data page and checks it on every read from disk; a mismatch is an
error (`invalid page in block`) and bumps
`pg_stat_database.checksum_failures`. Without them a page the storage
mangled is read as if valid. pgBackRest also validates page checksums on
every backup once the cluster has them. `pgbackrest verify` doesn't cover
this: it checks that repository files match what was backed up, which
includes a corrupt page faithfully copied.

**`wal_log_hints`.** It stays `off` and becomes moot: with checksums on,
Postgres WAL-logs hint-bit changes (a full-page image on the first change
after a checkpoint) whatever `wal_log_hints` says, because a torn
hint-bit write would otherwise break the page's checksum. That is also
the small extra WAL volume to expect afterwards. Leave the setting alone.

**Ordering: chosen, on the new 18 cluster after `pg_upgrade`.** The
alternative was to run `pg_checksums --enable` on the stopped 16 cluster
first, then initdb 18 with its default (checksums on) and drop
`--no-data-checksums`. Rejected:

- Rollback. It rewrites the 16 directory in place, so the cluster a
  rollback starts is no longer the one that ran production that morning,
  and an interrupted run leaves 16 itself half-rewritten (pg_checksums
  can be re-run, but that is fixing the rollback target in the middle of
  an outage). Enabling on 18 never touches 16: `pgdata16-old` stays
  exactly as stopped.
- Downtime. The same 11 to 13 GB is rewritten either way, so neither
  order is shorter. Doing it on 16 in an earlier, separate window would
  take a second outage, and 16 would then run with checksums (and their
  WAL cost) on the image being retired.
- Simplicity. Both orders need `initdb` to match: with the chosen one the
  rehearsed `initdb --no-data-checksums` and `pg_upgrade` steps don't
  change, and the new step sits after them, before the swap, where a
  failure is the existing "delete `pgdata18` and run again" case.

**Disk.** `pg_checksums` rewrites blocks in place: no headroom beyond what
`--copy` already needs (the Job's free-space check). WAL afterwards grows
somewhat (above), well inside the `pg_wal` room the volume already has.

**pgBackRest.** The stanza `ds` and `stanza-upgrade` are unchanged.
`pg_checksums` doesn't change the cluster's system identifier (the Job
compares it before and after), and it runs before the 18 cluster first
starts and before `stanza-upgrade`, so the stanza's db 2 records the
cluster as it will run. The post-upgrade full backup (step 6) is the first
to validate page checksums (`checksum-page`, on automatically). A failure
there is a warning in the backup log and the manifest, not a failed
backup, which is why the alert below matters.

## Follow-ups

- **Alert on checksum failures** (not implemented; nothing in
  `charts/distant-signal` or `docs/alerts.md` watches checksums today). Add
  `DistantSignalPostgresChecksumFailures` to the chart's PrometheusRule:
  `increase(pg_stat_database_checksum_failures{namespace="<release namespace>"}[1h]) > 0`,
  critical, with a `docs/alerts.md` entry (CI requires one per alert):
  which database, `checksum_last_failure`, the Postgres log's
  `invalid page in block` lines for the relation, then restore the page
  from backup or reindex/rebuild the relation. Check the exporter's
  `/metrics` for the exact series name first. pgBackRest's
  `invalid page checksum` backup warnings could feed the same alert
  through the backup Job's logs or the daily check.
- **Upgrade image CI.** `lucy/postgres-pgbackrest`'s upgrade-image CI runs
  the old commands; add the `pg_checksums --enable` step and a
  `SHOW data_checksums` = `on` check after starting 18.
- **Grafana.** Add `pg_stat_database_checksum_failures` to the Postgres
  dashboard (Ranma-Config `monitoring-dashboards.yaml`), next to the
  `pg_stat_bgwriter` panel fix above.

## Local development

`docker-compose.yml` runs Postgres 18 on a new volume, `postgres18_data`,
mounted at `/var/lib/postgresql` (18's PGDATA layout). Developers start
with an empty local database (the api migrates on start) and can remove
their old `postgres_data` volume; see the comment above that service.
