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
An empty `tag` falls back to the chart's appVersion. An explicit
`.image.digest` (e.g. `sha256:...`) takes priority over both: when set, the
rendered reference is `<repo>@<digest>` and `tag`/appVersion are ignored
entirely -- `repo:tag@digest` is valid OCI syntax, but the digest silently
wins on pull anyway, so rendering a tag alongside it would just be
misleading. Only this chart's first-party, CI-built images (api,
aggregator, notifier, enricher, frontend, the pollers, trustConsumer,
fullCoverageConsumer, trustBacklogConsumer, movementRelay,
pollerIrishRailGtfs/Live, pollerNirStations, scheduleFeed.ingest/reference)
carry a `digest` field at all -- see each's own `image.digest` values.yaml
comment (canonical copy: `api.image.digest`) and
`.github/workflows/containers.yml`'s `push-helm-chart` job for how it gets
populated in CI.

When `.Values.global.imageRegistry` is set, it replaces whatever comes
before the last two `/`-separated segments of `.image.repository` (e.g.
`ghcr.io/fasterspeeding/distant-signal/api` -> keep `distant-signal/api`,
prepend the override), so a single
`--set global.imageRegistry=registry.example.com/myfork` re-points every
image this chart renders -- including postgres/redis/devAuthentik/
sftpgo -- at a private mirror without touching each service's own
`image.repository`. A repository with two or fewer segments (e.g. the
bundled `postgres`/`redis` defaults) is kept whole and the registry is
just prepended in front of it. This override logic is shared by both the
digest and tag branches below. NOTE: this chart no longer builds/deploys
the derived MCP service ("distant-signal-mcp") itself, so there is no
railMcp image to re-point here any more -- see railMcp's own values.yaml
comment for how to link this chart's frontend to a separately, externally
deployed instance instead.
*/}}
{{- define "distant-signal.image" -}}
{{- $repo := .image.repository -}}
{{- if .root.Values.global.imageRegistry -}}
{{- $parts := splitList "/" $repo -}}
{{- if gt (len $parts) 2 -}}
{{- $repo = join "/" (slice $parts (sub (len $parts) 2) (len $parts)) -}}
{{- end -}}
{{- $repo = printf "%s/%s" .root.Values.global.imageRegistry $repo -}}
{{- end -}}
{{- if .image.digest -}}
{{- printf "%s@%s" $repo .image.digest }}
{{- else -}}
{{- printf "%s:%s" $repo (default .root.Chart.AppVersion .image.tag) }}
{{- end -}}
{{- end }}

{{/*
Per-component object names. Each takes root.
*/}}
{{- define "distant-signal.postgresFullname" -}}
{{- printf "%s-postgres" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
One postgresql.config value, rendered as Postgres expects it after
`-c name=`. YAML hands Helm whole numbers as float64 (so a bare 100000000
would print as 1e+08) and YAML 1.1 turns a bare on/off into a bool: whole
floats print as integers and bools as on/off. Everything else, including
unit strings like 512MB, prints as-is. Takes the value.
*/}}
{{- define "distant-signal.postgresConfValue" -}}
{{- if kindIs "bool" . -}}
{{- ternary "on" "off" . -}}
{{- else if and (kindIs "float64" .) (eq (float64 (int64 .)) .) -}}
{{- int64 . -}}
{{- else -}}
{{- toString . -}}
{{- end -}}
{{- end }}

