# Schedule feed SFTP: push account controls

The `sftp` container (SFTPGo v2.7.6) of the `schedulefeed-sftp` pod is the
one service on the node's public IP. It has had its own Deployment since
2026-10-08 (`scheduleFeed.sftp.separateDeployment`), so app deploys no
longer restart it; the container name, and so the Loki queries below, did
not change. DTD (the Rail Data Marketplace push) logs in as
`dtd-push` and uploads the CIF timetable zip, and sometimes the CORPUS
extract. DTD will not pin our host key or publish its source addresses, so an
IP allow-list and host-key pinning are not available. These controls make up
for that.

Code and config:

- `charts/distant-signal/files/schedule-sftp-entrypoint.sh` provisions the
  account through SFTPGo's `--loaddata-from` JSON;
- `charts/distant-signal/templates/schedulefeed-deployment.yaml` wires the
  `scheduleFeed.sftp.*` values into it;
- `crates/schedule-ingest` checks every delivered file.

## Least privilege for `dtd-push`

| Setting | Value | Why |
| --- | --- | --- |
| `permissions` on `/` | `upload`, `overwrite`, `list` | See the evidence below |
| `filters.denied_login_methods` | every method except `password` and `keyboard-interactive` (password mode), or except `publickey` (public-key mode) | DTD logs in with keyboard-interactive, which SFTPGo answers with the account password |
| `filters.denied_protocols` | `FTP`, `DAV`, `HTTP` | SSH/SFTP only. FTP and WebDAV are off server-wide anyway, and the web client with them |
| `max_sessions` | `2` (`scheduleFeed.sftp.maxSessions`) | DTD opens one session per delivery |
| `filters.max_upload_file_size` | 512 MiB (`scheduleFeed.sftp.maxUploadFileSize`) | ~6.6x the largest real file. A larger upload fails and SFTPGo deletes the partial file |

