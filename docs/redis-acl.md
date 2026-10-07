# Redis ACL users: one user per client

Status: **implemented, off by default** (`redis.acl` in the chart; ingest
architecture phase 0c, spec
`docs/superpowers/specs/2026-10-06-ingest-architecture-design.md` §8).
This page is the design summary and the rollout runbook.

## Why

Production runs `redis.auth` (`--requirepass`): one password for the
`default` user, `~* &* +@all`, shared by six Deployments and Ranma's
`redis_exporter`. Any of them can `FLUSHALL`, trim another client's stream
or destroy its consumer group. With ACL users each client can run only the
commands it needs on its own keys, and `default` is turned off.

## The users

Defined in `charts/distant-signal/files/redis-users.acl.tpl` (its header
explains the format). The kinds:

- **client**: an existing client. Stage `open`: `~* &* +@all`, today's
  rights. Stage `narrow`: its own rights.
- **final**: a client that does not exist yet (the phase 3 stream
  producers and `ingest-writer`): its final rights in every stage, so
  phase 3 needs no Redis restart.
- **admin**: `ds-admin`, for humans and the Redis probes: `~* &* +@all`.

| User | Kind | Narrow rights (summary) |
|---|---|---|
| `movement-relay` | client | `movement-events` and its dead-letter stream: XADD, XTRIM, XGROUP CREATE, XINFO, XLEN, XRANGE, EXISTS; INFO |
| `trust-consumer`, `trust-backlog-consumer` | client | `movement-events`: XREADGROUP, XACK, XAUTOCLAIM, XCLAIM, XPENDING, XGROUP CREATE, XINFO, XLEN, XRANGE, EXISTS; the dead-letter stream: XADD, XLEN. No ingest stream (decision D1 for trust-consumer) |
| `full-coverage-consumer` | client | as above, plus XADD and XREVRANGE on `ds:ingest:full-coverage` (phase 3a) |
| `enricher` | client | `incident-text-changed`: its consumer group commands |
| `api` | client | XADD on `incident-text-changed` only (write-only) |
| `exporter` | client | read-only metrics commands (`INFO`, `CONFIG GET`, `CLIENT LIST`, `SLOWLOG`, `LATENCY`, `XINFO`, `SCAN`, `MEMORY USAGE`, ...) |
| `poller-incidents` | final | XADD on `incident-text-changed` (phase 2c) |
| `poller-ldbws`, `poller-tfl`, `poller-tocs`, the three island-of-Ireland pollers | final | XADD and XREVRANGE on their own `ds:ingest:*` stream |
| `ingest-writer` | final | consumer and dead-letter commands on `ds:ingest:*` and `ds:dlq:*` |
| `ds-admin` | admin | everything |
| `default` | (values) | `defaultUser: "on"`: today's password and rights; `"off"`: disabled |

Every user also has the connection handshake (`PING`, `HELLO`, `AUTH`,
`CLIENT SETNAME`, `CLIENT SETINFO`, `CLIENT ID`).

