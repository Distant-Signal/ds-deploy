# Postgres roles: recommendation to stop running the app as a superuser

Status: **recommendation, not implemented** (Train Register R-048, 2026-10-01).

## The problem

The bundled Postgres has exactly one role, `distant_signal`
(`postgresql.auth.username`). The official image makes `POSTGRES_USER` its
bootstrap superuser, and every client connects as it:

- the api (pools and its migration connection) and every worker;
- pgBackRest (`PGBACKREST_PG1_USER`, see `templates/_pgbackrest.tpl`);
- Ranma-Config's postgres-exporter and nightly `pg_dump`;
- admin `kubectl exec ... psql` sessions.

That has two costs:

1. **No protected admin reserve.** `superuser_reserved_connections` (3)
   only keeps slots free from *non*-superusers. Every client is a
   superuser, so a pool leak or a connection storm can take all 100 slots
   and lock the operator out too. The chart's budget check
   (`templates/api-deployment.yaml`, `postgresql.connectionBudget`) now
   counts the out-of-chart clients and admin sessions, but it is a
   render-time estimate, not an enforced limit.
2. **Blast radius.** A SQL injection or a compromised service gets
   superuser: `COPY ... TO PROGRAM` (shell in the Postgres pod), reading
   any file the server can, `ALTER SYSTEM`, dropping the database.

No runtime code needs superuser. A search on 2026-10-01 found no
`pg_terminate_backend`, `ALTER SYSTEM`, `session_replication_role`,
`COPY ... PROGRAM` or `SET ROLE` in `crates/*/src`. The only migration that
needed superuser is `20260927070000_pg_stat_statements.sql`
(`CREATE EXTENSION pg_stat_statements`, not a trusted extension), and it is
already applied everywhere. `api::migrate::heal_invalid_indexes` only drops
indexes, which the table owner may do.

## Recommendation

Split the role, in this order. Each step can ship on its own.

1. **Owner role for migrations.** Create `distant_signal_owner`
   (`LOGIN NOSUPERUSER NOCREATEROLE`), and in one transaction run
   `REASSIGN OWNED BY distant_signal TO distant_signal_owner` in the
   `distant_signal` database. Make it own schema `public`. The api's
   migration connection (`api::migrate`) connects as this role through a
   new `MIGRATION_DATABASE_URL`. Moving migrations to a pre-install Job is
   optional, but it would keep the owner credentials out of the serving pod.
2. **App role for the services.** Create `distant_signal_app`
   (`LOGIN NOSUPERUSER`), then grant it what it needs:
   ```sql
   GRANT CONNECT ON DATABASE distant_signal TO distant_signal_app;
   GRANT USAGE ON SCHEMA public TO distant_signal_app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO distant_signal_app;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO distant_signal_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE distant_signal_owner IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO distant_signal_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE distant_signal_owner IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO distant_signal_app;
   ```
   Point the api pools and every worker's `DATABASE_URL` at it. Check any
   `TRUNCATE` in the services first: it needs its own `TRUNCATE` grant.
   Add `ALTER ROLE distant_signal_app CONNECTION LIMIT <pools + slack>` so
   the app can never take the admin slots.
3. **Dedicated roles for the other clients.**
   - postgres-exporter: `LOGIN` + `GRANT pg_monitor`.
   - `pg_dump`: `LOGIN` + `GRANT pg_read_all_data` (Postgres 14+).
   - pgBackRest: it needs `pg_backup_start`/`pg_backup_stop`,
     `pg_switch_wal` and `pg_create_restore_point` (grant `EXECUTE` on
     each). It also reads the data directory as the OS `postgres` user it
     already runs as, so it does not need superuser.
   Give each one a small `CONNECTION LIMIT`.
4. **Keep `distant_signal` for humans only.** It stays the bootstrap
   superuser, used only for admin `psql`. Then
   `superuser_reserved_connections` really does reserve admin slots, and
   `postgresql.connectionBudget.adminSessions` can drop to 0.

## Chart and secrets work this needs

- New Secret keys for each role's password. Seal them in Ranma-Config the
  same way as the current password, which was sealed as its current value
  and not rotated.
- The roles must be created on the existing data directory. The image's
  `/docker-entrypoint-initdb.d` only runs on an empty one, so use a one-off
  `kubectl exec` runbook or an idempotent Job, not an init script.
- Update the components' `DATABASE_URL` templates, the pgBackRest env
  (`PGBACKREST_PG1_USER`), and Ranma-Config's exporter and `pg_dump`.
- Test it on a scratch database restored from a `pg_dump`. Run the full
  `cargo test -p api -- --ignored` suite as the app role, then roll out.

The work is too big for the R-048 fix itself, which only reserves room for
these clients in the budget check.
