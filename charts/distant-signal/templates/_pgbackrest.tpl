{{/*
pgBackRest point-in-time recovery (postgresql.pgbackrest). Every helper here
takes root. Design: docs/superpowers/specs/2026-09-30-backup-and-observability-gaps-design.md,
item 1; runbook: docs/postgres-pitr.md.
*/}}

{{/*
True (non-empty) when pgBackRest renders at all: it needs the bundled
Postgres. Validates the required values, failing the render with a message
naming the value, the first time any template asks.
*/}}
{{- define "distant-signal.pgbackrestEnabled" -}}
{{- $pb := .Values.postgresql.pgbackrest -}}
{{- if $pb.enabled -}}
{{- if not .Values.postgresql.enabled -}}
{{- fail "postgresql.pgbackrest.enabled needs the bundled Postgres (postgresql.enabled: true). For an external database, set up its own backups." -}}
{{- end -}}
{{- if not (or $pb.image.tag $pb.image.digest) -}}
{{- fail "postgresql.pgbackrest.image.tag (or .digest) is required when postgresql.pgbackrest.enabled is true: the stable postgres-pgbackrest tag with its digest, e.g. \"pg16.15-pgbackrest2.59.1@sha256:...\". It never defaults to the chart appVersion, because a per-release image would restart Postgres on every deploy." -}}
{{- end -}}
{{- if not $pb.stanza -}}
{{- fail "postgresql.pgbackrest.stanza must not be empty." -}}
{{- end -}}
{{- if not (hasPrefix "/" (toString $pb.repo.path)) -}}
{{- fail "postgresql.pgbackrest.repo.path is required when postgresql.pgbackrest.enabled is true, and must be absolute (e.g. /mine-bringer/pgbackrest/distant-signal). pgBackRest's expire deletes under it." -}}
{{- end -}}
{{- if eq (trimSuffix "/" (toString $pb.repo.path)) "" -}}
{{- fail "postgresql.pgbackrest.repo.path must not be the bucket root: pgBackRest's expire deletes under it." -}}
{{- end -}}
{{- range $key := list "endpoint" "bucket" "existingSecret" -}}
{{- if not (get $pb.repo.s3 $key) -}}
{{- fail (printf "postgresql.pgbackrest.repo.s3.%s is required when postgresql.pgbackrest.enabled is true." $key) -}}
{{- end -}}
{{- end -}}
{{- if contains "://" (toString $pb.repo.s3.endpoint) -}}
{{- fail "postgresql.pgbackrest.repo.s3.endpoint is a host name without a scheme (e.g. ra.tail3e0c1e.ts.net); set the port in repo.s3.port." -}}
{{- end -}}
{{- if not (has (toString $pb.repo.s3.uriStyle) (list "path" "host")) -}}
{{- fail "postgresql.pgbackrest.repo.s3.uriStyle must be \"path\" or \"host\"." -}}
{{- end -}}
{{- if not $pb.repo.cipher.type -}}
{{- fail "postgresql.pgbackrest.repo.cipher.type must be set (aes-256-cbc): the repository is always encrypted client-side." -}}
{{- end -}}
{{- if lt (int64 $pb.repo.retentionFull) 1 -}}
{{- fail "postgresql.pgbackrest.repo.retentionFull must be at least 1." -}}
{{- end -}}
{{- if and $pb.archive.queueMax (not $pb.archive.async) -}}
{{- fail "postgresql.pgbackrest.archive.queueMax needs archive.async: true (archive-push-queue-max only applies to async archiving). Set queueMax to \"\" to run without the disk-full guard." -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
A single-unit Prometheus duration ("90s", "15m", "26h", "8d", "1w") as
whole seconds, for comparing against a timestamp difference in PromQL.
Takes the duration string.
*/}}
{{- define "distant-signal.durationSeconds" -}}
{{- $d := toString . -}}
{{- if not (regexMatch "^[0-9]+[smhdw]$" $d) -}}
{{- fail (printf "%q is not a single-unit duration such as 90s, 15m, 26h, 8d or 1w (metrics.prometheusRule.pgbackrest)." $d) -}}
{{- end -}}
{{- $n := int64 (trimSuffix (substr (sub (len $d) 1 | int) (len $d) $d) $d) -}}
{{- $unit := get (dict "s" 1 "m" 60 "h" 3600 "d" 86400 "w" 604800) (substr (sub (len $d) 1 | int) (len $d) $d) -}}
{{- mul $n $unit -}}
{{- end }}

{{/*
The Postgres container image: the pgBackRest image when enabled, else
postgresql.image. Unlike distant-signal.image, never falls back to the
chart appVersion (see postgresql.pgbackrest.image.tag).
*/}}
{{- define "distant-signal.postgresImage" -}}
{{- if include "distant-signal.pgbackrestEnabled" . -}}
{{- include "distant-signal.image" (dict "root" . "image" .Values.postgresql.pgbackrest.image) -}}
{{- else -}}
{{- include "distant-signal.image" (dict "root" . "image" .Values.postgresql.image) -}}
{{- end -}}
{{- end }}

{{- define "distant-signal.postgresImagePullPolicy" -}}
{{- if include "distant-signal.pgbackrestEnabled" . -}}
{{- .Values.postgresql.pgbackrest.image.pullPolicy -}}
{{- else -}}
{{- .Values.postgresql.image.pullPolicy -}}
{{- end -}}
{{- end }}

