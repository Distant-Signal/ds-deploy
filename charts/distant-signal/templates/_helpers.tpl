{{/*
api.chatbotAccess, validated at render time: `group` (default) or
`authenticated`. The api would also refuse a bad value at startup (clap),
but failing the render keeps a typo from crash-looping the api pod.
*/}}
{{- define "distant-signal.chatbotAccess" -}}
{{- $mode := .Values.api.chatbotAccess | default "group" | toString -}}
{{- if not (has $mode (list "group" "authenticated")) -}}
{{- fail (printf "api.chatbotAccess must be \"group\" or \"authenticated\", got %q." $mode) -}}
{{- end -}}
{{- $mode -}}
{{- end }}

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
Pod-template label set: the selector labels plus part-of and managed-by,
and nothing that changes between releases. Call as:
  {{- include "distant-signal.podLabels" (dict "root" . "component" "api") }}
Use it in every Deployment/StatefulSet/Job/CronJob pod template; the
objects' own metadata keeps the full `distant-signal.labels`. Any change to
a pod template rolls its pods, and `helm.sh/chart` (the chart version, which
CI stamps with the run number) and `app.kubernetes.io/version` (appVersion,
`sha-<commit>`) change on every CI build, so with them every release
restarted every workload, Redis (Recreate: an outage window) and
schedulefeed included, even when its image and settings were unchanged.
Without them a pod restarts only when its own spec (image digest, env,
config checksums) changes. scripts/check-pod-labels-chart.py fails CI if a
pod template carries either label again. The bundled Postgres StatefulSet
keeps its older selector-labels-plus-part-of set (no managed-by), so this
helper's introduction doesn't restart Postgres.
*/}}
{{- define "distant-signal.podLabels" -}}
{{ include "distant-signal.selectorLabels" . }}
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
The enricher pod's ServiceAccount: its dedicated one (enricher.serviceAccount,
`<fullname>-enricher` unless named) when created or named, else the shared
one above. Takes root.
*/}}
{{- define "distant-signal.enricherServiceAccountName" -}}
{{- $sa := .Values.enricher.serviceAccount | default dict -}}
{{- if $sa.create -}}
{{- default (printf "%s-enricher" (include "distant-signal.fullname" .) | trunc 63 | trimSuffix "-") $sa.name -}}
{{- else if $sa.name -}}
{{- $sa.name -}}
{{- else -}}
{{- include "distant-signal.serviceAccountName" . -}}
{{- end -}}
{{- end }}

{{/*
"true" when enricher.llm.auth is a workload identity federation mode
(openaiWifAuthentik / openaiWifKubernetes), empty for apiKey; fails on any
other value. Takes root.
*/}}
{{- define "distant-signal.enricherWif" -}}
{{- $auth := .Values.enricher.llm.auth | default "apiKey" -}}
{{- if not (has $auth (list "apiKey" "openaiWifAuthentik" "openaiWifKubernetes")) -}}
{{- fail (printf "enricher.llm.auth=%q is not one of apiKey, openaiWifAuthentik, openaiWifKubernetes." $auth) -}}
{{- end -}}
{{- if ne $auth "apiKey" -}}true{{- end -}}
{{- end }}

{{/*
Audience of the enricher's projected service-account token:
workloadIdentity.tokenAudience, else (openaiWifAuthentik) the Authentik
client ID. Takes root.
*/}}
{{- define "distant-signal.enricherTokenAudience" -}}
{{- $wi := .Values.enricher.llm.workloadIdentity -}}
{{- if $wi.tokenAudience -}}
{{- $wi.tokenAudience -}}
{{- else if eq .Values.enricher.llm.auth "openaiWifAuthentik" -}}
{{- $wi.authentik.clientId -}}
{{- end -}}
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
workloads at a Service that was never created. The URL itself never
carries a credential from this chart: with `redis.auth.enabled` the
password travels separately as REDIS_PASSWORD (distant-signal.redisPasswordEnv
below), and the services combine the two (crates/common/src/redis_auth.rs).
An operator can still put inline auth in `redis.externalUrl`, accepting
that it is visible in the rendered Deployment.
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
Redis AUTH (redis.auth, INF-3): the Secret and key holding the password.
The chart-generated password lives in the chart's main Secret under
`redis-password` (secret.yaml); an operator Secret is used as-is. An
external Redis cannot use a chart-generated password (nothing would set it
on the server), so that combination fails the render. Takes root.
*/}}
{{- define "distant-signal.redisAuthSecretName" -}}
{{- if .Values.redis.auth.existingSecret -}}
{{- .Values.redis.auth.existingSecret -}}
{{- else if .Values.redis.enabled -}}
{{- include "distant-signal.secretName" . -}}
{{- else -}}
{{- fail "redis.auth.enabled is true with an external Redis (redis.enabled=false) but redis.auth.existingSecret is empty. A chart-generated password cannot match a server this chart does not run: set redis.auth.existingSecret (and redis.auth.existingSecretKey) to the Secret holding the external Redis's password." -}}
{{- end -}}
{{- end }}

{{- define "distant-signal.redisAuthSecretKey" -}}
{{- if .Values.redis.auth.existingSecret -}}
{{- required "redis.auth.existingSecretKey must not be empty when redis.auth.existingSecret is set" .Values.redis.auth.existingSecretKey -}}
{{- else -}}
redis-password
{{- end -}}
{{- end }}

{{/*
REDIS_PASSWORD env entry for a Redis client container (and the bundled
Redis itself), from the redis.auth Secret. Renders NOTHING unless
redis.auth.enabled, so callers wrap it in no conditional of their own and
the default render is unchanged. Emits a leading newline and indents to 12
(container env list items), so call it as
`{{- include "distant-signal.redisPasswordEnv" . }}` directly after the
REDIS_URL entry. Takes root.
*/}}
{{- define "distant-signal.redisPasswordEnv" -}}
{{- if .Values.redis.auth.enabled }}
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ include "distant-signal.redisAuthSecretName" . }}
                  key: {{ include "distant-signal.redisAuthSecretKey" . }}
{{- end -}}
{{- end }}

{{/*
redis.acl (docs/redis-acl.md; ingest architecture phase 0c). Every helper
takes root unless it says otherwise.

distant-signal.redisAclEnabled: true (non-empty) when the ACL users are on.
Validates the values.
*/}}
{{- define "distant-signal.redisAclEnabled" -}}
{{- $acl := .Values.redis.acl -}}
{{- if $acl.enabled -}}
{{- if not $acl.existingSecret -}}
{{- fail "redis.acl.enabled needs redis.acl.existingSecret: the Secret with one <user>-password key per user in files/redis-users.acl.tpl (docs/redis-acl.md)." -}}
{{- end -}}
{{- if not (has (toString $acl.stage) (list "open" "narrow")) -}}
{{- fail (printf "redis.acl.stage must be open or narrow, not %q." (toString $acl.stage)) -}}
{{- end -}}
{{- if not (include "distant-signal.redisAclDefaultUserOn" .) -}}
{{- range $client, $on := $acl.clients -}}
{{- if not $on -}}
{{- fail (printf "redis.acl.defaultUser off locks out redis.acl.clients.%s, which still connects as default: move it to its own user first (rollout step 2)." $client) -}}
{{- end -}}
{{- end -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when the `default` user stays on. Accepts "on"/"off" and
YAML booleans.
*/}}
{{- define "distant-signal.redisAclDefaultUserOn" -}}
{{- $v := .Values.redis.acl.defaultUser -}}
{{- if kindIs "bool" $v -}}
{{- if $v }}true{{ end -}}
{{- else if eq (toString $v) "on" -}}
true
{{- else if ne (toString $v) "off" -}}
{{- fail (printf "redis.acl.defaultUser must be \"on\" or \"off\", not %q." (toString $v)) -}}
{{- end -}}
{{- end }}

{{/*
The ACL users from files/redis-users.acl.tpl, one "<user> <kind> <rules>"
per line (comments and blank lines dropped).
*/}}
{{- define "distant-signal.redisAclUserLines" -}}
{{- range $line := .Files.Get "files/redis-users.acl.tpl" | splitList "\n" }}
{{- $line = trim $line }}
{{- if and $line (not (hasPrefix "#" $line)) }}
{{ $line }}
{{- end }}
{{- end }}
{{- end }}

{{/*
The env var an ACL user's password is passed in. Takes the user name.
*/}}
{{- define "distant-signal.redisAclPasswordVar" -}}
{{- printf "REDIS_ACL_PASSWORD_%s" (. | upper | replace "-" "_") -}}
{{- end }}

{{/*
users.acl with ${REDIS_ACL_PASSWORD_*} placeholders, for the stage and
default user in values. The Redis initContainer fills in the passwords.
*/}}
{{- define "distant-signal.redisAclFile" -}}
{{- $root := . -}}
{{- $stage := toString .Values.redis.acl.stage -}}
{{- if include "distant-signal.redisAclDefaultUserOn" . -}}
{{- if .Values.redis.auth.enabled }}
user default reset on >${REDIS_ACL_PASSWORD_DEFAULT} ~* &* +@all
{{- else }}
user default reset on nopass ~* &* +@all
{{- end }}
{{- else }}
user default reset off
{{- end }}
{{- range $line := include "distant-signal.redisAclUserLines" . | trim | splitList "\n" }}
{{- $parts := regexSplit "\\s+" $line 3 }}
{{- $user := index $parts 0 }}
{{- $kind := index $parts 1 }}
{{- $rules := "" }}
{{- if eq (len $parts) 3 }}
{{- $rules = index $parts 2 }}
{{- end }}
{{- $var := include "distant-signal.redisAclPasswordVar" $user }}
{{- $rights := $rules }}
{{- if or (eq $kind "admin") (and (eq $kind "client") (eq $stage "open")) }}
{{- $rights = "~* &* +@all" }}
{{- else if not (has $kind (list "client" "final")) }}
{{- fail (printf "files/redis-users.acl.tpl: user %s has unknown kind %q" $user $kind) }}
{{- end }}
user {{ $user }} reset on >${{ printf "{%s}" $var }} >${{ printf "{%s_PREVIOUS}" $var }} {{ $rights }}
{{- end }}
{{- end }}

