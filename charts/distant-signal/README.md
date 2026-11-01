# distant-signal Helm chart

Deploys the whole National Rail status stack into a single namespace: a
bundled single-replica **PostgreSQL** StatefulSet, a bundled single-replica
**Redis** (persistent by default — see "Using an external Redis" below),
the **api**, the **aggregator**, the **enricher**, the **notifier**, the
**frontend**, the three movement-stream consumers (**trust-consumer**,
**full-coverage-consumer**, **trust-backlog-consumer**) and
**movement-relay** (`movementRelay`), the one Kafka client for RDM's Train
Movements feed, which fills the `movement-events` Redis stream the three
consumers read. It also has these optional, off-by-default workloads:

- five **pollers** under `pollers.*` — four Rail Data Marketplace pollers
  (incidents, stations, tocs, ldbws) plus a TfL Unified API poller (tfl);
- three island-of-Ireland pollers (`pollerIrishRailGtfs`,
  `pollerIrishRailLive`, `pollerNirStations`);
- **schedulefeed** (`scheduleFeed`): an SFTP server for the pushed CIF
  timetable delivery (its own `schedulefeed-sftp` pod by default), and a
  pod running `schedule-ingest` and `schedule-reference`, sharing one PVC.

The chart has no subchart
dependencies and no `dependencies:` block, so `helm dependency update` is
never needed and it installs in an air-gapped cluster given the images. It
mirrors the topology, environment contract and cadences that the
repository's `docker-compose.yml` and `local.env.example`/`dev.env.example`
already establish, so the two deployment paths do not drift.

This chart does **not** deploy the derived MCP service ("distant-signal-mcp",
a fork of train-mcp) — see the `railMcp` section under "Values reference"
below for how to point the frontend's in-app chat at a separately-deployed
instance of it.

## Prerequisites

- **Kubernetes >= 1.23.** The chart declares `kubeVersion: ">=1.23.0-0"`.
  The NetworkPolicy templates select the ingress-controller namespace via
  the automatic `kubernetes.io/metadata.name` label, GA from 1.22.
- **Helm 3.8+ or 4.x.** Developed and verified against Helm v4.1.4.
- **A default StorageClass**, or set `postgresql.persistence.storageClass`
  and `redis.persistence.storageClass` explicitly — the bundled Postgres
  uses a `volumeClaimTemplates` entry and the bundled Redis a standalone
  PVC. Set `postgresql.persistence.enabled: false` or
  `redis.persistence.enabled: false` for throwaway testing (data is lost
  on reschedule), or `postgresql.enabled: false` / `redis.enabled: false`
  to use a managed database/Redis instead.
- **Images already present in a registry the cluster can pull from.** This
  chart builds nothing.

## Building and pushing the images (manual)

`.github/workflows/containers.yml` builds and publishes every image below to
`ghcr.io/fasterspeeding/distant-signal/<service>` automatically (build-only
sanity check on PRs; build + push + cosign-sign on push to main/master) — you
do not need to do this by hand for a normal install, and every `*.image.repository`
below already defaults to that exact path. The table and commands below are
for building and pushing to a registry of your own (e.g. a private registry,
or testing a local change) without going through that pipeline — either set
`global.imageRegistry` (see values.yaml) to swap every image's registry at
once, or point each `*.image.repository` you're replacing at your own
`$REG/...` directly.

The same workflow packages this chart and pushes it to
`oci://ghcr.io/fasterspeeding/charts/distant-signal`, cosign-signed keylessly
like the images (INF-12). Verify a pulled chart with:

```bash
cosign verify ghcr.io/fasterspeeding/charts/distant-signal@sha256:<digest> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/FasterSpeeding/Distant-Signal/.github/workflows/containers.yml@refs/heads/main
```

In Flux, set `verify.provider: cosign` with a `matchOIDCIdentity` entry for
that issuer and subject on the chart's OCIRepository/HelmRepository.

**Debian package updates.** Each image's runtime stage runs `apt-get upgrade`
in the same layer as its `apt-get install`. That upgrades the base image's
own packages (libc, openssl, ...) to whatever the Debian archive carries when
the layer is built. Ordinary builds reuse that layer from the build cache,
and so does a local rebuild, so they don't pick up new fixes. Only the
workflow's weekly forced rebuild (Mondays 02:41 UTC, or a manual run with
`no-cache`) rebuilds the runtime stage uncached and refreshes them; a
base-image digest bump (Renovate) does too. An image's digest, and with it
a rollout of its Deployment, therefore changes at least weekly. Images are
reproducible otherwise: an unchanged service keeps its digest from push to
push.

| Dockerfile | Default image repository |
|---|---|
| `docker/api.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/api` |
| `docker/aggregator.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/aggregator` |
| `docker/enricher.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/enricher` |
| `docker/notifier.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/notifier` |
| `docker/poller-incidents.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-incidents` |
| `docker/poller-stations.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-stations` |
| `docker/poller-tocs.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-tocs` |
| `docker/poller-ldbws.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-ldbws` |
| `docker/poller-tfl.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-tfl` |
| `docker/poller-irish-rail-gtfs.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-irish-rail-gtfs` |
| `docker/poller-irish-rail-live.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-irish-rail-live` |
| `docker/poller-nir-stations.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/poller-nir-stations` |
| `docker/trust-consumer.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/trust-consumer` |
| `docker/full-coverage-consumer.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/full-coverage-consumer` |
| `docker/trust-backlog-consumer.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/trust-backlog-consumer` |
| `docker/movement-relay.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/movement-relay` |
| `docker/schedule-ingest.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/schedule-ingest` |
| `docker/schedule-reference.Dockerfile` | `ghcr.io/fasterspeeding/distant-signal/schedule-reference` |
| `frontend/Dockerfile` (target `runtime-prod`) | `ghcr.io/fasterspeeding/distant-signal/frontend` |

```bash
REG=registry.example.com/distant-signal
TAG=0.1.0
docker build -f docker/api.Dockerfile                     -t $REG/api:$TAG .
docker build -f docker/aggregator.Dockerfile              -t $REG/aggregator:$TAG .
docker build -f docker/enricher.Dockerfile                -t $REG/enricher:$TAG .
docker build -f docker/notifier.Dockerfile                -t $REG/notifier:$TAG .
docker build -f docker/poller-incidents.Dockerfile        -t $REG/poller-incidents:$TAG .
docker build -f docker/poller-stations.Dockerfile         -t $REG/poller-stations:$TAG .
docker build -f docker/poller-tocs.Dockerfile             -t $REG/poller-tocs:$TAG .
docker build -f docker/poller-ldbws.Dockerfile            -t $REG/poller-ldbws:$TAG .
docker build -f docker/poller-tfl.Dockerfile              -t $REG/poller-tfl:$TAG .
docker build -f docker/poller-irish-rail-gtfs.Dockerfile  -t $REG/poller-irish-rail-gtfs:$TAG .
docker build -f docker/poller-irish-rail-live.Dockerfile  -t $REG/poller-irish-rail-live:$TAG .
docker build -f docker/poller-nir-stations.Dockerfile     -t $REG/poller-nir-stations:$TAG .
docker build -f docker/trust-consumer.Dockerfile          -t $REG/trust-consumer:$TAG .
docker build -f docker/full-coverage-consumer.Dockerfile  -t $REG/full-coverage-consumer:$TAG .
docker build -f docker/trust-backlog-consumer.Dockerfile  -t $REG/trust-backlog-consumer:$TAG .
docker build -f docker/movement-relay.Dockerfile          -t $REG/movement-relay:$TAG .
docker build -f docker/schedule-ingest.Dockerfile         -t $REG/schedule-ingest:$TAG .
docker build -f docker/schedule-reference.Dockerfile      -t $REG/schedule-reference:$TAG .
docker build -f frontend/Dockerfile --target runtime-prod -t $REG/frontend:$TAG .
for i in api aggregator enricher notifier poller-incidents poller-stations poller-tocs poller-ldbws poller-tfl poller-irish-rail-gtfs poller-irish-rail-live poller-nir-stations trust-consumer full-coverage-consumer trust-backlog-consumer movement-relay schedule-ingest schedule-reference frontend; do
  docker push $REG/$i:$TAG
done
```

Redis is **not** in this table: the bundled Redis is the upstream Valkey
image (`redis.image.*`, `valkey/valkey`), which this repository does not build.

Then point each `*.image.repository` value at `$REG/...`. An empty
`image.tag` falls back to the chart's `appVersion`.

**Pinning by content digest instead of tag.** Every first-party service
above also has an `*.image.digest` value (e.g. `api.image.digest`), empty
by default. When set to a real `sha256:<64 hex chars>` digest, it takes
priority over `tag`/`appVersion` entirely: the rendered image reference
becomes `<repository>@<digest>` with no tag at all, since a digest is the
only fully immutable reference (a tag, even an otherwise-immutable-looking
`sha-<short-sha>` one, can in principle be re-pushed to point at different
content; `repo@sha256:...` cannot). `.github/workflows/containers.yml`'s
`push-helm-chart` job populates this automatically for every first-party
image in the packaged chart's own default `values.yaml`, from that same
run's real, already-pushed image digests -- so a plain `helm install`
against a chart pulled from `oci://ghcr.io/fasterspeeding/charts` already
pins every first-party image by digest with no operator action needed. Set
it by hand (`--set api.image.digest=sha256:...`) only if you're building
and pushing your own images per the table above and want the same
guarantee for them. `postgresql`, `redis`, `devAuthentik` and
`scheduleFeed.sftp` (all externally-sourced, not built by this repo) have
no `digest` field. They are digest-pinned through the tag instead: their
default `tag` values carry the digest (e.g. `8.1.10-trixie@sha256:...`), which
`distant-signal.image` appends after `:`, giving `<repository>:<tag>@<digest>`.
Override one the same way, with a `<tag>@sha256:...` string.

## Install

```bash
helm install distant-signal ./charts/distant-signal -n distant-signal --create-namespace \
  --set enricher.llm.baseUrl=https://llm.example.com/v1 \
  --set enricher.llm.model=your-model-name \
  --set api.sso.issuerUrl=https://sso.example.com/realms/rail \
  --set api.sso.clientId=distant-signal \
  --set api.sso.clientSecret=your-oidc-client-secret \
  --set api.sso.redirectUrl=https://status.example.com/api/auth/callback \
  --set api.sso.postLoginRedirectUrl=https://status.example.com/ \
  --set trustConsumer.kafka.brokers=kafka.example.com:9092 \
  --set trustConsumer.kafka.topic=TRAIN_MVT_ALL_TOC \
  --set trustConsumer.kafka.consumerGroup=SC-your-rdm-group-id \
  --set trustConsumer.kafka.saslMechanism=PLAIN \
  --set trustConsumer.kafka.saslUsername=your-rdm-kafka-username \
  --set trustConsumer.kafka.saslPassword=your-rdm-kafka-password
```

An install brings up **postgres + redis + api + aggregator + enricher +
notifier + frontend + trust-consumer + full-coverage-consumer +
trust-backlog-consumer + movement-relay**, with **every poller and the
schedulefeed pod off**. See "Enabling the pollers" below for why the
pollers are off.

`enricher.llm.baseUrl`, `enricher.llm.model`, the five `api.sso.*` values
and the RDM Train Movements Kafka connection above are the chart's
**required** values; everything else has a working default. Leaving any of
them empty **aborts the render** with an explicit message rather than
deploying a pod that cannot work:

- The enricher has no `enabled` toggle, and `baseUrl`/`model` become plain
  (non-optional) env vars on its binary, so an empty value would deploy a
  pod that fails every extraction request forever with nothing but log
  noise to show for it.
- The api's five `SSO_*` env vars are declared with no defaults in
  `crates/api/src/data/config.rs`, so an api container missing any of them
  exits immediately with "the following required arguments were not
  provided" and `CrashLoopBackOff`s. See "Single sign-on (OIDC)" below.
- movement-relay is on by default because the three consumers read only
  the stream it writes. Each `movementRelay.kafka.*` value that is left
  empty falls back to the matching `trustConsumer.kafka.*` value (brokers,
  topic, consumer group, SASL mechanism), and without a credential of its
  own it reads trust-consumer's SASL credential, so the one
  `trustConsumer.kafka.*` block above configures it. The render fails if
  neither block has brokers, topic, consumer group and SASL mechanism. For
  an install that does not ingest TRUST train movements, set
  `movementRelay.enabled=false` instead; the three consumers then run on
  an empty stream. None of the consumers reads Kafka directly: their
  legacy Kafka backend was removed (Deploy C), and a leftover
  `trustConsumer.movementFeed` or `fullCoverageConsumer.movementFeed` of
  `kafka` fails the render.

The enricher is a strictly additive signal: its extractions only adjust
the severity an incident already gives a line (a high-confidence
`apparent_severity` can raise it; a resolved/residual status, a schedule
window that excludes now or an elapsed period can lower it), and never
suppress a status. A missing, failed or
low-confidence extraction is a no-op, so a broken LLM endpoint degrades the
enricher's own output and nothing else — the status pages keep working.
To pick `enricher.llm.model` and check that it fits your timeouts on your
hardware, see `docs/enricher-model-eval.md`.

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

> **Upgrade note: Redis 7.4 to Valkey 8.1 (DS-174).** `redis.image` is now
> `valkey/valkey:8.1.x` instead of `redis:7.4.11`. Every value, Secret, port,
> probe and client is unchanged, but a running Redis 7.4's AOF has an RDB 12
> preamble that Valkey cannot load. **Before upgrading an install with
> `redis.persistence.enabled`, rewrite its AOF without the preamble**
> (`CONFIG SET aof-use-rdb-preamble no`, then `BGREWRITEAOF`), or the new
> pod crash-loops on startup. The steps, checks and rollback (back to
> `redis:7.4.11`, which reads what Valkey 8.1 writes) are in
> `docs/valkey-migration.md`. Keep the previous image with
> `--set redis.image.repository=redis --set redis.image.tag=7.4.11@sha256:44960b3100c48159a66d28c18b0588c193492e25f7551d60e51dd464fc3eca5b`
> until you are ready.

> **Upgrade note: Redis PVC default 1Gi -> 4Gi (2026-09-27).** A PVC can only
> grow, and only on a StorageClass with `allowVolumeExpansion: true`; k3s
> `local-path` and other hostPath-style classes cannot expand. **Before
> upgrading an existing install whose Redis PVC was created at 1Gi on such a
> class, pin its current size (`--set redis.persistence.size=1Gi`, or in your
> values/overlay) or set `redis.persistence.existingClaim` to the PVC.**
> Otherwise the upgrade can fail when Kubernetes rejects the PVC change.
> During a real `helm install`/`helm upgrade` (the helm CLI or Flux) the
> chart also protects itself: it looks up the existing PVC and keeps its
> current size when its StorageClass cannot expand it, and NOTES.txt says
> so. That lookup sees nothing under `helm template`, `--dry-run` or Argo CD,
> which is why the pin is still required there. On an expandable class the
> PVC is resized to 4Gi in place.

> **Upgrade note: movement-relay on by default (2026-09-28).**
> `movementRelay.enabled` now defaults to `true`, taking any empty
> `movementRelay.kafka.*` value from `trustConsumer.kafka.*`. An install
> that already sets `movementRelay.enabled` explicitly is unaffected. One
> that left it at the old `false` default gains a movement-relay pod, and
> its render fails unless `trustConsumer.kafka.*` (or
> `movementRelay.kafka.*`) holds the Kafka connection; set
> `movementRelay.enabled=false` to keep the old behaviour.

> **Upgrade note: the consumers' Kafka backend is gone (Deploy C,
> 2026-10-01).** `trustConsumer.movementFeed`,
> `fullCoverageConsumer.movementFeed` and
> `fullCoverageConsumer.kafka.consumerGroup` were removed: trust-consumer
> and full-coverage-consumer only read movement-relay's `movement-events`
> stream and no longer receive any `KAFKA_*` env var. A leftover
> `movementFeed: redis-stream` is ignored, `movementFeed: kafka` fails the
> render, and the binaries refuse `MOVEMENT_FEED_BACKEND=kafka` at
> startup. `trustConsumer.kafka.*` is unchanged and is still the
> connection movement-relay falls back to.

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
  what was and wasn't actually verified (that plan was pruned from the tree
  in commit `b7f71a8a`; read it from git history).

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

Redis backs two independent Redis Streams here, with two very different
durability profiles:

- `incident-text-changed`: poller-incidents publishes an event when an
  incident's text changes, and the enricher consumes it to re-extract promptly. Losing
  this one costs nothing but promptness — the enricher's hourly sweep
  re-finds anything a dropped event would have triggered.
