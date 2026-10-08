-- Postgres role split for Distant Signal (docs/postgres-app-role.md).
--
-- Idempotent: safe to run any number of times, on a brand-new cluster (the
-- chart runs it from /docker-entrypoint-initdb.d, before any migration) or
-- on an existing one (the chart's `postgresql.roles.setupJob`, or by hand).
-- Run it with psql as a SUPERUSER, connected to the application database:
--
--   psql -X -v ON_ERROR_STOP=1 -d distant_signal \
--     -v old_owner=distant_signal \
--     -v owner=distant_signal_owner -v owner_connection_limit=4 \
--     -v app=distant_signal_app -v app_connection_limit=97 \
--     -v exporter=distant_signal_exporter -v exporter_connection_limit=3 \
--     -v dump=distant_signal_dump -v dump_connection_limit=2 \
--     -v backup=distant_signal_backup -v backup_connection_limit=4 \
--     -v backup_database=postgres \
--     -f postgres-roles.sql
--
-- Passwords come from the environment, never the command line (psql's
-- \getenv): DS_PG_OWNER_PASSWORD, DS_PG_APP_PASSWORD,
-- DS_PG_EXPORTER_PASSWORD, DS_PG_DUMP_PASSWORD, DS_PG_BACKUP_PASSWORD. All
-- five are required. (pgBackRest itself connects over the local socket,
-- which the official image trusts, so it never sends the backup role's
-- password; the password is defence in depth for any network login.)
--
-- What it does, in the application database, in ONE transaction:
--   1. Creates or updates the five roles: LOGIN, NOSUPERUSER, NOCREATEDB,
--      NOCREATEROLE, NOREPLICATION, NOBYPASSRLS (BYPASSRLS for the dump
--      role, see below), the given CONNECTION LIMIT and password. Refuses a name that is the superuser itself or
--      any other existing superuser, so a typo can never demote an admin.
--   2. Built-in role memberships: owner pg_read_all_stats (the migration
--      heal reads other sessions' pg_stat_progress_create_index rows),
--      exporter pg_monitor, dump pg_read_all_data, backup
--      pg_read_all_settings and pg_checkpoint.
--   3. Makes the owner role own schema public and every object in it that
--      `old_owner` owns (tables, partitions, views, sequences, functions,
--      types), except extension members (pg_stat_statements stays with
--      the superuser). REASSIGN OWNED is NOT used: `old_owner` is the
--      image's bootstrap superuser, and Postgres refuses to reassign its
--      objects ("required by the database system"). Fails if anything in
--      public is still owned by `old_owner` afterwards.
--   4. App role: CONNECT, USAGE on public, SELECT/INSERT/UPDATE/DELETE on
--      every table (no TRUNCATE, REFERENCES or TRIGGER: no service uses
--      them), USAGE/SELECT/UPDATE on every sequence, EXECUTE on the
--      owner's functions (analyze_publish_keys), read-only on
--      _sqlx_migrations, and the same through ALTER DEFAULT PRIVILEGES FOR
--      ROLE owner, so every table a later migration creates is covered.
--   5. Backup role: EXECUTE on pg_backup_start, pg_backup_stop,
--      pg_switch_wal and pg_create_restore_point, granted in this database
--      and in `backup_database` (function grants are per database, and
--      pgBackRest connects to `postgres` unless pg1-database says
--      otherwise).
--   6. CREATE EXTENSION IF NOT EXISTS pg_stat_statements, best effort, so
--      the owner role (which may not create it) finds it on a new cluster.
--   7. The application database: no CONNECT or TEMPORARY for PUBLIC, so
--      only roles granted CONNECT (the five here, and each per-service role
--      in the per-service grants script) may log in to it, and none may create
--      temporary tables (no service does); no EXECUTE for PUBLIC on the
--      functions the owner creates later (security review L5).
--
-- The session first turns statement logging off (security review L8): the
-- passwords are literals in the set_config call below, so a failing
-- statement (log_min_error_statement), a slow one
-- (log_min_duration_statement) or log_statement = all would otherwise
-- write them to the server log.

\set ON_ERROR_STOP on

SET log_min_error_statement = panic;
SET log_min_duration_statement = -1;
SET log_statement = none;

\if :{?old_owner}
\else
\set old_owner distant_signal
\endif
\if :{?owner}
\else
\set owner distant_signal_owner
\endif
\if :{?app}
\else
\set app distant_signal_app
\endif
\if :{?exporter}
\else
\set exporter distant_signal_exporter
\endif
\if :{?dump}
\else
\set dump distant_signal_dump
\endif
\if :{?backup}
\else
\set backup distant_signal_backup
\endif
\if :{?owner_connection_limit}
\else
\set owner_connection_limit 4
\endif
\if :{?app_connection_limit}
\else
\set app_connection_limit 97
\endif
\if :{?exporter_connection_limit}
\else
\set exporter_connection_limit 3
\endif
\if :{?dump_connection_limit}
\else
\set dump_connection_limit 2
\endif
\if :{?backup_connection_limit}
\else
\set backup_connection_limit 4
\endif
\if :{?backup_database}
\else
\set backup_database postgres
\endif

\getenv owner_password DS_PG_OWNER_PASSWORD
\getenv app_password DS_PG_APP_PASSWORD
\getenv exporter_password DS_PG_EXPORTER_PASSWORD
\getenv dump_password DS_PG_DUMP_PASSWORD
\getenv backup_password DS_PG_BACKUP_PASSWORD
\if :{?owner_password}
\else
\set owner_password ''
\endif
\if :{?app_password}
\else
\set app_password ''
\endif
\if :{?exporter_password}
\else
\set exporter_password ''
\endif
\if :{?dump_password}
\else
\set dump_password ''
\endif
\if :{?backup_password}
\else
\set backup_password ''
\endif

BEGIN;

-- The settings below live only until COMMIT (set_config(..., true)), so
-- the passwords never outlast this transaction.
SELECT
    set_config('ds_roles.old_owner', :'old_owner', true),
    set_config('ds_roles.owner', :'owner', true),
    set_config('ds_roles.app', :'app', true),
    set_config('ds_roles.exporter', :'exporter', true),
    set_config('ds_roles.dump', :'dump', true),
    set_config('ds_roles.backup', :'backup', true),
    set_config('ds_roles.owner_password', :'owner_password', true),
    set_config('ds_roles.app_password', :'app_password', true),
    set_config('ds_roles.exporter_password', :'exporter_password', true),
    set_config('ds_roles.dump_password', :'dump_password', true),
    set_config('ds_roles.backup_password', :'backup_password', true),
    set_config('ds_roles.owner_connection_limit', :'owner_connection_limit', true),
    set_config('ds_roles.app_connection_limit', :'app_connection_limit', true),
    set_config('ds_roles.exporter_connection_limit', :'exporter_connection_limit', true),
    set_config('ds_roles.dump_connection_limit', :'dump_connection_limit', true),
    set_config('ds_roles.backup_connection_limit', :'backup_connection_limit', true)
\gset ignored_

-- 1. Roles, 2. memberships.
DO $roles$
DECLARE
    old_owner text := current_setting('ds_roles.old_owner');
    r record;
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'postgres-roles.sql must run as a superuser, not %', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = old_owner) THEN
        RAISE EXCEPTION 'old_owner % does not exist', old_owner;
    END IF;

    FOR r IN
        SELECT v.kind, current_setting('ds_roles.' || v.kind) AS name,
               current_setting('ds_roles.' || v.kind || '_password') AS password,
               current_setting('ds_roles.' || v.kind || '_connection_limit') AS connection_limit
        FROM (VALUES ('owner'), ('app'), ('exporter'), ('dump'), ('backup')) AS v(kind)
    LOOP
        IF r.name = '' OR r.name = old_owner OR r.name = current_user THEN
            RAISE EXCEPTION 'the % role name % must be a new, separate role', r.kind, r.name;
        END IF;
        IF (SELECT count(*) FROM (VALUES
                (current_setting('ds_roles.owner')), (current_setting('ds_roles.app')),
                (current_setting('ds_roles.exporter')), (current_setting('ds_roles.dump')),
                (current_setting('ds_roles.backup'))) AS n(name)
            WHERE n.name = r.name) > 1 THEN
            RAISE EXCEPTION 'the role name % is used for more than one role', r.name;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name AND rolsuper) THEN
            RAISE EXCEPTION 'refusing to demote existing superuser % (the % role)', r.name, r.kind;
        END IF;
        IF r.connection_limit !~ '^(-1|[0-9]+)$' THEN
            RAISE EXCEPTION 'the % role connection limit % is not a whole number', r.kind, r.connection_limit;
        END IF;
        IF r.password = '' THEN
            RAISE EXCEPTION 'no password for the % role: set DS_PG_%_PASSWORD', r.kind, upper(r.kind);
        END IF;

        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name) THEN
            EXECUTE format('CREATE ROLE %I', r.name);
        END IF;
        -- The dump role alone bypasses row-level security (ingest plan
        -- 3c.3): pg_dump runs with row_security off and refuses a table with
        -- RLS (line_status, 20261009131300_line_status_rls.sql) for a role
        -- that does not, which would fail the nightly dump. It only ever
        -- reads (pg_read_all_data), and PostgreSQL recommends BYPASSRLS for
        -- such a role; a dump then holds every row whatever the policies.
        EXECUTE format(
            'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
            '%s INHERIT CONNECTION LIMIT %s',
            r.name, CASE WHEN r.kind = 'dump' THEN 'BYPASSRLS' ELSE 'NOBYPASSRLS' END,
            r.connection_limit);
        EXECUTE format('ALTER ROLE %I PASSWORD %L', r.name, r.password);
    END LOOP;

    FOR r IN
        SELECT m.member, m.builtin
        FROM (VALUES
            (current_setting('ds_roles.owner'), 'pg_read_all_stats'),
            (current_setting('ds_roles.exporter'), 'pg_monitor'),
            (current_setting('ds_roles.dump'), 'pg_read_all_data'),
            (current_setting('ds_roles.backup'), 'pg_read_all_settings'),
            -- Postgres 15+: lets pgBackRest's start-fast force the checkpoint.
            (current_setting('ds_roles.backup'), 'pg_checkpoint')) AS m(member, builtin)
        WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = m.builtin)
          AND NOT EXISTS (
              SELECT 1 FROM pg_auth_members a
              WHERE a.roleid = (SELECT oid FROM pg_roles WHERE rolname = m.builtin)
                AND a.member = (SELECT oid FROM pg_roles WHERE rolname = m.member))
    LOOP
        EXECUTE format('GRANT %I TO %I', r.builtin, r.member);
    END LOOP;
