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

Defined in Distant-Signal/ds-core's `access/redis-users.acl.tpl` (its
header explains the format). The chart reads a vendored copy,
`charts/distant-signal/files/redis-users.acl.tpl`, which
`uv run scripts/sync-core.py` writes from the ds-core release
`core-version` names (CI fails when the copy differs). The kinds:

- **client**: an existing client. Stage `open`: `~* &* +@all`, today's
  rights. Stage `narrow`: its own rights.
- **final**: a client that does not exist yet (the phase 3 stream
  producers and `ingest-writer`): its final rights in every stage, so
  phase 3 needs no Redis restart.
- **optional**: a producer that ships off (the transit networks'
  `poller-transit-<network>`): `final` rights, but rendered only while its
  client, `redis.acl.clients.<user in camelCase>`, is on. See
  [Optional users](#optional-users-new-producers-that-ship-off).
- **admin**: `ds-admin`, for humans and the Redis `exec` probes: `~* &* +@all`.

| User | Kind | Narrow rights (summary) |
|---|---|---|
| `movement-relay` | client | `movement-events` and its dead-letter stream: XADD, XTRIM, XGROUP CREATE, XINFO, XLEN, XRANGE, EXISTS; INFO |
| `trust-consumer`, `trust-backlog-consumer` | client | `movement-events`: XREADGROUP, XACK, XAUTOCLAIM, XCLAIM, XPENDING, XGROUP CREATE, XINFO, XLEN, XRANGE, EXISTS; the dead-letter stream: XADD, XLEN. No ingest stream (decision D1 for trust-consumer) |
| `full-coverage-consumer` | client | as above, plus XADD and XREVRANGE on `ds:ingest:full-coverage` (phase 3a) |
| `enricher` | client | `incident-text-changed`: its consumer group commands |
| `exporter` | client | read-only metrics commands (`INFO`, `CONFIG GET`, `CLIENT LIST`, `SLOWLOG`, `LATENCY`, `XINFO`, `SCAN`, `MEMORY USAGE`, `COMMAND INFO`, ...) |
| `poller-incidents` | final | XADD on `incident-text-changed` (phase 2c) |
| `poller-ldbws`, `poller-tfl`, `poller-tocs`, the three island-of-Ireland pollers | final | XADD and XREVRANGE on their own `ds:ingest:*` stream (one each: the island-of-Ireland pollers have `ds:ingest:ioi-gtfs`, `-live` and `-nir`; `poller-tfl` also has `ds:ingest:tfl-stops`, its daily stops job's) |
| `poller-transit-metrolink` | optional | XADD and XREVRANGE on `ds:ingest:transit-metrolink` (transit networks; rendered only with `redis.acl.clients.pollerTransitMetrolink`) |
| `darwin-relay` | optional | XADD and XREVRANGE on `ds:ingest:darwin` (the Darwin Push Port, DS-152; rendered only with `redis.acl.clients.darwinRelay`) |
| `poller-transit-net` | optional | XADD and XREVRANGE on `ds:ingest:transit-net` (Nottingham Express Transit; rendered only with `redis.acl.clients.pollerTransitNet`, Secret key `poller-transit-net-password`) |
| `poller-transit-tfl` | optional | XADD and XREVRANGE on `ds:ingest:transit-tfl` (TfL's Underground, DLR and London Trams timetable; rendered only with `redis.acl.clients.pollerTransitTfl`, Secret key `poller-transit-tfl-password`) |
| `ingest-writer` | final | consumer-group and gauge commands on `ds:ingest:*` (never XADD, XTRIM or XDEL there); XADD, XTRIM, XLEN, XRANGE and MEMORY USAGE on `ds:dlq:*` |
| `ds-admin` | admin | everything |
| `default` | (values) | `defaultUser: "on"`: today's password and rights; `"on-unshared"`: on, with its own password that no client is given (step 4); `"off"`: disabled |

Every user also has the connection handshake (`PING`, `HELLO`, `AUTH`,
`CLIENT SETNAME`, `CLIENT SETINFO`, `CLIENT ID`).

`crates/common/tests/redis_acl.rs` (CI's rust-db-test job, against a real
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
  `enricher-password`, `exporter-password`,
  `poller-incidents-password`, `poller-ldbws-password`,
  `poller-tfl-password`, `poller-tocs-password`,
  `poller-irish-rail-gtfs-password`, `poller-irish-rail-live-password`,
  `poller-nir-stations-password`, `ingest-writer-password`,
  `ds-admin-password`. An `optional` user's key
  (`poller-transit-metrolink-password`, `darwin-relay-password`) only once
  its client is on.
  `api-password` is no longer needed: the `api` user went with the api's
  Redis client (ingest phase 5, step 5.4; see
  [Retired users](#retired-users)).
- **Redis.** A `<release>-redis-acl` ConfigMap holds `users.acl` with
  `${REDIS_ACL_PASSWORD_<USER>}` placeholders. The Redis pod's `redis-acl`
  initContainer (the Redis image's `sh` and `awk`) fills them from the
  Secret into a memory-backed emptyDir; an empty password, or one with
  whitespace, fails the pod instead of starting Redis with a broken file.
  Redis starts with `--aclfile` instead of `--requirepass`, and its probes
  (with `redis.probes.*.type: exec`; tcpSocket by default) run
  `redis-cli --user ds-admin ping`. How Redis picks up a change to the
  users is `redis.acl.reload.mode`: by default (`restart`) a pod
  annotation with the file's checksum restarts Redis when the stage or the
  users change; with `inPlace` a sidecar reloads them without a restart
  (see [Reloading the users in place](#reloading-the-users-in-place)).
- **Clients.** `redis.acl.clients.<client>: true` gives that Deployment
  `REDIS_USERNAME` and `REDIS_PASSWORD` from `<user>-password`. The
  services combine them with `REDIS_URL` in
  `ds_runtime::redis_auth::redis_url_with_credentials`
  (`redis://<user>:<password>@host`). Without `REDIS_USERNAME` nothing
  changes.
- `scripts/render-redis-acl.py` renders the same file outside Helm, for a
  staging Redis (`--passwords-from-env`).

## Reloading the users in place

`redis.acl.reload.mode` (values.yaml):

- **`restart`** (the default): the Redis pod template carries
  `checksum/redis-acl`, the users file's checksum, so any change to it (a
  ds-core users-file change, `stage`, `defaultUser`, an optional client
  turned on or off) restarts Redis: Recreate, about 4 s of AOF load, and
  every stream client reconnects. The initContainer reads the passwords
  from env, so a password change needs a restart too (Reloader or
  `kubectl rollout restart`).
- **`inPlace`**: no ACL change restarts Redis. The pod template no longer
  depends on the users or their passwords:
  - `redis.acl.existingSecret` is mounted as a volume
    (`/etc/redis-acl-secret`; with `defaultUser: "on"` the `redis.auth`
    key too, at `/etc/redis-auth/password`), and the ConfigMap gains a
    password map (`passwords`: which file holds each
    `${REDIS_ACL_PASSWORD_*}`) and the script,
    `files/redis-acl-reload.sh`. The kubelet updates mounted ConfigMaps and
    Secrets in place, within about a minute.
  - The `redis-acl` initContainer runs the script's `render`: the same awk
    program and checks as the restart mode's (CI checks they are
    identical), from the files.
  - A `redis-acl-reload` sidecar (the Redis image's `sh` and `awk`) runs
    the script's `watch`: every `redis.acl.reload.intervalSeconds` (10) it
    renders the file again and, when the result differs from the live
    `users.acl`, swaps it in and sends `ACL LOAD` as `ds-admin`. It
    authenticates with `ds-admin`'s password from the file Redis runs now,
    then the Secret's current and `-previous` ones, so rotating `ds-admin`
    reloads too.
  - The pod template's only checksum is the script's
    (`checksum/redis-acl-reload`): a new version of the script restarts
    Redis once, as switching the mode does. `kubectl exec` defaults to the
    `redis` container.

`ACL LOAD` is all or nothing: if Redis refuses the new file (a rule this
Valkey does not know, say), it keeps its current users ("no change to the
previously active ACL rules was performed"), and the sidecar puts the
previous file back, so a Redis restart loads what it runs now. A render
that fails (a missing or empty `<user>-password` key, a password with
whitespace) changes nothing either. Either way the sidecar logs the reason
(`redis-acl-reload: ACL LOAD failed: ...` or `... the users do not render:
...`) and retries, backing off to about five minutes; it never exits, so it
cannot make the pod unready. On Valkey 8.1 (checked against the pinned
image) clients stay connected through a reload, and their next command
is checked against their new rights; a removed user's clients are
disconnected.

The safe fallbacks:

- `kubectl -n distant-signal rollout restart deploy/distant-signal-redis`
  renders the file from the same volumes at start, exactly as the sidecar
  would;
- `redis.acl.reload.mode: restart` puts back the checksum restart (one
  restart to switch).

Unlike `restart`, where a missing key fails the new Redis pod (and the
Recreate strategy has already stopped the old one), a missing key here
leaves Redis running its current users until the key arrives. But the
initContainer refuses the same file, so fix it before Redis next restarts.

A Helm post-upgrade hook Job was the alternative: it can't reach the
rendered file (a memory emptyDir in the Redis pod), so it would have to
replay every user with `ACL SETUSER`, keeping a second copy of the rules
that drifts from the file Redis loads at its next start, and it would not
see a Secret change made outside a release.

**Ranma-Config.** Production's `redis.podAnnotations` has
`reloader.stakater.com/auto: "true"` (checked 2026-11-08). Reloader's
`auto` restarts the Deployment when any ConfigMap or Secret it references
changes, mounted volumes included, so with `inPlace` it would still restart
Redis on every users or password change. Switch the mode and drop that
annotation in the same Ranma-Config change (one Redis restart for both).
After a change, look for `redis-acl-reload:` in the Redis pod's logs
(`kubectl -n distant-signal logs deploy/distant-signal-redis -c
redis-acl-reload`) and check `ACL USERS`.

## Optional users: new producers that ship off

Every user in the template needs its `<user>-password` key, and any
change to the users file restarts Redis (the ConfigMap checksum on the
pod). So a `final` line for a producer that ships off would restart Redis
on the deploy that merges it, and Redis would not start again until the
Secret had the key. An `optional` line avoids both: the chart renders it
(the ACL line, and the password env in the Redis pod) only while its
client is on, so merging it changes nothing that renders
(`scripts/check-ingest-phase0-chart.py` checks this).

To turn one on (the transit networks' rollout, transit spec §15.1, and
[transit-networks.md](https://github.com/Distant-Signal/ds-pollers/blob/main/docs/transit-networks.md), its runbook):

1. Add `<user>-password` to the users Secret (the SealedSecret in
   Ranma-Config), letters and digits only.
2. Set `redis.acl.clients.<client>: true` in the same Ranma-Config change.
3. With `reload.mode: restart`, Redis restarts once for the new users
   file: about 4 s of AOF load, with the consumers retrying, as any Redis
   restart. If Reloader also restarts Redis for the Secret change, that is
   a second restart in quick succession. With `inPlace`, the sidecar loads
   the user once both the ConfigMap and the Secret key have reached the pod
   (it logs `REDIS_ACL_PASSWORD_<USER> is empty` until the key does).
   Verify `ACL USERS` lists the user.

With `restart`, that is the operational cost of each new producer: one
Redis restart when it is turned on. Turning the client off again removes
the user (another restart, or a reload with `inPlace`, which disconnects
its clients); remove the Secret key after that.

`scripts/render-redis-acl.py --client <key>` renders an optional user
outside Helm.

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
      enricher: true   # then trustBacklogConsumer, fullCoverageConsumer,
                       # trustConsumer, movementRelay, one per release
```

`ingestWriter` (user `ingest-writer`) connects only once an
`ingestWriter.streams` entry is not `off` (plan 3a.3); turn it on before
or with the writer's first stream.

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

### 4. Take `default` away from clients (`defaultUser: "on-unshared"`)

The chart refuses this while any `clients.<x>` is still false. Since
release A, it counts only clients whose workload is deployed. On the chart
before release A, also set `clients.pollerIrishRailGtfs`,
`pollerIrishRailLive` and `pollerNirStations` (they have no pods, so this
is harmless). Set `ingestWriter`, `pollerIncidents`, `pollerLdbws`,
`pollerTfl` and `pollerTocs` here too: on the `http` sinks those pods don't
connect to Redis yet, and release A needs them.

`on-unshared` keeps `default` enabled with `~* &* +@all`, but with its own
password. That password is the `default-password` key of
`redis.acl.existingSecret`: a fresh random value (letters and digits) that
no client is ever given. So no service can authenticate as `default`, and
the old shared `redis.auth` password no longer works.

**Why not `"off"`.** Prod ran with `defaultUser: "off"` on 2026-10-09 and
found that Redis 7.4 ACL-checks the MULTI/EXEC blocks it replays from the
AOF at startup as the `default` user. That replay comes from the fake AOF
client, which is hard-wired to `default`. With `default` off, it rejected
them: ACL LOG showed about 82k `xclaim` and 18k `xgroup|setid` entries in
`context multi` from the fake client (`id=18446744073709551615`). The
`trust-event-backlog` consumer group came back about 18 minutes behind,
and about 10k entries were redelivered. The consumers' dedup keys absorbed
it with no duplicate rows, but it happens again on every restart.
`on-unshared` replays cleanly. `"off"` still renders, for anyone who
accepts that.

Valkey 8.1 (the chart's server since DS-174) does not ACL-check that
replay: in the docs/valkey-migration.md rehearsal it loaded the same AOF
with `default` off identically and an empty `ACL LOG`, where Redis 7.4
lost the groups' progress. Stay on `on-unshared` anyway while a rollback
to Redis 7.4 is possible; revisit `"off"` once it is not.

Steps:

1. Add `default-password` to the users Secret (the SealedSecret in
   Ranma-Config). Mint it with a script that never prints it.
2. Set `redis.acl.defaultUser: "on-unshared"`. Only Redis restarts.
3. Verify:
   - `ACL GETUSER default` shows `on`;
   - a plain `redis-cli -a <old shared password> ping` gets `WRONGPASS`;
   - `CLIENT LIST` has no `user=default`;
   - after the restart, `ACL LOG` has no `context multi` entries from the
     fake client.

Exit: `CLIENT LIST` has no `user=default`, and `ACL LOG` is empty for 7
days.

**Rotating `default-password`:** put a new value in the Secret and restart
Redis (with `reload.mode: inPlace`, wait for the sidecar's reload instead). No client uses this password, so nothing else needs to restart, and
no `-previous` key is needed.

### Ordering against the ingest streams (security review, 2026-10-08)

The chart enforces where the ingest switch-ons fall in this rollout:

- `redis.acl.enabled` with `defaultUser: "on"` needs `redis.auth.enabled`
  (M3): `default` is never passwordless next to the ACL users.
- A stream producer (`pollers.<ldbws|tfl|tocs>.ingest.sink` `http+shadow`
  or `stream`, `fullCoverageConsumer.ingest.sink` `http+shadow` or
  `stream`, an enabled island-of-Ireland poller) and the ingest-writer with
  any stream not `off` need step 3 (`stage: narrow`) and their own
  `clients.<client>` (H3). So step 3 comes before the first `shadow`.
- Any `ingestWriter.streams` entry on `apply` needs step 4
  (`defaultUser: "on-unshared"` or `"off"`, H3). So step 4 comes before the first `apply`.

Rolling back step 4 (or 3) therefore means first moving those streams and
producers back (`apply` to `shadow`, or the sinks to `http`) in the same or
an earlier release.

**Release A (2026-10-09)** makes those producers' `stream` sinks and the
writer's four `apply` streams the chart defaults
(`docs/ingest-phase5-runbook.md`, §0), so it needs step 4 done. Ship it
only after step 4 has had **24 hours of an empty `ACL LOG`** and no
`NOPERM`/`NOAUTH`/`WRONGPASS` in any client's logs. Without step 4 the
release refuses to render, listing what is missing. After release A:

- rolling back a producer is its `ingest.sink: http`; its stream may stay
  on `apply`, which still needs step 4 in place;
- rolling back step 4 needs every stream producer on `http` **and**
  `ingestWriter.streams.<station-samples|full-coverage|tfl|reference>:
  "off"` in the same or an earlier release; rolling back step 3 needs the
  same (no stream may be on `shadow` either).

### Rollback

The previous step's values:

- step 4 → `defaultUser: "on"` (from `on-unshared` or `off`);
- step 3 → `stage: open` (every client back to `+@all`);
- step 2 → `clients.<x>: false` (back to `default` with the shared
  password);
- step 1 → `acl.enabled: false` (Redis back to `--requirepass`).

Each is one Redis restart (or one client restart for step 2).

### Rotation

Add `<user>-password-previous` with the current password and put the new
one in `<user>-password`. With `reload.mode: restart`, Reloader (or a
`kubectl rollout restart`) restarts Redis, which then accepts both; with
`inPlace` the sidecar reloads it within a minute or two (its log says
`reloaded the ACL users`). Restart the client; then remove `-previous`
(another Redis restart, or reload).

## Retired users

- **`api`** (2026-10-14, ingest phase 5 step 5.4). The api's only Redis
  use, `POST /private/incidents`' `XADD incident-text-changed`, went with
  release B; poller-incidents publishes those entries as its own user.
  The api now has no Redis client, no `REDIS_*` env and no NetworkPolicy
  path to Redis, and `files/redis-users.acl.tpl` no longer lists `api`.
  Removing the line changes the ACL ConfigMap, so **that release restarted
  Redis once** (`checksum/redis-acl`, about 4 s of AOF load; every stream
  client reconnects; with `reload.mode: inPlace` it would be a reload). A leftover `redis.acl.clients.api` in values is
  ignored. After the release, Ranma may drop `api-password` from the
  users Secret; that Secret change makes Reloader restart Redis (and every
  client that reads the Secret) once more, so batch it with the next
  rotation. Rollback: the previous chart, which puts the `api` line back
  (and needs `api-password` again, so keep the key until the release has
  settled).
