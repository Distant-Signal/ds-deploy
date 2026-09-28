#!/bin/sh
# Provisions the SFTP-login account DTD's push client authenticates as,
# then execs `sftpgo serve`. The one copy of this script: the chart's
# schedulefeed-configmap.yaml embeds it with .Files.Get, and
# docker-compose.yml's `schedule-sftp` service bind-mounts it.
#
# Environment:
#   SCHEDULE_SFTP_USERNAME      the account (required)
#   SCHEDULE_SFTP_HOME_DIR      its home_dir, which is also its SFTP chroot
#                               root (required)
#   SCHEDULE_SFTP_PUBLIC_KEY    authenticate by this public key, or else
#   SCHEDULE_SFTP_PASSWORD      by this password (one of the two required)
#   SCHEDULE_SFTP_LOADDATA_DIR  where the loaddata JSON is written
#                               (default /run/sftp-bootstrap)
#
# SFTPGO_DEFAULT_ADMIN_USERNAME/PASSWORD (this repo's earlier approach, now
# removed) never worked for this: confirmed directly against SFTPGo's own
# Go source (github.com/drakkan/sftpgo, internal/dataprovider/admin.go's
# Admin.setFromEnv(), called only from dataprovider.go's checkDefaultAdmin())
# that those env vars bootstrap SFTPGo's web-UI/REST-API *admin* account
# (dataprovider.Admin, PermAdminAny) -- a completely separate entity from an
# SFTP-login *user* (dataprovider.User). Setting them never created the
# account DTD (or this service's own paramiko-based test) tries to log in
# as, which is exactly the "not found: sql: no rows in result set" error
# this task started from.
#
# The real, documented mechanism is SFTPGo's own `--loaddata-from` flag /
# SFTPGO_LOADDATA_FROM env var: it loads a JSON dump (same shape as its
# `dumpdata` REST endpoint produces) of users/folders/admins/etc at startup.
# Confirmed directly against SFTPGo's source (checked against the `main`
# branch):
#   - internal/cmd/root.go wires SFTPGO_LOADDATA_FROM/_MODE/_CLEAN/_SCAN as
#     real, current flags (loaddata-mode defaults to 1: "new users are
#     added, existing users are not modified").
#   - internal/service/service.go's Service.Start() -> startServices() calls
#     Service.LoadInitialData() (which reads/parses/restores this file)
#     BEFORE binding the SFTP/FTP/HTTP listeners -- so the account exists
#     before anything can connect, and this needs no HTTP/API port exposed
#     at all.
#   - internal/httpd/api_maintenance.go's RestoreUsers does a
#     username-keyed upsert: with mode 1, a user that already exists from a
#     prior run is left untouched -- so re-running this on every container
#     restart is safe and never resets an already-changed password.
#   - internal/dataprovider/dataprovider.go's createUserPasswordHash (run
#     for every Add/UpdateUser, including the loaddata restore path) hashes
#     any password that isn't already in a recognized hash format -- so the
#     plaintext password below is correct as-is.
#
# SCHEDULE_SFTP_HOME_DIR is set, by the chart and by docker-compose.yml, to
# the same absolute path schedule-ingest watches (WATCH_DIR), so DTD's push
# client -- which sees itself uploading to "/" once connected -- lands files
# exactly where schedule-ingest looks for them. SFTPGo creates home_dir
# itself if it doesn't already exist (vfs/osfs.go's CheckRootPath).
set -eu

: "${SCHEDULE_SFTP_USERNAME:?SCHEDULE_SFTP_USERNAME must be set}"
: "${SCHEDULE_SFTP_HOME_DIR:?SCHEDULE_SFTP_HOME_DIR must be set}"

# INF-13: the loaddata file holds the push account's credential, so it
# lives on a memory-backed directory (the chart's `sftp-bootstrap`
# emptyDir: never on disk, gone with the Pod) and is readable by this user
# only. Local dev points SCHEDULE_SFTP_LOADDATA_DIR at /tmp.
LOADDATA_FILE="${SCHEDULE_SFTP_LOADDATA_DIR:-/run/sftp-bootstrap}/schedule-sftp-loaddata.json"
umask 077

# Every value below goes into a JSON string literal, escaped here: a
# '"' or '\' in an operator-set password (or username, or key) must not
# be able to break out of its string and add or change fields, such as
# the account's permissions or home_dir. Control characters are
# rejected outright; none of these values legitimately contains one.
reject_control_chars() {
    case "$2" in
        *[[:cntrl:]]*)
            echo "sftp-entrypoint: $1 contains a control character (newline, tab, ...); refusing to provision the account" >&2
            exit 1
            ;;
        *) ;; # no control characters: accept
    esac
}
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

reject_control_chars SCHEDULE_SFTP_USERNAME "${SCHEDULE_SFTP_USERNAME}"
reject_control_chars SCHEDULE_SFTP_HOME_DIR "${SCHEDULE_SFTP_HOME_DIR}"

# authMethod=public-key uses SFTPGo's own "public_keys" user field
# instead of a password; authMethod=password (the only other value
# schedulefeed-deployment.yaml's own `fail` guard allows) requires
# SCHEDULE_SFTP_PASSWORD. The chart sets exactly one of the two, matching
# scheduleFeed.sftp.authMethod; docker-compose.yml sets the password.
if [ -n "${SCHEDULE_SFTP_PUBLIC_KEY:-}" ]; then
    # A key read from a file usually ends in a newline; the command
    # substitution drops trailing newlines.
    public_key="$(printf '%s' "${SCHEDULE_SFTP_PUBLIC_KEY}")"
    reject_control_chars SCHEDULE_SFTP_PUBLIC_KEY "${public_key}"
    AUTH_FIELD="\"public_keys\": [\"$(json_escape "${public_key}")\"]"
else
    : "${SCHEDULE_SFTP_PASSWORD:?SCHEDULE_SFTP_PASSWORD must be set when SCHEDULE_SFTP_PUBLIC_KEY is not}"
    reject_control_chars SCHEDULE_SFTP_PASSWORD "${SCHEDULE_SFTP_PASSWORD}"
    AUTH_FIELD="\"password\": \"$(json_escape "${SCHEDULE_SFTP_PASSWORD}")\""
fi
USERNAME_JSON="$(json_escape "${SCHEDULE_SFTP_USERNAME}")"
HOME_DIR_JSON="$(json_escape "${SCHEDULE_SFTP_HOME_DIR}")"

cat >"${LOADDATA_FILE}" <<EOF
{
  "version": 17,
  "users": [
    {
      "status": 1,
      "username": "${USERNAME_JSON}",
      "home_dir": "${HOME_DIR_JSON}",
      "permissions": {
        "/": ["*"]
      },
      ${AUTH_FIELD}
    }
  ]
}
EOF

exec sftpgo serve --loaddata-from "${LOADDATA_FILE}" --loaddata-mode 1