{{/*
REDIS_USERNAME and REDIS_PASSWORD for a Redis client. Takes (dict "root" $
"client" <redis.acl.clients key> "user" <ACL user>). With
redis.acl.clients.<client> on, the client's own user and password (from
redis.acl.existingSecret); otherwise exactly distant-signal.redisPasswordEnv
(nothing unless redis.auth.enabled). Same leading newline and indent.
*/}}
{{- define "distant-signal.redisClientAuthEnv" -}}
{{- $root := .root -}}
{{- if get $root.Values.redis.acl.clients .client -}}
{{- if not (include "distant-signal.redisAclEnabled" $root) -}}
{{- fail (printf "redis.acl.clients.%s needs redis.acl.enabled (rollout step 1 first)." .client) -}}
{{- end }}
            - name: REDIS_USERNAME
              value: {{ .user | quote }}
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ $root.Values.redis.acl.existingSecret }}
                  key: {{ printf "%s-password" .user }}
{{- else -}}
{{- include "distant-signal.redisPasswordEnv" $root -}}
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
movement-relay's effective Kafka connection settings. Each
movementRelay.kafka.* field falls back to the matching trustConsumer.kafka.*
field when left empty, so an install configures its one RDM Train Movements
connection once (in either place) and movement-relay, which is on by
default, picks it up. RDM issues one consumer group per account, and
movement-relay is its only member: since Deploy C (PL-15a) no consumer
reads Kafka directly. trustConsumer.kafka.* is kept as the place this
connection is configured, so existing installs keep working unchanged.
*/}}
{{- define "distant-signal.movementRelayKafkaBrokers" -}}
{{- .Values.movementRelay.kafka.brokers | default .Values.trustConsumer.kafka.brokers }}
{{- end }}

{{- define "distant-signal.movementRelayKafkaTopic" -}}
{{- .Values.movementRelay.kafka.topic | default .Values.trustConsumer.kafka.topic }}
{{- end }}

{{- define "distant-signal.movementRelayKafkaConsumerGroup" -}}
{{- .Values.movementRelay.kafka.consumerGroup | default .Values.trustConsumer.kafka.consumerGroup }}
{{- end }}

{{- define "distant-signal.movementRelayKafkaSaslMechanism" -}}
{{- .Values.movementRelay.kafka.saslMechanism | default .Values.trustConsumer.kafka.saslMechanism }}
{{- end }}

{{/*
Non-empty when movement-relay has its OWN SASL credential configured:
movementRelay.kafka.existingSecret, or an inline saslUsername/saslPassword
(rendered into the chart Secret as movement-relay-kafka-sasl-*). Otherwise
movement-relay reads trust-consumer's credential exactly as trust-consumer
itself would (trustConsumer.kafka.existingSecret or the chart Secret, with
trustConsumer.kafka.existingSecret*Key). No OAuth username/password keys
here (unlike every other crate in this repo): movement-relay never calls
api.
*/}}
{{- define "distant-signal.movementRelayOwnKafkaCredential" -}}
{{- if or .Values.movementRelay.kafka.existingSecret .Values.movementRelay.kafka.saslUsername .Values.movementRelay.kafka.saslPassword }}true{{ end }}
{{- end }}

{{- define "distant-signal.movementRelaySecretName" -}}
{{- if include "distant-signal.movementRelayOwnKafkaCredential" . }}
{{- default (include "distant-signal.secretName" .) .Values.movementRelay.kafka.existingSecret }}
{{- else }}
{{- default (include "distant-signal.secretName" .) .Values.trustConsumer.kafka.existingSecret }}
{{- end }}
{{- end }}

{{- define "distant-signal.movementRelaySaslUsernameKey" -}}
{{- if include "distant-signal.movementRelayOwnKafkaCredential" . }}
{{- .Values.movementRelay.kafka.existingSecretUsernameKey }}
{{- else }}
{{- .Values.trustConsumer.kafka.existingSecretUsernameKey }}
{{- end }}
{{- end }}

{{- define "distant-signal.movementRelaySaslPasswordKey" -}}
{{- if include "distant-signal.movementRelayOwnKafkaCredential" . }}
{{- .Values.movementRelay.kafka.existingSecretPasswordKey }}
{{- else }}
{{- .Values.trustConsumer.kafka.existingSecretPasswordKey }}
{{- end }}
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
{{- include "distant-signal.databaseEnvFor" (dict "root" . "service" "") -}}
{{- end }}

{{/*
distant-signal.databaseEnv for one service: takes (dict "root" $ "service"
"api"|"aggregator"|"enricher"|"notifier"). With
postgresql.roles.perService.<service>.connect (Stage 0b) the service
connects as its own role from files/db-grants.yaml; otherwise exactly as
distant-signal.databaseEnv.
*/}}
{{- define "distant-signal.databaseEnvFor" -}}
{{- $service := .service -}}
{{- with .root -}}
{{- if .Values.postgresql.enabled -}}
{{- /* With postgresql.roles.enabled, the non-superuser app role. */}}
{{- $user := .Values.postgresql.auth.username -}}
{{- $secretName := include "distant-signal.postgresSecretName" . -}}
{{- $secretKey := include "distant-signal.postgresSecretPasswordKey" . -}}
{{- if include "distant-signal.postgresRolesEnabled" . -}}
{{- $user = .Values.postgresql.roles.app.username -}}
{{- $secretName = include "distant-signal.postgresRoleSecretName" (dict "root" . "role" "app") -}}
{{- $secretKey = include "distant-signal.postgresRoleSecretKey" (dict "root" . "role" "app") -}}
{{- end -}}
{{- if and $service (include "distant-signal.perServiceConnects" (dict "root" . "service" $service)) -}}
{{- /* Stage 0b: the service's own role (a member of app). */}}
{{- $user = include "distant-signal.perServiceRoleName" (dict "root" . "service" $service) -}}
{{- $secretName = include "distant-signal.perServiceSecretName" (dict "root" . "service" $service) -}}
{{- $secretKey = include "distant-signal.perServiceSecretKey" (dict "root" . "service" $service) -}}
{{- end -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $secretName }}
      key: {{ $secretKey }}
- name: DATABASE_URL
  value: {{ printf "postgres://%s:$(PGPASSWORD)@%s:%d/%s" $user (include "distant-signal.postgresFullname" .) (int .Values.postgresql.service.port) .Values.postgresql.auth.database | quote }}
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
{{- end -}}
{{- end }}

{{/*
postgresql.roles (docs/postgres-app-role.md). Every helper takes root
unless it says otherwise.

distant-signal.postgresRolesEnabled: true (non-empty) when the services
connect as the separate roles. Needs the bundled Postgres.
*/}}
{{- define "distant-signal.postgresRolesEnabled" -}}
{{- if .Values.postgresql.roles.enabled -}}
{{- if not .Values.postgresql.enabled -}}
{{- fail "postgresql.roles.enabled needs the bundled Postgres (postgresql.enabled: true). For an external database, give externalDatabase a non-superuser URL instead." -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when anything needs the role passwords: the services
(roles.enabled) or the setup Job (roles.setupJob.enabled).
*/}}
{{- define "distant-signal.postgresRolesOn" -}}
{{- if or (include "distant-signal.postgresRolesEnabled" .) (include "distant-signal.postgresRolesSetupJob" .) -}}
true
{{- end -}}
{{- end }}

{{- define "distant-signal.postgresRolesSetupJob" -}}
{{- if .Values.postgresql.roles.setupJob.enabled -}}
{{- if not .Values.postgresql.enabled -}}
{{- fail "postgresql.roles.setupJob.enabled needs the bundled Postgres (postgresql.enabled: true)." -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when the Postgres pod carries the initdb script.
*/}}
{{- define "distant-signal.postgresRolesInitScript" -}}
{{- if and (include "distant-signal.postgresRolesEnabled" .) .Values.postgresql.roles.initScript -}}
true
{{- end -}}
{{- end }}

{{/*
Secret name / key of one role's password. Takes (dict "root" $ "role"
"owner"|"app"|"exporter"|"dump"|"backup"). Same three-way shape as auth.password:
the role's existingSecret, else the chart's Secret.
*/}}
{{- define "distant-signal.postgresRoleSecretName" -}}
{{- $role := get .root.Values.postgresql.roles .role -}}
{{- default (include "distant-signal.secretName" .root) $role.existingSecret -}}
{{- end }}

{{- define "distant-signal.postgresRoleSecretKey" -}}
{{- $role := get .root.Values.postgresql.roles .role -}}
{{- required (printf "postgresql.roles.%s.existingSecretPasswordKey must not be empty" .role) $role.existingSecretPasswordKey -}}
{{- end }}

