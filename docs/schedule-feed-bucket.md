# Schedule feed bucket source

schedule-ingest can take the CIF timetable (`timetable_full.zip`) and Network
Rail CORPUS (`CORPUSExtract.json.gz`, or the NLC XML snapshot
`NLC<YYYYMMDD>.xml.gz`) from a dedicated Google Cloud Storage
bucket that the Rail Data Marketplace pushes into, as well as, or instead of,
the SFTP receiver ([schedule-feed-sftp.md](schedule-feed-sftp.md)). This page
is the operator's view from Distant Signal's side. The design and its
decisions (D1–D14) are in
[the GCS landing design](superpowers/specs/2026-10-02-schedule-feed-gcs-landing-design.md);
the bucket, its IAM and its usage alerts are
[`charts/ds-ingest-bucket`](../charts/ds-ingest-bucket/README.md)'s.

Every name below is a placeholder (`example-project`, `example-ds-ingest`).
The real project, bucket, service accounts and budget live in Ranma-Config,
never in this repository.

**Status (2026-10-20, DS-22):** the chart side and the schedule-ingest
bucket source (the Rust reader) are both built, off by default
(`scheduleFeed.bucket.enabled: false`). Nothing is provisioned yet: the
remaining steps are in [Switching it on](#switching-it-on). The audit-log
shipper (`bucket.auditLogs.ship`, [below](#audit-log)) and the Pub/Sub
trigger ([Pub/Sub notifications](#pubsub-notifications)) are built too,
both off by default.

## Switching it on

Each step is the previous one's prerequisite. Every concrete name, id and
email lives in Ranma-Config, never here.

1. **Ranma: the Google project, keyless sign-in and the bucket.** The
   dedicated GCP project and its budget, Crossplane and the GCP providers,
   a `ds-ingest-bucket` release (`enabled: true`, the bucket name, DTD's
   service accounts as `publisher.members`, the reader as `reader.member`)
   with every resource `READY`, and for keyless sign-in the workload
   identity pool and provider (the k3s JWKS uploaded, subject
   `system:serviceaccount:distant-signal:distant-signal-schedulefeed`,
   audience `gcp-ds-ingest`), the `roles/iam.workloadIdentityUser` binding
   on the reader service account, and the `external_account` ConfigMap (its
   `credential_source.file` is `/var/run/secrets/distant-signal/gcs-token/token`).
   See Ranma-Config's `docs/specs/ds-ingest-gcs.md`.
2. **The user: ask RDM/DTD to add the bucket** as a delivery destination
   for the timetable and CORPUS subscriptions, alongside SFTP, with the
   questions in the GCS spec §14 (object names, the scanner, read-back
   time, dual delivery, a test delivery).
3. **Flip the values** in the DS release (Ranma-Config), SFTP still on:

   ```yaml
   scheduleFeed:
     serviceAccount:
       create: true
     bucket:
       enabled: true
       name: <the bucket>
       auth: workloadIdentity
       workloadIdentity:
         credentialConfigMap: <the external_account ConfigMap>
   ```

   (Key mode instead: `auth: key` and `existingSecret` naming the sealed
   reader key.) Add `auditLogs: {ship: true, bucket: <the audit-log
   bucket>}` under `bucket` to ship its audit log (below). The first poll
   logs `bucket source started`; an empty
   bucket is fine. `DistantSignalScheduleBucketAccessRevoked` firing means
   step 1's sign-in is not right yet; SFTP carries on regardless.
4. **Verify both** for a week, as in [Adoption](#adoption-from-dss-side).

## The two switches

`charts/distant-signal` has one switch per source:

| `scheduleFeed.sftp.enabled` | `scheduleFeed.bucket.enabled` | Mode |
| --- | --- | --- |
| `true` (default) | `false` (default) | SFTP only, as before. Nothing bucket-related renders |
| `true` | `true` | Both. Each delivery arrives twice and is ingested once (deduplicated by SHA-256). When the two differ within `scheduleFeed.disagreementWindowMinutes`, `scheduleFeed.sourcePrecedence` decides (the bucket, D5) and `DistantSignalScheduleFeedSourcesDisagree` fires |
| `false` | `true` | Bucket only. No `sftp` container, Service, NodePort, host keys, entrypoint or SFTP alerts: the cluster's only public listener goes away. The PVC and the `ingest`/`reference` containers stay |
| `false` | `false` | Refused: `scheduleFeed.enabled` with neither source fails the render |

With the bucket on, the chart:

- passes the `ingest` container the `SFTP_SOURCE_ENABLED`,
  `BUCKET_SOURCE_ENABLED`, `SOURCE_PRECEDENCE`,
  `DISAGREEMENT_WINDOW_MINUTES`, `BUCKET_*` and
  `GOOGLE_SERVICE_ACCOUNT_PATH` env vars (`GOOGLE_APPLICATION_CREDENTIALS`
  instead of the last in keyless mode; `scheduleFeed.ingest.extraEnv`
  can override any of them);
- mounts the reader key read-only on `ingest` only (below), or in keyless
  mode the credential configuration and a projected token;
- adds the Google endpoints' ports to the schedulefeed egress rule when
  `networkPolicy.egress.internetPorts` narrows it;
- renders the `distant-signal.schedule-bucket` alerts
  ([alerts.md](alerts.md#schedule-bucket));
- refuses bad values at render time, naming the key.

The full value list is the chart README's "scheduleFeed: bucket source"
table.

## Adoption, from DS's side

Steps 1–3 of the design's adoption plan (§12) are Ranma's: the project and
its bootstrap, Crossplane, and a `ds-ingest-bucket` release with its
resources `READY`. Then:

1. **Turn the source on with SFTP still on**: `scheduleFeed.bucket.enabled:
   true`, `scheduleFeed.bucket.name`, and the credential (keyless, or
   `scheduleFeed.bucket.existingSecret` naming the sealed reader key), and
   `scheduleFeed.bucket.auditLogs.ship: true` with the audit-log bucket
   (below).
2. **Ask RDM to add the bucket** as a destination for the timetable and
   CORPUS subscriptions, in addition to SFTP.
3. **Verify both for at least 7 daily deliveries and one CORPUS**: each
   CIF has one `accepted` and one `duplicate` line with the same `sha256`,
   there is no disagreement, the bucket copy arrives within minutes of
   SFTP's, the audit log shows only the publisher's principals writing and
   the reader reading and deleting, and
   `DistantSignalScheduleReferencePublishStale` stays quiet.
4. **Choose the steady state**, reversibly: both (the default), bucket
   only, or SFTP only. Bucket only means `scheduleFeed.sftp.enabled: false`
   (see [schedule-feed-sftp.md](schedule-feed-sftp.md#alongside-the-bucket-source)).

Each step is undone by flipping the value back.

## An object's life

Data is held in the bucket only in transit (D11):

1. The publisher writes `timetable_full.zip` or `CORPUSExtract.json.gz` at
   the bucket root and reads it back.
2. schedule-ingest lists the root every `bucket.pollIntervalSecs`. Nested
   names are never seen.
3. An **expected** object (it matches `bucket.expectedKeys` and is routable
   to CIF or CORPUS) that it has not confirmed before is downloaded, that
   generation only, if its listed size is within `bucket.maxObjectBytes`.
   Size and CRC32C are checked against the object's metadata; the raw
   object is archived on the PVC under
   `/data/schedule-feed/sources/bucket/archive` (`bucket.archiveKeep` of
   them), its name, generation and SHA-256 recorded, and it joins the same
   pipeline as an SFTP delivery. A failed check keeps the object in the
   bucket and retries on a later poll.
4. Anything else is **unexpected**: it is never downloaded, one log line
   names it (escaped and truncated) and
   `DistantSignalScheduleBucketUnexpectedObject` fires.
5. Once an object is `bucket.deleteMinAgeSecs` old (default 1 hour, so
   the publisher's read-back and scan finish) and either confirmed or
   unexpected, it is deleted with `ifGenerationMatch`: a newer upload under
   the same name in between is never lost.

The bucket backs this up: versioning is off, soft delete keeps a deleted
object for 7 days, and a 7-day lifecycle rule deletes anything the reader
missed.

### How the reader hands a delivery on

The reader (`crates/schedule-ingest/src/bucket.rs`, over the GCS JSON API
in `gcs.rs`) runs in its own task, so a slow or refused bucket never holds
up the SFTP scan. A verified object is hard-linked into
`/data/schedule-feed/sources/bucket/incoming/<name>` with its creation
time as its mtime. That folder is the bucket's *lane*: every scan interval
runs the same CIF and CORPUS cycles over each lane, in
`scheduleFeed.sourcePrecedence` order, as over the SFTP landing folder. So
a bucket delivery gets exactly the SFTP path's zip extraction caps, CIF
checks, quarantine, CORPUS JSON/NLC XML handling and records, with the
object's creation time as `delivered_at`.

With the bucket on, every decision is also appended to a content ledger
(`/data/schedule-feed/sources/ledger.jsonl`), and each arrival is judged
against it first (`crates/schedule-ingest/src/sources.rs`):

| What arrives | Outcome |
| --- | --- |
| Content (SHA-256) the other source already delivered | `duplicate`: not extracted, checked or recorded again |
| Different content of the same kind from the other source within `disagreementWindowMinutes` | A disagreement (`schedule_feed_source_disagreement_total{kind}`, `DistantSignalScheduleFeedSourcesDisagree`). The lower-ranked source's copy is `superseded`; the higher-ranked one is ingested, and if the other copy's delivery directory is newer it is retired, so schedule-reference publishes the winner. A quarantined winner never promotes the loser |
| Anything else | The normal path |

The audit lines gain `source` (`sftp` or `bucket`), and on `duplicate` and
`superseded` lines `duplicate_of` and `first_source`. With SFTP alone none
of this runs: no ledger, no new fields.

Every recorded delivery also names its lane in Postgres:
`schedule_feed_ingests.delivery_source` and
`corpus_deliveries.delivery_source` are `sftp` or `bucket` (the accepted
copy's), whichever sources are on (migration
`20261021090500_schedule_delivery_source.sql`, DS-89). Rows from before it,
or from an older schedule-ingest, are NULL: they all came over SFTP.

Reader state on the PVC, all under `/data/schedule-feed/sources/`:

| Path | What |
| --- | --- |
| `bucket/.seen` | Confirmed `(name, generation)` pairs (the last 200), so a restart never downloads one again |
| `bucket/archive/` | Raw objects, `<created>-<generation>-<name>`, the newest `archiveKeep` |
| `bucket/incoming/` | The bucket lane's landing folder |
| `bucket/.partial-*` | A download in progress (removed at start) |
| `ledger.jsonl` | The content ledger (the last 500 to 1000 decisions) |

The reader's metrics, all `{source="bucket"}` and registered at 0:
`distant_signal_schedule_feed_source_access_revoked`,
`..._last_new_object_seconds`, `..._errors_total{kind}` (`auth`, `list`,
`get`, `verify`, `delete`, `size`), `..._unexpected_objects_total{reason}`
(`name`, `size`, `unroutable`), `..._download_capped`,
`..._downloaded_bytes_total` and `..._poll_duration_seconds`; plus
`distant_signal_schedule_feed_source_disagreement_total{kind}` and, keyless,
`distant_signal_gcp_token_exchange_total{stage,outcome}` and
`distant_signal_gcp_token_remaining_seconds`.

The reader never re-downloads a confirmed generation, even across restarts,
and has loop guards: at most `bucket.maxDownloadsPerPoll` downloads per
poll, and `bucket.maxDownloadBytesPerHour` / `PerDay` bytes per rolling
hour and day (`DistantSignalScheduleBucketDownloadBudget`). Errors back off
up to `bucket.maxBackoffSecs`. None of this ever blocks or restarts the SFTP
source.

### Tests against a GCS emulator

`crates/schedule-ingest/src/bucket_e2e_tests.rs` runs the reader end to
end against [fake-gcs-server](https://github.com/fsouza/fake-gcs-server):
paged root listings, generation-pinned downloads, the size, CRC32C and MD5
checks, the archive and seen ledger, the delete once
`deleteMinAgeSecs` old, the loop guards, and cross-source dedup with the
SFTP lane. A small in-process proxy in front of the emulator tampers with
listings and downloads, plays the publisher mid-poll, refuses calls (403)
and enforces `ifGenerationMatch` on deletes, which fake-gcs-server 1.55
ignores. No Google credentials: the reader signs in with a throwaway key.
The tests are `#[ignore]`d and need `FAKE_GCS_URL`; CI's `rust-db-test`
job runs them:

```sh
docker run -d --rm --name fake-gcs -p 127.0.0.1:4443:4443 \
  fsouza/fake-gcs-server:1.55 -scheme http -port 4443 -backend memory
FAKE_GCS_URL=http://127.0.0.1:4443 \
  cargo test -p schedule-ingest bucket_e2e_tests -- --ignored
```

### PVC sizing

The archive lives on the existing schedulefeed PVC. With the bucket on,
budget `(bucket.archiveKeep + 1) × bucket.maxObjectBytes` on top of today's
use. The defaults fit in the default 5Gi: about 0.5 GB for real 78 MB
objects, against about 2.3 GB for three extracted deliveries.

## The reader key

The reader is a Google service account with `roles/storage.objectViewer`
and a delete-only custom role on the bucket (and `objectViewer` on the
audit-log bucket). Its JSON key:

- is created once by a person (`gcloud iam service-accounts keys create`
  piped straight into `kubeseal`, never written to disk) and sealed in deploy
  config as the Secret `scheduleFeed.bucket.existingSecret` names, under
  the key `scheduleFeed.bucket.serviceAccountKey` (default
  `service-account.json`);
- is never put in values, and the chart never renders it: it only
  references the Secret by name;
- is mounted read-only at `/var/run/secrets/distant-signal/gcs` on the
  `ingest` container only, as an **optional** volume. A missing Secret or
  key leaves the pod (and SFTP) running; the bucket source reports revoked
  access instead;
- is mounted without `subPath`, so a rotated key reaches the running pod
  with no restart; the reader re-reads it on each retry.

**Rotation, every 90 days, with two keys overlapping:** create the new key,
reseal it into the same Secret, wait for the next poll to succeed (the
`DistantSignalScheduleBucketAccessRevoked` gauge stays 0 and schedule-ingest
logs a successful listing), then delete the old key in Google Cloud.

Never read the Secret to check it. `kubectl get secret <name>` shows that
it exists; that is enough.

### Keyless instead: workload identity federation

`scheduleFeed.bucket.auth: workloadIdentity` drops the key. Google's
workload identity pool trusts the k3s token issuer (an uploaded JWKS) and
lets exactly the subject
`system:serviceaccount:<namespace>:<release>-schedulefeed` impersonate the
reader's service account. The pod gets a projected token (audience
`gcp-ds-ingest`, one hour, rotated by the kubelet) at
`/var/run/secrets/distant-signal/gcs-token/token` and an `external_account`
credential configuration from a ConfigMap (no secret in it), and the
reader exchanges the token at Google STS and then `generateAccessToken` for
an hour-long access token. Nothing to rotate. The values, the ConfigMap's
exact JSON and the guards are in the chart README's "Keyless bucket access
(workload identity federation)"; the reader's side is the GCS spec's
"Keyless reader credentials". It needs a schedule-ingest image whose bucket
reader supports `external_account`. Key mode stays the default, for any
deployment without workload identity federation.

In keyless mode, revoked access means the impersonation binding or the
pool provider was removed (or the ConfigMap is missing), and the kill
switch works the same way: remove the reader's bucket bindings.

## Pub/Sub notifications

Off by default (`scheduleFeed.bucket.notifications.pubsub.enabled: false`):
polling alone is the chosen design (GCS spec §6) and loses nothing. With
it on, the bucket's `OBJECT_FINALIZE` notifications go to a Pub/Sub topic
with a pull subscription, and schedule-ingest
(`crates/schedule-ingest/src/pubsub.rs`) pulls it over outbound HTTPS, so
a new object is fetched within seconds instead of at the next poll:

- A message for this bucket's `OBJECT_FINALIZE` wakes the reader for a
  poll right away (at most one every 60 s, never during a backoff), and is
  acknowledged once a poll that started after it has finished (its ack
  deadline extended meanwhile, for up to 30 minutes). Any other message is
  acknowledged at once.
- A message is only a hint: the listing decides what is fetched and every
  guard applies as before. A lost, duplicated or forged message changes
  the timing only.
- Polling stays as the fallback: every `reconcileIntervalSecs` (default 1
  hour) while the subscription delivers, every `pollIntervalSecs` while it
  fails (refused, missing, unreachable). A failure is logged once per
  change and retried with backoff.
- Metrics, all `{source="bucket"}`:
  `distant_signal_schedule_feed_source_notifications_healthy` (1 while
  pulls succeed), `..._notifications_total{outcome}` (`wake`, `ignored`) and
  `..._notification_errors_total{kind}` (`auth`, `pull`, `ack`).

### What Ranma creates (RANMA-87)

Everything below is in Ranma-Config, with its real names; the names here
are `charts/ds-ingest-bucket`'s defaults and placeholders.

1. **The Pub/Sub API** on the dedicated project:
   `gcloud services enable pubsub.googleapis.com --project=<project>` (or
   its OpenTofu `google_project_service`).
2. **The Cloud Storage service agent**, which publishes the notifications:
   `gcloud storage service-agent --project=<project>` prints
   `service-<project number>@gs-project-accounts.iam.gserviceaccount.com`
   (and creates it if it does not exist yet).
3. **The Crossplane Pub/Sub provider**: `provider-gcp-pubsub` from
   provider-upjet-gcp v3.0.0 (`xpkg.upbound.io/upbound/provider-gcp-pubsub`),
   on the same `ClusterProviderConfig` as the storage provider. The
   provider's service account needs to create topics and subscriptions and
   set their IAM policies (`roles/pubsub.admin` on the project), on top of
   what it has for the bucket (the bucket notification is a bucket update).
4. **The `ds-ingest-bucket` release values**:

   ```yaml
   gcp:
     projectNumber: "<the project's number>"
   notifications:
     pubsub:
       enabled: true
       # The defaults, shown for reference:
       topicName: ds-ingest-events
       subscriptionName: ds-ingest-events-schedule-ingest
       ackDeadlineSeconds: 600
       messageRetentionDuration: 604800s
   ```

   which renders, and Crossplane must report `READY` for each:

   | Resource | What |
   | --- | --- |
   | `Topic` `ds-ingest-events` | The notification topic |
   | `TopicIAMMember` | `roles/pubsub.publisher` on the topic for `serviceAccount:service-<project number>@gs-project-accounts.iam.gserviceaccount.com` only |
   | `Subscription` `ds-ingest-events-schedule-ingest` | Pull (no push endpoint), ack deadline 600 s, 7-day retention, never expires, no dead-letter topic |
   | `SubscriptionIAMMember` | `roles/pubsub.subscriber` on the subscription for `reader.member` only (the reader service account; keyless sign-in impersonates it, so nothing more is needed) |
   | `Notification` | On the delivery bucket: `OBJECT_FINALIZE` only, payload `JSON_API_V1`, to the topic |

5. **The DS release values**, once those are `READY`:

   ```yaml
   scheduleFeed:
     bucket:
       notifications:
         pubsub:
           enabled: true
           subscription: projects/<project>/subscriptions/ds-ingest-events-schedule-ingest
   ```

   The chart adds `pubsub.googleapis.com` to the schedulefeed egress rule.
   Check: the `ingest` container logs `bucket Pub/Sub subscriber started`,
   then `the bucket's Pub/Sub subscription is delivering`, and
   `..._notifications_healthy` is 1; an upload logs `a new bucket object
   was notified; polling the bucket early`.

The kill switch needs nothing new: Pub/Sub carries a few small messages a
day. Removing the subscriber binding only turns the reader back to polling
every `pollIntervalSecs`; the watcher need not pause it.

## Restoring a deleted object

Soft delete keeps every deleted object for 7 days, including unexpected
objects deleted unread. To inspect one, find its generation and restore it
(placeholders only; use the real bucket from Ranma-Config):

```sh
gcloud storage ls --soft-deleted gs://example-ds-ingest/
gcloud storage restore gs://example-ds-ingest/<name>#<generation>
```

Download it with your own credentials, not the reader's, and delete it
again afterwards: a restored expected object would otherwise be picked up
by the reader on its next poll as a new generation.

## Kill switch

Ranma owns the kill switch (D13). When a budget or usage alert trips, it
removes IAM bindings in Google Cloud, and its watcher pauses the matching
`BucketIAMMember` resources (`crossplane.io/paused: "true"`, selected by the
label `ds-ingest-bucket/kill-switch-group: publisher` or `reader`) so
Crossplane doesn't re-create them. The chart only sets the labels; it never
sets `crossplane.io/paused`, so a Helm upgrade leaves a pause in place.

| Trip | What DS sees | What to do |
| --- | --- | --- |
| Egress spike: the reader is removed | `DistantSignalScheduleBucketAccessRevoked` within minutes. The bucket source backs off and logs once per state change; SFTP carries on | Find the cause in the audit log (a reader loop shows as repeated `objects.get` of one generation) and fix it, then ask Ranma to reapply. Nothing in DS restarts it |
| Ingress or write spike: the publishers are removed | Nothing new in the bucket; `DistantSignalScheduleBucketNoNewObject` after 30 hours (if SFTP is off, `DistantSignalScheduleReferencePublishStale` too). The publisher's transfers fail on their side | Read the audit log for what was written and by whom, examine soft-deleted or unexpected objects, then a deliberate reapply |
| Budget: everything is removed | Both of the above | As above, plus the billing report |

Recovery is always a deliberate Ranma reapply, which removes
`crossplane.io/paused` from the paused bindings. A missing or rotated reader
key looks the same from DS (`AccessRevoked`); check the Secret exists first.

## Audit log

The bucket's Cloud Audit Logs (Data Access on, so every object read, write
and delete is logged with the caller's principal and IP) are sunk into a
private audit-log bucket by Ranma's Logging sink, as hourly files
`cloudaudit.googleapis.com/<log>/YYYY/MM/DD/<hour>_S<n>.json`. With
`scheduleFeed.bucket.auditLogs.ship` on (and `auditLogs.bucket` naming that
bucket), schedule-ingest (`crates/schedule-ingest/src/bucket_audit.rs`)
lists it every `auditLogs.pollIntervalSecs` with the reader's own
credential, downloads each new file (CRC32C checked, at most 64 MiB) and
writes one JSON line per entry to stdout, target
`schedule_ingest::bucket_access`, message `bucket access`:

| Field | From the entry |
| --- | --- |
| `principal` | `protoPayload.authenticationInfo.principalEmail` |
| `caller_ip` | `protoPayload.requestMetadata.callerIp` |
| `method` | `protoPayload.methodName` (`storage.objects.get`, `.create`, `.delete`, `storage.setIamPermissions`, ...) |
| `object` | the object's name, when `resourceName` is an object (absent for a bucket-level call) |
| `resource` | `protoPayload.resourceName`, in full |
| `status` | `OK`, or the `google.rpc.Code` name (`PERMISSION_DENIED`, `NOT_FOUND`, ...) |
| `time` | the entry's `timestamp` |
| `log` | `activity` (Admin Activity) or `data_access` |
| `insert_id` | the entry's `insertId`, unique per entry |

Every folder from two days back is listed, so a late shard of an earlier
hour is still shipped; each file is shipped once, recorded in
`/data/schedule-feed/sources/bucket/.audit-shipped`, so a restart ships
nothing twice (a crash between a file's lines and that record ships the
file again: `insert_id` tells copies apart). A first start ships the last
two days. A refused listing (the reader has no `objectViewer` on the
audit-log bucket) is logged once and retried at `maxBackoffSecs`; nothing
here affects the reader or SFTP. Metrics:
`distant_signal_schedule_feed_bucket_audit_lines_total` and
`..._bucket_audit_errors_total{kind}` (`auth`, `list`, `get`, `verify`,
`parse`).

Every bucket call, as a table:

```logql
{namespace="distant-signal", container="ingest"} |= `schedule_ingest::bucket_access`
  | json principal, caller_ip, method, object, status, time
  | line_format "{{.time}} {{.principal}} {{.method}} {{.object}} {{.status}}"
```

A reader loop (the same object fetched again and again):

```logql
sum by (object) (count_over_time(
  {namespace="distant-signal", container="ingest"} |= `schedule_ingest::bucket_access`
  | json method, object | method = "storage.objects.get" [1h]))
```

Refused calls:

```logql
{namespace="distant-signal", container="ingest"} |= `schedule_ingest::bucket_access`
  | json status | status =~ "PERMISSION_DENIED|UNAUTHENTICATED"
```

Once Alloy tags these lines, `{audit="bucket-delivery"}` is the cheaper
selector. The Loki rules on them (unexpected principal, publisher deletes,
IAM changes and so on) are Ranma's.
