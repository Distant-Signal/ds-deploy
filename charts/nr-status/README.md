# nr-status Helm chart

Deploys the whole National Rail status stack into a single namespace: a
bundled single-replica **PostgreSQL** StatefulSet, a bundled single-replica
**Redis** (a disposable trigger queue, no persistence), the **api**, the
**aggregator**, the **enricher**, the **frontend**, and four optional Rail
Data Marketplace **pollers** (incidents, stations, tocs, ldbws). The chart has no subchart
dependencies and no `dependencies:` block, so `helm dependency update` is
never needed and it installs in an air-gapped cluster given the images. It
mirrors the topology, environment contract and cadences that the
repository's `docker-compose.yml` and `.env.example` already establish, so
the two deployment paths do not drift.

## Prerequisites

- **Kubernetes >= 1.23.** The chart declares `kubeVersion: ">=1.23.0-0"`.
  The NetworkPolicy templates select the ingress-controller namespace via
  the automatic `kubernetes.io/metadata.name` label, GA from 1.22.
- **Helm 3.8+ or 4.x.** Developed and verified against Helm v4.1.4.
- **A default StorageClass**, or set `postgresql.persistence.storageClass`
  explicitly — the bundled Postgres uses a `volumeClaimTemplates` entry. Set
  `postgresql.persistence.enabled: false` for throwaway testing (data is
  lost on reschedule), or `postgresql.enabled: false` to use a managed
  database.
- **Images already present in a registry the cluster can pull from.** This
  chart builds nothing.

## Building and pushing the images (manual)

There is **no image build or publish pipeline in this repository**, and this
chart does not add one. The Dockerfiles build locally only; you must build
and push them yourself before installing.

| Dockerfile | Default image repository |
|---|---|
| `docker/api.Dockerfile` | `nr-status/api` |
| `docker/aggregator.Dockerfile` | `nr-status/aggregator` |
| `docker/enricher.Dockerfile` | `nr-status/enricher` |
| `docker/poller-incidents.Dockerfile` | `nr-status/poller-incidents` |
| `docker/poller-stations.Dockerfile` | `nr-status/poller-stations` |
| `docker/poller-tocs.Dockerfile` | `nr-status/poller-tocs` |
| `docker/poller-ldbws.Dockerfile` | `nr-status/poller-ldbws` |
| `frontend/Dockerfile` (target `runtime-prod`) | `nr-status/frontend` |

```bash
REG=registry.example.com/nr-status
TAG=0.1.0
docker build -f docker/api.Dockerfile              -t $REG/api:$TAG .
docker build -f docker/aggregator.Dockerfile       -t $REG/aggregator:$TAG .
docker build -f docker/enricher.Dockerfile         -t $REG/enricher:$TAG .
docker build -f docker/poller-incidents.Dockerfile -t $REG/poller-incidents:$TAG .
docker build -f docker/poller-stations.Dockerfile  -t $REG/poller-stations:$TAG .
docker build -f docker/poller-tocs.Dockerfile      -t $REG/poller-tocs:$TAG .
docker build -f docker/poller-ldbws.Dockerfile     -t $REG/poller-ldbws:$TAG .
docker build -f frontend/Dockerfile --target runtime-prod -t $REG/frontend:$TAG .
for i in api aggregator enricher poller-incidents poller-stations poller-tocs poller-ldbws frontend; do
  docker push $REG/$i:$TAG
done
```

Redis is **not** in this table: the bundled Redis uses the upstream `redis`
image (`redis.image.*`), which this repository does not build.

Then point each `*.image.repository` value at `$REG/...`. An empty
`image.tag` falls back to the chart's `appVersion`.

## Install

```bash
helm install nr-status ./charts/nr-status -n nr-status --create-namespace \
  --set enricher.llm.baseUrl=https://llm.example.com/v1 \
  --set enricher.llm.model=your-model-name
```

An install brings up **postgres + redis + api + aggregator + enricher +
frontend**, with **all four pollers off**. See "Enabling the pollers" below
for why.

`enricher.llm.baseUrl` and `enricher.llm.model` are the chart's only two
**required** values — the enricher has no `enabled` toggle, and both become
plain (non-optional) env vars on its binary, so an empty value would deploy
a pod that fails every extraction request forever with nothing but log noise
to show for it. Leaving either empty **aborts the render** with an explicit
message rather than deploying that. Everything else has a working default.

