#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Render charts/ds-ingest-bucket and check its GCS bucket and IAM settings.

  scripts/check-ingest-bucket-chart.py [--helm HELM]

Renders the chart with ci/example-values.yaml in several modes and checks:

  - disabled (the default) renders nothing;
  - both buckets have uniform bucket-level access, public access prevention
    enforced, no object retention, no force-destroy, EUROPE-WEST2 and
    `helm.sh/resource-policy: keep`, and are orphaned on delete;
  - the delivery bucket is unversioned (data is only in transit: the
    reader deletes after a verified download), keeps soft-deleted objects
    7 days, and has exactly two lifecycle rules (the 7-day backstop delete
    and aborting unfinished multipart uploads after 1 day);
  - every IAM grant is a bucket-level, non-authoritative BucketIAMMember
    (or, with Pub/Sub, a topic/subscription member): no project-level IAM,
    no *IAMPolicy/*IAMBinding, every member a `serviceAccount:` and never
    allUsers/allAuthenticatedUsers, and every binding fully managed so that
    removing it revokes it;
  - the publisher bindings are exactly members x the four default roles on
    the delivery bucket; the reader has exactly objectViewer plus the
    delete-only custom role (whose only permission is storage.objects.delete)
    on the delivery bucket and objectViewer on the audit-log bucket, so it
    can never create or overwrite; the sink writer only objectCreator on
    the audit-log bucket;
  - usage alerts and Pub/Sub render only when enabled, and the topic only
    accepts the Cloud Storage service agent;
  - bad values fail to render, including enabled with no publisher members.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "ds-ingest-bucket"
EXAMPLE = CHART / "ci" / "example-values.yaml"
BUCKET = "example-ds-ingest"
AUDIT_BUCKET = "example-ds-ingest-audit"
PUBLISHERS = (
    "serviceAccount:publisher@example-publisher.iam.gserviceaccount.com",
    "serviceAccount:scanner@example-publisher.iam.gserviceaccount.com",
)
READER = "serviceAccount:reader@example-project.iam.gserviceaccount.com"
SINK = "serviceAccount:audit-sink@example-project.iam.gserviceaccount.com"
PROJECT_NUMBER = "000000000000"
STORAGE_AGENT = (
    f"serviceAccount:service-{PROJECT_NUMBER}"
    "@gs-project-accounts.iam.gserviceaccount.com"
)
PUBLISHER_ROLES = (
    "roles/storage.objectViewer",
    "roles/storage.legacyBucketReader",
    "roles/storage.bucketViewer",
    "roles/storage.legacyBucketWriter",
)
ORPHAN_POLICIES = ["Observe", "Create", "Update", "LateInitialize"]
EXPECTED_LIFECYCLE = [
    {"action": {"type": "Delete"}, "condition": {"age": 7}},
    {"action": {"type": "AbortIncompleteMultipartUpload"}, "condition": {"age": 1}},
]
SOFT_DELETE_SECONDS = 7 * 86400
DELETE_ROLE = "projects/example-project/roles/dsIngestObjectDeleter"
# Bucket size, object count, write and delete requests, received and sent bytes.
USAGE_ALERTS = 6
IAM_MEMBER_KINDS = {"BucketIAMMember", "TopicIAMMember", "SubscriptionIAMMember"}
SERVICE_ACCOUNT = re.compile(
    r"^serviceAccount:[^@\s]+@[a-z0-9.-]+\.gserviceaccount\.com$"
)
PUBSUB_SET = (
    "--set",
    "notifications.pubsub.enabled=true",
    "--set",
    f"gcp.projectNumber={PROJECT_NUMBER}",
)
ALERTS_SET = (
    "--set",
    "usageAlerts.enabled=true",
    "--set",
    "usageAlerts.notificationChannels[0]=projects/example-project/notificationChannels/0",
)

type Doc = dict[str, object]
type Binding = tuple[str, str, str]  # (bucket or topic/subscription, role, member)


class Checker:
    """Collects failed checks."""

    def __init__(self, helm: str) -> None:
        """Use `helm` to render."""
        self.helm = helm
        self.failures: list[str] = []

    def check(self, *, ok: bool, message: str) -> None:
        """Record `message` as a failure unless `ok`."""
        if not ok:
            self.failures.append(message)

    def render(self, *args: str) -> tuple[int, str]:
        """Run `helm template` with the example values and `args`."""
        result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "t", str(CHART), "-f", str(EXAMPLE), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def docs(self, *args: str) -> list[Doc]:
        """Render and parse every non-empty YAML document."""
        code, out = self.render(*args)
        if code != 0:
            self.failures.append(f"render {list(args)} failed: {out.strip()}")
            return []
        return [cast("Doc", d) for d in yaml.safe_load_all(out) if d]


def spec(doc: Doc) -> dict[str, object]:
    """Return the resource's spec."""
    return cast("dict[str, object]", doc["spec"])


def for_provider(doc: Doc) -> dict[str, object]:
    """Return the managed resource's spec.forProvider."""
    return cast("dict[str, object]", spec(doc)["forProvider"])


def by_kind(docs: Sequence[Doc], kind: str) -> dict[str, Doc]:
    """Resources of `kind`, by metadata.name."""
    out: dict[str, Doc] = {}
    for doc in docs:
        if doc.get("kind") == kind:
            name = cast("dict[str, str]", doc["metadata"])["name"]
            out[name] = doc
    return out


def buckets(docs: Sequence[Doc]) -> dict[str, Doc]:
    """Bucket resources, by their GCS name (forProvider has none; use external-name)."""
    out: dict[str, Doc] = {}
    for doc in by_kind(docs, "Bucket").values():
        meta = cast("dict[str, dict[str, str]]", doc["metadata"])
        out[meta["annotations"]["crossplane.io/external-name"]] = doc
    return out


def bindings(docs: Sequence[Doc], kind: str = "BucketIAMMember") -> set[Binding]:
    """Every (target, role, member) granted by IAM member resources of `kind`."""
    out: set[Binding] = set()
    for doc in by_kind(docs, kind).values():
        fp = for_provider(doc)
        target = fp.get("bucket") or fp.get("topic") or fp.get("subscription")
        out.add((str(target), str(fp["role"]), str(fp["member"])))
    return out


def check_bucket_privacy(c: Checker, name: str, doc: Doc) -> None:
    """Check UBLA, PAP, no retention, location, keep and orphan-on-delete."""
    fp = for_provider(doc)
    c.check(ok=fp.get("uniformBucketLevelAccess") is True, message=f"{name}: UBLA off")
    c.check(
        ok=fp.get("publicAccessPrevention") == "enforced",
        message=f"{name}: public access prevention not enforced",
    )
    c.check(
        ok=fp.get("enableObjectRetention") is False and "retentionPolicy" not in fp,
        message=f"{name}: object retention or a retention policy is set",
    )
    c.check(
        ok=fp.get("forceDestroy") is False, message=f"{name}: forceDestroy not false"
    )
    c.check(
        ok=fp.get("location") == "EUROPE-WEST2", message=f"{name}: not EUROPE-WEST2"
    )
    c.check(ok="logging" not in fp, message=f"{name}: usage logging set")
    meta = cast("dict[str, dict[str, str]]", doc["metadata"])
    c.check(
        ok=meta.get("annotations", {}).get("helm.sh/resource-policy") == "keep",
        message=f"{name}: missing helm.sh/resource-policy: keep",
    )
    c.check(
        ok=spec(doc).get("managementPolicies") == ORPHAN_POLICIES,
        message=f"{name}: not orphaned on delete",
    )
    labels = cast("dict[str, str]", fp.get("labels", {}))
    c.check(
        ok=labels.get("purpose") == "schedule-feed-ingest",
        message=f"{name}: missing purpose label",
    )


def check_buckets(c: Checker, docs: Sequence[Doc]) -> None:
    """Both buckets are private; the delivery bucket is versioned with lifecycle."""
    found = buckets(docs)
    c.check(
        ok=set(found) == {BUCKET, AUDIT_BUCKET},
        message=f"expected the delivery and audit-log buckets, got {sorted(found)}",
    )
    for name, doc in found.items():
        check_bucket_privacy(c, name, doc)
    delivery = found.get(BUCKET)
    if delivery is None:
        return
    fp = for_provider(delivery)
    c.check(
        ok=fp.get("versioning") == {"enabled": False},
        message="delivery bucket: versioning is not off",
    )
    soft = cast("dict[str, int]", fp.get("softDeletePolicy", {}))
    seconds = soft.get("retentionDurationSeconds")
    c.check(
        ok=seconds == SOFT_DELETE_SECONDS,
        message=f"delivery bucket: soft delete {seconds}s, expected 7 days",
    )
    c.check(
        ok=fp.get("lifecycleRule") == EXPECTED_LIFECYCLE,
        message=f"delivery bucket: lifecycle {fp.get('lifecycleRule')}",
    )
    c.check(ok="encryption" not in fp, message="delivery bucket: CMEK set by default")


def check_iam(c: Checker, docs: Sequence[Doc], *, pubsub: bool) -> None:
    """Every grant is a bucket-level (or topic/subscription) member, as expected."""
    for doc in docs:
        kind = str(doc.get("kind"))
        if re.search(r"IAM(Policy|Binding|Member|AuditConfig)$", kind):
            c.check(
                ok=kind in IAM_MEMBER_KINDS,
                message=f"{kind}: only bucket/topic/subscription members allowed",
            )
        if kind in IAM_MEMBER_KINDS:
            c.check(
                ok=spec(doc).get("managementPolicies") == ["*"],
                message=f"{kind}: not fully managed, so removal won't revoke it",
            )
    granted = bindings(docs)
    for _, role, member in granted:
        c.check(
            ok=bool(SERVICE_ACCOUNT.match(member)),
            message=f"member {member} ({role}) is not a service account",
        )
    expected = {(BUCKET, role, m) for m in PUBLISHERS for role in PUBLISHER_ROLES}
    expected |= {
        (BUCKET, "roles/storage.objectViewer", READER),
        (BUCKET, DELETE_ROLE, READER),
        (AUDIT_BUCKET, "roles/storage.objectViewer", READER),
        (AUDIT_BUCKET, "roles/storage.objectCreator", SINK),
    }
    c.check(
        ok=granted == expected,
        message=f"bucket bindings: unexpected {sorted(granted - expected)}, "
        f"missing {sorted(expected - granted)}",
    )
    roles = by_kind(docs, "ProjectIAMCustomRole")
    c.check(ok=len(roles) == 1, message=f"expected one custom role, got {len(roles)}")
    for doc in roles.values():
        meta = cast("dict[str, dict[str, str]]", doc["metadata"])
        c.check(
            ok=for_provider(doc).get("permissions") == ["storage.objects.delete"]
            and meta["annotations"]["crossplane.io/external-name"]
            == DELETE_ROLE.rsplit("/", 1)[1],
            message=f"reader delete role: {for_provider(doc)}",
        )
    topic = bindings(docs, "TopicIAMMember")
    subscription = bindings(docs, "SubscriptionIAMMember")
    if pubsub:
        c.check(
            ok=topic == {("ds-ingest-events", "roles/pubsub.publisher", STORAGE_AGENT)},
            message=f"topic: publishers {topic}",
        )
        c.check(
            ok=subscription
            == {
                ("ds-ingest-events-schedule-ingest", "roles/pubsub.subscriber", READER)
            },
            message=f"subscription: subscribers {subscription}",
        )
    else:
        c.check(
            ok=not topic and not subscription, message="Pub/Sub IAM without Pub/Sub"
        )


def check_pubsub(c: Checker, docs: Sequence[Doc]) -> None:
    """Check the notification sends only OBJECT_FINALIZE to the topic."""
    notes = by_kind(docs, "Notification")
    c.check(ok=len(notes) == 1, message=f"expected one Notification, got {len(notes)}")
    for doc in notes.values():
        fp = for_provider(doc)
        c.check(
            ok=fp.get("bucket") == BUCKET
            and fp.get("eventTypes") == ["OBJECT_FINALIZE"]
            and fp.get("topic") == "projects/example-project/topics/ds-ingest-events",
            message=f"notification: {fp}",
        )
    subs = by_kind(docs, "Subscription")
    c.check(ok=len(subs) == 1, message="expected one pull Subscription")
    for doc in subs.values():
        c.check(
            ok="pushConfig" not in for_provider(doc),
            message="subscription is push, expected pull",
        )


def check_alerts(c: Checker, docs: Sequence[Doc]) -> None:
    """Six alert policies, all on the delivery bucket, all notifying."""
    policies = by_kind(docs, "AlertPolicy")
    c.check(
        ok=len(policies) == USAGE_ALERTS,
        message=f"expected {USAGE_ALERTS} AlertPolicy, got {len(policies)}",
    )
    for name, doc in policies.items():
        fp = for_provider(doc)
        conditions = cast("list[dict[str, dict[str, str]]]", fp["conditions"])
        c.check(
            ok=all(
                f'resource.label.bucket_name="{BUCKET}"'
                in cond["conditionThreshold"]["filter"]
                for cond in conditions
            ),
            message=f"{name}: not scoped to the delivery bucket",
        )
        c.check(ok=bool(fp.get("notificationChannels")), message=f"{name}: no channel")


def check_failures(c: Checker) -> None:
    """Bad values must fail to render."""
    cases: list[tuple[str, list[str]]] = [
        ("no publisher members", ["--set", "publisher.members=null"]),
        ("allUsers publisher", ["--set", "publisher.members[0]=allUsers"]),
        (
            "allAuthenticatedUsers publisher",
            ["--set", "publisher.members[0]=allAuthenticatedUsers"],
        ),
        ("domain publisher", ["--set", "publisher.members[0]=domain:example.com"]),
        ("group publisher", ["--set", "publisher.members[0]=group:g@example.com"]),
        ("admin role", ["--set", "publisher.roles[0]=roles/storage.admin"]),
        ("objectAdmin role", ["--set", "publisher.roles[0]=roles/storage.objectAdmin"]),
        (
            "legacyBucketOwner role",
            ["--set", "publisher.roles[0]=roles/storage.legacyBucketOwner"],
        ),
        ("non-storage role", ["--set", "publisher.roles[0]=roles/owner"]),
        ("missing project", ["--set", "gcp.projectId="]),
        ("missing reader", ["--set", "reader.member="]),
        ("allUsers reader", ["--set", "reader.member=allUsers"]),
        ("dotted bucket name", ["--set", "bucket.name=a.b.c"]),
        ("soft delete under 7 days", ["--set", "bucket.softDeleteRetentionDays=3"]),
        ("soft delete over 90 days", ["--set", "bucket.softDeleteRetentionDays=91"]),
        ("alerts without channels", ["--set", "usageAlerts.enabled=true"]),
        (
            "pubsub without project number",
            ["--set", "notifications.pubsub.enabled=true"],
        ),
    ]
    for label, args in cases:
        code, _ = c.render(*args)
        c.check(ok=code != 0, message=f"{label}: rendered, expected a failure")


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; return the exit status."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--helm", default=shutil.which("helm") or "helm", help="helm binary"
    )
    args = parser.parse_args(argv)
    c = Checker(args.helm)

    code, out = c.render("--set", "enabled=false")
    c.check(
        ok=code == 0 and not [d for d in yaml.safe_load_all(out) if d],
        message="disabled chart rendered resources",
    )

    default = c.docs()
    check_buckets(c, default)
    check_iam(c, default, pubsub=False)
    c.check(
        ok=not by_kind(default, "AlertPolicy") and not by_kind(default, "Topic"),
        message="alerts or Pub/Sub rendered while off",
    )

    everything = c.docs(*PUBSUB_SET, *ALERTS_SET)
    check_buckets(c, everything)
    check_iam(c, everything, pubsub=True)
    check_pubsub(c, everything)
    check_alerts(c, everything)

    check_failures(c)

    for failure in c.failures:
        print(f"FAIL: {failure}")
    if not c.failures:
        print("ds-ingest-bucket chart: all checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
