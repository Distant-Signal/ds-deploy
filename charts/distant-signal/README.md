# distant-signal Helm chart

Deploys the whole National Rail status stack into a single namespace: a
bundled single-replica **PostgreSQL** StatefulSet, a bundled single-replica
**Redis** (a disposable trigger queue, no persistence), the **api**, the
**aggregator**, the **enricher**, the **frontend**, and five optional
**pollers** — four Rail Data Marketplace pollers (incidents, stations, tocs, ldbws)
plus a TfL Unified API poller (tfl). The chart has no subchart
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

`.github/workflows/containers.yml` builds and publishes every image below to
`ghcr.io/fasterspeeding/distant-signal/<service>` automatically (build-only
sanity check on PRs; build + push + cosign-sign on push to main/master) — you
do not need to do this by hand for a normal install. The table and commands
below are for building and pushing to a registry of your own (e.g. a private
registry, or testing a local change) without going through that pipeline.

| Dockerfile | Default image repository |
|---|---|
| `docker/api.Dockerfile` | `distant-signal/api` |
| `docker/aggregator.Dockerfile` | `distant-signal/aggregator` |
| `docker/enricher.Dockerfile` | `distant-signal/enricher` |
| `docker/notifier.Dockerfile` | `distant-signal/notifier` |
| `docker/poller-incidents.Dockerfile` | `distant-signal/poller-incidents` |
| `docker/poller-stations.Dockerfile` | `distant-signal/poller-stations` |
| `docker/poller-tocs.Dockerfile` | `distant-signal/poller-tocs` |
| `docker/poller-ldbws.Dockerfile` | `distant-signal/poller-ldbws` |
| `docker/trust-consumer.Dockerfile` | `distant-signal/trust-consumer` |
| `docker/poller-tfl.Dockerfile` | `distant-signal/poller-tfl` |
| `docker/schedule-ingest.Dockerfile` | `distant-signal/schedule-ingest` |
| `docker/schedule-reference.Dockerfile` | `distant-signal/schedule-reference` |
| `frontend/Dockerfile` (target `runtime-prod`) | `distant-signal/frontend` |

```bash
REG=registry.example.com/distant-signal
TAG=0.1.0
docker build -f docker/api.Dockerfile                 -t $REG/api:$TAG .
docker build -f docker/aggregator.Dockerfile          -t $REG/aggregator:$TAG .
docker build -f docker/enricher.Dockerfile            -t $REG/enricher:$TAG .
docker build -f docker/notifier.Dockerfile            -t $REG/notifier:$TAG .
docker build -f docker/poller-incidents.Dockerfile    -t $REG/poller-incidents:$TAG .
docker build -f docker/poller-stations.Dockerfile     -t $REG/poller-stations:$TAG .
docker build -f docker/poller-tocs.Dockerfile         -t $REG/poller-tocs:$TAG .
docker build -f docker/poller-ldbws.Dockerfile        -t $REG/poller-ldbws:$TAG .
docker build -f docker/trust-consumer.Dockerfile      -t $REG/trust-consumer:$TAG .
docker build -f docker/poller-tfl.Dockerfile          -t $REG/poller-tfl:$TAG .
docker build -f docker/schedule-ingest.Dockerfile     -t $REG/schedule-ingest:$TAG .
docker build -f docker/schedule-reference.Dockerfile  -t $REG/schedule-reference:$TAG .
docker build -f frontend/Dockerfile --target runtime-prod -t $REG/frontend:$TAG .
for i in api aggregator enricher notifier poller-incidents poller-stations poller-tocs poller-ldbws trust-consumer poller-tfl schedule-ingest schedule-reference frontend; do
  docker push $REG/$i:$TAG
done
```

Redis is **not** in this table: the bundled Redis uses the upstream `redis`
image (`redis.image.*`), which this repository does not build.

Then point each `*.image.repository` value at `$REG/...`. An empty
`image.tag` falls back to the chart's `appVersion`.

## Install