- `movement-events`: the **sole transport** between movement-relay (this
  chart's one real Kafka client) and its three downstream consumer groups,
  trust-consumer, full-coverage-consumer and trust-backlog-consumer. There
  is no replay source
  behind it — losing this one loses data outright, not just promptness.

Because of the second stream, the bundled Redis runs with persistence
**on by default**: a PVC (`redis.persistence.*`, disable with
`redis.persistence.enabled: false`) plus `--appendonly yes`. This was not
always the case — see redis-deployment.yaml's own comment for the
2026-09-04 production incident (a Redis restart with no persistence wiped
`movement-events` and both consumer groups) that this default responds to.

### Sizing

Nearly all of this Redis's memory is `movement-events`. Its length is capped
at `movementRelay.streamMaxLen` entries (default 1,048,576, about 24 hours
of traffic at the ~1M entries/day measured in production). That default
comes from a 1 GiB budget at roughly 1 KiB per entry. `redis.maxmemory`
(1536mb) is that budget plus 50%, and the 2560Mi memory limit leaves room
above `maxmemory` for fragmentation and fork copy-on-write:

| | Memory |
|---|---|
| Steady state: 1,048,576 entries at ~920 B each | ~920 MiB |
| `maxmemory` 1536mb x 1.06 fragmentation | ~1628 MiB |
| + 25% copy-on-write during an AOF rewrite | +384 MiB |
| + process baseline and client buffers | +~30 MiB, ~2.0 GiB total |
| Limit | 2560Mi (~520 MiB spare) |

Change the three together. Every extra 100,000 entries needs about 100 MiB
more `maxmemory` and about 130 MiB more limit. At this size the AOF on
disk can reach 1-2 GB, so `redis.persistence.size` defaults to 4Gi (it was
1Gi before 2026-09-27; see the upgrade note under [Upgrade](#upgrade)); a
full volume makes Redis refuse writes.
RDB snapshots are off (`redis.save: ""`) because AOF already persists
everything, and each snapshot forks the process.

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
created. For a password-protected external Redis, keep `externalUrl`
credential-free and use `redis.auth` with `existingSecret` (next section);
a URL with inline credentials also works but is visible in the rendered
Deployment.

## Redis authentication (optional)

Off by default (`redis.auth.enabled: false`); with it off the chart renders
exactly what it rendered before the option existed. When enabled:

- every Redis client (api, enricher, trust-consumer, trust-backlog-consumer,
  full-coverage-consumer, movement-relay) gets `REDIS_PASSWORD` from a
  Secret via `secretKeyRef`. `REDIS_URL` stays credential-free; the services
  combine the two at startup (`crates/common/src/redis_auth.rs`, which
  percent-encodes the password, so any characters work) and authenticate as
  `AUTH default <password>`. The password is never in values, in the URL or
  in any rendered manifest, and the services never log it;
- with `redis.auth.requirePass: true` (the default) the bundled Redis starts
  with `--requirepass` from the same Secret, and `redis-cli` in the pod (the `exec` probes, `kubectl exec`) authenticates
  through `REDISCLI_AUTH`.

The password comes from `redis.auth.existingSecret` / `existingSecretKey`
(preferred; required for an external Redis) or, when that is empty, from a
random 32-character `redis-password` the chart generates in its own Secret
and keeps across upgrades (see "Generated secrets and the `lookup`
limitation").

**Enabling it on a running deployment, without an outage.** Valkey 8.1 (the
chart's image), like Redis 7.4, accepts `AUTH default <anything>` while the
default user has no password yet, and that is the form the clients send
(verified against `redis:7.4.11` and `valkey/valkey:8.1.10`). So give the
clients the password first:

1. Create the Secret, then upgrade with `redis.auth.enabled=true`,
   `redis.auth.requirePass=false`, `redis.auth.existingSecret=<name>`. Only
   the six client Deployments roll; the Redis pod is not touched. Confirm
   they are Ready and log no `NOAUTH`/`WRONGPASS`.
2. Upgrade again with `redis.auth.requirePass=true` (the default). Only Redis
   restarts (Recreate, AOF on the PVC: the same short gap as any Redis
   restart), and the clients reconnect with the password.

Doing both in one upgrade works too, but client pods still on the old spec
get `NOAUTH` until they are replaced. A fresh install can enable both at
once.

**Rotation.** Clients read the password at process start and Redis at pod
start; changing the Secret restarts nothing. To rotate without errors:
upgrade with `requirePass=false` (Redis restarts without a password),
update the Secret and `kubectl rollout restart` the six client
Deployments, then upgrade with `requirePass=true`.

## Redis ACL users (optional)

Off by default (`redis.acl.enabled: false`); with it off nothing below
renders. It builds on `redis.auth`: each client gets its own Redis user
with only the commands and keys it needs, and the shared `default` user can
then be turned off. The users and their rights are in
`files/redis-users.acl.tpl`; `crates/common/tests/redis_acl.rs` checks
them against a real Redis.

With `redis.acl.enabled` (and the bundled Redis), an initContainer renders
`users.acl` into a memory-backed emptyDir from one password per user in
`redis.acl.existingSecret` (`<user>-password`, required), and Redis starts
with `--aclfile` instead of `--requirepass`. `redis-cli` in the pod (the `exec` probes, `kubectl exec`) authenticates as
`ds-admin`. `redis.acl.clients.<client>` gives that client `REDIS_USERNAME`
and its own `REDIS_PASSWORD`.

The four-step, no-downtime rollout (`stage: open`, then the clients one by
one, then `stage: narrow`, then `defaultUser: "off"`), its checks and its
rollback are in `docs/redis-acl.md`.

**Render guards (security review, 2026-10-08).** An ingest stream producer
or the ingest-writer must reach Redis as its own narrow user, never as
`default` (`~* &* +@all`) or as a `client` user at stage `open` (the same
rights). The render fails:

- with `redis.acl.enabled` and `defaultUser` on but `redis.auth.enabled`
  off (`default` would be passwordless, M3);
- for an enabled `pollers.<ldbws|tfl|tocs|incidents>`, the
  full-coverage-consumer, an enabled island-of-Ireland poller, or any
  `ingestWriter.streams` entry not `off`,
  unless `redis.acl.enabled`, `redis.acl.stage: narrow` and that
  component's own `redis.acl.clients.<client>` are all set (H3);
- for any `ingestWriter.streams` entry on `apply` unless
  `redis.acl.defaultUser` is `"off"` or `"on-unshared"` (H3), which in turn needs every client on
  its own user. Only clients whose workload renders count: a disabled
  poller (the island-of-Ireland ones by default), movement-relay or
  ingest-writer has no pod to lock out, so its `clients.<client>` may stay
  `false`.

Since release B (ingest phase 5) the stream producers always XADD and the
writer always applies, so the ACL at its last step is a prerequisite of
every install (see "Release B" below).

## Per-service Postgres roles (optional)

Off by default (`postgresql.roles.perService.enabled: false`). Stage 0b of
`docs/postgres-app-role.md`: on top of `postgresql.roles`, each DB service
(api, aggregator, enricher, notifier) can connect as its own role, a member
of `distant_signal_app` with no grants of its own, so `pg_stat_statements`
and `pg_stat_activity` attribute every query to its service. The roles and
the grants they will later be narrowed to are in `files/db-grants.yaml`;
`scripts/gen-db-grants.py` renders them into `files/postgres-grants.sql`,
which the role setup Job runs. `perService.<service>.connect` moves one
service at a time; with the api on its own role its pool is
`perService.api.maxConnections` (16). The render fails if the connection
limits of every role in use exceed `max_connections` minus 3.

**Narrow-role components connect only as their role (security review H2,
2026-10-08).** Every component with a narrow role fails the render unless
its `postgresql.roles.perService.<role>.connect` is on (which needs
`postgresql.roles.enabled`, `setupJob.enabled` and `perService.enabled`):
it never falls back to `distant_signal_app` or the superuser.

| Component | Needs `perService.<role>.connect` |
|---|---|
| `pollers.stations` (enabled) | `stations` |
| `pollers.incidents` (enabled) | `incidents` |
| trust-backlog-consumer | `trust_backlog` |
| trust-consumer (its writes and its reads) | `trust_consumer` |
| full-coverage-consumer's reads | `full_coverage_ro` |
| `pollers.ldbws` (enabled; its reads) | `ldbws_ro` |
| `scheduleFeed` (enabled): schedule-ingest | `schedule_ingest` |
| `scheduleFeed` (enabled): schedule-reference | `schedule_reference` |
| `ingestWriter.streams.tfl: apply` (M4: `line_status`'s row policy binds the writer's role only) | `writer` |

Since release B these components always write or read Postgres directly,
so every install needs the bundled Postgres with `postgresql.roles`; an
external database cannot render the chart.

### Rollout strategy and connection limits

A per-service role's computed CONNECTION LIMIT covers every pod that can
hold a pool at once, plus 1 for the startup probe connection. On
2026-10-10 trust-backlog-consumer took the Deployment default rollout
(RollingUpdate, one surge pod) with a role limit of pool + 1 = 4: the old
pod held 3 backends, the surge pod could not connect past the limit, so it
never became Ready and the old pod was never stopped. Each Postgres
workload's rollout is now:

| Workload | Rollout | Per-service limit |
|---|---|---|
| trust-backlog-consumer, trust-consumer, full-coverage-consumer, enricher | `<component>.updateStrategy`, default `Recreate` | (replicas + maxSurge) × pool + 1; maxSurge is 0 under Recreate |
| aggregator, notifier, ingest-writer, schedulefeed (schedule-ingest, schedule-reference), every poller | fixed `Recreate` (two copies would double-write) | pool + 1 (writer: pool + lock session + 1) |
| api | `api.strategy.type`, Recreate or RollingUpdate (maxSurge 1, maxUnavailable 0) | (replicas + 1) × pool + 2 + the api-maintenance pool |
| movement-relay | RollingUpdate, maxSurge 1 (no Postgres) | none |

The limits (and every grant) are applied by the setup Job, a
**pre-upgrade hook** after the migrate Job, so they are in place before the
pods roll (docs/postgres-app-role.md "Hook order"). Without `migrate.job`,
and for the one upgrade that adds a password key to the chart's Secret, it
is a post-upgrade hook, which an upgrade stuck waiting on its pods never
reaches: then `ALTER ROLE ... CONNECTION LIMIT` by hand, or delete the old
pod, or patch the live Deployment to Recreate; see
docs/ingest-phase5-runbook.md §0.5 "Rollout deadlock".

## Release B: the producers' only paths

Since release A (2026-10-09) every ingest producer wrote to its new sink
and every internal reader read Postgres directly by default; release B
(ingest phase 5) removed the api's `/private` routes and the producers'
HTTP paths, so these are now the only paths, with no switch:

| Producer | Writes |
|---|---|
| poller-stations, poller-incidents | Postgres, each as its own role (poller-incidents also XADDs `incident-text-changed`) |
| trust-consumer, trust-backlog-consumer | Postgres, each as its own role |
| schedule-ingest, schedule-reference | Postgres, each as its own role |
| poller-ldbws, poller-tfl, poller-tocs | `ds:ingest:station-samples`, `ds:ingest:tfl`, `ds:ingest:reference` |
| full-coverage-consumer | `ds:ingest:full-coverage` |
| ingest-writer | applies the four streams (`ingestWriter.streams.<name>: apply`) |
| full-coverage-consumer, trust-consumer, poller-ldbws (reads) | read Postgres, each as its own read role |

No producer gets a `/private` URL, an internal OAuth credential,
`INGEST_SINK` or a `*_SOURCE` any more, and since release D the binaries
no longer read those two. The producers'
`postgresql.roles.perService.<role>.connect` (and the writer's) default to
`true`; a `connect` whose component is not deployed is a no-op.

Every install therefore needs the per-service Postgres roles, the
ingest-writer with its loops, and the Redis ACL through `defaultUser:
"on-unshared"` (or `"off"`). A values file without them fails the render
once, from `templates/zz-ingest-sink-preflight.yaml`, listing every missing
setting. `ci/ingest-prereqs.yaml` is the least a fresh install adds
(including `max_connections: 200`: the role limits exceed the default
100's budget); `values-example.yaml` carries the same.

The retired switches (`pollers.<name>.ingest.sink`,
`<consumer>.ingest.sink`, `scheduleFeed.ingest.sink`,
`scheduleFeed.reference.ingest.sink`, `<reader>.internalReads.source`) are
gone from `values.yaml`, and since release D nothing reads them: a values
file that still sets one renders as if it did not (delete it when
convenient). An `ingestWriter.streams` entry set to the removed `shadow`
fails the render, naming it. There is no per-producer
rollback: going back to the HTTP paths means the previous chart release and
images (`docs/ingest-phase5-runbook.md`).

## Migrations, maintenance and the ingest-writer (optional)

Ingest architecture phase 1B (`docs/superpowers/plans/2026-10-06-ingest-architecture-plan.md`).
Every switch is off by default and renders nothing until turned on. The api
image's `maintenance` and `ds-migrate` (1B.1) binaries, the `ingest-writer`
image and the api's `API_MIGRATE_ON_STARTUP` (1B.3) exist.

**The schema gate** (spec §12.2, plan 1B.2). The aggregator, enricher,
notifier and ingest-writer, and the api when `api.migrateOnStartup` is
`false`, wait before their loops, readiness and listener until the database
has the newest migration built into the image and their role holds the
grants `files/db-grants.yaml` gives it (one `has_table_privilege` per
table). They poll every 5 s and exit after 15 minutes. While they wait,
`distant_signal_db_schema_ready` is 0, and liveness stays up. On a database
that is already migrated (every deploy today, where the api migrates first)
the gate passes at its first check.

**The migrate hook Job** (`migrate.job.enabled`, `templates/migrate-job.yaml`)
runs `ds-migrate run` (plan 1B.1) from the api image as a
`pre-upgrade,post-install` hook (weight -5, `before-hook-creation`,
`backoffLimit: 1`, `activeDeadlineSeconds: 900`). On an upgrade it migrates
before any new pod starts; a failed migration fails the release and the old
pods keep serving. It waits for Postgres with `pg_isready` (an init container
from the Postgres image) and is the only pod with the schema owner's
`MIGRATION_DATABASE_URL`. With `networkPolicy.enabled`, two hook
NetworkPolicies (weight -10) admit it to Postgres and, with egress policies
on, let it out to Postgres and DNS; the role setup Job gets an egress policy
too. Raise the HelmRelease `timeout` to cover the deadline (20 minutes).

Rollout: enable the Job and set `api.migrateOnStartup: false` in the same
release; the api then has no `MIGRATION_DATABASE_URL` and gets
`API_MIGRATE_ON_STARTUP=false`, so it waits at the schema gate instead of
migrating (an image older than 1B.3 ignores the variable and still migrates,
so keep `true` until the image has 1B.3). Rollback: `api.migrateOnStartup: true`, then the Job off. The hook
NetworkPolicies outlive the Job; delete them by hand
(`kubectl delete networkpolicy -l app.kubernetes.io/component=migrate`).

**The api's rollout strategy** (`api.strategy.type`, default `Recreate`).
Once the hook Job migrates and `api.migrateOnStartup` is `false`, a new api
pod no longer changes the schema under the old one, and
`RollingUpdate` (`maxSurge: 1`, `maxUnavailable: 0`) ends the outage per
deploy. The render refuses `RollingUpdate` in any other configuration, and
counts the surge pod's pool in the Postgres connection budget (INF-7): with
the default 50-connection pool lower `api.database.maxConnections` first, or
move the api to its own role (16).

**Graceful shutdown and readiness** (`api.shutdown`,
`api.terminationGracePeriodSeconds`, `api.probes.readinessPath`). A deleted
api pod first runs its `preStop` sleep (5 s; the Service drops its endpoint
meanwhile), then on SIGTERM stops accepting connections, lets in-flight
requests finish (idle keep-alive connections are closed, HTTP/2 gets a
GOAWAY) and stops its background loops, releasing their advisory locks, for
up to `drainSecs` (20 s), all inside the 35 s grace period. Readiness is
`/public/ready`: 503 while the pod is draining. Together these let a
`RollingUpdate` deploy finish with no failed requests (plan 1B.10).
Liveness stays on `/public/health`. By default readiness does not check the
database: with one replica, NotReady would empty the Service, and callers
would get connection errors instead of the api's 503 + `Retry-After`. With
two or more replicas, `api.readiness.checkDatabase: true` also takes a pod
that cannot reach the database (bounded 2 s `SELECT 1`, cached 5 s) out of
the Service while the others serve; the render refuses it with fewer.

**The api-maintenance CronJob** (`apiMaintenance.enabled`,
`templates/api-maintenance-cronjob.yaml`) runs the api image's `maintenance`
binary (plan 1B.8) hourly (`concurrencyPolicy: Forbid`, `timeZone: Etc/UTC`):
one pass of the session cleanup, dead share/invite link prune and
personal-data retention that the api's own loop runs today, with the api's
retention values and the api's database role. Turn it on in the release
that sets `API_BACKGROUND_LOOPS=false` (`api.backgroundLoops: false`, plan
1B.7); both are idempotent.
Postgres admits it, and with egress policies on it may reach Postgres and
DNS.

**The ingest-writer** (`ingestWriter.enabled`,
`templates/ingest-writer-deployment.yaml`; image `ingestWriter.image`, plan 1B.6) is one
`Recreate` replica with the standard worker probes and metrics port. In
phase 1B it runs the train-domain loops (`ingestWriter.loops.enabled`, i.e.
`INGEST_WRITER_LOOPS`) under advisory locks, so it can overlap with the
api's own loops until `API_BACKGROUND_LOOPS=false`: the api's loops take
the same locks (plan 1B.7), so each sweep runs in one process at a time.
Cutover: `ingestWriter.loops.enabled: true` in one release, then
`api.backgroundLoops: false` with `apiMaintenance.enabled: true` in the
next. The loops' metrics are `distant_signal_loop_*` from either process
(`sum by (loop) (distant_signal_loop_lock_held)` is 1 while exactly one
process runs a loop). It connects as the app
role, or as `distant_signal_writer` (a member of app, created by the role
setup Job with `postgresql.roles.perService.enabled`) with
`perService.writer.connect`; its pool and its loop-lock session count in the
connection budgets. Its
NetworkPolicy admits the monitoring namespace (metrics) and the health port,
with egress to Postgres; Postgres admits it; the
PodMonitor scrapes it; `DistantSignalIngestWriterDown` fires when it is down
(see `docs/alerts.md`).

From phase 3 it also consumes the Redis ingest streams, each with its own
mode in `ingestWriter.streams` (`INGEST_WRITER_STREAMS`; the four producer
streams `apply` by default, the island-of-Ireland ones `off`): `apply`
writes (release B removed the cutover's `shadow` mode), recording each entry's key in `ingest_dedup` in the same
transaction (pruned hourly after 48 h) so a redelivered entry applies once.
It trims dead-letter entries older than 7 days hourly (`XTRIM … MINID ~`).
Once any stream is not `off` the writer gets `REDIS_URL`, its Redis user
(`redis.acl.clients.ingestWriter`),
`POD_NAME` (its consumer name) and NetworkPolicy egress to Redis, and
Redis admits it. It refuses to start a stream it has no handlers for yet:
station-samples and full-coverage get theirs in plan 3a.6, the others in
3c.

### schedule-reference's db sink (ingest plan 2a)

schedule-reference writes its products to Postgres directly through
`ds_store` (its only path since release B removed the api's `/private`
routes). Only the `reference` container gets its `DATABASE_URL`; it connects
as `distant_signal_schedule_reference`
(`postgresql.roles.perService.schedule_reference.connect`), with a pool of `scheduleFeed.reference.ingest.database.maxConnections`
(3), counted in the connection budgets, and waits for the schema gate before
its first poll. The schedulefeed NetworkPolicy gains Postgres egress and the
postgres policy admits `schedulefeed`. It counts
`distant_signal_db_writes_total{operation, outcome}` and
`distant_signal_store_schedule_publish_staged_mismatch_total`, which the
staged-mismatch alert reads alongside the api's name, and
`distant_signal_store_schedule_publish_rows_total{product, outcome}` (rows
written vs skipped as unchanged; the api counts the same under `api_`).
Rollback: `http`.

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

## Cold archive (optional)

Off by default. When `archive.enabled` is false, the aggregator renders no
`ARCHIVE_*` env and its retention prunes simply delete. When enabled, the
rows that `trains` retention is about to prune (together with their
`train_movement_events`/`train_current_state`/`train_reasons` children) are written first to
S3-compatible storage as zstd JSON Lines, and deleted only once the upload
is confirmed (size plus ETag = body MD5). `archive.s3.bucket` and
`archive.s3.existingSecret` are required when enabled. Something must also
expire archived objects, so the chart refuses to render until either
`archive.s3.lifecycleConfirmed: true` (you confirm the bucket has an S3
lifecycle expiration rule) or `archive.expiry.enabled: true` (the
aggregator expires them itself, for stores such as Thoth with no lifecycle
API; dry-run by default, with a hard 90-day retention floor, protected
prefixes and a per-run cap). `trust_event_backlog` and the LDBWS-derived
tables cannot be archived, and movement events are archived without their
TRUST `raw_body` (licensing). See [docs/cold-archive.md](../../docs/cold-archive.md)
for the key layout, the failure policy, and how to read an archive with
DuckDB.

## Movement-stream dead letters

`trust-consumer`, `trust-backlog-consumer` and `full-coverage-consumer` move
records that can never succeed (an explicit `api` data rejection, or a
malformed or unparseable entry) to the Redis stream
`movement-events-deadletter`. They never move an entry there only because
`api` was down or slow; those entries stay pending and are retried. The
stream is never trimmed. Alert on
`distant_signal_movement_feed_deadlettered_total`. See
[docs/movement-events-deadletter.md](../../docs/movement-events-deadletter.md)
for how to inspect records and re-inject them with `redis-cli`.

The same three consumers, and `movement-relay`, serve two health paths.
`/healthz` is readiness: it needs the Redis connection (for
`movement-relay`, a confirmed Kafka partition assignment). `/livez` is
liveness and does not depend on Redis, Kafka or Postgres. Both answer 503
`stalled` when no loop iteration has completed for
`<component>.progressStallSecs` (300s, or 900s for `fullCoverageConsumer` and
`movementRelay`). The liveness probes use `/livez`, so they restart a wedged
pod but not one that is waiting for Redis to come back.

## Full-coverage consumer restarts

On start, `full-coverage-consumer` waits for its first schedule population
load, then replays the current rail day from `movement-events` before it
consumes as its group, so a restart no longer corrupts that day's stats. A day
it cannot replay in full (its start already trimmed) is
marked `partial`. The measured startup peak with a production-sized population
and a full day's stream is about 373 MiB, well inside
`fullCoverageConsumer.resources.limits.memory`. With
`fullCoverageConsumer.windowedStats.enabled` the steady working set is
430-570 MiB (measured in shadow, 2026-10-02), bounded and reset at each
rail-day rollover; the request is sized for a ~650 MiB target. See
[docs/full-coverage-consumer.md](../../docs/full-coverage-consumer.md) for the
startup sequence, partial days, and the metrics to alert on
(`distant_signal_full_coverage_consumer_startup_complete`,
`..._day_partial`, `..._stream_gap_detected_total` and more).

## Scheduled jobs and time zones

Every CronJob this chart renders sets `spec.timeZone` explicitly, and CI
(`.github/workflows/ci.yml`, helm-lint job, "every CronJob sets timeZone")
fails any render that has a CronJob without one, or with a zone name that
tzdata doesn't know. Without the field, a schedule is read in the node's
local zone, which belongs to the host rather than the cluster. On
mine-bringer the node runs at UTC+2, so a job commented "02:00 UTC" really
ran at 00:00 UTC.

- Backups and maintenance use `Etc/UTC`, so they have no DST jumps.
- Jobs tied to the GB rail day use `Europe/London`, and keep their schedules
  out of 01:00–02:59 local, where DST transitions skip or repeat a run.

Start backup schedules at or after 03:00 UTC (mine-bringer's own backups
occupy 03:00–04:15, so the pgBackRest jobs start at 05:00). The nightly schedule
ingest takes deliveries between 22:00 and 01:30 and runs whole-day
publishes, the heaviest WAL and CPU bursts of the day, and 03:00 UTC is
clear of that window in both GMT and BST. See
[the backup design](../../docs/superpowers/specs/2026-09-30-backup-and-observability-gaps-design.md),
item 2.

## Point-in-time recovery (optional)

Off by default (`postgresql.pgbackrest.enabled: false`), and while off the
Postgres pod renders exactly as without it. When enabled, the bundled
Postgres runs `postgresql.pgbackrest.image` (the same
`postgres:18.6-trixie` plus pgBackRest and `pgbackrest-daily-check`, with
tini as PID 1; this repo doesn't build one, see docs/postgres-pitr.md),
archives its WAL to an S3
repository with client-side AES-256 encryption, and CronJobs take a weekly
full backup, a daily differential, a daily check (`check` and a WAL gap
check) and a weekly `verify`. The CronJobs `kubectl exec` into the Postgres
pod; their Role allows only `get` and `pods/exec` on that one pod.
`archive.queueMax` (archive-push-queue-max) drops WAL rather than let
`pg_wal` fill the disk when the repository is unreachable, and the daily
check reports the gap that leaves.

Required when enabled: `postgresql.pgbackrest.image.repository` and
`.tag` (the image's stable tag with its digest), `repo.path`, `repo.s3.endpoint`,
`repo.s3.bucket` and `repo.s3.existingSecret`, a Secret holding
`access-key-id`, `secret-access-key` and `cipher-pass`. No secret is ever
taken from values. Keep an offline copy of the cipher passphrase: the
repository can't be read without it.

Run `pgbackrest stanza-create` in the Postgres pod as soon as it is ready
after enabling (docs/postgres-pitr.md, "Enabling it", step 7); the chart
doesn't do it for you. Until then every archive-push fails: WAL builds up
in `pg_wal` and the spool and the archive alerts fire, but Postgres keeps
running. Only pin image tags with `-tini`: in older images the postmaster
is PID 1, and a failing async archive-push crash-restarts it every ~10 s.

A stop while S3 is unreachable waits for the archiver's last archive-push
attempts, each up to `archive.pushTimeoutSecs` (30s). With pgBackRest on,
the Postgres pod's `terminationGracePeriodSeconds` and `PGCTLTIMEOUT`
therefore default to 6 x that + 60 (240s): a stop then ends cleanly
instead of in a SIGKILL and crash recovery, and a first-start initdb
waits instead of failing. Still, enable pgBackRest on an initialised
database with S3 reachable (docs/postgres-pitr.md, "Enabling it").

It doesn't replace a logical dump. See
[docs/postgres-pitr.md](../../docs/postgres-pitr.md) for enabling it
(including `stanza-create`), the alerts, a point-in-time restore, and the
restore drill.

## Ingress

Off by default (`ingress.enabled: false`). The production deployment does not
use it at all: it runs no ingress controller, and public traffic arrives
through a Cloudflare tunnel (Cloudflare -> cloudflared -> the frontend
Service), with the frontend proxying `/api/*` to the in-cluster api Service.

When enabled, one `Ingress` object with up to two **separate hostnames**, both optional and
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

> If you do not need external API access, leave `ingress.api.enabled:
> false`; the frontend reaches the api over the in-cluster Service either
> way.
>
> Until 2026-09-25 it also published the api's `/metrics` endpoint the same
> way, because api served `/metrics` on its own public HTTP port rather
> than on a separate `metrics.port` — unauthenticated, internet-readable
> whenever `metrics.enabled` was also true (a Signal Box Audit Low
> finding). api now serves `/metrics` on its own internal-only listener,
> same as every other workload, so enabling `ingress.api.enabled` no
> longer exposes it at all.

Enabling either host without setting its hostname aborts the render.

## NetworkPolicy

Off by default (`networkPolicy.enabled: false`). Many clusters run a CNI
that does not enforce NetworkPolicy at all, and a silently-unenforced policy
is worse than an absent one because it looks like protection.

When enabled, the chart renders default-deny ingress per workload plus these
explicit allows:

- **postgres** ← api, aggregator, enricher and notifier only (the pollers
  and consumers never talk to it; they reach the database only indirectly,
  via the api's ingest endpoints).
- **redis** ← api, enricher, trust-consumer, trust-backlog-consumer,
  full-coverage-consumer and movement-relay. Rendered only when
  `redis.enabled`.
- **postgres/redis ← workloads outside the chart** (off by default):
  `networkPolicy.postgresClients` and `networkPolicy.redisClients` are lists
  of NetworkPolicy peers (`podSelector`, `namespaceSelector`) admitted on the
  service port, e.g. a postgres-exporter, a pg_dump backup CronJob or a
  redis-exporter. There are two ways to admit them: these values (the chart
  then owns every way into the database), or a NetworkPolicy kept beside
  the release (NetworkPolicies are additive, so it keeps working).
  `networkPolicy.components.postgres.extraIngress` takes whole rules when a
  client needs another port. The pgBackRest CronJobs need neither: they
  `kubectl exec` into the Postgres pod through the API server.
- **api** ← frontend, every enabled poller, the consumers, schedulefeed, and — when `ingress.enabled` and
  `ingress.api.enabled` — the namespace named by
  `networkPolicy.ingressControllerNamespace`, plus every namespace in
  `networkPolicy.apiExtraIngressNamespaces`, all on `api.service.port`.
  An extra namespace with an entry in
  `networkPolicy.apiExtraIngressPodLabels` admits only the pods matching
  those labels. The default entry narrows `ds-mcp` to the
  Distant-Signal-MCP's `mcp` pod, so its Redis cannot reach the api.
- **frontend** ← that same ingress-controller namespace, when
  `ingress.enabled` and `ingress.frontend.enabled`, and the tunnel
  connector pods, when `networkPolicy.tunnel.enabled`.
- **api**: `metrics.port` from the namespace named by
  `networkPolicy.monitoringNamespace` when `metrics.enabled`. This is
  separate from, and does not widen, the `api.service.port` allow above: api
  serves `/metrics` on its own internal-only listener.
- **Background workers** (aggregator, enricher, notifier, trust-consumer,
  trust-backlog-consumer, full-coverage-consumer, movement-relay, every
  poller including the three island-of-Ireland ones; INF-10): their own
  metrics port from the monitoring namespace (when `metrics.enabled`), and
  their health port(s) from any source, because kubelet probes come from the
  node, which no selector can name. Nothing else. `networkPolicy.healthIngress`
  scopes the health ports: `from` lists the allowed peers (e.g. the nodes'
  addresses as ipBlocks), and `enabled: false` drops the rule. kube-router
  (k3s's policy controller) accepts all traffic from a pod's own node before
  any policy applies, and Calico and Cilium allow it by default, so probes
  keep working there either way. Check your CNI before dropping it: one that
  filters node traffic would fail every probe.
- **schedulefeed-sftp** (INF-2): SFTP on `scheduleFeed.sftp.port` from any
  source, or only from `scheduleFeed.sftp.allowedCidrs` when set. The
  allow-list only works when the pod sees the client's real address
  (`scheduleFeed.service.externalTrafficPolicy: Local`, or a load balancer
  that preserves it);
  behind source NAT it blocks every push. Also SFTPGo's telemetry port from
  the monitoring namespace. Egress is DNS only. With
  `scheduleFeed.sftp.separateDeployment: false` these SFTP rules are on the
  schedulefeed policy instead.
- **schedulefeed**: the `ingest` and `reference` containers' health ports
  and metrics ports. Egress to the api, the OAuth token endpoint, the
  bucket's endpoints when on, and Postgres under either db sink.

**Tunnels (cloudflared).** With no Ingress, a tunnel connector running in
the cluster publishes the site. `networkPolicy.tunnel.enabled: true` (off by
default) admits the pods matching `networkPolicy.tunnel.podLabels` in
`networkPolicy.tunnel.namespace` (default `cloudflared`,
`app.kubernetes.io/name: cloudflared`) to the frontend only. The api keys its
rate limits on the `X-Real-IP` header (`api.rateLimit.trustXRealIp`), which
the frontend's proxy overwrites from `CF-Connecting-IP`. A request sent
straight from the tunnel to the api keeps whatever `X-Real-IP` its client
sent, so the client could choose its own rate-limit key. Route public
hostnames to the frontend. If a hostname really must go straight to the api,
set `networkPolicy.tunnel.api: true`. The render then fails unless
`api.rateLimit.trustXRealIp` is false. With that off, every request the
frontend proxies shares the frontend pod's one bucket, so the per-client
limits become one site-wide limit.

NetworkPolicies are additive. A cluster-level policy that already lets the
tunnel reach the api on `api.service.port` still does after this.
Narrowing the chart's policies cannot remove it.

**Egress is unrestricted by default.** `networkPolicy.egress.enabled: true`
(off by default) adds an egress policy to every component. Each may reach
DNS (port 53), the in-cluster services it calls, and, where it needs it, the
public internet minus `networkPolicy.egress.privateCidrs`/`privateCidrsV6`
and `extraDeniedCidrs`. That stops a notifier tricked into pushing to a
private address, or a compromised poller, from reaching the rest of the
cluster.

| Component | In-cluster | Public internet (why) |
|---|---|---|
| api | postgres, redis, dev IdP | yes (OIDC discovery and JWKS for `api.sso.issuerUrl` and `api.internalOauth.issuerUrl`) |
| frontend | api | no (the `/chat` Anthropic calls run in the browser) |
| aggregator | postgres | only with `archive.enabled` (S3) |
| enricher | postgres, redis | yes (`enricher.llm.baseUrl`, or `enricher.llm.anthropic.baseUrl` with provider `anthropic`) |
| notifier | postgres | yes (Web Push services) |
| pollers, consumers, movement-relay | api and/or redis, dev IdP | yes (upstream feeds, Kafka, the OAuth token endpoint) |
| schedulefeed | api, dev IdP | yes (the OAuth token endpoint) |
| postgres | none | only with `postgresql.pgbackrest.enabled` (the repository's S3) |
| redis | none | no |

DNS goes to any destination on port 53 unless `networkPolicy.egress.dnsPeers`
names the resolvers (e.g. CoreDNS: namespace `kube-system`, pods
`k8s-app: kube-dns`; leave it empty with NodeLocal DNSCache).

The internet rule allows only TCP `networkPolicy.egress.internetPorts`
(default 443) plus the ports each component's own upstreams use, read from
its configuration: an explicit port or the scheme's default in its URLs
(the Irish Rail live feed is `http://`, so port 80), the Kafka brokers'
ports, and `postgresql.pgbackrest.repo.s3.port`. Web Push services, OIDC and
the OAuth token endpoint use 443. A self-hosted push service or other
upstream on another port needs `networkPolicy.components.<component>.internetPorts`;
`[]` (globally or per component) allows every port.

`networkPolicy.components.<component>` (keyed by the
`app.kubernetes.io/component` label) tunes one component: `internet`
adds or drops its public-internet rule, `egress: false` leaves it
without an egress policy, and `extraEgress`/`extraIngress` append raw
NetworkPolicy egress/ingress rules to that component's policy alone (e.g.
only the aggregator may reach a private archive endpoint). Before enabling, add a rule for anything reached
at a private address: an external Redis or Postgres, an OIDC or OAuth
endpoint inside the cluster or on a tailnet (`100.64.0.0/10`), a private
Kafka broker, LLM endpoint, archive or pgBackRest S3 endpoint, or a proxy.

**Exclude the nodes' own public addresses.** `privateCidrs` names only the
reserved ranges. A node with a public IP is therefore reachable through the
internet rule: the API server (6443), the kubelet (10250) and every port the
host publishes. List those addresses in
`networkPolicy.egress.extraDeniedCidrs` (`/32` or `/128`; IPv4 and IPv6 may
be mixed). They are added to the defaults rather than replacing them, and
because policies are additive, no policy outside the chart can take this
allowance away.

## Distant-Signal-MCP as a service caller

The Distant-Signal-MCP is a separate release (its own `ds-mcp` namespace)
that calls this api's **public** routes in-cluster, directly, not through
the frontend. Without help it would have no `X-Real-IP`, so every MCP user
would share the MCP pod's single anonymous bucket. Instead it proves who it
is with an internal OAuth2 client-credentials token (the only internal
token the api verifies since release B removed `/private`), and gets its own, **finite** budget (`api.rateLimit.mcp`, 5x
the public defaults), keyed on the caller (`svc:mcp`, one bucket for the
whole service). It is not exempt: all other validation and timeouts apply,
and a runaway loop or leaked credential is capped by that budget.

On `/Trips/plan`, `/Train/by-uid/*` and non-GET public routes:

| Request | Result |
|---|---|
| No `Authorization: Bearer` | Anonymous, per client IP (unchanged; the frontend never forwards `Authorization`). |
| Bearer verifies and its `groups` contain `api.internalOauth.groups.mcp` | The MCP budget. |
| Bearer fails verification (malformed, expired, bad signature, wrong issuer/audience) | `401` with `WWW-Authenticate: Bearer error="invalid_token"`. Never falls back to anonymous. |
| Bearer verifies but lacks the MCP group | `403`, logged with the token's `sub`. |

Login routes ignore the bearer. With `api.internalOauth.groups.mcp` empty
the whole feature is inert and any bearer is ignored. End-user auth is the
session cookie, never `Authorization`, so the bearer does not change who
the request is as far as any handler is concerned. Rejections are counted in
`distant_signal_api_rate_limit_service_auth_rejected_total{class,reason}` and
429s in `distant_signal_api_rate_limited_total{class,caller="mcp"}`.

**What the MCP sends.** Before calling the api (and again before the
token's `exp`), POST a client-credentials grant to the token endpoint:

```
POST <the provider's token endpoint>   # e.g. https://sso.example.com/application/o/token/
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<api.internalOauth.clientId>
&username=<the MCP service account's username>&password=<its app password/token>
&scope=groups
```

then send `Authorization: Bearer <access_token>` on every api call. The
token's `aud` must be `api.internalOauth.clientId`, its `iss` must be `api.internalOauth.issuerUrl`,
and its `groups` claim must contain `api.internalOauth.groups.mcp`. Cache
the token until shortly before `exp`; don't fetch one per request. On `401`
refresh the token once; on `429` honour `Retry-After`.

**Operator steps.** In Authentik, under the existing internal-service OAuth2
provider (the one whose client id is `api.internalOauth.clientId`): create a
service account for the MCP (e.g. `srv-ds-mcp`) and a group of the same name
(`srv-ds-mcp`), add the account to the group, and give the MCP its username
and app password. Then, in
this chart's values: set `api.internalOauth.groups.mcp` to that group (the
default is `srv-ds-mcp`), optionally tune `api.rateLimit.mcp`, and, when
`networkPolicy.enabled`, add the MCP's namespace to
`networkPolicy.apiExtraIngressNamespaces` (e.g. `[ds-mcp]`). The default
`networkPolicy.apiExtraIngressPodLabels.ds-mcp` then admits only the MCP pod
(`app.kubernetes.io/name: distant-signal-mcp`,
`app.kubernetes.io/component: mcp`). Change it if your MCP release sets a
`nameOverride`, or add an entry for a namespace with a different name. Any
cluster-level default-deny policy needs the same allowance.

## Enabling the pollers

| Poller | Base URL env var | Writes | Default cadence (s) |
|---|---|---|---|
| `incidents` | `RDM_INCIDENTS_BASE_URL` | Postgres (`distant_signal_incidents`) | 300 |
| `stations` | `RDM_STATIONS_BASE_URL` | Postgres (`distant_signal_stations`) | 86400 |
| `tocs` | `RDM_TOCS_BASE_URL` | `ds:ingest:reference` | 86400 |
| `ldbws` | `LDBWS_BASE_URL` | `ds:ingest:station-samples` | 60 |
| `tfl` | `TFL_BASE_URL` | `ds:ingest:tfl` | 300 |

`ldbws` additionally sets `NUM_ROWS`, and reads its sample stations from
Postgres as `distant_signal_ldbws_ro`. Each needs its role's or Redis
user's `connect`/`redis.acl.clients.poller<Name>` (the render says which).

**All five are disabled by default.** The four RDM feeds are real, and
production runs all four, but each base URL and API key comes from the
operator's own Rail Data Marketplace subscription (the repository owner
accepted the RDM terms for the current and planned use on 2026-09-27). The
chart therefore ships no RDM base URL: `pollers.<name>.baseUrl` defaults to
`""`, and `local.env.example` uses non-resolving `*.example.invalid`
placeholders. A default install works immediately instead of running four
pods that cannot authenticate. `tfl` has a working default `baseUrl`
(`https://api.tfl.gov.uk`) but is still off by default; it reads its
subscription key from `TFL_APP_KEY` rather than `RDM_API_KEY`.

The three island-of-Ireland pollers (`pollerIrishRailGtfs`,
`pollerIrishRailLive`, `pollerNirStations`) are separate top-level values,
also off by default, with working public default URLs; see their comments
in `values.yaml`. Since ingest plan 3c.2 (decision D8) each writes only to
its own Redis stream (`ds:ingest:ioi-gtfs`, `ds:ingest:ioi-live`,
`ds:ingest:ioi-nir`), which the ingest-writer applies
(`ingestWriter.streams.ioi-gtfs` / `ioi-live` / `ioi-nir: apply`); they no longer
call the api, so they need no OAuth credential or api-side group.

Enabling a poller without setting its `baseUrl` **aborts the render** with an
explicit message, rather than deploying a pod that cannot work.

```bash
helm upgrade distant-signal ./charts/distant-signal -n distant-signal \
  --set pollers.incidents.enabled=true \
  --set pollers.incidents.baseUrl=https://rdm.example.com/incidents \
  --set pollers.incidents.apiKey=your-rdm-key
```

## Logging

Every Rust workload and the frontend's server side log **one JSON object
per line** on stdout (no ANSI colour, no multi-line output), for Loki/Alloy.
The chart sets nothing for this: `LOG_FORMAT` defaults to `json` in the
binaries themselves (`common::logging`, `frontend/lib/logger.ts`). To get
human-readable text from one workload while debugging, add
`LOG_FORMAT=pretty` through its `extraEnv` (where it has one). Verbosity is
unchanged: the per-component `logLevel` values (`RUST_LOG`, or the
notifier's `LOG_LEVEL`).

| Key | Value |
|---|---|
| `timestamp` | RFC 3339, UTC (`2026-10-01T09:30:00.123456Z`; the frontend has millisecond precision) |
| `level` | `TRACE`, `DEBUG`, `INFO`, `WARN` or `ERROR` |
| `service` | the binary: `api`, `aggregator`, `notifier`, `enricher`, `movement-relay`, `trust-consumer`, `full-coverage-consumer`, `trust-backlog-consumer`, `poller-<name>`, `schedule-ingest`, `schedule-reference`, `frontend` |
| `target` | Rust: the module path (`api::routes::lines`), the `log` crate's target (`librdkafka`), `panic` or `fatal`. Frontend: the module (`lib/api`), `next` (`onRequestError`) or `console` (Next's own console output) |
| `message` | the event's message |
| *(event fields)* | flattened to the top level as snake_case keys (`line_id`, `count`, ...) |
| `error` | an error's text with its cause chain |
| `stack` | a backtrace: panics, frontend `Error`s |
| `spans` | Rust only, when inside a span: the enclosing spans root first, each `{"name": ..., <fields>}` (e.g. the api's per-request span) |

A panic is one `ERROR` line with target `panic`, message
`panicked at <file:line:col>: <payload>` and a `stack`; an error returned from
`main` is one `ERROR` line with target `fatal`. Credentials are never logged
(Rust `Secret` fields print `Secret(***)`; the frontend logger redacts
token/secret/password/cookie/authorization/email keys).

LogQL examples:

```logql
{namespace="distant-signal"} | json | level="ERROR"
{namespace="distant-signal"} | json | target="panic"
{namespace="distant-signal"} | json | service="trust-consumer" | message=~".*NOGROUP.*"
```

Clap's own argument errors (a missing required env var at startup) are
still printed by clap as plain text before logging starts, then the process
exits with code 2.

## Postgres and Redis probes

The bundled Postgres and Redis have startup, readiness and liveness probes
set by `postgresql.probes` and `redis.probes`. Each probe's `type` is
`tcpSocket` (the default) or `exec`:

- `tcpSocket`: the kubelet opens a TCP connection to the container port.
  Nothing runs in the container. The defaults probe readiness every 10s
  and liveness every 30s, allowing 6 liveness failures (3 minutes).
- `exec`: the old commands, `pg_isready` and `redis-cli ping` (`--user
  ds-admin` with `redis.acl.enabled`). Each period forks a process
  through containerd and its shim. Until this chart version, readiness
  ran every 5s, which adds load to a CPU-saturated node.

The trade-off: a TCP probe passes once the server listens, before it can
answer queries.

- Readiness turns true slightly earlier during Postgres crash recovery
  (WAL redo after an unclean shutdown) and while Redis loads its AOF.
  Until they finish, clients get "the database system is starting up" or
  `-LOADING` errors instead of finding no endpoint.
- Liveness no longer kills Postgres in the middle of WAL redo, or Redis in
  the middle of a long AOF load. With `exec`, the startup probe's budget
  (`postgresql.probes.startup`, 10s x 90 = 15 minutes) was all that
  protected a long redo. A Postgres that listens but has hung is no longer
  restarted. To keep `pg_isready` semantics for one probe only, set
  `postgresql.probes.readiness.type: exec` and leave liveness on TCP.

TCP probes come from the node, like the api's and the workers' HTTP
probes, so they rely on the CNI not applying NetworkPolicy to node-local
traffic (see `networkPolicy.healthIngress`). On a CNI that does, admit the
node through `networkPolicy.postgresClients` and `networkPolicy.redisClients`,
or use `exec`.

Changing either probe changes the pod template, so the upgrade that first
applies these defaults restarts Postgres (StatefulSet) and Redis
(Deployment, `Recreate`) once.

## Values reference

### Global

| Key | Default | Description |
|---|---|---|
| `nameOverride` | `""` | Override the chart name used in resource names and labels. |
| `fullnameOverride` | `""` | Override the fully-qualified release name entirely. |
| `imagePullSecrets` | `[]` | Image pull secrets applied to every pod in the chart. |
| `revisionHistoryLimit` | `3` | Old ReplicaSets and StatefulSet ControllerRevisions kept, on every Deployment and StatefulSet (Kubernetes' default is 10). A whole number >= 0; `helm rollback` does not need them. |
| `global.imageRegistry` | `""` | Registry (and optional path prefix) put in front of every image in place of the registry its `image.repository` carries, e.g. `registry.example.com/myfork`. Empty keeps each repository as written. |
| `serviceAccount.create` | `true` | Create a ServiceAccount for the chart's workloads. |
| `serviceAccount.name` | `""` | Name to use. Empty + `create` uses the fullname. |
| `serviceAccount.annotations` | `{}` | Annotations for the ServiceAccount (e.g. workload identity). |

### Shared secrets

| Key | Default | Description |
|---|---|---|
| `secrets` | `{}` | Reserved for a future chart-wide secret not tied to one service; currently unused. |

`secrets` is currently empty — reserved for any future chart-wide secret
that isn't tied to one specific service. The shared internal-token header
this block used to hold is retired, and so is the top-level
`internalOauth` block (the producers' token endpoint and client id), which
release B (ingest phase 5) removed with the producers' `/private` paths.

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
| `postgresql.image.tag` | `18.6-trixie@sha256:…` | Postgres 18, the major the compose stack and CI use, digest-pinned in the tag. An existing 16 data directory needs pg_upgrade first (docs/postgres-18-upgrade.md). |
| `postgresql.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `postgresql.service.port` | `5432` | Port the headless Service and the container listen on. |
| `postgresql.connectionBudget.externalClients` | `2` | Connections the render-time budget check reserves for clients deployed outside this chart that connect as the app role: Ranma-Config's postgres-exporter (1) and its nightly `pg_dump` (1). The app role is a superuser, so `superuser_reserved_connections` protects none of these. pgBackRest adds 2 more on its own when enabled. |
| `postgresql.connectionBudget.adminSessions` | `3` | Interactive `kubectl exec ... psql` sessions the budget check keeps room for. |
| `postgresql.roles.enabled` | `false` | Connect the api pools and every worker as `app`, api's migrations as `owner`, and the pgBackRest CronJobs as `backup` (docs/postgres-app-role.md). The roles must exist first (`setupJob`, or `initScript` on a new cluster). Does not restart Postgres on its own. |
| `postgresql.roles.initScript` | `true` | With `enabled`: run files/postgres-roles.sql from /docker-entrypoint-initdb.d, which the image runs only on an EMPTY data directory (a new cluster), before any client connects. Turning it on restarts Postgres: set it false on an existing cluster. |
| `postgresql.roles.setupJob.enabled` | `false` | Run files/postgres-roles.sql as a Helm hook Job (post-install; pre-upgrade after the migrate hook Job, or post-upgrade without migrate.job: docs/postgres-app-role.md "Hook order"), as the superuser over the Service, on every install and upgrade (idempotent: creates/updates the roles, moves ownership to `owner`, grants). Independent of `enabled`: on an existing cluster turn this on first. A failure fails the release. |
| `postgresql.roles.setupJob.activeDeadlineSeconds` | `600` | Seconds before the Job gives up (at least 120). The pod keeps 60 s of it in hand: its bounded wait for Postgres, every try and backoff end by then, so it fails by itself and keeps its log (docs/postgres-app-role.md "When the setup Job fails"). |
| `postgresql.roles.setupJob.backoffLimit` | `0` | New pods after a failed one. The pod retries a lock timeout and the like itself; a new one would only repeat a final error with less of the deadline left. |
| `postgresql.roles.setupJob.attempts` | `5` | Tries per script when one fails for a reason a later try can get past (lock or statement timeout, deadlock, lost connection). Backoff 5 s, doubling, at most 60 s; never past the deadline. |
| `postgresql.roles.setupJob.lockTimeout` | `10s` | `lock_timeout` of the setup's sessions: a lock queued behind a long transaction fails and is retried instead of waiting (and blocking the queries behind it) until the deadline. |
| `postgresql.roles.setupJob.statementTimeout` | `120s` | `statement_timeout` of the setup's sessions (each section is one statement). |
| `postgresql.roles.setupJob.resources` | requests `10m`/`32Mi`, limit `128Mi` | Container resources. |
| `postgresql.roles.setupJob.podSecurityContext` | `{}` | Overrides the pod securityContext (default: uid/gid 999, the image's postgres user, non-root, RuntimeDefault seccomp). |
| `postgresql.roles.owner.username` | `distant_signal_owner` | Role name of the schema owner. |
| `postgresql.roles.owner.password` | `""` | Password. Generated (32 alphanumeric chars, kept across upgrades) when empty and no `existingSecret`. |
| `postgresql.roles.owner.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.owner.existingSecretPasswordKey` | `postgres-owner-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.owner.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `api.replicaCount` + 2. |
| `postgresql.roles.app.username` | `distant_signal_app` | Role name the services connect as. |
| `postgresql.roles.app.password` | `""` | Password. Generated (32 alphanumeric chars, kept across upgrades) when empty and no `existingSecret`. |
| `postgresql.roles.app.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.app.existingSecretPasswordKey` | `postgres-app-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.app.connectionLimit` | `""` | CONNECTION LIMIT. Empty: the chart's pools (api `maxConnections` x `replicaCount`, aggregator 10, notifier 5, enricher 5, archive 2 when enabled) plus `connectionLimitSlack`. It is what makes `superuser_reserved_connections` a real admin reserve. |
| `postgresql.roles.app.connectionLimitSlack` | `5` | Added to the computed CONNECTION LIMIT, for one-off binaries (backfills) run as the app role. |
| `postgresql.roles.exporter.username` | `distant_signal_exporter` | Role name for postgres-exporter (pg_monitor). |
| `postgresql.roles.exporter.password` | `""` | Password. Generated (32 alphanumeric chars, kept across upgrades) when empty and no `existingSecret`. |
| `postgresql.roles.exporter.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.exporter.existingSecretPasswordKey` | `postgres-exporter-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.exporter.connectionLimit` | `3` | CONNECTION LIMIT. |
| `postgresql.roles.dump.username` | `distant_signal_dump` | Role name for pg_dump (pg_read_all_data). |
| `postgresql.roles.dump.password` | `""` | Password. Generated (32 alphanumeric chars, kept across upgrades) when empty and no `existingSecret`. |
| `postgresql.roles.dump.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.dump.existingSecretPasswordKey` | `postgres-dump-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.dump.connectionLimit` | `2` | CONNECTION LIMIT (a parallel `pg_dump -j N` needs N + 1). |
| `postgresql.roles.backup.username` | `distant_signal_backup` | Role name for pgBackRest (EXECUTE on pg_backup_start/stop, pg_switch_wal, pg_create_restore_point; pg_read_all_settings, pg_checkpoint). |
| `postgresql.roles.backup.password` | `""` | Password. Generated (32 alphanumeric chars, kept across upgrades) when empty and no `existingSecret`. Defence in depth: pgBackRest uses the trusted local socket and never sends it. |
| `postgresql.roles.backup.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.backup.existingSecretPasswordKey` | `postgres-backup-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.backup.connectionLimit` | `4` | CONNECTION LIMIT. |
| `postgresql.roles.backup.database` | `postgres` | Database pgBackRest connects to (its pg1-database default); the function grants are made there and in `auth.database`. |
| `postgresql.roles.perService.enabled` | `false` | Create the per-service roles: the setup Job (and, on a new cluster, `initScript`) also runs files/postgres-grants.sql. |
| `postgresql.roles.perService.api.connect` | `false` | Connect the api pools as `distant_signal_api` instead of `app` (needs `enabled` here and `roles.enabled`). |
| `postgresql.roles.perService.api.maxConnections` | `16` | The api pool size (DATABASE_MAX_CONNECTIONS) while `connect` is on, instead of `api.database.maxConnections` (spec §6.6: 16; the observed peak is 6). |
| `postgresql.roles.perService.api.password` | `""` | Password. |
| `postgresql.roles.perService.api.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.api.existingSecretPasswordKey` | `postgres-api-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.api.connectionLimit` | `""` | CONNECTION LIMIT. |
| `postgresql.roles.perService.aggregator.connect` | `false` | Connect the aggregator as `distant_signal_aggregator`. |
| `postgresql.roles.perService.aggregator.password` | `""` | Password. |
| `postgresql.roles.perService.aggregator.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.aggregator.existingSecretPasswordKey` | `postgres-aggregator-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.aggregator.connectionLimit` | `""` | CONNECTION LIMIT. |
| `postgresql.roles.perService.enricher.connect` | `false` | Connect the enricher as `distant_signal_enricher`. |
| `postgresql.roles.perService.enricher.password` | `""` | Password. |
| `postgresql.roles.perService.enricher.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.enricher.existingSecretPasswordKey` | `postgres-enricher-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.enricher.connectionLimit` | `""` | CONNECTION LIMIT. |
| `postgresql.roles.perService.notifier.connect` | `false` | Connect the notifier as `distant_signal_notifier`. |
| `postgresql.roles.perService.notifier.password` | `""` | Password. |
| `postgresql.roles.perService.notifier.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.notifier.existingSecretPasswordKey` | `postgres-notifier-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.notifier.connectionLimit` | `""` | CONNECTION LIMIT. |
| `postgresql.roles.perService.writer.connect` | `true` | Connect the ingest-writer (`ingestWriter.enabled` required) as `distant_signal_writer`. The role is created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.writer.password` | `""` | Password. |
| `postgresql.roles.perService.writer.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.writer.existingSecretPasswordKey` | `postgres-writer-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.writer.connectionLimit` | `""` | CONNECTION LIMIT. Empty: its pool (`ingestWriter.database.maxConnections`), plus 1 for the loop-lock session, plus the 1 every role has for a probe or brief overlap (pool 6, limit 8). |
| `postgresql.roles.perService.schedule_ingest.connect` | `true` | Connect schedule-ingest (`scheduleFeed.enabled`) as `distant_signal_schedule_ingest`: a narrow role, not a member of app, with only the CORPUS and feed-marker grants. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.schedule_ingest.password` | `""` | Password. |
| `postgresql.roles.perService.schedule_ingest.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.schedule_ingest.existingSecretPasswordKey` | `postgres-schedule-ingest-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.schedule_ingest.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `scheduleFeed.ingest.database.maxConnections` + 1. |
| `postgresql.roles.perService.schedule_reference.connect` | `true` | Connect schedule-reference (`scheduleFeed.enabled`) as `distant_signal_schedule_reference`, a narrow role (not a member of app; exactly its `files/db-grants.yaml` grants). Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.schedule_reference.password` | `""` | Password. |
| `postgresql.roles.perService.schedule_reference.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.schedule_reference.existingSecretPasswordKey` | `postgres-schedule-reference-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.schedule_reference.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `scheduleFeed.reference.ingest.database.maxConnections` + 2 (its pool, its run-lock session and one spare). |
| `postgresql.roles.perService.stations.connect` | `true` | Connect poller-stations (`pollers.stations.enabled`) as `distant_signal_stations` (plan 2b.3). A narrow role, not a member of `app`: SELECT, INSERT and UPDATE on `stations` and `ingest_freshness` only. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.stations.password` | `""` | Password. |
| `postgresql.roles.perService.stations.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.stations.existingSecretPasswordKey` | `postgres-stations-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.stations.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `pollers.stations.ingest.database.maxConnections` + 1. |
| `postgresql.roles.perService.incidents.connect` | `true` | Connect poller-incidents (`pollers.incidents.enabled`) as `distant_signal_incidents`, a `narrow` role (exactly its `db-grants.yaml` grants, never an app member). The role is created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.incidents.password` | `""` | Password. |
| `postgresql.roles.perService.incidents.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.incidents.existingSecretPasswordKey` | `postgres-incidents-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.incidents.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `pollers.incidents.ingest.database.maxConnections` + 1. |
| `postgresql.roles.perService.trust_backlog.connect` | `true` | Connect trust-backlog-consumer as `distant_signal_trust_backlog` (plan 3b.2). A narrow role, not a member of `app`: the backlog, shared-movement and reasons writes and the STANOX/CRS read only. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.trust_backlog.password` | `""` | Password. |
| `postgresql.roles.perService.trust_backlog.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.trust_backlog.existingSecretPasswordKey` | `postgres-trust-backlog-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.trust_backlog.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `trustBacklogConsumer.ingest.database.maxConnections` + 1. |
| `postgresql.roles.perService.trust_consumer.connect` | `true` | Connect trust-consumer as `distant_signal_trust_consumer` (plans 3b.4, D1, and 4.4). A narrow role, not a member of `app`: the train-event, forward-signal and `train_event_outbox` writes, SELECT only on `train_subscriptions`, and the phase 4 reads. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while the component is deployed, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled` (the render fails otherwise, never falling back to the app role). |
| `postgresql.roles.perService.trust_consumer.password` | `""` | Password. |
| `postgresql.roles.perService.trust_consumer.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.trust_consumer.existingSecretPasswordKey` | `postgres-trust-consumer-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.trust_consumer.connectionLimit` | `""` | CONNECTION LIMIT. Empty: its one pool (the larger of `trustConsumer.ingest.database.maxConnections` and `trustConsumer.internalReads.database.maxConnections`) + 1. |
| `postgresql.roles.perService.full_coverage_ro.connect` | `true` | Connect full-coverage-consumer's direct reads as `distant_signal_full_coverage_ro` (ingest plan 4.7). A narrow, read-only role, not a member of `app`: SELECT on the line populations and STANOX/CRS only. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): always takes effect (the consumer always runs), and needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled`. |
| `postgresql.roles.perService.full_coverage_ro.password` | `""` | Password. |
| `postgresql.roles.perService.full_coverage_ro.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.full_coverage_ro.existingSecretPasswordKey` | `postgres-full-coverage-ro-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.full_coverage_ro.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `fullCoverageConsumer.internalReads.database.maxConnections` + 1. |
| `postgresql.roles.perService.ldbws_ro.connect` | `true` | Connect poller-ldbws's direct sample-station read as `distant_signal_ldbws_ro` (ingest plan 4.7). A narrow, read-only role, not a member of `app`: SELECT on the views `ingest_sample_station_pins` and `ingest_custom_line_stations` only. Created with the others whenever `perService.enabled`, unused until then. Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"): takes effect only while poller-ldbws is enabled, and then needs `perService.enabled`, `postgresql.roles.enabled` and `setupJob.enabled`. |
| `postgresql.roles.perService.ldbws_ro.password` | `""` | Password. |
| `postgresql.roles.perService.ldbws_ro.existingSecret` | `""` | Read the password from this pre-existing Secret instead. |
| `postgresql.roles.perService.ldbws_ro.existingSecretPasswordKey` | `postgres-ldbws-ro-password` | Key within `existingSecret` (and in the chart's Secret). |
| `postgresql.roles.perService.ldbws_ro.connectionLimit` | `""` | CONNECTION LIMIT. Empty: `pollers.ldbws.internalReads.database.maxConnections` + 1. |
| `postgresql.probes.startup.type` | `tcpSocket` | `tcpSocket` or `exec` (`pg_isready`). See [Postgres and Redis probes](#postgres-and-redis-probes). |
| `postgresql.probes.startup.periodSeconds` | `10` | Startup probe period. Liveness and readiness start only after it succeeds. |
| `postgresql.probes.startup.timeoutSeconds` | `5` | Startup probe timeout. |
| `postgresql.probes.startup.failureThreshold` | `90` | Startup probe failures allowed (90 x 10s = 15 minutes of crash recovery). |
| `postgresql.probes.readiness.type` | `tcpSocket` | `tcpSocket` or `exec` (`pg_isready`). TCP turns ready slightly earlier during crash recovery. |
| `postgresql.probes.readiness.periodSeconds` | `10` | Readiness probe period (5 before tcpSocket became the default). |
| `postgresql.probes.readiness.timeoutSeconds` | `5` | Readiness probe timeout. |
| `postgresql.probes.readiness.failureThreshold` | `5` | Readiness probe failures allowed (5 x 10s = 50s). |
| `postgresql.probes.liveness.type` | `tcpSocket` | `tcpSocket` or `exec` (`pg_isready`). TCP never kills Postgres mid-WAL-redo. |
| `postgresql.probes.liveness.periodSeconds` | `30` | Liveness probe period. |
| `postgresql.probes.liveness.timeoutSeconds` | `5` | Liveness probe timeout. |
| `postgresql.probes.liveness.failureThreshold` | `6` | Liveness probe failures allowed (6 x 30s = 3 minutes). |
| `postgresql.persistence.enabled` | `true` | Attach a PVC. When false an emptyDir is used and data is lost on reschedule. |
| `postgresql.persistence.size` | `20Gi` | Requested volume size. |
| `postgresql.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `postgresql.persistence.accessModes` | `[ReadWriteOnce]` | PVC access modes. |
| `postgresql.persistence.existingClaim` | `""` | Use a pre-existing PVC instead of a `volumeClaimTemplates` entry. |
| `postgresql.extraEnv` | `[]` | Extra container env vars. One named like a chart-set var replaces it. Server settings go in `postgresql.config`. |
| `postgresql.config` | see below | `postgresql.conf` settings, rendered as `-c name=value` args. Sized for the default 5Gi limit. A `null` key falls back to the Postgres default. Changing it restarts Postgres. |
| `postgresql.shm.enabled` | `true` | Mount a memory-backed emptyDir at `/dev/shm` (the runtime default is 64MiB). |
| `postgresql.shm.sizeLimit` | `1Gi` | Size of `/dev/shm`. Empty means unbounded apart from the memory limit. |
| `postgresql.podSecurityContext` | `{}` | Merged over the pod securityContext. uid/gid/fsGroup 999 are pinned by default and required by this image. |
| `postgresql.resources` | requests `250m`/`3Gi`, limit `5Gi` | Container resource requests/limits. The `config` and `shm` defaults assume the 5Gi limit. |
| `postgresql.nodeSelector` | `{}` | Pod node selector. |
| `postgresql.tolerations` | `[]` | Pod tolerations. |
| `postgresql.affinity` | `{}` | Pod affinity rules. |
| `postgresql.podAnnotations` | `{}` | Pod annotations. |
| `postgresql.terminationGracePeriodSeconds` | `""` | Seconds Kubernetes waits after the stop signal (a fast shutdown) before SIGKILL. Empty: unset (Kubernetes' 30s) with pgBackRest off; with it on, 6 x `pgbackrest.archive.pushTimeoutSecs` + 60 (240), since a stop while S3 is unreachable waits for the archiver. |

#### Postgres memory and checkpoint tuning

`postgresql.config` is merged key by key over these defaults, so an
override only needs to name the keys it changes:

| Setting | Chart default | Postgres default | Why |
|---|---|---|---|
| `shared_buffers` | `1GB` | `128MB` | About 20% of the 5Gi limit. |
| `effective_cache_size` | `3584MB` | `4GB` | Planner hint only, about 70% of the limit. |
| `work_mem` | `16MB` | `4MB` | Per sort/hash node. Hash nodes may use twice this (`hash_mem_multiplier`). |
| `maintenance_work_mem` | `256MB` | `64MB` | Manual VACUUM, CREATE INDEX. |
| `autovacuum_work_mem` | `128MB` | `-1` (= `maintenance_work_mem`) | Per autovacuum worker (3). Decoupled so maintenance bumps don't triple. |
| `wal_buffers` | `32MB` | `-1` (shared_buffers/32, max 16MB) | Bulk schedule publishes fill small WAL buffers. |
| `max_wal_size` | `4GB` | `1GB` | Fewer size-triggered checkpoints during bulk writes. Needs up to this much `pg_wal` disk. |
| `min_wal_size` | `1GB` | `80MB` | Keeps WAL segments recycled rather than deleted and recreated. |
| `checkpoint_timeout` | `15min` | `5min` | Fewer full-page images. Crash recovery takes longer. |
| `wal_compression` | `lz4` | `off` | Compresses the full-page images written after each checkpoint. Needs Postgres 15+ built with lz4 (the chart's `postgres:18` image is). |
| `random_page_cost` | `1.1` | `4` | **Assumes SSD-class storage.** Set `"4"` on spinning disks. |
| `max_connections` | `200` | `100` | Production's value. The service pools, migrations, backups and psql sessions outgrew 100; the render-time connection budget checks sum against it. Each backend can use `work_mem` per sort/hash node, so check memory before raising it. |
| `huge_pages` | `off` | `try` | The pod requests no hugepages. `try` can SIGBUS on nodes where hugepages exist but aren't granted to the pod. |
| `shared_preload_libraries` | `pg_stat_statements` | `""` | Per-query statistics. Only loads at server start. The extension is created by migration `20260927070000`. |
| `pg_stat_statements.track` | `top` | `top` | Top-level statements only. Pinned explicitly. |
| `log_min_duration_statement` | `1s` | `-1` | Logs statements taking 1s or more. Expect one line per schedule-publish bulk batch. |
| `log_parameter_max_length` | `0` | `-1` | Never log bind parameters: bulk arrays of ~250k rows, user data, session tokens. |
| `log_lock_waits` | `on` | `off` | Logs lock waits longer than `deadlock_timeout` (1s). |
| `track_io_timing` | `on` | `off` | I/O timings in `EXPLAIN (ANALYZE, BUFFERS)`, `pg_stat_statements` and `pg_stat_database`. |
| `idle_in_transaction_session_timeout` | `10min` | `0` | Backstop for sessions that skip `common::pg` (psql, exporters): ends one left idle inside an open transaction. The pools set 30s; pg_dump sets its own 0. |
| `tcp_keepalives_idle` / `_interval` / `_count` | `60` / `10` / `6` | `0` (OS: 2h) | Detects a client that vanished without closing its socket. Same values every pool sets. |
| `client_connection_check_interval` | `10s` | `0` | A running query checks its socket and aborts once the client is gone. |

`statement_timeout` is deliberately not set server-wide. pgBackRest's
`pg_backup_stop()` waits for WAL archiving, which takes minutes while object
storage is down. Manual VACUUM, CREATE INDEX and the api's migration
connection also run long. Every service pool sets its own 60s.

`random_page_cost` defaults to `"1.1"`, which assumes SSD-class storage
(SSD/NVMe, or network block storage backed by it, as most managed
StorageClasses are). On spinning disks set it back to `"4"`, Postgres's
own default. If you change `resources.limits.memory`, scale
`shared_buffers` (~20-25%), `effective_cache_size` (~70%) and
`postgresql.shm.sizeLimit` with it. If you raise
`maintenance_work_mem` or `work_mem`, raise `postgresql.shm.sizeLimit` too:
a parallel VACUUM keeps its whole dead-tuple array (up to
`maintenance_work_mem`) in `/dev/shm`. With the runtime default of 64MiB,
that fails with `could not resize shared memory segment ... No space left
on device`. Memory used in `/dev/shm` counts against the container's
memory limit.

The settings are passed on the command line, so they override both
`postgresql.conf` and `ALTER SYSTEM`. They are part of the pod template,
so changing any of them rolls the StatefulSet (`RollingUpdate`, one pod).
Postgres restarts, which `shared_buffers`, `wal_buffers` and `huge_pages`
need anyway. Expect a short outage. Postgres stops cleanly (the image's
SIGINT stop signal requests a fast shutdown), then starts with a cold
buffer cache. The pod template carries only labels that stay the same
across releases (no `helm.sh/chart` or `app.kubernetes.io/version`), so a
chart upgrade that changes none of these settings (and not the Postgres
image or the rest of the pod spec) leaves Postgres running. Because the
settings are in the pod template itself, no config checksum annotation is
needed.

#### Observability settings (2026-09-27)

`shared_preload_libraries` only takes effect when Postgres restarts. Any
change to `postgresql.config` rolls the StatefulSet, so the upgrade that
adds it restarts Postgres. An override that sets its own `postgresql.config`
keeps these defaults unless it names the same keys, because the keys merge.
On an external database, set the same parameters in its own configuration.
The migration creates the extension only if the app role is allowed to, and
logs a warning otherwise. In that case, run
`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` as a privileged role.

#### Connection pools and timeouts

Every pool (api, aggregator, notifier, enricher) sets these at connect time
(`crates/common/src/pg.rs`):

- `application_name`: `distant-signal-api`, `-aggregator`, `-notifier`,
  `-enricher`, or `-api-migrations`.
- `statement_timeout`, from `databasePool.statementTimeoutSecs` (60).
- `idle_in_transaction_session_timeout`, from
  `databasePool.idleInTransactionTimeoutSecs` (30).

A pool acquire fails after `databasePool.acquireTimeoutSecs` (5).

Some work is expected to run longer and raises `statement_timeout` for its
own transaction with `SET LOCAL`:

| Work | `statement_timeout` |
|---|---|
| Schedule publish chunks | 120s |
| Final-chunk delete phase | 120s |
| Retention prunes | 600s |
| Archive batches | 600s, plus 15min idle-in-transaction |

api migrates at startup on its own connection, with
`api.migrations.lockTimeoutSecs` (10) and
`api.migrations.statementTimeoutSecs` (240, well below the 900s startup-probe
budget). Before it migrates, it drops any INVALID index that a failed
`CREATE INDEX CONCURRENTLY` left behind.

| Value | Default | Description |
|---|---|---|
| `databasePool.statementTimeoutSecs` | `60` | `statement_timeout` for every pooled connection. `0` disables it. |
| `databasePool.idleInTransactionTimeoutSecs` | `30` | `idle_in_transaction_session_timeout`. `0` disables it. |
| `databasePool.acquireTimeoutSecs` | `5` | How long to wait for a free pool connection. |
| `api.database.maxConnections` | `50` | api pool size per replica. Pools total api 50 per replica + aggregator 10 + notifier 5 + enricher 5 = 70 at one api replica, plus every poller and consumer pool, against the chart's `postgresql.config.max_connections` of 200. Each extra api replica (and a RollingUpdate surge pod) adds 50; the render fails when the total no longer fits, so raise `postgresql.config.max_connections` (restart; check memory) or lower this. |
| `api.migrations.lockTimeoutSecs` | `10` | `lock_timeout` for startup migrations. |
| `api.migrations.statementTimeoutSecs` | `240` | `statement_timeout` for each startup migration statement. Keep it below the startup probe budget. |

#### pgBackRest (point-in-time recovery)

Off by default. See [Point-in-time recovery (optional)](#point-in-time-recovery-optional)
and [docs/postgres-pitr.md](../../docs/postgres-pitr.md).

| Key | Default | Description |
|---|---|---|
| `postgresql.pgbackrest.enabled` | `false` | Turn on WAL archiving, backups and checks with pgBackRest. Needs `postgresql.enabled`. Turning it on or off restarts Postgres once. |
| `postgresql.pgbackrest.image.repository` | `""` | **Required when enabled**: a Postgres-plus-pgBackRest image: the `postgresql.image` Postgres with pgBackRest, tini as PID 1 and `/usr/local/bin/pgbackrest-daily-check` (e.g. lucy/postgres-pgbackrest). No default: this chart doesn't build one (docs/postgres-pitr.md). |
| `postgresql.pgbackrest.image.tag` | `""` | **Required when enabled**: the stable tag with its digest, e.g. `pg18.6-pgbackrest2.59.1-tini0.19.0@sha256:…` (only `-tini` tags: see docs/postgres-pitr.md, "Why tini"). Never falls back to `appVersion`: a per-release image would restart Postgres on every deploy. |
| `postgresql.pgbackrest.image.digest` | `""` | Content digest, used instead of `tag` when set. |
| `postgresql.pgbackrest.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `postgresql.pgbackrest.stanza` | `ds` | pgBackRest stanza name. |
| `postgresql.pgbackrest.processMax` | `2` | Parallel processes for async archive-push, backup and restore. |
| `postgresql.pgbackrest.repo.path` | `""` | **Required when enabled**: absolute repository path in the bucket, e.g. `/mine-bringer/pgbackrest/distant-signal`. `expire` deletes under it only. |
| `postgresql.pgbackrest.repo.retentionFullType` | `time` | `time`: keep what's needed to restore to any point in the last `retentionFull` days. `count`: keep that many full backups. |
| `postgresql.pgbackrest.repo.retentionFull` | `7` | Days (or full backups). Backups hold personal data until they expire, so keep it short. |
| `postgresql.pgbackrest.repo.bundle` | `true` | Bundle small files into fewer repository objects. |
| `postgresql.pgbackrest.repo.s3.endpoint` | `""` | **Required when enabled**: S3 endpoint host name, no scheme. Also the TLS name that is verified. |
| `postgresql.pgbackrest.repo.s3.port` | `443` | Endpoint port. |
| `postgresql.pgbackrest.repo.s3.storageHost` | `""` | Connect to this host instead of `endpoint`, which is still used for TLS and signing. |
| `postgresql.pgbackrest.repo.s3.bucket` | `""` | **Required when enabled**: bucket name. |
| `postgresql.pgbackrest.repo.s3.region` | `us-east-1` | Region requests are signed for. |
| `postgresql.pgbackrest.repo.s3.uriStyle` | `path` | `path` or `host` (virtual-hosted) addressing. |
| `postgresql.pgbackrest.repo.s3.verifyTls` | `true` | Verify the endpoint's certificate. |
| `postgresql.pgbackrest.repo.s3.existingSecret` | `""` | **Required when enabled**: existing Secret with the S3 key pair (and, by default, the cipher passphrase). |
| `postgresql.pgbackrest.repo.s3.accessKeyIdKey` / `.secretAccessKeyKey` | `access-key-id` / `secret-access-key` | Keys within `repo.s3.existingSecret`. |
| `postgresql.pgbackrest.repo.cipher.type` | `aes-256-cbc` | Client-side repository encryption. Can't be empty. |
| `postgresql.pgbackrest.repo.cipher.existingSecret` | `""` | Existing Secret with the cipher passphrase. Empty means `repo.s3.existingSecret`. Keep an offline copy of the passphrase. |
| `postgresql.pgbackrest.repo.cipher.passphraseKey` | `cipher-pass` | Key within that Secret. |
| `postgresql.pgbackrest.archive.async` | `true` | Asynchronous, parallel archive-push through a spool on the data volume. |
| `postgresql.pgbackrest.archive.queueMax` | `8GB` | Disk-full guard (`archive-push-queue-max`): past this much queued WAL, pgBackRest drops WAL (a PITR gap the daily check reports) instead of filling the disk. Base-1024 units. Needs `async`; `""` turns it off. |
| `postgresql.pgbackrest.archive.timeoutSecs` | `60` | Postgres `archive_timeout`: bounds the recovery point objective while anything writes. |
| `postgresql.pgbackrest.archive.pushTimeoutSecs` | `30` | pgBackRest `archive-timeout` for `archive_command` only (backup and check keep 60s): how long each archive-push waits for the async worker. Sets the default grace period and `PGCTLTIMEOUT` (6 x this + 60). Changing it restarts Postgres. |
| `postgresql.pgbackrest.compress.type` / `.level` | `zst` / `3` | Repository compression. |
| `postgresql.pgbackrest.backup.timeZone` | `Etc/UTC` | `spec.timeZone` of the CronJobs. |
| `postgresql.pgbackrest.backup.fullSchedule` | `0 5 * * 0` | Weekly full backup (Sunday 05:00 UTC). |
| `postgresql.pgbackrest.backup.diffSchedule` | `0 5 * * 1-6` | Differential backup the other days (05:00 UTC). |
| `postgresql.pgbackrest.backup.checkSchedule` | `0 7 * * *` | Daily `check` and WAL gap check (07:00 UTC, after the backup). |
| `postgresql.pgbackrest.backup.verifySchedule` | `0 8 * * 0` | Weekly `pgbackrest verify` CronJob (Sunday 08:00 UTC, after the full backup). It reads the whole repository back. Empty renders no verify CronJob. |
| `postgresql.pgbackrest.backup.startFast` | `true` | Pass `--start-fast` to the full and diff backups, so `pg_backup_start` forces an immediate checkpoint. Off, it waits for the timed checkpoint under way and then a new spread one: 13.5-27 minutes as an `active` superuser query, which trips a long-running-query alert. Costs one burst of up to `shared_buffers` of writes at 05:00 UTC. |
| `postgresql.pgbackrest.backup.suspend` | `false` | Suspend the CronJobs (archiving continues). |
| `postgresql.pgbackrest.backup.activeDeadlineSeconds` | `21600` | Kill a backup or check Job that runs longer. |
| `postgresql.pgbackrest.backup.backoffLimit` | `1` | Retries of a failed Job. A retried backup resumes. |
| `postgresql.pgbackrest.backup.startingDeadlineSeconds` | `3600` | Skip a run that can't start within this long of its time. |
| `postgresql.pgbackrest.backup.ttlSecondsAfterFinished` | `259200` | The Jobs' `ttlSecondsAfterFinished`: a finished Job and its pod are deleted three days after it ends (longer than the alerts' `recentJobWindow`). null leaves them to the history limits. |
| `postgresql.pgbackrest.backup.image.repository` / `.tag` / `.pullPolicy` | `registry.k8s.io/kubectl`, `v1.36.5@sha256:…`, `IfNotPresent` | The image the CronJobs run `kubectl exec` from. Keep it within one minor version of the cluster. |
| `postgresql.pgbackrest.backup.resources` | requests `20m`/`64Mi`, limit `128Mi` | CronJob pod resources. The work happens in the Postgres container. |
| `postgresql.pgbackrest.backup.podSecurityContext` | `{}` | Merged over the CronJob pods' securityContext (non-root uid 65532 by default). |
| `metrics.prometheusRule.ingestWriterDown` | see `values.yaml` | `DistantSignalIngestWriterDown`: `enabled`, `for` (5m), `severity` (critical). Renders only with `ingestWriter.enabled`. |
| `metrics.prometheusRule.ingestStreamBacklog` / `ingestStreamStalled` / `ingestDeadLetters` / `ingestDeadLetterExpiring` / `ingestUnsupportedSchema` / `ingestProducerXaddFailing` / `ingestStreamMemoryHigh` / `ingestClockSkew` | see `values.yaml` | The ingest stream alerts (ingest spec §14.2): `DistantSignalIngestStreamBacklog` (pending over 600s or 0.5 of MAXLEN: warning; 0.8: critical, which replaces the warning once it fires; `maxlen` per stream, kept in step with `crates/ingest-stream/src/budget.rs`), `…StreamStalled` (XADDs succeed but nothing applied for `stallAfterSecs` per stream; critical), `…DeadLetters` (any in 15m), `…DeadLetterExpiring` (within 4h of the 7-day trim), `…UnsupportedSchema` (critical), `…ProducerXaddFailing` (every XADD failed for 10m; critical), `…StreamMemoryHigh` (over 0.75 of 576 MiB), `…ClockSkew` (an observed time clamped, D13). (`…ShadowMismatch` went with the producers' `http+shadow` sink in release B.) Each has `enabled`, `for`, `severity`. Render only with `ingestWriter.enabled`, and silent until a stream is on. Runbooks: `docs/alerts.md`, `docs/ingest-streams-deadletter.md`. |
| `metrics.prometheusRule.writerLoopStale` / `.writerLoopUnowned` | see `values.yaml` | `DistantSignalWriterLoopStale`: a loop's newest success (`loop_last_success_timestamp_seconds`) older than `maxAgeSeconds` (1800; `maxAgeSecondsByCycle` per loop, `excludeCycles` [canary]), for 10m. `DistantSignalWriterLoopUnowned`: no process holds a loop's lock (`sum by (loop) (loop_lock_held) < 1`) for 15m (`forByLoop` per loop). Each has `enabled`, `for`, `severity`; render only with `ingestWriter.enabled` and `ingestWriter.loops.enabled`. |
| `metrics.prometheusRule.ingestApplyWritesNothing` | see `values.yaml` | `DistantSignalIngestApplyWritesNothing`: over `window` (15m) the writer applied entries of a stream but wrote no row (`ingest_stream_row_writes_total{outcome="written"}` flat), for 5m; `excludeStreams` (the daily `ds:ingest:reference`). Renders only with `ingestWriter.enabled`. |
| `metrics.prometheusRule.ingestSourceStale` | see `values.yaml` | `DistantSignalIngestSourceStale`: an `ingest_freshness` row (`ingest_freshness_timestamp_seconds{source}`, read by the writer every 60s) older than its `maxAgeSeconds` entry (incidents and tfl 900, stations and tocs 172800, the stream-only snapshot sources 1800, the island-of-Ireland catalogues' per-network sources 172800, a transit network's timetable 129600 and situations 1800), for 10m; a source in `whileStreamApplies` only while the writer applies that stream. Renders only with `ingestWriter.enabled`. |
| `metrics.prometheusRule.dbPoolAcquireTimeouts` / `.directWritesFailing` | see `values.yaml` | `DistantSignalDbPoolAcquireTimeouts`: a component's pool timed out an acquire (`db_pool_acquire_timeouts_total`) within `window` (10m). `DistantSignalDirectWritesFailing`: a direct writer's writes of one operation (`db_writes_total`) all failed (timeout, transient, rejected; no ok) over `window` (15m), for 30m. Each has `enabled`, `for`, `severity`. |
| `metrics.prometheusRule.incidentSnapshotsMissing` | see `values.yaml` | `DistantSignalIncidentSnapshotsMissing`: no incidents snapshot reached the database (`store_incident_removal_inference_total`, any outcome, from poller-incidents; `api_` before ingest phase 5) within `window` (2h), for 15m. Only with `pollers.incidents.enabled`. |
| `metrics.prometheusRule.scheduleFeedMarkerStale` | see `values.yaml` | `DistantSignalScheduleFeedMarkerStale`: the newest recorded CIF delivery (`schedule_feed_last_ingest_delivered_at_seconds`) older than `maxAgeSeconds` (108000, 30h), for 15m. Only with `scheduleFeed.enabled`. |
| `metrics.prometheusRule.trainEventOutbox` | see `values.yaml` | `DistantSignalTrainEventOutboxRejected`: `rejected.window` (1h), `rejected.severity` (warning); `DistantSignalTrainEventOutboxStuck`: `stuck.maxAgeSeconds` (120), `stuck.for` (1m), `stuck.severity` (critical); and `enabled`. Render only with `ingestWriter.enabled`. |
| `metrics.prometheusRule.postgresDown` | see `values.yaml` | `DistantSignalPostgresDown`: `enabled`, `for` (3m), `severity` (critical) and `pgUpSelector`, extra label matchers for postgres_exporter's `pg_up` (see [Alerts](#alerts)). |
| `metrics.prometheusRule.statefulImagePull` | see `values.yaml` | `DistantSignalStatefulImagePullFailing`: `enabled`, `for` (5m), `postgresSeverity` and `redisSeverity` (both critical). One rule per bundled component (`postgresql.enabled` / `redis.enabled`). |
| `metrics.prometheusRule.apiDatabaseDown` | see `values.yaml` | `DistantSignalApiDatabaseDown`: `enabled`, `for` (2m) and `severity` (critical). |
| `metrics.prometheusRule.schemaGateWaiting` | see `values.yaml` | `DistantSignalSchemaGateWaiting`: `enabled`, `for` (10m) and `severity` (critical). |
| `metrics.prometheusRule.consumerApiErrors` | see `values.yaml` | `DistantSignalConsumerApiCallsFailing`: `enabled`, `window` (5m), `minErrors` (3), `for` (10m) and `severity` (warning). |
| `metrics.prometheusRule.api5xx` | see `values.yaml` | `DistantSignalApiPublic5xx`: `enabled`, `window` (5m), `routeLabel` (`exported_endpoint`: the route label as Prometheus stores it behind the PodMonitor), and under `public` `ratio` (0.05), `minErrors` (3), `for` (10m) and `severity` (warning). (The `ingest` scope went with `/private` in release B.) |
| `metrics.prometheusRule.cycleStalled` | see `values.yaml` | `DistantSignalAggregatorCycleFailing` / `DistantSignalNotifierCycleFailing`: `enabled`, `maxAgeSeconds` (900), `for` (0m) and `severity` (warning). |
| `metrics.prometheusRule.pgbackrest` | see `values.yaml` | The pgBackRest alerts' windows, ages, `archiverSelector` and severity (see [Alerts](#alerts)). |
| `metrics.prometheusRule.scheduledJobs` | see `values.yaml` | The scheduled-job alerts (`<fullname>-scheduled-jobs` PrometheusRule): `enabled`, `recentJobWindow` (26h), `intervalMultiple` (1.5), `minAgeSeconds` (1800), `staleFor` (10m), `pendingFor` (15m) and `severity` (see [Alerts](#alerts) and `docs/scheduled-jobs.md`). |

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
| `api.image.repository` | `ghcr.io/fasterspeeding/distant-signal/api` | api image repository. |
| `api.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `api.image.digest` | `""` | Exact content digest (`sha256:...`). When set, takes priority over `tag`/appVersion -- see "Pinning by content digest instead of tag" above. CI populates this automatically for images it builds and pushes. |
| `api.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `api.replicaCount` | `1` | Replicas. >1 is safe for migrations — sqlx's Migrator takes a Postgres advisory lock — but each replica adds `api.database.maxConnections` (50) connections: see that row before scaling. |
| `api.strategy.type` | `Recreate` | Rollout strategy: `Recreate` (a short outage per deploy) or `RollingUpdate` (`maxSurge: 1`, `maxUnavailable: 0`), which the render refuses unless `migrate.job.enabled` and `api.migrateOnStartup` is `false`. The surge pod's pool counts in the connection budget. |
| `api.minReadySeconds` | `10` | Seconds a new pod must stay Ready before a `RollingUpdate` counts it available and stops an old one. Rendered only with `RollingUpdate`. |
| `api.shutdown.drainSecs` | `20` | `API_SHUTDOWN_DRAIN_SECS` (1-3600): after SIGTERM the api stops accepting, lets in-flight requests finish and stops its background loops (releasing their advisory locks) for up to this long, then exits. |
| `api.shutdown.preStopSleepSecs` | `5` | The `preStop` `sleep` before SIGTERM, so the pod leaves the Service's endpoints before it stops accepting. `0` drops the hook. |
| `api.terminationGracePeriodSeconds` | `35` | The pod's grace period. The render fails unless it is at least `preStopSleepSecs` + `drainSecs` + 5. |
| `api.readiness.checkDatabase` | `false` | `API_READINESS_CHECKS_DB`: `/public/ready` also answers 503 while the request pool cannot run `SELECT 1` within 2 s (checked at most every 5 s). Opt-in, and refused below `api.replicaCount: 2`: with one replica NotReady empties the Service, so a database blip would become a full outage. |
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
| `api.internalOauth.clientId` | `""` | **Required.** Expected `aud` claim on a verified token (the internal provider's client id; must differ from `api.sso.clientId`). |
| `api.internalOauth.groups.mcp` | `srv-ds-mcp` | Authentik group of the Distant-Signal-MCP's service account, the only internal-token use left since release B removed `/private`: it only moves the MCP's public requests onto `api.rateLimit.mcp`. Empty turns that off. See "Distant-Signal-MCP as a service caller" below. |
| `api.delegated.clients` | `[]` | Delegated access ([docs/connected-apps.md](../../docs/connected-apps.md)): the registered clients, sent to the api as `DELEGATED_CLIENTS` JSON. Each has `name`, `displayName`, `issuerUrl`, `audience`, `maxScopes`, `enabled` (default `true`; `false` is the kill switch) and `serviceGroup`. Empty (the default) turns the feature off: no delegated listener, no delegated Service, and every delegated bearer refused. |
| `api.delegated.port` | `8082` | The delegated listener's port, published only by the `<release>-api-delegated` ClusterIP Service and admitted by the NetworkPolicy only from `networkPolicy.apiExtraIngressNamespaces`. The only port that accepts delegated bearers. Must differ from `api.service.port` and `metrics.port`. |
| `api.delegated.dailySaveLimit` / `.dailyDeleteLimit` | `50` / `20` | Journeys one user's client may save, and delete, per rolling 24 hours (429 `daily_limit`). |
| `api.delegated.revokedGrantRetentionDays` | `365` | Days a revoked grant is kept, as the record of when and why an app's access ended, before the hourly maintenance pass (api loop or `api-maintenance` CronJob) deletes it. |
| `api.rateLimit.enabled` | `true` | Master switch for the per-client limits on login, `/Trips/plan`, `/Train/by-uid/*` and public writes (`crates/api/src/rate_limit.rs`). |
| `api.rateLimit.trustXRealIp` | `true` | Key anonymous clients on the frontend proxy's `X-Real-IP`. Safe only while `ingress.api.enabled` is off. |
| `api.rateLimit.login.perMinute` / `.burst` | `10` / `20` | Per client IP, `/public/auth/login` and `/public/auth/callback`. |
| `api.rateLimit.tripPlan.perMinute` / `.burst` | `20` / `10` | Per client IP, `/Trips/plan`. |
| `api.rateLimit.trainByUid.perMinute` / `.burst` | `120` / `60` | Per client IP, `/Train/by-uid/*`. |
| `api.rateLimit.publicWrite.perMinute` / `.burst` | `120` / `60` | Per client IP, every other non-GET public route. |
| `api.rateLimit.events.perMinute` / `.burst` | `60` / `30` | Per client IP, `POST /public/events` (opt-in usage analytics, [docs/usage-analytics.md](../../docs/usage-analytics.md)); the MCP gets the same. |
| `api.rateLimit.mcp.tripPlan.perMinute` / `.burst` | `100` / `30` | The MCP's own `/Trips/plan` budget: one bucket for the whole service (`svc:mcp`), 5x public. |
| `api.rateLimit.mcp.trainByUid.perMinute` / `.burst` | `600` / `200` | The MCP's own `/Train/by-uid/*` budget. |
| `api.rateLimit.mcp.publicWrite.perMinute` / `.burst` | `600` / `200` | The MCP's own public-write budget. Login has no MCP budget: the bearer is ignored there. |
| `api.rateLimit.delegated.read.perMinute` / `.burst` | `60` / `20` | Per user, for a delegated client's `GET /Journeys/mine` and `GET /Journeys/{id}`. |
| `api.rateLimit.delegated.write.perMinute` / `.burst` | `10` / `5` | Per user, for a delegated client's `POST /Journeys/itinerary`. |
| `api.rateLimit.delegated.delete.perMinute` / `.burst` | `5` / `3` | Per user, for a delegated client's `DELETE /Journeys/{id}`. |
| `api.rateLimit.delegated.clientMultiplier` | `20` | Each delegated client's ceiling across all its users is the per-user values times this. A delegated request must pass both buckets, and never uses the per-IP or MCP ones. |
| `api.probes.path` | `/public/health` | Path the startup and liveness probes and the `helm test` pod hit: 200 whenever the process listens, so a database blip never restarts the pod. |
| `api.probes.readinessPath` | `/public/ready` | Path the readiness probe hits: 503 while the pod is draining, and with `api.readiness.checkDatabase` while the database is unreachable. |
| `api.probes.startup.periodSeconds` | `2` | Startup probe period. |
| `api.probes.startup.failureThreshold` | `450` | Startup probe failures allowed (450 x 2s = 900s for in-process migrations, matching the Postgres startupProbe's 15 minutes). |
| `api.probes.startup.timeoutSeconds` | `3` | Startup probe timeout. |
| `api.probes.readiness.periodSeconds` | `10` | Readiness probe period. |
| `api.probes.readiness.failureThreshold` | `3` | Readiness probe failures allowed. |
| `api.probes.readiness.timeoutSeconds` | `3` | Readiness probe timeout. |
| `api.probes.liveness.periodSeconds` | `10` | Liveness probe period. |
| `api.probes.liveness.failureThreshold` | `3` | Liveness probe failures allowed. |
| `api.probes.liveness.timeoutSeconds` | `3` | Liveness probe timeout. |
| `api.reconciliationSweepIntervalSecs` | `300` | How often the reconciliation sweep retries fixing a stuck tracked-train row. |
| `api.scheduleEnrichmentGraceMinutes` | `30` | How long past a train's origin departure the reconciliation sweep waits before trying a schedule-only match. |
| `api.backlogMatchSweepIntervalSecs` | `300` | How often still-pending pins are matched against the TRUST event backlog. |
| `api.sessionCleanupIntervalSecs` | `3600` | How often expired sessions are deleted; the personal-data retention limits below run on the same sweep. |
| `api.backgroundLoops` | `true` | Run the api's background loops, as today: the schedule-match, reconciliation and backlog-match sweeps, the CORPUS crosswalk check at startup, and the session cleanup. The first four take the ingest-writer's advisory locks, on one of the api pool's own connections. `false` renders `API_BACKGROUND_LOOPS=false`: set it a release after `ingestWriter.loops.enabled`, with `apiMaintenance.enabled`. See [Migrations, maintenance and the ingest-writer](#migrations-maintenance-and-the-ingest-writer-optional). |
| `api.pastTravelRetentionDays` | `548` | Days after the travel date that tracked trains, tickets, journeys and template skip markers are kept (18 months). `0` disables. See `docs/personal-data-retention.md`. |
| `api.stalePushSubscriptionDays` | `365` | Drop push subscriptions whose user has not logged in (and that were not renewed) for this many days. `0` disables. |
| `api.inactiveAccountRetentionDays` | `0` | Delete accounts with no login and no live session for this many days. Off by default: the app holds no email addresses, so users cannot be warned first, and enabling it is an operator decision the privacy notice must state. |
| `api.transitNetworks` | `[]` | The transit networks `/public/transit` serves (`API_TRANSIT_NETWORKS`). Empty: every `/public/transit` route answers 404 and the frontend hides its pages. List a network (`[metrolink]`, `[metrolink, net]`) only after its timetable has been applied for a day. |
| `api.adminGroup` | `""` | SSO group whose members may end any user's sessions (`POST /api/admin/users/revoke-sessions`). Empty: nobody is an admin. Read at login. See `docs/session-revocation.md`. |
| `api.oidcStoredGroupsExtra` | `""` | Extra IdP groups kept on the user at login besides `adminGroup`; every other group is dropped. Comma-separated; `""` keeps none. Nothing reads extra groups today: distant-signal-mcp takes its groups straight from Authentik. |
| `api.timeouts.requestTimeoutSecs` | `30` | Public requests still running after this get a 408. |
| `api.timeouts.headerReadTimeoutSecs` | `10` | Disconnect an HTTP/1 client that has not sent its full headers within this. |
| `api.timeouts.unavailableRetryAfterSecs` | `30` | `Retry-After` (seconds, 1-3600) on a 503. A route that cannot reach the database answers `503 {"error":"service_unavailable","retryable":true}` instead of a 500. |
| `api.corpusFallback.enabled` | `false` | Use Network Rail CORPUS as a fallback for TIPLOC/STANOX→CRS lookups the timetable has no CRS for; the timetable always wins a conflict. Does nothing until CORPUS is loaded (`scheduleFeed.corpus.enabled`). Review `corpus_compare` (in the api image) first. |
| `api.scheduleProvisionalAfterDays` | `7` | Days after today a service date's timetable counts as firm (`SCHEDULE_PROVISIONAL_AFTER_DAYS`, 0-60). Train search, `/Train/by-uid` and `/Trips/plan` report a later date as `provisional: true`, because late STP changes (engineering works) can still land. |
| `api.tripPlanGraphCache.dates` | `2` | Service dates whose connections graph `/Trips/plan` keeps built (about 27.5 MB each for rail, 44 MB with Metrolink and `tfl`; the prewarm keeps both built). The interned stop and trip codes are never freed: watch `api_trip_plan_interned_codes` level off. `0` disables the cache. |
| `api.tripPlanGraphCache.maxAgeSecs` | `21600` | Rebuild a cached graph after this long even with no new schedule publish or transit feed (both rebuild it at once): a safety net for a publish cycle that failed a product and wrote no marker, and for reference data no marker covers. `600` until DS-225. |
| `api.tripPlanGraphCache.prewarmIntervalSecs` | `60` | How often the api checks today's and tomorrow's graphs and builds any that are missing or stale, before a request has to (one at a time, only with a planning slot free, never evicting a graph used in the last 15 minutes). `0` turns it off. |
| `api.tripPlanGraphCache.snapshot` | `false` | DS-235: build one timetable snapshot per schedule publish or transit feed in the prewarm loop (a minute or so of reads, one api process at a time under an advisory lock), store it in `planner_snapshots` (gzip JSON, tens of MB; the newest two kept) and build every date's graph from it instead of a ~490k-row read. Rebuilt after `maxAgeSecs`. Needs `prewarmIntervalSecs` above 0. |
| `api.tripPlanMaxWaypoints` | `20` | Most `?waypoints=` one `/Trips/plan` request may name (clamped to 1-20). |
| `api.tripPlanRoadWaterChangeMinutes` | `5` | Extra minutes `/Trips/plan` adds to a change on each bus or ferry side of it (clamped to 0-30): bus-to-train +N, bus-to-bus +2N, never at the start or end of the journey. |
| `api.tripPlanTransitNetworks` | `[]` | Transit networks whose trams `/Trips/plan` plans with (`API_TRIP_PLAN_TRANSIT_NETWORKS`). Empty: the graph, its cache key and every response are as before. Listed, the tram and metro trips join each cached day graph (about 5 MB a date for Metrolink, 8 MB for `tfl`) and a request plans with them when it sends `transit=true` or names a tram stop (a TfL stop only with `transit=true`). `metrolink` or `tfl`; the render fails on anything else. Rollout: docs/transit-networks.md, "Trip planning". |
| `api.tripPlanTransitChangeMinutes` | `2` | Extra minutes `/Trips/plan` adds on the tram side of a rail-to-tram or tram-to-rail change (clamped to 0-15). A change between two trams owes none. |
| `api.tripPlanMaxOptionsSearchSize` | `252` | Largest `results=options` `/Trips/plan` search: `(waypoints + 1) * (2 * vias + 1) * (maxChanges + 2)` (a 400 above it; clamped to 14-504). |
| `api.tripPlanLive.enabled` | `true` | Apply TRUST and Darwin data to `/Trips/plan` for today's and yesterday's service dates: cancelled trains and calls are withdrawn, known delays applied, and the plan re-run. `false` is the kill-switch: every plan is timetable-only, as with `?live=false`. |
| `api.tripPlanLive.maxReplans` | `3` | Extra planning passes per `results=fastest` request when newly read live data changes the plan. |
| `api.tripPlanLive.maxReplansOptions` | `1` | The same for `results=options` (RAPTOR, the expensive pass); 1 keeps the worst case near 1 s. |
| `api.tripPlanLive.trustMaxAgeMinutes` | `30` | Ignore a TRUST delay whose train state was last updated longer ago than this. Actual times and cancellations are always used. |
| `api.tripPlanLive.horizonMinutes` / `.lookbackMinutes` | `180` / `120` | Only legs booked to depart between `lookbackMinutes` ago and `horizonMinutes` ahead get live data. |
| `api.tripPlanLive.maxTrains` | `60` | Most trains whose live data one request reads. |
| `api.tripPlanLive.inSearch` | `true` | Run every search on today's TRUST delays and cancellations (all trains TRUST knows to be cancelled, cut short or running late), so the planner finds the quickest route on live times and never plans a change that is impossible on them. `false`: plan on the timetable first and let only the bounded re-plans see live data. |
| `api.tripPlanLive.snapshotTtlSeconds` | `30` | How long one service date's snapshot of that TRUST state is reused before it is read and built again. |
| `api.journeyBreaks.enabled` | `false` | Kill switch for mid-journey suggestions: the api's break sweep and the notifier's suggestion pushes (docs/journey-breaks.md). Off, the in-app banner and sheet still work. |
| `api.journeyBreaks.intervalSecs` | `60` | Break sweep cadence; each tick holds an advisory lock, so one api replica runs it. |
| `api.journeyBreaks.maxJourneysPerTick` | `200` | Most journeys the sweep evaluates per tick. |
| `api.journeyBreaks.suggestionsMaxAgeSecs` | `120` | Stored suggestions older than this are re-planned when opened. |
| `api.tflArrivals.enabled` | `true` | Live arrivals on `/tfl/stops/{id}` (`GET /public/tfl/stops/{id}/arrivals`), fetched from TfL on demand. Off, that route answers 503. Needs egress to `baseUrl`; the api's internet rule allows it and gains `baseUrl`'s port. |
| `api.tflArrivals.baseUrl` | `https://api.tfl.gov.uk` | TfL Unified API root. |
| `api.tflArrivals.useTflPollerKey` | `true` | Send `pollers.tfl`'s subscription key (its Secret, when `pollers.tfl` is enabled or has an `existingSecret`; `optional`). Without a key the api calls TfL anonymously, at TfL's lower rate limit. |
| `api.tflArrivals.cacheSecs` | `30` | How long one stop's arrivals are served from the cache (one upstream call per stop at a time, across every replica with `sharedCache`). |
| `api.tflArrivals.errorCacheSecs` | `10` | How long a failed TfL fetch is remembered before the next try. |
| `api.tflArrivals.timeoutSecs` | `5` | Per-request timeout for the TfL call. |
| `api.tflArrivals.sharedCache` | `true` | Share each stop's answer between the api's replicas through Postgres (`tfl_arrivals_cache`, an UNLOGGED table): the replica that finds it stale claims a short lease and fetches, the others serve the previous answer (up to `staleSecs` old) or wait for the new one. Off, or with Postgres unreachable, each pod caches on its own. |
| `api.tflArrivals.memoryCacheSecs` | `5` | With `sharedCache`: how long a pod keeps a shared answer in memory before reading the row again (never past the answer's own `cacheSecs`). |
| `api.tflArrivals.staleSecs` | `120` | With `sharedCache`: how old an answer may be and still be served while another replica fetches its replacement. |
| `api.fullCoverageEnabledDefault` | `true` | Treat every catalogued line as `full_coverage_enabled`, whatever its `lines/*.toml` entry says, so TRUST-vs-schedule delay and cancellation data is used everywhere. Set `aggregator.fullCoverageEnabledDefault` to the same value: both services gate on it. |
| `api.malloc.arenaMax` | `"2"` | `MALLOC_ARENA_MAX`: caps glibc's retained per-arena memory. |
| `api.malloc.mmapThreshold` | `"131072"` | `MALLOC_MMAP_THRESHOLD_` in bytes: allocations at least this large are returned to the OS on free. |
| `api.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `api.resources` | requests `200m`/`512Mi`, limit `1Gi` | Container resource requests/limits, sized from production (working set p95 about 300Mi, max 500Mi over 20 days; the resource-tuning study's request, limit twice it). |
| `api.nodeSelector` | `{}` | Pod node selector. |
| `api.tolerations` | `[]` | Pod tolerations. |
| `api.affinity` | `{}` | Pod affinity rules. |
| `api.podAnnotations` | `{}` | Pod annotations. |
| `api.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `api.migrateOnStartup` | `true` | Migrate at startup, as today. `false` needs `migrate.job.enabled`: the api then gets no `MIGRATION_DATABASE_URL` and `API_MIGRATE_ON_STARTUP=false`, and waits at the schema gate (up to 15 minutes) instead of migrating. Keep `true` until the api image has 1B.3. See [Migrations, maintenance and the ingest-writer](#migrations-maintenance-and-the-ingest-writer-optional). |

### migrate

Off by default. See [Migrations, maintenance and the ingest-writer](#migrations-maintenance-and-the-ingest-writer-optional).

| Key | Default | Description |
|---|---|---|
| `migrate.job.enabled` | `false` | Run the migrations in a `pre-upgrade,post-install` hook Job (weight -5). Needs `ds-migrate` (plan 1B.1) in the api image. |
| `migrate.job.command` | `["/usr/local/bin/ds-migrate"]` | Command, in the api image. |
| `migrate.job.args` | `["run"]` | Arguments to `command`. |
| `migrate.job.backoffLimit` | `1` | Retries after a failed run. |
| `migrate.job.connectDeadlineSecs` | `120` | `MIGRATION_CONNECT_DEADLINE_SECS`: how long `ds-migrate run` retries its first connection (a saturated node, a late NetworkPolicy) before failing. At least 1. |
| `migrate.job.activeDeadlineSeconds` | `900` | Deadline for the whole Job, the Postgres wait included. The HelmRelease `timeout` must cover it. |
| `migrate.job.logLevel` | `info` | `RUST_LOG`. |
| `migrate.job.resources` | requests `10m`/`64Mi`, limit `512Mi` | The migrate container's resources (keeps the hook pod out of BestEffort; the wait-for-postgres init container has fixed `10m`/`16Mi`, limit `64Mi`). |
| `migrate.job.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `migrate.job.nodeSelector` | `{}` | Node selector. |
| `migrate.job.tolerations` | `[]` | Tolerations. |

### apiMaintenance

Off by default. See [Migrations, maintenance and the ingest-writer](#migrations-maintenance-and-the-ingest-writer-optional).

| Key | Default | Description |
|---|---|---|
| `apiMaintenance.enabled` | `false` | Run the api's user-data sweeps (session cleanup, dead links, personal-data retention) as an hourly CronJob. Needs the `maintenance` binary (plan 1B.8) in the api image. |
| `apiMaintenance.schedule` | `"17 * * * *"` | Cron schedule. |
| `apiMaintenance.timeZone` | `Etc/UTC` | The schedule's time zone. |
| `apiMaintenance.command` | `["/usr/local/bin/maintenance"]` | Command, in the api image: one pass, then exit. |
| `apiMaintenance.args` | `[]` | Arguments to `command`. |
| `apiMaintenance.database.maxConnections` | `2` | Its Postgres pool; counted in the connection budgets. |
| `apiMaintenance.database.connectDeadlineSecs` | `120` | `MAINTENANCE_CONNECT_DEADLINE_SECS`: how long a run retries its first connection (a saturated node, a late NetworkPolicy) before failing. At least 1. `databasePool.acquireTimeoutSecs` is unchanged. |
| `apiMaintenance.startingDeadlineSeconds` | `600` | A run missed by more than this is skipped. |
| `apiMaintenance.backoffLimit` | `3` | Retries after a failed run (every step is idempotent). |
| `apiMaintenance.activeDeadlineSeconds` | `1800` | Deadline per run. |
| `apiMaintenance.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `apiMaintenance.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `apiMaintenance.ttlSecondsAfterFinished` | `86400` | The Jobs' `ttlSecondsAfterFinished`: a finished run's Job and pod are deleted a day after it ends, even within the history limits. null leaves them to the history limits. The Helm hook Jobs (migrate, roles setup) have none: their hook delete policy handles them. |
| `apiMaintenance.logLevel` | `info` | `RUST_LOG`. |
| `apiMaintenance.resources` | requests `10m`/`32Mi`/ephemeral `16Mi`, limits `128Mi`/ephemeral `256Mi` | Container resources. Never empty: a BestEffort pod is the first the kubelet evicts. |
| `apiMaintenance.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `apiMaintenance.nodeSelector` | `{}` | Node selector. |
| `apiMaintenance.tolerations` | `[]` | Tolerations. |

### ingestWriter

Off by default. See [Migrations, maintenance and the ingest-writer](#migrations-maintenance-and-the-ingest-writer-optional).

| Key | Default | Description |
|---|---|---|
| `ingestWriter.enabled` | `false` | Deploy the ingest-writer (one replica, `Recreate`). Needs its image (plan 1B.6). |
| `ingestWriter.image` | `ghcr.io/fasterspeeding/distant-signal/ingest-writer`, tag `""` (appVersion), digest `""`, `IfNotPresent` | Image; same shape as `api.image`. |
| `ingestWriter.loops.enabled` | `false` | Run the train-domain loops (`INGEST_WRITER_LOOPS`) with the api's intervals. |
| `ingestWriter.streams.station-samples` | `apply` | Mode of `ds:ingest:station-samples` (`INGEST_WRITER_STREAMS`): `off` (not read) or `apply` (release B removed `shadow`; the render refuses it). Any stream not `off` gives the writer `REDIS_URL`, its Redis user (`redis.acl.clients.ingestWriter`) and the NetworkPolicy paths to Redis. The writer refuses a stream it has no handlers for yet (plan 3a.6). An enabled producer of the stream needs it on `apply` (the render fails otherwise); any stream not `off` needs the Redis ACL (stage `narrow`, `defaultUser: "on-unshared"` or `"off"` for `apply`). |
| `ingestWriter.streams.full-coverage` | `apply` | Mode of `ds:ingest:full-coverage`, as `station-samples` (handlers: plan 3a.6). Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"), as `station-samples`. |
| `ingestWriter.streams.tfl` | `apply` | Mode of `ds:ingest:tfl`, as `station-samples` (handlers: plan 3c). `apply` needs `postgresql.roles.perService.writer.connect` (security review M4: `line_status`'s row policy binds only the writer's own role; the render fails otherwise). Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"), as `station-samples`. |
| `ingestWriter.streams.reference` | `apply` | Mode of `ds:ingest:reference` (tocs), as `station-samples` (handlers: plan 3c). Default since release A (2026-10-09; docs/ingest-phase5-runbook.md, "Release A"), as `station-samples`. |
| `ingestWriter.streams.tfl-stops` | `off` | Mode of `ds:ingest:tfl-stops` (`tfl-stops` and `tfl-line-routes`, poller-tfl's daily stops and route-sequences jobs; `pollers.tfl.stops.enabled` needs `apply`). `apply` needs `postgresql.roles.perService.writer.connect`, as `tfl` (the writer's `ingest_freshness` row policy binds its own role only). |
| `ingestWriter.streams.ioi-gtfs` | `off` | Mode of `ds:ingest:ioi-gtfs` (`pollerIrishRailGtfs`: `ioi-stations`, `ioi-lines`), as `station-samples` (handlers: plan 3c). One stream per island-of-Ireland poller (security review H1): the writer applies only the schemas each stream carries. The old shared `island-of-ireland` key fails the render unless `off`. |
| `ingestWriter.streams.ioi-nir` | `off` | Mode of `ds:ingest:ioi-nir` (`pollerNirStations`: `ioi-stations`, `ioi-lines`), as `ioi-gtfs`. |
| `ingestWriter.streams.ioi-live` | `off` | Mode of `ds:ingest:ioi-live` (`pollerIrishRailLive`: `ioi-station-samples`), as `ioi-gtfs`. |
| `ingestWriter.streams.transit-metrolink` | `off` | Mode of `ds:ingest:transit-metrolink` (the transit networks' metrolink producer: `transit-timetable`, `transit-timetable-commit`, `transit-situations`, which also writes the network's `line_status` rows). `apply` needs `postgresql.roles.perService.writer.connect`, as `tfl` does. |
| `ingestWriter.streams.darwin` | `off` | Mode of `ds:ingest:darwin` (darwin-relay's Darwin Push Port batches, `darwin-pushport`; DS-152). `apply` needs `postgresql.roles.perService.writer.connect`, as `tfl` does; `darwinRelay.enabled` needs it on `apply`. |
| `ingestWriter.streams.transit-net` | `off` | Mode of `ds:ingest:transit-net` (the transit networks' net producer, Nottingham Express Transit: its timetable only, as NET has no disruptions source). `apply` needs `postgresql.roles.perService.writer.connect`, as `tfl` does. |
| `ingestWriter.streams.transit-tfl` | `off` | Mode of `ds:ingest:transit-tfl` (the transit networks' tfl producer, TfL's Underground, DLR and London Trams: its timetable only, as the lines' status is poller-tfl's). `apply` needs `postgresql.roles.perService.writer.connect`, as `tfl` does. |
| `ingestWriter.changedRowsOnly` | `false` | Changed rows only (`INGEST_WRITER_CHANGED_ROWS_ONLY`, plan 3a.9): `station_full_coverage_samples` rows whose stats are unchanged are not rewritten, and readers derive their age from the feed's observed time. The other snapshot tables keep their per-row time. Turn on only after the api with the derived readers is deployed and `streams.full-coverage` has soaked on `apply`. |
| `ingestWriter.unsupportedDeadlineSecs` | `3600` | `INGEST_WRITER_UNSUPPORTED_DEADLINE_SECS`: an entry pending as unsupported (a newer schema or envelope version) this long is dead-lettered, reason `unsupported_expired`, instead of blocking its stream for good. Roll the writer forward within this time. `0` never dead-letters it. |
| `ingestWriter.database.maxConnections` | `6` | Its Postgres pool; counted, plus its one loop-lock session, in the connection budgets. |
| `ingestWriter.progressStallSecs` | `900` | `/livez` stall window (`PROGRESS_STALL_SECS`). |
| `ingestWriter.logLevel` | `info` | `RUST_LOG`. |
| `ingestWriter.extraEnv` | `[]` | Extra env entries; one with a chart entry's name replaces it. |
| `ingestWriter.resources` | requests `25m`/`192Mi`, limit `384Mi` | Container resources (production p95 141Mi, max 167Mi). |
| `ingestWriter.nodeSelector` | `{}` | Node selector. |
| `ingestWriter.tolerations` | `[]` | Tolerations. |
| `ingestWriter.affinity` | `{}` | Affinity. |
| `ingestWriter.podAnnotations` | `{}` | Pod annotations. |
| `ingestWriter.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### devAuthentik

See "Local dev identity provider (devAuthentik)" above. Always off by
default; nothing here is rendered unless `devAuthentik.enabled` is `true`.

| Key | Default | Description |
|---|---|---|
| `devAuthentik.enabled` | `false` | Deploy a throwaway local Authentik instance for exercising this app's own login flow. An install pointing `api.sso.*` at a real external IdP is unaffected either way. |
| `devAuthentik.hostname` | `authentik.localhost` | The one hostname both the developer's browser and the api Pod must resolve identically. Resolves to loopback with no `/etc/hosts` entry needed in modern browsers (RFC 6761). |
| `devAuthentik.image.repository` | `ghcr.io/goauthentik/server` | Authentik server image repository. |
| `devAuthentik.image.tag` | `2026.8.0@sha256:…` | Pinned (digest in the tag); Authentik's ~3-month release cadence and 2-version support window mean this needs periodic bumping, not automated by this chart. |
| `devAuthentik.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `devAuthentik.secretKey` | `""` | `AUTHENTIK_SECRET_KEY`. Chart-generated (lookup-then-`randAlphaNum`) when empty, same pattern as `postgres-password`, so it survives `helm upgrade`. No `existingSecret` override — throwaway dev IdP only. |
| `devAuthentik.service.port` | `30900` | ClusterIP-facing port. Must equal `service.nodePort` — the render aborts if they differ. |
| `devAuthentik.service.nodePort` | `30900` | NodePort. Must equal `service.port`; default sits inside Kubernetes' default 30000-32767 NodePort range. |
| `devAuthentik.hostAliasIP` | `""` | Explicit override for the IP the api Deployment's `hostAliases` entry points `devAuthentik.hostname` at. Empty uses `lookup` against the live Service's ClusterIP at render time — unresolvable on a from-scratch `helm install` (see the "Two manual steps" note above and NOTES.txt). |
| `devAuthentik.postgresql.image` | `postgres:16.15-alpine@sha256:…` | Image for Authentik's own dedicated Postgres — independent of, and not a second database on, this chart's bundled `postgresql`. |
| `devAuthentik.postgresql.persistence.enabled` | `true` | Attach a PVC for Authentik's Postgres. |
| `devAuthentik.postgresql.persistence.size` | `1Gi` | Requested volume size. |
| `devAuthentik.postgresql.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `devAuthentik.postgresql.resources` | requests `50m`/`128Mi`, limit `512Mi` | Authentik Postgres container resource requests/limits. |
| `devAuthentik.resources` | requests `100m`/`256Mi`, limit `1Gi` | Authentik server container resource requests/limits. |
| `devAuthentik.nodeSelector` | `{}` | Pod node selector. |
| `devAuthentik.tolerations` | `[]` | Pod tolerations. |
| `devAuthentik.affinity` | `{}` | Pod affinity rules. |

### aggregator

There is intentionally no `replicaCount`: the aggregator is a singleton
write loop, pinned to `replicas: 1` with `strategy: Recreate`.

| Key | Default | Description |
|---|---|---|
| `aggregator.image.repository` | `ghcr.io/fasterspeeding/distant-signal/aggregator` | aggregator image repository. |
| `aggregator.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `aggregator.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `aggregator.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `aggregator.pollIntervalSecs` | `60` | Recompute cadence. |
| `aggregator.historyRetentionDays` | `7` | How long `line_status_history` rows are kept. |
| `aggregator.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`): one aggregation pass. |
| `aggregator.dailyStatsRetentionDays` | `300` | Days `line_status_daily_stats` (LDBWS-derived) rows are kept. Must stay under 365: the RDM Live Departure Board licence requires deleting received data within a year. |
| `aggregator.halfHourlyStatsRetentionHours` | `840` | Hours `line_status_half_hourly_stats` rows are kept (35 days). |
| `aggregator.fullCoverageEnabledDefault` | `true` | See `api.fullCoverageEnabledDefault`; set both to the same value. |
| `aggregator.fullCoverageWindow.mode` | `off` | Windowed full-coverage severity: `off`, `shadow` (record a verdict per line, change nothing) or `enforce` (also escalate the lines in `enforceLines`). |
| `aggregator.fullCoverageWindow.enforceLines` | `""` | Lines `enforce` may change: comma list, or `*` for every full-coverage-enabled line. Empty enforces nothing. |
| `aggregator.fullCoverageWindow.minEscalationRank` | `4` | Only verdicts of at least this severity rank are enforced (4 is Severe Delays / Part Suspended). |
| `aggregator.fullCoverageWindow.sparseEnforceLines` | `""` | Lines `enforce` may raise with a sparse all-cancelled verdict (below the 6-train sample size, every train cancelled: Part Suspended). Its own allowlist, not covered by `enforceLines`: comma list or `*`. Empty: recorded in shadow, never shown. |
| `aggregator.fullCoverageWindow.sparseMinCancelled` | `2` | Fewest cancelled trains (all of the window's) the sparse rule needs. 0 turns it off; a line's `full_coverage_sparse_min_cancelled` override wins. |
| `aggregator.fullCoverageWindow.retentionDays` | `14` | Days window stats and verdicts are kept (pruned in every mode). |
| `aggregator.trustEventBacklogRetentionDays` | `1` | Days `trust_event_backlog` rows are kept. Deliberately 1: a TRUST licensing safeguard. |
| `aggregator.scheduleDestinationDeparturesRetentionDays` | `8` | Service dates of `schedule_destination_departures` kept (about 377,000 rows a day); covers the train search's 7-day backward window. |
| `aggregator.trainsRetentionDays` | `30` | Days a `trains` row (with its movement events and current state) is kept when a user tracked it. |
| `aggregator.untrackedTrainsRetentionDays` | `14` | Days a `trains` row is kept when nobody tracked it. |
| `aggregator.logLevel` | `info` | `RUST_LOG` value. |
| `aggregator.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `aggregator.resources` | requests `100m`/`256Mi`, limit `384Mi` | Container resource requests/limits. |
| `aggregator.nodeSelector` | `{}` | Pod node selector. |
| `aggregator.tolerations` | `[]` | Pod tolerations. |
| `aggregator.affinity` | `{}` | Pod affinity rules. |
| `aggregator.podAnnotations` | `{}` | Pod annotations. |
| `aggregator.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### archive

Cold archive of pruned rows to S3-compatible storage, run by the aggregator.
See "Cold archive (optional)" above and `docs/cold-archive.md`. Off by
default; nothing here is rendered unless `archive.enabled` is `true`.

| Key | Default | Description |
|---|---|---|
| `archive.enabled` | `false` | Upload rows to S3 before the aggregator's retention prune deletes them. |
| `archive.tables` | `[trains]` | Tables to archive (explicit opt-in). `trains` archives each trains row with its movement events (without `raw_body`) and current state. `trust_event_backlog` and every LDBWS-derived table are refused: their retention is a licensing safeguard. |
| `archive.failurePolicy` | `retain` | On a failed upload: `retain` keeps the rows and retries next cycle; `delete` prunes them unarchived. |
| `archive.s3.endpoint` | `""` | S3 endpoint URL. Empty means AWS's regional endpoint. |
| `archive.s3.bucket` | `""` | **Required** when enabled. |
| `archive.s3.prefix` | `""` | Key prefix inside the bucket, e.g. `distant-signal/archive`. |
| `archive.s3.region` | `us-east-1` | Region used for request signing; most non-AWS servers accept any value. |
| `archive.s3.pathStyle` | `true` | Path-style addressing (`https://endpoint/bucket/key`), which most non-AWS servers need. `false` is virtual-hosted style. |
| `archive.s3.allowHttp` | `false` | Permit a plain `http://` endpoint. |
| `archive.s3.lifecycleConfirmed` | `false` | When enabled, this or `archive.expiry.enabled` **must be `true`**, or the render fails. Confirms the bucket has an S3 lifecycle expiration rule: the archiver itself never deletes an object. |
| `archive.s3.existingSecret` | `""` | **Required** when enabled: pre-existing Secret holding the access key pair. The chart never renders these credentials. |
| `archive.s3.accessKeyIdKey` | `access-key-id` | Key within `archive.s3.existingSecret` for the access key id. |
| `archive.s3.secretAccessKeyKey` | `secret-access-key` | Key within `archive.s3.existingSecret` for the secret access key. |
| `archive.expiry.enabled` | `false` | Client-side expiry of archived objects by the aggregator, for stores with no lifecycle API (Thoth). Needs `archive.enabled` and an `archive.s3.prefix` of at least two path segments. Satisfies the `lifecycleConfirmed` gate. Deletes only keys matching `<prefix>/<table>/service_date=YYYY-MM-DD/part-<19 digits>.jsonl.zst`, dated by the key, never by `LastModified`. |
| `archive.expiry.dryRun` | `true` | Log and count what would be deleted (`aggregator_archive_expiry_would_delete`), delete nothing. Set `false` only after checking the dry-run metrics. |
| `archive.expiry.retentionDays` | `730` | Keep objects whose key `service_date` is at most this many rail days old. Anything under 90 fails the render and the aggregator's startup; that floor is fixed in code. |
| `archive.expiry.intervalSecs` | `86400` | Seconds between expiry runs; the first runs at aggregator startup. |
| `archive.expiry.maxDeletesPerRun` | `20000` | Most objects one run deletes (or would delete), oldest date first. Hitting it bumps `aggregator_archive_expiry_cap_reached_total`. |
| `archive.expiry.protectedPrefixes` | `[]` | `ARCHIVE_PROTECTED_PREFIXES`: key prefixes the archive prefix must never equal, contain or sit inside (e.g. `mine-bringer/backups/`). The aggregator refuses to start on an overlap. |

### notifier

Web Push for pinned lines, tracked trains and journeys. A singleton loop
(`replicas: 1`, `strategy: Recreate`): two replicas would send every push
twice.

| Key | Default | Description |
|---|---|---|
| `notifier.image.repository` | `ghcr.io/fasterspeeding/distant-signal/notifier` | notifier image repository. |
| `notifier.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `notifier.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `notifier.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `notifier.progressStallSecs` | `900` | `/livez` stall window (`PROGRESS_STALL_SECS`, see `workerHealth`). |
| `notifier.pollIntervalSecs` | `60` | How often line status and tracked trains are polled for changes. |
| `notifier.cooldownMinutes` | `20` | How long a de-escalation or lateral line notification is suppressed after the last one sent to the same user for the same line. |
| `notifier.trainDelayThresholdMinutes` | `15` | Delay, in minutes, at or above which a tracked train's delay is worth a notification. |
| `notifier.cursorGraceSeconds` | `120` | How long a cursor watermark proposal must age before it is promoted to the cursor's `last_processed_id`. |
| `notifier.lineHistoryMaxAgeSeconds` | `900` | Line-status history rows whose `computed_at` is older than this are skipped, not pushed (`LINE_HISTORY_MAX_AGE_SECS`, ingest plan 3c.4, D13): the cursor moves past them, they stay the next row's "previous" status, and each counts in `notifier_line_history_skipped_total{reason="stale"}`. Deploy before `ingestWriter.streams.tfl: apply`. `0` turns the skip off. |
| `notifier.forwardQueuePollIntervalSecs` | `15` | Poll cadence for the forwarding queue (notifications api hands over), faster than `pollIntervalSecs`. |
| `notifier.skipCheckPollIntervalSecs` | `90` | Cadence of the station-skip check for tracked journeys. |
| `notifier.templateSweepPollIntervalSecs` | `3600` | Cadence of the recurring-journey materialisation sweep. |
| `notifier.predictedStartPollIntervalSecs` | `60` | Cadence of the predicted-late-start check (DS-127): one push per subscriber for a high-confidence predicted late start at or above `trainDelayThresholdMinutes`, sharing the delay pushes' state; an open journey break on the train sends its suggestion push instead. `0` turns it off. |
| `notifier.autoCommitLeadMinutes` | `120` | Lead time, in minutes, for the commit check on `auto`-mode journey legs. |
| `notifier.journeyBreaksPollIntervalSecs` | `15` | How often open journey breaks are checked for a suggestion push (only with `api.journeyBreaks.enabled`). |
| `notifier.push.workers` | `8` | Notifications sent at once (each fans out to all of that user's subscriptions). |
| `notifier.push.queueCapacity` | `1024` | Notifications waiting for a worker. Beyond this new ones are dropped (`distant_signal_notifier_push_dropped_total`). |
| `notifier.push.perUserInFlight` | `2` | At most this many of one user's notifications in flight, so one user's slow endpoints cannot take the whole pool. |
| `notifier.push.perUserQueued` | `64` | At most this many of one user's notifications waiting. |
| `notifier.push.pruneAfterTimeouts` | `3` | Delete a subscription after this many consecutive timeouts (in-memory count, reset on restart). |
| `notifier.push.shutdownGraceSecs` | `20` | On SIGTERM, seconds to let queued and in-flight pushes finish. |
| `notifier.vapid.subject` | `""` | `mailto:` or `https:` contact for the VAPID `sub` claim (RFC 8292). |
| `notifier.vapid.publicKey` | `""` | VAPID public key (uncompressed, base64url). Must pair with `privateKey`: the notifier refuses to start on a mismatch. Never auto-generated. |
| `notifier.vapid.privateKey` | `""` | VAPID private key, any of: PEM (SEC1 `EC PRIVATE KEY` from `openssl ecparam -genkey -name prime256v1 -noout`, or PKCS#8 `PRIVATE KEY`); that PEM with newlines as literal `\n` or spaces; or the raw 32-byte key as base64url/base64, padding optional (`npx web-push generate-vapid-keys`). values.yaml has a raw-to-PEM one-liner. Never auto-generated. |
| `notifier.vapid.existingSecret` | `""` | Read the VAPID key pair from this pre-existing Secret instead. |
| `notifier.vapid.existingSecretPublicKeyKey` | `vapid-public-key` | Key within `notifier.vapid.existingSecret` for the public key. |
| `notifier.vapid.existingSecretPrivateKeyKey` | `vapid-private-key` | Key within `notifier.vapid.existingSecret` for the private key. |
| `notifier.logLevel` | `info` | `LOG_LEVEL` value. |
| `notifier.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `notifier.resources` | requests `50m`/`128Mi`, limit `384Mi` | Container resource requests/limits. |
| `notifier.nodeSelector` | `{}` | Pod node selector. |
| `notifier.tolerations` | `[]` | Pod tolerations. |
| `notifier.affinity` | `{}` | Pod affinity rules. |
| `notifier.podAnnotations` | `{}` | Pod annotations. |
| `notifier.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### redis

There is intentionally no `replicaCount`: this is a single, non-clustered
instance (Deployment+PVC, not a StatefulSet — see redis-deployment.yaml's
own comment for why). See "Using an external Redis" above for what it's
used for and why persistence defaults on.

| Key | Default | Description |
|---|---|---|
| `redis.enabled` | `true` | Deploy the bundled Redis Deployment and Service. |
| `redis.externalUrl` | `""` | Connection URL of an externally-managed Redis. Used only when `redis.enabled` is false, where it is **required** — empty aborts the render. |
| `redis.auth.enabled` | `false` | Redis AUTH: give every Redis client `REDIS_PASSWORD` from a Secret. See "Redis authentication (optional)". |
| `redis.auth.requirePass` | `true` | With `auth.enabled` and the bundled Redis, start it with `--requirepass` and authenticate `redis-cli` in the pod (`exec` probes). `false` is step 1 of the no-outage enable sequence. |
| `redis.auth.existingSecret` | `""` | Secret holding the password. Empty: the chart generates `redis-password` in its own Secret (bundled Redis only; an external Redis requires this). |
| `redis.auth.existingSecretKey` | `redis-password` | Key within `redis.auth.existingSecret`. |
| `redis.acl.enabled` | `false` | Turn the ACL users on (needs `existingSecret`). |
| `redis.acl.stage` | `open` | `open`: every existing client's user has `~* &* +@all` (step 1). |
| `redis.acl.defaultUser` | `on` | `"on"`: the `default` user keeps today's password (redis.auth) and rights. |
| `redis.acl.existingSecret` | `""` | Existing Secret (a SealedSecret in Ranma-Config) with one key per user: `<user>-password` (letters and digits only), for every user in files/redis-users.acl.tpl. |
| `redis.acl.clients.enricher` | `false` | enricher, as user `enricher`. |
| `redis.acl.clients.movementRelay` | `false` | movement-relay, as user `movement-relay`. |
| `redis.acl.clients.trustConsumer` | `false` | trust-consumer, as user `trust-consumer`. |
| `redis.acl.clients.fullCoverageConsumer` | `false` | full-coverage-consumer, as user `full-coverage-consumer`. |
| `redis.acl.clients.trustBacklogConsumer` | `false` | trust-backlog-consumer, as user `trust-backlog-consumer`. |
| `redis.acl.clients.ingestWriter` | `false` | ingest-writer, as user `ingest-writer` (only once an `ingestWriter.streams` entry is not `off`). |
| `redis.acl.clients.pollerIncidents` | `false` | poller-incidents (its `incident-text-changed` XADD), as user `poller-incidents`. |
| `redis.acl.clients.pollerLdbws` | `false` | poller-ldbws (its stream sink), as user `poller-ldbws` (`~ds:ingest:station-samples +xadd +xrevrange`). |
| `redis.acl.clients.pollerTfl` / `.pollerTocs` | `false` | poller-tfl's and poller-tocs's stream sinks (ingest plan 3c.2), as users `poller-tfl` and `poller-tocs`. |
| `redis.acl.clients.pollerIrishRailGtfs` / `.pollerIrishRailLive` / `.pollerNirStations` | `false` | The island-of-Ireland pollers (each one's only sink is its own `ds:ingest:ioi-gtfs`, `ds:ingest:ioi-live` or `ds:ingest:ioi-nir`, D8), as users `poller-irish-rail-gtfs`, `poller-irish-rail-live` and `poller-nir-stations`. |
| `redis.acl.clients.pollerTransitMetrolink` | `false` | poller-transit-schedule for network metrolink (transit networks; off by default), as user `poller-transit-metrolink` (`~ds:ingest:transit-metrolink +xadd +xrevrange`). An `optional` user: rendered into the ACL file, and so needing the `poller-transit-metrolink-password` Secret key, only while this is on; turning it on restarts Redis once (docs/redis-acl.md). |
| `redis.acl.clients.darwinRelay` | `false` | darwin-relay (off by default; DS-152), as user `darwin-relay` (`~ds:ingest:darwin +xadd +xrevrange`). An `optional` user like `pollerTransitMetrolink`: it needs the `darwin-relay-password` Secret key only while this is on, and turning it on restarts Redis once. |
| `redis.acl.clients.pollerTransitNet` | `false` | poller-transit-schedule for network net (Nottingham Express Transit; off by default), as user `poller-transit-net` (`~ds:ingest:transit-net +xadd +xrevrange`). An `optional` user, as `pollerTransitMetrolink`: needs the `poller-transit-net-password` Secret key while on; turning it on restarts Redis once. |
| `redis.acl.clients.pollerTransitTfl` | `false` | poller-transit-schedule for network tfl (TfL's Underground, DLR and London Trams; off by default), as user `poller-transit-tfl` (`~ds:ingest:transit-tfl +xadd +xrevrange`). An `optional` user, as `pollerTransitMetrolink`: needs the `poller-transit-tfl-password` Secret key while on; turning it on restarts Redis once. |
| `redis.image.repository` | `valkey/valkey` | Server image repository: upstream Valkey, Redis-compatible (this repo builds no image for it). A running Redis 7.4 needs `docs/valkey-migration.md` before switching. |
| `redis.image.tag` | `8.1.10-trixie@sha256:…` | Valkey 8.1, digest-pinned in the tag. Renovate skips majors: Valkey 9 writes RDB 80, which the `redis:7.4.11` rollback cannot read. |
| `redis.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `redis.service.port` | `6379` | Service and container port; also sets the `REDIS_URL` every Redis client (api, enricher, the three consumers, movement-relay) gets. |
| `redis.persistence.enabled` | `true` | Attach a PVC and run redis with `--appendonly yes`. When false an emptyDir is used and data is lost on reschedule. |
| `redis.persistence.size` | `4Gi` | Requested volume size. The AOF can reach 1-2 GB at the default `maxmemory` (see Sizing above). An existing PVC on a non-expandable StorageClass keeps its size; read the [Upgrade](#upgrade) note before upgrading from a 1Gi install. |
| `redis.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `redis.persistence.accessModes` | `[ReadWriteOnce]` | PVC access modes. |
| `redis.persistence.existingClaim` | `""` | Use a pre-existing PVC instead of a chart-rendered one. |
| `redis.maxmemory` | `1536mb` | Passed as `--maxmemory`. With `noeviction`, a full Redis refuses writes (movement-relay backs off and Kafka holds the backlog) instead of being OOMKilled. Empty or null leaves it unbounded. |
| `redis.maxmemoryPolicy` | `noeviction` | Passed as `--maxmemory-policy`. Keep `noeviction`: any evicting policy deletes whole stream keys. |
| `redis.protoMaxBulkLen` | `2mb` | Passed as `--proto-max-bulk-len`: the largest single argument a client may send (Redis' default is 512mb). The largest legitimate one is an ingest entry's body (1 MiB accepted). At least `1mb`; empty or null keeps Redis' default. Changing it restarts Redis. |
| `redis.save` | `""` | Passed as `--save`. `""` disables RDB snapshots (AOF covers durability); null keeps the image's built-in schedule. |
| `redis.resources` | `{requests: {cpu: 50m, memory: 1536Mi}, limits: {memory: 2560Mi}}` | Container resource requests/limits, sized for `movementRelay.streamMaxLen` at `redis.maxmemory` plus fork copy-on-write; see values.yaml for the arithmetic. |
| `redis.probes.startup.type` | `tcpSocket` | `tcpSocket` or `exec` (`redis-cli ping`). See [Postgres and Redis probes](#postgres-and-redis-probes). |
| `redis.probes.startup.periodSeconds` | `5` | Startup probe period. Liveness and readiness start only after it succeeds. |
| `redis.probes.startup.timeoutSeconds` | `5` | Startup probe timeout. |
| `redis.probes.startup.failureThreshold` | `60` | Startup probe failures allowed (60 x 5s = 5 minutes for an AOF load). |
| `redis.probes.readiness.type` | `tcpSocket` | `tcpSocket` or `exec` (`redis-cli ping`). TCP turns ready slightly earlier during an AOF load. |
| `redis.probes.readiness.periodSeconds` | `10` | Readiness probe period (5 before tcpSocket became the default). |
| `redis.probes.readiness.timeoutSeconds` | `5` | Readiness probe timeout. |
| `redis.probes.readiness.failureThreshold` | `3` | Readiness probe failures allowed (3 x 10s = 30s). |
| `redis.probes.liveness.type` | `tcpSocket` | `tcpSocket` or `exec` (`redis-cli ping`). |
| `redis.probes.liveness.periodSeconds` | `30` | Liveness probe period. |
| `redis.probes.liveness.timeoutSeconds` | `5` | Liveness probe timeout. |
| `redis.probes.liveness.failureThreshold` | `6` | Liveness probe failures allowed (6 x 30s = 3 minutes). |
| `redis.nodeSelector` | `{}` | Pod node selector. |
| `redis.tolerations` | `[]` | Pod tolerations. |
| `redis.affinity` | `{}` | Pod affinity rules. |
| `redis.podAnnotations` | `{}` | Pod annotations. |
| `redis.podSecurityContext` | `{runAsUser: 999, runAsGroup: 999}` | Merged over the chart-wide pod securityContext defaults. Pinned (unlike most other `podSecurityContext` defaults in this chart) because the upstream `valkey/valkey` image (like `redis:7` before it) runs as root with no `USER` set at all; 999 is its `valkey` user's uid/gid, the same as the old `redis` user's, so an existing AOF volume stays readable. Override if you point `redis.image` at a different image/tag whose non-root uid differs. |

### enricher

There is intentionally no `replicaCount` and no `enabled` toggle: the
enricher is a singleton consumer of one Redis consumer group plus one sweep
loop, and it renders unconditionally.

`enricher.llm.baseUrl` and `enricher.llm.model` are **required** (alongside
the five `api.sso.*` values) — leaving either empty aborts the render, because both
become non-optional env vars on the binary and an empty value would deploy a
pod that fails every request forever. With `enricher.llm.provider: anthropic`
(the Claude API, [docs/enricher-anthropic.md](../../docs/enricher-anthropic.md))
they are not used: `enricher.llm.anthropic.*` has defaults for both, and
`enricher.llm.anthropic.existingSecret` is the required value instead.

| Key | Default | Description |
|---|---|---|
| `enricher.image.repository` | `ghcr.io/fasterspeeding/distant-signal/enricher` | enricher image repository. |
| `enricher.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `enricher.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `enricher.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `enricher.llm.provider` | `openai` | `LLM_PROVIDER`. `openai` is any OpenAI-compatible Chat Completions endpoint (the keys below). `anthropic` is the Claude API, configured by `enricher.llm.anthropic.*` (`baseUrl`/`model`/`apiKey`/`existingSecret` here are then unused, and `auth` must stay `apiKey`). Switching either way re-extracts every live incident once. Any other value aborts the render. |
| `enricher.llm.baseUrl` | `""` | **Required** (provider `openai`). Base URL of an OpenAI-compatible chat-completions endpoint. Empty aborts the render. |
| `enricher.llm.model` | `""` | **Required.** Model name that endpoint serves. Empty aborts the render. Also stored as the extraction's `model_version`, so changing it re-extracts every incident on the next sweep. |
| `enricher.llm.apiKey` | `""` | API key for that endpoint. Rendered into the chart Secret when `existingSecret` is empty. Empty is valid for a local endpoint needing no auth, and is never auto-generated. |
| `enricher.llm.existingSecret` | `""` | Read the API key from this pre-existing Secret instead. |
| `enricher.llm.existingSecretApiKeyKey` | `llm-api-key` | Key within `enricher.llm.existingSecret`. |
| `enricher.llm.reasoningEffort` | `""` | `LLM_REASONING_EFFORT`, sent as `reasoning_effort`. Empty renders nothing (no `reasoning_effort` sent). OpenAI's gpt-6-luna needs `none` (see [docs/enricher-openai.md](../../docs/enricher-openai.md)); NVIDIA's GLM wants `low`. An `enricher.extraEnv` entry of the same name wins. |
| `enricher.llm.auth` | `apiKey` | `LLM_AUTH`. `apiKey` uses `apiKey`/`existingSecret` above, as before. `openaiWifAuthentik` (primary) and `openaiWifKubernetes` (fallback) are keyless OpenAI workload identity federation: see "Keyless OpenAI auth" below. `anthropicWifAuthentik` (provider `anthropic` only) is keyless Claude auth through a separate Authentik provider, with a fresh Authentik token per exchange: see [docs/enricher-anthropic.md](../../docs/enricher-anthropic.md#keyless-auth). Any other value aborts the render. |
| `enricher.llm.workloadIdentity.identityProviderId` | `""` | `OPENAI_IDENTITY_PROVIDER_ID`, the OpenAI Workload Identity Provider's ID. Required in both WIF modes. |
| `enricher.llm.workloadIdentity.serviceAccountId` | `""` | `OPENAI_SERVICE_ACCOUNT_ID`, the OpenAI service account the mapping resolves to. Required in both WIF modes. |
| `enricher.llm.workloadIdentity.tokenAudience` | `""` | Audience of the projected service-account token. Empty in `openaiWifAuthentik` means `authentik.clientId`; required in `openaiWifKubernetes`, where it must equal the OpenAI provider's audience. |
| `enricher.llm.workloadIdentity.tokenExchangeUrl` | `https://auth.openai.com/oauth/token` | `LLM_TOKEN_EXCHANGE_URL`. |
| `enricher.llm.workloadIdentity.authentik.tokenUrl` | `""` | `LLM_AUTHENTIK_TOKEN_URL` (e.g. `https://sso.example.com/application/o/token/`). Required in `openaiWifAuthentik`. |
| `enricher.llm.workloadIdentity.authentik.clientId` | `""` | `LLM_AUTHENTIK_CLIENT_ID`, the dedicated Authentik OAuth2 provider's client ID. Required in `openaiWifAuthentik` and `anthropicWifAuthentik` (a different provider per mode, so a token for one can't be replayed at the other). |
| `enricher.llm.workloadIdentity.anthropic.organizationId` | `""` | `ANTHROPIC_ORGANIZATION_ID`, the Claude organization UUID. Required in `anthropicWifAuthentik`. |
| `enricher.llm.workloadIdentity.anthropic.serviceAccountId` | `""` | `ANTHROPIC_SERVICE_ACCOUNT_ID`, the Claude service account (`svac_...`). Required in `anthropicWifAuthentik`. |
| `enricher.llm.workloadIdentity.anthropic.federationRuleId` | `""` | `ANTHROPIC_FEDERATION_RULE_ID`, the federation rule (`fdrl_...`). Required in `anthropicWifAuthentik`. |
| `enricher.llm.workloadIdentity.anthropic.workspaceId` | `""` | `ANTHROPIC_WORKSPACE_ID` (`wrkspc_...`); only needed when the rule spans more than one workspace. |
| `enricher.llm.workloadIdentity.anthropic.tokenExchangeUrl` | `https://api.anthropic.com/v1/oauth/token` | `LLM_TOKEN_EXCHANGE_URL` in `anthropicWifAuthentik`. Its port gets the NetworkPolicy egress entry. |
| `enricher.llm.workloadIdentity.authentik.scope` | `""` | `LLM_AUTHENTIK_SCOPE`, e.g. `profile` so Authentik's mapping emits the `groups` claim. Empty sends no `scope`. |
| `enricher.llm.anthropic.baseUrl` | `https://api.anthropic.com/v1` | `LLM_BASE_URL` with provider `anthropic` (requests go to `{baseUrl}/messages` and `{baseUrl}/messages/batches`). Its port gets the NetworkPolicy egress entry. |
| `enricher.llm.anthropic.model` | `claude-haiku-5-5` | `LLM_MODEL` with provider `anthropic`. `claude-sonnet-5-5` is the step-up if the quality eval shows misses (see the doc's "Model"). |
| `enricher.llm.anthropic.existingSecret` | `""` | **Required** with provider `anthropic`: a pre-existing Secret holding the Claude API key (sent as `x-api-key`). The chart never renders this key into its own Secret. |
| `enricher.llm.anthropic.existingSecretApiKeyKey` | `anthropic-api-key` | Key within `enricher.llm.anthropic.existingSecret`. |
| `enricher.llm.anthropic.promptCache` | `1h` | `LLM_PROMPT_CACHE`: `1h`, `5m` or `off`, the `cache_control` TTL on each call's static system prompt (see the doc's caching analysis). Any other value aborts the render. |
| `enricher.llm.anthropic.thinking` | `""` | `LLM_THINKING`, sent as `thinking.type` when set (e.g. `disabled` on Haiku 5.5, `between_tools` on Sonnet 5.5). Empty: the model's default (adaptive thinking). |
| `enricher.llm.profile` | `""` | `LLM_PROFILE`: a built-in generation profile (`openai-gpt-6-luna`, `openai-default`, `claude-haiku-5`, `claude-default`). Empty picks it by provider and model: OpenAI-compatible endpoints get `temperature: 0` (gpt-6-luna also effort `none`), Claude no sampling parameters. A profile for the other provider, or an unknown name, aborts the render. See [docs/enricher-anthropic.md](../../docs/enricher-anthropic.md#tuning-per-model). |
| `enricher.llm.adversarialModel` | `""` | `LLM_ADVERSARIAL_MODEL`: the model for the two adversarial calls (the resolution and severity checks), on the same provider, endpoint and credential as the primary model (e.g. `claude-haiku-5-5` with `enricher.llm.anthropic.model: claude-sonnet-5-5`). A model of the other provider aborts the render. Empty (the default): the primary model, as before. Part of `model_version`, so setting, changing or clearing it re-extracts every live incident once. See [docs/enricher-anthropic.md](../../docs/enricher-anthropic.md#tuning-per-model). |
| `enricher.llm.adversarialProfile` | `""` | `LLM_ADVERSARIAL_PROFILE`: the adversarial calls' profile (same names as `enricher.llm.profile`), for their sampling defaults and prompt files; `temperature`, `topP`, `reasoningEffort` and `anthropic.thinking` apply to the primary call only. Empty picks it by provider and `adversarialModel`. Needs `adversarialModel`; an unknown name or another provider's aborts the render. |
| `enricher.llm.temperature` | `""` | `LLM_TEMPERATURE`: overrides the profile's temperature; a number (quote it, e.g. `"0"`), or `omit` to send none. A value a model is known to reject fails at startup; anything else aborts the render. |
| `enricher.llm.topP` | `""` | `LLM_TOP_P`: overrides the profile's `top_p` (none); a number (quoted) or `omit`. |
| `enricher.llm.openaiPromptCache` | `""` | `LLM_OPENAI_PROMPT_CACHE` (Chat Completions only): `explicit` caches the static system prompt (explicit-only mode, one breakpoint, a stable `prompt_cache_key` per profile and call), `off` disables caching and its cache writes, `omit` sends nothing (the endpoint's default). Empty keeps the profile's: `explicit` for gpt-6-luna, nothing otherwise. Any other value aborts the render. See [docs/enricher-openai.md](../../docs/enricher-openai.md#prompt-caching). |
| `enricher.llm.prompts.configMap` | `""` | An existing ConfigMap of system-prompt overrides, mounted at `/etc/enricher/prompts` (`LLM_PROMPTS_DIR`): keys `<profile>.<call>.txt` or `<call>.txt` (`primary`, `adversarial`, `severity_adversarial`); a call with neither keeps the built-in prompt. A prompt change changes `model_version`, so incidents re-extract. The tuned Haiku set is `crates/enricher/prompts/` (`claude-haiku-5.*.txt`). |
| `enricher.llm.carryForwardSemanticNoops` | `true` | `CARRY_FORWARD_SEMANTIC_NOOPS`: a text change that only touches HTML, whitespace, entities, case or in-word punctuation re-stamps the existing extraction instead of re-running the LLM (`enricher_extraction_carried_forward_total`). `false` re-runs the LLM for every text change; the edit class then only labels `enricher_extraction_rerun_total` and `enricher_extraction_churn_total` (`edit_class`). Must be a boolean. |
| `enricher.llm.batch.mode` | `normal` | `LLM_MODE`. `normal`: the stream loop extracts each text change synchronously; the hourly sweep follows `sweepMode`. `batch`: the stream loop and reclaim make no LLM call; a sweep every `sweepIntervalSecs` batches at least `minItems` stale incidents and extracts fewer synchronously. `batch-only`: as `batch`, but everything is batched (half price; results in minutes to hours). Both ignore `sweepMode`. Either provider (OpenAI needs `api.batch.write`: docs/enricher-openai.md, "Batch mode"). Any other value aborts the render. See [docs/enricher-anthropic.md](../../docs/enricher-anthropic.md#llm-modes). |
| `enricher.llm.batch.sweepIntervalSecs` | `""` | `LLM_BATCH_SWEEP_INTERVAL_SECS`: the sweep's interval in mode `batch`/`batch-only`. Empty: 120 (`batch`) or 300 (`batch-only`). Anything but a positive whole number aborts the render. |
| `enricher.llm.batch.sweepMode` | `sync` | `LLM_SWEEP_MODE` (mode `normal` only). `batch` sends the reconciliation sweep's extractions through the provider's batch API (Claude Message Batches, or the OpenAI Batch API with provider `openai`, which needs `api.batch.write`: docs/enricher-openai.md, "Batch mode"), at half price, results usually within an hour (at most 24 h); the stream loop and reclaim stay synchronous. Any other value aborts the render. |
| `enricher.llm.batch.minItems` | `20` | `LLM_BATCH_MIN_ITEMS`: a sweep that finds fewer incidents runs them synchronously (ignored in mode `batch-only`). |
| `enricher.llm.batch.maxItems` | `2000` | `LLM_BATCH_MAX_ITEMS`: most incidents per batch (the service caps it at 5000). |
| `enricher.llm.batch.pollIntervalSecs` | `60` | `LLM_BATCH_POLL_SECS`: how often in-flight batches are polled. |
| `enricher.serviceAccount.create` | `false` | Create a dedicated ServiceAccount for the enricher (`<fullname>-enricher`, or `name`), with no RBAC and `automountServiceAccountToken: false`. Off: the enricher uses the shared `serviceAccount`. Required (or `name`) by both WIF modes. |
| `enricher.serviceAccount.name` | `""` | Its name. With `create: false`, an existing ServiceAccount, which must not be the shared one. |
| `enricher.llmRequestTimeoutSecs` | `300` | Per-request timeout for a single LLM call (`LLM_REQUEST_TIMEOUT_SECS`). One incident makes three sequential calls. Behind a gateway that cuts calls itself (e.g. a 504 at ~302 s), set this slightly above the gateway's cutoff (e.g. `320`) so the 504 is what gets reported. |
| `enricher.sweepIntervalSecs` | `3600` | Cadence of the backstop sweep that re-checks every uncleared incident's text hash and model version. |
| `enricher.reclaimIntervalSecs` | `60` | How often the reclaim loop checks for stream entries stuck unacked past `reclaimMinIdleSecs` (a timed-out request, or a crash between processing and acking). |
| `enricher.reclaimMinIdleSecs` | `1000` | How long a pending entry must sit unacked before it's eligible for reclaim, i.e. the retry delay for a failed extraction. Entries whose incident is still being processed are skipped, so this is not a correctness bound; keeping it above `3 * llmRequestTimeoutSecs` avoids needless claim-and-skip passes. |
| `enricher.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`): one incident is up to three LLM calls of `llmRequestTimeoutSecs`. |
| `enricher.updateStrategy` | `{type: Recreate}` | The Deployment's rollout. `Recreate` stops the old pod before the new one starts (a gap of seconds; unacked entries stay pending and are reclaimed. A surge pod also has its own in-flight dedupe, so an incident could be extracted, and paid for, twice). `RollingUpdate` takes `rollingUpdate.maxSurge` (default 0) and `rollingUpdate.maxUnavailable` (default 1); a maxSurge above 0 is added to the replicas in the computed CONNECTION LIMIT and the max_connections budget. A surge with a role sized for one pod deadlocked the 2026-10-10 rollout: see "Rollout strategy and connection limits" below and docs/ingest-phase5-runbook.md "Rollout deadlock". |
| `enricher.logLevel` | `info` | `RUST_LOG` value. |
| `enricher.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. The off-by-default enricher settings below are set here. |
| `enricher.resources` | requests `50m`/`128Mi`, limit `256Mi` | Container resource requests/limits. |
| `enricher.nodeSelector` | `{}` | Pod node selector. |
| `enricher.tolerations` | `[]` | Pod tolerations. |
| `enricher.affinity` | `{}` | Pod affinity rules. |
| `enricher.podAnnotations` | `{}` | Pod annotations. |
| `enricher.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

The enricher has off-by-default settings with no dedicated value (apart
from `LLM_REASONING_EFFORT`, which `enricher.llm.reasoningEffort` also
sets). Set them through `enricher.extraEnv`; with none set, it sends the
same requests as before and never retries a call in-process.

| Env var | Default | Description |
|---|---|---|
| `LLM_REASONING_EFFORT` | unset | Sent as `reasoning_effort` (e.g. `low`, or `none` for OpenAI's gpt-6-luna). Reasoning models whose default effort is high need it, or they can spend their whole budget thinking and return empty content (`outcome="empty_content"`). Prefer `enricher.llm.reasoningEffort`. |
| `LLM_MAX_TOKENS` | unset | Sent as `max_tokens` (e.g. `8192`). Leave unset for OpenAI's gpt-6-luna, which rejects `max_tokens`. |
| `LLM_MAX_IN_FLIGHT` | unset | Cap on concurrent LLM HTTP requests across the stream, sweep and reclaim loops. Also how many incidents the synchronous sweep extracts at once (unset: one at a time). |
| `LLM_RATE_LIMIT_RETRIES` | `0` | In-call retries on HTTP 429. Each waits `Retry-After` (at least `LLM_RATE_LIMIT_RETRY_SECS`); a `Retry-After` over 600 s fails the call instead. A 429 whose body says the account is out of quota or at its billing limit (`insufficient_quota`, `outcome="quota_exhausted"`) is never retried. |
| `LLM_RATE_LIMIT_RETRY_SECS` | `20` | Minimum wait before a 429 retry. |
| `LLM_GATEWAY_RETRIES` | `0` | In-call retries on 502/503/504 and client timeouts. A 502/503/504 retry waits the response's `Retry-After` (over 600 s fails the call instead), or else 2 s, 4 s, ... up to 30 s; a client timeout retries at once. Above `0` it also stops a timeout or 504 feeding the per-text retry backoff. (429, 502 and 503 never feed it.) |

For example, for a slow, rate-limited hosted reasoning model:

```yaml
enricher:
  llmRequestTimeoutSecs: 320
  reclaimMinIdleSecs: 3600
  extraEnv:
    - { name: LLM_REASONING_EFFORT, value: "low" }
    - { name: LLM_MAX_TOKENS, value: "8192" }
    - { name: LLM_MAX_IN_FLIGHT, value: "3" }
    - { name: LLM_RATE_LIMIT_RETRIES, value: "3" }
    - { name: LLM_GATEWAY_RETRIES, value: "2" }
```

#### Keyless OpenAI auth (workload identity federation)

Off by default. With `enricher.llm.auth` set to `openaiWifAuthentik` or
`openaiWifKubernetes`, the enricher holds no API key: it exchanges a
short-lived, audience-bound Kubernetes service-account token for an OpenAI
access token (at most an hour), through Authentik in the primary mode or
directly in the fallback. The setup on the Authentik and OpenAI side, and
why there are two modes, are in
[docs/enricher-openai.md](../../docs/enricher-openai.md#keyless-auth-workload-identity-federation).

The chart then:

- requires a dedicated ServiceAccount (`enricher.serviceAccount.create:
  true`, or `name` naming another existing account) and fails the render
  without one: a trust rule on the shared account's subject would let every
  pod in the release mint OpenAI tokens. The pod keeps
  `automountServiceAccountToken: false`;
- mounts a projected volume with only a `serviceAccountToken` source (path
  `token`, the configured audience, `expirationSeconds: 3600`) read-only at
  `/var/run/secrets/openai`: no `ca.crt`, no namespace, no API access. The
  kubelet rotates it and the enricher re-reads it on every exchange;
- renders `LLM_AUTH`, the IDs and URLs as env, and neither `LLM_API_KEY`
  nor the Secret's `llm-api-key` entry. A non-empty `enricher.llm.apiKey`
  or `existingSecret` fails the render, as does a missing required value;
- with `networkPolicy.egress.enabled`, adds the token-exchange and
  (primary mode) Authentik token URLs' ports to the enricher's internet
  rule. The rule is IP-based: it opens ports, not hosts;
- renders `DistantSignalEnricherTokenExchangeFailing` (with
  `metrics.prometheusRule.enabled`).

For example, the primary mode:

```yaml
enricher:
  serviceAccount:
    create: true
  llm:
    baseUrl: https://api.openai.com/v1
    model: gpt-6-luna
    reasoningEffort: none
    auth: openaiWifAuthentik
    workloadIdentity:
      identityProviderId: <openai-identity-provider-id>
      serviceAccountId: <openai-service-account-id>
      authentik:
        tokenUrl: https://sso.example.com/application/o/token/
        clientId: <authentik-client-id>
        scope: profile
```

Its metrics, on top of `enricher_llm_call_total` (whose `outcome` gains
`auth_error`, no token could be minted, and `unauthorized`, a 401 to a
freshly exchanged token):

| Metric | Type | Meaning |
|---|---|---|
| `distant_signal_enricher_llm_token_exchange_total{stage, outcome}` | counter | Token requests by `stage` (`authentik`, `openai`) and `outcome`: `success`, `token_file_error` (the projected token is missing or empty), `invalid_grant`, `invalid_client`, `invalid_subject_token` (the endpoint's OAuth error), `http_error` (any other non-2xx), `timeout` (10 s), `error` (connection or response failure). Registered at 0 for each mode's stages. Alert on failures with no success. |
| `distant_signal_enricher_llm_token_remaining_seconds` | gauge | Remaining lifetime of the cached OpenAI token, set at each exchange and each LLM call (so it holds still between calls); 0 before the first token and after a 401. |

The enricher refreshes a token `max(LLM_TOKEN_REFRESH_SKEW_SECS, 10% of its
lifetime)` before it expires (default skew 60 s; set it through
`enricher.extraEnv`), and keeps using the cached token while a refresh fails
until it expires.

LLM token usage, in every auth mode, for a keyless spend estimate (the
query is in [docs/enricher-openai.md](../../docs/enricher-openai.md#cost)):

| Metric | Type | Meaning |
|---|---|---|
| `distant_signal_enricher_llm_tokens_total{call, kind}` | counter | Tokens the endpoint reported in each response's `usage`, by `call` (`primary`, `resolution_adversarial`, `severity_adversarial`, as on `enricher_llm_call_total`) and `kind` (`prompt`, `completion`, `reasoning`, `cached`). Counted on every 2xx response that carries `usage`, including ones the enricher then rejects (refused, empty, bad JSON), since those are billed; an endpoint that sends no `usage` counts nothing. `cached` is a subset of `prompt`, and `reasoning` a subset of `completion`: never add them on top. All 12 series are registered at 0. |
| `distant_signal_enricher_llm_model_info{model, base_url_host}` | gauge | Always 1: the configured `enricher.llm.model` and the host (and port) of `enricher.llm.baseUrl`, for joining a price onto the counter. |
| `distant_signal_enricher_llm_call_model_info{call, model, profile}` | gauge | Always 1, one series per `call`: the model and profile that call is sent to. All three are the primary model unless `enricher.llm.adversarialModel` is set; join on `call` to price the token counter per model. |

No spend alert here: the OpenAI project's own budget is the limit.

### trustConsumer

Resolves tracked trains from TRUST train movements. It reads
movement-relay's `movement-events` stream and has no Kafka connection of
its own. `trustConsumer.kafka.*` is the RDM Kafka connection movement-relay
falls back to (see `movementRelay`).

| Key | Default | Description |
|---|---|---|
| `trustConsumer.image.repository` | `ghcr.io/fasterspeeding/distant-signal/trust-consumer` | trust-consumer image repository. |
| `trustConsumer.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `trustConsumer.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `trustConsumer.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `trustConsumer.kafka.brokers` | `""` | RDM Train Movements broker address(es). Used by movement-relay when `movementRelay.kafka.brokers` is empty. trust-consumer itself does not read Kafka. |
| `trustConsumer.kafka.topic` | `""` | Train Movements topic (production: `TRAIN_MVT_ALL_TOC`). Same fallback as `brokers`. |
| `trustConsumer.kafka.consumerGroup` | `distant-signal-trust-consumer` | Kafka consumer group. For RDM this must be the RDM-issued `SC-...` id; RDM issues one per account, and movement-relay is its only member. |
| `trustConsumer.kafka.saslMechanism` | `""` | SASL mechanism (production: `PLAIN`). Same fallback as `brokers`. |
| `trustConsumer.kafka.saslUsername` | `""` | SASL username, rendered into the chart Secret as `kafka-sasl-username`. Never auto-generated. |
| `trustConsumer.kafka.saslPassword` | `""` | SASL password, rendered as `kafka-sasl-password`. Never auto-generated. |
| `trustConsumer.kafka.existingSecret` | `""` | Read the SASL credential from this pre-existing Secret instead. |
| `trustConsumer.kafka.existingSecretUsernameKey` | `kafka-sasl-username` | Key for the SASL username. |
| `trustConsumer.kafka.existingSecretPasswordKey` | `kafka-sasl-password` | Key for the SASL password. |
| `trustConsumer.ingest.database.maxConnections` | `2` | The pool under `sink: db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `trustConsumer.referenceReloadSecs` | `60` | How often the active-tracked-trains reference set is reloaded from api. |
| `trustConsumer.stanoxCrsReloadSecs` | `3600` | How often the live STANOX-to-CRS table is reloaded from Postgres. |
| `trustConsumer.internalReads.database.maxConnections` | `2` | Its Postgres pool under `db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `trustConsumer.retentionDays` | `90` | Days `train_movement_events` rows are kept before pruning. |
| `trustConsumer.healthPort` | `8081` | Port for `/healthz` (readiness) and `/livez` (liveness). |
| `trustConsumer.progressStallSecs` | `300` | `/livez` answers 503 once no consume-loop iteration has completed for this many seconds. |
| `trustConsumer.replicaCount` | `1` | Replicas. Exists so trust-consumer can be scaled to 0 through `helm upgrade`. |
| `trustConsumer.updateStrategy` | `{type: Recreate}` | The Deployment's rollout. `Recreate` stops the old pod before the new one starts (a gap of seconds; the consumer group keeps every entry and the new pod replays its own pending entries). `RollingUpdate` takes `rollingUpdate.maxSurge` (default 0) and `rollingUpdate.maxUnavailable` (default 1); a maxSurge above 0 is added to the replicas in the computed CONNECTION LIMIT and the max_connections budget. A surge with a role sized for one pod deadlocked the 2026-10-10 rollout: see "Rollout strategy and connection limits" below and docs/ingest-phase5-runbook.md "Rollout deadlock". |
| `trustConsumer.redisAutoclaimMinIdleSecs` | `30` | How long an entry may sit unacknowledged in this consumer's pending list before the periodic sweep reclaims it. |
| `trustConsumer.redisGapCheckSecs` | `60` | How often the consumer group's position is compared with the stream's oldest entry to detect a gap. |
| `trustConsumer.metricsPort` | `9095` | Prometheus `/metrics` port. |
| `trustConsumer.logLevel` | `info` | `RUST_LOG` value. |
| `trustConsumer.trustTimestampCorrectionEnabled` | `true` | Kill switch for the TRUST timestamp Europe/London-mislabelling correction (`crates/common/src/trust_timestamp.rs`). |
| `trustConsumer.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `trustConsumer.resources` | requests `200m`/`256Mi`, limit `384Mi` | Container resource requests/limits. |
| `trustConsumer.nodeSelector` | `{}` | Pod node selector. |
| `trustConsumer.tolerations` | `[]` | Pod tolerations. |
| `trustConsumer.affinity` | `{}` | Pod affinity rules. |
| `trustConsumer.podAnnotations` | `{}` | Pod annotations. |
| `trustConsumer.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### fullCoverageConsumer

Computes whole-network delay and cancellation stats per line from the
movement stream and the CIF schedule population. It reads
movement-relay's `movement-events` stream and has no Kafka connection of
its own.

| Key | Default | Description |
|---|---|---|
| `fullCoverageConsumer.image.repository` | `ghcr.io/fasterspeeding/distant-signal/full-coverage-consumer` | full-coverage-consumer image repository. |
| `fullCoverageConsumer.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `fullCoverageConsumer.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `fullCoverageConsumer.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `fullCoverageConsumer.shadowLines` | `*` | Comma-separated line ids to compute, or `*` for every catalogued line with at least one TIPLOC. Does not decide whether the stats are shown; that is `api`/`aggregator.fullCoverageEnabledDefault` and each line's `full_coverage_enabled`. |
| `fullCoverageConsumer.populationReloadSecs` | `300` | How often the per-line schedule population is reloaded from api. |
| `fullCoverageConsumer.stanoxCrsReloadSecs` | `3600` | How often the STANOX-to-CRS table is reloaded from api. |
| `fullCoverageConsumer.internalReads.database.maxConnections` | `2` | Its Postgres pool under `db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `fullCoverageConsumer.statsWriteIntervalSecs` | `60` | How often computed stats are posted to api. |
| `fullCoverageConsumer.healthPort` | `8082` | Port for `/healthz` (readiness) and `/livez` (liveness). |
| `fullCoverageConsumer.progressStallSecs` | `900` | `/livez` answers 503 once no consume-loop iteration has completed for this many seconds. |
| `fullCoverageConsumer.metricsPort` | `9093` | Prometheus `/metrics` port. |
| `fullCoverageConsumer.replicaCount` | `1` | Replicas. |
| `fullCoverageConsumer.updateStrategy` | `{type: Recreate}` | The Deployment's rollout. `Recreate` stops the old pod before the new one starts (a gap of seconds; the consumer group keeps every entry and the new pod replays its own pending entries). `RollingUpdate` takes `rollingUpdate.maxSurge` (default 0) and `rollingUpdate.maxUnavailable` (default 1); a maxSurge above 0 is added to the replicas in the computed CONNECTION LIMIT and the max_connections budget. A surge with a role sized for one pod deadlocked the 2026-10-10 rollout: see "Rollout strategy and connection limits" below and docs/ingest-phase5-runbook.md "Rollout deadlock". |
| `fullCoverageConsumer.redisAutoclaimMinIdleSecs` | `30` | See `trustConsumer.redisAutoclaimMinIdleSecs`. |
| `fullCoverageConsumer.redisGapCheckSecs` | `60` | See `trustConsumer.redisGapCheckSecs`. |
| `fullCoverageConsumer.windowedStats.enabled` | `false` | Windowed full-coverage stats (`docs/superpowers/specs/2026-09-27-full-coverage-windowed-stats-design.md`). Off: only the whole-day rows are written. Turn on only after api and schedule-reference support it; `aggregator.fullCoverageWindow.mode` is a separate switch. |
| `fullCoverageConsumer.windowedStats.recentWindowMinutes` | `60` | The `recent` window covers trains due in the last this-many minutes. |
| `fullCoverageConsumer.windowedStats.graceMinutes` | `10` | Windows end this many minutes ago (feed lag p99 plus the write cadence). |
| `fullCoverageConsumer.windowedStats.activationsMin` | `20` | Fewer Activations than this in the last hour marks the write `feed_stale`, so its windows cannot affect severity. |
| `fullCoverageConsumer.windowedStats.feedStaleSecs` | `300` | A newest consumed movement older than this also marks the write `feed_stale`. |
| `fullCoverageConsumer.windowedStats.lineMembership` | `scope` | Which trains count for a line's windowed stats: `scope` (`schedule-reference`'s train membership, `scope == line`, §4.1 for entries without it; production's setting), `legacy` (§4.1: operator + calls at two stations; the binary's own default) or `shadow` (legacy output, both compared in the `full_coverage_consumer_line_membership_*` gauges and an info log per line). Needs `windowedStats.enabled`. See `docs/superpowers/specs/2026-10-06-line-membership-design.md`. |
| `fullCoverageConsumer.logLevel` | `info` | `RUST_LOG` value. |
| `fullCoverageConsumer.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `fullCoverageConsumer.resources` | requests `200m`/`640Mi`, limit `1Gi` | Container resource requests/limits. Holds per-line population caches, so it scales with the line catalogue; with windowed stats on, the working set is 430-570 MiB (target ~650 MiB). |
| `fullCoverageConsumer.nodeSelector` | `{}` | Pod node selector. |
| `fullCoverageConsumer.tolerations` | `[]` | Pod tolerations. |
| `fullCoverageConsumer.affinity` | `{}` | Pod affinity rules. |
| `fullCoverageConsumer.podAnnotations` | `{}` | Pod annotations. |
| `fullCoverageConsumer.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### trustBacklogConsumer

Keeps a short backlog of TRUST events at key journey points on catalogued
lines, so a train pinned after it has already departed can still be matched.
Always reads movement-relay's stream (there is no Kafka mode).

| Key | Default | Description |
|---|---|---|
| `trustBacklogConsumer.image.repository` | `ghcr.io/fasterspeeding/distant-signal/trust-backlog-consumer` | trust-backlog-consumer image repository. |
| `trustBacklogConsumer.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `trustBacklogConsumer.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `trustBacklogConsumer.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `trustBacklogConsumer.ingest.database.maxConnections` | `3` | The pool under `sink: db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `trustBacklogConsumer.stanoxCrsReloadSecs` | `3600` | How often the STANOX-to-CRS table is reloaded from api. |
| `trustBacklogConsumer.redisAutoclaimMinIdleSecs` | `30` | See `trustConsumer.redisAutoclaimMinIdleSecs`. |
| `trustBacklogConsumer.redisGapCheckSecs` | `60` | See `trustConsumer.redisGapCheckSecs`. |
| `trustBacklogConsumer.healthPort` | `8083` | Port for `/healthz` (readiness) and `/livez` (liveness). |
| `trustBacklogConsumer.progressStallSecs` | `300` | `/livez` answers 503 once no consume-loop iteration has completed for this many seconds. |
| `trustBacklogConsumer.updateStrategy` | `{type: Recreate}` | The Deployment's rollout. `Recreate` stops the old pod before the new one starts (a gap of seconds; the consumer group keeps every entry and the new pod replays its own pending entries). `RollingUpdate` takes `rollingUpdate.maxSurge` (default 0) and `rollingUpdate.maxUnavailable` (default 1); a maxSurge above 0 is added to the replicas in the computed CONNECTION LIMIT and the max_connections budget. A surge with a role sized for one pod deadlocked the 2026-10-10 rollout: see "Rollout strategy and connection limits" below and docs/ingest-phase5-runbook.md "Rollout deadlock". |
| `trustBacklogConsumer.metricsPort` | `9096` | Prometheus `/metrics` port. |
| `trustBacklogConsumer.retentionDaysWarningAcknowledged` | `false` | Documentation-only flag; the binary does not read it. The retention safeguard is `aggregator.trustEventBacklogRetentionDays`. |
| `trustBacklogConsumer.logLevel` | `info` | `RUST_LOG` value. |
| `trustBacklogConsumer.trustTimestampCorrectionEnabled` | `true` | See `trustConsumer.trustTimestampCorrectionEnabled`. |
| `trustBacklogConsumer.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `trustBacklogConsumer.resources` | requests `50m`/`128Mi`, limit `256Mi` | Container resource requests/limits. |
| `trustBacklogConsumer.nodeSelector` | `{}` | Pod node selector. |
| `trustBacklogConsumer.tolerations` | `[]` | Pod tolerations. |
| `trustBacklogConsumer.affinity` | `{}` | Pod affinity rules. |
| `trustBacklogConsumer.podAnnotations` | `{}` | Pod annotations. |
| `trustBacklogConsumer.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### movementRelay

The one Kafka client for RDM's Train Movements feed: it copies every
message into the `movement-events` Redis stream the three consumers above
read. On by default. Each empty `movementRelay.kafka.*` connection value
falls back to the matching `trustConsumer.kafka.*` value, and without a
credential of its own it uses trust-consumer's; see "Install" above.

| Key | Default | Description |
|---|---|---|
| `movementRelay.enabled` | `true` | Deploy movement-relay. Set `false` only for an install that does not ingest TRUST movements. |
| `movementRelay.image.repository` | `ghcr.io/fasterspeeding/distant-signal/movement-relay` | movement-relay image repository. |
| `movementRelay.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `movementRelay.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `movementRelay.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `movementRelay.kafka.brokers` | `""` | Broker address(es). Empty: `trustConsumer.kafka.brokers`. |
| `movementRelay.kafka.topic` | `""` | Train Movements topic. Empty: `trustConsumer.kafka.topic`. |
| `movementRelay.kafka.consumerGroup` | `""` | RDM-issued consumer group id (`SC-...`). Empty: `trustConsumer.kafka.consumerGroup`. |
| `movementRelay.kafka.saslMechanism` | `""` | SASL mechanism. Empty: `trustConsumer.kafka.saslMechanism`. |
| `movementRelay.kafka.autoOffsetReset` | `earliest` | Where to start when the consumer group has no committed offset: `earliest` replays the oldest retained message (downstream dedup keys absorb the duplicates), `latest` skips to new messages. |
| `movementRelay.kafka.saslUsername` | `""` | movement-relay's own SASL username, rendered into the chart Secret as `movement-relay-kafka-sasl-username`. With this, `saslPassword` and `existingSecret` all empty, trust-consumer's credential is used. |
| `movementRelay.kafka.saslPassword` | `""` | movement-relay's own SASL password (`movement-relay-kafka-sasl-password`). |
| `movementRelay.kafka.existingSecret` | `""` | Read movement-relay's own SASL credential from this pre-existing Secret. |
| `movementRelay.kafka.existingSecretUsernameKey` | `movement-relay-kafka-sasl-username` | Key for movement-relay's own SASL username. |
| `movementRelay.kafka.existingSecretPasswordKey` | `movement-relay-kafka-sasl-password` | Key for movement-relay's own SASL password. |
| `movementRelay.streamLagPollSecs` | `30` | How often consumer-group lag on the stream is polled for the lag gauge. |
| `movementRelay.deadLetterMaxAgeSecs` | `86400` | Dead letters older than this are deleted by movement-relay (`XTRIM MINID`), under the TRUST 1-day retention safeguard. 3600 to 86400; the render fails outside that range. |
| `movementRelay.streamMaxLen` | `1048576` | `MAXLEN ~` cap on the `movement-events` stream, in entries: about 24 hours of traffic and the consumers' only replay window. Move `redis.maxmemory`/`redis.resources` with it (about 100 MiB per 100,000 entries). Minimum 1000. |
| `movementRelay.healthPort` | `8083` | Port for `/healthz` (readiness: partition assignment confirmed) and `/livez` (liveness). |
| `movementRelay.progressStallSecs` | `900` | `/livez` answers 503 once no relay-loop iteration has completed for this many seconds (waiting for Kafka never counts). |
| `movementRelay.metricsPort` | `9094` | Prometheus `/metrics` port. |
| `movementRelay.logLevel` | `info` | `RUST_LOG` value. |
| `movementRelay.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `movementRelay.resources` | requests `100m`/`128Mi`, limit `192Mi` | Container resource requests/limits. |
| `movementRelay.nodeSelector` | `{}` | Pod node selector. |
| `movementRelay.tolerations` | `[]` | Pod tolerations. |
| `movementRelay.affinity` | `{}` | Pod affinity rules. |
| `movementRelay.podAnnotations` | `{}` | Pod annotations. |
| `movementRelay.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### darwinRelay

The Darwin Push Port relay (DS-152; [docs/darwin-pushport.md](../../docs/darwin-pushport.md)):
it reads the Rail Data Marketplace's Push Port JSON topic and produces the
parts DS keeps (schedules, platforms with their confirmed and suppressed
flags, forecasts, associations, cancellations) on `ds:ingest:darwin`, which
the ingest-writer applies. Off by default. Enabling it needs
`ingestWriter.streams.darwin: apply` and `redis.acl.clients.darwinRelay`;
the render fails otherwise. Every Kafka setting comes from the Secret
`darwinRelay.kafka.existingSecret`.

| Key | Default | Description |
|---|---|---|
| `darwinRelay.enabled` | `false` | Deploy darwin-relay. |
| `darwinRelay.image.repository` | `ghcr.io/fasterspeeding/distant-signal/darwin-relay` | darwin-relay image repository. |
| `darwinRelay.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `darwinRelay.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `darwinRelay.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `darwinRelay.kafka.existingSecret` | `distant-signal-darwin-pushport` | The Secret holding the Push Port's Kafka connection, one key per setting. Never rendered by this chart. |
| `darwinRelay.kafka.keys.bootstrapServer` / `.topic` / `.consumerGroup` / `.username` / `.password` / `.saslMechanism` / `.securityProtocol` / `.saslConfig` | `kafka-bootstrap-server`, `kafka-topic`, `consumer-group`, `username`, `password`, `sasl-mechanism`, `security-protocol`, `sasl-config` | That Secret's key names. `saslConfig` (the Java client's JAAS line) is optional and only a fallback for the user name and password. |
| `darwinRelay.kafka.autoOffsetReset` | `latest` | Where to start when the group has no committed offset. `latest`: the Push Port is current state, and `earliest` would replay the topic's whole retention on the first start. |
| `darwinRelay.kafka.brokerPorts` | `[9092]` | The bootstrap server's TCP port(s), for the egress policy when `networkPolicy.egress.internetPorts` restricts ports (the address is only in the Secret). |
| `darwinRelay.batchMaxSecs` | `30` | A batch is published this many seconds after its first record. At least `minBatchMaxSecs`. |
| `darwinRelay.minBatchMaxSecs` | `30` | The batch interval `ds:ingest:darwin`'s `MAXLEN` is budgeted for (two hours of writer outage); the render refuses a smaller `batchMaxSecs`. |
| `darwinRelay.batchMaxRecords` | `5000` | ... or once it holds this many Kafka records. |
| `darwinRelay.healthPort` | `8083` | Port for `/healthz` (readiness: partitions assigned) and `/livez` (liveness). |
| `darwinRelay.progressStallSecs` | `900` | `/livez` answers 503 once no relay-loop iteration has completed for this many seconds (waiting for Kafka or out a Redis outage never counts). |
| `darwinRelay.metricsPort` | `9095` | Prometheus `/metrics` port. |
| `darwinRelay.logLevel` | `info` | `RUST_LOG` value. |
| `darwinRelay.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `darwinRelay.resources` | requests `100m`/`128Mi`, limit `384Mi` | Container resource requests/limits. |
| `darwinRelay.nodeSelector` | `{}` | Pod node selector. |
| `darwinRelay.tolerations` | `[]` | Pod tolerations. |
| `darwinRelay.affinity` | `{}` | Pod affinity rules. |
| `darwinRelay.podAnnotations` | `{}` | Pod annotations. |
| `darwinRelay.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### frontend

| Key | Default | Description |
|---|---|---|
| `frontend.image.repository` | `ghcr.io/fasterspeeding/distant-signal/frontend` | frontend image repository. |
| `frontend.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `frontend.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `frontend.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `frontend.replicaCount` | `1` | Safe to raise, with one documented caveat. frontend/lib/liveDataCache.ts keeps a process-local stale-data cache so a backend outage shows the last-known line status instead of an error page (docs/superpowers/specs/2026-09-02-frontend-disconnect-reconnect-ux-design.md). That cache is per-pod: with more than one replica, during an outage one visitor may get stale-but-useful content from a warm pod while another gets the auto-retrying error page from a cold one. Each pod stays internally consistent and no stale data crosses users (entries are session-scoped), so this is a degraded-experience caveat, not a correctness one -- deliberately documented rather than blocked, unlike postgresql.replicaCount above. |
| `frontend.service.type` | `ClusterIP` | Service type. |
| `frontend.service.port` | `3000` | Service and container port. |
| `frontend.probes.path` | `/healthz` | Probe path: the dependency-free `frontend/app/healthz/route.ts`, which never calls the api (probing `/` restarted the frontend whenever the api was down). |
| `frontend.probes.readiness.periodSeconds` | `10` | Readiness probe period. |
| `frontend.probes.readiness.failureThreshold` | `3` | Readiness probe failures allowed. |
| `frontend.probes.readiness.timeoutSeconds` | `3` | Readiness probe timeout. |
| `frontend.probes.liveness.periodSeconds` | `10` | Liveness probe period. |
| `frontend.probes.liveness.failureThreshold` | `3` | Liveness probe failures allowed. |
| `frontend.probes.liveness.timeoutSeconds` | `3` | Liveness probe timeout. |
| `frontend.siteUrl` | `""` | The deployment's public origin (e.g. `https://rail.example.com`), used for share and invite links and same-origin checks. Set it in production. Empty derives it from `ingress.frontend.host` when this chart's ingress publishes the frontend. |
| `frontend.apiBaseUrl` | `""` | Override `API_BASE_URL`. Empty uses the in-cluster api Service. |
| `frontend.legalPagesPublished` | `false` | Publish the DRAFT legal pages (`/privacy`, `/terms`, `/cookies`, `/contact`) and their footer links. Off by default: the text needs the operator's and a lawyer's review, and the operator values in `frontend/lib/legal.ts` must be filled in first. Even when `true`, the pages stay 404 while any placeholder is left in that file. `/attribution` is always public. |
| `frontend.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `frontend.resources` | requests `100m`/`256Mi`, limit `768Mi` | Container resource requests/limits. |
| `frontend.nodeSelector` | `{}` | Pod node selector. |
| `frontend.tolerations` | `[]` | Pod tolerations. |
| `frontend.affinity` | `{}` | Pod affinity rules. |
| `frontend.podAnnotations` | `{}` | Pod annotations. |
| `frontend.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

The frontend is the one workload with `readOnlyRootFilesystem: false`:
`next start` writes its incremental cache under `.next/cache`.

### railMcp

**This chart does not deploy or configure the derived MCP service
("distant-signal-mcp", a fork of train-mcp) itself.** That project has its
own repository, its own CI/tests, and its own Helm chart
(`Distant-Signal-MCP`). Deploy it as its **own, separate Helm release**, then
set the values below. `railMcp` now only configures the frontend's **in-app
chat** (`/chat`) link to that service, plus the connector URL
(`<publicUrl>/mcp`) shown on the `/connect-claude` instructions page and in
`/chat`'s "use it in your own assistant" section. Only the browser talks to the MCP
service (the `/chat` MCP client, its `/chat/callback` OAuth exchange, and the
CSP `connect-src` entry that allows both); the frontend pod makes no
server-to-server call to it.

The MCP service handles its own login as an Authentik OIDC client, so the
old DS consent bridge (`/connect-claude/authorize`) and its values
(`railMcp.baseUrl`, `railMcp.internalCompleteToken`,
`railMcp.existingSecret`, `railMcp.existingSecretInternalCompleteTokenKey`)
were removed on 2026-09-29. The chart has no values schema, so leftover
copies of those keys in an existing values file are ignored; delete them at
your convenience. Everything below is optional and off by default: leaving
`railMcp.enabled` at `false` renders no railMcp env vars at all.

| Key | Default | Description |
|---|---|---|
| `railMcp.enabled` | `false` | Point the frontend's in-app chat (and the `/connect-claude` instructions page) at a separately, externally-deployed instance of the derived MCP service. |
| `railMcp.publicUrl` | `""` | The other release's own `PUBLIC_URL`, surfaced to the browser as `NEXT_PUBLIC_RAILMCP_PUBLIC_URL` (read at request time). `/chat` connects to `<publicUrl>/mcp` and the CSP `connect-src` allows its origin. Must match what that release was configured with. Blank: `/chat` reports that chat is not configured. |

### pollers

Keys below exist under each of `pollers.incidents`, `pollers.stations`,
`pollers.tocs`, `pollers.ldbws` and `pollers.tfl`; the tfl-only and
ldbws-only rows are marked. The three island-of-Ireland pollers are
separate top-level values (`pollerIrishRailGtfs`, `pollerIrishRailLive`,
`pollerNirStations`), documented in `values.yaml`.

| Key | Default | Description |
|---|---|---|
| `pollers.<name>.enabled` | `false` | Deploy this poller. All five are off by default. |
| `pollers.<name>.image.repository` | `ghcr.io/fasterspeeding/distant-signal/poller-<name>` | Poller image repository. |
| `pollers.<name>.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollers.<name>.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `pollers.<name>.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollers.<name>.baseUrl` | `""` (tfl: `https://api.tfl.gov.uk`) | Upstream feed base URL. Required when enabled; empty aborts the render. |
| `pollers.<name>.baseUrlEnvVar` | per-poller | Env var the binary reads the base URL from. Do not change. |
| `pollers.<name>.pollIntervalSecs` | 300 / 86400 / 86400 / 60 / 300 | Poll cadence (incidents / stations / tocs / ldbws / tfl). |
| `pollers.<name>.apiKey` | `""` | RDM API key (tfl: TfL subscription key). Rendered into the chart Secret when `existingSecret` is empty. |
| `pollers.<name>.existingSecret` | `""` | Read the API key AND the internal-oauth username/password below from this pre-existing Secret instead. |
| `pollers.<name>.existingSecretApiKeyKey` | `rdm-<name>-api-key` | Key within `pollers.<name>.existingSecret`. |
| `pollers.<name>.logLevel` | `info` | `RUST_LOG` value. |
| `pollers.<name>.extraEnv` | `[]` (tfl: `TFL_MODES`) | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `pollers.<name>.resources` | requests `25m`/`64Mi`-`128Mi`, limit `128Mi`-`192Mi` | Container resource requests/limits. |
| `pollers.<name>.nodeSelector` | `{}` | Pod node selector. |
| `pollers.<name>.tolerations` | `[]` | Pod tolerations. |
| `pollers.<name>.affinity` | `{}` | Pod affinity rules. |
| `pollers.<name>.podAnnotations` | `{}` | Pod annotations. |
| `pollers.<name>.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `pollers.stations.ingest.database.maxConnections` | `1` | stations only: its Postgres pool under `db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `pollers.incidents.ingest.rowHeartbeat` | `true` | incidents only (plan 2c.6): `INCIDENTS_ROW_HEARTBEAT` for poller-incidents' writes. `false` writes only changed rows (about 600k fewer `incidents` updates a day); readers show the feed's snapshot time. Rendered only when `false`. Rollback: `true`. |
| `pollers.incidents.ingest.database.maxConnections` | `2` | incidents only: its Postgres pool under `db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `pollers.tfl.apiKeyEnvVar` | `TFL_APP_KEY` | tfl only: env var the key is passed in (the RDM pollers default to `RDM_API_KEY`). Do not change. |
| `pollers.tfl.stops.enabled` | `false` | tfl only: the daily Tube, DLR and tram stops job (`TFL_STOPS_ENABLED`; docs/superpowers/specs/2026-10-20-tfl-stop-lookup-design.md), which reads TfL's StopPoint API with the same key and XADDs one `tfl-stops/1` snapshot to `ds:ingest:tfl-stops` as the `poller-tfl` ACL user. Needs `ingestWriter.streams.tfl-stops: apply` (the render fails otherwise). |
| `pollers.tfl.stops.intervalSecs` | `86400` | tfl only: seconds between stops runs (`TFL_STOPS_INTERVAL_SECS`). |
| `pollers.tfl.stops.maxDropFraction` | `0.2` | tfl only: the stops feed guard (`TFL_STOPS_MAX_DROP_FRACTION`): a run whose stop count fell by more than this fraction of the last snapshot's is refused (`tfl_stops_refused_total{reason="sharp_drop"}`) and the stored stops stay. |
| `pollers.tfl.stops.routes` | `false` | tfl only, with `pollers.tfl.stops.enabled`: the daily route-sequences job (`TFL_ROUTES_ENABLED`, DS-151), each Tube, DLR and tram line's routes in stop order, with their timetabled running times between stops (DS-206: one `GET /Line/{id}/Timetable/{from}/to/{to}` per route, about 80 a run; `tfl_route_timetables_failed_total`; a 404, TfL publishing none for that route, counts `tfl_route_timetables_unpublished_total` instead, and that route's untimed steps take the reverse direction's times, `tfl_route_steps_mirrored_total`; 429s wait what TfL asks, `tfl_fetch_retries_total`), as `tfl-line-routes/1` on `ds:ingest:tfl-stops`, at the stops job's interval and drop fraction (`tfl_routes_refused_total{reason}`; `cycle="tfl-routes"`). The planner's Tube access legs time rides along them and charge a change between branches. Re-run the grants job after the migration (`tfl_line_routes`, the writer's `tfl-line-routes` freshness row). |
| `pollers.tfl.stops.mode` | `deployment` | tfl only: where the daily stops and routes jobs run. `deployment`: in the poller-tfl Deployment beside the status poll (`TFL_JOBS=all`). `cronjob`: a CronJob of their own, `<fullname>-poller-tfl-daily` (`TFL_JOBS=daily`, `RUN_ONCE`: each enabled job once, each skipping while its snapshot is fresh, each waiting for its snapshot to reach the stream), while the Deployment runs the status poll alone (`TFL_JOBS=status`). Needs `pollers.tfl.stops.enabled`. As a CronJob their DistantSignalPollerStale rules (`tfl-stops`, `tfl-routes`) are off (no `/metrics`); the scheduled-job alerts and the `tfl-stops` and `tfl-line-routes` ingest_freshness sources (2 days, in both modes) cover them. See `docs/scheduled-jobs.md`. |
| `pollers.tfl.stops.cron.schedule` | `"40 5 * * *"` | The run's schedule: 05:40 UTC, clear of the 03:00 dump, the 03:30–04:40 scheduled pollers and the 05:00 backups, and before the morning peak. |
| `pollers.tfl.stops.cron.timeZone` | `Etc/UTC` | The schedule's time zone. Required. |
| `pollers.tfl.stops.cron.suspend` | `false` | Suspend the CronJob (the status poll keeps running). |
| `pollers.tfl.stops.cron.startingDeadlineSeconds` | `3600` | A start missed by more than this is skipped (the freshness alerts allow 2 days). |
| `pollers.tfl.stops.cron.activeDeadlineSeconds` | `2700` | Kill a run, or one still Pending, after this long: about 110 TfL calls plus the waits TfL asks for on a 429. |
| `pollers.tfl.stops.cron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1; a job that already succeeded skips). Exit 3 (a feed guard refused a snapshot) and 137 (OOMKilled) fail the Job at once. |
| `pollers.tfl.stops.cron.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `pollers.tfl.stops.cron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `pollers.tfl.stops.cron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after it ends. |
| `pollers.tfl.stops.cron.skipIfFresherThanSecs` | `43200` | `RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS`: a job whose own snapshot on the stream is newer than this fetches nothing. |
| `pollers.tfl.stops.cron.resources` | requests `10m`/`96Mi`/ephemeral `16Mi`, limits `256Mi`/ephemeral `256Mi` | The run's container resources (the routes job's peak). |
| `pollers.ldbws.numRows` | `10` | ldbws only: LDBWS `numRows` query parameter. |
| `pollers.ldbws.hourlyRequestBudget` | `0` | ldbws only (LEG-18): max LDBWS requests per rolling hour, spread evenly over cycles; skipped stations count in `ldbws_budget_skipped_polls_total`. `0` = no budget, env not rendered. |
| `pollers.ldbws.samplePinnedLinesOnly` | `false` | ldbws only (LEG-18): sample only stations on lines some user has pinned. |
| `pollers.ldbws.sampleMaxStations` | `0` | ldbws only (LEG-18): cap on sample stations, chosen line-fairly by api, most-pinned lines first. `0` = no cap. |
| `pollers.ldbws.trackedStops.enabled` | `false` | ldbws only (DS-127): also poll the boards of tracked trains' own stops, narrowed to each train's booked time. Off by default: it adds LDBWS requests. Needs the grants job to have applied the writer's `tracked-stop-boards` freshness row policy (DS-183); without it every tracked-stop entry dead-letters. |
| `pollers.ldbws.trackedStops.maxRequestsPerCycle` | `20` | ldbws only (DS-127): the tracked-stop lane's own budget, requests a cycle, soonest departure first. |
| `pollers.ldbws.trackedStopArrivals.enabled` | `false` | ldbws only (DS-152): also poll the LDBWS arrivals product for tracked trains' alighting stops with a booked arrival, narrowed to that arrival, so a terminating or set-down-only stop gets a live platform. Its own RDM subscription. Needs `ingestWriter.streams.station-samples: apply`. |
| `pollers.ldbws.trackedStopArrivals.maxRequestsPerCycle` | `6` | ldbws only (DS-152): the arrivals sub-lane's own budget, requests a cycle, soonest arrival first. It also gets 3 s of the cycle, taken from the departure lane's 10 s, so the 45 s rotation and both lanes stay under the 60 s interval. |
| `pollers.ldbws.trackedStopArrivals.secretName` | `distant-signal-ldbws-arrivals` | The existing Secret with the arrivals product's URL and consumer key (never rendered by the chart). |
| `pollers.ldbws.trackedStopArrivals.urlKey` | `url` | Its key with the product URL (`LDBWS_ARRIVALS_URL`): the product's base, an operation URL ending in `/Get...`, or a URL with a `{crs}` placeholder. |
| `pollers.ldbws.trackedStopArrivals.consumerKeyKey` | `consumer-key` | Its key with the consumer key, sent as `x-apikey` (`LDBWS_ARRIVALS_CONSUMER_KEY`). |
| `pollers.ldbws.trackedStopArrivals.consumerSecretKey` | `consumer-secret` | Its key with the consumer secret (`LDBWS_ARRIVALS_CONSUMER_SECRET`, optional): wired but not sent, as RDM API products authenticate with the consumer key alone. |
| `pollers.ldbws.internalReads.database.maxConnections` | `1` | ldbws only: its Postgres pool under `db` (`DATABASE_MAX_CONNECTIONS`; spec §6.6). |
| `pollers.tocs.mode` | `deployment` | tocs only: `deployment` (an always-on pod) or `cronjob` (a CronJob, `cron` below: each run polls once with `RUN_ONCE`, waits for its snapshot to reach the stream, and exits). Exactly one renders. As a CronJob it has no `/metrics`, so its DistantSignalPollerStale rule is off and DistantSignalPollerFailing can't see it; the scheduled-job alerts and the `tocs` ingest_freshness source cover it. `pollers.stations.mode` is the same for poller-stations; the other pollers refuse `cronjob` until their binaries wait for their writes. See `docs/scheduled-jobs.md`. |
| `pollers.tocs.cron.schedule` | `"40 4 * * *"` | tocs only, `mode: cronjob`: the run's schedule (daily, clear of the 03:00 and 05:00 UTC backups). |
| `pollers.tocs.cron.timeZone` | `Etc/UTC` | The schedule's time zone. Required. |
| `pollers.tocs.cron.suspend` | `false` | Suspend the CronJob (a run already going is not stopped). |
| `pollers.tocs.cron.startingDeadlineSeconds` | `3600` | A start missed by more than this is skipped. |
| `pollers.tocs.cron.activeDeadlineSeconds` | `900` | Kill a run, or one still Pending, after this long. |
| `pollers.tocs.cron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1). Exit 3 (the feed refused) and 137 (OOMKilled) fail the Job at once. |
| `pollers.tocs.cron.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `pollers.tocs.cron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `pollers.tocs.cron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after it ends. |
| `pollers.tocs.cron.skipIfFresherThanSecs` | `43200` | `RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS`: a run finding the TOC list landed less than this long ago fetches nothing, so a doubled or manual run spends no RDM request. `0` always fetches. |
| `pollers.tocs.cron.resources` | requests `5m`/`16Mi`/ephemeral `16Mi`, limits `128Mi`/ephemeral `256Mi` | The run's container resources (`pollers.tocs.resources` is the Deployment's). |
| `pollers.stations.mode` | `deployment` | stations only: as `pollers.tocs.mode`. Its write is one awaited transaction inside the run, so a run exits once its stations are committed. As a CronJob the `stations` ingest_freshness source (48h) and the scheduled-job alerts cover it. |
| `pollers.stations.cron.schedule` | `"20 4 * * *"` | stations only, `mode: cronjob`: the run's schedule (daily; clear of the 03:00 and 05:00 UTC backups and the other scheduled pollers). |
| `pollers.stations.cron.timeZone` | `Etc/UTC` | The schedule's time zone. Required. |
| `pollers.stations.cron.suspend` | `false` | Suspend the CronJob. |
| `pollers.stations.cron.startingDeadlineSeconds` | `3600` | A start missed by more than this is skipped. |
| `pollers.stations.cron.activeDeadlineSeconds` | `1800` | Kill a run, or one still Pending, after this long: the fetch plus the write's 15-minute retry budget. |
| `pollers.stations.cron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1). Exit 3 (the write refused the data) and 137 (OOMKilled) fail the Job at once. |
| `pollers.stations.cron.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `pollers.stations.cron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `pollers.stations.cron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after it ends. |
| `pollers.stations.cron.skipIfFresherThanSecs` | `43200` | `RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS`: a run finding the stations landed less than this long ago fetches nothing. `0` always fetches. |
| `pollers.stations.cron.resources` | requests `5m`/`128Mi`/ephemeral `16Mi`, limits `256Mi`/ephemeral `256Mi` | The run's container resources (the feed's parse peaked at 168Mi). |

### pollerIrishRailGtfs

Island-of-Ireland stations and lines from Transport for Ireland's public
Irish Rail GTFS zip. Off by default; no API key needed.

| Key | Default | Description |
|---|---|---|
| `pollerIrishRailGtfs.enabled` | `false` | Deploy the poller (off by default, decision D8). It writes only to the `ds:ingest:ioi-gtfs` stream (ingest plan 3c.2: `REDIS_URL`, the user in `redis.acl.clients.pollerIrishRailGtfs`; no api route or OAuth), so turn on `ingestWriter.streams.ioi-gtfs: apply` with it. |
| `pollerIrishRailGtfs.image.repository` | `ghcr.io/fasterspeeding/distant-signal/poller-irish-rail-gtfs` | Image repository. |
| `pollerIrishRailGtfs.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollerIrishRailGtfs.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `pollerIrishRailGtfs.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollerIrishRailGtfs.gtfsUrl` | `https://www.transportforireland.ie/transitData/Data/GTFS_Irish_Rail.zip` | GTFS zip URL (public, key-free). |
| `pollerIrishRailGtfs.pollIntervalSecs` | `86400` | Poll cadence. The feed's real refresh cadence is unknown; 24h matches the other reference-data pollers. |
| `pollerIrishRailGtfs.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`): one download plus up to 15 minutes of ingest retries. |
| `pollerIrishRailGtfs.logLevel` | `info` | `RUST_LOG` value. |
| `pollerIrishRailGtfs.metricsPort` | `9091` | Prometheus `/metrics` port. |
| `pollerIrishRailGtfs.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `pollerIrishRailGtfs.feedMaxDropFraction` | `0.3` | `FEED_MAX_DROP_FRACTION`: refuse to publish a feed whose station or line count fell by more than this fraction since the last published one (in-memory baseline; the first feed after a start always publishes). `1.0` turns it off; an empty feed is always refused. |
| `pollerIrishRailGtfs.resources` | requests `100m`/`256Mi`, limit `768Mi` | Container resource requests/limits. The whole GTFS archive is held in memory. |
| `pollerIrishRailGtfs.nodeSelector` | `{}` | Pod node selector. |
| `pollerIrishRailGtfs.tolerations` | `[]` | Pod tolerations. |
| `pollerIrishRailGtfs.affinity` | `{}` | Pod affinity rules. |
| `pollerIrishRailGtfs.podAnnotations` | `{}` | Pod annotations. |
| `pollerIrishRailGtfs.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### pollerIrishRailLive

Live departure samples from Irish Rail's public realtime API. Off by
default; no API key needed.

| Key | Default | Description |
|---|---|---|
| `pollerIrishRailLive.enabled` | `false` | Deploy the poller (off by default, decision D8). It writes only to the `ds:ingest:ioi-live` stream (ingest plan 3c.2: `REDIS_URL`, the user in `redis.acl.clients.pollerIrishRailLive`; no api route or OAuth), so turn on `ingestWriter.streams.ioi-live: apply` with it. |
| `pollerIrishRailLive.image.repository` | `ghcr.io/fasterspeeding/distant-signal/poller-irish-rail-live` | Image repository. |
| `pollerIrishRailLive.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollerIrishRailLive.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `pollerIrishRailLive.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollerIrishRailLive.irishRailBaseUrl` | `http://api.irishrail.ie/realtime/realtime.asmx` | Irish Rail realtime service root (public, key-free). |
| `pollerIrishRailLive.pollIntervalSecs` | `300` | Poll cadence. Conservative: the API's rate limits are unknown and every station is sampled. |
| `pollerIrishRailLive.stationCodesOverride` | `""` | Comma-separated station-code allowlist. Empty polls every station the API lists. |
| `pollerIrishRailLive.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`): one sampling cycle plus its ingest retries. |
| `pollerIrishRailLive.logLevel` | `info` | `RUST_LOG` value. |
| `pollerIrishRailLive.metricsPort` | `9091` | Prometheus `/metrics` port. |
| `pollerIrishRailLive.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `pollerIrishRailLive.resources` | requests `25m`/`64Mi`, limit `256Mi` | Container resource requests/limits. |
| `pollerIrishRailLive.nodeSelector` | `{}` | Pod node selector. |
| `pollerIrishRailLive.tolerations` | `[]` | Pod tolerations. |
| `pollerIrishRailLive.affinity` | `{}` | Pod affinity rules. |
| `pollerIrishRailLive.podAnnotations` | `{}` | Pod annotations. |
| `pollerIrishRailLive.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### pollerNirStations

Northern Ireland Railways stations and halts from OpenDataNI's public CSVs.
Off by default; no API key needed.

| Key | Default | Description |
|---|---|---|
| `pollerNirStations.enabled` | `false` | Deploy the poller (off by default, decision D8). It writes only to the `ds:ingest:ioi-nir` stream (ingest plan 3c.2: `REDIS_URL`, the user in `redis.acl.clients.pollerNirStations`; no api route or OAuth), so turn on `ingestWriter.streams.ioi-nir: apply` with it. |
| `pollerNirStations.image.repository` | `ghcr.io/fasterspeeding/distant-signal/poller-nir-stations` | Image repository. |
| `pollerNirStations.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollerNirStations.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `pollerNirStations.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollerNirStations.stationsCsvUrl` | OpenDataNI `translink_rail_stations.csv` | Stations CSV URL (see values.yaml for the full URL). |
| `pollerNirStations.haltsCsvUrl` | OpenDataNI `translink_halts.csv` | Halts CSV URL (see values.yaml for the full URL). |
| `pollerNirStations.pollIntervalSecs` | `86400` | Poll cadence. The CSVs change irregularly. |
| `pollerNirStations.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`): one download plus up to 15 minutes of ingest retries. |
| `pollerNirStations.logLevel` | `info` | `RUST_LOG` value. |
| `pollerNirStations.metricsPort` | `9091` | Prometheus `/metrics` port. |
| `pollerNirStations.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it; the rest follow the chart's own. |
| `pollerNirStations.resources` | requests `25m`/`64Mi`, limit `256Mi` | Container resource requests/limits. |
| `pollerNirStations.nodeSelector` | `{}` | Pod node selector. |
| `pollerNirStations.tolerations` | `[]` | Pod tolerations. |
| `pollerNirStations.affinity` | `{}` | Pod affinity rules. |
| `pollerNirStations.podAnnotations` | `{}` | Pod annotations. |
| `pollerNirStations.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |

### pollerTransitSchedule

Transit networks that are neither National Rail nor TfL, Manchester
Metrolink first (docs/superpowers/specs/2026-10-09-transit-networks-design.md).
One Deployment per enabled network, `<release>-poller-transit-schedule-<network>`
(`strategy: Recreate`), from one image. Each XADDs its network's BODS
timetable (GTFS) and disruptions (SIRI-SX) to `ds:ingest:transit-<network>`
as its own Redis user; no database, api or key. The rollout order is the
spec's §15.1.

| Key | Default | Description |
|---|---|---|
| `pollerTransitSchedule.image.repository` | `ghcr.io/fasterspeeding/distant-signal/poller-transit-schedule` | Image repository. |
| `pollerTransitSchedule.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `pollerTransitSchedule.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `pollerTransitSchedule.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `pollerTransitSchedule.progressStallSecs` | `3600` | `/livez` stall window (see `workerHealth`): one GTFS download, its parse and the produce. |
| `pollerTransitSchedule.logLevel` | `info` | `RUST_LOG` value. |
| `pollerTransitSchedule.metricsPort` | `9091` | Prometheus `/metrics` port. |
| `pollerTransitSchedule.networks.metrolink.enabled` | `false` | Deploy Metrolink's producer. Needs `redis.acl.clients.pollerTransitMetrolink: true` and `ingestWriter.streams.transit-metrolink: apply`. |
| `pollerTransitSchedule.networks.metrolink.adapter` | `gtfs` | `TRANSIT_ADAPTER`: `gtfs` or `txc`; must be the network file's. |
| `pollerTransitSchedule.networks.metrolink.gtfsUrl` | BODS North West GTFS | The timetable zip (see values.yaml for the URL). |
| `pollerTransitSchedule.networks.metrolink.gtfsMaxBytes` | `419430400` | The GTFS download's byte cap. |
| `pollerTransitSchedule.networks.metrolink.situationsUrl` | BODS SIRI-SX bulk archive | The disruptions archive (a 302 to a signed URL on `download.bus-data.dft.gov.uk`). |
| `pollerTransitSchedule.networks.metrolink.naptanUrl` | NaPTAN access nodes | `{area}` is the network file's NaPTAN area. |
| `pollerTransitSchedule.networks.metrolink.timetableIntervalSecs` | `86400` | Seconds between timetable fetches. |
| `pollerTransitSchedule.networks.metrolink.situationsIntervalSecs` | `300` | Seconds between SIRI-SX fetches. |
| `pollerTransitSchedule.networks.metrolink.situationsEnabled` | `false` | Run the SIRI-SX loop. The ingest-writer applies `transit-situations/1` (plan 4.3); a writer image older than that dead-letters every snapshot, so turn it on only once the writer has it. |
| `pollerTransitSchedule.networks.metrolink.horizonDays` | `60` | Days of service dates stored after today. |
| `pollerTransitSchedule.networks.metrolink.feedMaxDropFraction` | `0.3` | A feed whose line, stop or trip count fell by more than this since the last commit is refused. |
| `pollerTransitSchedule.networks.metrolink.timetableRefresh` | `""` | `TRANSIT_TIMETABLE_REFRESH`: a one-shot timetable refresh. Set a new token (a date or a counter; 1-64 of `A-Z a-z 0-9 . _ : -`) and the producer fetches and publishes the timetable at start instead of waiting out the day. The commit records the token, so it is answered once; change it to ask again, never unset it. Empty: none. |
| `pollerTransitSchedule.networks.metrolink.workDirSizeLimit` | `512Mi` | The `/work` emptyDir's size limit (the GTFS zip). |
| `pollerTransitSchedule.networks.metrolink.resources` | requests `100m`/`192Mi`, limit `768Mi` | Container resource requests/limits. |
| `pollerTransitSchedule.networks.metrolink.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it. |
| `pollerTransitSchedule.networks.metrolink.nodeSelector` | `{}` | Pod node selector. |
| `pollerTransitSchedule.networks.metrolink.tolerations` | `[]` | Pod tolerations. |
| `pollerTransitSchedule.networks.metrolink.affinity` | `{}` | Pod affinity rules. |
| `pollerTransitSchedule.networks.metrolink.podAnnotations` | `{}` | Pod annotations. |
| `pollerTransitSchedule.networks.metrolink.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `pollerTransitSchedule.networks.metrolink.timetableMode` | `deployment` | `deployment` (one always-on pod, `TRANSIT_LOOPS=both`) or `cronjob`: the timetable as a CronJob (`timetableCron`; `TRANSIT_LOOPS=timetable`, `RUN_ONCE`) and, while `situationsEnabled`, the Deployment (same name) running the situations loop alone (`TRANSIT_LOOPS=situations`, `situationsResources`). Each loop runs in exactly one workload. In cron mode the timetable's DistantSignalPollerStale rule is off (no `/metrics`); the scheduled-job alerts and the `transit-metrolink-timetable` source (36h) cover it, and the situations Deployment keeps its gauges and the `transit-metrolink-situations` source (30m). See `docs/scheduled-jobs.md`. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.schedule` | `"30 3 * * *"` | The run's schedule: after the 03:00 UTC dump, apart from the other scheduled pollers (04:00, 04:20, 04:40) and the 05:00 UTC backups. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.timeZone` | `Etc/UTC` | The schedule's time zone. Required. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.suspend` | `false` | Suspend the CronJob (the situations Deployment keeps running). |
| `pollerTransitSchedule.networks.metrolink.timetableCron.startingDeadlineSeconds` | `3600` | A start missed by more than this is skipped (the freshness alert allows 36h). |
| `pollerTransitSchedule.networks.metrolink.timetableCron.activeDeadlineSeconds` | `1800` | Kill a run, or one still Pending, after this long. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1). Exit 3 (the feed guard refused the timetable) and 137 (OOMKilled) fail the Job at once. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after it ends. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.skipIfFresherThanSecs` | `43200` | `RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS`: a run finding a timetable commit newer than this fetches nothing; an unanswered `timetableRefresh` token overrides it. |
| `pollerTransitSchedule.networks.metrolink.timetableCron.resources` | requests `100m`/`128Mi`/ephemeral `192Mi`, limits `512Mi`/ephemeral `768Mi` | The run's container resources: the parse streams (48 MiB peak), plus ephemeral storage for the ~159 MB zip in `/work`. |
| `pollerTransitSchedule.networks.metrolink.situationsResources` | requests `10m`/`32Mi`, limit `256Mi` | The situations-only Deployment's resources, while `timetableMode: cronjob` and `situationsEnabled`. |
| `pollerTransitSchedule.networks.net.enabled` | `false` | Deploy Nottingham Express Transit's producer (docs/superpowers/specs/2026-10-24-nottingham-net-design.md). Needs `redis.acl.clients.pollerTransitNet: true`, `ingestWriter.streams.transit-net: apply` and, with an `ftp://` `txcUrl`, `txcCredentials.existingSecret`. |
| `pollerTransitSchedule.networks.net.adapter` | `txc` | `TRANSIT_ADAPTER`: `gtfs` or `txc`; must be the network file's (the producer refuses otherwise). |
| `pollerTransitSchedule.networks.net.txcUrl` | TNDS East Midlands over FTP | `TRANSIT_TXC_URL`: the TransXChange zip, filtered to NOC `NEXT` (`ftp://ftp.tnds.co.uk/EM.zip`; OGL). An `https://` zip of TXC documents works too, without a login. With `networkPolicy.egress.internetPorts` set, passive FTP also needs `networkPolicy.components.poller-transit-schedule.internetPorts: []`. |
| `pollerTransitSchedule.networks.net.txcMaxBytes` | `419430400` | The TXC download's byte cap. |
| `pollerTransitSchedule.networks.net.txcCredentials.existingSecret` | `""` | A Secret with the Traveline (TNDS) FTP login, as `TRANSIT_TXC_USERNAME` and `TRANSIT_TXC_PASSWORD`. Required while enabled with an `ftp://` `txcUrl`. Plain FTP sends it in clear; it is a free registration guarding nothing else. |
| `pollerTransitSchedule.networks.net.txcCredentials.usernameKey` | `username` | The Secret's key for the login name. |
| `pollerTransitSchedule.networks.net.txcCredentials.passwordKey` | `password` | The Secret's key for the password. |
| `pollerTransitSchedule.networks.net.situationsUrl` | BODS SIRI-SX bulk archive | Unused while `situationsEnabled` is off (BODS has no Nottingham participant). |
| `pollerTransitSchedule.networks.net.naptanUrl` | NaPTAN access nodes | `{area}` is the network file's NaPTAN area. |
| `pollerTransitSchedule.networks.net.timetableIntervalSecs` | `86400` | Seconds between timetable fetches (TNDS updates Monday to Thursday; the daily re-send keeps the freshness moving). |
| `pollerTransitSchedule.networks.net.situationsIntervalSecs` | `300` | Seconds between SIRI-SX fetches (unused while off). |
| `pollerTransitSchedule.networks.net.situationsEnabled` | `false` | Must stay off: NET's network file names no SIRI-SX participant, and the producer refuses to start with it on. Its stream is budgeted for the timetable only. |
| `pollerTransitSchedule.networks.net.horizonDays` | `60` | Days of service dates stored after today. |
| `pollerTransitSchedule.networks.net.feedMaxDropFraction` | `0.3` | As metrolink's. |
| `pollerTransitSchedule.networks.net.timetableRefresh` | `""` | A one-shot timetable refresh token, as metrolink's. |
| `pollerTransitSchedule.networks.net.workDirSizeLimit` | `512Mi` | The `/work` emptyDir's size limit (the TXC zip). |
| `pollerTransitSchedule.networks.net.resources` | requests `100m`/`192Mi`, limit `768Mi` | Container resource requests/limits. |
| `pollerTransitSchedule.networks.net.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it. |
| `pollerTransitSchedule.networks.net.nodeSelector` | `{}` | Pod node selector. |
| `pollerTransitSchedule.networks.net.tolerations` | `[]` | Pod tolerations. |
| `pollerTransitSchedule.networks.net.affinity` | `{}` | Pod affinity rules. |
| `pollerTransitSchedule.networks.net.podAnnotations` | `{}` | Pod annotations. |
| `pollerTransitSchedule.networks.net.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `pollerTransitSchedule.networks.tfl.enabled` | `false` | Deploy the producer of TfL's Underground, DLR and London Trams timetable (docs/superpowers/specs/2026-10-30-tfl-transit-design.md). Needs `redis.acl.clients.pollerTransitTfl: true` and `ingestWriter.streams.transit-tfl: apply`. |
| `pollerTransitSchedule.networks.tfl.adapter` | `txc` | `TRANSIT_ADAPTER`: must be the network file's (`txc`). |
| `pollerTransitSchedule.networks.tfl.txcUrl` | TfL's Journey Planner export | `TRANSIT_TXC_URL`: `https://tfl.gov.uk/tfl/syndication/feeds/journey-planner-timetables.zip` (weekly, no login); the network file names the inner rail zip. |
| `pollerTransitSchedule.networks.tfl.txcMaxBytes` | `419430400` | The download's byte cap (the export was 141 MB on 2026-10-26). |
| `pollerTransitSchedule.networks.tfl.txcCredentials.existingSecret` | `""` | Unused: TfL's export needs no login. |
| `pollerTransitSchedule.networks.tfl.txcCredentials.usernameKey` | `username` | Unused. |
| `pollerTransitSchedule.networks.tfl.txcCredentials.passwordKey` | `password` | Unused. |
| `pollerTransitSchedule.networks.tfl.situationsUrl` | BODS SIRI-SX bulk archive | Unused: the lines' status is TfL's own (poller-tfl). |
| `pollerTransitSchedule.networks.tfl.naptanUrl` | NaPTAN access nodes | `{area}` is the network file's NaPTAN area. |
| `pollerTransitSchedule.networks.tfl.timetableIntervalSecs` | `86400` | Seconds between timetable fetches (TfL updates weekly; the daily re-send keeps the freshness moving). |
| `pollerTransitSchedule.networks.tfl.situationsIntervalSecs` | `300` | Unused. |
| `pollerTransitSchedule.networks.tfl.situationsEnabled` | `false` | Must stay off: the network file names no SIRI-SX participant, and the producer refuses to start with it on. |
| `pollerTransitSchedule.networks.tfl.horizonDays` | `60` | Days of service dates stored after today (TfL's export runs about 60 days ahead). |
| `pollerTransitSchedule.networks.tfl.feedMaxDropFraction` | `0.3` | As metrolink's. |
| `pollerTransitSchedule.networks.tfl.timetableRefresh` | `""` | A one-shot timetable refresh token, as metrolink's. |
| `pollerTransitSchedule.networks.tfl.workDirSizeLimit` | `512Mi` | The `/work` emptyDir's size limit (the export). |
| `pollerTransitSchedule.networks.tfl.resources` | requests `100m`/`448Mi`, limit `1Gi` | Container resource requests/limits (332 MiB peak parsing TfL's 2026-10-26 export). |
| `pollerTransitSchedule.networks.tfl.extraEnv` | `[]` | Extra env vars for the container. One named like a chart-set var replaces it. |
| `pollerTransitSchedule.networks.tfl.nodeSelector` | `{}` | Pod node selector. |
| `pollerTransitSchedule.networks.tfl.tolerations` | `[]` | Pod tolerations. |
| `pollerTransitSchedule.networks.tfl.affinity` | `{}` | Pod affinity rules. |
| `pollerTransitSchedule.networks.tfl.podAnnotations` | `{}` | Pod annotations. |
| `pollerTransitSchedule.networks.tfl.podSecurityContext` | `{}` | Merged over the chart-wide pod securityContext defaults. |
| `pollerTransitSchedule.networks.tfl.timetableMode` | `deployment` | tfl only: `deployment` (one always-on pod, `TRANSIT_LOOPS=both`) or `cronjob` (a CronJob, `timetableCron`, whose run fetches and publishes one timetable and exits: `TRANSIT_LOOPS=timetable`, `RUN_ONCE`). Exactly one renders. tfl has no situations loop, so nothing of it stays always on (with `situationsEnabled` the chart would need a `situationsResources`, as Metrolink's; `net` refuses `cronjob`). As a CronJob its DistantSignalPollerStale rules are off and DistantSignalTransitFeedRefused can't see it (no `/metrics`): the scheduled-job alerts (a refused feed exits 3 and fails the Job) and its `transit-tfl-timetable` ingest_freshness source (36h) cover it. See `docs/scheduled-jobs.md`. |
| `pollerTransitSchedule.networks.tfl.timetableCron.schedule` | `"0 4 * * *"` | The run's schedule: after the 03:00 UTC dump, before the 05:00 UTC backups, apart from the other scheduled pollers. |
| `pollerTransitSchedule.networks.tfl.timetableCron.timeZone` | `Etc/UTC` | The schedule's time zone. Required. |
| `pollerTransitSchedule.networks.tfl.timetableCron.suspend` | `false` | Suspend the CronJob. |
| `pollerTransitSchedule.networks.tfl.timetableCron.startingDeadlineSeconds` | `3600` | A start missed by more than this is skipped (the freshness alert allows 36h). |
| `pollerTransitSchedule.networks.tfl.timetableCron.activeDeadlineSeconds` | `1800` | Kill a run, or one still Pending (it needs 448Mi of free requests on the node), after this long. A run is about 20 s. |
| `pollerTransitSchedule.networks.tfl.timetableCron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1). Exit 3 (the feed guard refused the timetable) and 137 (OOMKilled) fail the Job at once. |
| `pollerTransitSchedule.networks.tfl.timetableCron.successfulJobsHistoryLimit` | `1` | Finished Jobs kept. |
| `pollerTransitSchedule.networks.tfl.timetableCron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `pollerTransitSchedule.networks.tfl.timetableCron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after it ends. |
| `pollerTransitSchedule.networks.tfl.timetableCron.skipIfFresherThanSecs` | `43200` | `RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS`: a run finding a timetable commit on the stream newer than this fetches nothing; an unanswered `timetableRefresh` token overrides it. |
| `pollerTransitSchedule.networks.tfl.timetableCron.resources` | requests `100m`/`448Mi`/ephemeral `192Mi`, limits `1Gi`/ephemeral `768Mi` | The run's container resources: as the Deployment's (332 MiB parse peak), plus ephemeral storage for the 141 MB zip in `/work`. |

### scheduleFeed

An SFTP server (SFTPGo) that receives the pushed CIF timetable, and the
schedulefeed pod: `schedule-ingest` (waits for a complete delivery and
posts it to api) and `schedule-reference` (derives the schedule products
from it). Off by default. The feed has two sources, switched separately:
`scheduleFeed.sftp.enabled` (the SFTP receiver, on by default) and
`scheduleFeed.bucket.enabled` (a Google Cloud Storage bucket, off; see
"scheduleFeed: bucket source" below and
[docs/schedule-feed-bucket.md](../../docs/schedule-feed-bucket.md)). Both
can run at once, deduplicated by content; with neither the render fails.

**SFTPGo has its own Deployment** (`<fullname>-schedulefeed-sftp`,
`scheduleFeed.sftp.separateDeployment: true`, the default since
2026-10-08). DTD pushes the daily CIF zip at about 20:00–20:06 UTC, and
nothing says its client retries a failed push. When SFTPGo was a container
of the schedulefeed pod, every app deploy recreated it along with
`ingest`/`reference`. Now its pod template holds only pinned inputs: the
SFTPGo image, the entrypoint script's hash, the Secrets and the env. Its
labels are the release-stable `distant-signal.podLabels`, with no chart or
app version, like every pod template. So an app deploy leaves it running, and only a change to SFTPGo's
own settings restarts it. The SFTP Service keeps its name, type and
NodePort and selects the new pod. Both pods mount the same ReadWriteOnce
PVC, which **assumes a single-node cluster**: ReadWriteOnce is per node,
so on several nodes pin both pods to one node (`scheduleFeed.nodeSelector`
or `affinity`, which apply to both), use a ReadWriteMany class, or turn
the split off.

Switching either way (the first deploy of this change, or a rollback to
`separateDeployment: false`) recreates both pods once. Expect an SFTP
outage of under a minute, so deploy away from 19:45–20:30 UTC. The PVC and
its data are untouched, and so are the host keys and the push account, so
DTD sees the same host key and credentials. Roll back by setting
`scheduleFeed.sftp.separateDeployment: false`, which renders the earlier
one-pod layout.

| Key | Default | Description |
|---|---|---|
| `scheduleFeed.enabled` | `false` | Deploy the schedulefeed pod, the SFTP Deployment and Service, and the PVC. |
| `scheduleFeed.sftp.enabled` | `true` | Run the SFTP receiver (the `sftp` container, its Service/NodePort, host keys and entrypoint). Off for bucket-only delivery (`scheduleFeed.bucket`); the PVC and the ingest/reference containers stay. `scheduleFeed.enabled` with neither source enabled fails the render. |
| `scheduleFeed.sftp.separateDeployment` | `true` | Run SFTPGo in its own Deployment (`<fullname>-schedulefeed-sftp`) so that app deploys never restart it. Assumes a single node (one ReadWriteOnce PVC for both pods). `false` puts it back in the schedulefeed pod, as before 2026-10-08. Switching recreates both pods once; the PVC is untouched. |
| `scheduleFeed.sftp.podAnnotations` | `{}` | Annotations for the separate SFTP pod. Unused with `separateDeployment: false`. |
| `scheduleFeed.sftp.image.repository` | `drakkan/sftpgo` | SFTP server image. |
| `scheduleFeed.sftp.image.tag` | `v2.7.6@sha256:…` | Must be a real `drakkan/sftpgo` tag: an empty tag would fall back to this chart's version. |
| `scheduleFeed.sftp.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `scheduleFeed.sftp.port` | `2022` | SFTP container and Service port. |
| `scheduleFeed.sftp.allowedCidrs` | `[]` | See the networkPolicy table below. |
| `scheduleFeed.sftp.publicHostname` | `""` | Informational only: the public hostname pointed at the Service, shown in NOTES.txt with the other details to give the feed provider. |
| `scheduleFeed.sftp.username` | `dtd-push` | The push account's username on this server. |
| `scheduleFeed.sftp.authMethod` | `""` | `password` or `public-key`. No default: enabling without it fails the render. |
| `scheduleFeed.sftp.password` | `""` | Push account password (`authMethod: password`). Generated and preserved across upgrades when empty; NOTES.txt shows how to read it back. |
| `scheduleFeed.sftp.publicKey` | `""` | The feed provider's public key (`authMethod: public-key`). |
| `scheduleFeed.sftp.passwordPolicy.minLength` | `24` | Minimum push account password length. A `password` in values is checked at render time; one from `existingSecret` by the sftp entrypoint at start (only the length is logged). The generated default is 32 random alphanumerics (~190 bits). |
| `scheduleFeed.sftp.passwordPolicy.enforce` | `true` | At start, refuse to run with a shorter password (`true`) or only log a warning (`false`). |
| `scheduleFeed.sftp.existingSecret` | `""` | Read the SFTP credentials from this pre-existing Secret instead. |
| `scheduleFeed.sftp.existingSecretPasswordKey` | `schedule-sftp-password` | Key for the push account password. |
| `scheduleFeed.sftp.existingSecretPublicKeyKey` | `schedule-sftp-dtd-public-key` | Key for the provider's public key. |
| `scheduleFeed.sftp.existingSecretHostKey` | `""` | Pre-existing Secret holding this server's own SSH host key (not the provider's). Empty: the chart generates and preserves them. |
| `scheduleFeed.sftp.hostKeys` | `[ssh_host_ecdsa_key, ssh_host_ed25519_key]` | Keys in the host-key Secret that SFTPGo serves (`SFTPGO_SFTPD__HOST_KEYS`), mounted 0440 for the pod's fsGroup. ECDSA covers clients without ed25519 (DTD's JSch 0.1.54). With `existingSecretHostKey`, that Secret must hold every listed key. Empty: SFTPGo generates fresh keys on every start. |
| `scheduleFeed.sftp.webAdmin.enabled` | `false` | Run SFTPGo's web admin/REST listener. Off, because with no admin account anyone who reaches it can create one. |
| `scheduleFeed.sftp.extraEnv` | `[]` | Extra env entries for the SFTPGo container (e.g. `SFTPGO_*` telemetry, defender or log settings). One named like a chart-set var (say `SFTPGO_COMMON__DEFENDER__THRESHOLD`) replaces it, so each name is rendered once; the rest follow the chart's own. |
| `scheduleFeed.sftp.permissions` | `[upload, overwrite, list]` | SFTPGo permissions the push account has on its home directory. Least privilege from DTD's observed client behaviour: no download, delete, rename, mkdir, symlink, chmod/chown/chtimes or copy. `overwrite` is needed because DTD replaces `timetable_full.zip` in place daily. `*` is refused. See [docs/schedule-feed-sftp.md](../../docs/schedule-feed-sftp.md). |
| `scheduleFeed.sftp.maxSessions` | `2` | Simultaneous sessions for the push account. `0` = unlimited. |
| `scheduleFeed.sftp.maxUploadFileSize` | `536870912` | Largest single upload in bytes (512 MiB, ~6.6x the 77 MB CIF zip). Larger uploads fail and are deleted. `0` = unlimited. |
| `scheduleFeed.sftp.sshCommands` | `[]` | SSH commands SFTPGo runs besides SFTP (`SFTPGO_SFTPD__ENABLED_SSH_COMMANDS`; the image enables `md5sum`, `sha1sum`, `sha256sum`, `cd`, `pwd`, `scp`). None by default. |
| `scheduleFeed.sftp.defender.enabled` | `true` | SFTPGo's in-process brute-force defender: bans a source IP whose score reaches `threshold` within `observationTime` minutes. Needs real client IPs (`scheduleFeed.service.externalTrafficPolicy: Local`); behind source NAT a ban locks out every client. Bans are in memory, so a pod restart clears them. |
| `scheduleFeed.sftp.defender.banTime` / `.banTimeIncrement` | `60` / `100` | Ban length in minutes, and the percentage of it added each time a banned host connects again. |
| `scheduleFeed.sftp.defender.threshold` | `8` | Score at which a host is banned. |
| `scheduleFeed.sftp.defender.scoreValid` / `.scoreInvalid` | `2` / `2` | Score per wrong password for an existing account, and per unknown username. An OpenSSH or JSch client with a wrong password scores twice per connection (keyboard-interactive, then password). |
| `scheduleFeed.sftp.defender.scoreLimitExceeded` | `4` | Score per rejected connection from the rate limiter or the per-host connection cap. |
| `scheduleFeed.sftp.defender.scoreNoAuth` | `0` | Score per connection that never tried to authenticate (the kubelet's TCP probe, port scanners). Keep 0 unless the probe source is safelisted. |
| `scheduleFeed.sftp.defender.observationTime` | `30` | Minutes of history a host's score covers. |
| `scheduleFeed.sftp.defender.entriesSoftLimit` / `.entriesHardLimit` | `500` / `1000` | Hosts kept in memory. |
| `scheduleFeed.sftp.defender.safelist` | `[]` | IPs/CIDRs never scored, banned or rate-limited (loaded as SFTPGo IP list entries through the push account's loaddata file). |
| `scheduleFeed.sftp.maxPerHostConnections` | `8` | Simultaneous connections allowed from one source IP; `0` disables the cap. |
| `scheduleFeed.sftp.rateLimit.average` / `.periodMs` / `.burst` | `20` / `60000` / `10` | Per-source SSH connection rate limit: `average` connections per `periodMs`, bursting to `burst`. `average: 0` disables it. |
| `scheduleFeed.sftp.rateLimit.entriesSoftLimit` / `.entriesHardLimit` | `100` / `150` | Source IPs the rate limiter keeps in memory, trimmed back to the soft limit at the hard limit (`SFTPGO_COMMON__RATE_LIMITERS__0__ENTRIES_SOFT_LIMIT` / `_HARD_LIMIT`). Needs `1 <= soft < hard`. |
| `scheduleFeed.sftp.logLevel` | `debug` | SFTPGo log level (`SFTPGO_LOG_LEVEL`). Failed logins and defender score changes are only logged at `debug`, so log-based alerts on them need it. Timestamps are always UTC (`SFTPGO_LOG_UTC_TIME`). Fields: [docs/schedule-feed-sftp.md](../../docs/schedule-feed-sftp.md#audit-log). |
| `scheduleFeed.sftp.telemetry.enabled` | `true` | SFTPGo's telemetry listener (`/metrics`, `/healthz`), as container port `sftp-metrics` and a PodMonitor endpoint. Needs `metrics.enabled`. Not on the NodePort Service; the NetworkPolicy admits only the monitoring namespace. No auth, no profiler. |
| `scheduleFeed.sftp.telemetry.port` | `9097` | Telemetry port. |
| `scheduleFeed.sftp.destinationFolder` | `incoming` | Folder on the PVC the push account is chrooted to; also schedule-ingest's `WATCH_DIR`. |
| `scheduleFeed.sftp.folderPath` | `""` | Optional subfolder within `destinationFolder`. |
| `scheduleFeed.sftp.resources` | requests `25m`/`64Mi`, limit `128Mi` | SFTP container resource requests/limits. |
| `scheduleFeed.ingest.image.repository` | `ghcr.io/fasterspeeding/distant-signal/schedule-ingest` | schedule-ingest image repository. |
| `scheduleFeed.ingest.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `scheduleFeed.ingest.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `scheduleFeed.ingest.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `scheduleFeed.ingest.checkTimes` | `22:00,22:30,…,01:30,16:00` | Europe/London times of the provider's delivery window. Only the last entry matters now: after it an incomplete delivery is logged as an error. Scanning is driven by `pollIntervalSecs`. |
| `scheduleFeed.ingest.pollIntervalSecs` | `120` | Seconds between scans of the watch folder. |
| `scheduleFeed.ingest.retentionKeepDeliveries` | `2` | Complete deliveries kept on disk (current plus fallback). |
| `scheduleFeed.ingest.stabilityCycles` | `5` | Consecutive unchanged scans before a file is treated as complete. |
| `scheduleFeed.ingest.database.maxConnections` | `1` | The pool under `sink: db` (`DATABASE_MAX_CONNECTIONS`). |
| `scheduleFeed.ingest.extraEnv` | `[]` | Extra env entries for the schedule-ingest container. One named like a chart-set var (say `RUST_LOG`) replaces it, so each name is rendered once; the rest follow the chart's own. |
| `scheduleFeed.ingest.progressStallSecs` | `1800` | `/livez` stall window (see `workerHealth`) for one scan cycle, including posting a delivery to api. |
| `scheduleFeed.ingest.resources` | requests `50m`/`128Mi`, limit `256Mi` | schedule-ingest container resource requests/limits. |
| `scheduleFeed.reference.image.repository` | `ghcr.io/fasterspeeding/distant-signal/schedule-reference` | schedule-reference image repository. |
| `scheduleFeed.reference.image.tag` | `""` | Empty means "use the chart's appVersion". |
| `scheduleFeed.reference.image.digest` | `""` | Exact content digest (`sha256:...`). See `api.image.digest` above. |
| `scheduleFeed.reference.image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `scheduleFeed.reference.pollIntervalSecs` | `1800` | How often the storage folder is checked for a new complete delivery (`mode: deployment`). |
| `scheduleFeed.reference.mode` | `deployment` | `deployment` (the `reference` container of the schedulefeed pod) or `cronjob` (its own CronJob, `cron` below: each run takes the run lock, publishes the newest delivery if it is new, and exits). Exactly one renders. As a CronJob it mounts the schedulefeed PVC read-only (ReadWriteOnce: same node), serves no `/metrics`, and its four alerts read the ingest-writer's gauges of its database rows (`schedule_reference_signals`, `schedule_reference_publishes`; needs `ingestWriter.enabled`). See `docs/scheduled-jobs.md`, "schedule-reference". |
| `scheduleFeed.reference.cron.schedule` | `"*/30 * * * *"` | `mode: cronjob`: every 30 minutes, the Deployment's poll interval. A run with nothing new exits 0 in seconds. |
| `scheduleFeed.reference.cron.timeZone` | `Europe/London` | The schedule's time zone. Required. On DST nights the 01:00 and 01:30 runs are skipped (spring) or may run twice (autumn, the second a no-op). |
| `scheduleFeed.reference.cron.suspend` | `false` | Suspend the CronJob (DistantSignalScheduleReferenceNotSeeded stays quiet while suspended). |
| `scheduleFeed.reference.cron.startingDeadlineSeconds` | `600` | A start missed by more than this is skipped; the next is 30 minutes later. |
| `scheduleFeed.reference.cron.activeDeadlineSeconds` | `5400` | Kill a run, or one still Pending, after this long. A full publish takes 18-38 minutes. |
| `scheduleFeed.reference.cron.backoffLimit` | `2` | Retries of a run that failed transiently (exit 1), each a full republish. Exit 3 (a product refused, or input it can't publish; the delivery is settled) and 137 (OOMKilled) fail the Job at once. |
| `scheduleFeed.reference.cron.successfulJobsHistoryLimit` | `3` | Finished Jobs kept. |
| `scheduleFeed.reference.cron.failedJobsHistoryLimit` | `3` | Failed Jobs kept. |
| `scheduleFeed.reference.cron.ttlSecondsAfterFinished` | `86400` | A finished Job and its pod are deleted this long after. |
| `scheduleFeed.reference.cron.resources` | requests `250m`/`1536Mi`/`64Mi` ephemeral, limits `4Gi`/`512Mi` ephemeral | The run's resources, held only while it runs: the request covers the nightly peak (1429Mi), the limit keeps production's headroom. Leave the Deployment's all-day request unclaimed on the node as this Job's budget (`docs/scheduled-jobs.md`, "Capacity"). |
| `scheduleFeed.reference.forwardPublishDays` | `28` | Days beyond today each delivery publishes the per-date timetable products for (`SCHEDULE_FORWARD_PUBLISH_DAYS`, 28-60, never below the 28-day tracking horizon): how far ahead train search, `/Train/by-uid` stops, service modes and `/Trips/plan` reach. Each day costs ~190 MB of Postgres and ~30 s of publish cycle. Raising it fills the new dates on the next delivery. |
| `scheduleFeed.reference.ingest.database.maxConnections` | `3` | The `db` sink's pool (`DATABASE_MAX_CONNECTIONS`). |
| `scheduleFeed.reference.healthPort` | `8091` | Health port. Must differ from `workerHealth.port`, which the ingest container in the same pod uses. |
| `scheduleFeed.reference.progressStallSecs` | `7200` | `/livez` stall window: one cycle publishes every derived product of a full timetable. |
| `scheduleFeed.reference.metricsPort` | `9092` | Prometheus `/metrics` port. Must differ from `metrics.port`, which the ingest container uses. |
| `scheduleFeed.reference.resources` | requests `250m`/`512Mi`, limit `2304Mi` | schedule-reference container resource requests/limits: the largest in the chart, since it parses the whole timetable. The ~1.2-1.4GiB peak (1429Mi max in production over 20 days) lasts only the nightly publish; afterwards the binary returns its freed heap (`malloc_trim`) and RSS falls to tens of MiB (`distant_signal_schedule_reference_post_publish_resident_memory_bytes`), so `512Mi` sizes the idle state and the limit is 1.6x the peak. |
| `scheduleFeed.service.type` | `LoadBalancer` | `LoadBalancer`, or `NodePort` behind an external load balancer. Not an Ingress: SFTP is not HTTP. |
| `scheduleFeed.service.annotations` | `{}` | Service annotations. |
| `scheduleFeed.service.nodePort` | `null` | Explicit NodePort for the SFTP port. Empty lets Kubernetes assign one. |
| `scheduleFeed.service.externalTrafficPolicy` | `""` | `Local` or `Cluster`; empty renders nothing (Kubernetes defaults to `Cluster`). Only valid with type `NodePort` or `LoadBalancer`; anything else fails the render. `Local` keeps DTD's real source IP, which `scheduleFeed.sftp.allowedCidrs` needs, and only routes to nodes with a ready schedulefeed pod (no cost on a single node). |
| `scheduleFeed.persistence.enabled` | `true` | Attach a PVC for deliveries. |
| `scheduleFeed.persistence.size` | `5Gi` | Requested volume size. With `scheduleFeed.bucket` on, budget `(archiveKeep + 1) × maxObjectBytes` on top of today's use; the defaults fit in 5Gi. |
| `scheduleFeed.persistence.storageClass` | `""` | StorageClass name. Empty means the cluster default. |
| `scheduleFeed.persistence.accessModes` | `[ReadWriteOnce]` | PVC access modes. |
| `scheduleFeed.persistence.existingClaim` | `""` | Use this existing PVC instead of creating one. |
| `scheduleFeed.logLevel` | `info` | `RUST_LOG` value for both Rust containers. |
| `scheduleFeed.resources` | `{}` | Fallback for any container whose own `resources` is empty. |
| `scheduleFeed.nodeSelector` | `{}` | Pod node selector, for both the schedulefeed and SFTP pods. |
| `scheduleFeed.tolerations` | `[]` | Pod tolerations, for both pods. |
| `scheduleFeed.affinity` | `{}` | Pod affinity rules, for both pods. |
| `scheduleFeed.podAnnotations` | `{}` | Annotations for the schedulefeed (ingest/reference) pod. The separate SFTP pod uses `scheduleFeed.sftp.podAnnotations`. |
| `scheduleFeed.serviceAccount.create` | `false` | Create a dedicated ServiceAccount for the schedulefeed pod (`<fullname>-schedulefeed`, or `name`), with no RBAC and `automountServiceAccountToken: false`. Off: the pod uses the shared `serviceAccount`. Required (or `name`) by `scheduleFeed.bucket.auth=workloadIdentity`. |
| `scheduleFeed.serviceAccount.name` | `""` | Its name. With `create: false`, an existing ServiceAccount, which must not be the shared one in `workloadIdentity` mode. |
| `scheduleFeed.podSecurityContext` | `{fsGroup: 1000}` | `fsGroup: 1000` is required: the SFTPGo image runs as UID 1000 and does not chown a fresh volume. |

### workerHealth

Shared `/livez` and `/healthz` settings for the single-container workers
(aggregator, enricher, notifier, every poller including the island-of-Ireland ones, and schedule-ingest). Each
worker's own `progressStallSecs` sets how long its loop may go without
progress before `/livez` fails.

| Key | Default | Description |
|---|---|---|
| `workerHealth.port` | `8090` | Port the workers serve `/livez` and `/healthz` on. |
| `workerHealth.liveness.initialDelaySeconds` | `30` | Liveness probe initial delay. |
| `workerHealth.liveness.periodSeconds` | `30` | Liveness probe period. |
| `workerHealth.liveness.timeoutSeconds` | `5` | Liveness probe timeout. |
| `workerHealth.liveness.failureThreshold` | `6` | Liveness failures allowed (6 x 30s = 3 minutes on top of the stall window). |
| `workerHealth.readiness.periodSeconds` | `10` | Readiness probe period. |
| `workerHealth.readiness.timeoutSeconds` | `3` | Readiness probe timeout. |
| `workerHealth.readiness.failureThreshold` | `3` | Readiness failures allowed. |
| `workerHealth.pollerProgressStallSecs` | `1800` | Stall window for every poller under `pollers`: one fetch plus up to 15 minutes of ingest retries. |

### ingress

| Key | Default | Description |
|---|---|---|
| `ingress.enabled` | `false` | Render the Ingress object. |
| `ingress.className` | `""` | IngressClass name. Empty uses the cluster default. |
| `ingress.annotations` | `{}` | Annotations on the Ingress, e.g. `cert-manager.io/cluster-issuer`. |
| `ingress.frontend.enabled` | `true` | Publish the frontend host (only when `ingress.enabled`). |
| `ingress.frontend.host` | `""` | Hostname for the web UI. Required when enabled. |
| `ingress.api.enabled` | `false` | Publish the api host. |
| `ingress.api.host` | `""` | Hostname for the api. Required when enabled. |
| `ingress.tls` | `[]` | TLS blocks passed through verbatim. |

### metrics

On by default: the exporters are in-process and add no runtime dependency
once the images are built. `metrics.enabled: false` is a real off switch,
not merely an un-scraped one — it renders `METRICS_ENABLED=false` into every
workload, and each binary then never starts its `/metrics` listener at all
(api keeps its own public HTTP listener, but drops the request-metrics
middleware and never starts the internal `/metrics` listener below). Every
Rust workload, api included, serves `/metrics` on a listener separate from its
own public/service port: `metrics.port` for most, and a component-specific
port for the three consumers, movement-relay, the island-of-Ireland pollers
and schedulefeed's reference container (`<component>.metricsPort`,
`scheduleFeed.reference.metricsPort`). Until 2026-09-25 api was the
one exception (it served `/metrics` on `api.service.port` itself, which
made it internet-reachable through the api Ingress whenever
`ingress.api.enabled` was also set — a Signal Box Audit Low finding); it
now matches every other workload.

| Key | Default | Description |
|---|---|---|
| `metrics.enabled` | `true` | Expose Prometheus `/metrics` on every workload, and render the metrics port, env, `prometheus.io/*` scrape annotations and NetworkPolicy allows. |
| `metrics.port` | `9091` | Port api, the aggregator, the enricher, the notifier, the `pollers.*` and schedule-ingest serve `/metrics` on — a listener separate from api's own public/service port. The workloads listed above use their own `metricsPort` values. |
| `metrics.podMonitor.enabled` | `false` | Render a Prometheus Operator `PodMonitor`. Off by default — the CRD is absent on clusters without the operator, and installing it would fail the release outright. |
| `metrics.podMonitor.interval` | `30s` | Scrape interval on the `PodMonitor`. |
| `metrics.podMonitor.scrapeTimeout` | `10s` | Scrape timeout on the `PodMonitor`. Must stay below `interval`. |
| `metrics.prometheusRule.enabled` | `false` | Render a Prometheus Operator `PrometheusRule` with the alerts below. Off by default for the same CRD reason as the `PodMonitor`; also needs `metrics.enabled`. |
| `metrics.prometheusRule.labels` | `{}` | Extra labels on the `PrometheusRule` object — whatever your Prometheus's `ruleSelector` matches (e.g. `release: kube-prometheus-stack`). |
| `metrics.prometheusRule.annotations` | `{}` | Extra annotations on the `PrometheusRule` object. |
| `metrics.prometheusRule.ruleLabels` | `{}` | Extra labels added to every alert, next to `severity`. |
| `metrics.prometheusRule.runbookBaseUrl` | GitHub `main` | Prefix for each alert's `runbook_url`; `/docs/alerts.md#<alert name, lowercased>` is appended. |
| `metrics.prometheusRule.<alert>` | see `values.yaml` | Per-alert `enabled`, `for`, `severity` and threshold settings, `for` durations, severities and thresholds for `movementLag`, `movementLagGrowing`, `streamGap`, `deadLetter`, `deadLetterFull`, `relayPublishFailing`, `redisPersistence`, `groupRecreated`, `deadLetterExpiring`, `longPending`, `parseEnvelope`, `enricherErrors`, `enricherTokenExchange`, `enricherBatches`, `componentMemory`, `fullCoverageWindow`, `notifierPushDropped`, `notifierForwardQueue`, `userSignupSpike`, `archiveUploadFailures`, `archiveStale`, `archiveBatchChurn`, `retentionStepFailing`, `archiveExpiry`, `schedulePipeline`, `scheduleSftp`, `scheduleBucket`, `pollerFailures`, `pollerStale`, `transitFeedRefused`, `transitUnmappedLineRef`, `tflRoutesMissing`, `ldbwsStalestStation`, `ldbwsInvalidCrs` and `incidentRemovalStalled`. |

#### Alerts

Every alert is named `DistantSignal*` and covers only what this chart's own
metrics can tell (plus per-container memory headroom). Generic signals —
OOMKilled, restart spikes, pods not ready — belong to the cluster's own
rules and are deliberately not duplicated here. The reverse also holds: this
chart is the source for every `DistantSignal*` alert below, so a cluster's
own rules should not define an alert with the same name (two copies fire
twice). Every
expression is scoped to `namespace="<release namespace>"`, the label the
`PodMonitor` attaches, so the rules only see series scraped that way (or by
an equivalent scrape that sets `namespace`). The `movement-events` group is
rendered only when `movementRelay.enabled` is true, the full-coverage-window
group only when `fullCoverageConsumer.windowedStats.enabled`, the archive
alert only when `archive.enabled`, the archive-expiry group only when
`archive.expiry.enabled` too, the schedule-pipeline group only when
`scheduleFeed.enabled`, and the enricher token-exchange group only when
`enricher.llm.auth` is a keyless (workload identity) mode.

Each alert carries a one-line `summary`, a short `description` and a
`runbook_url` into [docs/alerts.md](../../docs/alerts.md), which holds the
explanation and what to do. Expressions longer than about 300 characters are
recording rules (`distant_signal:*`) in the same group, so the alert's
`generatorURL` stays short. Both keep a 3-alert notification under ntfy's
4,096-byte limit; `scripts/check-alert-payloads.py` checks it in CI.

| Alert | Severity | Fires when (defaults) |
|---|---|---|
| `DistantSignalPostgresDown` | critical | postgres_exporter's `pg_up` is 0 (`postgresDown.pgUpSelector` narrows its series), or, with `postgresql.enabled`, the bundled Postgres StatefulSet has no ready replica (kube-state-metrics' `kube_statefulset_status_replicas_ready`), for 3m. |
| `DistantSignalStatefulImagePullFailing` | critical | A container or init container of the bundled Postgres or Redis pod has waited in `ImagePullBackOff`, `ErrImagePull` or `InvalidImageName` for 5m (kube-state-metrics' `kube_pod_[init_]container_status_waiting_reason`). Both are single pods replaced without overlap, so this is an outage; the cluster's generic `KubeContainerWaiting` stays a warning. |
| `DistantSignalApiDatabaseDown` | critical | api's own `SELECT 1` probe through its request pool (`api_db_up`, every 15s) is 0 on some replica for 2m: a rotated password, an exhausted pool, `max_connections`, a NetworkPolicy, or the database itself. |
| `DistantSignalSchemaGateWaiting` | critical | A service's schema gate (`db_schema_ready`) is 0 on some pod for 10m: the database lacks the newest migration in its image, or its role lacks a `files/db-grants.yaml` grant. One alert per component. |
| `DistantSignalConsumerApiCallsFailing` | warning | trust-consumer, trust-backlog-consumer or full-coverage-consumer failed at least 3 reads or writes within 5m, continuously for 10m (`*_errors_total` for: trust-consumer `reload_tracked_trains`, `reload_stanox_crs`, `startup_reference_load`, `db_write`; trust-backlog-consumer `reload_stanox_crs`, `db_write`, `db_write_reasons`; full-coverage-consumer `reload_line_population_fetch`, `reload_stanox_crs`). One alert per consumer. |
| `DistantSignalApiPublic5xx` | warning | More than 5% of api's public requests (every route but `/metrics`, `/public/health`, `/public/ready` and unmatched paths) answered 5xx over 5m, with at least 3 of them, for 10m (`http_requests_total` by `exported_endpoint`). A dependency-unavailable 503 counts. |
| `DistantSignalAggregatorCycleFailing` | warning | No successful aggregation cycle for 15m (`aggregator_last_success_timestamp_seconds{cycle}`, the process start until the first success; `aggregator_cycles_total{cycle,result}` counts both outcomes). |
| `DistantSignalNotifierCycleFailing` | warning | No successful run of a notifier loop (`line_status`, `forward_queue`, `skip_check`; not the hourly `template_sweep`) for 15m (`notifier_last_success_timestamp_seconds{cycle}`). |
| `DistantSignalMovementLagHigh` | warning | A consumer group's `movement_relay_stream_lag` plus `movement_relay_stream_pending` (delivered but un-ACKed: a consumer whose downstream fails keeps reading, so its backlog sits in pending) is above 25% of the stream cap (`movement_relay_stream_maxlen`, falling back to `movementRelay.streamMaxLen`) for 10m. |
| `DistantSignalMovementLagCritical` | critical | The same, above 50%. |
| `DistantSignalMovementLagGrowing` | warning | A group's lag has a positive `deriv` and grew by more than 5000 entries over 30m, for 10m. Lag never reads 0, so neither alert is on `> 0`. |
| `DistantSignalStreamGap` | warning | trust-consumer, full-coverage-consumer or trust-backlog-consumer counted a stream gap (`*_stream_gap_detected_total`) within the last 1h. |
| `DistantSignalDeadLetterGrowing` | warning | Any record dead-lettered (`movement_feed_deadlettered_total`) within the last 1h. |
| `DistantSignalDeadLetterNearFull` | warning | The dead-letter stream's length (`movement_relay_deadletter_length`, read by movement-relay every tick; falls back to the consumers' `movement_feed_deadletter_length`) above 80% of the 10,000-record cap. |
| `DistantSignalDeadLetterFull` | critical | A dead-letter write was refused because the stream is full (`movement_feed_deadletter_full_total`) within the last 1h. |
| `DistantSignalDeadLetterExpiring` | warning | The oldest dead letter (`movement_relay_deadletter_oldest_age_seconds`) is within 4h (`deadLetterExpiring.warnBeforeTrimSecs`) of `movementRelay.deadLetterMaxAgeSecs` (24h), after which movement-relay deletes it, for 5m. Re-inject it first. |
| `DistantSignalMovementFeedLongPending` | warning | A consumer group re-read entries already delivered more than 240 times (`movement_feed_long_pending_total{group}`, above `longPending.threshold` 0) within the last 1h. They are retried forever and never dead-lettered (R-040). |
| `DistantSignalTrustEnvelopeParseDrops` | warning | trust-consumer, full-coverage-consumer or trust-backlog-consumer dropped TRUST envelopes whose body did not parse (`*_errors_total{operation="parse_envelope",msg_type}`, above `parseEnvelope.threshold` 0) within the last 1h: a feed schema change (R-097). |
| `DistantSignalMovementRelayPublishFailing` | critical | movement-relay failed every `XADD` (`movement_relay_errors_total{operation=~"publish_event\|redis_oom"}`) and published nothing over 10m, for 5m: Redis is refusing writes and TRUST ingestion has stopped. |
| `DistantSignalRedisPersistenceFailing` | critical | Redis's last AOF write or rewrite failed (`redis_aof_last_write_ok` / `redis_aof_last_bgrewrite_ok` is 0, from movement-relay's `INFO persistence`), or, for the bundled Redis with persistence, AOF is off, for 5m. |
| `DistantSignalMovementGroupRecreated` | warning | Within 1h a consumer recreated its group after `NOGROUP` (`movement_feed_group_recreated_total`), or movement-relay recreated a missing stream with every group (`movement_relay_stream_created_total`): Redis lost its data. |
| `DistantSignalEnricherErrors` | warning | Over 30m, more than 50% of an LLM call site's calls (`enricher_llm_call_total{outcome!="success"}`: `error`, `timeout`, `rate_limited`, `quota_exhausted`, `gateway_error`, `http_error`, `empty_content` or `refused`) failed, with at least 3 failures, for 15m. |
| `DistantSignalEnricherTokenExchangeFailing` | warning | Keyless auth only (`enricher.llm.auth` not `apiKey`; not rendered otherwise): at least 3 token requests to one `stage` (`enricher_llm_token_exchange_total{outcome!="success"}`) failed over 30m with none succeeding, for 10m (`enricherTokenExchange.minFailures`, `window`, `for`). The cached OpenAI or Claude token lasts at most an hour after the last success. |
| `DistantSignalEnricherBatchFailing` | warning | Batch mode only (`enricher.llm.batch.sweepMode: batch`, or `enricher.llm.batch.mode` `batch`/`batch-only`): at least 2 batches failed to submit or were abandoned over 6h, for 15m (`enricherBatches.minFailures`, `window`). |
| `DistantSignalEnricherBatchResultsFailing` | warning | Batch mode only: over 6h, more than 20% of ended batch requests were errored, expired, canceled or auth-expired, with at least 20 ended (`enricherBatches.resultFailureRatio`, `minResults`). |
| `DistantSignalEnricherBatchStuck` | warning | Batch mode only: a batch in flight for more than 26 h (`enricherBatches.maxAgeSeconds`; batches expire after 24 h). |
| `DistantSignalEnricherBatchInvalid` | warning | Batch mode only: at least one batch failed input validation (`enricher_llm_batches_total{event="validation_failed"}`, OpenAI's `failed` status) over 6h, for 15m, per `provider`. Nothing in it ran. |
| `DistantSignalEnricherBatchAuthExpired` | warning | Batch mode on OpenAI keyless auth: at least one batch had requests (or all of it) fail `token_expired` (`enricher_llm_batches_total{event="auth_expired"}`) over 6h, for 15m, per `provider`. OpenAI authorises a batch with its creator's token after creating it; those requests did not run and go into the next sweep. |
| `DistantSignalEnricherBatchFileCleanupFailing` | warning | Batch mode on OpenAI only: at least 2 batch file deletions failed (`enricher_llm_batch_files_total{event="delete_failed"}`) over 6h, for 15m (`enricherBatches.minFailures`). The files expire on their own after 2 days. |
| `DistantSignalFullCoverageWindowFeedStale` | warning | full-coverage-consumer has marked its windows `feed_stale` (`full_coverage_consumer_window_feed_stale` is 1) for 15m. |
| `DistantSignalFullCoverageWindowStatsStalled` | warning | No window rows posted (`full_coverage_consumer_window_rows_posted_total`) over 10m, for 15m. |
| `DistantSignalComponentMemoryHigh` | warning | A container in this release's pods (`pod=~"<fullname>-.*"`) has a working set (cadvisor) above 80% of its memory limit (kube-state-metrics) for 10m. |
| `DistantSignalNotifierPushDropped` | warning | The notifier dropped at least 5 decided pushes (`notifier_push_dropped_total{reason}`) within the last 1h. Delivery is at-most-once. |
| `DistantSignalNotifierForwardQueueRetentionOverdue` | warning | The oldest `notifier_forward_queue` row (`notifier_forward_queue_oldest_age_seconds`, sampled after every forward-queue cycle) is older than the longer of `aggregator.trainsRetentionDays` and `untrackedTrainsRetentionDays` + 2 days (`retentionGraceDays`), for 1h. The table is not archived and its rows leave only with their `trains` row, so the trains prune is not removing them. |
| `DistantSignalNotifierForwardQueueLarge` | warning | `notifier_forward_queue` holds more than 50000 rows (`notifier_forward_queue_rows`, `maxRows`) for 1h: a producer flooding it, or tracked trains have outgrown keeping it unpruned and unarchived. |
| `DistantSignalUserSignupSpike` | warning | api created more than 20 user accounts (`userSignupSpike.metric`, default `distant_signal_api_users_created_total`) within the last 1h. Sign-up is open to any account the IdP admits (in production, any Discord account), so a burst is real growth or scripted sign-ups. Check the newest `users` rows and the IdP's enrolment log; to stop it, restrict enrolment in the IdP, then revoke the unwanted sessions ([session revocation](../../docs/session-revocation.md)). Needs an api build that exports the counter; silent until then. |
| `DistantSignalArchiveUploadFailures` | warning | At least 3 cold-archive batch uploads or verifications failed (`aggregator_archive_upload_failures_total`) within the last 1h. |
| `DistantSignalArchiveExpiryErrors` | warning | Any cold-archive expiry error (`aggregator_archive_expiry_errors_total{stage}`: a failed LIST or DELETE, or a key refused by the pre-delete check) within the last 1d. |
| `DistantSignalArchiveExpiryUnmatchedKeys` | warning | Expiry listed keys under an archive table prefix that do not match the archive layout (`aggregator_archive_expiry_skipped_unmatched_total{table}`) within the last 1d. They are never deleted. |
| `DistantSignalArchiveExpiryCapReached` | warning | An expiry run hit `archive.expiry.maxDeletesPerRun` (`aggregator_archive_expiry_cap_reached_total`) within the last 1d. |
| `DistantSignalArchiveExpiryOverdue` | warning | The oldest archived `service_date` (`aggregator_archive_oldest_service_date_seconds{table}`) is older than `archive.expiry.retentionDays` + 7 days, for 1h: expiry is not running, is failing, or is still in dry-run. |
| `DistantSignalScheduleReferenceNotSeeded` | warning | schedule-reference has not read its last completed publish from api (`schedule_reference_seeded` is 0) for 30m. As a CronJob (`scheduleFeed.reference.mode: cronjob`): no run has read it for 1h (`schedulePipeline.cronNotSeededAfter`, the schedule's interval plus 30m; the ingest-writer's `schedule_reference_recorded_signal_timestamp_seconds{kind="seeded"}`), never while the CronJob is suspended. |
| `DistantSignalScheduleFeedZipRejected` | warning | schedule-ingest quarantined a delivery zip (`schedule_feed_zip_rejected_total`) within the last 6h: over the extraction caps, or failing a CIF content check (`scheduleFeed.ingest.cifChecks`). The `schedule_ingest::audit` line names the reason. |
| `DistantSignalScheduleFeedIngestRejected` | warning | api answered 400/413/422 to schedule-ingest's delivery record (`schedule_feed_ingest_rejected_total`) within the last 6h (`schedulePipeline.rejectedWindow`). The delivery is not retried until a new upload. |
| `DistantSignalCorpusRejected` | warning | schedule-ingest refused a CORPUS file of either format (`schedule_feed_corpus_rejected_total{format}`) within the last 6h. The series exists only while `scheduleFeed.corpus.enabled`. |
| `DistantSignalCorpusLoadFailing` | warning | schedule-ingest failed to scan `watch_dir`, or to read or load a stable CORPUS file (`schedule_feed_corpus_failures_total{stage}`), at least 3 times within 30m (`schedulePipeline.corpusFailingMin`, `corpusFailingWindow`): about 6 minutes of a persistent failure at the 120s poll. A real failure, kept apart from "no new file yet". The series exists only while `scheduleFeed.corpus.enabled`. |
| `DistantSignalCorpusFormatDrift` | warning | An `NLC*` file that is not `.xml.gz` is in the landing folder: the NLC XML CORPUS format may have drifted, and the file is ignored. schedule-ingest counts `schedule_feed_corpus_failures_total{stage="drift"}` every poll while it is there (and sets `schedule_feed_corpus_format_drift_files`), so this fires on the first poll that sees it and clears `corpusFailingWindow` after the file is removed. The series exists only while `scheduleFeed.corpus.enabled`. |
| `DistantSignalCorpusStale` | warning, then critical | The newest loaded CORPUS delivery (`store_corpus_last_delivered_at_seconds`, set by the ingest-writer from `corpus_deliveries`; `api_` before ingest phase 5) is over 35 days old (`schedulePipeline.corpusStaleWarningDays`, warning) or over 45 days (`corpusStaleCriticalDays`, `corpusStaleCriticalSeverity`), for 1h; only one fires at a time. CORPUS is not daily: RDM lists it as monthly and it has arrived every few days to couple of weeks, so 35 days is one monthly cycle plus four days' grace and 45 days means at least one delivery was missed. Rendered only when `scheduleFeed.corpus.enabled`, and silent before the first load. |
| `DistantSignalScheduleReferencePublishStale` | warning | No CIF delivery fully published for over 30h (`schedule_reference_last_published_delivery_timestamp_seconds`; as a CronJob the ingest-writer's `schedule_reference_recorded_published_delivery_timestamp_seconds`, from `schedule_reference_publishes`), for 15m. |
| `DistantSignalSchedulePublishStagedMismatch` | warning | api, or schedule-reference's db sink, skipped a final chunk's delete because the staged key count did not match (`store_schedule_publish_staged_mismatch_total{product}`; as a CronJob the ingest-writer's `schedule_reference_recorded_signal_timestamp_seconds{kind="staged_mismatch"}`) within the last 6h. |
| `DistantSignalScheduleReferencePublishRejected` | warning | api answered 400/413/422 to a schedule-reference product (`schedule_reference_publishes_total{outcome="rejected"}`; as a CronJob the ingest-writer's `schedule_reference_recorded_signal_timestamp_seconds{kind="rejected"}`) within the last 6h. |
| `DistantSignalLinePopulationMissing` | warning | After 06:00 London, some line still has no schedule population for today (`full_coverage_consumer_population_missing_past_deadline_lines` above 0) for 15m. |
| `DistantSignalSftpNoUpload` | warning | SFTPGo received no upload (`sftpgo_uploads_total`) in 30h (`scheduleSftp.noUploadWindow`), for 30m: DTD's daily push did not arrive. Quiet until the counter has a full window of history. Uploads are not per file type, so `DistantSignalScheduleReferencePublishStale` stays authoritative for the CIF. Group `distant-signal.schedule-sftp`, rendered with `scheduleFeed.enabled` and `scheduleFeed.sftp.telemetry.enabled`. Runbook: `docs/schedule-feed-sftp.md`. |
| `DistantSignalSftpUploadErrors` | warning | Any failed or interrupted upload (`sftpgo_upload_errors_total`) in the last 1h. |
| `DistantSignalSftpUserStoreDown` | critical | SFTPGo's user store is unavailable (`sftpgo_dataprovider_availability` 0) for 5m: every login fails. Failed-login alerts are Loki rules, not metrics: the counters carry no username or IP. |
| `DistantSignalScheduleBucketAccessRevoked` | critical | Every schedule-feed bucket call was refused (`schedule_feed_source_access_revoked{source="bucket"}` is 1) for 10m (`scheduleBucket.accessRevokedFor`): the kill switch removed the reader, or its key is missing, rotated or disabled. SFTP carries on. Only with `scheduleFeed.bucket.enabled`. |
| `DistantSignalScheduleBucketNoNewObject` | warning | No new expected object in the bucket (`schedule_feed_source_last_new_object_seconds{source="bucket"}`) for 30h (`scheduleBucket.noNewObjectHours`), for 30m. Quiet until the first object. |
| `DistantSignalScheduleBucketReadErrors` | warning | List, get, verify or delete errors (`schedule_feed_source_errors_total{source="bucket", kind!="auth"}`) in the last 1h, for 15m. |
| `DistantSignalScheduleBucketUnexpectedObject` | warning | An unexpected object (wrong name, too large, or unroutable) was flagged in the last 1h (`schedule_feed_source_unexpected_objects_total`); it is deleted unread. |
| `DistantSignalScheduleBucketDownloadBudget` | critical | The hourly or daily download cap stopped the reader (`schedule_feed_source_download_capped{source="bucket"}` is 1): a loop or an attack. |
| `DistantSignalScheduleFeedSourcesDisagree` | warning | SFTP and the bucket delivered different content of one kind within `scheduleFeed.disagreementWindowMinutes` (`schedule_feed_source_disagreement_total{kind}`) in the last 1d; the bucket copy won. Only with both sources on. |
| `DistantSignalPollerFailing` | warning | A poller completed no successful cycle and at least one failed one (`poller_cycle_total{result}`) over the last 2h, or more than half its cycles over the last 15m failed (`pollerFailures.failureRatio`, `ratioWindow`), for 10m (SVC-08). Rendered only when a poller (including an island-of-Ireland one) is enabled, in a separate `<fullname>-pollers` PrometheusRule. |
| `DistantSignalPollerStale` | warning | A poller's last successful cycle (`poller_last_success_timestamp_seconds{cycle}`, the process start until one succeeds) is older than 2x its `pollIntervalSecs` and at least 1800s (`pollerStale.intervalMultiple`, `minAgeSeconds`) for 10m. Needs no failed cycle, so it also catches a poller that stopped cycling. One rule per enabled poller, in the `<fullname>-pollers` PrometheusRule. |
| `DistantSignalTransitFeedRefused` | warning | `poller-transit-schedule` refused a network's timetable (`transit_feed_refused_total{network, reason}`) within 26h (`transitFeedRefused.window`); the previous one stays active. Only while a `pollerTransitSchedule` network is enabled. |
| `DistantSignalTransitUnmappedLineRef` | warning | A network's SIRI-SX disruptions named a `LineRef` its network file has no alias for (`transit_situation_unmapped_line_refs_total{network, ref}`) within 1h. Only while a network is enabled. |
| `DistantSignalTflRoutesMissing` | warning | The api planned a TfL stop end on a line with no routes loaded (`api_tfl_access_routes_missing_total{line, reason="line"}`) within 1h (`tflRoutesMissing.window`): its Tube rides were straight-line estimates. Only while `pollers.tfl.stops.routes` is on. |
| `DistantSignalLdbwsStationStale` | warning | The least recently sampled LDBWS station (`ldbws_stalest_station_age_seconds`) is over 900s old (the aggregator's sample-age limit, past which it drops the station) for 15m: the rotation stopped reaching part of the list (SVC-04). Stations LDBWS rejects as an invalid CRS are excluded. Only when `pollers.ldbws.enabled`. |
| `DistantSignalLdbwsInvalidCrs` | warning | LDBWS has answered "Invalid crs code supplied" for a sample station (`ldbws_invalid_crs_station{crs}` is 1) for 15m: a `lines/*.toml` typo. The poller re-probes it hourly instead of every cycle. Only when `pollers.ldbws.enabled`. |
| `DistantSignalIncidentSnapshotsMissing` | warning | No incidents snapshot reached the database (`store_incident_removal_inference_total`, every outcome, counted by poller-incidents; `api_` before ingest phase 5) over 2h, for 15m. |
| `DistantSignalWriterLoopStale` | warning | A background loop's newest successful run (`loop_last_success_timestamp_seconds{cycle}`, max over api and writer) is over 1800s old (`ingest_dedup_prune`: 9000s; the canary left out), for 10m. With `ingestWriter.loops.enabled`. |
| `DistantSignalWriterLoopUnowned` | warning | No process holds a loop's advisory lock (`sum by (loop) (loop_lock_held) < 1`) for 15m (`ingest_dedup_prune`: 75m). With `ingestWriter.loops.enabled`. |
| `DistantSignalIngestApplyWritesNothing` | warning | Over 15m the writer applied entries of a stream (`ingest_stream_consumed_total`) but wrote no row (`ingest_stream_row_writes_total{outcome="written"}`), for 5m; not `ds:ingest:reference`. |
| `DistantSignalIngestSourceStale` | warning | An `ingest_freshness` row (`ingest_freshness_timestamp_seconds{source}`) is older than its `ingestSourceStale.maxAgeSeconds` entry, for 10m; the stream-only sources only while their stream is in `apply`. |
| `DistantSignalDbPoolAcquireTimeouts` | warning | A component's Postgres pool timed out an acquire (`db_pool_acquire_timeouts_total`) within 10m. |
| `DistantSignalDirectWritesFailing` | warning | A direct writer's writes of one operation (`db_writes_total{operation}`) all failed (`timeout`, `transient`, `rejected`; no `ok`) over 15m, for 30m. |
| `DistantSignalScheduleFeedMarkerStale` | warning | The newest CIF delivery whose feed marker landed (`schedule_feed_last_ingest_delivered_at_seconds`) is over 30h old, for 15m. |
| `DistantSignalPgBackRestCheckFailed` | critical | The daily pgBackRest check Job or the weekly verify Job failed within 26h: archiving is broken, `verify` found a bad file, or WAL is missing (a PITR gap). This group renders only with `postgresql.pgbackrest.enabled`, in a separate `<fullname>-pgbackrest` PrometheusRule (`metrics.prometheusRule.pgbackrest`), and reads kube-state-metrics and postgres_exporter series rather than this chart's own. Runbook: `docs/postgres-pitr.md`. |
| `DistantSignalPgBackRestBackupFailed` | warning | A full or diff backup Job failed within 26h. |
| `DistantSignalPgBackRestBackupStale` | warning | No full or diff backup CronJob success (`kube_cronjob_status_last_successful_time`) for 30h, for 10m. |
| `DistantSignalPgBackRestFullBackupStale` | warning | No full backup success for 8d, for 10m. |
| `DistantSignalPgBackRestArchiveFailing` | warning | postgres_exporter's `pg_stat_archiver_failed_count` rose over 15m, for 10m. |
| `DistantSignalPgBackRestArchiveStalled` | warning | `pg_stat_archiver_archived_count` didn't rise over 15m, for 10m (`archive_timeout` switches segments every minute while anything writes). |
| `DistantSignalScheduledJobFailed` | warning | A Job of one of the chart's own CronJobs (not pgBackRest's) that started within 26h failed (`kube_job_status_failed`, with its `reason`: `BackoffLimitExceeded`, `DeadlineExceeded`, `Evicted`, or `Other`, which includes the pod failure policy's FailJob on exit 137 or 3). This group is a separate `<fullname>-scheduled-jobs` PrometheusRule (`metrics.prometheusRule.scheduledJobs`) of kube-state-metrics series. Runbook: `docs/scheduled-jobs.md`. |
| `DistantSignalScheduledJobStale` | warning | One of those CronJobs, not suspended and with no run going (`kube_cronjob_status_active`), has had no success (`kube_cronjob_status_last_successful_time`, or since its creation) for 1.5x its schedule interval and at least 30m, for 10m. |
| `DistantSignalScheduledJobOOMKilled` | warning | A container of a Job pod named `<fullname>-...` created within 26h ended OOMKilled (`kube_pod_container_status_terminated_reason`). `restartPolicy: Never` means no restart, so restart-based OOM alerts miss it. |
| `DistantSignalScheduledJobPending` | warning | A Job pod named `<fullname>-...` has been Pending for 15m: no room for its requests, or its image won't pull. |

Metric names above omit the `distant_signal_` prefix every app metric
carries. The dead-letter and stream-gap counters are registered at 0 when
each process starts, so those alerts use a plain `increase()`. They no
longer also fire on a series that is new within the window: every rollout
creates new per-pod series, so that clause fired on every rollout.

### networkPolicy

| Key | Default | Description |
|---|---|---|
| `networkPolicy.enabled` | `false` | Render default-deny NetworkPolicies with explicit allows. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | Namespace the ingress controller runs in, matched by `kubernetes.io/metadata.name`. |
| `networkPolicy.apiExtraIngressNamespaces` | `[]` | Extra namespaces allowed to reach `api.service.port` (e.g. `[ds-mcp]` for the Distant-Signal-MCP). |
| `networkPolicy.apiExtraIngressPodLabels` | `ds-mcp`: `app.kubernetes.io/name: distant-signal-mcp`, `app.kubernetes.io/component: mcp` | Per-namespace pod labels that narrow an `apiExtraIngressNamespaces` entry to the calling pods. A namespace with no entry admits all its pods. Set an entry to `null` to clear it; `{}` merges with the default and does not clear it. |
| `networkPolicy.healthIngress.enabled` | `true` | Render the rule admitting the workers' and schedulefeed's health (probe) ports. `false` closes them to every pod; probes still work on CNIs that exempt node-local traffic (kube-router, Calico and Cilium by default). |
| `networkPolicy.healthIngress.from` | `[]` | NetworkPolicy peers allowed to reach the health ports, e.g. the nodes' addresses as ipBlocks. Empty allows any source. |
| `networkPolicy.postgresClients` | `[]` | Extra NetworkPolicy peers (pod/namespace selectors) admitted to the bundled Postgres, e.g. a postgres-exporter or backup CronJob. See [NetworkPolicy](#networkpolicy). |
| `networkPolicy.redisClients` | `[]` | Extra NetworkPolicy peers admitted to the bundled Redis, e.g. a redis-exporter. |
| `networkPolicy.tunnel.enabled` | `false` | Admit an in-cluster tunnel connector (e.g. cloudflared) to the frontend. See [NetworkPolicy](#networkpolicy). |
| `networkPolicy.tunnel.namespace` | `cloudflared` | Namespace the connector runs in, matched by `kubernetes.io/metadata.name`. |
| `networkPolicy.tunnel.podLabels` | `app.kubernetes.io/name: cloudflared` | Labels selecting the connector pods. Empty admits the whole namespace. |
| `networkPolicy.tunnel.api` | `false` | Also admit the connector to `api.service.port`, for a hostname routed straight to the api. Requires `api.rateLimit.trustXRealIp: false`. |
| `networkPolicy.monitoringNamespace` | `monitoring` | Namespace Prometheus runs in, matched by `kubernetes.io/metadata.name`. Allowed to reach each workload's metrics port. Only used when `metrics.enabled` is true. |
| `networkPolicy.egress.enabled` | `false` | Render an egress policy for every component (see [NetworkPolicy](#networkpolicy)). |
| `networkPolicy.egress.privateCidrs` | RFC 1918, CGNAT, loopback, link-local, reserved | IPv4 ranges excluded from the public-internet egress allow. |
| `networkPolicy.egress.privateCidrsV6` | loopback, ULA, link-local, multicast, NAT64/6to4/Teredo | IPv6 ranges excluded from the public-internet egress allow. |
| `networkPolicy.egress.extraDeniedCidrs` | `[]` | More CIDRs (IPv4 and IPv6 mixed) excluded from the public-internet egress allow, on top of `privateCidrs`/`privateCidrsV6`. Set the nodes' own public addresses here. |
| `networkPolicy.egress.dnsPeers` | `[]` | NetworkPolicy peers the DNS egress rule (port 53) is limited to, e.g. CoreDNS (`kube-system`, `k8s-app: kube-dns`). Empty allows port 53 to any destination. |
| `networkPolicy.egress.internetPorts` | `[443]` | TCP ports the public-internet egress rule allows. Each component also gets its own upstreams' ports (from their URLs, the Kafka brokers, the pgBackRest S3 port). `[]` allows every port. |
| `networkPolicy.egress.extraRules` | `[]` | Extra NetworkPolicyEgressRule entries appended to every egress policy the chart renders. |
| `networkPolicy.components` | `{}` | Per-component settings keyed by the `app.kubernetes.io/component` label (`api`, `postgres`, `poller-ldbws`, ...); an unknown key fails the render. Each entry: `egress` (`false` renders no egress policy for it), `internet` (add or drop its public-internet rule), `internetPorts` (replaces `egress.internetPorts` for it), `extraEgress` / `extraIngress` (raw NetworkPolicy rules appended to its policy). See [NetworkPolicy](#networkpolicy). |
| `scheduleFeed.sftp.allowedCidrs` | `[]` | Source CIDRs allowed to reach SFTP when `networkPolicy.enabled`. Empty allows any source. Needs the real client IP: set `scheduleFeed.service.externalTrafficPolicy: Local`. |

### scheduleFeed: CIF routing and CORPUS

See `docs/superpowers/specs/2026-09-28-corpus-sftp-ingest-design.md`.

| Key | Default | Description |
|---|---|---|
| `scheduleFeed.ingest.cifFilePattern` | `timetable_full.zip` | Case-insensitive `*` globs (comma-separated) naming CIF deliveries in the landing folder. Locked to DTD's exact delivery name. |
| `scheduleFeed.ingest.cifExcludePattern` | `CORPUSExtract*` | Globs that are never CIF deliveries, so a CORPUS or SMART file pushed as a zip is never published as the timetable. |
| `scheduleFeed.ingest.cifChecks.maxGeneratedAgeDays` | `3` | Quarantine a CIF delivery whose MSN `Generated` date is more than this many days before the delivery (a replayed old extract). The structural checks (full-extract `HD`, known record types, one `ZZ` trailer, a `Generated` date) always run. `0` disables. See [docs/schedule-feed-sftp.md](../../docs/schedule-feed-sftp.md#delivery-checks). |
| `scheduleFeed.ingest.cifChecks.minSchedules` | `100000` | Quarantine an MCA with fewer `BS` (schedule) records; the real extract has ~505,000. `0` disables. |
| `scheduleFeed.ingest.cifChecks.maxRecordDropPercent` | `20` | Quarantine a delivery whose `BS` or `TI` count fell by more than this percentage since the last accepted delivery (real day-to-day change is under 0.3%). `0` disables. |
| `scheduleFeed.corpus.enabled` | `false` | Load Network Rail CORPUS (pushed to the same SFTP account and folder) into `corpus_locations`, in either format below. Off: the files stay in the landing folder with a one-time stray warning. |
| `scheduleFeed.corpus.jsonEnabled` | `true` | Load the JSON extract (`CORPUSExtract.json.gz`, the older CORPUS API version: every location, ~56,000 rows). |
| `scheduleFeed.corpus.filePattern` | `CORPUSExtract.json.gz` | Globs naming the CORPUS extract. `CORPUSExtract.csv.gz` (SMART berth data) is deliberately ignored. |
| `scheduleFeed.corpus.minRows` | `10000` | Fewer rows than this rejects the JSON extract instead of replacing the table. |
| `scheduleFeed.corpus.xml.enabled` | `true` | Load the NLC XML snapshot (`NLC<YYYYMMDD>.xml.gz`, the newer API version: TIPLOC-bearing locations only, ~12,100 rows), picked by the `NLC` prefix and `.xml.gz` extension. Any other `NLC*` file is never loaded and raises `DistantSignalCorpusFormatDrift`. |
| `scheduleFeed.corpus.xml.minRows` | `10000` | Fewer rows than this rejects the XML snapshot. |
| `scheduleFeed.corpus.formatPrecedence` | `json` | Which file loads when both formats arrive: `json`, `xml` or `newest` (by mtime; the table then alternates between formats). The other format is still checked, compared and archived, and loads only once the preferred one has not loaded for `preferredGraceDays`. |
| `scheduleFeed.corpus.preferredGraceDays` | `35` | Days without a load of the preferred format before the other format takes over. |
| `scheduleFeed.corpus.compareFormats` | `true` | Compare each file with the newest one of the other format; logs and `schedule_feed_corpus_format_diff{measure}`. Read-only. |
| `scheduleFeed.corpus.maxDecompressedBytes` | `268435456` | gzip-bomb guard. |
| `scheduleFeed.corpus.retentionKeep` | `3` | Loaded files kept under `/data/schedule-feed/corpus/`, and separately rejected (`corpus/rejected/`) and superseded, unloaded ones (`corpus/superseded/`). |

### scheduleFeed: bucket source

The Google Cloud Storage delivery source, an alternative (or addition) to
SFTP: schedule-ingest polls a dedicated bucket, downloads only the expected
names, verifies size and CRC32C, archives each object on the PVC and deletes
it from the bucket. `charts/ds-ingest-bucket` provisions the bucket and its
IAM. See [docs/schedule-feed-bucket.md](../../docs/schedule-feed-bucket.md)
and `docs/superpowers/specs/2026-10-02-schedule-feed-gcs-landing-design.md`.
Off by default. schedule-ingest has the bucket source since DS-22
(2026-10-20); an older image ignores these values.

| Key | Default | Description |
|---|---|---|
| `scheduleFeed.bucket.enabled` | `false` | Run the bucket source. With `scheduleFeed.sftp.enabled` too, both run, deduplicated by SHA-256. |
| `scheduleFeed.bucket.provider` | `gcs` | Only `gcs` is implemented; anything else fails the render. |
| `scheduleFeed.bucket.name` | `""` | Bucket name (no dots). Required when enabled; set in deploy values. |
| `scheduleFeed.bucket.baseUrl` | `""` | Empty means `https://storage.googleapis.com`. Set only for a fake-GCS test server. |
| `scheduleFeed.bucket.auth` | `key` | How the reader authenticates. `key`: a sealed service-account key from `existingSecret` (`GOOGLE_SERVICE_ACCOUNT_PATH`). `workloadIdentity`: keyless Google workload identity federation (`GOOGLE_APPLICATION_CREDENTIALS`, an `external_account` config; see "Keyless bucket access" below). Anything else fails the render. |
| `scheduleFeed.bucket.existingSecret` | `""` | `key` mode: pre-existing Secret holding the reader's service-account key (sealed in deploy config; never put the key in values). Required in `key` mode, refused in `workloadIdentity` mode. Mounted read-only into `ingest` only, as an optional volume: a missing Secret leaves SFTP running and raises `DistantSignalScheduleBucketAccessRevoked`. |
| `scheduleFeed.bucket.serviceAccountKey` | `service-account.json` | `key` mode: key in `existingSecret` holding the JSON key. Changing it in `workloadIdentity` mode fails the render. |
| `scheduleFeed.bucket.workloadIdentity.audience` | `gcp-ds-ingest` | Audience of the projected service-account token: what the Google workload identity pool provider allows. |
| `scheduleFeed.bucket.workloadIdentity.credentialConfigMap` | `""` | `workloadIdentity` mode: pre-existing ConfigMap holding the `external_account` credential configuration (no secret in it). Required in that mode, refused in `key` mode. Mounted read-only into `ingest` only, as an optional volume, like the key Secret. |
| `scheduleFeed.bucket.workloadIdentity.credentialConfigKey` | `credential-config.json` | Key in `credentialConfigMap` holding that JSON. |
| `scheduleFeed.bucket.expectedKeys` | `[timetable_full.zip, CORPUSExtract.json.gz, NLC*.xml.gz]` | Case-insensitive `*` globs naming the objects (bucket root only) the reader downloads. A match is downloaded only if it also routes to CIF (`ingest.cifFilePattern`) or, with `scheduleFeed.corpus` on, CORPUS (the JSON extract or the NLC XML snapshot). Anything else is flagged and deleted unread after `deleteMinAgeSecs`. |
| `scheduleFeed.bucket.pollIntervalSecs` | `300` | Seconds between bucket listings. At least 60. |
| `scheduleFeed.bucket.deleteMinAgeSecs` | `3600` | Objects are deleted only once this old, so the publisher's read-back and scan finish first. Under 6 days (the bucket's lifecycle backstop is 7). |
| `scheduleFeed.bucket.archiveKeep` | `5` | Raw objects kept under `/data/schedule-feed/sources/bucket/archive`. |
| `scheduleFeed.bucket.maxObjectBytes` | `268435456` | Objects listed larger than this are never downloaded (flagged as unexpected). At most `maxDownloadBytesPerHour`. |
| `scheduleFeed.bucket.maxDownloadsPerPoll` | `2` | Loop guard: downloads per poll; more wait for the next poll. |
| `scheduleFeed.bucket.maxDownloadBytesPerHour` | `268435456` | Loop guard: bytes per rolling hour. Hitting it raises `DistantSignalScheduleBucketDownloadBudget`. At most `maxDownloadBytesPerDay`. |
| `scheduleFeed.bucket.maxDownloadBytesPerDay` | `1073741824` | Loop guard: bytes per rolling day. |
| `scheduleFeed.bucket.maxBackoffSecs` | `3600` | Ceiling of the exponential backoff after errors, and the retry interval while access is revoked. At least `pollIntervalSecs`. |
| `scheduleFeed.bucket.auditLogs.ship` | `false` | Ship the bucket's Cloud Audit Logs (from the audit-log bucket) to stdout as `schedule_ingest::bucket_access` lines for Loki, with the reader's credential (objectViewer on the audit-log bucket). Fields and queries: [docs/schedule-feed-bucket.md](../../docs/schedule-feed-bucket.md#audit-log). Needs a schedule-ingest image from 2026-10-21 (DS-90) on; an older one logs a warning and ships nothing. |
| `scheduleFeed.bucket.auditLogs.bucket` | `""` | Audit-log bucket name. Required with `ship`; must differ from `name`. |
| `scheduleFeed.bucket.auditLogs.pollIntervalSecs` | `600` | Seconds between audit-log bucket listings. At least 60. |
| `scheduleFeed.bucket.notifications.pubsub.enabled` | `false` | Wake the reader on the bucket's `OBJECT_FINALIZE` notifications (a Pub/Sub pull subscription, outbound HTTPS) instead of waiting for the next poll; polling stays as the fallback. Needs Ranma's `ds-ingest-bucket` release with `notifications.pubsub.enabled` ([docs/schedule-feed-bucket.md](../../docs/schedule-feed-bucket.md#pubsub-notifications)) and a schedule-ingest image from 2026-10-21 (DS-91) on. |
| `scheduleFeed.bucket.notifications.pubsub.subscription` | `""` | The pull subscription's full name, `projects/<project>/subscriptions/<name>`. Required when enabled; the reader needs `roles/pubsub.subscriber` on it. |
| `scheduleFeed.bucket.notifications.pubsub.reconcileIntervalSecs` | `3600` | Seconds between polls while the subscription delivers. At least `pollIntervalSecs`; while it fails, the reader polls every `pollIntervalSecs`. |
| `scheduleFeed.bucket.notifications.pubsub.baseUrl` | `""` | Empty: `https://pubsub.googleapis.com`. Set only for a fake Pub/Sub test server. |
| `scheduleFeed.sourcePrecedence` | `[bucket, sftp]` | Which source wins when SFTP and the bucket deliver different content within `disagreementWindowMinutes` (decision D5: the bucket). Must be a permutation of `[bucket, sftp]`. |
| `scheduleFeed.disagreementWindowMinutes` | `120` | Different content from the two sources within this many minutes is a disagreement (`DistantSignalScheduleFeedSourcesDisagree`). |

#### Keyless bucket access (workload identity federation)

`scheduleFeed.bucket.auth: workloadIdentity` replaces the reader key with a
short-lived token. Google's workload identity pool trusts the cluster's
service-account token issuer (an uploaded JWKS) and is pinned to the subject
`system:serviceaccount:<namespace>:<fullname>-schedulefeed` (for the
`distant-signal` release in the `distant-signal` namespace:
`system:serviceaccount:distant-signal:distant-signal-schedulefeed`), which is
allowed to impersonate the reader's Google service account. The chart then:

- mounts a projected service-account token (audience
  `workloadIdentity.audience`, one hour, nothing else in the projection)
  read-only on `ingest` only, at
  `/var/run/secrets/distant-signal/gcs-token/token`;
- mounts the `external_account` credential configuration from
  `workloadIdentity.credentialConfigMap` at
  `/var/run/secrets/distant-signal/gcs/credential-config.json` and points
  `GOOGLE_APPLICATION_CREDENTIALS` at it (no key Secret, no
  `GOOGLE_SERVICE_ACCOUNT_PATH`);
- requires `scheduleFeed.serviceAccount` (a render on the shared account
  fails), and refuses `existingSecret` or a changed `serviceAccountKey`;
- opens `sts.googleapis.com` and `iamcredentials.googleapis.com` (443) in
  the NetworkPolicy instead of `oauth2.googleapis.com`.

The ConfigMap (deploy config's; it holds no secret) must point
`credential_source.file` at the token path above:

```json
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/<pool-id>/providers/<provider-id>",
  "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
  "token_url": "https://sts.googleapis.com/v1/token",
  "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/<reader-sa>@<project-id>.iam.gserviceaccount.com:generateAccessToken",
  "credential_source": {
    "file": "/var/run/secrets/distant-signal/gcs-token/token",
    "format": { "type": "text" }
  }
}
```

The provider's allowed audience must be `workloadIdentity.audience`
(`gcp-ds-ingest`), not the `//iam.googleapis.com/...` name: the k8s token's
`aud` claim is what Google checks against the provider's allowed audiences.
Needs a schedule-ingest image whose bucket reader supports
`external_account` (see the GCS spec's "Keyless reader credentials"). `key`
stays the default and renders exactly as before.

```yaml
scheduleFeed:
  serviceAccount:
    create: true
  bucket:
    enabled: true
    name: <bucket-name>
    auth: workloadIdentity
    workloadIdentity:
      audience: gcp-ds-ingest
      credentialConfigMap: <configmap-name>
      credentialConfigKey: credential-config.json
```

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
- **No HorizontalPodAutoscaler.** The aggregator, the enricher and every
  poller are singleton loops that must not be scaled, and the api is
  database-bound.
- **No multi-node support for scheduleFeed's shared volume.** The SFTP pod
  and the schedulefeed pod mount one ReadWriteOnce PVC, so they must run on
  the same node. See "scheduleFeed" above.
- **No backup or HA for the bundled Redis.** It is a single replica with
  AOF persistence on a PVC; see "Using an external Redis" above.
- **No backup, restore or replication** for the bundled Postgres. It is a
  single-replica StatefulSet on a PVC. Set `postgresql.enabled: false` and
  use a managed database if you need HA.
- **No ServiceMonitor, no bundled Prometheus and no dashboards.** Every
  service does expose a Prometheus `/metrics` endpoint (see the `metrics`
  values below), and the chart can render a `PodMonitor` for Prometheus
  Operator, but it never installs Prometheus itself, ships no Grafana
  dashboards, only Distant-Signal-specific alerting rules (an opt-in
  `PrometheusRule`, see `metrics.prometheusRule` below; generic pod/cluster
  alerts are left to the cluster), and offers no `ServiceMonitor`
  alternative — `PodMonitor` alone, because the pollers, the aggregator and
  the enricher have no Service in front of them at all.