The enricher is a strictly additive signal: it only ever *demotes* a
severity a line already has, and never suppresses one. A missing, failed or
low-confidence extraction is a no-op, so a broken LLM endpoint degrades the
enricher's own output and nothing else — the status pages keep working.

With the worked example values:

```bash
helm install nr-status ./charts/nr-status -n nr-status --create-namespace \
  -f charts/nr-status/values-example.yaml
```

## Upgrade

```bash
helm upgrade nr-status ./charts/nr-status -n nr-status
```

Read the next section before upgrading if you rely on generated secrets.

## Generated secrets and the `lookup` limitation

`postgres-password` and `internal-token` are generated with
`randAlphaNum 32` when their values are left empty and no `existingSecret`
is given.

They are **preserved across `helm upgrade`**: `templates/secret.yaml` reads
the live Secret back out of the cluster with Helm's `lookup` function and
reuses whatever is already there, rather than generating a fresh password
and rotating it out from under the running database's PVC.

**Limitation:** `lookup` returns nothing during `helm template` and
`--dry-run`, so an offline render shows a **different** generated value
every time you run it. That is cosmetic for dry runs, but it does mean
**`helm template | kubectl apply` is not a supported install path when you
rely on generated secrets** — the applied password would differ from the one
already in the cluster. For that workflow, set explicit values
(`postgresql.auth.password`, `secrets.internalToken`) or use `existingSecret`.

Read the generated values back out:

```bash
kubectl get secret -n nr-status nr-status \
  -o jsonpath='{.data.postgres-password}' | base64 -d; echo
kubectl get secret -n nr-status nr-status \
  -o jsonpath='{.data.internal-token}' | base64 -d; echo
```

## Using externally-managed secrets

Every secret value accepts an `existingSecret` + key override, so a
production install can leave all values empty and point at Secrets managed
by External Secrets Operator, Vault or SOPS. **Any key supplied this way is
omitted from the chart-rendered Secret entirely** — the chart never sees,
stores or renders the value.

```yaml
postgresql:
  auth:
    existingSecret: nr-status-db
    existingSecretPasswordKey: password

secrets:
  existingSecret: nr-status-shared
  existingSecretInternalTokenKey: internal-token

pollers:
  incidents:
    enabled: true
    baseUrl: https://rdm.example.com/incidents
    existingSecret: nr-status-rdm
    existingSecretApiKeyKey: incidents-api-key
  ldbws:
    enabled: true
    baseUrl: https://rdm.example.com/LDBWS/api/20220120
    existingSecret: nr-status-rdm
    existingSecretApiKeyKey: ldbws-api-key

enricher:
  llm:
    baseUrl: https://llm.example.com/v1
    model: your-model-name
    existingSecret: nr-status-llm
    existingSecretApiKeyKey: llm-api-key
```

The Postgres StatefulSet and its api/aggregator/enricher consumers resolve
the password reference through the *same* template helper, so an
`existingSecret` override can never leave them disagreeing about which
Secret holds the password.

## Using an external database

Set `postgresql.enabled: false` and configure `externalDatabase`. Provide
**either** an `existingSecret` holding the whole connection URL (preferred —
it keeps the password out of `helm get values` as well as out of the
Deployment spec):

```yaml
postgresql:
  enabled: false
externalDatabase:
  existingSecret: nr-status-db
  existingSecretUrlKey: database-url
```

**or** a literal URL:

```yaml
postgresql:
  enabled: false
externalDatabase:
  url: postgres://nr_status:s3cret@db.example.com:5432/nr_status
```

Setting `postgresql.enabled: false` with neither aborts rendering with an
explicit message rather than deploying an api that cannot connect. When the
bundled Postgres is disabled, no Postgres objects render at all and no
`PGPASSWORD` env var is injected.

## Using an external Redis

Redis here is a **disposable trigger queue, not a system of record**: the api
publishes an event when an incident's text changes, and the enricher consumes
it to re-extract promptly. Losing the queue costs nothing but promptness —
the enricher's hourly sweep re-finds anything a dropped event would have
triggered, and the api logs and continues if a publish fails. The bundled
Redis therefore runs with **no persistence** on purpose.

Set `redis.enabled: false` and give a URL to point at a managed instance
instead:

```yaml
redis:
  enabled: false
  externalUrl: redis://redis.example.com:6379
```

