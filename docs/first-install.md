# First install

What a fresh cluster needs before `helm install` of the `distant-signal`
chart works, in the order to do it: verify what you pull, create the
Secrets, let the chart's checks tell you what is missing, install, check the
migrations and the Postgres roles, enable the pollers, verify.

This page is a checklist. Why each piece exists is in
[postgres-app-role.md](postgres-app-role.md) (the Postgres roles),
[redis-acl.md](redis-acl.md) (the Redis users) and the chart's
[README](../charts/distant-signal/README.md). Every name, key and message
below was read from the chart's templates and values, or taken from a render
of it (`helm template`); a value in angle brackets is yours to choose.

Throughout, the release is `distant-signal` in the namespace
`distant-signal`; with another release name, the object names that start
`distant-signal-` change with it (the chart's `fullname`).

## What you need

- A Kubernetes cluster (>= 1.23) with a default StorageClass, `kubectl` and
  Helm 3.8+ or 4.x. The chart's README lists the rest.
- `cosign` and `openssl` for the checks and the password recipes below.
- Your own values file. Start from
  [`values-minimal.yaml`](../charts/distant-signal/values-minimal.yaml): the
  smallest install, with no LLM, no Kafka broker, no Rail Data Marketplace
  (RDM) key and no external identity provider. When you want to size it for
  real use, read
  [`values-production-example.yaml`](../charts/distant-signal/values-production-example.yaml)
  (the layout of a real single-node deployment, every credential an
  `existingSecret` reference).

The chart builds nothing. Its images are the ones `values.yaml` pins, by tag
and digest, from `ghcr.io/distant-signal/images`, and the chart itself is
`oci://ghcr.io/distant-signal/charts/distant-signal`. Both are public.

## 1. Verify what you pull

Everything is signed keylessly by GitHub Actions of the `Distant-Signal`
organization. The issuer is always
`https://token.actions.githubusercontent.com`; the certificate identity
tells you which workflow signed.

The chart (signed by this repository's `chart.yml` on `main`; pass the
digest `helm pull` or `helm show chart` prints, not a tag):

```sh
cosign verify ghcr.io/distant-signal/charts/distant-signal@sha256:<chart digest> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/Distant-Signal/ds-deploy/.github/workflows/chart.yml@refs/heads/main
```

An image (every first-party image is built and signed by the organization's
shared `image.yml` workflow in `Distant-Signal/ci`, whichever repository
calls it, so the identity is matched by prefix; the digest is the one in
`values.yaml`, for example `api.image.digest`):

```sh
cosign verify ghcr.io/distant-signal/images/api@sha256:<digest from values.yaml> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/Distant-Signal/ci/\.github/workflows/image\.yml@'
```

The access files the chart vendors (below) come from a `Distant-Signal/ds-core`
release, and each has a Sigstore bundle. The chart's own check does exactly
this, for the release in `core-version`:

```sh
cosign verify-blob --bundle postgres-roles.sql.sigstore.json \
  --certificate-identity https://github.com/Distant-Signal/ds-core/.github/workflows/release.yml@refs/tags/<core-version, e.g. v0.14.5> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  postgres-roles.sql
```

From a checkout of this repository,
`uv run scripts/sync-core.py --check` does that for all six files and fails
when a vendored copy differs from the release. The files:

| ds-core release asset | Vendored at | What it is |
|---|---|---|
| `postgres-roles.sql` | `charts/distant-signal/files/` | Creates the owner, exporter, dump and backup roles, moves schema `public` to the owner, and drops the retired shared `app` role. Idempotent. |
| `postgres-grants.sql` | `charts/distant-signal/files/` | Creates one role per service and its grants, rendered from `db-grants.yaml`. Idempotent. |
| `db-grants.yaml` | `charts/distant-signal/files/` | The source of the grants: every table, sequence and function, and each role's rights. The chart reads it for the role names. |
| `postgres-roles-setup.sh` | `charts/distant-signal/files/` | The setup Job's entrypoint: waits for Postgres, runs each SQL file, retries a lock timeout. |
| `redis-users.acl.tpl` | `charts/distant-signal/files/` | One Redis user per client, with its rights, and a placeholder for each password. |
| `render-redis-acl.py` | `scripts/` | Renders that file without Helm, for checking or a staging Redis. |

Release assets cannot change once published (ds-core's tags and releases
are immutable), so the chart's copy is the release's.

## 2. The namespace

```sh
kubectl create namespace distant-signal
```

If you set `networkPolicy.enabled`, label the namespace the way your
cluster's policies expect; `values-minimal.yaml` leaves NetworkPolicies off.

## 3. The Secrets, in order

Create these before `helm install`. The chart fails to render, or its pods
stay pending, without the ones marked required. Use letters and digits only
for every password: Postgres passwords are put into URLs the chart cannot
percent-encode, and Redis ACL passwords cannot contain whitespace. The
recipes use `openssl rand -hex 24` (48 characters of `0-9a-f`) and never
print a value.

### 3.1 Postgres role passwords (optional)

By default the chart generates every Postgres password on first install,
into its own Secret `distant-signal`, and keeps them across upgrades (it
reads the live Secret back; this needs a real `helm install`, not
`helm template`, which shows throwaway values). **For a first trial, create
nothing here.**

To own the passwords (an external secrets manager, a rebuild that must keep
them), create one Secret per role and name it in values as
`postgresql.roles.<role>.existingSecret` or
`postgresql.roles.perService.<service>.existingSecret`. The key in each
Secret is the default below (change it with the matching
`existingSecretPasswordKey`). One Secret may hold several keys.

The superuser, `distant_signal`, is the image's bootstrap role, for humans
only (`kubectl exec ... psql`): `postgresql.auth.existingSecret`, key
`postgres-password`.

| Role (default name) | Values key | Secret key | Used by |
|---|---|---|---|
| `distant_signal_owner` | `postgresql.roles.owner` | `postgres-owner-password` | Owns schema `public` and every table. The api's migrations (`MIGRATION_DATABASE_URL`) and the migrate Job connect as it, nothing else. |
| `distant_signal_exporter` | `postgresql.roles.exporter` | `postgres-exporter-password` | `pg_monitor`, for a Postgres exporter outside the chart. |
| `distant_signal_dump` | `postgresql.roles.dump` | `postgres-dump-password` | `pg_read_all_data`, for a `pg_dump` job outside the chart. |
| `distant_signal_backup` | `postgresql.roles.backup` | `postgres-backup-password` | The pgBackRest CronJobs (only with `postgresql.pgbackrest.enabled`). |
| `distant_signal_api` | `...perService.api` | `postgres-api-password` | The api, api-maintenance and bank-holidays jobs. |
| `distant_signal_aggregator` | `...perService.aggregator` | `postgres-aggregator-password` | aggregator. |
| `distant_signal_enricher` | `...perService.enricher` | `postgres-enricher-password` | enricher (the role is created even with `enricher.enabled: false`). |
| `distant_signal_notifier` | `...perService.notifier` | `postgres-notifier-password` | notifier. |
| `distant_signal_writer` | `...perService.writer` | `postgres-writer-password` | ingest-writer. |
| `distant_signal_schedule_ingest` | `...perService.schedule_ingest` | `postgres-schedule-ingest-password` | schedule-ingest (with `scheduleFeed.enabled`). |
| `distant_signal_schedule_reference` | `...perService.schedule_reference` | `postgres-schedule-reference-password` | schedule-reference (with `scheduleFeed.enabled`). |
| `distant_signal_stations` | `...perService.stations` | `postgres-stations-password` | poller-stations (with `pollers.stations.enabled`). |
| `distant_signal_incidents` | `...perService.incidents` | `postgres-incidents-password` | poller-incidents (with `pollers.incidents.enabled`). |
| `distant_signal_trust_backlog` | `...perService.trust_backlog` | `postgres-trust-backlog-password` | trust-backlog-consumer. |
| `distant_signal_trust_consumer` | `...perService.trust_consumer` | `postgres-trust-consumer-password` | trust-consumer. |
| `distant_signal_full_coverage_ro` | `...perService.full_coverage_ro` | `postgres-full-coverage-ro-password` | full-coverage-consumer (reads). |
| `distant_signal_ldbws_ro` | `...perService.ldbws_ro` | `postgres-ldbws-ro-password` | poller-ldbws (reads its sample stations; with `pollers.ldbws.enabled`). |

Every service connects as its own role, `NOSUPERUSER`, holding exactly the
grants `db-grants.yaml` gives it, so the shared app role of earlier releases
no longer exists. The setup Job needs a password for every role in the table
whether or not the service is enabled, because the roles are created
together.

A recipe that creates one Secret per role, never printing a password:

```sh
for key in postgres-owner-password postgres-api-password; do   # one Secret per key
  kubectl -n distant-signal create secret generic "distant-signal-${key%-password}" \
    --from-literal="${key}=$(openssl rand -hex 24)"
done
```

(The names `distant-signal-postgres-owner` and so on are only a convention;
the production example uses them. Extend the loop to the keys you need.)

### 3.2 The Redis users Secret (required)

The ingest preflight (section 4) refuses to render without
`redis.acl.enabled: true` and a `redis.acl.existingSecret`: the chart cannot
generate this one. One Secret, with a `<user>-password` key for **every**
user in `redis-users.acl.tpl`, whether or not you run that service: the
Redis pod's `redis-acl` init container reads every key, and a missing one
leaves the pod in `CreateContainerConfigError`, naming the key. With
`defaultUser: "on-unshared"` (the value the minimal, example and production
files use) it also needs `default-password`: the `default` user's own
password, given to no client.

| Key | Redis user | Rights at `stage: narrow` |
|---|---|---|
| `default-password` | `default` | Today's rights, but with a password no client holds, so the shared default user cannot be used by a client by accident. |
| `movement-relay-password` | `movement-relay` | `movement-events` and its dead-letter stream (XADD, XTRIM, XGROUP CREATE, the info commands). |
| `trust-consumer-password`, `trust-backlog-consumer-password` | `trust-consumer`, `trust-backlog-consumer` | Read `movement-events` as a consumer group; write its dead-letter stream. |
| `full-coverage-consumer-password` | `full-coverage-consumer` | As the two above, plus XADD on `ds:ingest:full-coverage`. |
| `enricher-password` | `enricher` | Its consumer group on `incident-text-changed`. |
| `exporter-password` | `exporter` | Read-only metrics commands, for a Redis exporter outside the chart. |
| `poller-incidents-password` | `poller-incidents` | XADD on `incident-text-changed`. |
| `poller-ldbws-password`, `poller-tfl-password`, `poller-tocs-password` | `poller-ldbws`, `poller-tfl`, `poller-tocs` | XADD on their own `ds:ingest:*` stream. |
| `poller-irish-rail-gtfs-password`, `poller-irish-rail-live-password`, `poller-nir-stations-password` | the island-of-Ireland pollers | XADD on their own `ds:ingest:ioi-*` stream. |
| `ingest-writer-password` | `ingest-writer` | The consumer-group commands on `ds:ingest:*` (never XADD or XDEL there) and its `ds:dlq:*` dead letters. |
| `ds-admin-password` | `ds-admin` | Everything. For humans (`kubectl exec ... redis-cli --user ds-admin`) and the Redis pod's own probes. |

Four more users exist only while their client is on, and then need their
key first: `poller-transit-metrolink-password`, `poller-transit-net-password`,
`poller-transit-tfl-password` and `darwin-relay-password` (clients
`pollerTransitMetrolink`, `pollerTransitNet`, `pollerTransitTfl`,
`darwinRelay`). An optional `<user>-password-previous` key keeps the old
password valid while you rotate.

```sh
users="default movement-relay trust-consumer trust-backlog-consumer full-coverage-consumer \
  enricher exporter poller-incidents poller-ldbws poller-tfl poller-tocs poller-irish-rail-gtfs \
  poller-irish-rail-live poller-nir-stations ingest-writer ds-admin"
args=()
for user in ${users}; do
  args+=("--from-literal=${user}-password=$(openssl rand -hex 24)")
done
kubectl -n distant-signal create secret generic distant-signal-redis-users "${args[@]}"
```

Check the key names, never the values:

```sh
kubectl -n distant-signal get secret distant-signal-redis-users \
  -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
```

Then in values: `redis.acl.existingSecret: distant-signal-redis-users`,
`redis.acl.stage: narrow`, `redis.acl.defaultUser: "on-unshared"` (quoted),
and `redis.acl.clients.<client>: true` for every workload that runs (the
three values files list them). `redis.auth` (the older shared password) is
not needed.

### 3.3 Sign-in

With `devAuthentik.enabled: true` (the minimal file) the chart computes the
issuer, client and secret, and there is nothing to create. For a real OIDC
provider, create a Secret with the client secret under `sso-client-secret`
and set `api.sso.issuerUrl`, `clientId`, `existingSecret`, `redirectUrl` and
`postLoginRedirectUrl` (the redirect URL is on the **frontend's** host).
See the README's "Single sign-on (OIDC)".

### 3.4 Only when you turn the feature on

Not needed for `values-minimal.yaml`; each is a Secret you create and name
in values.

| Feature | Secret key | Values |
|---|---|---|
| The enricher (`enricher.enabled: true`) | `llm-api-key` | `enricher.llm.existingSecret`, with `enricher.llm.baseUrl` and `model` |
| Each RDM poller | `rdm-incidents-api-key`, `rdm-stations-api-key`, `rdm-tocs-api-key`, `rdm-ldbws-api-key` | `pollers.<name>.existingSecret`, with `baseUrl` from your RDM subscription |
| The TfL poller | `rdm-tfl-api-key` | `pollers.tfl.existingSecret` |
| movement-relay (RDM Train Movements) | `movement-relay-kafka-sasl-username`, `movement-relay-kafka-sasl-password` | `movementRelay.kafka.existingSecret`, with `brokers`, `topic`, `consumerGroup`, `saslMechanism` (falling back to `trustConsumer.kafka.*`) |
| Web Push | `vapid-public-key`, `vapid-private-key` | `notifier.vapid.existingSecret` (never generated) |
| The timetable SFTP server | `schedule-sftp-password` (and the host key) | `scheduleFeed.sftp.existingSecret`; see [schedule-feed-sftp.md](schedule-feed-sftp.md) |

## 4. What the chart checks before it renders

Since release B of the ingest rework, every ingest producer writes Postgres
as its own role or adds to a Redis stream the ingest-writer applies. The
chart's ingest preflight (`templates/zz-ingest-sink-preflight.yaml`, which
runs before every other template) fails the render with one message listing
everything missing, not one error at a time. With the chart's default
values, `helm template distant-signal oci://ghcr.io/distant-signal/charts/distant-signal`
prints:

```text
Error: execution error at (distant-signal/templates/zz-ingest-sink-preflight.yaml:9:4): Since release B (ingest phase 5) every ingest producer writes Postgres as its own role or XADDs to a Redis stream the ingest-writer applies; the api's /private routes are gone (docs/ingest-phase5-runbook.md). These values miss its prerequisites:
  - ingestWriter.enabled: true and ingestWriter.loops.enabled: true (pollers.stations and trustConsumer need its CORPUS crosswalk and train_event_outbox loops; plans 2b.2 and 3b.3)
  - redis.acl.clients.fullCoverageConsumer: true (fullCoverageConsumer XADDs to ds:ingest:full-coverage; docs/redis-acl.md step 2)
  - ingestWriter.enabled: true (it applies the stream producers' ds:ingest:* streams)
  - postgresql.roles.enabled: true (docs/postgres-app-role.md)
  - postgresql.roles.setupJob.enabled: true (it creates the per-service roles)
  - postgresql.roles.perService.enabled: true
  - redis.acl.enabled: true with redis.acl.existingSecret (one <user>-password key per user; docs/redis-acl.md step 1)
  - redis.acl.stage: narrow, so that redis.acl.clients.fullCoverageConsumer get their own rights, not +@all (docs/redis-acl.md step 3; security review H3)
  - redis.acl.defaultUser: off or, recommended, "on-unshared", quoted in YAML (docs/redis-acl.md step 4; the writer's apply needs it, security review H3)
  - redis.acl.clients.enricher: true (defaultUser on-unshared or off would lock it out; docs/redis-acl.md step 2)
  - redis.acl.clients.movementRelay: true (defaultUser on-unshared or off would lock it out; docs/redis-acl.md step 2)
  - redis.acl.clients.trustBacklogConsumer: true (defaultUser on-unshared or off would lock it out; docs/redis-acl.md step 2)
  - redis.acl.clients.trustConsumer: true (defaultUser on-unshared or off would lock it out; docs/redis-acl.md step 2)
```

(The line numbers and the `docs/` paths in the message are the chart's own;
the docs it names are in the other repositories, or here.) What each line
asks for:

| Line starts | Set |
|---|---|
| `ingestWriter.enabled` / `ingestWriter.loops.enabled` | `ingestWriter.enabled: true` and `ingestWriter.loops.enabled: true` |
| `ingestWriter.streams.<stream>: apply` | One entry per enabled poller that adds to a stream: `ioi-gtfs`, `ioi-live`, `ioi-nir` for the island-of-Ireland pollers; `tfl`, `reference`, `station-samples` and `full-coverage` are `apply` by default |
| `postgresql.roles.enabled`, `.setupJob.enabled`, `.perService.enabled` | all `true`; and `postgresql.enabled: true` (an external database gets no per-service roles) |
| `redis.acl.enabled: true with redis.acl.existingSecret` | section 3.2 |
| `redis.acl.stage: narrow` | `redis.acl.stage: narrow` |
| `redis.acl.defaultUser` | `redis.acl.defaultUser: "on-unshared"`, quoted |
| `redis.acl.clients.<client>: true` | one `true` for each stream producer and each deployed Redis client: with `movementRelay.enabled` on, `movementRelay`; `trustConsumer`, `trustBacklogConsumer`, `fullCoverageConsumer`, `ingestWriter`, and `enricher` unless `enricher.enabled: false` (its Redis user and key are still rendered, but no pod connects as it); each enabled poller's `poller<Name>` |

Separately, the preflight refuses a writer stream left in the removed
`shadow` mode, and these checks fail later in the render with their own
message:

- `api.sso.issuerUrl is empty and devAuthentik.enabled is false. Set
  api.sso.issuerUrl to your OIDC provider's issuer base URL ...` (and the same
  for `clientId`, `clientSecret`, `redirectUrl`, `postLoginRedirectUrl`):
  section 3.3.
- `movement-relay is enabled (the default) but has no Kafka broker. Set
  movementRelay.kafka.brokers or trustConsumer.kafka.brokers ... or set
  movementRelay.enabled=false if this install does not ingest TRUST train
  movements ...`.
- `enricher.llm.baseUrl is empty. Set it to the base URL of an
  OpenAI-compatible chat-completions endpoint ...` (and `enricher.llm.model`):
  set them, or `enricher.enabled: false`.
- A sum of Postgres role connection limits above `max_connections` minus 3
  (the default 100 leaves 97): set `postgresql.config.max_connections: 200`,
  as all three values files do.

`values-minimal.yaml` satisfies all of them with nothing on the command
line. Check that before you change anything:

```sh
helm template distant-signal oci://ghcr.io/distant-signal/charts/distant-signal \
  --version <chart version> -f values-minimal.yaml > /dev/null && echo renders
```

## 5. Install

```sh
helm install distant-signal oci://ghcr.io/distant-signal/charts/distant-signal \
  --version <chart version> -n distant-signal -f values-minimal.yaml
```

**Do not pass `--wait` or `--atomic` on the first install.** The Postgres
role setup Job (and the migrate Job, if `migrate.job.enabled`) are
post-install hooks, which Helm runs only after the wait; the workloads'
schema gate waits for the grants that Job applies once the tables exist. The
same ordering deadlock, on an upgrade, is the "Hook order" section of
[postgres-app-role.md](postgres-app-role.md). Watch it instead:

```sh
kubectl -n distant-signal get pods -w
```

With `devAuthentik.enabled`, run `helm upgrade` once straight after the
first install (the chart README, "Local dev identity provider").

## 6. The migrations and the roles

What happens, in order, on `helm install` with the bundled Postgres:

1. Postgres starts on an empty data directory. Because
   `postgresql.roles.enabled` and `initScript` (default `true`) are on, the
   image's initdb hook runs `10-distant-signal-roles.sh`, which runs
   `postgres-roles.sql` and then `postgres-grants.sql` as the superuser,
   with the passwords from the Secrets above. The roles exist before any
   client connects. (On a data directory that is not empty, such as a kept
   PersistentVolumeClaim from an earlier install, this does not run; the
   setup Job below creates the roles instead, and the passwords must match
   the ones already set.)
2. The api migrates the schema as `distant_signal_owner` when it starts, or,
   with `migrate.job.enabled: true` (as the production example), the hook Job
   `distant-signal-migrate` does it first (`ds-migrate run`, weight -5).
3. The hook Job `distant-signal-postgres-roles-setup` (post-install, weight
   0, after the migrate Job) runs `postgres-roles.sql` and
   `postgres-grants.sql` again, now that the tables exist, so every
   per-service role gets its grants on them. It waits for Postgres and
   retries a lock timeout, and it runs again on every upgrade; a failed run
   fails the release.
4. Each workload's schema gate passes once its grants are there, and the pod
   becomes Ready.

Check the Jobs and their logs:

```sh
kubectl -n distant-signal get jobs
kubectl -n distant-signal logs job/distant-signal-postgres-roles-setup
kubectl -n distant-signal logs job/distant-signal-migrate     # only with migrate.job.enabled
```

And the roles, as the superuser (a trust connection over the pod's local
socket):

```sh
kubectl -n distant-signal exec -it distant-signal-postgres-0 -- \
  psql -U distant_signal -d distant_signal -c \
  "SELECT rolname, rolsuper, rolconnlimit, rolpassword IS NOT NULL AS has_password FROM pg_authid WHERE rolname LIKE 'distant\_signal\_%' ORDER BY 1;"
```

Expect `rolsuper` false and `has_password` true on every row (the setup Job
creates the four infrastructure roles and one per service), and
`SELECT nspowner::regrole FROM pg_namespace WHERE nspname = 'public';` to
say `distant_signal_owner`. When the Job fails, "When the setup Job fails" in
[postgres-app-role.md](postgres-app-role.md) has the log reading and the
safe re-run.

## 7. The pollers

`values-minimal.yaml` runs three feeds that need no key: the island-of-Ireland
pollers (`pollerIrishRailGtfs`, `pollerIrishRailLive`, `pollerNirStations`),
which add to `ds:ingest:ioi-*` streams the ingest-writer applies. The five
other pollers (`pollers.incidents`, `stations`, `tocs`, `ldbws`, `tfl`) and
the Kafka relay are off, because each needs an account of yours: an RDM
subscription for the four RDM feeds and the Train Movements Kafka topic, and
a TfL application key for `tfl`.

To enable one, in your values file:

1. Create its key Secret (section 3.4) and set `pollers.<name>.enabled: true`,
   `baseUrl` (from your subscription; the chart ships no RDM URL, and the
   render fails with an explicit message without it) and `existingSecret`.
2. Set `redis.acl.clients.poller<Name>: true` for `ldbws`, `tfl`, `tocs` and
   `incidents` (the poller's Redis user; the key is already in the Secret
   from step 3.2). `incidents` and `stations` write Postgres directly and
   need no stream.
3. For a poller that adds to a stream, check `ingestWriter.streams.<stream>`
   is `apply` (the defaults are, for `tfl`, `reference` and
   `station-samples`).
4. `helm upgrade`. The setup Job runs again first and re-applies the grants.

For movement-relay, set `movementRelay.enabled: true` and
`movementRelay.kafka.brokers`, `topic`, `consumerGroup`, `saslMechanism`
with its credential Secret; RDM issues one consumer group per account per
product, and it belongs to movement-relay. The chart README's "Enabling the
pollers" has the table of feeds.

## 8. Verify

```sh
# Everything Running or Completed, none in CrashLoopBackOff
kubectl -n distant-signal get pods

# The api is ready (service port 8080, api.probes.readinessPath)
kubectl -n distant-signal port-forward svc/distant-signal-api 8080:8080 &
curl -fsS http://localhost:8080/public/ready

# The Redis users exist (REDISCLI_AUTH in the pod is ds-admin's password)
kubectl -n distant-signal exec deploy/distant-signal-redis -c redis -- \
  redis-cli --user ds-admin ACL USERS

# The ingest-writer is applying the streams it was given
kubectl -n distant-signal logs deploy/distant-signal-ingest-writer --tail=20
```

Then reach the site with the port-forward the install notes print (or your
ingress), and sign in. With the minimal file there are no live train
movements (movement-relay is off) and no AI summaries (the enricher is off);
line status and the island-of-Ireland pages fill as the feeds poll.

The chart's `docs/alerts.md` runbooks cover the alerts once you turn the
Prometheus rules on (`metrics.prometheusRule.enabled`).

## Not covered here

Point-in-time recovery and its image ([postgres-pitr.md](postgres-pitr.md)),
the timetable feed and its SFTP account ([schedule-feed-sftp.md](schedule-feed-sftp.md)),
upgrades between releases ([postgres-18-upgrade.md](postgres-18-upgrade.md),
the chart README's "Upgrade") and an external database or Redis (the README).
