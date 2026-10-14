#!/bin/sh
# The postgresql.roles.setupJob entrypoint (templates/postgres-roles.yaml,
# docs/postgres-app-role.md "When the setup Job fails"):
#
#   postgres-roles-setup.sh wait
#   postgres-roles-setup.sh run PSQL_ARG... --file=SCRIPT.sql
#
# `wait` polls pg_isready until Postgres accepts connections. `run` runs
# one idempotent setup script with psql (ON_ERROR_STOP, verbose errors with
# the SQLSTATE, the failed statement echoed, per-statement timing) and runs
# it again, after a backoff, when it failed for a reason that a later try
# can get past:
#   - 55P03 lock_not_available: lock_timeout fired (PGOPTIONS sets it, so a
#     lock queued behind a long transaction fails fast instead of waiting
#     until the Job's deadline, 2026-10-14);
#   - 57014 query_canceled: statement_timeout fired (a statement slowed
#     down by a starved node, or a lock wait that lock_timeout missed);
#   - 40P01 deadlock_detected, or "tuple concurrently updated" (a
#     concurrent GRANT or ALTER on the same catalog row);
#   - psql exit 2 (the connection failed or was lost) and a timed-out try.
# Before each retry it prints the sessions that may be in the way (long
# transactions, lock waiters and what they hold), without their query
# text when it could hold a password. Any other failure is final.
#
# Connection: libpq's environment (PGHOST, PGPORT, PGUSER, PGDATABASE,
# PGCONNECT_TIMEOUT, PGOPTIONS; the password from its env var). Settings,
# all optional:
#   DS_SETUP_DEADLINE         epoch seconds by which this pod must be done:
#                             no wait, try or backoff runs past it (default
#                             now + 540; the Job passes its
#                             activeDeadlineSeconds minus a margin)
#   DS_SETUP_WAIT_SECONDS     `wait` gives up after this long (180)
#   DS_SETUP_ATTEMPTS         `run` tries at most this many times (5)
#   DS_SETUP_ATTEMPT_SECONDS  hard limit on one try, psql killed after it
#                             (180)
#   DS_SETUP_BACKOFF_SECONDS  the first backoff, doubled after each try and
#                             capped at 60 (5)
#
# Exit status: 0 done; 64 bad usage; 69 Postgres not ready in time; 75
# every try failed for a retriable reason, or the deadline left no room for
# another; otherwise psql's own status from a final failure (1, or 3: an
# SQL error under ON_ERROR_STOP).
set -eu

EX_USAGE=64
EX_UNAVAILABLE=69
EX_TEMPFAIL=75

log() {
    stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s postgres-roles-setup: %s\n' "${stamp}" "$*"
}

now() {
    date +%s
}

whole_number() {
    case $2 in
        '' | *[!0-9]*)
            log "$1 must be a whole number of seconds, not '$2'"
            exit "${EX_USAGE}"
            ;;
        *) ;;
    esac
}

started=$(now)
deadline=${DS_SETUP_DEADLINE:-$((started + 540))}
whole_number DS_SETUP_DEADLINE "${deadline}"

# Strip passwords from psql's output: an echoed failed statement may be the
# set_config call that carries them, and an error's CONTEXT may quote an
# ALTER ROLE ... PASSWORD. Single-quoted literals, '' escapes included.
redact() {
    sed -E \
        -e "s/(_password', *)'([^']|'')*'/\\1'<redacted>'/g" \
        -e "s/([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd] +)'([^']|'')*'/\\1'<redacted>'/g"
}

wait_for_postgres() {
    limit=${DS_SETUP_WAIT_SECONDS:-180}
    whole_number DS_SETUP_WAIT_SECONDS "${limit}"
    t=$(now)
    give_up=$((t + limit))
    if [ "${give_up}" -gt "${deadline}" ]; then
        give_up=${deadline}
    fi
    polls=0
    while :; do
        status=0
        said=$(pg_isready --timeout=5 2>&1) || status=$?
        if [ "${status}" -eq 0 ]; then
            log "Postgres is ready: ${said}"
            return 0
        fi
        t=$(now)
        if [ "${t}" -ge "${give_up}" ]; then
            log "giving up: Postgres was not ready after $((t - started))s" \
                "(pg_isready exit ${status}: ${said})"
            exit "${EX_UNAVAILABLE}"
        fi
        # Every 30 s or so, not every poll.
        if [ $((polls % 15)) -eq 0 ]; then
            log "waiting for Postgres (pg_isready exit ${status}: ${said})"
        fi
        polls=$((polls + 1))
        sleep 2
    done
}