Setting `redis.enabled: false` **without** `redis.externalUrl` aborts
rendering with an explicit message — previously the chart would silently
point both the api and the enricher at an in-chart Service that was never
created. Unlike `DATABASE_URL` there is no `existingSecret` form; a URL with
inline credentials works but is visible in the rendered Deployment.

## Password encoding caveat

`DATABASE_URL` is a URL. A password containing any of `@ : / ? # [ ] %` must
be **percent-encoded by you** before being put into
`postgresql.auth.password` (or into an `existingSecret`). The chart cannot
do it for you: with `existingSecret` it never sees the value, and with the
bundled path the password is injected as `$(PGPASSWORD)` and expanded by the
kubelet at container start, never by the template engine.

Generated passwords use `randAlphaNum` (letters and digits only), so the
default path is never affected.

The password is deliberately never written into a Deployment spec. It is
injected as its own `secretKeyRef` env entry and referenced from
`DATABASE_URL` with Kubernetes' `$(VAR)` syntax, so `get deployments` — a
strictly wider audience than `get secrets` — never sees it.

## Ingress

One `Ingress` object with up to two **separate hostnames**, both optional and
independently toggleable:

| Value | Backend | Path |
|---|---|---|
| `ingress.frontend.host` | frontend Service :3000 | `/` |
| `ingress.api.host` | api Service :8080 | `/` |

**Separate hostnames, not path-splitting one host.** The api mounts its
four TfL-compatible line-status endpoints at *unprefixed* top-level paths
(`GET /Line/Mode/national-rail/Status`, `GET /StopPoint/{crs}/Disruption`)
rather than under `/public`, so clients written against TfL's own API work
unchanged (`crates/api/src/routes/mod.rs`). Splitting a single host by path
prefix would either collide with Next.js's own routes or break that
compatibility.

`className`, arbitrary `annotations` and a `tls` list are all pass-through
values; the `tls` block is shaped for cert-manager's `cluster-issuer`
annotation but is issuer-agnostic.

> **Security warning.** Enabling `ingress.api.enabled` publishes `/private/*`
> to the internet as well. Those ingestion endpoints are protected **only**
> by the `X-Internal-Token` shared secret (`crates/api/src/auth.rs`) — there
> is no other authentication in front of them. If you do not need external
> API access, leave `ingress.api.enabled: false`; the frontend reaches the
> api over the in-cluster Service either way.

Enabling either host without setting its hostname aborts the render.

## NetworkPolicy

Off by default (`networkPolicy.enabled: false`). Many clusters run a CNI
that does not enforce NetworkPolicy at all, and a silently-unenforced policy
is worse than an absent one because it looks like protection.

When enabled, the chart renders default-deny ingress per workload plus these
explicit allows:

- **postgres** ← api, aggregator, enricher only (the pollers never talk to
  it; they reach the database only indirectly, via the api's ingest
  endpoints).
- **redis** ← api (publisher) and enricher (consumer) only. Rendered only
  when `redis.enabled`.
- **api** ← frontend, every enabled poller, and — when `ingress.enabled` and
  `ingress.api.enabled` — the namespace named by
  `networkPolicy.ingressControllerNamespace`.
- **frontend** ← that same ingress-controller namespace, when
  `ingress.enabled` and `ingress.frontend.enabled`.
- **aggregator**, **enricher** and every poller: default-deny; they expose no
  listener.

**Egress is deliberately unrestricted.** The pollers must reach arbitrary
external Rail Data Marketplace hosts, and constraining that would mean
making every operator enumerate them.

## Enabling the pollers

| Poller | Base URL env var | Ingest path | Default cadence (s) |
|---|---|---|---|
| `incidents` | `RDM_INCIDENTS_BASE_URL` | `/private/incidents` | 300 |
| `stations` | `RDM_STATIONS_BASE_URL` | `/private/stations` | 86400 |
| `tocs` | `RDM_TOCS_BASE_URL` | `/private/tocs` | 86400 |
| `ldbws` | `LDBWS_BASE_URL` | `/private/station-samples` | 60 |

`ldbws` additionally sets `NUM_ROWS` and `API_SAMPLE_STATIONS_URL`
(`/private/sample-stations`), which is a second api endpoint separate from
its ingest path.

