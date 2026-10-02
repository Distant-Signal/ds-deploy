# ds-ingest-bucket

The Google Cloud side of the schedule feed's bucket source: a dedicated
Cloud Storage bucket the Rail Data Marketplace (RDM) pushes
`timetable_full.zip` and `CORPUSExtract.json.gz` into, bucket-level IAM
for the publisher's service accounts and for schedule-ingest's reader, a
private audit-log bucket, and optional usage alerts and Pub/Sub object
notifications. The bucket source runs alongside the SFTP receiver, or
instead of it; `charts/distant-signal`'s `scheduleFeed` switches each one
on separately.

Design and rationale:
[2026-10-02-schedule-feed-gcs-landing-design](../../docs/superpowers/specs/2026-10-02-schedule-feed-gcs-landing-design.md)
(the file name predates the move from S3 to GCS).

**Off by default** (`enabled: false`) and not deployed anywhere yet. This
chart holds only generic templates: every project id, bucket name and
service-account email is set by Ranma-Config.

## What it renders

Crossplane v2 namespaced managed resources from
[provider-upjet-gcp](https://github.com/crossplane-contrib/provider-upjet-gcp).
Nothing runs in the cluster; the providers turn the resources into GCP API
calls.

| Template | Kinds | Provider | Renders when |
| --- | --- | --- | --- |
| `bucket.yaml` | `storage.gcp.m.upbound.io/v1beta1` `Bucket` (delivery) | storage | `enabled` |
| `audit-bucket.yaml` | `Bucket` (audit-log sink destination) | storage | `enabled` and `auditLogs.bucket.enabled` |
| `iam.yaml` | `BucketIAMMember` (publisher members × roles, reader, audit sink); `cloudplatform.gcp.m.upbound.io/v1beta1` `ProjectIAMCustomRole` (the reader's delete-only role) | storage, cloudplatform | `enabled` |
| `alerts.yaml` | `monitoring.gcp.m.upbound.io/v1beta1` `AlertPolicy` × 6 | monitoring | `enabled` and `usageAlerts.enabled` |
| `pubsub.yaml` | `pubsub.gcp.m.upbound.io/v1beta1` `Topic`, `Subscription`, `TopicIAMMember`, `SubscriptionIAMMember`; `storage` `Notification` | pubsub, storage | `enabled` and `notifications.pubsub.enabled` |

The delivery bucket:

- has uniform bucket-level access and enforced public access prevention;
- uses Google-managed encryption (CMEK optional);
- is unversioned, with a 7-day soft delete: data is only in transit, since
  schedule-ingest deletes each object after a verified download;
- deletes anything older than 7 days (a backstop for a reader outage) and
  aborts unfinished multipart uploads after 1 day.

Every grant is a non-authoritative, bucket-level `BucketIAMMember`, never
project-level. Publisher members get the roles in `publisher.roles`
(default: the publisher's four documented roles). The reader gets
`objectViewer` plus a custom role holding only `storage.objects.delete`;
it can never create or overwrite. IAM bindings are fully managed, so
removing a member from the values revokes it; the buckets are orphaned on
delete and carry `helm.sh/resource-policy: keep`.

## Prerequisites

1. Crossplane v2 and the GCP providers (`provider-gcp-storage`,
   `provider-gcp-cloudplatform`; `-monitoring` and `-pubsub` if used),
   installed by Ranma-Config, with a `ClusterProviderConfig` (default
   name `default`) holding the controller's sealed service-account key.
   `helm template` and `helm lint` work without the CRDs.
2. The one-off project bootstrap in Ranma-Config: the project, budget,
   the reader's service account, the Data Access audit config and sink,
   and the kill switch (design spec §7, §12).

## Install

In production, Ranma-Config's Flux HelmRelease sets the values. By hand
(placeholders):

```sh
helm upgrade --install ds-ingest-bucket charts/ds-ingest-bucket \
  --namespace ds-ingest-gcp --create-namespace \
  --set enabled=true \
  --set gcp.projectId=<project-id> \
  --set bucket.name=<globally unique name> \
  --set 'publisher.members[0]=serviceAccount:<publisher>@<publisher-project>.iam.gserviceaccount.com' \
  --set reader.member=serviceAccount:<reader>@<project-id>.iam.gserviceaccount.com
```

## After install

- **Publisher.** Nothing to hand over: the publisher's own service
  accounts are bound. To revoke, remove them from `publisher.members`.
- **Reader key.** The controller never mints keys (a key minted by a
  managed resource would land unsealed in a Secret). With the bootstrap
  identity, run `gcloud iam service-accounts keys create` for the reader
  and pipe it straight into `kubeseal`, producing the
  `distant-signal-schedulefeed-bucket` SealedSecret (key
  `service-account.json`) in Ranma-Config. Don't write it to disk.
- **Rotation**, every 90 days: create a second key, reseal, roll the
  ingest pod, confirm a successful poll, delete the old key.
- **Kill switch.** Ranma's function removes bindings on a usage or budget
  trip. Binding resources carry the label
  `ds-ingest-bucket/kill-switch-group` (`publisher` or `reader`) so that a
  watcher can pause them (`crossplane.io/paused: "true"`) and Crossplane
  doesn't re-create them. The chart never sets that annotation itself.

## Values

`values.yaml` documents every key. Required when enabled: `gcp.projectId`,
`bucket.name`, `publisher.members` and `reader.member`; also
`usageAlerts.notificationChannels` with `usageAlerts.enabled`, and
`gcp.projectNumber` with `notifications.pubsub.enabled`. The templates
fail closed without them, and refuse public or non-service-account
members, roles that can change IAM or bucket settings, and out-of-range
soft delete.

## Tests

```sh
helm lint --strict charts/ds-ingest-bucket
helm lint --strict charts/ds-ingest-bucket -f charts/ds-ingest-bucket/ci/example-values.yaml
uv run scripts/check-ingest-bucket-chart.py
```

`check-ingest-bucket-chart.py` renders every mode and checks the bucket
settings (UBLA, enforced PAP, versioning off, soft delete, lifecycle, no
retention, orphan and keep), every grant (bucket-level only, service
accounts only, never public, fully managed, exactly the expected
publisher and reader bindings, the delete-only custom role), and that bad
values refuse to render.