END
$roles$;

-- 3. Ownership.
DO $ownership$
DECLARE
    old_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('ds_roles.old_owner'));
    owner_role text := current_setting('ds_roles.owner');
    r record;
    leftover text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_namespace
        WHERE nspowner = old_oid
          AND nspname NOT IN ('public', 'information_schema')
          AND nspname NOT LIKE 'pg\_%'
    ) THEN
        RAISE EXCEPTION 'schemas other than public are owned by %; this script only moves public',
            current_setting('ds_roles.old_owner');
    END IF;

    EXECUTE format('ALTER SCHEMA public OWNER TO %I', owner_role);

    -- Relations. Indexes and TOAST tables follow their table; a sequence
    -- owned by a column (serial/identity) follows its table too, and may
    -- not be altered on its own.
    FOR r IN
        SELECT c.relkind, c.oid::regclass AS name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relowner = old_oid
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c')
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
          AND NOT (c.relkind = 'S' AND EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))
        ORDER BY c.relkind = 'S', c.oid
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
            CASE r.relkind
                WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW'
                WHEN 'f' THEN 'FOREIGN TABLE'
                WHEN 'S' THEN 'SEQUENCE'
                WHEN 'c' THEN 'TYPE'
                ELSE 'TABLE'
            END,
            r.name, owner_role);
    END LOOP;

    -- Functions, procedures, aggregates.
    FOR r IN
        SELECT p.prokind, p.oid::regprocedure AS name
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proowner = old_oid
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
            CASE r.prokind WHEN 'a' THEN 'AGGREGATE' WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            r.name, owner_role);
    END LOOP;

    -- Standalone types: enums, domains, ranges (array types and table row
    -- types follow their element type or table).
    FOR r IN
        SELECT t.typtype, t.oid::regtype AS name
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public'
          AND t.typowner = old_oid
          AND t.typtype IN ('e', 'd', 'r', 'b')
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype IN ('e', 'i'))
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
            CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, r.name, owner_role);
    END LOOP;

    -- Nothing in public may be left behind.
    SELECT string_agg(what, ', ') INTO leftover FROM (
        SELECT format('relation %s', c.oid::regclass) AS what
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner = old_oid
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
        UNION ALL
        SELECT format('function %s', p.oid::regprocedure)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proowner = old_oid
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
        UNION ALL
        SELECT format('type %s', t.oid::regtype)
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typowner = old_oid
          AND t.typtype IN ('e', 'd', 'r', 'b')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype IN ('e', 'i'))
    ) AS left_behind;
    IF leftover IS NOT NULL THEN
        RAISE EXCEPTION 'still owned by %: %', current_setting('ds_roles.old_owner'), leftover;
    END IF;