**All four are disabled by default** because, as documented in
`.env.example`, no confirmed Rail Data Marketplace endpoint exists for any
of the four feeds — every base URL in the repository today is a deliberately
non-functional `*.example.invalid` placeholder. A default install therefore
works immediately instead of running four pods that log connection failures.

Enabling a poller without setting its `baseUrl` **aborts the render** with an
explicit message, rather than deploying a pod that cannot work.

```bash
helm upgrade nr-status ./charts/nr-status -n nr-status \
  --set pollers.incidents.enabled=true \
  --set pollers.incidents.baseUrl=https://rdm.example.com/incidents \
  --set pollers.incidents.apiKey=your-rdm-key
```

## Values reference

### Global

| Key | Default | Description |
|---|---|---|
| `nameOverride` | `""` | Override the chart name used in resource names and labels. |
| `fullnameOverride` | `""` | Override the fully-qualified release name entirely. |
| `imagePullSecrets` | `[]` | Image pull secrets applied to every pod in the chart. |
| `serviceAccount.create` | `true` | Create a ServiceAccount for the chart's workloads. |
| `serviceAccount.name` | `""` | Name to use. Empty + `create` uses the fullname. |
| `serviceAccount.annotations` | `{}` | Annotations for the ServiceAccount (e.g. workload identity). |

### Shared secrets

| Key | Default | Description |
|---|---|---|
| `secrets.internalToken` | `""` | Shared `X-Internal-Token` secret. Generated (32 alphanumeric chars) when empty. |
| `secrets.existingSecret` | `""` | Read the internal token from this pre-existing Secret instead. |
| `secrets.existingSecretInternalTokenKey` | `internal-token` | Key within `secrets.existingSecret`. |

### postgresql

There is intentionally no `replicaCount`: this is a single-replica
StatefulSet with no replication, backup or restore story.

| Key | Default | Description |
|---|---|---|
| `postgresql.enabled` | `true` | Deploy the bundled PostgreSQL StatefulSet. |
| `postgresql.auth.username` | `nr_status` | Database role the api and aggregator connect as. |
| `postgresql.auth.database` | `nr_status` | Database name. |
| `postgresql.auth.password` | `""` | Password. Generated when empty. Percent-encode reserved characters yourself. |
| `postgresql.auth.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.auth.existingSecretPasswordKey` | `postgres-password` | Key within `postgresql.auth.existingSecret`. |
| `postgresql.image.repository` | `postgres` | PostgreSQL image repository. |
| `postgresql.image.tag` | `"16"` | Pinned to the major the compose stack uses. |
| `postgresql.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `postgresql.service.port` | `5432` | Port the headless Service and the container listen on. |
| `postgresql.persistence.enabled` | `true` | Attach a PVC. When false an emptyDir is used and data is lost on reschedule. |
| `postgresql.persistence.size` | `8Gi` | Requested volume size. |
| `postgresql.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `postgresql.persistence.accessModes` | `[ReadWriteOnce]` | PVC access modes. |
| `postgresql.persistence.existingClaim` | `""` | Use a pre-existing PVC instead of a `volumeClaimTemplates` entry. |
| `postgresql.extraEnv` | `[]` | Extra container env vars (e.g. `shared_buffers` tuning). |
| `postgresql.podSecurityContext` | `{}` | Merged over the pod securityContext. uid/gid/fsGroup 999 are pinned by default and required by this image. |
| `postgresql.resources` | `{}` | Container resource requests/limits. |
| `postgresql.nodeSelector` | `{}` | Pod node selector. |
| `postgresql.tolerations` | `[]` | Pod tolerations. |
| `postgresql.affinity` | `{}` | Pod affinity rules. |
| `postgresql.podAnnotations` | `{}` | Pod annotations. |

### externalDatabase

Used only when `postgresql.enabled` is `false`.

| Key | Default | Description |
|---|---|---|
| `externalDatabase.url` | `""` | Full connection URL, e.g. `postgres://user:pass@host:5432/nr_status`. |
| `externalDatabase.existingSecret` | `""` | Pre-existing Secret holding the whole connection URL (preferred). |
| `externalDatabase.existingSecretUrlKey` | `database-url` | Key within `externalDatabase.existingSecret`. |

### api