```bash
helm install distant-signal ./charts/distant-signal -n distant-signal --create-namespace \
  --set enricher.llm.baseUrl=https://llm.example.com/v1 \
  --set enricher.llm.model=your-model-name \
  --set api.sso.issuerUrl=https://sso.example.com/realms/rail \
  --set api.sso.clientId=distant-signal \
  --set api.sso.clientSecret=your-oidc-client-secret \
  --set api.sso.redirectUrl=https://status.example.com/api/auth/callback \
  --set api.sso.postLoginRedirectUrl=https://status.example.com/
```

An install brings up **postgres + redis + api + aggregator + enricher +
frontend**, with **all five pollers off**. See "Enabling the pollers" below
for why.

`enricher.llm.baseUrl`, `enricher.llm.model` and the five `api.sso.*`
values above are the chart's **required** values; everything else has a
working default. Leaving any of them empty **aborts the render** with an
explicit message rather than deploying a pod that cannot work:

- The enricher has no `enabled` toggle, and `baseUrl`/`model` become plain
  (non-optional) env vars on its binary, so an empty value would deploy a
  pod that fails every extraction request forever with nothing but log
  noise to show for it.
- The api's five `SSO_*` env vars are declared with no defaults in
  `crates/api/src/data/config.rs`, so an api container missing any of them
  exits immediately with "the following required arguments were not
  provided" and `CrashLoopBackOff`s. See "Single sign-on (OIDC)" below.

The enricher is a strictly additive signal: it only ever *demotes* a
severity a line already has, and never suppresses one. A missing, failed or
low-confidence extraction is a no-op, so a broken LLM endpoint degrades the
enricher's own output and nothing else — the status pages keep working.

With the worked example values:

```bash
helm install distant-signal ./charts/distant-signal -n distant-signal --create-namespace \
  -f charts/distant-signal/values-example.yaml
```

## Upgrade

```bash
helm upgrade distant-signal ./charts/distant-signal -n distant-signal
```

Read the next section before upgrading if you rely on generated secrets.

