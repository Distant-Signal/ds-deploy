# Postgres roles: the app no longer connects as a superuser

Status: **implemented, off by default** (`postgresql.roles` in the chart;
Train Register R-048 follow-up, 2026-10-01). This page is the design and
the rollout runbook.

## Why

The bundled Postgres had one role, `distant_signal`
(`postgresql.auth.username`): the official image's bootstrap superuser.
Every client connected as it: the api (pools and migrations), every worker,
pgBackRest, Ranma-Config's postgres-exporter and `pg_dump`, and admins.
That cost two things:

1. **No protected admin reserve.** `superuser_reserved_connections` (3)
   only keeps slots free from *non*-superusers, so a pool leak could take
   all 100 slots and lock the operator out too.
2. **Blast radius.** A SQL injection or a compromised service had
   superuser: `COPY ... TO PROGRAM` (a shell in the Postgres pod), reading
   server files, `ALTER SYSTEM`, dropping the database.

## The roles

`distant_signal` stays the bootstrap superuser, **for humans only**
(`kubectl exec ... psql`). Five new roles, all `LOGIN NOSUPERUSER
NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` with a `CONNECTION
LIMIT`:

| Role (default name) | Used by | Privileges | Connection limit (default) |
|---|---|---|---|
| `distant_signal_owner` | api's migrations only (`MIGRATION_DATABASE_URL`) | Owns schema `public` and every table, sequence, view and function in it; `pg_read_all_stats` | `api.replicaCount` + 2 (3) |
| `distant_signal_app` | api pools, aggregator, enricher, notifier (`DATABASE_URL`) | `CONNECT`; `USAGE` on `public`; `SELECT, INSERT, UPDATE, DELETE` on every table; `USAGE, SELECT, UPDATE` on every sequence; `EXECUTE` on the owner's functions; `SELECT` only on `_sqlx_migrations`; the same via `ALTER DEFAULT PRIVILEGES FOR ROLE distant_signal_owner` | the chart's pools + 5 (75) |
| `distant_signal_exporter` | Ranma-Config's postgres-exporter | `pg_monitor` | 3 |
| `distant_signal_dump` | Ranma-Config's nightly `pg_dump` | `pg_read_all_data` | 2 |
| `distant_signal_backup` | the pgBackRest CronJobs | `EXECUTE` on `pg_backup_start(text, boolean)`, `pg_backup_stop(boolean)`, `pg_switch_wal()`, `pg_create_restore_point(text)` (in `postgres` and `distant_signal`); `pg_read_all_settings`; `pg_checkpoint` | 4 |

Not granted to the app role, because no service uses them: `TRUNCATE`,
`REFERENCES`, `TRIGGER`, `CREATE` anywhere. It keeps `TEMP` (PUBLIC's
default) and `CONNECT`.

The app role's limit is what makes the reserve real: with defaults, the
five roles can hold at most 75 + 3 + 3 + 2 + 4 = 87 of the 100 slots, and
the last 3 are `superuser_reserved_connections`, so an admin can always get
in.

Every role, the backup role included, has a password (defence in depth).
pgBackRest itself never sends it: it runs inside the Postgres container and
connects over the local socket, which the image trusts, so the CronJobs set
no `PGPASSWORD` (that would put the password on the `kubectl exec` command
line). pgBackRest 2.59
needs exactly the grants above as a non-superuser (pgBackRest's and EDB's
non-superuser notes): `pg_read_all_settings` so it can read
`data_directory` and the archive settings, the four functions, and
`pg_checkpoint` for `start-fast`. Checked locally against Postgres 16.15
with pgBackRest 2.59.1: `stanza-create`, `check`, a full backup with
`start-fast=y`, a diff backup and `verify` all succeed as the backup role,
and fail as the app role ("unable to find primary cluster").

Everything is set up by one idempotent script,
`charts/distant-signal/files/postgres-roles.sql`, run as the superuser.
It refuses to touch an existing superuser and fails if anything in
`public` is still owned by `distant_signal` when it is done.