{{/*
A role's CONNECTION LIMIT. Takes (dict "root" $ "role" ...). An empty
owner/app value is computed: the owner gets api.replicaCount + 2; the app
gets the chart's pools (the same ones the api-deployment.yaml budget check
counts, without the migration connection, which is the owner's) plus
app.connectionLimitSlack.
*/}}
{{- define "distant-signal.postgresRoleConnectionLimit" -}}
{{- $root := .root -}}
{{- $role := get $root.Values.postgresql.roles .role -}}
{{- $limit := toString (default "" $role.connectionLimit) -}}
{{- if ne $limit "" -}}
{{- if not (regexMatch "^[0-9]+$" $limit) -}}
{{- fail (printf "postgresql.roles.%s.connectionLimit must be a whole number, or empty to compute it." .role) -}}
{{- end -}}
{{- $limit -}}
{{- else if eq .role "owner" -}}
{{- add (int $root.Values.api.replicaCount) 2 -}}
{{- else if eq .role "app" -}}
{{- /* Stage 0b: a service connecting as its own role no longer counts
     against app. */ -}}
{{- $total := int $role.connectionLimitSlack -}}
{{- range $service := list "api" "aggregator" "enricher" "notifier" -}}
{{- if not (include "distant-signal.perServiceConnects" (dict "root" $root "service" $service)) -}}
{{- $total = add $total (include "distant-signal.servicePool" (dict "root" $root "service" $service)) -}}
{{- end -}}
{{- end -}}
{{- /* The api-maintenance CronJob connects as the api does. */ -}}
{{- if not (include "distant-signal.perServiceConnects" (dict "root" $root "service" "api")) -}}
{{- $total = add $total (include "distant-signal.apiMaintenancePool" $root) -}}
{{- end -}}
{{- if not (include "distant-signal.perServiceConnects" (dict "root" $root "service" "writer")) -}}
{{- $total = add $total (include "distant-signal.ingestWriterPool" $root) -}}
{{- end -}}
{{- if not (include "distant-signal.perServiceConnects" (dict "root" $root "service" "schedule_ingest")) -}}
{{- $total = add $total (include "distant-signal.scheduleIngestPool" $root) -}}
{{- end -}}
{{- if not (include "distant-signal.perServiceConnects" (dict "root" $root "service" "schedule_reference")) -}}
{{- $total = add $total (include "distant-signal.scheduleReferencePool" $root) -}}
{{- end -}}
{{- /* Phase 2: the pollers that write directly, as the app role. */ -}}
{{- $total = add $total (include "distant-signal.pollerAppPools" $root) -}}
{{- $total -}}
{{- else -}}
{{- fail (printf "postgresql.roles.%s.connectionLimit must be set." .role) -}}
{{- end -}}
{{- end }}

{{/*
The DS_PG_<ROLE>_PASSWORD env entries files/postgres-roles.sql reads with
psql's \getenv.
*/}}
{{- define "distant-signal.postgresRolesPasswordEnv" -}}
{{- $root := . -}}
{{- range $role := list "owner" "app" "exporter" "dump" "backup" }}
- name: {{ printf "DS_PG_%s_PASSWORD" (upper $role) }}
  valueFrom:
    secretKeyRef:
      name: {{ include "distant-signal.postgresRoleSecretName" (dict "root" $root "role" $role) }}
      key: {{ include "distant-signal.postgresRoleSecretKey" (dict "root" $root "role" $role) }}
{{- end }}
{{- if include "distant-signal.perServiceEnabled" $root }}
{{- range $service := include "distant-signal.perServiceKeys" $root | splitList " " }}
- name: {{ printf "DS_PG_%s_PASSWORD" (upper $service) }}
  valueFrom:
    secretKeyRef:
      name: {{ include "distant-signal.perServiceSecretName" (dict "root" $root "service" $service) }}
      key: {{ include "distant-signal.perServiceSecretKey" (dict "root" $root "service" $service) }}
{{- end }}
{{- end }}
{{- end }}

{{/*
psql --variable arguments for files/postgres-roles.sql, one per line.
`old_owner` is auth.username: the image's bootstrap superuser, whose
objects move to the owner role.
*/}}
{{- define "distant-signal.postgresRolesPsqlVariables" -}}
{{- $roles := .Values.postgresql.roles -}}
--variable=old_owner={{ .Values.postgresql.auth.username }}
{{- range $role := list "owner" "app" "exporter" "dump" "backup" }}
--variable={{ $role }}={{ (get $roles $role).username }}
--variable={{ $role }}_connection_limit={{ include "distant-signal.postgresRoleConnectionLimit" (dict "root" $ "role" $role) }}
{{- end }}
--variable=backup_database={{ $roles.backup.database }}
{{- end }}

{{/*
Stage 0b, postgresql.roles.perService (docs/postgres-app-role.md). The
roles themselves are defined in files/db-grants.yaml (names, groups);
files/postgres-grants.sql is generated from it by scripts/gen-db-grants.py.
Every helper takes root unless it says otherwise.

distant-signal.perServiceKeys: the services this chart can move to their
own role, space-separated. Each must be a created (not `planned`) role in
db-grants.yaml. `writer` is the ingest-writer's (ingestWriter, plan 1B.9);
`schedule_ingest` is schedule-ingest's under scheduleFeed.ingest.sink=db
(plan 2d.2); `schedule_reference` is schedule-reference's db sink's (plan
2a); `stations` and `incidents` are poller-stations' and poller-incidents'
(pollers.<name>.ingest.sink db, plans 2b.3 and 2c.3). All four are narrow
roles.
*/}}
{{- define "distant-signal.perServiceKeys" -}}
api aggregator enricher notifier writer schedule_ingest schedule_reference stations incidents
{{- end }}

{{- define "distant-signal.perServiceEnabled" -}}
{{- if .Values.postgresql.roles.perService.enabled -}}
{{- if not (include "distant-signal.postgresRolesSetupJob" .) -}}
{{- fail "postgresql.roles.perService.enabled needs postgresql.roles.setupJob.enabled: the setup Job creates the per-service roles (files/postgres-grants.sql)." -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when the service connects as its own role. Takes (dict
"root" $ "service" "api"|...).
*/}}
{{- define "distant-signal.perServiceConnects" -}}
{{- $cfg := get .root.Values.postgresql.roles.perService .service -}}
{{- if $cfg.connect -}}
{{- if not (include "distant-signal.perServiceEnabled" .root) -}}
{{- fail (printf "postgresql.roles.perService.%s.connect needs postgresql.roles.perService.enabled (which creates the role) first." .service) -}}
{{- end -}}
{{- if not (include "distant-signal.postgresRolesEnabled" .root) -}}
{{- fail (printf "postgresql.roles.perService.%s.connect needs postgresql.roles.enabled: the per-service roles are members of the app role." .service) -}}
{{- end -}}
{{- if and (eq .service "writer") (not .root.Values.ingestWriter.enabled) -}}
{{- fail "postgresql.roles.perService.writer.connect needs ingestWriter.enabled: nothing else connects as the writer role." -}}
{{- end -}}
{{- if and (eq .service "schedule_ingest") (not (include "distant-signal.scheduleIngestSinkDb" .root)) -}}
{{- fail "postgresql.roles.perService.schedule_ingest.connect needs scheduleFeed.enabled and scheduleFeed.ingest.sink=db: nothing else connects as the schedule_ingest role." -}}
{{- end -}}
{{- if and (eq .service "schedule_reference") (not (include "distant-signal.scheduleReferenceDbSink" .root)) -}}
{{- fail "postgresql.roles.perService.schedule_reference.connect needs scheduleFeed.enabled and scheduleFeed.reference.ingest.sink: db: nothing else connects as the schedule_reference role." -}}
{{- end -}}
{{- if hasKey .root.Values.pollers .service -}}
{{- if not (include "distant-signal.pollerSinkDb" (dict "root" .root "name" .service "poller" (get .root.Values.pollers .service))) -}}
{{- fail (printf "postgresql.roles.perService.%s.connect needs pollers.%s.enabled with pollers.%s.ingest.sink: db: nothing else connects as its role." .service .service .service) -}}
{{- end -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when the poller is enabled and pollers.<name>.ingest.sink
is `db` (ingest architecture phase 2: it writes Postgres directly). Fails on
a sink other than `http` or `db`. poller-stations' `db` sink also needs the
ingest-writer's loops (plan 2b.2): with no POST to the api, the writer's
10-minute CORPUS crosswalk loop is what gives a new station its fills.
Takes (dict "root" $ "name" <pollers key> "poller" <its values>).
*/}}
{{- define "distant-signal.pollerSinkDb" -}}
{{- $ingest := .poller.ingest | default dict -}}
{{- $sink := toString ($ingest.sink | default "http") -}}
{{- if not (has $sink (list "http" "db")) -}}
{{- fail (printf "pollers.%s.ingest.sink must be http or db, not %q." .name $sink) -}}
{{- end -}}
{{- if and .poller.enabled (eq $sink "db") -}}
{{- if and (eq .name "stations") (not (and .root.Values.ingestWriter.enabled .root.Values.ingestWriter.loops.enabled)) -}}
{{- fail "pollers.stations.ingest.sink=db needs ingestWriter.enabled and ingestWriter.loops.enabled: without the api's POST, the ingest-writer's CORPUS crosswalk loop rebuilds the crosswalk after a stations refresh (ingest architecture plan 2b.2)." -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
The Postgres pools of the pollers that write directly (pollers.<name>.ingest.sink:
db) and do not connect as their own per-service role, summed: they count
against the app role. Takes root.
*/}}
{{- define "distant-signal.pollerAppPools" -}}
{{- $root := . -}}
{{- $total := 0 -}}
{{- range $name, $poller := .Values.pollers -}}
{{- if include "distant-signal.pollerSinkDb" (dict "root" $root "name" $name "poller" $poller) -}}
{{- if not (and (hasKey $root.Values.postgresql.roles.perService $name) (include "distant-signal.perServiceConnects" (dict "root" $root "service" $name))) -}}
{{- $total = add $total (int $poller.ingest.database.maxConnections) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $total -}}
{{- end }}

{{/*
Every poller that writes Postgres directly (pollers.<name>.ingest.sink db),
their pools summed whichever role they connect as: the max_connections
budget in api-deployment.yaml. Takes root.
*/}}
{{- define "distant-signal.pollerDbPools" -}}
{{- $root := . -}}
{{- $total := 0 -}}
{{- range $name, $poller := .Values.pollers -}}
{{- if include "distant-signal.pollerSinkDb" (dict "root" $root "name" $name "poller" $poller) -}}
{{- $total = add $total (int $poller.ingest.database.maxConnections) -}}
{{- end -}}
{{- end -}}
{{- $total -}}
{{- end }}

{{/*
The role name from files/db-grants.yaml. Takes (dict "root" $ "service" ...).
*/}}
{{- define "distant-signal.perServiceRoleName" -}}
{{- $grants := .root.Files.Get "files/db-grants.yaml" | fromYaml -}}
{{- $role := get $grants.roles .service -}}
{{- if or (not $role) (eq (toString $role.status) "planned") -}}
{{- fail (printf "files/db-grants.yaml has no created role %q (status observed or narrow)." .service) -}}
{{- end -}}
{{- $role.name -}}
{{- end }}