Not granted: `download`, `delete`, `rename`, `create_dirs`, `create_symlinks`,
`chmod`, `chown`, `chtimes`, `copy`. A stolen password can then add or replace
files in the one directory, but can't read, delete, rename or hide anything,
and can't fill the volume with one upload. What it can still do, replace the
timetable with a crafted one, is what schedule-ingest's checks are for
([below](#delivery-checks)).

No quota (`quota_size`) is set: schedule-ingest never deletes the CIF zip, so
SFTPGo's used-quota counter would only grow, and a quota would eventually
block a legitimate delivery.

The SQLite user store is on the container's writable layer, so every
container start creates the account afresh from the loaddata file, and
`--loaddata-mode 0` updates it if it ever already exists. The file holds the
credential, so `--loaddata-clean` deletes it as soon as SFTPGo has loaded it.
The pod template carries a `checksum/sftp-entrypoint` annotation, so a change
to the script rolls the pod.

### Evidence: what DTD's client does

DTD's client identifies as `SSH-2.0-JSCH-0.1.54`. Sources, all read-only on
2026-10-01:

- Loki, `{namespace="distant-signal", container="sftp"}`, 2026-09-30 to
  2026-10-01 (Loki's whole retention at the time). The schedulefeed pod had
  restarted at 16:24Z, so `kubectl logs --since=72h` held nothing older.
- `ls -la --time-style=full-iso` of the PVC from the `ingest` container.

There was one DTD session in that window, on 2026-09-30:

| Time (UTC) | Line | Content |
| --- | --- | --- |
| 19:59:54.927 | `login` | `dtd-push`, method `keyboard-interactive`, client `SSH-2.0-JSCH-0.1.54` |
| 19:59:55.647 | `SFTP` (debug) | transfer added |
| 19:59:59.840 | `Upload` | `/timetable_full.zip`, 77,222,226 bytes, 4,192 ms |
| 19:59:59.875 | `SFTP` | connection closed, exit status 0 |

No `Rename`, `Remove`, `Mkdir`, `Rmdir` or `SetStat` line, all of which
SFTPGo logs at info. So the client uploads straight to the final name: no
temporary name and rename, no mtime preservation, no cleanup.

The PVC shows the same pattern for earlier deliveries. `incoming/`'s own
mtime is 2026-09-28 06:49, unchanged since then, while `timetable_full.zip`
was rewritten on 2026-09-28 20:04:46, 2026-09-29 19:59:01 and 2026-09-30
19:59:59 (the extraction directories' names). A rewrite that leaves the
directory's mtime alone is an in-place overwrite: no entry was created,
deleted or renamed. So `overwrite` is required. Without it the second day's
upload fails, because schedule-ingest reads the zip where it lands and never
removes it.

`list` is kept although no listing was seen. SFTPGo logs neither stat nor
directory reads, and its `Stat`/`Lstat` handlers require `list` on the
parent directory (`internal/sftpd/handler.go`). JSch's `put` stats the
target and treats a failure as "not a directory", so a bare `put` works
without `list`. But JSch's `cd` stats its target and fails without it, and
the log can't show whether DTD's client runs `cd` or `ls` first. Removing it
risks a silent nightly failure; keeping it only lets the account see the
names and sizes of files it delivered itself.

Nothing in the window shows a 16:00 UTC delivery, and the observed time
(~20:00 UTC) is outside schedule-ingest's `checkTimes` overnight window
(22:00 to 01:30 London time). Revisit both once Loki has a month of data.

### Verified against SFTPGo v2.7.5

The release binary (`sftpgo_v2.7.5_linux_x86_64.tar.xz`) was run locally with
this entrypoint and a bolt data provider. With OpenSSH's `sftp` as
`dtd-push` over keyboard-interactive:

| Command | Result |
| --- | --- |
| `put small.zip timetable_full.zip`, twice | both succeed (the second overwrites) |
| `ls -l` | succeeds |
| `get timetable_full.zip` | `remote open: Permission denied` |
| `rename timetable_full.zip x.zip` | `Permission denied` |
| `rm timetable_full.zip` | `Permission denied` |
| `mkdir sub` | `Permission denied` |
| `symlink` | `Operation unsupported` |
| `chmod 600` | `remote setstat: Permission denied` |
| `put` of a file over `maxUploadFileSize` | `Failure`; SFTPGo logs "denying write due to space limit" and deletes the partial file |

SFTPGo does not log refused operations, only the transfers and commands that
succeed, so a probe of these permissions leaves no trace beyond the `login`
line.

The ingest container does not use SFTP. It reads, moves and deletes files on
the shared PVC directly (as group 1000 through the pod's `fsGroup`), so none
of these permissions affect it.

## Password strength

DTD can't pin our host key, so the `dtd-push` password is the whole defence.

- The chart generates it as `randAlphaNum 32` when neither
  `scheduleFeed.sftp.password` nor `existingSecret` is set: 32 characters
  from 62, log2(62^32) ≈ 190.5 bits, far beyond online guessing even
  without the defender. It's kept across upgrades.
- `scheduleFeed.sftp.passwordPolicy.minLength` (24) is checked at render time
  for a password set in values; the error gives the length, not the value.
- A password from `existingSecret` (production's sealed secret) can't be seen
  at render time, so the sftp entrypoint checks its length at start. With
  `passwordPolicy.enforce: true` (the default) a shorter password stops the
  container; with `false` it logs a warning and starts. Either way only the
  length is printed.
- Length is the only automated check. A 24-character password is strong only
  if it is random: generate it, for example with `openssl rand -base64 24`
  (32 characters, 192 bits), never by hand.

## Brute force and abuse

Server-wide SFTPGo settings, all on by default (`scheduleFeed.sftp.*`):

- **SSH commands off** (`sshCommands: []`). The image enables `md5sum`,
  `sha1sum`, `sha256sum`, `cd`, `pwd` and `scp`; DTD uses only the SFTP
  subsystem. Verified: `ssh dtd-push@... sha256sum` and `scp` both get
  "exec request failed".
- **Defender** (`defender.*`): a source IP is banned for 60 minutes (longer
  each time it comes back) once its score reaches 8 in 30 minutes. A wrong
  password for `dtd-push` scores 2 per attempt, and OpenSSH and JSch each make
  two attempts per connection, so about two bad connections ban a source.
  Verified locally: the second wrong-password connection logged
  `"sender":"defender","event":"banned"`, and the correct password from the
  same IP was then refused ("connection refused, ip ... is banned").
  Connections that never authenticate (the kubelet's TCP probe, scanners)
  score 0. Bans are in memory and a pod restart clears them.
- **Per-host cap** of 8 simultaneous connections, and a **per-source rate
  limit** of 20 connections a minute (burst 10); hitting either scores 4.
- `defender.safelist` exempts given IPs/CIDRs from both. Empty: DTD's
  addresses are unknown.

The defender needs real client addresses
(`scheduleFeed.service.externalTrafficPolicy: Local`). Behind source NAT every
client shares one address, and a ban would lock DTD out too.

## Audit log

SFTPGo writes one JSON object per line to stdout, which Alloy ships to Loki
as `{namespace="distant-signal", container="sftp"}`. The chart pins the level
to `debug` (`scheduleFeed.sftp.logLevel`), because failed logins and defender
scores are only logged at debug, and sets `SFTPGO_LOG_UTC_TIME=true`, so the
zone-less `time` field is UTC. Every line has `level` (lowercase), `time`
(`2006-01-02T15:04:05.000`, UTC) and `sender`. Lines below are from the local
SFTPGo 2.7.5 run (IPs are loopback there; in production they are the client's
real address, since `externalTrafficPolicy: Local`).

| `sender` | Level | Fields | Meaning |
| --- | --- | --- | --- |
| `login` | info | `ip`, `username`, `method` (`keyboard-interactive`, `password`, `publickey`), `protocol` (`SSH`), `connection_id`, `client` (SSH version string, DTD's is `SSH-2.0-JSCH-0.1.54`), `encrypted`, `info` (negotiated algorithms) | A successful login |
| `connection_failed` | debug | `client_ip`, `username` (empty if none was tried), `login_type` (`keyboard-interactive`, `password`, `publickey`, `no_auth_tried`), `protocol`, `error` (`invalid credentials`; `not found: username "x" does not exist`) | A failed login. `no_auth_tried` is a connection that never authenticated: the kubelet's TCP probe (every 30s from the pod network) and port scanners |
| `Upload` | info, or error when it failed | `remote_addr` (`ip:port`), `local_addr`, `username`, `file_path`, `virtual_path`, `size_bytes`, `elapsed_ms`, `connection_id` (`SFTP_<login connection_id>_<n>`), `protocol` (`SFTP`), `error` (only on failure) | A completed or failed upload |
| `Rename`, `Remove`, `Mkdir`, `Rmdir`, `SetStat` | info | `remote_addr`, `username`, `file_path`, `target_path`, `connection_id`, ... | A filesystem command that succeeded. `dtd-push` has no permission for any of them, so one appearing means the policy changed |
| `defender` | debug (score), info (ban) | `client_ip`, `protocol`, `event` (`LoginFailed`, `UserNotFound`, `NoLoginTried`, `LimitExceeded`; `banned`), `increase_score_by`, `score` | Defender scoring and bans |

Examples:

```json
{"level":"info","time":"2026-10-01T16:53:57.743","sender":"login","ip":"127.0.0.1","username":"dtd-push","method":"keyboard-interactive","protocol":"SSH","connection_id":"adb66d8d…","client":"SSH-2.0-OpenSSH_10.2","encrypted":true,"info":"negotiated algorithms: {...}"}
{"level":"info","time":"2026-10-01T16:53:57.751","sender":"Upload","local_addr":"127.0.0.1:2299","remote_addr":"127.0.0.1:37628","elapsed_ms":0,"size_bytes":4000,"username":"dtd-push","file_path":"/data/schedule-feed/incoming/timetable_full.zip","virtual_path":"/timetable_full.zip","connection_id":"SFTP_adb66d8d…_1","protocol":"SFTP"}
{"level":"error","time":"2026-10-01T16:53:57.880","sender":"Upload","local_addr":"127.0.0.1:2299","remote_addr":"127.0.0.1:37636","elapsed_ms":15,"size_bytes":0,"username":"dtd-push","file_path":"/data/schedule-feed/incoming/big.zip","virtual_path":"/big.zip","connection_id":"SFTP_cd88a96e…_1","protocol":"SFTP","error":"failure: denying write due to space limit"}
{"level":"debug","time":"2026-10-01T16:53:58.052","sender":"connection_failed","client_ip":"127.0.0.1","username":"dtd-push","login_type":"keyboard-interactive","protocol":"SSH","error":"invalid credentials"}
{"level":"debug","time":"2026-10-01T16:54:01.437","sender":"connection_failed","client_ip":"127.0.0.1","username":"scanner","login_type":"keyboard-interactive","protocol":"SSH","error":"not found: username \"scanner\" does not exist"}
{"level":"debug","time":"2026-10-01T16:54:01.437","sender":"defender","client_ip":"127.0.0.1","protocol":"SSH","event":"UserNotFound","increase_score_by":2,"score":8}
{"level":"info","time":"2026-10-01T16:54:01.437","sender":"defender","client_ip":"127.0.0.1","protocol":"SSH","event":"banned"}
```

Note the field names differ by line: the client address is `ip` on `login`,
`client_ip` on `connection_failed` and `defender`, and `remote_addr` (with the
port) on `Upload`. SFTPGo never logs passwords or keyboard-interactive
answers, and does not log a refused operation (a `get` or `rm` the account
has no permission for), only the ones that succeed.

schedule-ingest adds one line per delivered file (`target` =
`schedule_ingest::audit`, see [delivery checks](#delivery-checks)). Join it
to the `Upload` line on file name, size and time: ingest's `delivered_at` is
the file's mtime, which SFTPGo sets when the upload closes.

### LogQL for login anomalies

For the Loki ruler (Ranma owns the rules). The selectors below use the raw
container stream; once Alloy tags the audit lines, `{audit="sftp-delivery"}`
is the cheaper selector for the login lines.

Successful `dtd-push` logins, as a table:

```logql
{namespace="distant-signal", container="sftp"} |= `"sender":"login"`
  | json username, ip, method, client
  | username = "dtd-push"
```

A successful `dtd-push` login from an IP not seen in the previous 30 days
(needs 30 days of retention for these lines, and a `max_query_length` over
30 days):

```logql
sum by (ip) (count_over_time(
  {namespace="distant-signal", container="sftp"} |= `"sender":"login"`
    | json username, ip | username = "dtd-push" [10m]))
unless on (ip)
sum by (ip) (count_over_time(
  {namespace="distant-signal", container="sftp"} |= `"sender":"login"`
    | json username, ip | username = "dtd-push" [30d] offset 10m))
```

A successful `dtd-push` login outside the expected windows. LogQL has no
hour function, so this matches on the UTC `time` field; label regexes are
anchored. For 22:00 to 01:29 and 16:00 to 16:59 UTC:

```logql
sum by (ip) (count_over_time(
  {namespace="distant-signal", container="sftp"} |= `"sender":"login"`
    | json username, ip, time
    | username = "dtd-push"
    | time !~ `.*T(22|23|00):.*|.*T01:[0-2].*|.*T16:.*` [10m])) > 0
```

The deliveries actually seen came at about 20:00 UTC (19:59 and 20:04), which
those windows would flag. Until a month of logins shows DTD's real pattern,
the observed window is the safer one: `time !~ ".*T(19|20):.*"`.

Failed logins that tried a password (excluding probes):

```logql
{namespace="distant-signal", container="sftp"} |= `"sender":"connection_failed"`
  | json client_ip, username, login_type, error
  | login_type != "no_auth_tried"
```

Bans: `{namespace="distant-signal", container="sftp"} |= "\"event\":\"banned\""`.

## Telemetry and alerts

`scheduleFeed.sftp.telemetry` (on with `metrics.enabled`) serves SFTPGo's
`/metrics` and `/healthz` on port 9097, named `sftp-metrics`, scraped by the
chart's PodMonitor and admitted only from the monitoring namespace. It is not
on the NodePort Service. The counters are global, with no username, IP or
protocol labels, so login anomalies come from the log, not the metrics.

### Alerts

Group `distant-signal.schedule-sftp` (`metrics.prometheusRule.scheduleSftp`):

| Alert | Fires when | What to do |
| --- | --- | --- |
| `DistantSignalSftpNoUpload` | no upload in 30h | Check the `login`/`Upload` lines for DTD's last session; if none, DTD did not push. A CORPUS upload also counts, so `DistantSignalScheduleReferencePublishStale` is the timetable signal |
| `DistantSignalSftpUploadErrors` | an upload failed or was cut off in the last hour | Find the `Upload` line with `"level":"error"` and its `error`; check that the next ingest went through |
| `DistantSignalSftpUserStoreDown` | SFTPGo's user store is down for 5m | Every login fails. Restart the pod; the store is rebuilt from the entrypoint at start |

## Host keys

SFTPGo serves the chart's preserved ECDSA (P-256, for JSch 0.1.54) and
ed25519 host keys (`scheduleFeed.sftp.hostKeys`); there is no RSA key. DTD
does not pin them, so the fingerprints are for information only. They can't
be derived without reading the private keys from the Secret, so read them
from SFTPGo's startup log after a deploy:

```logql
{namespace="distant-signal", container="sftp"} |= `"sender":"sftpd"` |= `fingerprint`
```

(`Host key "/srv/sftpgo/host_keys/ssh_host_ecdsa_key" loaded, type
"ecdsa-sha2-nistp256", fingerprint "SHA256:..."`), or from outside with
`ssh-keyscan -p 30450 <host> | ssh-keygen -lf -`.

| Key | Fingerprint |
| --- | --- |
| `ecdsa-sha2-nistp256` | not yet recorded: the running pod (2026-10-01) still generates throwaway keys; record after the first deploy that serves the preserved ones |
| `ssh-ed25519` | as above |

## Delivery checks

schedule-ingest (`crates/schedule-ingest`) reads what lands in the push
account's directory straight off the shared volume. Anyone with the password
can upload, so nothing is trusted because it arrived.

### CIF checks

Before a delivery is marked complete (so before schedule-reference can
publish from it), schedule-ingest checks it. A failure quarantines the
delivery: an `error` log line and a `quarantined` audit line give the reason,
`schedule_feed_zip_rejected_total` counts it
(`DistantSignalScheduleFeedZipRejected`), the extraction is deleted, and the
previous timetable stays in service until a new upload replaces the zip.

| Check | Where | Real deliveries (2026-09-28 to 09-30) |
| --- | --- | --- |
| Zip: at most 64 entries and 4 GiB uncompressed; each entry inflates to its declared size; no nested or escaping paths | `delivery.rs` (PL-5, before this work) | ~12 entries, ~730 MB |
| Exactly one `RJTTF*MCA.txt` and one `RJTTF*MSN.txt` | `cif_check.rs` | `RJTTF97xMCA.txt`, `RJTTF97xMSN.txt` |
| MCA starts with an `HD` record whose update indicator (column 47) is `F` (full extract), has no other `HD`, and ends with exactly one `ZZ` (not truncated) | `cif_check.rs` | all three |
| Every MCA record type is a CIF one (`HD TI TA TD AA BS BX TN LO LI CR LT LN ZZ`) | `cif_check.rs` | `HD TI AA BS BX LO LI CR LT ZZ` |
| The MSN banner has `/!! Generated: dd/mm/yyyy`, at most one day after the delivery date and at most `cifChecks.maxGeneratedAgeDays` (3) before it | `cif_check.rs` | generated the delivery day |
| `Generated` is not older than the last accepted delivery's | `cif_check.rs` | 28/09, 29/09, 30/09 |
| At least `cifChecks.minSchedules` (100,000) `BS` records | `cif_check.rs` | 504,182 to 505,342 |
| `BS` and `TI` counts fell by at most `cifChecks.maxRecordDropPercent` (20%) since the last accepted delivery | `cif_check.rs` | changes under 0.3% (`TI` 12,095 to 12,096) |

The `HD` record's own dates are not checked: they are a fixed 2011 dataset
identity (`TPS.UCFCATE.PD110719`, user dates `190711`–`300912`) carried in
every real extract, so a check on them would reject every delivery. The
counts of the last accepted delivery are saved as `.cif-stats.json` in its
directory; for a delivery accepted before that file existed, its MCA is read
once to rebuild it. The checks run on the real-shaped excerpt in
`crates/schedule-ingest/tests/fixtures/cif_delivery_excerpt/` (records copied
from the 2026-09-30 delivery, CRLF, 80 columns).

To accept a legitimate delivery a check refuses (for example, a genuine large
timetable change), set that threshold to `0` in
`scheduleFeed.ingest.cifChecks`; the pod restarts and re-reads the zip.
Restore it afterwards.

CORPUS keeps its own checks (`corpus.rs`): gzip, a size cap on
decompression, the `TIPLOCDATA` JSON shape, every row with an NLC, and at
least `scheduleFeed.corpus.minRows` (10,000) rows.

### Provenance and the audit line

Every delivered file is hashed with SHA-256 as schedule-ingest reads it:

- the CIF zip: `schedule_feed_ingests.source_file`, `source_bytes`,
  `source_sha256`, and each extracted file's hash in `files[].sha256`
  (also kept in the delivery directory's `.delivery-complete` marker);
- the CORPUS file: `corpus_deliveries.source_bytes` and `sha256`.

Rows from before 2026-10-01 have NULLs there. Each decision about a file is
logged as one line with `target` `schedule_ingest::audit`:

```json
{"timestamp":"2026-09-30T20:00:35.123456Z","level":"INFO","service":"schedule-ingest","target":"schedule_ingest::audit","message":"delivery decision","file":"timetable_full.zip","bytes":77222226,"sha256":"…","delivered_at":"2026-09-30T19:59:59Z","outcome":"accepted"}
```

| `outcome` | Meaning | `reason` |
| --- | --- | --- |
| `accepted` | extracted (CIF) or loaded (CORPUS) and recorded by api | absent |
| `quarantined` | a CIF zip that failed a check; not retried until a new upload replaces it. Counts in `schedule_feed_zip_rejected_total` (`DistantSignalScheduleFeedZipRejected`) | the failed check |
| `rejected_by_api` | api refused the record with 400/413/422 (`DistantSignalScheduleFeedIngestRejected`) | api's error |
| `corpus_rejected` | a CORPUS file that failed its checks, or an older one superseded unloaded (`DistantSignalCorpusRejected`) | the failed check |

A file whose api POST fails transiently gets its line when the retry
succeeds. Once api accepts a CIF delivery, schedule-ingest writes the zip's
name, size, mtime and `sha256` into the delivery directory's
`.delivery-ingested` file. After a restart it recognises that zip on the
first cycle and does not post it again, so there is no second `accepted`
line. A delivery that was extracted but not yet posted when the pod
stopped is posted then, with no stability wait. So is a directory from
before 2026-10-01 that has no `.delivery-ingested` file. Either one logs
`accepted` once more. Query:

```logql
{namespace="distant-signal", container="ingest"} | json | target = "schedule_ingest::audit"
```

## If DTD's deliveries break

The `ingest` container logs "no .zip delivery observed" after the day's
final check time, and `DistantSignalScheduleReferencePublishStale` fires. To
widen the account without a code change, add the permission to
`scheduleFeed.sftp.permissions` (for example `rename`, if DTD switches to
uploading under a temporary name); the pod restarts with the new policy.

## Alongside the bucket source

The same feed can also arrive through a Google Cloud Storage bucket
([schedule-feed-bucket.md](schedule-feed-bucket.md)), as well as or instead
of SFTP. `scheduleFeed.sftp.enabled` (default `true`) switches this receiver;
`scheduleFeed.bucket.enabled` the bucket. With both on, each delivery is
ingested once (deduplicated by SHA-256) and the bucket copy wins a
disagreement.

Bucket only (`scheduleFeed.sftp.enabled: false`) removes the `sftp`
container, its NodePort Service (the cluster's only public listener), host
keys, entrypoint, SFTP ingress rule, telemetry endpoint and the
`distant-signal.schedule-sftp` alerts. The PVC and the ingest/reference
containers stay. Ranma then drops the SFTP NodePort from
`public-exposure-check.yml` and its monitoring exception. Turn SFTP off only
after the bucket has carried the feed for a while (the adoption steps in
schedule-feed-bucket.md).