### Why not `REASSIGN OWNED BY distant_signal`

`distant_signal` is the image's *bootstrap* superuser (oid 10), and
Postgres refuses: "cannot reassign ownership of objects owned by role
distant_signal because they are required by the database system". The
script instead runs `ALTER ... OWNER TO distant_signal_owner` on schema
`public` and on each relation, function and type in it that
`distant_signal` owns, skipping extension members (`pg_stat_statements`'s
view and functions stay with the superuser) and objects that follow their
parent (indexes, TOAST tables, a column's sequence, array types).

## Runtime operations that needed a decision

Searched in `crates/*/src` (2026-10-01) for everything a DML-only role
cannot do:

| Operation | Where | As the app role | Handling |
|---|---|---|---|
| `ANALYZE <staging table>` | schedule publish, final chunk (`finish_publish_part`) | **Silently skipped.** Postgres 16 has no `MAINTAIN` privilege (it came in 17), so only the owner may ANALYZE; a non-owner gets "WARNING: permission denied to analyze ..., skipping it" and the statement *succeeds*. The 2026-09-27 nested-loop delete would come back. | Now `SELECT analyze_publish_keys('<table>')`, a `SECURITY DEFINER` function owned by the owner role (migration `20261001140000`). It analyzes only the two staging tables, with a fixed `search_path`; `EXECUTE` is revoked from PUBLIC and granted to the app role. A DB test checks `pg_class.reltuples` really moves for the connecting role. |
| `CREATE UNLOGGED TABLE` | none at runtime | n/a | The staging tables are permanent `UNLOGGED` tables created by migration `20260926183000`; the services only do DML on them. |
| `TRUNCATE` | none | n/a | Not granted. |
| Advisory locks (`pg_advisory_xact_lock`, `pg_try_advisory_xact_lock`) | publish, corpus load, subscriptions | Allowed for any role | None. |
| `SET LOCAL statement_timeout`, `idle_in_transaction_session_timeout`, `enable_*`; the startup options `client_connection_check_interval`, `tcp_keepalives_*` | `common::pg`, publish, aggregator | All user-settable | None. |
| `pg_stat_statements` | no runtime reads | n/a | The migration `20260927070000` `CREATE EXTENSION` needs a superuser; as the owner it only warns. The setup script creates the extension, so a new cluster has it. Read it as the exporter role (`pg_monitor` sees every query). |
| Migrations, `_sqlx_migrations` | `api::migrate` | n/a | The owner connection (`MIGRATION_DATABASE_URL`). |
| Heal of INVALID indexes: `DROP INDEX CONCURRENTLY`, reading `pg_stat_progress_create_index` | `api::migrate` | n/a | Owner connection. The owner owns the indexes; `pg_read_all_stats` lets it see another role's in-flight build, which would otherwise show NULL `relid`s and look abandoned. |
| `_sqlx_migrations` read before migrating (`ensure_ready_for_contract_migration`) | api pool | `SELECT` granted | None. |

Nothing at runtime goes through the owner connection except the
migrations: the api opens it once at startup and closes it.

## Rollout runbook (Ranma)

Production: Flux `HelmRelease` `distant-signal` in namespace
`distant-signal`, release name `distant-signal`. Flux's helm-controller runs
Helm hooks, which the setup Job is.

### 0. Prerequisites

- The deployed release includes migration `20261001140000`:
  ```sql
  SELECT version FROM _sqlx_migrations WHERE version = 20261001140000;
  ```
  (one row). Deploy the release with `postgresql.roles` still off first if
  not.
- Nobody is mid-way through a manual schema change.

### 1. Create the role passwords Secret

One Secret, e.g. `distant-signal-postgres-roles` in `distant-signal`, sealed
in Ranma-Config like the existing Postgres password. Keys:

| Key | Role |
|---|---|
| `postgres-owner-password` | `distant_signal_owner` |
| `postgres-app-password` | `distant_signal_app` |
| `postgres-exporter-password` | `distant_signal_exporter` |
| `postgres-dump-password` | `distant_signal_dump` |
| `postgres-backup-password` | `distant_signal_backup` |

Use letters and digits only (e.g. 32 random alphanumerics): the owner and
app passwords are spliced into a URL and the chart cannot percent-encode a
value it never sees.

### 2. Stage A: create the roles and move ownership (no service changes)

Values:

```yaml
postgresql:
  roles:
    enabled: false          # still off
    initScript: false       # existing data directory: it would never run, and
                            # turning it on restarts Postgres
    setupJob:
      enabled: true
    owner:
      existingSecret: distant-signal-postgres-roles
    app:
      existingSecret: distant-signal-postgres-roles
    exporter:
      existingSecret: distant-signal-postgres-roles
    dump:
      existingSecret: distant-signal-postgres-roles
    backup:
      existingSecret: distant-signal-postgres-roles
```

Reconcile. The post-upgrade hook Job `distant-signal-postgres-roles-setup`
runs `postgres-roles.sql` as `distant_signal` over the Service. Nothing
restarts. The services still connect as the superuser, which ignores
ownership, so nothing they do changes.

Verify (as `distant_signal` in `psql`):

```sql
-- The five roles, none a superuser, with their limits.
SELECT rolname, rolsuper, rolconnlimit, rolpassword IS NOT NULL AS has_password
FROM pg_authid WHERE rolname LIKE 'distant\_signal\_%' ORDER BY 1;
--   expect rolsuper false and has_password true for all five
-- Nothing in public is left with the superuser except pg_stat_statements.
SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = 'distant_signal';
--   expect only pg_stat_statements and pg_stat_statements_info (views)
SELECT nspowner::regrole FROM pg_namespace WHERE nspname = 'public';
--   expect distant_signal_owner
\ddp
--   expect the owner's default privileges for distant_signal_app
```

`kubectl -n distant-signal logs job/distant-signal-postgres-roles-setup`
shows any error; a failed Job fails the upgrade, and re-running it is safe.

### 3. Stage B: switch the services

```yaml
postgresql:
  roles:
    enabled: true
```

Reconcile. api, aggregator, enricher and notifier restart with
`DATABASE_URL` as `distant_signal_app`; api's `MIGRATION_DATABASE_URL` is
`distant_signal_owner`; the pgBackRest CronJobs run pgBackRest as
`distant_signal_backup`. **Postgres does not restart** (with
`initScript: false` its pod is unchanged; CI checks this). api is
`Recreate`, so expect its usual brief restart.

Verify:

```sql
SELECT usename, application_name, count(*) FROM pg_stat_activity
WHERE datname = 'distant_signal' GROUP BY 1, 2 ORDER BY 1, 2;
--   every distant-signal-* application as distant_signal_app;
--   distant-signal-api-migrations (only during an api start) as distant_signal_owner
```

- api logs: migrations finished, no `permission denied`.
- `kubectl -n distant-signal create job --from=cronjob/distant-signal-pgbackrest-check pgbackrest-check-roles`
  succeeds (needs `postgresql.pgbackrest.enabled`); then the next full/diff
  backups.
- After the next schedule publish, the staging tables were analyzed:
  ```sql
  SELECT relname, last_analyze FROM pg_stat_user_tables
  WHERE relname LIKE '%publish_keys';
  ```
- Grep every service's logs for `permission denied` (SQLSTATE 42501) for a
  day.

### 4. Stage C: the out-of-chart clients (Ranma-Config)

- postgres-exporter: connect as `distant_signal_exporter` with
  `postgres-exporter-password`.
- `pg_dump` CronJob: connect as `distant_signal_dump` with
  `postgres-dump-password`. Its `CONNECTION LIMIT` is 2; a parallel
  `pg_dump -j N` needs `postgresql.roles.dump.connectionLimit: N + 1`.

Verify: the exporter's `pg_up` is 1 and its metrics are back; trigger one
dump by hand and check its size against last night's.

### 5. Afterwards

- `postgresql.connectionBudget.adminSessions` can drop to 0 if the slots
  are needed: the 3 `superuser_reserved_connections` now really are kept
  for admins. Leaving it is harmless.
- Keep `setupJob.enabled: true`. On every upgrade it re-applies the limits
  (e.g. after raising `api.database.maxConnections`, the app role's limit
  follows) and moves to the owner anything a superuser created meanwhile.
- Admins doing DDL by hand should `SET ROLE distant_signal_owner;` first,
  so the new objects get the owner and the app role's default privileges.
  A table created as `distant_signal` is invisible to the services
  (`permission denied`) until the next setup Job run.

### Rollback

- **Stage C**: point the exporter and `pg_dump` back at `distant_signal`.
- **Stage B**: `postgresql.roles.enabled: false` and reconcile. The
  services and CronJobs go back to the superuser, which ignores ownership.
  Nothing else is needed; the roles and ownership can stay.
- **Stage A** (only if the roles must go): `setupJob.enabled: false`, then
  as `distant_signal`:
  ```sql
  -- in distant_signal
  REASSIGN OWNED BY distant_signal_owner TO distant_signal;  -- allowed: owner is not the bootstrap role
  ALTER SCHEMA public OWNER TO pg_database_owner;
  DROP OWNED BY distant_signal_app, distant_signal_exporter, distant_signal_dump,
                distant_signal_backup, distant_signal_owner;
  \c postgres
  DROP OWNED BY distant_signal_backup;
  DROP ROLE distant_signal_app, distant_signal_exporter, distant_signal_dump,
            distant_signal_backup, distant_signal_owner;
  ```

## A new cluster

`postgresql.roles.enabled: true` with `initScript: true` (the default) and
`setupJob.enabled: true`. On an empty data directory the image's initdb
hook runs `/docker-entrypoint-initdb.d/10-distant-signal-roles.sh` before
any client connects: the roles exist, the owner owns `public`, and its
default privileges cover every table the first migration run creates. The
setup Job then runs after each install/upgrade (it waits for Postgres), and
takes back the app role's write access to `_sqlx_migrations`, which the
default privileges granted when the migrator created it.

## How it was tested

- `scripts/test-postgres-roles.py` (CI's `rust-test` job runs it): a fresh
  database, the roles from the chart's own script, then the DB suites with
  `DATABASE_URL` as the app role and `MIGRATION_DATABASE_URL` as the owner.
  `--mode existing` migrates as the superuser and converts (this runbook);
  `--mode new` creates the roles first and migrates as the owner. The api,
  aggregator, notifier, `common::pg` and enricher DB suites pass as the app
  role. Only the `#[sqlx::test]` modules are skipped: each test creates its
  own database, which needs `CREATEDB` (a test-harness right, not a runtime
  one).
- `migrate::tests::the_app_role_has_dml_only_and_the_owner_owns_the_schema`
  proves such a run really is split: the app role is not a superuser and
  gets `permission denied` for `CREATE TABLE`, `CREATE UNLOGGED TABLE`,
  `ALTER TABLE`, `TRUNCATE`, `DROP INDEX`, `CREATE INDEX` and writing
  `_sqlx_migrations`; the owner owns `public` and every table in it.
- By hand on a copy of production's setup (Postgres 16.15 with
  `distant_signal` as the bootstrap superuser, as the image makes it):
  migrate as the superuser, run the setup twice (idempotent), then the full
  suites as the app/owner roles; `pg_dump -Fc` as the dump role; the
  exporter role reads `pg_stat_statements`, `pg_stat_activity` and
  `pg_settings`; pgBackRest as above. And the rendered chart's own initdb
  script and setup Job command on a fresh cluster, then migrations as the
  owner and the migrator/publish tests as the app role.
- `helm template (postgres roles)` in CI checks the wiring, and that the
  default render is unchanged.