{{- define "distant-signal.perServiceSecretName" -}}
{{- $cfg := get .root.Values.postgresql.roles.perService .service -}}
{{- default (include "distant-signal.secretName" .root) $cfg.existingSecret -}}
{{- end }}

{{- define "distant-signal.perServiceSecretKey" -}}
{{- $cfg := get .root.Values.postgresql.roles.perService .service -}}
{{- required (printf "postgresql.roles.perService.%s.existingSecretPasswordKey must not be empty" .service) $cfg.existingSecretPasswordKey -}}
{{- end }}

{{/*
A service's Postgres pool as the chart runs it: the api's DATABASE_MAX_CONNECTIONS
times its replicas, the workers' crate defaults (common::pg), the
aggregator's archive pool, the ingest-writer's ingestWriter.database.maxConnections
(whether or not it is enabled; see distant-signal.ingestWriterPool). Takes
(dict "root" $ "service" ...).
*/}}
{{- define "distant-signal.servicePool" -}}
{{- $root := .root -}}
{{- if eq .service "api" -}}
{{- mul (int (include "distant-signal.apiPods" $root)) (int (include "distant-signal.apiMaxConnections" $root)) -}}
{{- else if eq .service "aggregator" -}}
{{- add 10 (ternary 2 0 ($root.Values.archive.enabled | default false)) -}}
{{- else if eq .service "writer" -}}
{{- /* Its pool plus the loop-lock session (crates/ingest-writer). */ -}}
{{- add1 (int $root.Values.ingestWriter.database.maxConnections) -}}
{{- else if eq .service "schedule_ingest" -}}
{{- int $root.Values.scheduleFeed.ingest.database.maxConnections -}}
{{- else if eq .service "schedule_reference" -}}
{{- int $root.Values.scheduleFeed.reference.ingest.database.maxConnections -}}
{{- else if hasKey $root.Values.pollers .service -}}
{{- /* A poller that writes directly (phase 2): pollers.<name>.ingest.database. */ -}}
{{- int (get $root.Values.pollers .service).ingest.database.maxConnections -}}
{{- else -}}
5
{{- end -}}
{{- end }}

{{- /*
The ingest-writer's INGEST_WRITER_STREAMS from ingestWriter.streams (plan
3a.3): "<stream>:<mode>" joined by commas, in key order. Fails on a mode
other than off/shadow/apply. Takes root.
*/ -}}
{{- define "distant-signal.ingestWriterStreams" -}}
{{- $items := list -}}
{{- range $name, $mode := .Values.ingestWriter.streams -}}
{{- if not (has (toString $mode) (list "off" "shadow" "apply")) -}}
{{- fail (printf "ingestWriter.streams.%s must be off, shadow or apply, not %q." $name (toString $mode)) -}}
{{- end -}}
{{- $items = append $items (printf "%s:%s" $name (toString $mode)) -}}
{{- end -}}
{{- join "," $items -}}
{{- end }}

{{- /*
True (non-empty) when the ingest-writer is enabled and reads any stream
(an ingestWriter.streams entry not "off"): it then gets REDIS_URL, its
Redis credentials and the NetworkPolicy paths to Redis. Takes root.
*/ -}}
{{- define "distant-signal.ingestWriterStreamsOn" -}}
{{- if .Values.ingestWriter.enabled -}}
{{- range $name, $mode := .Values.ingestWriter.streams -}}
{{- if ne (toString $mode) "off" }}true{{ end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
True (non-empty) when the schedulefeed Deployment renders and its ingest
container writes Postgres directly (scheduleFeed.ingest.sink=db, plan 2d);
fails the render on an unknown sink. Takes root.
*/}}
{{- define "distant-signal.scheduleIngestSinkDb" -}}
{{- $sink := toString (.Values.scheduleFeed.ingest.sink | default "http") -}}
{{- if not (has $sink (list "http" "db")) -}}
{{- fail (printf "scheduleFeed.ingest.sink must be http or db, got %q" $sink) -}}
{{- end -}}
{{- if and .Values.scheduleFeed.enabled (eq $sink "db") -}}
true
{{- end -}}
{{- end }}

{{/*
schedule-ingest's Postgres pool (scheduleFeed.ingest.database.maxConnections)
under scheduleFeed.ingest.sink=db, else 0. Takes root.
*/}}
{{- define "distant-signal.scheduleIngestPool" -}}
{{- if include "distant-signal.scheduleIngestSinkDb" . -}}
{{- int .Values.scheduleFeed.ingest.database.maxConnections -}}
{{- else -}}
0
{{- end -}}
{{- end }}

{{/*
True (non-empty) when schedule-reference writes Postgres directly
(scheduleFeed.reference.ingest.sink: db, ingest architecture plan 2a): its
container gets DATABASE_URL and the schedulefeed pod may reach Postgres.
Fails on a sink other than http or db. Takes root.
*/}}
{{- define "distant-signal.scheduleReferenceDbSink" -}}
{{- $sink := toString .Values.scheduleFeed.reference.ingest.sink -}}
{{- if not (has $sink (list "http" "db")) -}}
{{- fail (printf "scheduleFeed.reference.ingest.sink must be http or db, not %q." $sink) -}}
{{- end -}}
{{- if and .Values.scheduleFeed.enabled (eq $sink "db") -}}
true
{{- end -}}
{{- end }}

{{/*
True (non-empty) when a schedulefeed container connects to Postgres (its
NetworkPolicy egress, and the postgres policy's ingress): either
schedule-reference's db sink (plan 2a) or schedule-ingest's (plan 2d).
Takes root.
*/}}
{{- define "distant-signal.scheduleFeedPostgres" -}}
{{- if or (include "distant-signal.scheduleReferenceDbSink" .) (include "distant-signal.scheduleIngestSinkDb" .) -}}
true
{{- end -}}
{{- end }}

{{/*
schedule-reference's connections when its db sink is on, else 0. Takes
root.
*/}}
{{- define "distant-signal.scheduleReferencePool" -}}
{{- if include "distant-signal.scheduleReferenceDbSink" . -}}
{{- int .Values.scheduleFeed.reference.ingest.database.maxConnections -}}
{{- else -}}
0
{{- end -}}
{{- end }}

{{/*
poller-incidents' DB sink (ingest plan 2c.2): true (non-empty) when
pollers.incidents is enabled with ingest.sink db (distant-signal.pollerSinkDb,
which validates the value). Takes root.
*/}}
{{- define "distant-signal.pollerIncidentsDbSink" -}}
{{- include "distant-signal.pollerSinkDb" (dict "root" . "name" "incidents" "poller" (.Values.pollers.incidents | default dict)) -}}
{{- end }}

{{/*
INCIDENTS_ROW_HEARTBEAT's value for both incident writers
(pollers.incidents.ingest.rowHeartbeat, plan 2c.6): "false", or empty for
the default (true). Takes root.
*/}}
{{- define "distant-signal.incidentsRowHeartbeatOff" -}}
{{- if eq (toString (dig "ingest" "rowHeartbeat" true (.Values.pollers.incidents | default dict))) "false" -}}
true
{{- end -}}
{{- end }}

{{/*
The ingest-writer's connections when ingestWriter.enabled, else 0: its
pool (ingestWriter.database.maxConnections) plus the one dedicated
loop-lock session (crates/ingest-writer's LockSession). Takes root.
*/}}
{{- define "distant-signal.ingestWriterPool" -}}
{{- if .Values.ingestWriter.enabled -}}
{{- add1 (int .Values.ingestWriter.database.maxConnections) -}}
{{- else -}}
0
{{- end -}}
{{- end }}

{{/*
The api-maintenance CronJob's pool (apiMaintenance.database.maxConnections)
when apiMaintenance.enabled, else 0. Takes root.
*/}}
{{- define "distant-signal.apiMaintenancePool" -}}
{{- if .Values.apiMaintenance.enabled -}}
{{- int .Values.apiMaintenance.database.maxConnections -}}
{{- else -}}
0
{{- end -}}
{{- end }}

{{/*
The api's DATABASE_MAX_CONNECTIONS: perService.api.maxConnections while the
api connects as its own role, else api.database.maxConnections.
*/}}
{{- define "distant-signal.apiMaxConnections" -}}
{{- if include "distant-signal.perServiceConnects" (dict "root" . "service" "api") -}}
{{- int .Values.postgresql.roles.perService.api.maxConnections -}}
{{- else -}}
{{- int .Values.api.database.maxConnections -}}
{{- end -}}
{{- end }}

{{/*
A per-service role's CONNECTION LIMIT: the explicit value, else computed
from its pool. Takes (dict "root" $ "service" ...).
*/}}
{{- define "distant-signal.perServiceConnectionLimit" -}}
{{- $root := .root -}}
{{- $cfg := get $root.Values.postgresql.roles.perService .service -}}
{{- $limit := toString (default "" $cfg.connectionLimit) -}}
{{- if ne $limit "" -}}
{{- if not (regexMatch "^[0-9]+$" $limit) -}}
{{- fail (printf "postgresql.roles.perService.%s.connectionLimit must be a whole number, or empty to compute it." .service) -}}
{{- end -}}
{{- $limit -}}
{{- else if eq .service "api" -}}
{{- $pool := int $cfg.maxConnections -}}
{{- add (mul (add (int $root.Values.api.replicaCount) 1) $pool) 2 (include "distant-signal.apiMaintenancePool" $root) -}}
{{- else if eq .service "writer" -}}
{{- /* Its servicePool already counts the lock session: pool + 1 (spec §6.6). */ -}}
{{- include "distant-signal.servicePool" . -}}
{{- else -}}
{{- add (include "distant-signal.servicePool" .) 1 -}}
{{- end -}}
{{- end }}

{{/*
psql --variable arguments for files/postgres-grants.sql, one per line: the
app role's name and each per-service role's CONNECTION LIMIT. (The role
names are db-grants.yaml's own defaults.)
*/}}
{{- define "distant-signal.postgresGrantsPsqlVariables" -}}
--variable=app={{ .Values.postgresql.roles.app.username }}
{{- range $service := include "distant-signal.perServiceKeys" $ | splitList " " }}
{{- $_ := include "distant-signal.perServiceRoleName" (dict "root" $ "service" $service) }}
--variable={{ $service }}_connection_limit={{ include "distant-signal.perServiceConnectionLimit" (dict "root" $ "service" $service) }}
{{- end }}
{{- end }}