# The sessions that can hold up the setup: in a transaction for over 5 s,
# waiting on a lock, or blocking someone. Each with what it holds in schema
# public (relation and lock mode) and its query, cut short, or <redacted>
# when it mentions a password. Best effort, with its own short timeouts.
print_blockers() {
    log "sessions that may be in the way (pid, application_name, state, xact_start, query):"
    status=0
    # shellcheck disable=SC2310 # best effort: its failure is reported below
    said=$(PGOPTIONS="${PGOPTIONS:-} -c statement_timeout=10s -c lock_timeout=5s" \
        run_limited 20 psql -X --no-password --quiet --expanded \
        --set=ON_ERROR_STOP=1 --pset=pager=off --command="
SELECT a.pid,
       a.usename,
       a.application_name,
       a.client_addr,
       a.backend_type,
       a.state,
       a.wait_event_type || ':' || a.wait_event AS waiting_on,
       pg_blocking_pids(a.pid) AS blocked_by,
       a.xact_start,
       date_trunc('second', now() - a.xact_start) AS xact_age,
       (SELECT left(string_agg(c.relname || ' ' || l.mode, ', ' ORDER BY c.relname), 300)
        FROM pg_locks l
        JOIN pg_class c ON c.oid = l.relation
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE l.pid = a.pid AND l.granted AND n.nspname = 'public') AS holds,
       CASE WHEN a.query ~* '(password|set_config)' THEN '<redacted>'
            ELSE left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300)
       END AS query
FROM pg_stat_activity a
WHERE a.pid <> pg_backend_pid()
  AND (a.xact_start < now() - interval '5 seconds'
       OR a.wait_event_type = 'Lock'
       OR a.pid IN (SELECT unnest(pg_blocking_pids(w.pid))
                    FROM pg_stat_activity w WHERE w.wait_event_type = 'Lock'))
ORDER BY a.xact_start NULLS LAST
LIMIT 20" 2>&1) || status=$?
    printf '%s\n' "${said}" | redact
    if [ "${status}" -ne 0 ]; then
        log "could not list the sessions (exit ${status})"
    fi
}

# run_limited SECONDS COMMAND...: the command, killed after SECONDS when
# timeout(1) is there (the Postgres image has it); exit 124 when it was.
run_limited() {
    limit=$1
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout --kill-after=10 "${limit}" "$@"
    else
        "$@"
    fi
}

run_setup() {
    if [ "$#" -eq 0 ]; then
        log "run: no psql arguments"
        exit "${EX_USAGE}"
    fi
    attempts=${DS_SETUP_ATTEMPTS:-5}
    per_try=${DS_SETUP_ATTEMPT_SECONDS:-180}
    backoff=${DS_SETUP_BACKOFF_SECONDS:-5}
    whole_number DS_SETUP_ATTEMPTS "${attempts}"
    whole_number DS_SETUP_ATTEMPT_SECONDS "${per_try}"
    whole_number DS_SETUP_BACKOFF_SECONDS "${backoff}"
    # The last argument names the script, for the log.
    for script; do :; done
    script=${script#--file=}
    script=${script##*/}
    work=${TMPDIR:-/tmp}/postgres-roles-setup.$$
    mkdir -p "${work}"
    trap 'rm -rf "${work}"' EXIT
    try=1
    while :; do
        t=$(now)
        left=$((deadline - t))
        limit=${per_try}
        if [ "${left}" -lt "${limit}" ]; then
            limit=${left}
        fi
        if [ "${limit}" -lt 15 ]; then
            log "giving up on ${script}: ${left}s left before the deadline, too little for try ${try}"
            exit "${EX_TEMPFAIL}"
        fi
        log "${script}: try ${try} of ${attempts} (at most ${limit}s; PGOPTIONS=${PGOPTIONS:-})"
        # psql's status survives the pipe through a file: the pipeline's
        # own status is the last command's.
        {
            status=0
            # shellcheck disable=SC2310 # psql's status is kept, not fatal
            run_limited "${limit}" psql -X --no-password --quiet --echo-errors \
                --set=ON_ERROR_STOP=1 --set=VERBOSITY=verbose \
                --command='\timing on' "$@" 2>&1 || status=$?
            echo "${status}" >"${work}/status"
        } | redact | tee "${work}/output"
        status=$(cat "${work}/status")
        t=$(now)
        if [ "${status}" -eq 0 ]; then
            log "${script}: done in $((t - started))s since the pod started"
            return 0
        fi
        reason=""
        if grep -q -E 'ERROR: +55P03' "${work}/output"; then
            reason="lock timeout (55P03)"
        elif grep -q -E 'ERROR: +57014' "${work}/output"; then
            reason="statement timeout (57014)"
        elif grep -q -E 'ERROR: +40P01' "${work}/output"; then
            reason="deadlock (40P01)"
        elif grep -q 'tuple concurrently updated' "${work}/output"; then
            reason="tuple concurrently updated"
        elif [ "${status}" -eq 124 ]; then
            reason="no result in ${limit}s"
        elif [ "${status}" -eq 2 ]; then
            reason="connection failed or lost (psql exit 2)"
        fi
        if [ -z "${reason}" ]; then
            log "${script}: failed (psql exit ${status}); not retried"
            exit "${status}"
        fi
        log "${script}: try ${try} failed: ${reason}"
        print_blockers
        if [ "${try}" -ge "${attempts}" ]; then
            log "giving up on ${script} after ${try} tries; last: ${reason}"
            exit "${EX_TEMPFAIL}"
        fi
        t=$(now)
        if [ $((t + backoff + 15)) -gt "${deadline}" ]; then
            log "giving up on ${script}: no time left before the deadline for another try; last: ${reason}"
            exit "${EX_TEMPFAIL}"
        fi
        log "${script}: retrying in ${backoff}s"
        sleep "${backoff}"
        backoff=$((backoff * 2))
        if [ "${backoff}" -gt 60 ]; then
            backoff=60
        fi
        try=$((try + 1))
    done
}

case ${1:-} in
    wait)
        wait_for_postgres
        ;;
    run)
        shift
        run_setup "$@"
        ;;
    *)
        log "usage: $0 wait | run PSQL_ARG..."
        exit "${EX_USAGE}"
        ;;
esac
