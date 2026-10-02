# ds-ingest-bucket

The AWS side of the schedule feed's S3 source: the bucket the Rail Data
Marketplace (RDM) pushes `timetable_full.zip` and `CORPUSExtract.json.gz`
into, a private access-log bucket, a write-only IAM user for the publisher,
a read-only IAM user for schedule-ingest, and optional object-created
notifications to SQS. The S3 source runs alongside the SFTP receiver, or
instead of it; `charts/distant-signal`'s `scheduleFeed` switches each one
on separately.

Design and rationale:
[2026-10-02-schedule-feed-s3-landing-design](../../docs/superpowers/specs/2026-10-02-schedule-feed-s3-landing-design.md).

**Off by default** (`enabled: false`) and not deployed anywhere yet.

## What it renders

Every resource is an [ACK](https://aws-controllers-k8s.github.io/community/)
custom resource. Nothing runs in the cluster; the ACK controllers turn the
resources into AWS API calls.

| Template | Kind | Controller | Renders when |
| --- | --- | --- | --- |
| `bucket.yaml` | `s3.services.k8s.aws/v1alpha1` `Bucket` | s3 | `enabled` |
| `log-bucket.yaml` | `Bucket` (access logs) | s3 | `enabled` and `accessLogs.enabled` |
| `iam-users.yaml` | `iam.services.k8s.aws/v1alpha1` `User` (reader; writer in `iamUser` mode) | iam | `enabled` |
| `sqs.yaml` | `sqs.services.k8s.aws/v1alpha1` `Queue` (queue + dead-letter queue) | sqs | `enabled` and `notifications.sqs.enabled` |

The delivery bucket:

- blocks all public access and disables ACLs (`BucketOwnerEnforced`);
- denies any request that isn't TLS 1.2 or later;
- encrypts with SSE-S3 by default;
- is versioned;
- expires current deliveries after 14 days and replaced ones after 3, and
  aborts unfinished multipart uploads after 1 day;
- logs every request to the access-log bucket.

The publisher can only `PutObject` under `bucket.deliveryPrefix`. The reader
can only list and get under it, unless `reader.allowDelete` is set. Both
users carry the permissions boundary in `iam.permissionsBoundaryArn`.

## Prerequisites

1. The ACK `s3-chart` and `iam-chart` controllers, plus `sqs-chart` if
   notifications are on, installed by Ranma-Config, each with:
   - a sealed static-credentials Secret for the controller's IAM user;
   - `aws.region: eu-west-2`;
   - `watchNamespace` set to this release's namespace.

   `helm template` and `helm lint` work without the CRDs. `helm install`
   fails until the controllers are installed.
2. The one-off bootstrap in the AWS account: the controller IAM user, its
   policy and the permissions boundary policy (design spec, "Controller
   credentials").

## Install

```sh
helm upgrade --install ds-ingest-bucket charts/ds-ingest-bucket \
  --namespace ds-ingest-aws --create-namespace \
  --set enabled=true \
  --set aws.accountId=<12 digits> \
  --set bucket.name=<globally unique name> \
  --set iam.permissionsBoundaryArn=arn:aws:iam::<account>:policy/ds-ingest/ds-ingest-boundary
```

In production, Ranma-Config's Flux HelmRelease sets these values instead.

## After install

ACK's iam controller has no `AccessKey` kind, so it never handles a secret
key. Once both `User` resources show `ACK.ResourceSynced=True`:

1. **Reader key.** With the bootstrap profile, run
   `aws iam create-access-key --user-name ds-ingest-reader`. Pipe the
   output straight into `kubeseal`, which writes the
   `distant-signal-schedulefeed-bucket` SealedSecret (keys
   `access-key-id` and `secret-access-key`) in the `distant-signal`
   namespace in Ranma-Config. Don't write it to disk or a terminal
   scrollback you keep.
2. **Writer key** (`writer.mode: iamUser`). Create it the same way and
   enter it in RDM's destination form for the timetable and CORPUS
   subscriptions. Don't keep a copy. If it's lost, make a new one.
3. **Rotation.** Every 90 days, and whenever someone who handled a key
   leaves:
   - create a second key (IAM allows two per user);
   - swap it in (reseal it, or update RDM's form);
   - confirm the next delivery, then delete the old key.

## Values

`values.yaml` documents every key. The ones without a usable default are
`enabled`, `aws.accountId`, `bucket.name` and `iam.permissionsBoundaryArn`,
plus `writer.principalArns` in `crossAccount` mode. The templates refuse
to render without them, and refuse malformed names, prefixes, ARNs and
CIDRs.

## Tests

```sh
helm lint --strict charts/ds-ingest-bucket
helm lint --strict charts/ds-ingest-bucket -f charts/ds-ingest-bucket/ci/example-values.yaml
uv run scripts/check-ingest-bucket-chart.py
```

`check-ingest-bucket-chart.py` renders the chart in each mode and checks
its policies:

- the publisher gets only `PutObject`;
- the reader gets no delete or write;
- TLS-only denies are present;
- the queue accepts only S3 events from this bucket;
- the bad-value guards fail as expected.
