{{/*
Chart name, overridable.
*/}}
{{- define "nr-status.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully-qualified release name. Every object in this chart derives its name
from this, so overriding it renames the whole install consistently.
*/}}
{{- define "nr-status.fullname" -}}
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
{{- define "nr-status.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels. Call as:
  {{- include "nr-status.selectorLabels" (dict "root" . "component" "api") }}
These land in an immutable `selector.matchLabels`, so nothing that changes
between releases (version, chart version) may appear here.
*/}}
{{- define "nr-status.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nr-status.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Full common label set. Call as:
  {{- include "nr-status.labels" (dict "root" . "component" "api") }}
*/}}
{{- define "nr-status.labels" -}}
helm.sh/chart: {{ include "nr-status.chart" .root }}
{{ include "nr-status.selectorLabels" . }}
{{- if .root.Chart.AppVersion }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: nr-status
{{- end }}

{{/*
ServiceAccount name. Takes root.
*/}}
{{- define "nr-status.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "nr-status.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference. Call as:
  {{ include "nr-status.image" (dict "root" . "image" .Values.api.image) }}
An empty `tag` falls back to the chart's appVersion.
*/}}
{{- define "nr-status.image" -}}
{{- printf "%s:%s" .image.repository (default .root.Chart.AppVersion .image.tag) }}
{{- end }}

{{/*
Per-component object names. Each takes root.
*/}}
{{- define "nr-status.postgresFullname" -}}
{{- printf "%s-postgres" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nr-status.apiFullname" -}}
{{- printf "%s-api" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nr-status.frontendFullname" -}}
{{- printf "%s-frontend" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nr-status.redisFullname" -}}
{{- printf "%s-redis" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
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
{{- define "nr-status.redisUrl" -}}
{{- if .Values.redis.enabled -}}
{{- printf "redis://%s:%d" (include "nr-status.redisFullname" .) (int .Values.redis.service.port) }}
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
{{- define "nr-status.apiBaseUrl" -}}
{{- printf "http://%s:%d" (include "nr-status.apiFullname" .) (int .Values.api.service.port) }}
{{- end }}

{{/*
Pod-level security context. Call as:
  {{- include "nr-status.podSecurityContext" (dict "override" .Values.api.podSecurityContext) | nindent 8 }}
The chart-wide defaults below are merged with the workload's own
`podSecurityContext` value, which wins on conflict. Postgres deliberately
does NOT use this helper -- it must pin uid/gid 999, see
postgres-statefulset.yaml.
*/}}
{{- define "nr-status.podSecurityContext" -}}
{{- $defaults := dict "runAsNonRoot" true "seccompProfile" (dict "type" "RuntimeDefault") -}}
{{- toYaml (mergeOverwrite $defaults (default (dict) .override | deepCopy)) }}
{{- end }}

{{/*
Container-level security context. Call as:
  {{- include "nr-status.containerSecurityContext" (dict "readOnlyRootFilesystem" true) | nindent 12 }}
The frontend passes false: `next start` writes its incremental cache under
.next/cache.
*/}}
{{- define "nr-status.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: {{ .readOnlyRootFilesystem }}
capabilities:
  drop:
    - ALL
{{- end }}

{{/*
Name of the Secret this chart renders. Takes root.
*/}}
{{- define "nr-status.secretName" -}}
{{- include "nr-status.fullname" . }}
{{- end }}

{{/*
Resolved Secret name/key for the postgres password. Takes root.
Used by postgres-statefulset.yaml (POSTGRES_PASSWORD) AND by
api-deployment.yaml / aggregator-deployment.yaml (PGPASSWORD). Because both
sides call these, an `existingSecret` override can never desynchronise them.
*/}}
{{- define "nr-status.postgresSecretName" -}}
{{- default (include "nr-status.secretName" .) .Values.postgresql.auth.existingSecret }}
{{- end }}

{{- define "nr-status.postgresSecretPasswordKey" -}}
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
{{- define "nr-status.internalTokenSecretName" -}}
{{- default (include "nr-status.secretName" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "nr-status.internalTokenSecretKey" -}}
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
{{- define "nr-status.ssoClientSecretName" -}}
{{- default (include "nr-status.secretName" .) .Values.api.sso.existingSecret }}
{{- end }}

{{- define "nr-status.ssoClientSecretKey" -}}
{{- if .Values.api.sso.existingSecret }}
{{- .Values.api.sso.existingSecretClientSecretKey }}
{{- else }}
{{- print "sso-client-secret" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for one poller's RDM API key. Call as:
  {{ include "nr-status.pollerSecretName" (dict "root" $ "poller" $p) }}
  {{ include "nr-status.pollerSecretKey" (dict "root" $ "name" $name "poller" $p) }}
*/}}
{{- define "nr-status.pollerSecretName" -}}
{{- default (include "nr-status.secretName" .root) .poller.existingSecret }}
{{- end }}

{{- define "nr-status.pollerSecretKey" -}}
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
{{- define "nr-status.databaseEnv" -}}
{{- if .Values.postgresql.enabled -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "nr-status.postgresSecretName" . }}
      key: {{ include "nr-status.postgresSecretPasswordKey" . }}
- name: DATABASE_URL
  value: {{ printf "postgres://%s:$(PGPASSWORD)@%s:%d/%s" .Values.postgresql.auth.username (include "nr-status.postgresFullname" .) (int .Values.postgresql.service.port) .Values.postgresql.auth.database | quote }}
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
how api's Deployment and Service already share nr-status.apiFullname); the
worker Deployment and dedicated Postgres each get their own.
*/}}
{{- define "nr-status.devAuthentikFullname" -}}
{{- printf "%s-devauthentik" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nr-status.devAuthentikWorkerFullname" -}}
{{- printf "%s-devauthentik-worker" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nr-status.devAuthentikPostgresFullname" -}}
{{- printf "%s-devauthentik-postgres" (include "nr-status.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Resolved Secret name/keys for devAuthentik's own AUTHENTIK_SECRET_KEY and
its dedicated Postgres password. Takes root. Unlike the chart's other
secret pairs there is no existingSecret override -- see values.yaml's
devAuthentik.secretKey comment for why. Always the chart-rendered Secret,
devauthentik-secret.yaml.
*/}}
{{- define "nr-status.devAuthentikSecretName" -}}
{{- printf "%s-devauthentik" (include "nr-status.secretName" .) }}
{{- end }}

{{- define "nr-status.devAuthentikSecretKeySecretKey" -}}
{{- print "authentik-secret-key" }}
{{- end }}

{{- define "nr-status.devAuthentikPostgresSecretKey" -}}
{{- print "authentik-postgres-password" }}
{{- end }}

{{/*
Fixed, blueprint-provisioned OIDC client id/secret for the local dev IdP --
IDENTICAL literal values to authentik-blueprints/oauth2-client.yaml (the
compose path's blueprint, Task 1) and its byte-for-byte chart copy at
charts/nr-status/files/devauthentik-blueprints/oauth2-client.yaml (Task 6).
Not secret in any meaningful sense -- known-in-advance, dev-only, committed
to git in both places -- but still routed through secret.yaml's
sso-client-secret entry for clientSecret rather than inlined directly in a
Deployment spec, matching this chart's usual posture for anything named
"secret" even when its value isn't sensitive.
*/}}
{{- define "nr-status.devAuthentikClientId" -}}
{{- print "nr-status-dev" }}
{{- end }}

{{- define "nr-status.devAuthentikClientSecret" -}}
{{- print "nr-status-dev-local-only-not-a-real-secret" }}
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
{{- define "nr-status.devAuthentikIssuerUrl" -}}
{{- printf "http://%s:%d/application/o/nr-status/" .Values.devAuthentik.hostname (int .Values.devAuthentik.service.port) }}
{{- end }}

{{- define "nr-status.devAuthentikRedirectUrl" -}}
{{- print "http://localhost:3000/api/auth/callback" }}
{{- end }}

{{- define "nr-status.devAuthentikPostLoginRedirectUrl" -}}
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
{{- define "nr-status.devAuthentikHostAliasIP" -}}
{{- if .Values.devAuthentik.hostAliasIP -}}
{{- .Values.devAuthentik.hostAliasIP -}}
{{- else -}}
{{- $svc := lookup "v1" "Service" .Release.Namespace (include "nr-status.devAuthentikFullname" .) -}}
{{- if $svc -}}
{{- $svc.spec.clusterIP -}}
{{- end -}}
{{- end -}}
{{- end }}
