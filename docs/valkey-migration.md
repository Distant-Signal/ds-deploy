# Redis 7.4 to Valkey 8.1

Status: **chart default since DS-174; production not yet switched.** This
page is the runbook for moving a running install (production) from
`redis:7.4.11` to `valkey/valkey:8.1.x`, and for rolling back.

## Why and what changes

Valkey is the BSD-licensed fork of Redis 7.2. Valkey 8.1 speaks the same
protocol, and everything DS uses works unchanged: the stream commands
(XADD, XREADGROUP, XAUTOCLAIM, XCLAIM, XGROUP SETID, XINFO, XTRIM), EVAL,
CLIENT SETINFO, ACL files with selectors, `AUTH default <anything>` on a
`nopass` default user, and the `INFO persistence` fields movement-relay
reads. The chart keeps its `redis.*` values, Secrets, Service, PVC and
probes. The `valkey/valkey` image matches what the chart relies on:

- its entrypoint (tini, then `docker-entrypoint.sh`) prepends
  `valkey-server` when the first arg starts with `-`;
- it has no `USER`, and its `valkey` user is uid/gid 999, as the `redis`
  user was, so `redis.podSecurityContext` and the existing AOF files stay
  valid;
- `/data` is the WORKDIR;
- `redis-cli` and `redis-server` are symlinks to the Valkey binaries, and
  `REDISCLI_AUTH` works, so the `exec` probes and `kubectl exec ...
  redis-cli` are unchanged;
- it has `sh` and `awk` for the redis-acl initContainer.

`INFO server` reports `redis_version:7.2.4` plus `valkey_version:8.1.x`
and `server_name:valkey`.

**The one incompatibility: the AOF's RDB preamble.** Redis 7.4 writes
RDB version 12, and Valkey 8.1 reads only up to RDB 11. With
`aof-use-rdb-preamble yes` (the default, and production's setting), the
AOF's base file is an RDB 12 file (`appendonly.aof.N.base.rdb`), and
Valkey refuses to start on it. So Redis must first rewrite the AOF with a
plain command base (`appendonly.aof.N.base.aof`), which any version
loads. That is step 2 below.

**The way back.** Valkey 8.1 writes RDB 11, which Redis 7.4 reads. So a
rollback is just `redis.image` back to `redis:7.4.11`, at any time,
whatever Valkey has written (rehearsed below). Valkey 9 writes RDB 80,
which Redis cannot read: renovate.json5 disables valkey/valkey major
updates for that reason. Move to 9 only once the rollback is retired.

**`aof-use-rdb-preamble` stays at Valkey's default (`yes`).** After the
cutover, Valkey's next AOF rewrite gives the AOF a compact RDB 11 base
again. That keeps restarts fast and the AOF small (in the rehearsal the
plain base was 272 MB and the RDB base 16 MB for the same data), and RDB 11
is exactly what the rollback needs. Turning the preamble off for good
would buy nothing for the rollback and cost disk and restart time. The
`CONFIG SET ... no` in step 2 is only for the one rewrite before the
switch, and is lost when the Redis pod is replaced.

## Rehearsal (2026-10-26)

Done locally with podman: `redis:7.4.11` with production's arguments
(`--appendonly yes --save "" --maxmemory 2gb --maxmemory-policy noeviction
--proto-max-bulk-len 2mb`, the default preamble) and the chart's rendered
ACL file (`stage: narrow`, `defaultUser: on-unshared`, the metrolink user:
17 users). It held 8 streams like production: `movement-events` with
300,062 entries (258 MB used) and the three consumer groups,
`incident-text-changed` with `enricher`, and six `ds:ingest:*` streams with
`ingest-writer`. Every group had pending entries, written by each
client's own ACL user (XREADGROUP, XACK, XAUTOCLAIM, XCLAIM, XGROUP
SETID), and two groups had pending entries whose stream entries had
already been trimmed. Traffic kept running between the rewrite and the
cutover, so the incr file held MULTI/EXEC blocks of XCLAIM and XGROUP
SETID. Compared before and after: `XLEN`, `XINFO STREAM`, `XINFO GROUPS`
(last-delivered-id, entries-read, lag), the full `XPENDING` list (ids,
consumers, delivery counts), `XINFO CONSUMERS`, a hash of every stream's
`XRANGE`, `ACL LIST` and `ACL USERS`.

| Step | Time | Result |
|---|---|---|
| `BGREWRITEAOF` without preamble (300k entries, 258 MB) | 5.0 s | base `appendonly.aof.7.base.aof`, 272 MB (the RDB 12 base was 15 MB) |
| Redis stop (SIGTERM) | 2.8 s | |
| Valkey 8.1.10 load on the same volume | 0.95 s server-side (4.4 s including container start) | identical, except below |
| Valkey `BGREWRITEAOF` (default preamble) | 0.95 s | base `.base.rdb`, header `REDIS0011` |
| Rollback: `redis:7.4.11` on Valkey's AOF | 0.77 s server-side | identical |
| Control: `redis:7.4.11` on the plain cutover AOF | 1.7 s | identical to Valkey's result |