{{- define "distant-signal.apiFullname" -}}
{{- printf "%s-api" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "distant-signal.frontendFullname" -}}
{{- printf "%s-frontend" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
The frontend's pinned public origin (NEXT_PUBLIC_SITE_URL). frontend.siteUrl
wins when set; otherwise, when the chart itself publishes the frontend via
ingress, the origin is DERIVED from ingress.frontend.host (https when
ingress.tls is set -- the same expression NOTES.txt uses for the UI URL), so
share/invite links are pinned to an operator-configured value rather than
falling back to each request's Host/X-Forwarded-Proto headers. Empty only
when neither is available (e.g. port-forward-only installs), in which case
NOTES.txt prints a warning.
*/}}
{{- define "distant-signal.frontendSiteUrl" -}}
{{- if .Values.frontend.siteUrl -}}
{{- .Values.frontend.siteUrl -}}
{{- else if and .Values.ingress.enabled .Values.ingress.frontend.enabled .Values.ingress.frontend.host -}}
{{- printf "http%s://%s" (ternary "s" "" (not (empty .Values.ingress.tls))) .Values.ingress.frontend.host -}}
{{- end -}}
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
Resolved Secret name/key for the VAPID public/private keypair. Takes root.
Used by notifier-deployment.yaml (both keys) and api-deployment.yaml (the
public key only -- api serves it via /public/notifications/vapid-public-key
but never signs a push itself, so it never needs the private key). Same
existingSecret/name/key shape as the trust-consumer/poller-oauth pairs
below, except -- like sso-client-secret and kafka-sasl-* -- never
auto-generated: a random VAPID keypair would not be a genuine matched EC
keypair (see secret.yaml's own comment).
*/}}
{{- define "distant-signal.vapidPublicKeySecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.notifier.vapid.existingSecret }}
{{- end }}

{{- define "distant-signal.vapidPublicKeySecretKey" -}}
{{- if .Values.notifier.vapid.existingSecret }}
{{- .Values.notifier.vapid.existingSecretPublicKeyKey }}
{{- else }}
{{- print "vapid-public-key" }}
{{- end }}
{{- end }}

{{- define "distant-signal.vapidPrivateKeySecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.notifier.vapid.existingSecret }}
{{- end }}

{{- define "distant-signal.vapidPrivateKeySecretKey" -}}
{{- if .Values.notifier.vapid.existingSecret }}
{{- .Values.notifier.vapid.existingSecretPrivateKeyKey }}
{{- else }}
{{- print "vapid-private-key" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for the OIDC client secret. Takes root.
Used by api-deployment.yaml (SSO_CLIENT_SECRET) and, for the name, by
secret.yaml's decision on whether to render the key at all. Same
existingSecret/name/key shape as the poller-oauth pair below, except this
one is never auto-generated -- a random OAuth2 client secret would simply
be rejected by the issuer, so it is closer to the rdm-*-api-key /
llm-api-key / internal-oauth-* entries in that respect.
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
Resolved Secret name/key for one poller's own OAuth2 username/password
(distinct from its RDM apiKey, same Secret object, same existingSecret
toggle). Call as:
  {{ include "distant-signal.pollerSecretName" (dict "root" $ "poller" $p) }}
  {{ include "distant-signal.pollerOauthUsernameSecretKey" (dict "root" $ "name" $name "poller" $p) }}
  {{ include "distant-signal.pollerOauthPasswordSecretKey" (dict "root" $ "name" $name "poller" $p) }}
*/}}
{{- define "distant-signal.pollerOauthUsernameSecretKey" -}}
{{- if .poller.existingSecret }}
{{- .poller.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- printf "internal-oauth-username-poller-%s" .name }}
{{- end }}
{{- end }}

{{- define "distant-signal.pollerOauthPasswordSecretKey" -}}
{{- if .poller.existingSecret }}
{{- .poller.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- printf "internal-oauth-password-poller-%s" .name }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for trust-consumer's own OAuth2 credential.
*/}}
{{- define "distant-signal.trustConsumerSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.trustConsumer.existingSecret }}
{{- end }}

{{- define "distant-signal.trustConsumerOauthUsernameSecretKey" -}}
{{- if .Values.trustConsumer.existingSecret }}
{{- .Values.trustConsumer.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-trust-consumer" }}
{{- end }}
{{- end }}

{{- define "distant-signal.trustConsumerOauthPasswordSecretKey" -}}
{{- if .Values.trustConsumer.existingSecret }}
{{- .Values.trustConsumer.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-trust-consumer" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for full-coverage-consumer's own OAuth2
credential -- own service account, per Decision 5, mirroring
trust-consumer's own pattern above.
*/}}
{{- define "distant-signal.fullCoverageConsumerSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.fullCoverageConsumer.existingSecret }}
{{- end }}

{{- define "distant-signal.fullCoverageConsumerOauthUsernameSecretKey" -}}
{{- if .Values.fullCoverageConsumer.existingSecret }}
{{- .Values.fullCoverageConsumer.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-full-coverage-consumer" }}
{{- end }}
{{- end }}

{{- define "distant-signal.fullCoverageConsumerOauthPasswordSecretKey" -}}
{{- if .Values.fullCoverageConsumer.existingSecret }}
{{- .Values.fullCoverageConsumer.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-full-coverage-consumer" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for trust-backlog-consumer's own OAuth2
credential -- own service account, mirroring full-coverage-consumer's own
pattern above (docs/superpowers/plans/2026-09-05-trust-event-backlog-plan.md
Task 12).
*/}}
{{- define "distant-signal.trustBacklogConsumerSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.trustBacklogConsumer.existingSecret }}
{{- end }}

{{- define "distant-signal.trustBacklogConsumerOauthUsernameSecretKey" -}}
{{- if .Values.trustBacklogConsumer.existingSecret }}
{{- .Values.trustBacklogConsumer.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-trust-backlog-consumer" }}
{{- end }}
{{- end }}

{{- define "distant-signal.trustBacklogConsumerOauthPasswordSecretKey" -}}
{{- if .Values.trustBacklogConsumer.existingSecret }}
{{- .Values.trustBacklogConsumer.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-trust-backlog-consumer" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name for movement-relay's Kafka SASL credential --
mirrors trustConsumerSecretName's exact shape. No OAuth username/password
secret keys needed here (unlike every other crate in this repo):
movement-relay never calls api, so it has no internal-OAuth credential at
all.
*/}}
{{- define "distant-signal.movementRelaySecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.movementRelay.kafka.existingSecret }}
{{- end }}

{{/*
Resolved Secret name/key for schedule-ingest's / schedule-reference's own
OAuth2 credentials -- two independent service accounts sharing one Pod,
each with its own existingSecret toggle.
*/}}
{{- define "distant-signal.scheduleIngestSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.scheduleFeed.ingest.existingSecret }}
{{- end }}

{{- define "distant-signal.scheduleIngestOauthUsernameSecretKey" -}}
{{- if .Values.scheduleFeed.ingest.existingSecret }}
{{- .Values.scheduleFeed.ingest.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-schedule-ingest" }}
{{- end }}
{{- end }}

{{- define "distant-signal.scheduleIngestOauthPasswordSecretKey" -}}
{{- if .Values.scheduleFeed.ingest.existingSecret }}
{{- .Values.scheduleFeed.ingest.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-schedule-ingest" }}
{{- end }}
{{- end }}

{{- define "distant-signal.scheduleReferenceSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.scheduleFeed.reference.existingSecret }}
{{- end }}

{{- define "distant-signal.scheduleReferenceOauthUsernameSecretKey" -}}
{{- if .Values.scheduleFeed.reference.existingSecret }}
{{- .Values.scheduleFeed.reference.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-schedule-reference" }}
{{- end }}
{{- end }}

{{- define "distant-signal.scheduleReferenceOauthPasswordSecretKey" -}}
{{- if .Values.scheduleFeed.reference.existingSecret }}
{{- .Values.scheduleFeed.reference.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-schedule-reference" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for poller-irish-rail-gtfs's own OAuth2 credential
-- same shape as scheduleIngest/scheduleReference above (its own
existingSecret toggle, falling back to the shared chart-rendered Secret).
*/}}
{{- define "distant-signal.pollerIrishRailGtfsSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.pollerIrishRailGtfs.existingSecret }}
{{- end }}

{{- define "distant-signal.pollerIrishRailGtfsOauthUsernameSecretKey" -}}
{{- if .Values.pollerIrishRailGtfs.existingSecret }}
{{- .Values.pollerIrishRailGtfs.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-poller-irish-rail-gtfs" }}
{{- end }}
{{- end }}

{{- define "distant-signal.pollerIrishRailGtfsOauthPasswordSecretKey" -}}
{{- if .Values.pollerIrishRailGtfs.existingSecret }}
{{- .Values.pollerIrishRailGtfs.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-poller-irish-rail-gtfs" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for poller-irish-rail-live's own OAuth2
credential -- same shape as pollerIrishRailGtfs above.
*/}}
{{- define "distant-signal.pollerIrishRailLiveSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.pollerIrishRailLive.existingSecret }}
{{- end }}

{{- define "distant-signal.pollerIrishRailLiveOauthUsernameSecretKey" -}}
{{- if .Values.pollerIrishRailLive.existingSecret }}
{{- .Values.pollerIrishRailLive.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-poller-irish-rail-live" }}
{{- end }}
{{- end }}

{{- define "distant-signal.pollerIrishRailLiveOauthPasswordSecretKey" -}}
{{- if .Values.pollerIrishRailLive.existingSecret }}
{{- .Values.pollerIrishRailLive.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-poller-irish-rail-live" }}
{{- end }}
{{- end }}

{{/*
Resolved Secret name/key for poller-nir-stations's own OAuth2 credential --
same shape as pollerIrishRailGtfs/pollerIrishRailLive above.
*/}}
{{- define "distant-signal.pollerNirStationsSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.pollerNirStations.existingSecret }}
{{- end }}

{{- define "distant-signal.pollerNirStationsOauthUsernameSecretKey" -}}
{{- if .Values.pollerNirStations.existingSecret }}
{{- .Values.pollerNirStations.existingSecretInternalOauthUsernameKey }}
{{- else }}
{{- print "internal-oauth-username-poller-nir-stations" }}
{{- end }}
{{- end }}

{{- define "distant-signal.pollerNirStationsOauthPasswordSecretKey" -}}
{{- if .Values.pollerNirStations.existingSecret }}
{{- .Values.pollerNirStations.existingSecretInternalOauthPasswordKey }}
{{- else }}
{{- print "internal-oauth-password-poller-nir-stations" }}
{{- end }}
{{- end }}

{{/*
Environment entries giving a workload a working DATABASE_URL, plus the pool
session settings from `databasePool` (DATABASE_*_SECS). Takes root. Used
identically by the api, aggregator, notifier and enricher Deployments.

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
{{- end }}
{{- /* Pool session settings, read by crates/common/src/pg.rs. */}}
- name: DATABASE_STATEMENT_TIMEOUT_SECS
  value: {{ .Values.databasePool.statementTimeoutSecs | int | quote }}
- name: DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_SECS
  value: {{ .Values.databasePool.idleInTransactionTimeoutSecs | int | quote }}
- name: DATABASE_ACQUIRE_TIMEOUT_SECS
  value: {{ .Values.databasePool.acquireTimeoutSecs | int | quote }}
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

{{/*
railMcp: frontend's own linking config for the derived MCP service
("distant-signal-mcp", a fork of train-mcp -- see
docs/superpowers/specs/2026-09-01-train-mcp-integration-design.md
Decision 1). This chart no longer deploys that service itself -- it is
expected to run as its own, separately-operated Helm release (that
project's own chart, or the fork directly). What's left here is only
frontend's own consumption of it: the shared internal-complete-token
Secret. Same existingSecret/existingSecretXKey pattern as
trustConsumerOauthUsernameSecretKey/pollerSecretKey -- one existingSecret
toggle for the whole component (railMcp.existingSecret), each key
individually overridable within it.
*/}}
{{- define "distant-signal.railMcpSecretName" -}}
{{- default (include "distant-signal.secretName" .) .Values.railMcp.existingSecret }}
{{- end }}

{{/* railmcp-internal-complete-token key resolution -- the two Discord
helpers this chart used to render here (railMcpDiscordClientIdSecretKey,
railMcpDiscordAllowedUserIdsSecretKey) are retired along with
railMcp.discord.* (embedded-chatbot-shared-foundation-and-option-c plan,
Task 8: "superseded, not stacked"). */}}
{{- define "distant-signal.railMcpInternalCompleteTokenSecretKey" -}}
{{- if .Values.railMcp.existingSecret }}
{{- .Values.railMcp.existingSecretInternalCompleteTokenKey }}
{{- else }}
{{- print "internal-complete-token" }}
{{- end }}
{{- end }}


{{/*
Cold-archive env for the aggregator (crates/aggregator/src/archive.rs).
Renders NOTHING when archive.enabled is false, so a default install carries
no ARCHIVE_* env and needs no S3 settings. When enabled, fails rendering on
a missing bucket/secret or a table the binary would refuse anyway.
*/}}
{{- define "distant-signal.archiveEnv" -}}
{{- if .Values.archive.enabled -}}
{{- $a := .Values.archive -}}
{{- if not $a.s3.bucket -}}
{{- fail "archive.enabled is true but archive.s3.bucket is empty." -}}
{{- end -}}
{{- if not $a.s3.existingSecret -}}
{{- fail "archive.enabled is true but archive.s3.existingSecret is empty. Create a Secret holding the S3 access key pair (keys archive.s3.accessKeyIdKey / archive.s3.secretAccessKeyKey) and name it here." -}}
{{- end -}}
{{- range $a.tables -}}
{{- if not (has . (list "trains")) -}}
{{- fail (printf "archive.tables entry %q is not archivable. Supported: trains. trust_event_backlog (TRUST licensing safeguard) and LDBWS-derived tables (300-day licence ceiling) are deliberately excluded." .) -}}
{{- end -}}
{{- end -}}
{{- if not (has $a.failurePolicy (list "retain" "delete")) -}}
{{- fail (printf "archive.failurePolicy must be retain or delete, got %q." $a.failurePolicy) -}}
{{- end -}}
- name: ARCHIVE_ENABLED
  value: "true"
- name: ARCHIVE_TABLES
  value: {{ join "," $a.tables | quote }}
- name: ARCHIVE_FAILURE_POLICY
  value: {{ $a.failurePolicy | quote }}
{{- with $a.s3.endpoint }}
- name: ARCHIVE_S3_ENDPOINT
  value: {{ . | quote }}
{{- end }}
- name: ARCHIVE_S3_BUCKET
  value: {{ $a.s3.bucket | quote }}
- name: ARCHIVE_S3_PREFIX
  value: {{ $a.s3.prefix | quote }}
- name: ARCHIVE_S3_REGION
  value: {{ $a.s3.region | quote }}
- name: ARCHIVE_S3_PATH_STYLE
  value: {{ $a.s3.pathStyle | quote }}
- name: ARCHIVE_S3_ALLOW_HTTP
  value: {{ $a.s3.allowHttp | quote }}
- name: ARCHIVE_S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ $a.s3.existingSecret }}
      key: {{ $a.s3.accessKeyIdKey }}
- name: ARCHIVE_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $a.s3.existingSecret }}
      key: {{ $a.s3.secretAccessKeyKey }}
{{- end -}}
{{- end }}

{{/*
Background-worker health (SVC-08/INF-9, INF-5): the `health` container port,
the HEALTH_BIND_URL/PROGRESS_STALL_SECS env vars are written out literally
in each template (the crates' chart_env_wiring_tests grep the template text),
and the probes come from here. Every worker serves both paths on
`port` (crates/health-http::spawn_worker):
  /livez   -- 503 only once one loop iteration has run longer than
              PROGRESS_STALL_SECS (idle time between iterations never counts);
              200 while the worker is still retrying its initial connection,
              so a liveness restart never adds CrashLoopBackOff delay.
  /healthz -- additionally 503 until the initial Postgres/Redis connection is
              up; used as the readiness probe by the DB-backed workers.
Usage:
  include "distant-signal.workerHealthProbes" (dict "root" $root "port" 8090 "readiness" true)
*/}}
{{- define "distant-signal.workerHealthProbes" -}}
{{- $h := .root.Values.workerHealth -}}
{{- if .readiness }}
readinessProbe:
  httpGet:
    path: /healthz
    port: {{ .port }}
  periodSeconds: {{ $h.readiness.periodSeconds }}
  timeoutSeconds: {{ $h.readiness.timeoutSeconds }}
  failureThreshold: {{ $h.readiness.failureThreshold }}
{{- end }}
livenessProbe:
  httpGet:
    path: /livez
    port: {{ .port }}
  initialDelaySeconds: {{ $h.liveness.initialDelaySeconds }}
  periodSeconds: {{ $h.liveness.periodSeconds }}
  timeoutSeconds: {{ $h.liveness.timeoutSeconds }}
  failureThreshold: {{ $h.liveness.failureThreshold }}
{{- end }}

{{/*
NetworkPolicy for one background worker (INF-10). Ingress: the worker's
/metrics port from networkPolicy.monitoringNamespace (when metrics.enabled),
and its health port(s) from anywhere, because kubelet probes come from the
node, which no pod or namespace selector can name (INF-9's side note). A
worker serves nothing else, so everything else is denied.

Egress (only when networkPolicy.egress.enabled and `egress` is given): DNS,
the in-cluster services the worker actually calls (flags below), the public
internet minus networkPolicy.egress.privateCidrs(V6) (RDM/Irish Rail feeds,
Kafka brokers, the OAuth token endpoint, Web Push services), and
networkPolicy.egress.extraRules.
Usage:
  include "distant-signal.workerNetworkPolicy" (dict
    "root" $root "component" "trust-consumer"
    "metricsPort" 9095 "healthPorts" (list 8081)
    "egress" (dict "api" true "redis" true "postgres" false))
*/}}
{{- define "distant-signal.workerNetworkPolicy" -}}
{{- $root := .root -}}
{{- $np := $root.Values.networkPolicy -}}
{{- $egressOn := and .egress ($np.egress).enabled -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ printf "%s-%s" (include "distant-signal.fullname" $root) .component | trunc 63 | trimSuffix "-" }}
  labels:
    {{- include "distant-signal.labels" (dict "root" $root "component" .component) | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "distant-signal.selectorLabels" (dict "root" $root "component" .component) | nindent 6 }}
  policyTypes:
    - Ingress
    {{- if $egressOn }}
    - Egress
    {{- end }}
  ingress:
    {{- if $root.Values.metrics.enabled }}
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $np.monitoringNamespace | quote }}
      ports:
        - protocol: TCP
          port: {{ .metricsPort }}
    {{- end }}
    - ports:
        {{- range .healthPorts }}
        - protocol: TCP
          port: {{ . }}
        {{- end }}
  {{- if $egressOn }}
  egress:
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    {{- if .egress.api }}
    - to:
        - podSelector:
            matchLabels:
              {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "api") | nindent 14 }}
      ports:
        - protocol: TCP
          port: {{ $root.Values.api.service.port }}
    {{- if $root.Values.devAuthentik.enabled }}
    # The bundled dev IdP serves the internal OAuth token endpoint.
    - to:
        - podSelector:
            matchLabels:
              {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "devauthentik-server") | nindent 14 }}
      ports:
        - protocol: TCP
          port: 9000
    {{- end }}
    {{- end }}
    {{- if and .egress.redis $root.Values.redis.enabled }}
    - to:
        - podSelector:
            matchLabels:
              {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "redis") | nindent 14 }}
      ports:
        - protocol: TCP
          port: {{ $root.Values.redis.service.port }}
    {{- end }}
    {{- if and .egress.postgres $root.Values.postgresql.enabled }}
    - to:
        - podSelector:
            matchLabels:
              {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "postgres") | nindent 14 }}
      ports:
        - protocol: TCP
          port: {{ $root.Values.postgresql.service.port }}
    {{- end }}
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            {{- with $np.egress.privateCidrs }}
            except:
              {{- toYaml . | nindent 14 }}
            {{- end }}
        - ipBlock:
            cidr: "::/0"
            {{- with $np.egress.privateCidrsV6 }}
            except:
              {{- toYaml . | nindent 14 }}
            {{- end }}
    {{- with $np.egress.extraRules }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  {{- end }}
{{- end }}

{{/*
The storage request for the chart-rendered Redis PVC (DQ15). Normally
redis.persistence.size. But a PVC's size can only grow, and only on a
StorageClass with allowVolumeExpansion: an upgrade that changes the request
on any other class (k3s local-path, for one) fails outright. So when the PVC
already exists and its StorageClass cannot expand (or cannot be read), keep
the PVC's current request. NOTES.txt warns when that happens.

`lookup` only sees the cluster during a real install/upgrade (helm CLI or
Flux's helm-controller). `helm template`, `--dry-run` and Argo CD get
nothing back and render redis.persistence.size as-is -- pin the size (or use
existingClaim) there, as values.yaml's upgrade note says.
*/}}
{{- define "distant-signal.redisPvcSize" -}}
{{- $size := .Values.redis.persistence.size | toString -}}
{{- $existing := lookup "v1" "PersistentVolumeClaim" .Release.Namespace (include "distant-signal.redisFullname" .) -}}
{{- if $existing -}}
{{- $current := dig "spec" "resources" "requests" "storage" "" $existing | toString -}}
{{- if and $current (ne $current $size) -}}
{{- $expandable := false -}}
{{- $class := dig "spec" "storageClassName" "" $existing -}}
{{- if $class -}}
{{- $sc := lookup "storage.k8s.io/v1" "StorageClass" "" $class -}}
{{- $expandable = dig "allowVolumeExpansion" false $sc -}}
{{- end -}}
{{- if not $expandable -}}
{{- $size = $current -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $size -}}
{{- end }}
