# Schedule feed bucket source

schedule-ingest can take the CIF timetable (`timetable_full.zip`) and Network
Rail CORPUS (`CORPUSExtract.json.gz`) from a dedicated Google Cloud Storage
bucket that the Rail Data Marketplace pushes into, as well as, or instead of,
the SFTP receiver ([schedule-feed-sftp.md](schedule-feed-sftp.md)). This page
is the operator's view from Distant Signal's side. The design and its
decisions (D1–D13) are in
[the GCS landing design](superpowers/specs/2026-10-02-schedule-feed-gcs-landing-design.md);
the bucket, its IAM and its usage alerts are
[`charts/ds-ingest-bucket`](../charts/ds-ingest-bucket/README.md)'s.

Every name below is a placeholder (`example-project`, `example-ds-ingest`).
The real project, bucket, service accounts and budget live in Ranma-Config,
never in this repository.

**Status (2026-10-02):** the chart side is in place, off by default. The
schedule-ingest bucket source itself (the Rust reader) is not written yet;
until it ships, keep `scheduleFeed.bucket.enabled` false. An image without
it ignores the bucket env vars, so turning it on early does nothing.

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
   true`, `scheduleFeed.bucket.name`, and `scheduleFeed.bucket.existingSecret`
   naming the sealed reader key. Add `bucket.auditLogs.ship` and
   `bucket.auditLogs.bucket` to ship the audit log (below). The image must
   have the bucket source.
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

The reader never re-downloads a confirmed generation, even across restarts,
and has loop guards: at most `bucket.maxDownloadsPerPoll` downloads per
poll, and `bucket.maxDownloadBytesPerHour` / `PerDay` bytes per rolling
hour and day (`DistantSignalScheduleBucketDownloadBudget`). Errors back off
up to `bucket.maxBackoffSecs`. None of this ever blocks or restarts the SFTP
source.

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
private audit-log bucket. With `scheduleFeed.bucket.auditLogs.ship`,
schedule-ingest reads that bucket with the same key every
`auditLogs.pollIntervalSecs` and writes one JSON line per entry to stdout,
target `schedule_ingest::bucket_access` (`principal`, `caller_ip`,
`method`, `object`, `status`, `time`).

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
  | json status | status =~ "PERMISSION_DENIED|403|401"
```

Once Alloy tags these lines, `{audit="bucket-delivery"}` is the cheaper
selector. The Loki rules on them (unexpected principal, publisher deletes,
IAM changes and so on) are Ranma's.