| Key | Default | Description |
|---|---|---|
| `api.image.repository` | `nr-status/api` | api image repository. |
| `api.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `api.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `api.replicaCount` | `1` | Replicas. >1 is safe — sqlx's Migrator takes a Postgres advisory lock. |
| `api.service.type` | `ClusterIP` | Service type. |
| `api.service.port` | `8080` | Service and container port; also sets `BIND_URL`. |
| `api.logLevel` | `info` | `RUST_LOG` value (tracing-subscriber EnvFilter syntax). |
| `api.probes.path` | `/public/health` | Path all three probes and the `helm test` pod hit. |
| `api.probes.startup.periodSeconds` | `2` | Startup probe period. |
| `api.probes.startup.failureThreshold` | `30` | Startup probe failures allowed (30 x 2s = 60s for in-process migrations). |
| `api.probes.startup.timeoutSeconds` | `3` | Startup probe timeout. |
| `api.probes.readiness.periodSeconds` | `10` | Readiness probe period. |
| `api.probes.readiness.failureThreshold` | `3` | Readiness probe failures allowed. |
| `api.probes.readiness.timeoutSeconds` | `3` | Readiness probe timeout. |
| `api.probes.liveness.periodSeconds` | `10` | Liveness probe period. |
| `api.probes.liveness.failureThreshold` | `3` | Liveness probe failures allowed. |
| `api.probes.liveness.timeoutSeconds` | `3` | Liveness probe timeout. |
| `api.extraEnv` | `[]` | Extra env vars appended to the container. |
| `api.resources` | `{}` | Container resource requests/limits. |
| `api.nodeSelector` | `{}` | Pod node selector. |
| `api.tolerations` | `[]` | Pod tolerations. |
| `api.affinity` | `{}` | Pod affinity rules. |
| `api.podAnnotations` | `{}` | Pod annotations. |
| `api.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### aggregator

There is intentionally no `replicaCount`: the aggregator is a singleton
write loop, pinned to `replicas: 1` with `strategy: Recreate`.

| Key | Default | Description |
|---|---|---|
| `aggregator.image.repository` | `nr-status/aggregator` | aggregator image repository. |
| `aggregator.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `aggregator.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `aggregator.pollIntervalSecs` | `60` | Recompute cadence. |
| `aggregator.historyRetentionDays` | `7` | How long `line_status_history` rows are kept. |
| `aggregator.logLevel` | `info` | `RUST_LOG` value. |
| `aggregator.extraEnv` | `[]` | Extra env vars appended to the container. |
| `aggregator.resources` | `{}` | Container resource requests/limits. |
| `aggregator.nodeSelector` | `{}` | Pod node selector. |
| `aggregator.tolerations` | `[]` | Pod tolerations. |
| `aggregator.affinity` | `{}` | Pod affinity rules. |
| `aggregator.podAnnotations` | `{}` | Pod annotations. |
| `aggregator.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### redis

There is intentionally no `replicaCount` and no persistence: this is a
disposable trigger queue, not a system of record. See "Using an external
Redis" above.

| Key | Default | Description |
|---|---|---|
| `redis.enabled` | `true` | Deploy the bundled Redis Deployment and Service. |
| `redis.externalUrl` | `""` | Connection URL of an externally-managed Redis. Used only when `redis.enabled` is false, where it is **required** — empty aborts the render. |
| `redis.image.repository` | `redis` | Redis image repository (upstream image; this repo builds no Redis image). |
| `redis.image.tag` | `"7"` | Pinned to the major the compose stack uses. |
| `redis.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `redis.service.port` | `6379` | Service and container port; also sets the `REDIS_URL` the api and enricher get. |
| `redis.resources` | `{}` | Container resource requests/limits. |
| `redis.nodeSelector` | `{}` | Pod node selector. |
| `redis.tolerations` | `[]` | Pod tolerations. |
| `redis.affinity` | `{}` | Pod affinity rules. |
| `redis.podAnnotations` | `{}` | Pod annotations. |
| `redis.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### enricher

There is intentionally no `replicaCount` and no `enabled` toggle: the
enricher is a singleton consumer of one Redis consumer group plus one sweep
loop, and it renders unconditionally.

`enricher.llm.baseUrl` and `enricher.llm.model` are the chart's **only two
required values** — leaving either empty aborts the render, because both
become non-optional env vars on the binary and an empty value would deploy a
pod that fails every request forever.