{{/*
Stage 0b connection budget (spec §6.6; extends INF-7 in
api-deployment.yaml): with postgresql.roles.perService.enabled, the
CONNECTION LIMITs of every role a client can actually use -- owner, app,
exporter, dump, backup and each per-service role a service connects as --
must fit max_connections minus the 3 superuser_reserved_connections. A
per-service role nothing connects as yet is not counted. Renders nothing;
fails the render instead. Takes root.
*/}}
{{- define "distant-signal.postgresRoleBudgetCheck" -}}
{{- if and .Values.postgresql.enabled (include "distant-signal.perServiceEnabled" .) -}}
{{- $maxConn := 100 -}}
{{- with .Values.postgresql.config -}}
{{- if hasKey . "max_connections" -}}
{{- $maxConn = int (get . "max_connections") -}}
{{- end -}}
{{- end -}}
{{- $total := 0 -}}
{{- $parts := list -}}
{{- range $role := list "owner" "app" "exporter" "dump" "backup" -}}
{{- $limit := int (include "distant-signal.postgresRoleConnectionLimit" (dict "root" $ "role" $role)) -}}
{{- $total = add $total $limit -}}
{{- $parts = append $parts (printf "%s %d" $role $limit) -}}
{{- end -}}
{{- range $service := include "distant-signal.perServiceKeys" $ | splitList " " -}}
{{- if include "distant-signal.perServiceConnects" (dict "root" $ "service" $service) -}}
{{- $limit := int (include "distant-signal.perServiceConnectionLimit" (dict "root" $ "service" $service)) -}}
{{- $total = add $total $limit -}}
{{- $parts = append $parts (printf "%s %d" $service $limit) -}}
{{- end -}}
{{- end -}}
{{- $available := sub $maxConn 3 -}}
{{- if gt $total $available -}}
{{- fail (printf "Postgres role connection limits sum to %d (%s), more than max_connections (%d) minus 3 superuser-reserved = %d. Lower a postgresql.roles.*.connectionLimit or postgresql.roles.perService.*.connectionLimit, or raise postgresql.config.max_connections. Spec §6.6." $total (join ", " $parts) $maxConn $available) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "distant-signal.postgresRolesConfigMapName" -}}
{{- printf "%s-roles" (include "distant-signal.postgresFullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
api's MIGRATION_DATABASE_URL (the owner role) with postgresql.roles.enabled;
empty otherwise, and then api migrates with DATABASE_URL as before. Same
$(VAR) indirection as distant-signal.databaseEnv.
*/}}
{{- define "distant-signal.migrationDatabaseEnv" -}}
{{- if include "distant-signal.postgresRolesEnabled" . -}}
- name: PG_MIGRATION_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "distant-signal.postgresRoleSecretName" (dict "root" . "role" "owner") }}
      key: {{ include "distant-signal.postgresRoleSecretKey" (dict "root" . "role" "owner") }}
- name: MIGRATION_DATABASE_URL
  value: {{ printf "postgres://%s:$(PG_MIGRATION_PASSWORD)@%s:%d/%s" .Values.postgresql.roles.owner.username (include "distant-signal.postgresFullname" .) (int .Values.postgresql.service.port) .Values.postgresql.auth.database | quote }}
{{- end -}}
{{- end }}

{{/*
Ingest plan 1B.3/1B.4: true (non-empty) when the api migrates at startup
(api.migrateOnStartup, the default). false needs migrate.job.enabled, or
nothing would migrate. Takes root.
*/}}
{{- define "distant-signal.apiMigratesOnStartup" -}}
{{- if ne (toString .Values.api.migrateOnStartup) "false" -}}
true
{{- else if not .Values.migrate.job.enabled -}}
{{- fail "api.migrateOnStartup is false but migrate.job.enabled is not: nothing would run the migrations. Enable migrate.job (same release), or keep api.migrateOnStartup true." -}}
{{- end -}}
{{- end }}

{{/*
Ingest plan 1B.10: the api Deployment's strategy type, api.strategy.type.
Recreate (the default) or RollingUpdate; RollingUpdate only once the api no
longer migrates at startup (migrate.job.enabled and api.migrateOnStartup
false), or a new pod would migrate under the old one. Takes root.
*/}}
{{- define "distant-signal.apiStrategy" -}}
{{- $type := toString (dig "strategy" "type" "Recreate" .Values.api) -}}
{{- if eq $type "RollingUpdate" -}}
{{- if or (not .Values.migrate.job.enabled) (include "distant-signal.apiMigratesOnStartup" .) -}}
{{- fail "api.strategy.type RollingUpdate needs migrate.job.enabled and api.migrateOnStartup false: a new api pod that migrates at startup would change the schema under the old pod still serving (see api-deployment.yaml's strategy comment)." -}}
{{- end -}}
{{- else if ne $type "Recreate" -}}
{{- fail (printf "api.strategy.type must be Recreate or RollingUpdate, not %q." $type) -}}
{{- end -}}
{{- $type -}}
{{- end }}

{{/*
The most api pods that can run at once: api.replicaCount, plus the
RollingUpdate surge pod (maxSurge 1). Each has its own pool. Takes root.
*/}}
{{- define "distant-signal.apiPods" -}}
{{- add (int .Values.api.replicaCount) (ternary 1 0 (eq (include "distant-signal.apiStrategy" .) "RollingUpdate")) -}}
{{- end }}

{{/*
The migrate hook Job's MIGRATION_DATABASE_URL (templates/migrate-job.yaml):
the schema owner with postgresql.roles.enabled (as the api's
distant-signal.migrationDatabaseEnv), else the bundled superuser
(auth.username, what the api migrates as today), or the external
database's URL. Same $(VAR) indirection as distant-signal.databaseEnv.
Takes root.
*/}}
{{- define "distant-signal.migrateJobDatabaseEnv" -}}
{{- if include "distant-signal.postgresRolesEnabled" . -}}
{{- include "distant-signal.migrationDatabaseEnv" . -}}
{{- else if .Values.postgresql.enabled -}}
- name: PG_MIGRATION_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "distant-signal.postgresSecretName" . }}
      key: {{ include "distant-signal.postgresSecretPasswordKey" . }}
- name: MIGRATION_DATABASE_URL
  value: {{ printf "postgres://%s:$(PG_MIGRATION_PASSWORD)@%s:%d/%s" .Values.postgresql.auth.username (include "distant-signal.postgresFullname" .) (int .Values.postgresql.service.port) .Values.postgresql.auth.database | quote }}
{{- else if .Values.externalDatabase.existingSecret -}}
- name: MIGRATION_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalDatabase.existingSecret }}
      key: {{ .Values.externalDatabase.existingSecretUrlKey }}
{{- else if .Values.externalDatabase.url -}}
- name: MIGRATION_DATABASE_URL
  value: {{ .Values.externalDatabase.url | quote }}
{{- else -}}
{{- fail "migrate.job.enabled: postgresql.enabled is false but no external database is configured (externalDatabase.existingSecret or externalDatabase.url)." -}}
{{- end -}}
{{- end }}

{{/*
An egress-only NetworkPolicy for a Job's pods (the postgres-roles setup
Job, the migrate hook Job, the api-maintenance CronJob): DNS and the
in-cluster services in `deps` (distant-signal.egressRules). Renders nothing
unless distant-signal.egressOn for the component: without egress policies a
Job's egress is not restricted by this chart, and its ingress stays as
before (a Job serves nothing). `hook` (optional) makes it a Helm hook
created before that hook Job (weight -10). Takes (dict "root" $
"component" "migrate" "deps" (dict "postgres" true) "hook" "pre-install,pre-upgrade").
*/}}
{{- define "distant-signal.jobEgressNetworkPolicy" -}}
{{- $root := .root -}}
{{- if include "distant-signal.egressOn" (dict "root" $root "component" .component) -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ printf "%s-%s" (include "distant-signal.fullname" $root) .component | trunc 63 | trimSuffix "-" }}
  labels:
    {{- include "distant-signal.labels" (dict "root" $root "component" .component) | nindent 4 }}
  {{- with .hook }}
  annotations:
    helm.sh/hook: {{ . }}
    helm.sh/hook-weight: "-10"
    helm.sh/hook-delete-policy: before-hook-creation
  {{- end }}
spec:
  podSelector:
    matchLabels:
      {{- include "distant-signal.selectorLabels" (dict "root" $root "component" .component) | nindent 6 }}
  policyTypes:
    - Egress
  egress:
    {{- include "distant-signal.egressRules" (dict "root" $root "component" .component "deps" .deps "internet" false) | nindent 4 }}
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

{{/*
The schedulefeed pod's ServiceAccount: its dedicated one
(scheduleFeed.serviceAccount, `<fullname>-schedulefeed` unless named) when
created or named, else the shared one. Takes root.
*/}}
{{- define "distant-signal.scheduleFeedServiceAccountName" -}}
{{- $sa := .Values.scheduleFeed.serviceAccount | default dict -}}
{{- if $sa.create -}}
{{- default (include "distant-signal.scheduleFeedFullname" .) $sa.name -}}
{{- else if $sa.name -}}
{{- $sa.name -}}
{{- else -}}
{{- include "distant-signal.serviceAccountName" . -}}
{{- end -}}
{{- end }}

