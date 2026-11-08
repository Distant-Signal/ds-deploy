#!/bin/sh
# Renders the bundled Redis's users.acl and, with `redis.acl.reload.mode:
# inPlace`, reloads it into the running server (`ACL LOAD`) when the users
# or their passwords change, instead of restarting Redis
# (docs/redis-acl.md, "Reloading the users in place"). The chart's
# redis-acl-configmap.yaml embeds this file with .Files.Get; the Redis
# pod's `redis-acl` initContainer runs `render` and its `redis-acl-reload`
# sidecar runs `watch`, both with the Redis image's sh and awk.
#
#   redis-acl-reload.sh render   print users.acl: the template with each
#                                `>${VAR}` filled from the file the password
#                                map names for VAR. An empty *_PREVIOUS (no
#                                rotation in progress) is dropped; any other
#                                empty or missing password, or one with
#                                whitespace (a newline would start a new ACL
#                                rule line), fails (exit 1).
#   redis-acl-reload.sh watch    every RELOAD_INTERVAL seconds, render again.
#                                When the result differs from the live file,
#                                swap it in and send `ACL LOAD` as ds-admin.
#                                If Redis refuses it, put the previous file
#                                back: Redis keeps its current users (ACL
#                                LOAD is all or nothing), and a restart would
#                                load the same file it runs now. Never exits.
#
# Environment (the chart sets the paths; these are its defaults):
#   ACL_TEMPLATE      the users file with ${VAR} placeholders
#   ACL_PASSWORDS     the password map: one "VAR file" pair per line
#   ACL_FILE          the live users.acl Redis reads (--aclfile)
#   ACL_ADMIN_PASSWORD_FILE, ACL_ADMIN_PREVIOUS_FILE
#                     ds-admin's password files, to authenticate ACL LOAD
#   REDIS_PORT        Redis's port on localhost (default 6379)
#   RELOAD_INTERVAL   seconds between renders (default 10)
#   RELOAD_ONCE       if non-empty, `watch` runs one round and exits (tests)
set -u

TEMPLATE=${ACL_TEMPLATE:-/etc/redis-acl-template/users.acl.tpl}
PASSWORDS=${ACL_PASSWORDS:-/etc/redis-acl-template/passwords}
LIVE=${ACL_FILE:-/etc/redis-acl/users.acl}
ADMIN_FILE=${ACL_ADMIN_PASSWORD_FILE:-/etc/redis-acl-secret/ds-admin-password}
ADMIN_PREVIOUS_FILE=${ACL_ADMIN_PREVIOUS_FILE:-/etc/redis-acl-secret/ds-admin-password-previous}
PORT=${REDIS_PORT:-6379}
INTERVAL=${RELOAD_INTERVAL:-10}
ONCE=${RELOAD_ONCE:-}

umask 077

log() {
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ) || now=-
    printf '%s redis-acl-reload: %s\n' "${now}" "$*" >&2
}

# A file's exact content (trailing newlines included, which `$(cat f)`
# would strip). Empty if the file is missing.
read_exact() {
    [ -f "$1" ] || return 0
    content=$(
        cat "$1"
        printf x
    )
    printf '%s' "${content%x}"
}

# Same substitution and checks as the restart mode's inline initContainer
# (templates/redis-deployment.yaml), with each password read from its file
# into the environment of a subshell first.
render() {
    (
        while read -r var file; do
            case "${var}" in
                '' | '#'*) continue ;;
                *[!A-Z0-9_]*)
                    log "the password map names an invalid variable: ${var}"
                    exit 1
                    ;;
                *) ;;
            esac
            value=$(
                read_exact "${file}"
                printf x
            )
            export "${var}=${value%x}"
        done <"${PASSWORDS}" || exit 1
        awk '
        {
          out = ""
          n = split($0, field, " ")
          for (i = 1; i <= n; i++) {
            t = field[i]
            if (t ~ /^>[$][{][A-Z0-9_]+[}]$/) {
              name = substr(t, 4, length(t) - 4)
              v = ENVIRON[name]
              if (v == "") {
                if (name ~ /_PREVIOUS$/) continue
                print "redis-acl: " name " is empty" > "/dev/stderr"
                exit 1
              }
              if (v ~ /[[:space:]]/) {
                print "redis-acl: " name " contains whitespace" > "/dev/stderr"
                exit 1
              }
              t = ">" v
            }
            out = (out == "") ? t : out " " t
          }
          print out
        }' "${TEMPLATE}"
    )
}

