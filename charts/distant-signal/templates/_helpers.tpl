{{/*
Chart name, overridable.
*/}}
{{- define "distant-signal.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully-qualified release name. Every object in this chart derives its name
from this, so overriding it renames the whole install consistently.
*/}}
{{- define "distant-signal.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name-and-version for the helm.sh/chart label.
*/}}
{{- define "distant-signal.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels. Call as:
  {{- include "distant-signal.selectorLabels" (dict "root" . "component" "api") }}
These land in an immutable `selector.matchLabels`, so nothing that changes
between releases (version, chart version) may appear here.
*/}}
{{- define "distant-signal.selectorLabels" -}}
app.kubernetes.io/name: {{ include "distant-signal.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Full common label set. Call as:
  {{- include "distant-signal.labels" (dict "root" . "component" "api") }}
*/}}
{{- define "distant-signal.labels" -}}
helm.sh/chart: {{ include "distant-signal.chart" .root }}
{{ include "distant-signal.selectorLabels" . }}
{{- if .root.Chart.AppVersion }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: distant-signal
{{- end }}

{{/*
ServiceAccount name. Takes root.
*/}}
{{- define "distant-signal.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "distant-signal.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference. Call as:
  {{ include "distant-signal.image" (dict "root" . "image" .Values.api.image) }}
An empty `tag` falls back to the chart's appVersion.
*/}}
{{- define "distant-signal.image" -}}
{{- printf "%s:%s" .image.repository (default .root.Chart.AppVersion .image.tag) }}
{{- end }}

{{/*
Per-component object names. Each takes root.
*/}}
{{- define "distant-signal.postgresFullname" -}}
{{- printf "%s-postgres" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.apiFullname" -}}
{{- printf "%s-api" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.frontendFullname" -}}
{{- printf "%s-frontend" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.redisFullname" -}}
{{- printf "%s-redis" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Redis URL, consumed by both api (publisher) and enricher (consumer). Takes
root.

Mirrors the `postgresql.enabled` / `externalDatabase` contract below: the
bundled Redis is used when `redis.enabled`, an operator-supplied
`redis.externalUrl` otherwise, and disabling the bundled Redis without
supplying one aborts the render rather than silently pointing both
workloads at a Service that was never created. Unlike DATABASE_URL there is
no existingSecret form -- a Redis URL for a disposable trigger queue carries
no credential the chart needs to keep out of the Deployment spec; an
operator who does need one can point `redis.externalUrl` at a URL with
inline auth, accepting that it is visible in the rendered Deployment.
*/}}
{{- define "distant-signal.redisUrl" -}}
{{- if .Values.redis.enabled -}}
{{- printf "redis://%s:%d" (include "distant-signal.redisFullname" .) (int .Values.redis.service.port) }}
{{- else if .Values.redis.externalUrl -}}
{{- .Values.redis.externalUrl }}
{{- else -}}
{{- fail "redis.enabled is false but no external Redis is configured. Set redis.externalUrl (e.g. redis://redis.example.com:6379), or re-enable the bundled Redis with redis.enabled=true." -}}
{{- end -}}
{{- end }}

{{/*
In-cluster base URL of the api Service. Consumed by the frontend
(API_BASE_URL), by every poller (API_INGEST_URL / API_SAMPLE_STATIONS_URL)
and by the helm test pod. Takes root.
*/}}
{{- define "distant-signal.apiBaseUrl" -}}
{{- printf "http://%s:%d" (include "distant-signal.apiFullname" .) (int .Values.api.service.port) }}
{{- end }}

{{/*
Pod-level security context. Call as:
  {{- include "distant-signal.podSecurityContext" (dict "override" .Values.api.podSecurityContext) | nindent 8 }}
The chart-wide defaults below are merged with the workload's own
`podSecurityContext` value, which wins on conflict. Postgres deliberately
does NOT use this helper -- it must pin uid/gid 999, see
postgres-statefulset.yaml.
*/}}
{{- define "distant-signal.podSecurityContext" -}}
{{- $defaults := dict "runAsNonRoot" true "seccompProfile" (dict "type" "RuntimeDefault") -}}
{{- toYaml (mergeOverwrite $defaults (default (dict) .override | deepCopy)) }}
{{- end }}

{{/*
Container-level security context. Call as:
  {{- include "distant-signal.containerSecurityContext" (dict "readOnlyRootFilesystem" true) | nindent 12 }}
The frontend passes false: `next start` writes its incremental cache under
.next/cache.
*/}}
{{- define "distant-signal.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: {{ .readOnlyRootFilesystem }}
capabilities:
  drop:
    - ALL
{{- end }}

{{/*
Name of the Secret this chart renders. Takes root.
*/}}
{{- define "distant-signal.secretName" -}}
{{- include "distant-signal.fullname" . }}
{{- end }}

{{/*
Resolved Secret name/key for the postgres password. Takes root.
Used by postgres-statefulset.yaml (POSTGRES_PASSWORD) AND by
api-deployment.yaml / aggregator-deployment.yaml (PGPASSWORD). Because both
sides call these, an `existingSecret` override can never desynchronise them.
*/}}
{{- define "distant-signal.postgresSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.postgresql.auth.existingSecret }}
{{- end }}

{{- define "distant-signal.postgresSecretPasswordKey" -}}
{{- if .Values.postgresql.auth.existingSecret }}
{{- .Values.postgresql.auth.existingSecretPasswordKey }}
{{- else }}
{{- print "postgres-password" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for the shared internal token. Takes root.
Used by api-deployment.yaml and by all four poller deployments.
*/}}
{{- define "distant-signal.internalTokenSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "distant-signal.internalTokenSecretKey" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecretInternalTokenKey }}
{{- else }}
{{- print "internal-token" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for the OIDC client secret. Takes root.
Used by api-deployment.yaml (SSO_CLIENT_SECRET) and, for the name, by
secret.yaml's decision on whether to render the key at all. Same shape as
the internal-token pair above, except this one is never auto-generated -- a
random OAuth2 client secret would simply be rejected by the issuer, so it
is closer to the rdm-*-api-key / llm-api-key entries in that respect.
*/}}
{{- define "distant-signal.ssoClientSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.api.sso.existingSecret }}
{{- end }}

{{- define "distant-signal.ssoClientSecretKey" -}}
{{- if .Values.api.sso.existingSecret }}
{{- .Values.api.sso.existingSecretClientSecretKey }}
{{- else }}
{{- print "sso-client-secret" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for one poller's RDM API key. Call as:
  {{ include "distant-signal.pollerSecretName" (dict "root" $ "poller" $p) }}
  {{ include "distant-signal.pollerSecretKey" (dict "root" $ "name" $name "poller" $p) }}
*/}}
{{- define "distant-signal.pollerSecretName" -}}
{{- default (include "distant-signal.secretName" .root) .poller.existingSecret }}
{{- end }}

{{- define "distant-signal.pollerSecretKey" -}}
{{- if .poller.existingSecret }}
{{- .poller.existingSecretApiKeyKey }}
{{- else }}
{{- printf "rdm-%s-api-key" .name }}
{{- end }}
{{- end }}

{{/*
Environment entries giving a workload a working DATABASE_URL. Takes root.
Used identically by api-deployment.yaml and aggregator-deployment.yaml.

WHY THE $(PGPASSWORD) INDIRECTION: crates/api/src/data/config.rs and
crates/aggregator/src/config.rs both want ONE `DATABASE_URL` string that
contains the password -- there is no host/user/password-parts form. Writing
that string into env[].value would expose the password to anyone with
`get deployments`, a strictly wider audience than `get secrets`. Instead the
password is injected as its own secretKeyRef entry and referenced with
Kubernetes' `$(VAR)` syntax, which the kubelet expands at container start
from EARLIER entries in the SAME container's env list. Order therefore
matters: PGPASSWORD must come before DATABASE_URL. The password never
appears in the rendered Deployment.

CAVEAT (also in values.yaml and README.md): the chart cannot percent-encode
a password it never sees, so a password containing @ : / ? # [ ] % must be
percent-encoded by the operator. Generated passwords use randAlphaNum, so
the default path is never affected.
*/}}
{{- define "distant-signal.databaseEnv" -}}
{{- if .Values.postgresql.enabled -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "distant-signal.postgresSecretName" . }}
      key: {{ include "distant-signal.postgresSecretPasswordKey" . }}
- name: DATABASE_URL
  value: {{ printf "postgres://%s:$(PGPASSWORD)@%s:%d/%s" .Values.postgresql.auth.username (include "distant-signal.postgresFullname" .) (int .Values.postgresql.service.port) .Values.postgresql.auth.database | quote }}
{{- else if .Values.externalDatabase.existingSecret -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalDatabase.existingSecret }}
      key: {{ .Values.externalDatabase.existingSecretUrlKey }}
{{- else if .Values.externalDatabase.url -}}
- name: DATABASE_URL
  value: {{ .Values.externalDatabase.url | quote }}
{{- else -}}
{{- fail "postgresql.enabled is false but no external database is configured. Set externalDatabase.existingSecret (preferred) together with externalDatabase.existingSecretUrlKey, or set externalDatabase.url, or re-enable the bundled database with postgresql.enabled=true." -}}
{{- end -}}
{{- end }}

{{/*
Per-component devAuthentik object names. Each takes root. The server
Deployment and the NodePort Service in front of it share ONE name (matching
how api's Deployment and Service already share distant-signal.apiFullname); the
worker Deployment and dedicated Postgres each get their own.
*/}}
{{- define "distant-signal.devAuthentikFullname" -}}
{{- printf "%s-devauthentik" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.devAuthentikWorkerFullname" -}}
{{- printf "%s-devauthentik-worker" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.devAuthentikPostgresFullname" -}}
{{- printf "%s-devauthentik-postgres" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Resolved Secret name/keys for devAuthentik's own AUTHENTIK_SECRET_KEY and
its dedicated Postgres password. Takes root. Unlike the chart's other
secret pairs there is no existingSecret override -- see values.yaml's
devAuthentik.secretKey comment for why. Always the chart-rendered Secret,
devauthentik-secret.yaml.
*/}}
{{- define "distant-signal.devAuthentikSecretName" -}}
{{- printf "%s-devauthentik" (include "distant-signal.secretName" .) }}
{{- end }}

{{- define "distant-signal.devAuthentikSecretKeySecretKey" -}}
{{- print "authentik-secret-key" }}
{{- end }}

{{- define "distant-signal.devAuthentikPostgresSecretKey" -}}
{{- print "authentik-postgres-password" }}
{{- end }}

{{/*
Fixed, blueprint-provisioned OIDC client id/secret for the local dev IdP --
IDENTICAL literal values to authentik-blueprints/oauth2-client.yaml (the
compose path's blueprint, Task 1) and its byte-for-byte chart copy at
charts/distant-signal/files/devauthentik-blueprints/oauth2-client.yaml (Task 6).
Not secret in any meaningful sense -- known-in-advance, dev-only, committed
to git in both places -- but still routed through secret.yaml's
sso-client-secret entry for clientSecret rather than inlined directly in a
Deployment spec, matching this chart's usual posture for anything named
"secret" even when its value isn't sensitive.
*/}}
{{- define "distant-signal.devAuthentikClientId" -}}
{{- print "distant-signal-dev" }}
{{- end }}

{{- define "distant-signal.devAuthentikClientSecret" -}}
{{- print "distant-signal-dev-local-only-not-a-real-secret" }}
{{- end }}

{{/*
Computed api.sso.* defaults for when devAuthentik.enabled is true and the
corresponding api.sso.* value is left empty. Takes root. Consumed by
api-deployment.yaml's top-of-file local-variable block (Task 11), the only
caller.

redirectUrl/postLoginRedirectUrl assume the developer reaches the frontend
at exactly http://localhost:3000 -- e.g. via
`kubectl port-forward svc/<release>-frontend 3000:3000` -- the SAME fixed
strings docker-compose.authentik.yml's blueprint (Task 1) already commits
the redirect_uris to, since the blueprint's redirect_uris list is a fixed
literal either way. The design doc describes the computed-defaults
MECHANISM for the Helm path but does not spell out these two literal
strings; this plan resolves that gap by reusing the compose path's own
fixed values verbatim, so both deployment paths are interchangeable in a
developer's head -- matching the design's stated intent for the client
id/secret pair.
*/}}
{{- define "distant-signal.devAuthentikIssuerUrl" -}}
{{- printf "http://%s:%d/application/o/distant-signal/" .Values.devAuthentik.hostname (int .Values.devAuthentik.service.port) }}
{{- end }}

{{- define "distant-signal.devAuthentikRedirectUrl" -}}
{{- print "http://localhost:3000/api/auth/callback" }}
{{- end }}

{{- define "distant-signal.devAuthentikPostLoginRedirectUrl" -}}
{{- print "http://localhost:3000/" }}
{{- end }}

{{/*
IP for the api Deployment's hostAliases entry mapping devAuthentik.hostname
straight to devauthentik-service's ClusterIP. Takes root.
devAuthentik.hostAliasIP wins when set; otherwise `lookup`s the live
Service. Returns an empty string (NEVER a wrong value) when neither is
available -- see values.yaml's devAuthentik.hostAliasIP comment for the
from-scratch-install ordering gap this reflects. api-deployment.yaml (Task
11) omits the whole hostAliases entry when this returns empty, rather than
writing an IP of "".
*/}}
{{- define "distant-signal.devAuthentikHostAliasIP" -}}
{{- if .Values.devAuthentik.hostAliasIP -}}
{{- .Values.devAuthentik.hostAliasIP -}}
{{- else -}}
{{- $svc := lookup "v1" "Service" .Release.Namespace (include "distant-signal.devAuthentikFullname" .) -}}
{{- if $svc -}}
{{- $svc.spec.clusterIP -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Per-component scheduleFeed object names. Takes root. scheduleFeed is an
independent, fully opt-in subsystem (like devAuthentik above) that renders
ONE Deployment (an SFTP receiver + this app's own verifier, sharing a PVC --
see schedulefeed-deployment.yaml, Task 8) and its own Secret, so it gets its
own name/secret-name pair following devAuthentikFullname/
devAuthentikSecretName's exact pattern.
*/}}
{{- define "distant-signal.scheduleFeedFullname" -}}
{{- printf "%s-schedulefeed" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.scheduleFeedSecretName" -}}
{{- printf "%s-schedulefeed" (include "distant-signal.secretName" .) }}
{{- end }}

{{/*
Key name for THIS APP's own generated SSH host key within whichever Secret
holds it. Always the chart-rendered scheduleFeedSecretName's Secret UNLESS
an operator overrides the whole Secret by NAME via
scheduleFeed.sftp.existingSecretHostKey (a different Secret object
entirely, not a key-within-this-Secret override like the DTD credential
pair below) -- consuming that override is schedulefeed-deployment.yaml's
job (Task 8), so this helper only names the key, taking no root/dict
argument since the key name itself never varies.
*/}}
{{- define "distant-signal.scheduleFeedHostKeySecretKey" -}}
{{- print "ssh_host_ed25519_key" }}
{{- end }}

{{/*
Resolved Secret name/keys for the DTD account credential (password and/or
public key). Takes root. Same shape as distant-signal.pollerSecretName/
pollerSecretKey above: an operator-supplied scheduleFeed.sftp.existingSecret
wins for BOTH the password and the public key (they always live together in
one Secret, unlike the per-poller map, so no extra "name"/"poller" dict
indirection is needed -- root alone is enough, matching
distant-signal.postgresSecretName's simpler single-value shape instead).
*/}}
{{- define "distant-signal.scheduleFeedDtdPasswordSecretName" -}}
{{- default (include "distant-signal.scheduleFeedSecretName" .) .Values.scheduleFeed.sftp.existingSecret }}
{{- end }}

{{- define "distant-signal.scheduleFeedDtdPasswordSecretKey" -}}
{{- if .Values.scheduleFeed.sftp.existingSecret }}
{{- .Values.scheduleFeed.sftp.existingSecretPasswordKey }}
{{- else }}
{{- print "schedule-sftp-password" }}
{{- end }}
{{- end }}

{{- define "distant-signal.scheduleFeedDtdPublicKeySecretName" -}}
{{- default (include "distant-signal.scheduleFeedSecretName" .) .Values.scheduleFeed.sftp.existingSecret }}
{{- end }}

{{- define "distant-signal.scheduleFeedDtdPublicKeySecretKey" -}}
{{- if .Values.scheduleFeed.sftp.existingSecret }}
{{- .Values.scheduleFeed.sftp.existingSecretPublicKeyKey }}
{{- else }}
{{- print "schedule-sftp-dtd-public-key" }}
{{- end }}
{{- end }}

{{/*
DTD's landing folder, relative to its SFTP account's home directory --
scheduleFeed.sftp.destinationFolder, plus scheduleFeed.sftp.folderPath if
set. Takes root. One single place this path is assembled, consumed by both
schedulefeed-deployment.yaml (the ingest container's WATCH_DIR, which must
resolve to the same absolute path under /data/schedule-feed) and NOTES.txt
(the human-readable connection summary) -- so the two can never drift
apart the way two independently-written printf calls could.
*/}}
{{- define "distant-signal.scheduleFeedDestinationPath" -}}
{{/* toString first: an unquoted numeric-looking value (e.g. folderPath:
2026 for a year-based subfolder) parses as an int in both plain YAML and
`--set`, and Sprig's trimAll requires a real string -- confirmed by
`helm template` erroring on exactly this shape before this guard was
added. */}}
{{- $folder := .Values.scheduleFeed.sftp.destinationFolder | toString | trimAll "/" -}}
{{- $sub := .Values.scheduleFeed.sftp.folderPath | toString | trimAll "/" -}}
{{- if $sub -}}
{{- printf "%s/%s" $folder $sub -}}
{{- else -}}
{{- $folder -}}
{{- end -}}
{{- end }}