END
$ownership$;

-- 4. App role privileges, 5. backup functions (this database).
DO $privileges$
DECLARE
    owner_role text := current_setting('ds_roles.owner');
    app text := current_setting('ds_roles.app');
    r record;
BEGIN
    -- 7. Only the roles granted CONNECT below (and each per-service role,
    -- the per-service grants script) may connect; nobody gets TEMPORARY. The backup
    -- role too, in case backup_database is this database. Superusers are
    -- not affected.
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
        current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I, %I, %I, %I, %I',
        current_database(), owner_role, app,
        current_setting('ds_roles.exporter'), current_setting('ds_roles.dump'),
        current_setting('ds_roles.backup'));
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app);

    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', app);
    EXECUTE format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I', app);
    -- The owner's functions only: GRANT ... ON ALL FUNCTIONS would also
    -- hand out pg_stat_statements_reset().
    FOR r IN
        SELECT p.oid::regprocedure AS name
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
          AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = owner_role)
    LOOP
        EXECUTE format('GRANT EXECUTE ON ROUTINE %s TO %I', r.name, app);
    END LOOP;
    -- The migration history is the owner's business.
    IF to_regclass('public._sqlx_migrations') IS NOT NULL THEN
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON public._sqlx_migrations FROM %I', app);
    END IF;

    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                   'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', owner_role, app);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                   'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', owner_role, app);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                   'GRANT EXECUTE ON ROUTINES TO %I', owner_role, app);
    -- 7. A function the owner creates is not executable by PUBLIC (the
    -- Postgres default): only by the roles granted it (app above, a
    -- per-service role through the per-service grants script). A SECURITY DEFINER
    -- one runs with the owner's rights.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I '
                   'REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', owner_role);

    EXECUTE format('GRANT EXECUTE ON FUNCTION pg_catalog.pg_backup_start(text, boolean), '
                   'pg_catalog.pg_backup_stop(boolean), pg_catalog.pg_switch_wal(), '
                   'pg_catalog.pg_create_restore_point(text) TO %I',
                   current_setting('ds_roles.backup'));
END
$privileges$;

-- 6. pg_stat_statements (migration 20260927070000 does the same, but as
-- the owner role it may not).
DO $extension$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') THEN
        CREATE EXTENSION pg_stat_statements;
    END IF;
EXCEPTION
    WHEN undefined_file OR feature_not_supported THEN
        RAISE WARNING 'pg_stat_statements was not created (%: %)', SQLSTATE, SQLERRM;
END
$extension$;

COMMIT;

-- 5. Backup functions, in the database pgBackRest connects to.
\connect :"backup_database"
BEGIN;
SELECT set_config('ds_roles.backup', :'backup', true) \gset ignored_
DO $backup$
BEGIN
    EXECUTE format('GRANT EXECUTE ON FUNCTION pg_catalog.pg_backup_start(text, boolean), '
                   'pg_catalog.pg_backup_stop(boolean), pg_catalog.pg_switch_wal(), '
                   'pg_catalog.pg_create_restore_point(text) TO %I',
                   current_setting('ds_roles.backup'));
END
$backup$;
COMMIT;