# ds-admin's passwords to try, one per line: those in the users file
# Redis runs now ($1, the one this round replaces), then the Secret's
# current and -previous ones. After a rotation without a -previous key
# Redis still has the old password until this load.
admin_candidates() {
    awk '$1 == "user" && $2 == "ds-admin" {
      for (i = 3; i <= NF; i++) if ($i ~ /^>/) print substr($i, 2)
    }' "$1" 2>/dev/null || :
    read_exact "${ADMIN_FILE}"
    echo
    read_exact "${ADMIN_PREVIOUS_FILE}"
    echo
}

# ACL LOAD as ds-admin; $1 is the users file Redis runs now.
acl_load() {
    reply=
    candidates=$(admin_candidates "$1")
    while read -r candidate; do
        [ -n "${candidate}" ] || continue
        if reply=$(REDISCLI_AUTH=${candidate} redis-cli -h 127.0.0.1 -p "${PORT}" --user ds-admin ACL LOAD 2>&1) \
            && [ "${reply}" = OK ]; then
            return 0
        fi
        case "${reply}" in
            *WRONGPASS* | *NOAUTH* | *'invalid username-password'*) continue ;;
            *) break ;;
        esac
    done <<CANDIDATES
${candidates}
CANDIDATES
    log "ACL LOAD failed: ${reply:-no ds-admin password}"
    return 1
}

# One round: render, and reload if the users changed. 0 when nothing
# needed doing or the reload worked.
reload_once() {
    next="${LIVE}.next"
    if ! render >"${next}" 2>"${LIVE}.err"; then
        err=$(cat "${LIVE}.err")
        if [ "${err}" != "${last_error:-}" ]; then
            log "not reloading, the users do not render: ${err}"
        fi
        last_error=${err}
        rm -f "${next}" "${LIVE}.err"
        return 1
    fi
    rm -f "${LIVE}.err"
    last_error=
    if cmp -s "${next}" "${LIVE}"; then
        rm -f "${next}"
        return 0
    fi
    cp "${LIVE}" "${LIVE}.prev" 2>/dev/null || : >"${LIVE}.prev"
    mv "${next}" "${LIVE}"
    if acl_load "${LIVE}.prev"; then
        rm -f "${LIVE}.prev"
        users=$(grep -c '^user ' "${LIVE}") || users=0
        log "reloaded the ACL users (${users} users)"
        return 0
    fi
    mv "${LIVE}.prev" "${LIVE}"
    log "kept the previous users; retrying"
    return 1
}

watch() {
    log "watching ${TEMPLATE} and the password files every ${INTERVAL}s"
    delay=${INTERVAL}
    while :; do
        if [ -z "${ONCE}" ]; then
            sleep "${delay}"
        fi
        if reload_once; then
            status=0
            delay=${INTERVAL}
        else
            status=1
            if [ "${delay}" -lt 300 ]; then
                # Back off while a reload keeps failing (Redis loading, a
                # missing Secret key), up to about five minutes.
                delay=$((delay * 2))
            fi
        fi
        if [ -n "${ONCE}" ]; then
            return "${status}"
        fi
    done
}

case "${1:-}" in
    render) render ;;
    watch) watch ;;
    *)
        echo "usage: $0 render|watch" >&2
        exit 2
        ;;
esac