`api` and `aggregator` roll concurrently with no ordering guarantee between
them. When a release adds a database migration that `aggregator` depends on
(as `20260822120000_line_status_source.sql` did, for the `line_status.source`
column `aggregator`'s TfL write path requires), a new `aggregator` pod can
start before `api` has finished running its in-process migrations, and will
log write errors until `api` becomes ready and applies them. This is
self-healing — `aggregator` retries on its normal poll cycle, so no data is
lost — but expect a brief window of `aggregator` error logs during such an
upgrade; it is not a sign of a failed rollout.

## Renaming an existing release

Helm has no in-place chart-rename operation for a release. This chart's own
`templates/_helpers.tpl` derives every object name from `.Release.Name` /
`.Chart.Name`, so a plain `helm upgrade` of an existing release against this
renamed chart directory does **not** rename the existing objects — it
produces a brand-new set of derived names (a StatefulSet with a new name, and
a new, empty `volumeClaimTemplates` PVC alongside the old one) rather than
renaming what is already running.

If you have an existing release installed from this chart's previous
location (before it was renamed to `charts/distant-signal`) and want to move
it onto the new path under a new release name without losing data, this
chart's own `postgresql.persistence.existingClaim` value already supports
the low-risk path below:

```bash
# 1. Capture the current config.
helm get values <old-release> -n <ns> -o yaml > values.yaml

# 2. Note the existing Postgres PVC's actual name.
kubectl get pvc -n <ns>

# 2b. Capture the generated Postgres password BEFORE uninstalling — `helm
#     uninstall` deletes the Secret that holds it, and `helm get values`
#     (step 1) does not capture a render-time-generated value (see
#     "Generated secrets and the `lookup` limitation" below for why it's
#     generated at all).
kubectl get secret -n <ns> <old-release> \
  -o jsonpath='{.data.postgres-password}' | base64 -d; echo

# 3. Remove the old release. StatefulSet-owned PVCs are NOT deleted by
#    `helm uninstall`, so the data survives this step.
helm uninstall <old-release> -n <ns>

# 4. Install under the new chart/release name, binding the new StatefulSet
#    to the pre-existing PVC instead of provisioning an empty one, and
#    pinning the Postgres password to the value captured in step 2b — a
#    fresh `helm install` has no live Secret to `lookup` and would otherwise
#    generate a brand-new random password that can never authenticate
#    against the reused volume's already-`initdb`'d data directory.
helm install <new-release-name> ./charts/distant-signal -n <ns> \
  -f values.yaml \
  --set postgresql.persistence.existingClaim=<the PVC name from step 2> \
  --set postgresql.auth.password=<the value captured in step 2b>
```

**If the old release predates this chart's rename**, one more mismatch
applies: this rename changed `postgresql.auth.username` and
`postgresql.auth.database`'s defaults from `nr_status` to `distant_signal`.
An old release that never set these explicitly gets the *new* defaults on
the fresh install above, but the reused PVC's data directory still has the
*old* role and database (`nr_status`) created inside it — so the rendered
`DATABASE_URL` would point at a role/database that doesn't exist in the
reused volume. Either pin the install to the reused volume's actual,
pre-existing names:

```bash
  --set postgresql.auth.username=nr_status \
  --set postgresql.auth.database=nr_status
```

or, before reinstalling under the new defaults, rename them in place inside
the reused database (e.g. `ALTER ROLE nr_status RENAME TO distant_signal;`
and `ALTER DATABASE nr_status RENAME TO distant_signal;`).

**This path is untested against a real cluster** — no live install of this
chart exists to verify it against as of this writing. Take a backup or
snapshot of the database before attempting it regardless of how confident the
steps above look — this is doubly true given the password- and
default-name-migration steps above.

## Generated secrets and the `lookup` limitation

`postgres-password` is generated with `randAlphaNum 32` when its value is
left empty and no `existingSecret` is given. It is the only chart-generated
secret today — every internal caller's own OAuth2 credential (see
"Internal-service OAuth2" below) is assigned by Authentik, an external
system, so a randomly generated value would just be rejected by it; those
follow the same never-auto-generated posture as each poller's own RDM
`apiKey`.

`postgres-password` is **preserved across `helm upgrade`**:
`templates/secret.yaml` reads the live Secret back out of the cluster with
Helm's `lookup` function and reuses whatever is already there, rather than
generating a fresh password and rotating it out from under the running
database's PVC.

**Limitation:** `lookup` returns nothing during `helm template` and
`--dry-run`, so an offline render shows a **different** generated value
every time you run it. That is cosmetic for dry runs, but it does mean
**`helm template | kubectl apply` is not a supported install path when you
rely on the generated postgres password** — the applied password would
differ from the one already in the cluster. For that workflow, set an
explicit value (`postgresql.auth.password`) or use `existingSecret`.

Read the generated value back out:

```bash
kubectl get secret -n distant-signal distant-signal \
  -o jsonpath='{.data.postgres-password}' | base64 -d; echo
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
    existingSecret: distant-signal-db
    existingSecretPasswordKey: password

pollers:
  incidents:
    enabled: true
    baseUrl: https://rdm.example.com/incidents
    existingSecret: distant-signal-rdm
    existingSecretApiKeyKey: incidents-api-key
    # A single existingSecret toggle covers both this poller's RDM apiKey
    # AND its own internal-oauth username/password (all three keys must
    # live in the same referenced Secret).
    existingSecretInternalOauthUsernameKey: incidents-oauth-username
    existingSecretInternalOauthPasswordKey: incidents-oauth-password
  ldbws:
    enabled: true
    baseUrl: https://rdm.example.com/LDBWS/api/20220120
    existingSecret: distant-signal-rdm
    existingSecretApiKeyKey: ldbws-api-key

enricher:
  llm:
    baseUrl: https://llm.example.com/v1
    model: your-model-name
    existingSecret: distant-signal-llm
    existingSecretApiKeyKey: llm-api-key

api:
  sso:
    issuerUrl: https://sso.example.com/realms/rail
    clientId: distant-signal
    existingSecret: distant-signal-sso
    existingSecretClientSecretKey: client-secret
    redirectUrl: https://status.example.com/api/auth/callback
    postLoginRedirectUrl: https://status.example.com/
```

The Postgres StatefulSet and its api/aggregator/enricher consumers resolve
the password reference through the *same* template helper, so an
`existingSecret` override can never leave them disagreeing about which
Secret holds the password.

## Single sign-on (OIDC)

The api authenticates users against an external OIDC provider (Keycloak,
Authentik, Authelia, Entra ID, Okta, …) — this chart deploys no identity
provider of its own. Sign-in gates the **per-user** features only (pinning
lines and stations, creating and editing custom lines); the line-status
pages themselves stay readable without signing in.

All five `api.sso.*` values are **required** and the render aborts if any
is missing, because `crates/api` declares its `SSO_*` env vars with no
defaults — an api container without them exits before `main` runs.

Two details that are easy to get wrong:

- **`redirectUrl` is the frontend's origin, not the api's.** Register
  `https://<frontend-host>/api/auth/callback` with your provider and put
  that same string here. The callback issues the session cookie, and a
  cookie set on the api's origin would never be sent back by the browser,
  which talks to the frontend for everything else. The frontend's
  `/api/*` catch-all proxies the request through to the api's
  `/public/auth/callback` and forwards the `Set-Cookie` back unmodified.
- **The session cookie is `Secure`**, so sign-in only works over HTTPS.
  Terminate TLS at the ingress (see "Ingress" below) before expecting
  login to work.

`clientSecret` follows the chart's usual secret rule — supply it inline and
it is rendered into the chart Secret as `sso-client-secret`, or point
`api.sso.existingSecret` at a Secret you manage and the chart never sees
it. Unlike the postgres password it is **never auto-generated**: a random
value would simply be rejected by the issuer — same posture as every
internal-oauth username/password below.

## Local dev identity provider (devAuthentik)

For a local/dev Kubernetes cluster (kind, minikube, k3d) only — set
`devAuthentik.enabled: true` to bring up a throwaway local Authentik
instance and skip registering this app with a real external IdP entirely.
Mirrors `docker-compose.authentik.yml`'s job for the `docker compose`
deployment path. **Off by default; an install pointed at a real external
IdP is completely unaffected.**

When enabled and `api.sso.*` is left at its empty default, the chart
computes it from `devAuthentik.*` and the fixed, blueprint-provisioned
dev-only OIDC client (`client_id: distant-signal-dev`) — no manual IdP-side
setup, matching `docker-compose.authentik.yml`'s own zero-click bootstrap.
An explicit `api.sso.*` value always wins.

```yaml
devAuthentik:
  enabled: true
```

**Two manual steps this chart cannot do for you:**

1. **Forward `devAuthentik.service.nodePort` (default `30900`) to your
   machine's loopback interface**, using whatever mechanism your cluster
   tool provides:
   - kind: an `extraPortMappings` entry in your cluster config, e.g.
     ```yaml
     nodes:
       - role: control-plane
         extraPortMappings:
           - containerPort: 30900
             hostPort: 30900
     ```
   - minikube: `minikube tunnel`
   - k3d: `k3d cluster create --port 30900:30900@loadbalancer`

   This step lives in your cluster-creation config, not in this chart —
   see the design doc's Research for why a Helm chart cannot bind a port
   on the host machine itself.

2. **On your very first `helm install` with `devAuthentik.enabled: true`**
   (unless you also set `devAuthentik.hostAliasIP` explicitly), run `helm
   upgrade` once immediately afterward. `NOTES.txt` reminds you of this
   after every install/upgrade where `devAuthentik.enabled` is `true`.

**Known limitations / unverified, stated plainly rather than assumed
solved:**

- The `hostAliases`-vs-`lookup` first-install ordering gap above has no
  fully graceful degradation — it requires the one-time `helm upgrade`
  workaround, not a design this chart claims to have fully closed.
- The NodePort-forwarding step is entirely outside this chart's control and
  cannot be verified from inside a `helm template`/`helm install` run — if
  you skip it, the chart installs cleanly and the failure only shows up as
  "the browser can't reach Authentik," with no render-time signal.
- None of the above has been smoke-tested by this chart's own authors
  against a real kind/minikube/k3d cluster as of this writing — see
  `docs/superpowers/plans/2026-08-29-dev-oidc-server.md`'s Task 13 for
  what was and wasn't actually verified.

See `docs/superpowers/specs/2026-08-29-dev-oidc-server-design.md` for the
full design, including why `AUTHENTIK_BOOTSTRAP_*` is deliberately never
set (no default admin — see its Bootstrap section) and why this is a
hand-rolled deployment rather than the official `goauthentik/helm` chart
(see its Non-goals).

## Using an external database

Set `postgresql.enabled: false` and configure `externalDatabase`. Provide
**either** an `existingSecret` holding the whole connection URL (preferred —
it keeps the password out of `helm get values` as well as out of the
Deployment spec):

```yaml
postgresql:
  enabled: false
externalDatabase:
  existingSecret: distant-signal-db
  existingSecretUrlKey: database-url
```

**or** a literal URL:

```yaml
postgresql:
  enabled: false
externalDatabase:
  url: postgres://distant_signal:s3cret@db.example.com:5432/distant_signal
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
> to the internet as well. Those ingestion endpoints are protected by
> internal-service OAuth2 (`require_internal_oauth`, `crates/api/src/auth.rs`,
> delegated to Authentik) — there is no other authentication in front of
> them. If you do not need external API access, leave
> `ingress.api.enabled: false`; the frontend reaches the api over the
> in-cluster Service either way.
>
> It publishes the api's `/metrics` endpoint the same way when
> `metrics.enabled` is true (the default), because api serves `/metrics` on
> its own HTTP port rather than on a separate `metrics.port` — unlike the
> other seven binaries, whose metrics ports are never behind an Ingress.
> That endpoint is read-only request-count/latency telemetry with no
> secrets in it, and `metrics.enabled: false` removes the route entirely.

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
- **aggregator**, **enricher** and every poller: default-deny apart from
  `metrics.port` from the namespace named by
  `networkPolicy.monitoringNamespace`, and only when `metrics.enabled` is
  true. With `metrics.enabled: false` they are pure default-deny — those
  three expose no other listener. The api policy gains that same
  monitoring-namespace allow, needing no extra port since api serves
  `/metrics` on its existing one.

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
helm upgrade distant-signal ./charts/distant-signal -n distant-signal \
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

`secrets` is currently empty — reserved for any future chart-wide secret
that isn't tied to one specific service. The shared internal-token header
this block used to hold is retired; see "internalOauth (shared,
non-secret)" below for what replaced it.

### internalOauth (shared, non-secret)

| Key | Default | Description |
|---|---|---|
| `internalOauth.tokenUrl` | `""` | Authentik's client-credentials token endpoint. Required whenever any real caller is enabled. |
| `internalOauth.clientId` | `""` | The shared OAuth2 Provider's client_id — same value as `api.internalOauth.clientId`. Required. |
| `internalOauth.scope` | `groups` | Scope requested on every client-credentials POST. |

### postgresql

There is intentionally no `replicaCount`: this is a single-replica
StatefulSet with no replication, backup or restore story.

| Key | Default | Description |
|---|---|---|
| `postgresql.enabled` | `true` | Deploy the bundled PostgreSQL StatefulSet. |
| `postgresql.auth.username` | `distant_signal` | Database role the api and aggregator connect as. |
| `postgresql.auth.database` | `distant_signal` | Database name. |
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
| `externalDatabase.url` | `""` | Full connection URL, e.g. `postgres://user:pass@host:5432/distant_signal`. |
| `externalDatabase.existingSecret` | `""` | Pre-existing Secret holding the whole connection URL (preferred). |
| `externalDatabase.existingSecretUrlKey` | `database-url` | Key within `externalDatabase.existingSecret`. |

### api

| Key | Default | Description |
|---|---|---|
| `api.image.repository` | `distant-signal/api` | api image repository. |
| `api.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `api.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `api.replicaCount` | `1` | Replicas. >1 is safe — sqlx's Migrator takes a Postgres advisory lock. |
| `api.service.type` | `ClusterIP` | Service type. |
| `api.service.port` | `8080` | Service and container port; also sets `BIND_URL`. |
| `api.logLevel` | `info` | `RUST_LOG` value (tracing-subscriber EnvFilter syntax). |
| `api.sso.issuerUrl` | `""` | **Required.** OIDC issuer base URL; everything else is discovered from its `.well-known/openid-configuration`. |
| `api.sso.clientId` | `""` | **Required.** OIDC client id this deployment is registered as. |
| `api.sso.clientSecret` | `""` | **Required** unless `api.sso.existingSecret` is set. Rendered into the chart Secret as `sso-client-secret`. Never auto-generated. |
| `api.sso.existingSecret` | `""` | Read the client secret from this pre-existing Secret instead. |
| `api.sso.existingSecretClientSecretKey` | `sso-client-secret` | Key within `api.sso.existingSecret`. |
| `api.sso.redirectUrl` | `""` | **Required.** Callback URI registered with the SSO server — the *frontend's* origin plus `/api/auth/callback`, not the api's. |
| `api.sso.postLoginRedirectUrl` | `""` | **Required.** Where sign-in and sign-out send the browser afterwards — the frontend's root URL. |
| `api.sessionTtlDays` | `14` | Session lifetime in days. A fixed expiry stamped at sign-in, not a sliding window. |
| `api.internalOauth.issuerUrl` | `""` | **Required.** OIDC issuer base URL for the internal-service OAuth2 provider (may be the same Authentik instance as `api.sso.*`, a different Application/Provider). |
| `api.internalOauth.clientId` | `""` | **Required.** Expected `aud` claim on a verified token — same value as the top-level `internalOauth.clientId`. |
| `api.internalOauth.groups.incidents` | `svc-poller-incidents` | Required Authentik group for the incidents poller. Not secret. |
| `api.internalOauth.groups.stations` | `svc-poller-stations` | Required Authentik group for the stations poller. Not secret. |
| `api.internalOauth.groups.tocs` | `svc-poller-tocs` | Required Authentik group for the TOCs poller. Not secret. |
| `api.internalOauth.groups.ldbws` | `svc-poller-ldbws` | Required Authentik group for the LDBWS poller. Not secret. |
| `api.internalOauth.groups.tfl` | `svc-poller-tfl` | Required Authentik group for the TfL poller. Not secret. |
| `api.internalOauth.groups.trustConsumer` | `svc-trust-consumer` | Required Authentik group for trust-consumer (also accepted on `GET /private/stanox-crs`). Not secret. |
| `api.internalOauth.groups.scheduleIngest` | `svc-schedule-ingest` | Required Authentik group for schedule-ingest. Not secret. |
| `api.internalOauth.groups.scheduleReference` | `svc-schedule-reference` | Required Authentik group for schedule-reference (also accepted on `POST /private/stanox-crs`). Not secret. |
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

### devAuthentik

See "Local dev identity provider (devAuthentik)" above. Always off by
default; nothing here is rendered unless `devAuthentik.enabled` is `true`.

| Key | Default | Description |
|---|---|---|
| `devAuthentik.enabled` | `false` | Deploy a throwaway local Authentik instance for exercising this app's own login flow. An install pointing `api.sso.*` at a real external IdP is unaffected either way. |
| `devAuthentik.hostname` | `authentik.localhost` | The one hostname both the developer's browser and the api Pod must resolve identically. Resolves to loopback with no `/etc/hosts` entry needed in modern browsers (RFC 6761). |
| `devAuthentik.image.repository` | `ghcr.io/goauthentik/server` | Authentik server image repository. |
| `devAuthentik.image.tag` | `2026.8.0` | Pinned; Authentik's ~3-month release cadence and 2-version support window mean this needs periodic bumping, not automated by this chart. |
| `devAuthentik.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `devAuthentik.secretKey` | `""` | `AUTHENTIK_SECRET_KEY`. Chart-generated (lookup-then-`randAlphaNum`) when empty, same pattern as `postgres-password`, so it survives `helm upgrade`. No `existingSecret` override — throwaway dev IdP only. |
| `devAuthentik.service.port` | `30900` | ClusterIP-facing port. Must equal `service.nodePort` — the render aborts if they differ. |
| `devAuthentik.service.nodePort` | `30900` | NodePort. Must equal `service.port`; default sits inside Kubernetes' default 30000-32767 NodePort range. |
| `devAuthentik.hostAliasIP` | `""` | Explicit override for the IP the api Deployment's `hostAliases` entry points `devAuthentik.hostname` at. Empty uses `lookup` against the live Service's ClusterIP at render time — unresolvable on a from-scratch `helm install` (see the "Two manual steps" note above and NOTES.txt). |
| `devAuthentik.postgresql.image` | `postgres:16-alpine` | Image for Authentik's own dedicated Postgres — independent of, and not a second database on, this chart's bundled `postgresql`. |
| `devAuthentik.postgresql.persistence.enabled` | `true` | Attach a PVC for Authentik's Postgres. |
| `devAuthentik.postgresql.persistence.size` | `1Gi` | Requested volume size. |
| `devAuthentik.postgresql.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `devAuthentik.postgresql.resources` | `{}` | Authentik Postgres container resource requests/limits. |
| `devAuthentik.resources` | `{}` | Authentik server container resource requests/limits. |
| `devAuthentik.nodeSelector` | `{}` | Pod node selector. |
| `devAuthentik.tolerations` | `[]` | Pod tolerations. |
| `devAuthentik.affinity` | `{}` | Pod affinity rules. |

### aggregator

There is intentionally no `replicaCount`: the aggregator is a singleton
write loop, pinned to `replicas: 1` with `strategy: Recreate`.

| Key | Default | Description |
|---|---|---|
| `aggregator.image.repository` | `distant-signal/aggregator` | aggregator image repository. |
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
| `redis.podSecurityContext` | `{runAsUser: 999, runAsGroup: 999}` | Merged over the chart-wide pod securityContext defaults. Pinned (unlike most other `podSecurityContext` defaults in this chart) because the upstream `redis` image runs as root with no `USER` set at all -- confirmed against `redis:7`'s real image config; 999 is the `redis` user's actual uid/gid per docker-library/redis's own Dockerfile. Override if you point `redis.image` at a different image/tag whose non-root uid differs. |

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
| `enricher.image.repository` | `distant-signal/enricher` | enricher image repository. |
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
| `frontend.image.repository` | `distant-signal/frontend` | frontend image repository. |
| `frontend.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `frontend.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `frontend.replicaCount` | `1` | Safe to raise, with one documented caveat. frontend/lib/liveDataCache.ts keeps a process-local stale-data cache so a backend outage shows the last-known line status instead of an error page (docs/superpowers/specs/2026-09-02-frontend-disconnect-reconnect-ux-design.md). That cache is per-pod: with more than one replica, during an outage one visitor may get stale-but-useful content from a warm pod while another gets the auto-retrying error page from a cold one. Each pod stays internally consistent and no stale data crosses users (entries are session-scoped), so this is a degraded-experience caveat, not a correctness one -- deliberately documented rather than blocked, unlike postgresql.replicaCount above. |
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
| `pollers.<name>.image.repository` | `distant-signal/poller-<name>` | Poller image repository. |
| `pollers.<name>.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollers.<name>.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollers.<name>.baseUrl` | `""` | Upstream feed base URL. Required when enabled; empty aborts the render. |
| `pollers.<name>.baseUrlEnvVar` | per-poller | Env var the binary reads the base URL from. Do not change. |
| `pollers.<name>.ingestPath` | per-poller | Path on the api Service this poller POSTs results to. |
| `pollers.<name>.pollIntervalSecs` | 300 / 86400 / 86400 / 60 | Poll cadence. |
| `pollers.<name>.apiKey` | `""` | RDM API key. Rendered into the chart Secret when `existingSecret` is empty. |
| `pollers.<name>.existingSecret` | `""` | Read the API key AND the internal-oauth username/password below from this pre-existing Secret instead. |
| `pollers.<name>.existingSecretApiKeyKey` | `rdm-<name>-api-key` | Key within `pollers.<name>.existingSecret`. |
| `pollers.<name>.internalOauthUsername` | `""` | This poller's own Authentik service-account username. Never auto-generated. |
| `pollers.<name>.internalOauthPassword` | `""` | This poller's own Authentik app-password. Never auto-generated. |
| `pollers.<name>.existingSecretInternalOauthUsernameKey` | `internal-oauth-username-poller-<name>` | Key within `pollers.<name>.existingSecret`. |
| `pollers.<name>.existingSecretInternalOauthPasswordKey` | `internal-oauth-password-poller-<name>` | Key within `pollers.<name>.existingSecret`. |
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

### metrics

On by default: the exporters are in-process and add no runtime dependency
once the images are built. `metrics.enabled: false` is a real off switch,
not merely an un-scraped one — it renders `METRICS_ENABLED=false` into every
workload, and each binary then never starts its `/metrics` listener at all
(api keeps its own HTTP listener, but drops the `/metrics` route and its
request-metrics middleware). Note that api serves `/metrics` on
`api.service.port`, not on `metrics.port`, because it already has a
listener; only the aggregator, the enricher and the pollers use
`metrics.port`.

| Key | Default | Description |
|---|---|---|
| `metrics.enabled` | `true` | Expose Prometheus `/metrics` on every workload, and render the metrics port, env, `prometheus.io/*` scrape annotations and NetworkPolicy allows. |
| `metrics.port` | `9091` | Port the aggregator, enricher and each poller serve `/metrics` on. Not used by api, which serves it on `api.service.port`. |
| `metrics.podMonitor.enabled` | `false` | Render a Prometheus Operator `PodMonitor`. Off by default — the CRD is absent on clusters without the operator, and installing it would fail the release outright. |
| `metrics.podMonitor.interval` | `30s` | Scrape interval on the `PodMonitor`. |
| `metrics.podMonitor.scrapeTimeout` | `10s` | Scrape timeout on the `PodMonitor`. Must stay below `interval`. |

### networkPolicy

| Key | Default | Description |
|---|---|---|
| `networkPolicy.enabled` | `false` | Render default-deny NetworkPolicies with explicit allows. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | Namespace the ingress controller runs in, matched by `kubernetes.io/metadata.name`. |
| `networkPolicy.monitoringNamespace` | `monitoring` | Namespace Prometheus runs in, matched by `kubernetes.io/metadata.name`. Allowed to reach each workload's metrics port. Only used when `metrics.enabled` is true. |

### tests

| Key | Default | Description |
|---|---|---|
| `tests.enabled` | `true` | Render the `helm test` hook Pod. |
| `tests.image.repository` | `""` | Empty reuses the api image, which already ships `curl`. |
| `tests.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `tests.image.pullPolicy` | `IfNotPresent` | Image pull policy. |

## Testing

```bash
helm test distant-signal -n distant-signal
```

The hook Pod runs `curl -fsS --max-time 10 http://<release>-api:8080/public/health`
against the in-cluster api Service; `-f` makes curl exit non-zero on any HTTP
error status, which is what `helm test` reads as failure. Running it requires
a **live cluster with the images available** — it is an operator step, not
part of chart authoring.

## Uninstall

```bash
helm uninstall distant-signal -n distant-signal
```

> **The PVC created by `volumeClaimTemplates` survives uninstall.** Helm does
> not delete StatefulSet volume claims, which is deliberate — it is what
> stops an accidental `helm uninstall` from destroying the database. If you
> do not want the data, delete it manually:
>
> ```bash
> kubectl delete pvc -n distant-signal -l app.kubernetes.io/instance=distant-signal
> ```

## Not in scope

- **No image build or publish pipeline in this chart itself.** The repo-level
  `.github/workflows/containers.yml` covers that (see "Building and pushing
  the images (manual)" above for the fallback path and the full
  Dockerfile-to-repository mapping it uses).
- **No HorizontalPodAutoscaler.** The aggregator, the enricher and all four
  pollers are singleton loops that must not be scaled, and the api is
  database-bound.
- **No persistence, backup or HA for the bundled Redis.** It is a disposable
  trigger queue; see "Using an external Redis" above.
- **No backup, restore or replication** for the bundled Postgres. It is a
  single-replica StatefulSet on a PVC. Set `postgresql.enabled: false` and
  use a managed database if you need HA.
- **No ServiceMonitor, no bundled Prometheus and no dashboards.** Every
  service does expose a Prometheus `/metrics` endpoint (see the `metrics`
  values below), and the chart can render a `PodMonitor` for Prometheus
  Operator, but it never installs Prometheus itself, ships no Grafana
  dashboards and no alerting rules, and offers no `ServiceMonitor`
  alternative — `PodMonitor` alone, because the pollers, the aggregator and
  the enricher have no Service in front of them at all.