The DS Redis-backed tests passed against `valkey/valkey:8.1.10`
(`common --test redis_acl`, `ingest-stream --test redis_stream`,
`movement-feed`, `ingest-writer` and `enricher`, all `--ignored`; re-run
2026-10-27 after the `darwin-relay` and `poller-transit-net` users
landed), and `AUTH default <anything>` on a `nopass` default user returned
OK, as on Redis 7.4. The chart's ACL file with every `optional` user on
(19 users) loads in Valkey 8.1.10 when the redis-acl initContainer's own
awk fills it in the Valkey image, the `exec` probe (`redis-cli --user
ds-admin ping`) answers, and each new producer user can XADD only to its
own stream.

Surprises:

1. **Pending entries whose stream entry was already trimmed are dropped.**
   A plain AOF rewrite stores each pending entry as an XCLAIM, and XCLAIM
   of a missing id removes it from the PEL instead. 150 such entries in
   each of two groups were gone after the load; every live pending entry
   came back exactly. Redis 7.4 loading the same AOF drops them too (the
   control row), so this is the rewrite, not Valkey. Nothing is lost: the
   data was already trimmed, and the consumers' XAUTOCLAIM would only have
   reported those ids as deleted (the ingest consumers count them as
   `trimmed`). Expect `pending` in `XINFO GROUPS` to fall by the count from
   pre-check 4.