{{/*
"true" when the schedulefeed bucket source is on with
scheduleFeed.bucket.auth=workloadIdentity, empty otherwise (key mode, or
the bucket off). Fails on an auth value other than key/workloadIdentity
when the bucket is on. Takes root.
*/}}
{{- define "distant-signal.scheduleFeedBucketWif" -}}
{{- $b := .Values.scheduleFeed.bucket -}}
{{- if $b.enabled -}}
{{- $auth := $b.auth | default "key" | toString -}}
{{- if not (has $auth (list "key" "workloadIdentity")) -}}
{{- fail (printf "scheduleFeed.bucket.auth must be \"key\" or \"workloadIdentity\", got %q" $auth) -}}
{{- end -}}
{{- if eq $auth "workloadIdentity" -}}true{{- end -}}
{{- end -}}
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
SFTPGo host-key file paths (comma-separated, for SFTPGO_SFTPD__HOST_KEYS),
one per scheduleFeed.sftp.hostKeys entry under the host-key mount. Takes
root.
*/}}
{{- define "distant-signal.scheduleFeedHostKeyPaths" -}}
{{- $paths := list -}}
{{- range .Values.scheduleFeed.sftp.hostKeys -}}
{{- $paths = append $paths (printf "/srv/sftpgo/host_keys/%s" .) -}}
{{- end -}}
{{- join "," $paths -}}
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
Cold-archive env for the aggregator (crates/aggregator/src/archive.rs).
Renders NOTHING when archive.enabled is false, so a default install carries
no ARCHIVE_* env and needs no S3 settings. When enabled, fails rendering on
a missing bucket/secret or a table the binary would refuse anyway, and when
nothing would expire archived objects (neither lifecycleConfirmed nor
archive.expiry.enabled). The ARCHIVE_EXPIRY_* env (archive_expiry.rs) is
rendered only with archive.expiry.enabled; the checks here mirror the
binary's (90-day floor, two-segment prefix), which also checks
protectedPrefixes.
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
{{- $e := $a.expiry | default dict -}}
{{- if not (or $a.s3.lifecycleConfirmed $e.enabled) -}}
{{- fail "archive.enabled is true but neither archive.s3.lifecycleConfirmed nor archive.expiry.enabled is. The archiver never deletes an object: either configure an S3 lifecycle (expiration) rule on the archive bucket/prefix and set archive.s3.lifecycleConfirmed=true, or enable client-side expiry (archive.expiry.enabled). See docs/cold-archive.md, \"Object expiry\"." -}}
{{- end -}}
{{- if $e.enabled -}}
{{- if lt (int $e.retentionDays) 90 -}}
{{- fail (printf "archive.expiry.retentionDays is %v, below the hard 90-day floor." $e.retentionDays) -}}
{{- end -}}
{{- if lt (int $e.maxDeletesPerRun) 1 -}}
{{- fail "archive.expiry.maxDeletesPerRun must be at least 1 (set archive.expiry.enabled=false instead)." -}}
{{- end -}}
{{- if lt (int $e.intervalSecs) 60 -}}
{{- fail "archive.expiry.intervalSecs must be at least 60." -}}
{{- end -}}
{{- if lt (len (compact (splitList "/" $a.s3.prefix))) 2 -}}
{{- fail (printf "archive.expiry needs archive.s3.prefix to have at least two path segments (e.g. <cluster>/distant-signal-archive), got %q." $a.s3.prefix) -}}
{{- end -}}
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
- name: ARCHIVE_S3_LIFECYCLE_CONFIRMED
  value: {{ $a.s3.lifecycleConfirmed | toString | quote }}
{{- if $e.enabled }}
- name: ARCHIVE_EXPIRY_ENABLED
  value: "true"
- name: ARCHIVE_EXPIRY_DRY_RUN
  value: {{ $e.dryRun | toString | quote }}
- name: ARCHIVE_EXPIRY_RETENTION_DAYS
  value: {{ int $e.retentionDays | toString | quote }}
- name: ARCHIVE_EXPIRY_INTERVAL_SECS
  value: {{ int $e.intervalSecs | toString | quote }}
- name: ARCHIVE_EXPIRY_MAX_DELETES_PER_RUN
  value: {{ int $e.maxDeletesPerRun | toString | quote }}
{{- with $e.protectedPrefixes }}
- name: ARCHIVE_PROTECTED_PREFIXES
  value: {{ join "," . | quote }}
{{- end }}
{{- end }}
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
One NetworkPolicyPeer selecting the tunnel connector pods
(networkPolicy.tunnel: namespace, plus podLabels when non-empty). Takes
root; renders a single `- namespaceSelector: ...` list item.
*/}}
{{- define "distant-signal.tunnelPeer" -}}
{{- $t := .Values.networkPolicy.tunnel -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ $t.namespace | quote }}
  {{- with $t.podLabels }}
  podSelector:
    matchLabels:
      {{- toYaml . | nindent 6 }}
  {{- end }}
{{- end }}

{{/*
The public-internet NetworkPolicyEgressRule: 0.0.0.0/0 and ::/0, each minus
networkPolicy.egress.privateCidrs(V6) and the matching-family entries of
networkPolicy.egress.extraDeniedCidrs (an entry containing ":" is IPv6).
extraDeniedCidrs is where an operator names the node's own public
address(es): privateCidrs only covers the reserved ranges, so without it a
pod could reach the API server, the kubelet and any host-published port
through the node's public IP. `ports` (TCP) limits the rule; empty allows
every port. Takes (dict "root" $root "ports" (list 443)); renders one
`- to: ...` list item.
*/}}
{{- define "distant-signal.internetEgressRule" -}}
{{- $eg := .root.Values.networkPolicy.egress -}}
{{- $v4 := list -}}
{{- $v6 := list -}}
{{- range ($eg.extraDeniedCidrs | default list) -}}
{{- if not (regexMatch "^[0-9a-fA-F.:]+/[0-9]{1,3}$" .) -}}
{{- fail (printf "networkPolicy.egress.extraDeniedCidrs: %q is not a CIDR. Write a single address as a /32 (IPv4) or /128 (IPv6)." .) -}}
{{- end -}}
{{- if contains ":" . -}}
{{- $v6 = append $v6 . -}}
{{- else -}}
{{- $v4 = append $v4 . -}}
{{- end -}}
{{- end -}}
{{- $except4 := concat ($eg.privateCidrs | default list) $v4 -}}
{{- $except6 := concat ($eg.privateCidrsV6 | default list) $v6 -}}
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        {{- with $except4 }}
        except:
          {{- toYaml . | nindent 10 }}
        {{- end }}
    - ipBlock:
        cidr: "::/0"
        {{- with $except6 }}
        except:
          {{- toYaml . | nindent 10 }}
        {{- end }}
  {{- with .ports }}
  ports:
    {{- range . }}
    - protocol: TCP
      port: {{ . }}
    {{- end }}
  {{- end }}
{{- end }}

{{/*
The TCP port a URL connects to: its explicit port, else 443 for https and
80 for http, else nothing. Takes the URL string.
*/}}
{{- define "distant-signal.urlPort" -}}
{{- if contains "://" . -}}
{{- $u := urlParse . -}}
{{- $m := regexFind ":[0-9]+$" $u.host -}}
{{- if $m -}}
{{- trimPrefix ":" $m -}}
{{- else if eq $u.scheme "https" -}}
443
{{- else if eq $u.scheme "http" -}}
80
{{- end -}}
{{- end -}}
{{- end }}

{{/*
The TCP ports of the public-internet rule for one component, as a JSON list
of ints, or [] for every port: networkPolicy.components.<component>.internetPorts
if set, else networkPolicy.egress.internetPorts. A non-empty list also gets
the ports the component's own upstreams need: `urls` (distant-signal.urlPort),
`brokers` (a Kafka bootstrap list, host:port,...; 9092 when a port is
missing) and `ports`. Takes the egressRules dict.
*/}}
{{- define "distant-signal.internetPorts" -}}
{{- $cs := include "distant-signal.npComponent" . | fromYaml -}}
{{- $base := .root.Values.networkPolicy.egress.internetPorts | default list -}}
{{- if hasKey $cs "internetPorts" -}}
{{- $base = get $cs "internetPorts" | default list -}}
{{- end -}}
{{- $out := list -}}
{{- if $base -}}
{{- range $base -}}
{{- $out = append $out (int .) -}}
{{- end -}}
{{- range (.urls | default list) -}}
{{- with include "distant-signal.urlPort" (toString .) -}}
{{- $out = append $out (int .) -}}
{{- end -}}
{{- end -}}
{{- range (splitList "," (.brokers | default "" | toString)) -}}
{{- $b := trim . -}}
{{- if $b -}}
{{- $m := regexFind ":[0-9]+$" $b -}}
{{- $out = append $out (ternary (int (trimPrefix ":" $m)) 9092 (ne $m "")) -}}
{{- end -}}
{{- end -}}
{{- range (.ports | default list) -}}
{{- $out = append $out (int .) -}}
{{- end -}}
{{- end -}}
{{- $out | uniq | toJson -}}
{{- end }}

{{/*
Per-component NetworkPolicy settings: networkPolicy.components.<component>,
keyed by the app.kubernetes.io/component label (api, postgres,
poller-ldbws, ...), or an empty dict. Fails on a key naming no component
this chart can render, so a typo cannot silently drop a rule. Takes
(dict "root" $root "component" "api"); returns YAML (use fromYaml).
*/}}
{{- define "distant-signal.npComponent" -}}
{{- $root := .root -}}
{{- $all := $root.Values.networkPolicy.components | default dict -}}
{{- $known := list "api" "frontend" "aggregator" "enricher" "notifier" "postgres" "redis" "schedulefeed" "trust-consumer" "trust-backlog-consumer" "full-coverage-consumer" "movement-relay" "poller-irish-rail-gtfs" "poller-irish-rail-live" "poller-nir-stations" "postgres-roles" "migrate" "api-maintenance" "ingest-writer" -}}
{{- range $name, $_ := $root.Values.pollers -}}
{{- $known = append $known (printf "poller-%s" $name) -}}
{{- end -}}
{{- range $name, $_ := $all -}}
{{- if not (has $name $known) -}}
{{- fail (printf "networkPolicy.components.%s: no such component. Keys are app.kubernetes.io/component labels: %s." $name (join ", " $known)) -}}
{{- end -}}
{{- end -}}
{{- get $all .component | default dict | toYaml -}}
{{- end }}

