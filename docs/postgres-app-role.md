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
| Migrations, `_sqlx_migrations` | `ds_store::migrate` (api startup, `ds-migrate run`) | n/a | The owner connection (`MIGRATION_DATABASE_URL`). |
| Heal of INVALID indexes: `DROP INDEX CONCURRENTLY`, reading `pg_stat_progress_create_index` | `ds_store::migrate` (api startup, `ds-migrate run`) | n/a | Owner connection. The owner owns the indexes; `pg_read_all_stats` lets it see another role's in-flight build, which would otherwise show NULL `relid`s and look abandoned. |
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

## Stage 0b: one role per service (ingest architecture phase 0b)

Status: **implemented, off by default** (`postgresql.roles.perService`;
spec `docs/superpowers/specs/2026-10-06-ingest-architecture-design.md`
§6, plan phase 0b). Comes after Stages A–C above.

### What it is

Every DB service still has exactly the app role's rights, but connects as
its own role, so `pg_stat_statements` and `pg_stat_activity` say which
service ran what. That evidence (7 days of it) is what each later phase
narrows the roles from.

| Role | Used by | Privileges (Stage 0b) | Connection limit (default) |
|---|---|---|---|
| `distant_signal_api` | api pools | member of `distant_signal_app`; `read_shared`, `schema_gate` | (api.replicaCount + 1) × 16 + 2 = 34 |
| `distant_signal_aggregator` | aggregator (and its archive pool) | member of `app`; `read_shared`, `schema_gate` | 10 (+ 2 archive) + 1 = 11 |
| `distant_signal_enricher` | enricher | member of `app`; `schema_gate` | 5 + 1 = 6 |
| `distant_signal_notifier` | notifier | member of `app`; `read_shared`, `schema_gate` | 5 + 1 = 6 |
| `distant_signal_read_shared` (NOLOGIN) | group | `SELECT` on every shared-train, ingest, derived and reference table | – |
| `distant_signal_schema_gate` (NOLOGIN) | group | `SELECT` on `_sqlx_migrations` (the phase 1B schema gate) | – |

- **The source of truth is `charts/distant-signal/files/db-grants.yaml`.**
  It classifies every table, sequence, function and (later) view in
  `public`, with each role's target grants. `scripts/gen-db-grants.py
  render` turns it into `files/postgres-grants.sql`; CI fails when a
  migration adds an object the YAML does not classify, or when the SQL is
  stale.
- **The setup Job runs `postgres-grants.sql` after `postgres-roles.sql`**
  (and so does the initdb script on a new cluster). It is idempotent:
  roles, limits, passwords, memberships and grants are re-applied on every
  upgrade, and a grant removed from the YAML is revoked.
- **The app role's computed limit shrinks** by the pool of each service
  that connects as its own role (all four on: 5, its slack). The render
  fails when the limits of every role in use (owner, app, exporter, dump,
  backup and each connected service role) exceed `max_connections` − 3.
- **The api's pool drops from 50 to 16** while it connects as its own role
  (`perService.api.maxConnections`). Production's peak is 6 (2026-10-06).
  Its migrations still connect as the owner.

### pg_stat_statements

Checked in production (read-only, 2026-10-06): the extension is installed
(1.10), `shared_preload_libraries = pg_stat_statements` (chart
`postgresql.config`), `pg_stat_statements.max` 5000, `track` top,
`track_utility` on, `save` on, `dealloc` 0 since the 2026-10-03 reset, 706
statements, all under `distant_signal`. **Nothing has to be enabled.**
Ranma, for the observation window:

- After the last service has moved, reset the counters so the 7 days are
  clean: `SELECT pg_stat_statements_reset();` (as `distant_signal`).
- Watch `SELECT dealloc FROM pg_stat_statements_info;`. Each statement is
  now tracked once per role (about 4 × 700 entries); if `dealloc` rises,
  raise `postgresql.config."pg_stat_statements.max"` to 10000 (a Postgres
  restart) and reset again.
- `track: top` hides statements inside functions (`analyze_publish_keys`'s
  `ANALYZE`); that function's grants are in the YAML explicitly.

### Rollout runbook (Ranma)

Prerequisites: Stage B is done (every service connects as
`distant_signal_app`), and the setup Job is on.

**1. The passwords.** Add four keys to the roles Secret (or a Secret per
service, Q12's default; letters and digits only):
`postgres-api-password`, `postgres-aggregator-password`,
`postgres-enricher-password`, `postgres-notifier-password`. (From plan task
1B.9 the setup Job also creates `distant_signal_writer`, the ingest-writer's
role, unused until `perService.writer.connect`; without
`perService.writer.existingSecret` its password,
`postgres-writer-password`, is generated into the chart's Secret.) Then:

```yaml
postgresql:
  roles:
    perService:
      enabled: true
      api: {existingSecret: distant-signal-postgres-roles}
      aggregator: {existingSecret: distant-signal-postgres-roles}
      enricher: {existingSecret: distant-signal-postgres-roles}
      notifier: {existingSecret: distant-signal-postgres-roles}
