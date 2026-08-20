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
In-cluster Redis URL, consumed by both api (publisher) and enricher
(consumer). Takes root.
*/}}
{{- define "nr-status.redisUrl" -}}
{{- printf "redis://%s:%d" (include "nr-status.redisFullname" .) (int .Values.redis.service.port) }}
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