2. **The plain AOF base is much larger** (18 times here; the synthetic
   payloads compress well, production's about 4:1). With 862 MB used,
   expect a base of roughly 1 GB on disk, written next to the old base and
   incr files. Pre-check 5 checks the PVC has room.
3. **`ACL LIST` omits `sanitize-payload` on Valkey.** Redis 7.4 prints this
   default flag on every user and Valkey does not. The rules are
   otherwise identical, and `ACL USERS` is 17 on both.
4. **Valkey replays the AOF's MULTI/EXEC blocks without ACL checks.** With
   `defaultUser: "off"`, Redis 7.4 logged a `NOPERM` per replayed
   transaction and the groups came back with old last-delivered ids (the
   2026-10-09 incident in docs/redis-acl.md, reproduced), while Valkey
   loaded the same AOF identically with an empty `ACL LOG`. Production
   stays on `on-unshared`, which replays cleanly on both. Do not move to
   `"off"` while the rollback to Redis 7.4 is still possible.
5. **`INFO` says `loading:0` for an instant before the load starts.**
   Wait for `PING` to answer `PONG`, not for `loading:0`. The chart's
   default tcpSocket probes go Ready while the AOF loads, as before (the
   clients get `-LOADING` and retry).

## Production steps

Run by Ranma (`kubectl` against the cluster), each prod write only with
the user's approval. In this page:

```sh
NS=distant-signal
R="kubectl -n $NS exec deploy/distant-signal-redis -c redis --"
CLI="$R redis-cli --user ds-admin"   # REDISCLI_AUTH is set in the pod
```

### 0. Keep the chart bump from switching the image (prod write)

The chart default is Valkey once DS-174 merges, and the next DS release
would switch production with the preamble still in place, and so
crash-loop Redis. Before that release, Ranma-Config pins the current
image:

```yaml
redis:
  image:
    repository: redis
    tag: "7.4.11@sha256:44960b3100c48159a66d28c18b0588c193492e25f7551d60e51dd464fc3eca5b"
```

The DS release then changes nothing for Redis. The cutover in step 3 is
removing this pin.

### 1. Pre-checks (read-only)

1. Image and settings: `$CLI INFO server | grep -E 'redis_version|valkey'`
   (7.4.11), `$CLI CONFIG GET aof-use-rdb-preamble` (yes),
   `$CLI INFO persistence | grep -E 'aof_enabled|aof_last_.*status|aof_rewrite_in_progress'`
   (1, ok, ok, 0).
2. Keys: `$CLI --scan` (8 keys: `movement-events`,
   `incident-text-changed` and six `ds:ingest:*`).
3. Per stream, save the output:
   ```sh
   for k in $($CLI --scan); do
     echo "== $k"; $CLI XLEN "$k"; $CLI XINFO STREAM "$k" | head -20
     $CLI XINFO GROUPS "$k"
     for g in $($CLI --raw XINFO GROUPS "$k" | sed -n '2~12p'); do
       echo "-- $g"; $CLI XPENDING "$k" "$g"
     done
   done > valkey-precheck-$(date +%F).txt
   ```
   Keep the last-delivered-id, entries-read, pending and lag of every group.
4. Dead pending entries (surprise 1), per `movement-events` group:
   ```sh
   first=$($CLI --raw XINFO STREAM movement-events | sed -n '/^recorded-first-entry-id$/{n;p}')
   for g in trust-consumer full-coverage-consumer trust-event-backlog; do
     echo "$g $($CLI --raw XPENDING movement-events $g - "($first" 1000000 | grep -cE '^[0-9]+-[0-9]+$')"
   done
   ```
5. Disk: `$R df -h /data` and `$R du -sh /data/appendonlydir`. Free space
   must be at least 1.5 times `used_memory` (`$CLI INFO memory`), about
   1.3 GB for 862 MB.
6. ACL: `$CLI ACL USERS | wc -l` (17 on 2026-10-26; one more for each of
   the `optional` users `darwin-relay` and `poller-transit-net` whose
   client is on by then) and `$CLI ACL LOG 5` (note what is there; it
   should not grow).
7. Copy of the AOF (read-only, to the operator's machine):
   ```sh
   $R tar cf - -C /data appendonlydir > appendonlydir-pre-valkey-$(date +%F).tar
   ```
   Redis keeps appending while tar reads, so the last command may be cut
   off; Redis loads a truncated tail (`aof-load-truncated yes`). It is the
   last-resort restore point, nothing more.

### 2. Rewrite the AOF without the preamble (prod write)

Right before step 3, in the same session: the setting is lost if the pod
restarts, and an automatic rewrite after a restart brings the preamble
back.

```sh
$CLI CONFIG SET aof-use-rdb-preamble no
$CLI BGREWRITEAOF
# wait for aof_rewrite_in_progress:0 and aof_last_bgrewrite_status:ok
$CLI INFO persistence | grep -E 'aof_rewrite_in_progress|aof_rewrite_scheduled|aof_last_bgrewrite_status|aof_base_size'
$R ls -l /data/appendonlydir
$R cat /data/appendonlydir/appendonly.aof.manifest
```

The manifest's `type b` file must now end in `.base.aof`, not `.base.rdb`.
Rehearsed at 5 s for 258 MB; expect 15 to 30 s for production's 862 MB
on a loaded node. Clients keep running throughout (the rewrite is a fork).
If the rewrite fails (disk full), `aof_last_bgrewrite_status:err`: stop
here, Redis keeps the old AOF, set the preamble back with
`CONFIG SET aof-use-rdb-preamble yes`.

### 3. Deploy Valkey (prod write, passkey)

Remove the step 0 pin in Ranma-Config (the chart default
`valkey/valkey:8.1.10-trixie@sha256:640c5e62...` applies), and approve the
deploy through deploy-bringer. Only the Redis Deployment rolls (Recreate):
Redis stops (about 3 s), then Valkey loads the plain AOF (rehearsed under
a second for 258 MB; expect 3 to 10 s for production). The startup probe
allows 5 minutes. Clients reconnect on their own; movement-relay holds
Kafka records while Redis is away.

If step 2 was done more than a few minutes earlier, or the Redis pod
restarted since, check the manifest again first.

### 4. Verify (read-only)

1. `kubectl -n $NS get pod -l app.kubernetes.io/component=redis -o wide`:
   Running, Ready, no restarts.
   `$CLI INFO server | grep -E 'server_name|valkey_version'`: `valkey`,
   `8.1.10`.
2. `$CLI PING` is `PONG`; `$CLI INFO persistence`: `loading:0`,
   `aof_enabled:1`, `aof_last_write_status:ok`.
3. Streams against the pre-check: the same 8 keys; every group still
   there with a last-delivered-id and entries-read no lower than before
   (traffic continued); `pending` lower only by the step 1.4 count; lag
   back near its usual size within minutes.
4. `$CLI ACL USERS | wc -l` matches the pre-check, and `$CLI ACL LOG 5` has nothing new
   (no `context multi` entries).
5. Every client is back: `$CLI CLIENT LIST | grep -o 'user=[^ ]*' | sort | uniq -c`
   shows each client user and no `user=default`; the consumers' and
   movement-relay's logs have no `NOGROUP`, `NOAUTH`, `WRONGPASS` or
   `NOPERM`; `DistantSignalRedisPersistenceFailing` does not fire;
   Ranma's exporter scrapes (`redis_up` 1).
6. Then, to get a compact RDB 11 base back and fast restarts:
   `$CLI BGREWRITEAOF` (prod write, harmless), and check the manifest's
   base is `.base.rdb` again.

### 5. Rollback

At any point after step 3, put the step 0 pin back in Ranma-Config
(`redis` / `7.4.11@sha256:44960b31...`) and deploy. Redis 7.4 loads what
Valkey wrote: the plain base from step 2, or the RDB 11 base from step
4.6 (rehearsed: identical data). Conditions:

- Valkey stays on 8.x (Renovate's major rule); RDB 80 from Valkey 9
  cannot go back.
- `redis.acl.defaultUser` stays `on-unshared` (surprise 4).

Only if neither Valkey nor Redis can load the AOF: scale the Redis
Deployment to 0, restore `appendonlydir` from the step 1.7 tar onto the
PVC (losing everything written since), and deploy `redis:7.4.11`.

Before step 3 there is nothing to roll back: the rewrite only changed the
AOF's format, which Redis 7.4 loads. To restore the preamble,
`CONFIG SET aof-use-rdb-preamble yes` and `BGREWRITEAOF`, or restart the
pod.
