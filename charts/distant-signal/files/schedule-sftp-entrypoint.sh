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
#   SCHEDULE_SFTP_PERMISSIONS   comma-separated SFTPGo permissions on "/"
#                               (default upload,overwrite,list)
#   SCHEDULE_SFTP_MAX_SESSIONS  simultaneous sessions for the account
#                               (default 2; 0 = unlimited)
#   SCHEDULE_SFTP_MAX_UPLOAD_FILE_SIZE  largest single upload, in bytes
#                               (default 0 = unlimited)
#   SCHEDULE_SFTP_PASSWORD_MIN_LENGTH  shortest acceptable password
#                               (default 24)
#   SCHEDULE_SFTP_PASSWORD_POLICY  "enforce" (default: refuse to start with
#                               a shorter password) or "warn"
#   SCHEDULE_SFTP_SAFELIST      space-separated IPs/CIDRs the defender never
#                               scores or bans and the rate limiter never
#                               limits (default none)
#
# Least privilege (2026-10-01): the account can only write files into its
# home directory. docs/schedule-feed-sftp.md has the evidence behind each
# permission (what DTD's JSch 0.1.54 client was seen to do).
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
#     username-keyed upsert: mode 1 leaves a user that already exists
#     untouched, mode 0 (used below since 2026-10-01) updates it to match
#     this file. The SQLite user store lives on the container's writable
#     layer, so in practice every start creates the account afresh ("adding
#     new user" in the log); mode 0 makes that a guarantee, so a permission
#     or password change here can never be silently ignored.
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
    # Password strength. The account is internet-facing and DTD can't pin
    # our host key, so the password is the whole defence. The chart's
    # generated one is 32 random alphanumerics (~190 bits); a value from
    # existingSecret (a sealed secret) can't be checked at render time, so
    # it is checked here. Only the length is reported, never the value.
    min_length="${SCHEDULE_SFTP_PASSWORD_MIN_LENGTH:-24}"
    case "${min_length}" in
        '' | *[!0-9]*)
            echo "sftp-entrypoint: SCHEDULE_SFTP_PASSWORD_MIN_LENGTH must be a non-negative integer; refusing to start" >&2
            exit 1
            ;;
        *) ;; # digits only: accept
    esac
    if [ "${#SCHEDULE_SFTP_PASSWORD}" -lt "${min_length}" ]; then
        case "${SCHEDULE_SFTP_PASSWORD_POLICY:-enforce}" in
            warn)
                echo "sftp-entrypoint: WARNING: the push account password is ${#SCHEDULE_SFTP_PASSWORD} characters, under the minimum of ${min_length}; rotate it" >&2
                ;;
            enforce)
                echo "sftp-entrypoint: the push account password is ${#SCHEDULE_SFTP_PASSWORD} characters, under the minimum of ${min_length}; refusing to start (rotate it, or set scheduleFeed.sftp.passwordPolicy.enforce=false to only warn)" >&2
                exit 1
                ;;
            *)
                echo "sftp-entrypoint: SCHEDULE_SFTP_PASSWORD_POLICY must be enforce or warn; refusing to start" >&2
                exit 1
                ;;
        esac
    fi
    AUTH_FIELD="\"password\": \"$(json_escape "${SCHEDULE_SFTP_PASSWORD}")\""
fi
USERNAME_JSON="$(json_escape "${SCHEDULE_SFTP_USERNAME}")"
HOME_DIR_JSON="$(json_escape "${SCHEDULE_SFTP_HOME_DIR}")"

# A non-negative integer setting, or exit.
require_count() {
    case "$2" in
        '' | *[!0-9]*)
            echo "sftp-entrypoint: $1 must be a non-negative integer; refusing to start" >&2
            exit 1
            ;;
        *) ;; # digits only: accept
    esac
}
MAX_SESSIONS="${SCHEDULE_SFTP_MAX_SESSIONS:-2}"
MAX_UPLOAD_FILE_SIZE="${SCHEDULE_SFTP_MAX_UPLOAD_FILE_SIZE:-0}"
require_count SCHEDULE_SFTP_MAX_SESSIONS "${MAX_SESSIONS}"
require_count SCHEDULE_SFTP_MAX_UPLOAD_FILE_SIZE "${MAX_UPLOAD_FILE_SIZE}"