{{/*
"true" when the component gets an egress policy: networkPolicy.egress.enabled
and not networkPolicy.components.<component>.egress: false. Takes
(dict "root" $root "component" "api").
*/}}
{{- define "distant-signal.egressOn" -}}
{{- $cs := include "distant-signal.npComponent" . | fromYaml -}}
{{- if and .root.Values.networkPolicy.egress.enabled (ne (toString (get $cs "egress")) "false") -}}
true
{{- end -}}
{{- end }}

{{/*
The NetworkPolicyEgressRule list for one component (render under
`egress:`): DNS (port 53, to networkPolicy.egress.dnsPeers when set); the in-cluster services in `deps` (api, redis, postgres;
api and idp also admit the bundled dev IdP when devAuthentik.enabled); the
public internet (distant-signal.internetEgressRule, on the ports from
distant-signal.internetPorts, which reads `urls`, `brokers` and `ports`)
when `internet` is true, unless networkPolicy.components.<component>.internet
overrides it; then
networkPolicy.egress.extraRules and networkPolicy.components.<component>.extraEgress.
Takes (dict "root" $root "component" "api" "deps" (dict "postgres" true)
"internet" true).
*/}}
{{- define "distant-signal.egressRules" -}}
{{- $root := .root -}}
{{- $np := $root.Values.networkPolicy -}}
{{- $deps := .deps | default dict -}}
{{- $cs := include "distant-signal.npComponent" . | fromYaml -}}
{{- $internet := .internet -}}
{{- if hasKey $cs "internet" -}}
{{- $internet = get $cs "internet" -}}
{{- end -}}
- ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
  {{- with $np.egress.dnsPeers }}
  to:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- if $deps.api }}
- to:
    - podSelector:
        matchLabels:
          {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "api") | nindent 10 }}
  ports:
    - protocol: TCP
      port: {{ $root.Values.api.service.port }}
{{- end }}
{{- if and (or $deps.api $deps.idp) $root.Values.devAuthentik.enabled }}
# The bundled dev IdP serves the OAuth token, discovery and JWKS endpoints.
- to:
    - podSelector:
        matchLabels:
          {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "devauthentik-server") | nindent 10 }}
  ports:
    - protocol: TCP
      port: 9000
{{- end }}
{{- if and $deps.redis $root.Values.redis.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "redis") | nindent 10 }}
  ports:
    - protocol: TCP
      port: {{ $root.Values.redis.service.port }}
{{- end }}
{{- if and $deps.postgres $root.Values.postgresql.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "distant-signal.selectorLabels" (dict "root" $root "component" "postgres") | nindent 10 }}
  ports:
    - protocol: TCP
      port: {{ $root.Values.postgresql.service.port }}
{{- end }}
{{- if $internet }}
{{ include "distant-signal.internetEgressRule" (dict "root" $root "ports" (include "distant-signal.internetPorts" . | fromJsonArray)) }}
{{- end }}
{{- with $np.egress.extraRules }}
{{ toYaml . }}
{{- end }}
{{- with get $cs "extraEgress" }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
networkPolicy.components.<component>.extraIngress as NetworkPolicyIngressRule
list items, or nothing. Takes (dict "root" $root "component" "postgres").
*/}}
{{- define "distant-signal.extraIngress" -}}
{{- $cs := include "distant-signal.npComponent" . | fromYaml -}}
{{- with get $cs "extraIngress" -}}
{{- toYaml . -}}
{{- end -}}
{{- end }}

{{/*
`egress:` plus distant-signal.egressRules for one of the inline policies in
networkpolicy.yaml (api, frontend, postgres, redis, schedulefeed), or
nothing when distant-signal.egressOn is false. Same arguments as
egressRules.
*/}}
{{- define "distant-signal.egressSection" -}}
{{- if include "distant-signal.egressOn" . -}}
egress:
  {{- include "distant-signal.egressRules" . | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
The ingress rule for a workload's health (kubelet probe) ports, per
networkPolicy.healthIngress: from any source (the default), from
healthIngress.from when set, or nothing at all when healthIngress.enabled
is false. Takes (dict "root" $root "ports" (list 8090)); renders one list
item or nothing.
*/}}
{{- define "distant-signal.healthIngressRule" -}}
{{- $h := .root.Values.networkPolicy.healthIngress | default dict -}}
{{- if ne (toString $h.enabled) "false" -}}
- ports:
    {{- range .ports }}
    - protocol: TCP
      port: {{ . }}
    {{- end }}
  {{- with $h.from }}
  from:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end -}}
{{- end }}

{{/*
NetworkPolicy for one background worker (INF-10). Ingress: the worker's
/metrics port from networkPolicy.monitoringNamespace (when metrics.enabled),
and its health port(s) per networkPolicy.healthIngress (by default from
anywhere, because kubelet probes come from the node, which no pod or
namespace selector can name: INF-9's side note). A
worker serves nothing else, so everything else is denied.

Egress (only when networkPolicy.egress.enabled and `egress` is given): DNS,
the in-cluster services the worker actually calls (flags below), the public
internet minus networkPolicy.egress.privateCidrs(V6) and extraDeniedCidrs
(distant-signal.internetEgressRule; RDM/Irish Rail feeds,
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
{{- $egressOn := and .egress (include "distant-signal.egressOn" (dict "root" $root "component" .component)) -}}
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
    {{- include "distant-signal.healthIngressRule" (dict "root" $root "ports" .healthPorts) | nindent 4 }}
    {{- with include "distant-signal.extraIngress" (dict "root" $root "component" .component) }}
    {{- . | nindent 4 }}
    {{- end }}
  {{- if $egressOn }}
  egress:
    {{- include "distant-signal.egressRules" (dict "root" $root "component" .component "deps" .egress "internet" (ternary .internet true (hasKey . "internet")) "urls" .urls "brokers" .brokers) | nindent 4 }}
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

{{/*
The movement-events consumer groups movement-relay creates at the start of a
fresh stream (MOVEMENT_CONSUMER_GROUPS): every consumer reads the stream
since Deploy C removed the consumers' direct-Kafka backend (PL-15a).
Takes root.
*/}}
{{- define "distant-signal.movementConsumerGroups" -}}
{{- print "trust-consumer,full-coverage-consumer,trust-event-backlog" -}}
{{- end }}

{{/*
trustConsumer.movementFeed / fullCoverageConsumer.movementFeed were removed
in Deploy C (PL-15a, R-101): both consumers only read movement-relay's
movement-events stream. A leftover `redis-stream` is harmless and ignored;
anything else (in practice `kafka`) fails the render rather than being
silently ignored, so an install that still expects a direct Kafka consumer
finds out. Takes (dict "name" <values key> "value" <its value or nil>).
*/}}
{{- define "distant-signal.removedMovementFeedGuard" -}}
{{- $value := toString (default "redis-stream" .value) -}}
{{- if ne $value "redis-stream" -}}
{{- fail (printf "%s.movementFeed=%s is no longer supported: the consumers' direct Kafka backend was removed (Deploy C, PL-15a). movement-relay is the only Kafka client and every consumer reads its movement-events stream. Remove %s.movementFeed from your values." .name $value .name) -}}
{{- end -}}
{{- end }}

{{/*
movementRelay.deadLetterMaxAgeSecs, refused outside 3600..86400: the TRUST
1-day retention safeguard caps it at 24 hours. Takes root.
*/}}
{{- define "distant-signal.deadLetterMaxAgeSecs" -}}
{{- $age := int64 .Values.movementRelay.deadLetterMaxAgeSecs -}}
{{- if gt $age 86400 -}}
{{- fail "movementRelay.deadLetterMaxAgeSecs must be at most 86400 (24h, the TRUST 1-day retention safeguard)" -}}
{{- end -}}
{{- if lt $age 3600 -}}
{{- fail "movementRelay.deadLetterMaxAgeSecs must be at least 3600" -}}
{{- end -}}
{{- $age -}}
{{- end }}

{{/*
distant-signal.mergeEnv: a container's env list, the chart's own entries
merged with the operator's extraEnv by name. An extraEnv entry REPLACES the
chart entry of the same name, in the chart entry's place; extraEnv names
the chart does not set follow, in extraEnv order. Within extraEnv the last
entry of a name wins. Every name is rendered once: env is merged by name,
so server-side apply rejects a list that repeats one.

Args: dict "env" (the chart's entries, as rendered YAML text: an `include`
of the container's own env define) and "extra" (the extraEnv list).

The chart's entries the operator does not replace are copied as text,
comments included, so they render exactly as before. The text is cut into
one piece per list item (the lines at the first item's indent that start
with "- "; comment and blank lines go with the item after them) and paired
with the names parsed from the same text. extraEnv entries render with
toYaml, as they always have.
*/}}
{{- define "distant-signal.mergeEnv" -}}
{{- $extra := .extra | default list -}}
{{- $override := dict -}}
{{- range $entry := $extra -}}
{{- if not (and (kindIs "map" $entry) (get $entry "name")) -}}
{{- fail (printf "every extraEnv entry needs a name, got: %s" (toJson $entry)) -}}
{{- end -}}
{{- $_ := set $override $entry.name $entry -}}
{{- end -}}
{{- $text := default "" .env -}}
{{- $names := list -}}
{{- range $entry := fromYamlArray $text -}}
{{- if not (and (kindIs "map" $entry) (get $entry "name")) -}}
{{- fail (printf "distant-signal.mergeEnv: the chart's env is not a list of named entries: %s" (toJson $entry)) -}}
{{- end -}}
{{- $names = append $names $entry.name -}}
{{- end -}}
{{- $chunks := list -}}
{{- $current := list -}}
{{- $pending := list -}}
{{- $indent := -1 -}}
{{- range $line := splitList "\n" $text -}}
{{- $body := trim $line -}}
{{- $at := len (regexFind "^ *" $line) -}}
{{- if or (eq $body "") (hasPrefix "#" $body) -}}
{{- $pending = append $pending $line -}}
{{- else if and (hasPrefix "- " $body) (or (eq $indent -1) (eq $at $indent)) -}}
{{- if $current -}}
{{- $chunks = append $chunks $current -}}
{{- end -}}
{{- $current = append $pending $line -}}
{{- $pending = list -}}
{{- $indent = $at -}}
{{- else -}}
{{- $current = concat $current $pending (list $line) -}}
{{- $pending = list -}}
{{- end -}}
{{- end -}}
{{- if $current -}}
{{- $chunks = append $chunks $current -}}
{{- end -}}
{{- if ne (len $chunks) (len $names) -}}
{{- fail (printf "distant-signal.mergeEnv: cut the chart's env into %d items but parsed %d" (len $chunks) (len $names)) -}}
{{- end -}}
{{- $margin := repeat (max $indent 0 | int) " " -}}
{{- $out := list -}}
{{- $seen := dict -}}
{{- range $i, $chunk := $chunks -}}
{{- $name := index $names $i -}}
{{- if not (hasKey $seen $name) -}}
{{- if hasKey $override $name -}}
{{- $out = append $out (toYaml (list (get $override $name))) -}}
{{- else -}}
{{- $lines := list -}}
{{- range $line := $chunk -}}
{{- $lines = append $lines (trimPrefix $margin $line) -}}
{{- end -}}
{{- $out = append $out (join "\n" $lines) -}}
{{- end -}}
{{- $_ := set $seen $name true -}}
{{- end -}}
{{- end -}}
{{- range $entry := $extra -}}
{{- if not (hasKey $seen $entry.name) -}}
{{- $out = append $out (toYaml (list (get $override $entry.name))) -}}
{{- $_ := set $seen $entry.name true -}}
{{- end -}}
{{- end -}}
{{- join "\n" $out -}}
{{- end }}