| Key | Default | Description |
|---|---|---|
| `enricher.image.repository` | `nr-status/enricher` | enricher image repository. |
| `enricher.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `enricher.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `enricher.llm.baseUrl` | `""` | **Required.** Base URL of an OpenAI-compatible chat-completions endpoint. Empty aborts the render. |
| `enricher.llm.model` | `""` | **Required.** Model name that endpoint serves. Empty aborts the render. Also stored as the extraction's `model_version`, so changing it re-extracts every incident on the next sweep. |
| `enricher.llm.apiKey` | `""` | API key for that endpoint. Rendered into the chart Secret when `existingSecret` is empty. Empty is valid for a local endpoint needing no auth, and is never auto-generated. |
| `enricher.llm.existingSecret` | `""` | Read the API key from this pre-existing Secret instead. |
| `enricher.llm.existingSecretApiKeyKey` | `llm-api-key` | Key within `enricher.llm.existingSecret`. |
| `enricher.llmRequestTimeoutSecs` | `120` | Per-request timeout for a single LLM call. One incident makes three sequential calls, so raise `reclaimMinIdleSecs` to match if you raise this. |
| `enricher.sweepIntervalSecs` | `3600` | Cadence of the backstop sweep that re-checks every uncleared incident's text hash and model version. |
| `enricher.reclaimIntervalSecs` | `60` | How often the reclaim loop checks for stream entries stuck unacked past `reclaimMinIdleSecs` (a timed-out request, or a crash between processing and acking). |
| `enricher.reclaimMinIdleSecs` | `600` | How long a pending entry must sit unacked before it's eligible for reclaim. Must stay comfortably above `3 * llmRequestTimeoutSecs`. |
| `enricher.logLevel` | `info` | `RUST_LOG` value. |
| `enricher.extraEnv` | `[]` | Extra env vars appended to the container. |
| `enricher.resources` | `{}` | Container resource requests/limits. |
| `enricher.nodeSelector` | `{}` | Pod node selector. |
| `enricher.tolerations` | `[]` | Pod tolerations. |
| `enricher.affinity` | `{}` | Pod affinity rules. |
| `enricher.podAnnotations` | `{}` | Pod annotations. |
| `enricher.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### frontend

| Key | Default | Description |
|---|---|---|
| `frontend.image.repository` | `nr-status/frontend` | frontend image repository. |
| `frontend.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `frontend.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `frontend.replicaCount` | `1` | Replicas. |
| `frontend.service.type` | `ClusterIP` | Service type. |
| `frontend.service.port` | `3000` | Service and container port. |
| `frontend.probes.path` | `/` | Probe path. Next.js ships no dedicated health route. |
| `frontend.probes.readiness.periodSeconds` | `10` | Readiness probe period. |
| `frontend.probes.readiness.failureThreshold` | `3` | Readiness probe failures allowed. |
| `frontend.probes.readiness.timeoutSeconds` | `3` | Readiness probe timeout. |
| `frontend.probes.liveness.periodSeconds` | `10` | Liveness probe period. |
| `frontend.probes.liveness.failureThreshold` | `3` | Liveness probe failures allowed. |
| `frontend.probes.liveness.timeoutSeconds` | `3` | Liveness probe timeout. |
| `frontend.apiBaseUrl` | `""` | Override `API_BASE_URL`. Empty uses the in-cluster api Service. |
| `frontend.extraEnv` | `[]` | Extra env vars appended to the container. |
| `frontend.resources` | `{}` | Container resource requests/limits. |
| `frontend.nodeSelector` | `{}` | Pod node selector. |
| `frontend.tolerations` | `[]` | Pod tolerations. |
| `frontend.affinity` | `{}` | Pod affinity rules. |
| `frontend.podAnnotations` | `{}` | Pod annotations. |
| `frontend.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

The frontend is the one workload with `readOnlyRootFilesystem: false`:
`next start` writes its incremental cache under `.next/cache`.

### pollers

Keys below exist under each of `pollers.incidents`, `pollers.stations`,
`pollers.tocs` and `pollers.ldbws`; the last two rows are ldbws-only.