# Permissions on the account's root, its home directory and the only place
# it can see. Only names SFTPGo defines are accepted, so nothing here needs
# JSON escaping. "*" (every permission, what this account had before
# 2026-10-01) is refused.
PERMISSIONS_JSON=""
old_ifs="${IFS}"
IFS=,
set -f # split on commas only, never glob
for permission in ${SCHEDULE_SFTP_PERMISSIONS:-upload,overwrite,list}; do
    case "${permission}" in
        list | download | upload | overwrite | delete | delete_files | delete_dirs | rename | rename_files | rename_dirs | create_dirs | create_symlinks | chmod | chown | chtimes | copy) ;;
        *)
            echo "sftp-entrypoint: SCHEDULE_SFTP_PERMISSIONS entry '${permission}' is not a single SFTPGo permission; refusing to start" >&2
            exit 1
            ;;
    esac
    PERMISSIONS_JSON="${PERMISSIONS_JSON:+${PERMISSIONS_JSON}, }\"${permission}\""
done
set +f
IFS="${old_ifs}"
if [ -z "${PERMISSIONS_JSON}" ]; then
    echo "sftp-entrypoint: SCHEDULE_SFTP_PERMISSIONS is empty; refusing to start" >&2
    exit 1
fi

# Login methods the account may NOT use: everything except its one
# credential. DTD's JSch client logs in with keyboard-interactive (seen
# 2026-09-30), which SFTPGo answers by asking for the account's password,
# so password mode keeps both "password" and "keyboard-interactive".
if [ -n "${SCHEDULE_SFTP_PUBLIC_KEY:-}" ]; then
    DENIED_LOGIN_METHODS='"password", "password-over-SSH", "keyboard-interactive", "publickey+password", "publickey+keyboard-interactive", "TLSCertificate", "TLSCertificate+password"'
else
    DENIED_LOGIN_METHODS='"publickey", "publickey+password", "publickey+keyboard-interactive", "TLSCertificate", "TLSCertificate+password"'
fi

# Defender/rate-limiter safe list (scheduleFeed.sftp.defender.safelist),
# space-separated IPs/CIDRs. Each becomes two SFTPGo IP list entries for SSH
# (protocols 1): type 2 (defender) in mode 1 (allow: never scored or banned)
# and type 3 (rate limiter safe list). Restricted to IP/CIDR characters, so
# nothing here needs JSON escaping.
IP_LISTS_JSON=""
set -f # split on spaces only, never glob
for cidr in ${SCHEDULE_SFTP_SAFELIST:-}; do
    case "${cidr}" in
        *[!0-9A-Fa-f:./]*)
            echo "sftp-entrypoint: SCHEDULE_SFTP_SAFELIST entry '${cidr}' is not an IP or CIDR; refusing to start" >&2
            exit 1
            ;;
        *) ;; # IP/CIDR characters only: accept
    esac
    for list_type in 2 3; do
        IP_LISTS_JSON="${IP_LISTS_JSON:+${IP_LISTS_JSON},}
    {\"ipornet\": \"${cidr}\", \"description\": \"scheduleFeed.sftp.defender.safelist\", \"type\": ${list_type}, \"mode\": 1, \"protocols\": 1}"
    done
done
set +f

cat >"${LOADDATA_FILE}" <<EOF
{
  "version": 17,
  "users": [
    {
      "status": 1,
      "username": "${USERNAME_JSON}",
      "home_dir": "${HOME_DIR_JSON}",
      "permissions": {
        "/": [${PERMISSIONS_JSON}]
      },
      "max_sessions": ${MAX_SESSIONS},
      "filters": {
        "denied_login_methods": [${DENIED_LOGIN_METHODS}],
        "denied_protocols": ["FTP", "DAV", "HTTP"],
        "max_upload_file_size": ${MAX_UPLOAD_FILE_SIZE}
      },
      ${AUTH_FIELD}
    }
  ],
  "ip_lists": [${IP_LISTS_JSON}
  ]
}
EOF

# The umask 077 above is only for the loaddata file. SFTPGo inherits this
# process's umask for every file DTD uploads, and schedule-ingest reads
# them as a different uid through the pod's shared group (fsGroup), so
# uploads must be group-readable: 027 gives 0640 files and 0750 dirs.
# Under 077 a newly created upload was 0600 and ingest got EACCES (seen
# with CORPUSExtract.json.gz on 2026-10-02); timetable_full.zip only
# worked because DTD overwrites it in place and it kept an older mode.
umask 027

# --loaddata-mode 0: see the header comment. --loaddata-clean deletes the
# file, which holds the credential, as soon as SFTPGo has loaded it.
exec sftpgo serve --loaddata-from "${LOADDATA_FILE}" --loaddata-mode 0 --loaddata-clean