```

Reconcile. Only the setup Job changes; no Deployment restarts. Verify:

```sql
SELECT r.rolname, r.rolcanlogin, r.rolconnlimit,
       pg_has_role(r.rolname, 'distant_signal_app', 'MEMBER') AS in_app
FROM pg_roles r WHERE r.rolname LIKE 'distant\_signal\_%' ORDER BY 1;
--   api 34, aggregator 11, enricher 6, notifier 6, all in_app;
--   read_shared and schema_gate with rolcanlogin false
```

**2. Move one service per release**, lowest risk first: enricher, then
notifier, aggregator, api:

```yaml
postgresql:
  roles:
    perService:
      enricher: {connect: true, existingSecret: distant-signal-postgres-roles}
```

Only that Deployment restarts (and the setup Job re-runs, lowering the app
role's limit). Verify, then wait a day before the next:

```sql
SELECT usename, application_name, count(*) FROM pg_stat_activity
WHERE datname = 'distant_signal' GROUP BY 1, 2 ORDER BY 1, 2;
--   distant-signal-enricher as distant_signal_enricher, nothing else moved
```

and no `permission denied` (42501) or `too many connections for role` in
the service's logs. For the api, also watch its request latency and
`acquire` timeouts: its pool is now 16.

**3. Observe.** Reset `pg_stat_statements` (above), wait 7 days, then
produce the report (agents may run this; it is read-only):

```sh
kubectl -n distant-signal exec -i distant-signal-postgres-0 -- \
  psql -U distant_signal -d distant_signal -X -q -A -t -z -0 \
    -c "SET default_transaction_read_only = on" \
    -c "$(uv run scripts/observe-role-usage.py --print-query)" > statements.bin
uv run scripts/observe-role-usage.py --from-file statements.bin --output role-usage.md
```

Attach `role-usage.md` to the plan's tracking issue. Each "used, not
granted" row is fixed in `db-grants.yaml` (or explained) before that role
is narrowed in phases 2–5.

### Rollback

- **One service:** `perService.<service>.connect: false`. It goes back to
  the app role. The app role's limit grows back by that pool, but only when
  the post-upgrade setup Job runs, after the pod restarted, so for that
  window the service has the app role's old (smaller) limit. With every
  other service on its own role that is still the slack plus any pools
  still on app, enough to start. To avoid even that, set
  `postgresql.roles.app.connectionLimit` to the full value one release
  earlier.
- **All of it:** `perService.enabled: false` after every `connect` is off.
  The roles stay (harmless: members of app, nothing uses them); drop them
  by hand if wanted: `DROP ROLE distant_signal_api, ...` after
  `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM distant_signal_read_shared,
  distant_signal_schema_gate; DROP ROLE distant_signal_read_shared,
  distant_signal_schema_gate;`.

### How it was tested

- `scripts/tests/test_gen_db_grants.py`: classification gaps, stale SQL,
  limits over the budget, narrow rendering.
- `scripts/test-postgres-roles.py --mode per-service` (CI's rust-db-test job):
  the api, aggregator, notifier and enricher DB suites, each as its own
  role, on a database set up exactly as the chart does it.
- A narrowed role on a migrated database (by hand, 2026-10-06): exactly its
  YAML grants, column grants, its sequences and functions, no app
  membership; running the SQL twice is a no-op.
- `scripts/check-ingest-phase0-chart.py` (CI's scripts-lint job): every
  switch, the budget at 98 (fails) and 92 (passes), and with `--baseline`
  the default render unchanged.

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

- `scripts/test-postgres-roles.py` (CI's `rust-db-test` job runs it): a fresh
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