| Key | Default | Description |
|---|---|---|
| `pollers.<name>.enabled` | `false` | Deploy this poller. All four are off by default. |
| `pollers.<name>.image.repository` | `nr-status/poller-<name>` | Poller image repository. |
| `pollers.<name>.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollers.<name>.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollers.<name>.baseUrl` | `""` | Upstream feed base URL. Required when enabled; empty aborts the render. |
| `pollers.<name>.baseUrlEnvVar` | per-poller | Env var the binary reads the base URL from. Do not change. |
| `pollers.<name>.ingestPath` | per-poller | Path on the api Service this poller POSTs results to. |
| `pollers.<name>.pollIntervalSecs` | 300 / 86400 / 86400 / 60 | Poll cadence. |
| `pollers.<name>.apiKey` | `""` | RDM API key. Rendered into the chart Secret when `existingSecret` is empty. |
| `pollers.<name>.existingSecret` | `""` | Read the API key from this pre-existing Secret instead. |
| `pollers.<name>.existingSecretApiKeyKey` | `rdm-<name>-api-key` | Key within `pollers.<name>.existingSecret`. |
| `pollers.<name>.logLevel` | `info` | `RUST_LOG` value. |
| `pollers.<name>.extraEnv` | `[]` | Extra env vars appended to the container. |
| `pollers.<name>.resources` | `{}` | Container resource requests/limits. |
| `pollers.<name>.nodeSelector` | `{}` | Pod node selector. |
| `pollers.<name>.tolerations` | `[]` | Pod tolerations. |
| `pollers.<name>.affinity` | `{}` | Pod affinity rules. |
| `pollers.<name>.podAnnotations` | `{}` | Pod annotations. |
| `pollers.<name>.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `pollers.ldbws.sampleStationsPath` | `/private/sample-stations` | ldbws only: second api endpoint listing which stations to sample. |
| `pollers.ldbws.numRows` | `10` | ldbws only: LDBWS `numRows` query parameter. |

### ingress

| Key | Default | Description |
|---|---|---|
| `ingress.enabled` | `false` | Render the Ingress object. |
| `ingress.className` | `""` | IngressClass name. Empty uses the cluster default. |
| `ingress.annotations` | `{}` | Annotations on the Ingress, e.g. `cert-manager.io/cluster-issuer`. |
| `ingress.frontend.enabled` | `true` | Publish the frontend host (only when `ingress.enabled`). |
| `ingress.frontend.host` | `""` | Hostname for the web UI. Required when enabled. |
| `ingress.api.enabled` | `false` | Publish the api host. **Also exposes `/private/*`.** |
| `ingress.api.host` | `""` | Hostname for the api. Required when enabled. |
| `ingress.tls` | `[]` | TLS blocks passed through verbatim. |

### networkPolicy

| Key | Default | Description |
|---|---|---|
| `networkPolicy.enabled` | `false` | Render default-deny NetworkPolicies with explicit allows. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | Namespace the ingress controller runs in, matched by `kubernetes.io/metadata.name`. |

### tests

| Key | Default | Description |
|---|---|---|
| `tests.enabled` | `true` | Render the `helm test` hook Pod. |
| `tests.image.repository` | `""` | Empty reuses the api image, which already ships `curl`. |
| `tests.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `tests.image.pullPolicy` | `IfNotPresent` | Image pull policy. |

## Testing

```bash
helm test nr-status -n nr-status
```

The hook Pod runs `curl -fsS --max-time 10 http://<release>-api:8080/public/health`
against the in-cluster api Service; `-f` makes curl exit non-zero on any HTTP
error status, which is what `helm test` reads as failure. Running it requires
a **live cluster with the images available** — it is an operator step, not
part of chart authoring.

## Uninstall

```bash
helm uninstall nr-status -n nr-status
```

> **The PVC created by `volumeClaimTemplates` survives uninstall.** Helm does
> not delete StatefulSet volume claims, which is deliberate — it is what
> stops an accidental `helm uninstall` from destroying the database. If you
> do not want the data, delete it manually:
>
> ```bash
> kubectl delete pvc -n nr-status -l app.kubernetes.io/instance=nr-status
> ```

## Not in scope

- **No image build or publish pipeline.** See the manual loop above.
- **No HorizontalPodAutoscaler.** The aggregator, the enricher and all four
  pollers are singleton loops that must not be scaled, and the api is
  database-bound.
- **No persistence, backup or HA for the bundled Redis.** It is a disposable
  trigger queue; see "Using an external Redis" above.
- **No backup, restore or replication** for the bundled Postgres. It is a
  single-replica StatefulSet on a PVC. Set `postgresql.enabled: false` and
  use a managed database if you need HA.
- **No metrics or ServiceMonitor wiring** — the services expose no metrics
  endpoint today.