`crates/common/tests/redis_acl.rs` (CI's rust-test job, against a real
Redis) creates every user with its narrow rights and runs its client's
real command sequence, then checks with `ACL DRYRUN` that commands it must
not have (`FLUSHALL`, another client's stream, `XGROUP DESTROY`, ...) are
refused. Edit the template and that test together.

## How the chart delivers it

- **Secret.** One SealedSecret in Ranma-Config (say
  `distant-signal-redis-users`) with a key `<user>-password` for **every**
  user in the template (letters and digits only; Redis ACL passwords
  cannot contain spaces). A missing key stops the Redis pod from starting,
  so check the key list first (the keys, never the values):

  ```sh
  kubectl -n distant-signal get secret distant-signal-redis-users \
    -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
  ```

  Needed: `movement-relay-password`, `trust-consumer-password`,
  `trust-backlog-consumer-password`, `full-coverage-consumer-password`,
  `enricher-password`, `api-password`, `exporter-password`,
  `poller-incidents-password`, `poller-ldbws-password`,
  `poller-tfl-password`, `poller-tocs-password`,
  `poller-irish-rail-gtfs-password`, `poller-irish-rail-live-password`,
  `poller-nir-stations-password`, `ingest-writer-password`,
  `ds-admin-password`.
- **Redis.** A `<release>-redis-acl` ConfigMap holds `users.acl` with
  `${REDIS_ACL_PASSWORD_<USER>}` placeholders. The Redis pod's `redis-acl`
  initContainer (the Redis image's `sh` and `awk`) fills them from the
  Secret into a memory-backed emptyDir; an empty password, or one with
  whitespace, fails the pod instead of starting Redis with a broken file.
  Redis starts with `--aclfile` instead of `--requirepass`, and its probes
  run `redis-cli --user ds-admin ping`. A pod annotation with the file's
  checksum restarts Redis when the stage or the users change.
- **Clients.** `redis.acl.clients.<client>: true` gives that Deployment
  `REDIS_USERNAME` and `REDIS_PASSWORD` from `<user>-password`. The
  services combine them with `REDIS_URL` in
  `common::redis_auth::redis_url_with_credentials`
  (`redis://<user>:<password>@host`). Without `REDIS_USERNAME` nothing
  changes.
- `scripts/render-redis-acl.py` renders the same file outside Helm, for a
  staging Redis (`--passwords-from-env`).

## Rollout runbook (Ranma)

Each step is its own release. Each Redis change is one restart: about 4 s
of AOF load at today's size. movement-relay holds Kafka and the consumers
retry, as on any Redis restart. Before phase 3 no poller uses Redis.

Before step 1, check the exporter's commands against the `exporter` user
on a staging Redis (`render-redis-acl.py --stage narrow
--passwords-from-env`, then point a redis_exporter at it as `exporter`).

### 1. Users with today's rights (`stage: open`)

Create the SealedSecret, then:

```yaml
redis:
  auth: {enabled: true, existingSecret: <as today>}
  acl:
    enabled: true
    stage: open
    defaultUser: "on"
    existingSecret: distant-signal-redis-users
```

Only Redis restarts. `default` keeps today's password, so no client
changes. Verify:

```sh
kubectl -n distant-signal exec deploy/distant-signal-redis -- redis-cli --user ds-admin ACL USERS
kubectl -n distant-signal exec deploy/distant-signal-redis -- redis-cli --user ds-admin CLIENT LIST | grep -o 'user=[^ ]*' | sort | uniq -c
```

(17 users including `default`; every client still `user=default`.) The
exporter's `redis_up` stays 1.

### 2. Clients on their own users, one at a time

```yaml
redis:
  acl:
    clients:
      enricher: true   # then api, trustBacklogConsumer, fullCoverageConsumer,
                       # trustConsumer, movementRelay, one per release
```

Only that client restarts. Verify `CLIENT LIST` shows its connections as
`user=<its user>`, and its logs have no `NOAUTH`/`WRONGPASS`. Ranma moves
the exporter to the `exporter` user (`REDIS_USER` plus its password) in the
same step. At the end `CLIENT LIST` shows no `user=default`.

### 3. Narrow (`stage: narrow`)

Only Redis restarts. For a day watch:

```sh
kubectl -n distant-signal exec deploy/distant-signal-redis -- redis-cli --user ds-admin ACL LOG 50
```

and every client's logs for `NOPERM`. `ACL LOG` must stay empty.

### 4. Turn `default` off (`defaultUser: "off"`)

The chart refuses this while any `clients.<x>` is still false. Only Redis
restarts. Verify `ACL GETUSER default` shows `off`, and that a plain
`redis-cli -a <old password> ping` gets `WRONGPASS`/`NOAUTH`.

Exit: `ACL LIST` shows `default off`, `CLIENT LIST` no `user=default`, and
`ACL LOG` empty for 7 days.

### Rollback

The previous step's values:

- step 4 → `defaultUser: "on"`;
- step 3 → `stage: open` (every client back to `+@all`);
- step 2 → `clients.<x>: false` (back to `default` with the shared
  password);
- step 1 → `acl.enabled: false` (Redis back to `--requirepass`).

Each is one Redis restart (or one client restart for step 2).

### Rotation

Add `<user>-password-previous` with the current password and put the new
one in `<user>-password`. Reloader (or a `kubectl rollout restart`)
restarts Redis, which then accepts both; restart the client; then remove
`-previous` (another Redis restart).