{{/*
distant-signal.scheduleFeedValidateBucket: refuse bad scheduleFeed.bucket
values (and the two source settings) at render time, so a typo can't reach
schedule-ingest. Called from schedulefeed-deployment.yaml only when
scheduleFeed.bucket.enabled. Each message names the key.
*/}}
{{- define "distant-signal.scheduleFeedValidateBucket" -}}
{{- $sf := .Values.scheduleFeed -}}
{{- $b := $sf.bucket -}}
{{- $bucketName := "^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$" -}}
{{- if ne ($b.provider | default "" | toString) "gcs" -}}
{{- fail (printf "scheduleFeed.bucket.provider must be \"gcs\" (the only provider implemented), got %q" ($b.provider | default "" | toString)) -}}
{{- end -}}
{{- if not (regexMatch $bucketName ($b.name | default "" | toString)) -}}
{{- fail (printf "scheduleFeed.bucket.name must be a GCS bucket name (3-63 characters of a-z, 0-9, - and _, starting and ending with a letter or digit, no dots), got %q" ($b.name | default "" | toString)) -}}
{{- end -}}
{{- $dnsName := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" -}}
{{- $secret := $b.existingSecret | default "" | toString -}}
{{- $wi := $b.workloadIdentity | default dict -}}
{{- $cm := $wi.credentialConfigMap | default "" | toString -}}
{{- if include "distant-signal.scheduleFeedBucketWif" . -}}
{{- /* Keyless: the projected token, the external_account config from a
ConfigMap, a dedicated ServiceAccount, and no key anywhere. */ -}}
{{- if or $secret (ne ($b.serviceAccountKey | default "service-account.json" | toString) "service-account.json") -}}
{{- fail "scheduleFeed.bucket.auth=workloadIdentity is keyless: unset scheduleFeed.bucket.existingSecret and scheduleFeed.bucket.serviceAccountKey (key mode only)" -}}
{{- end -}}
{{- if not (and (regexMatch $dnsName $cm) (le (len $cm) 253)) -}}
{{- fail (printf "scheduleFeed.bucket.auth=workloadIdentity needs scheduleFeed.bucket.workloadIdentity.credentialConfigMap: the pre-existing ConfigMap holding the external_account credential configuration, got %q" $cm) -}}
{{- end -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" ($wi.credentialConfigKey | default "" | toString)) -}}
{{- fail (printf "scheduleFeed.bucket.workloadIdentity.credentialConfigKey must be a ConfigMap key (letters, digits, -, _ and .), got %q" ($wi.credentialConfigKey | default "" | toString)) -}}
{{- end -}}
{{- if not ($wi.audience | default "" | toString) -}}
{{- fail "scheduleFeed.bucket.auth=workloadIdentity needs scheduleFeed.bucket.workloadIdentity.audience: the audience the Google workload identity pool provider allows" -}}
{{- end -}}
{{- $sa := $sf.serviceAccount | default dict -}}
{{- if or (not (or $sa.create $sa.name)) (eq (include "distant-signal.scheduleFeedServiceAccountName" .) (include "distant-signal.serviceAccountName" .)) -}}
{{- fail (printf "scheduleFeed.bucket.auth=workloadIdentity needs a dedicated ServiceAccount: set scheduleFeed.serviceAccount.create=true (or scheduleFeed.serviceAccount.name to an existing account other than the shared %q). Google trusts the subject system:serviceaccount:<namespace>:<that name>; on the shared account every pod in the release could read the bucket." (include "distant-signal.serviceAccountName" .)) -}}
{{- end -}}
{{- else -}}
{{- if not (and (regexMatch $dnsName $secret) (le (len $secret) 253)) -}}
{{- fail (printf "scheduleFeed.bucket.existingSecret must name the pre-existing Secret holding the reader's service-account key (sealed in deploy config; never put the key in values), got %q" $secret) -}}
{{- end -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" ($b.serviceAccountKey | default "" | toString)) -}}
{{- fail (printf "scheduleFeed.bucket.serviceAccountKey must be a Secret key (letters, digits, -, _ and .), got %q" ($b.serviceAccountKey | default "" | toString)) -}}
{{- end -}}
{{- if $cm -}}
{{- fail "scheduleFeed.bucket.workloadIdentity.credentialConfigMap is set but scheduleFeed.bucket.auth is key: set auth=workloadIdentity (and unset existingSecret), or unset credentialConfigMap" -}}
{{- end -}}
{{- end -}}
{{- $keys := $b.expectedKeys | default list -}}
{{- if not (and (kindIs "slice" $keys) $keys) -}}
{{- fail "scheduleFeed.bucket.expectedKeys must list at least one object name (glob) to download" -}}
{{- end -}}
{{- range $keys -}}
{{- $k := . | default "" | toString -}}
{{- if or (eq $k "") (contains "/" $k) (contains "," $k) (gt (len $k) 1024) -}}
{{- fail (printf "scheduleFeed.bucket.expectedKeys: %q must be a non-empty name at the bucket root (no / or ,) of at most 1024 bytes" $k) -}}
{{- end -}}
{{- end -}}
{{- with $b.baseUrl -}}
{{- if not (regexMatch "^https?://[^/]+" (toString .)) -}}
{{- fail (printf "scheduleFeed.bucket.baseUrl must be empty or an http(s):// URL, got %q" (toString .)) -}}
{{- end -}}
{{- end -}}
{{- $poll := $b.pollIntervalSecs | int64 -}}
{{- if lt $poll 60 -}}
{{- fail (printf "scheduleFeed.bucket.pollIntervalSecs must be at least 60, got %d" $poll) -}}
{{- end -}}
{{- if lt ($b.maxBackoffSecs | int64) $poll -}}
{{- fail (printf "scheduleFeed.bucket.maxBackoffSecs (%d) must be at least scheduleFeed.bucket.pollIntervalSecs (%d)" ($b.maxBackoffSecs | int64) $poll) -}}
{{- end -}}
{{- $age := $b.deleteMinAgeSecs | int64 -}}
{{- if or (lt $age 0) (ge $age 518400) -}}
{{- fail (printf "scheduleFeed.bucket.deleteMinAgeSecs must be at least 0 and under 518400 (6 days; the bucket's 7-day lifecycle rule is the backstop), got %d" $age) -}}
{{- end -}}
{{- if lt ($b.archiveKeep | int64) 1 -}}
{{- fail "scheduleFeed.bucket.archiveKeep must be at least 1" -}}
{{- end -}}
{{- if lt ($b.maxDownloadsPerPoll | int64) 1 -}}
{{- fail "scheduleFeed.bucket.maxDownloadsPerPoll must be at least 1" -}}
{{- end -}}
{{- $object := $b.maxObjectBytes | int64 -}}
{{- $hour := $b.maxDownloadBytesPerHour | int64 -}}
{{- $day := $b.maxDownloadBytesPerDay | int64 -}}
{{- if not (and (ge $object 1) (le $object $hour) (le $hour $day)) -}}
{{- fail (printf "scheduleFeed.bucket needs 1 <= maxObjectBytes <= maxDownloadBytesPerHour <= maxDownloadBytesPerDay, got %d, %d and %d" $object $hour $day) -}}
{{- end -}}
{{- $order := $sf.sourcePrecedence | default list -}}
{{- if not (and (kindIs "slice" $order) (eq (len $order) 2) (has "bucket" $order) (has "sftp" $order)) -}}
{{- fail (printf "scheduleFeed.sourcePrecedence must be [bucket, sftp] or [sftp, bucket], got %s" (toJson $order)) -}}
{{- end -}}
{{- if lt ($sf.disagreementWindowMinutes | int64) 0 -}}
{{- fail "scheduleFeed.disagreementWindowMinutes must be at least 0" -}}
{{- end -}}
{{- with $b.auditLogs -}}
{{- if .ship -}}
{{- $audit := .bucket | default "" | toString -}}
{{- if or (not (regexMatch $bucketName $audit)) (eq $audit ($b.name | toString)) -}}
{{- fail (printf "scheduleFeed.bucket.auditLogs.ship needs scheduleFeed.bucket.auditLogs.bucket: the audit-log bucket's name (no dots), not scheduleFeed.bucket.name; got %q" $audit) -}}
{{- end -}}
{{- if lt (.pollIntervalSecs | int64) 60 -}}
{{- fail (printf "scheduleFeed.bucket.auditLogs.pollIntervalSecs must be at least 60, got %d" (.pollIntervalSecs | int64)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