{{/*
postgresql.config plus, with pgBackRest on, the archiving settings (which
win over the same keys in config). Returned as YAML; fromYaml it.
*/}}
{{- define "distant-signal.postgresConfig" -}}
{{- $config := deepCopy (default (dict) .Values.postgresql.config) -}}
{{- if include "distant-signal.pgbackrestEnabled" . -}}
{{- $pb := .Values.postgresql.pgbackrest -}}
{{- $_ := set $config "archive_mode" "on" -}}
{{- $_ := set $config "archive_command" (printf "pgbackrest --stanza=%s archive-push %%p" $pb.stanza) -}}
{{- $_ := set $config "archive_timeout" (int64 $pb.archive.timeoutSecs) -}}
{{- end -}}
{{- toYaml $config -}}
{{- end }}

{{/*
Where pgBackRest's async spool lives: on the data volume, beside PGDATA
(/var/lib/postgresql/data/pgdata) and not inside it, so a restore that
replaces PGDATA leaves it alone and it survives a pod restart.
*/}}
{{- define "distant-signal.pgbackrestSpoolPath" -}}
/var/lib/postgresql/data/pgbackrest-spool
{{- end }}

{{/*
PGBACKREST_* env for the Postgres container. `kubectl exec` runs pgBackRest
with the same env, so archive_command, the CronJobs' commands and a manual
`pgbackrest restore` in a one-off pod all read the same configuration.
*/}}
{{- define "distant-signal.pgbackrestEnv" -}}
{{- $pb := .Values.postgresql.pgbackrest -}}
{{- $s3 := $pb.repo.s3 -}}
{{- $cipherSecret := default $s3.existingSecret $pb.repo.cipher.existingSecret -}}
- name: PGBACKREST_STANZA
  value: {{ $pb.stanza | quote }}
- name: PGBACKREST_PG1_PATH
  value: /var/lib/postgresql/data/pgdata
- name: PGBACKREST_PG1_PORT
  value: {{ .Values.postgresql.service.port | quote }}
# The official image makes POSTGRES_USER the superuser; there is no
# `postgres` role. Local socket, trust auth (the image's pg_hba.conf).
- name: PGBACKREST_PG1_USER
  value: {{ .Values.postgresql.auth.username | quote }}
- name: PGBACKREST_REPO1_TYPE
  value: s3
- name: PGBACKREST_REPO1_PATH
  value: {{ $pb.repo.path | quote }}
- name: PGBACKREST_REPO1_S3_ENDPOINT
  value: {{ $s3.endpoint | quote }}
- name: PGBACKREST_REPO1_STORAGE_PORT
  value: {{ $s3.port | quote }}
{{- with $s3.storageHost }}
- name: PGBACKREST_REPO1_STORAGE_HOST
  value: {{ . | quote }}
{{- end }}
- name: PGBACKREST_REPO1_STORAGE_VERIFY_TLS
  value: {{ ternary "y" "n" (ne (toString $s3.verifyTls) "false") | quote }}
- name: PGBACKREST_REPO1_S3_BUCKET
  value: {{ $s3.bucket | quote }}
- name: PGBACKREST_REPO1_S3_REGION
  value: {{ $s3.region | quote }}
- name: PGBACKREST_REPO1_S3_URI_STYLE
  value: {{ $s3.uriStyle | quote }}
- name: PGBACKREST_REPO1_S3_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $s3.existingSecret | quote }}
      key: {{ $s3.accessKeyIdKey | quote }}
- name: PGBACKREST_REPO1_S3_KEY_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ $s3.existingSecret | quote }}
      key: {{ $s3.secretAccessKeyKey | quote }}
- name: PGBACKREST_REPO1_CIPHER_TYPE
  value: {{ $pb.repo.cipher.type | quote }}
- name: PGBACKREST_REPO1_CIPHER_PASS
  valueFrom:
    secretKeyRef:
      name: {{ $cipherSecret | quote }}
      key: {{ $pb.repo.cipher.passphraseKey | quote }}
- name: PGBACKREST_REPO1_RETENTION_FULL_TYPE
  value: {{ $pb.repo.retentionFullType | quote }}
- name: PGBACKREST_REPO1_RETENTION_FULL
  value: {{ int64 $pb.repo.retentionFull | quote }}
- name: PGBACKREST_REPO1_BUNDLE
  value: {{ ternary "y" "n" (ne (toString $pb.repo.bundle) "false") | quote }}
- name: PGBACKREST_COMPRESS_TYPE
  value: {{ $pb.compress.type | quote }}
- name: PGBACKREST_COMPRESS_LEVEL
  value: {{ int64 $pb.compress.level | quote }}
- name: PGBACKREST_PROCESS_MAX
  value: {{ int64 $pb.processMax | quote }}
- name: PGBACKREST_ARCHIVE_ASYNC
  value: {{ ternary "y" "n" (ne (toString $pb.archive.async) "false") | quote }}
- name: PGBACKREST_SPOOL_PATH
  value: {{ include "distant-signal.pgbackrestSpoolPath" . | quote }}
{{- with $pb.archive.queueMax }}
- name: PGBACKREST_ARCHIVE_PUSH_QUEUE_MAX
  value: {{ . | quote }}
{{- end }}
# No log files (nothing rotates them); warnings and errors go to the
# console, i.e. the Postgres log for archive-push. The CronJobs raise the
# console level to info for their own commands.
- name: PGBACKREST_LOG_LEVEL_FILE
  value: "off"
- name: PGBACKREST_LOG_LEVEL_CONSOLE
  value: warn
- name: PGBACKREST_LOG_LEVEL_STDERR
  value: "off"
{{- end }}
