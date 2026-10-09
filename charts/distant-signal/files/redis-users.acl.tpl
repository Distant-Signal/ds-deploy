# Redis ACL users for Distant Signal (`redis.acl`; docs/redis-acl.md; ingest
# architecture spec §8.2, with decision D1: trust-consumer writes Postgres
# directly, so it has no ingest-stream selector).
#
# This is NOT a Redis ACL file. One user per line:
#
#   <user> <kind> <rules...>
#
# and the chart (templates/redis-acl-configmap.yaml) turns each line into
#
#   user <user> reset on ><password> [><previous password>] <rights>
#
# where <rights> depends on <kind> and on `redis.acl.stage`:
#
#   client  an existing client. Stage `open`: `~* &* +@all` (today's rights,
#           step 1 of the rollout). Stage `narrow`: <rules>.
#   final   a client that does not exist yet (the phase 3 stream producers
#           and the ingest-writer). <rules> in every stage, so phase 3 needs
#           no Redis restart.
#   admin   humans, through `kubectl exec ... redis-cli --user ds-admin`,
#           and the Redis pod's own probes: `~* &* +@all` in every stage.
#
# The `default` user is not listed: `redis.acl.defaultUser` renders it (on
# with the `redis.auth` password and today's rights, or off).
#
# Every user gets the connection handshake explicitly: `+ping +hello +auth`
# (redis-rs connects with AUTH <user> <password>), `+client|setinfo` (it
# then sends CLIENT SETINFO LIB-NAME/LIB-VER and ignores a refusal, which
# would still fill ACL LOG), `+client|setname +client|id`.
#
# Key patterns: `~k` read and write, `%W~k` write only, `%R~k` read only. A
# parenthesised selector applies its own commands to its own keys. The
# rules are checked against a real Redis/valkey, user by user, with each
# client's real command sequence and a set of commands it must NOT be
# allowed: crates/common/tests/redis_acl.rs (CI's rust-test job). Edit this
# file and that test together.

# movement-relay: crates/movement-relay/src/{event_sink,deadletter,main}.rs.
# XADD (NOMKSTREAM) + XTRIM MAXLEN on movement-events, XGROUP CREATE ...
# MKSTREAM for every consumer group, XLEN and XINFO GROUPS for its gauges,
# INFO persistence for the AOF gauges, XTRIM MINID + XRANGE on the
# dead-letter stream.
movement-relay client ~movement-events ~movement-events-deadletter +xadd +xtrim +xgroup|create +xinfo|stream +xinfo|groups +xlen +xrange +exists +type +info +ping +hello +auth +client|setname +client|setinfo +client|id

# The three movement-events consumers: RedisStreamMovementFeed
# (crates/movement-feed/src/redis_stream.rs). XGROUP CREATE ... MKSTREAM,
# XREADGROUP, XACK, XAUTOCLAIM, XPENDING (summary and per consumer), XINFO
# GROUPS/STREAM, EXISTS, XRANGE (replay), and XLEN + XADD on their
# dead-letter stream (never XTRIM: the relay trims it).
trust-consumer client ~movement-events +xreadgroup +xack +xautoclaim +xclaim +xpending +xgroup|create +xgroup|createconsumer +xinfo|stream +xinfo|groups +xlen +xrange +exists +ping +hello +auth +client|setname +client|setinfo +client|id (~movement-events-deadletter +xadd +xlen)
trust-backlog-consumer client ~movement-events +xreadgroup +xack +xautoclaim +xclaim +xpending +xgroup|create +xgroup|createconsumer +xinfo|stream +xinfo|groups +xlen +xrange +exists +ping +hello +auth +client|setname +client|setinfo +client|id (~movement-events-deadletter +xadd +xlen)
# Plus its phase 3a stream (spec §7.1), with final rights now.
full-coverage-consumer client ~movement-events +xreadgroup +xack +xautoclaim +xclaim +xpending +xgroup|create +xgroup|createconsumer +xinfo|stream +xinfo|groups +xlen +xrange +exists +ping +hello +auth +client|setname +client|setinfo +client|id (~movement-events-deadletter +xadd +xlen) (~ds:ingest:full-coverage +xadd +xrevrange)

# enricher: crates/enricher/src/stream.rs (group `enricher` on
# incident-text-changed).
enricher client ~incident-text-changed +xreadgroup +xack +xautoclaim +xgroup|create +xinfo|stream +xinfo|groups +xlen +xrange +exists +ping +hello +auth +client|setname +client|setinfo +client|id

# api: XADD incident-text-changed MAXLEN ~ N (crates/api/src/data/queries.rs).
# Write-only. Until phase 2c moves the publish to poller-incidents; the api
# loses Redis altogether in phase 5.
api client %W~incident-text-changed +xadd +ping +hello +auth +client|setname +client|setinfo +client|id

# Ranma's redis_exporter (not in this chart): read-only metrics commands.
# Checked against the exporter's own command list in a staging run before
# production (docs/redis-acl.md). COMMAND INFO: redis_exporter v1.93 sends it
# on every scrape; refused, it fills ACL LOG (Ranma's staging run, 2026-10-09).
exporter client %R~* +info +ping +config|get +client|list +slowlog|get +slowlog|len +latency|latest +latency|histogram +xinfo|stream +xinfo|groups +xinfo|consumers +xlen +scan +type +memory|usage +command|info +select +hello +auth +client|setname +client|setinfo +client|id

# Future clients, created with their final rights (phase 2c / 3).
poller-incidents final %W~incident-text-changed +xadd +ping +hello +auth +client|setname +client|setinfo +client|id
poller-ldbws final ~ds:ingest:station-samples +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
poller-tfl final ~ds:ingest:tfl +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
poller-tocs final ~ds:ingest:reference +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
poller-irish-rail-gtfs final ~ds:ingest:ioi-gtfs +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
poller-irish-rail-live final ~ds:ingest:ioi-live +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
poller-nir-stations final ~ds:ingest:ioi-nir +xadd +xrevrange +ping +hello +auth +client|setname +client|setinfo +client|id
# ingest-writer (crates/ingest-stream/src/consumer.rs,
# crates/ingest-writer/src/stream.rs): its consumer group and gauges on
# ds:ingest:*, but never XADD, XTRIM or XDEL there, so it cannot forge or
# drop the producers' entries (security review L7); XADD, the MINID XTRIM,
# and the gauges' XLEN, XRANGE and MEMORY USAGE on ds:dlq:* only. (XGROUP
# and XREADGROUP write the group's state, so the source streams stay `~`.)
ingest-writer final ~ds:ingest:* +xreadgroup +xack +xautoclaim +xclaim +xpending +xgroup|create +xgroup|delconsumer +xinfo|stream +xinfo|groups +xinfo|consumers +xlen +memory|usage +ping +hello +auth +client|setname +client|setinfo +client|id (~ds:dlq:* +xadd +xtrim +xlen +xrange +memory|usage)

ds-admin admin
