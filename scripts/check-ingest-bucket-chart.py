#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Render charts/ds-ingest-bucket and check its AWS policies.

  scripts/check-ingest-bucket-chart.py [--helm HELM]

Renders the chart with ci/example-values.yaml in several modes and checks:

  - disabled (the default) renders nothing;
  - the publisher (writer) can only PutObject/AbortMultipartUpload under the
    delivery prefix, in both iamUser and crossAccount modes;
  - the reader can only list and get under the prefix (and the access-log
    prefix), with no delete unless reader.allowDelete, and never a write;
  - both buckets block public access, enforce bucket ownership and deny
    non-TLS and pre-1.2 TLS requests;
  - with notifications.sqs.enabled the queue accepts SendMessage only from
    s3.amazonaws.com for this bucket and account, and has a dead-letter
    queue; without it no Queue renders;
  - bad values (missing account, dotted bucket name, prefix without a
    trailing slash, crossAccount without principals) fail to render.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "ds-ingest-bucket"
EXAMPLE = CHART / "ci" / "example-values.yaml"
BUCKET_ARN = "arn:aws:s3:::example-ds-ingest"
LOG_BUCKET_ARN = "arn:aws:s3:::example-ds-ingest-logs"
QUEUE_ARN = "arn:aws:sqs:eu-west-2:123456789012:ds-ingest-events"
DLQ_ARN = "arn:aws:sqs:eu-west-2:123456789012:ds-ingest-events-dlq"
PUBLISHER_ROLE = "arn:aws:iam::111122223333:role/rdm-delivery"
WRITER_ACTIONS = {"s3:PutObject", "s3:AbortMultipartUpload"}
READER_S3_ACTIONS = {
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:GetObject",
    "s3:GetObjectVersion",
}
DELETE_ACTIONS = {"s3:DeleteObject", "s3:DeleteObjectVersion"}
# blockPublicACLs, blockPublicPolicy, ignorePublicACLs, restrictPublicBuckets.
PUBLIC_ACCESS_BLOCK_SWITCHES = 4

type Doc = dict[str, object]
type Statement = dict[str, object]


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


def by_kind(docs: Sequence[Doc], kind: str) -> dict[str, Doc]:
    """Resources of `kind`, by metadata.name."""
    out: dict[str, Doc] = {}
    for doc in docs:
        if doc.get("kind") == kind:
            name = cast("dict[str, str]", doc["metadata"])["name"]
            out[name] = doc
    return out


def statements(policy_json: object) -> list[Statement]:
    """Return the Statement list of a JSON policy string."""
    policy = cast("dict[str, object]", json.loads(cast("str", policy_json)))
    return cast("list[Statement]", policy["Statement"])


def as_list(value: object) -> list[str]:
    """Return a policy field that may be a string or a list, as a list."""
    if isinstance(value, str):
        return [value]
    return cast("list[str]", value)


def allowed(stmts: Sequence[Statement]) -> Iterator[tuple[str, str]]:
    """Every (action, resource) pair an Allow statement grants."""
    for stmt in stmts:
        if stmt.get("Effect") != "Allow":
            continue
        for action in as_list(stmt["Action"]):
            for resource in as_list(stmt["Resource"]):
                yield action, resource


def has_tls_denies(stmts: Sequence[Statement], arn: str) -> bool:
    """Whether the policy denies non-TLS and pre-1.2 TLS on `arn` and its objects."""
    sids = {
        cast("str", s.get("Sid"))
        for s in stmts
        if s.get("Effect") == "Deny"
        and set(as_list(s["Resource"])) == {arn, f"{arn}/*"}
    }
    return {"DenyInsecureTransport", "DenyTlsBelow12"} <= sids


def check_buckets(c: Checker, docs: Sequence[Doc]) -> None:
    """Both buckets are private, owner-enforced and TLS-only."""
    buckets = by_kind(docs, "Bucket")
    c.check(
        ok=set(buckets) == {"example-ds-ingest", "example-ds-ingest-logs"},
        message=f"expected the delivery and log buckets, got {sorted(buckets)}",
    )
    for name, arn in (
        ("example-ds-ingest", BUCKET_ARN),
        ("example-ds-ingest-logs", LOG_BUCKET_ARN),
    ):
        if name not in buckets:
            continue
        s = spec(buckets[name])
        block = cast("dict[str, bool]", s["publicAccessBlock"])
        c.check(
            ok=all(block.values()) and len(block) == PUBLIC_ACCESS_BLOCK_SWITCHES,
            message=f"{name}: public access not fully blocked",
        )
        rules = cast("dict[str, list[dict[str, str]]]", s["ownershipControls"])["rules"]
        c.check(
            ok=rules == [{"objectOwnership": "BucketOwnerEnforced"}],
            message=f"{name}: ownership is not BucketOwnerEnforced",
        )
        c.check(
            ok=has_tls_denies(statements(s["policy"]), arn),
            message=f"{name}: TLS denies missing",
        )
        meta = cast("dict[str, dict[str, str]]", buckets[name]["metadata"])
        c.check(
            ok=meta.get("annotations", {}).get("helm.sh/resource-policy") == "keep",
            message=f"{name}: missing helm.sh/resource-policy: keep",
        )
    if "example-ds-ingest" in buckets:
        s = spec(buckets["example-ds-ingest"])
        c.check(
            ok=cast("dict[str, str]", s["versioning"])["status"] == "Enabled",
            message="delivery bucket: versioning is not Enabled",
        )
        c.check(
            ok=cast("dict[str, dict[str, str]]", s["logging"])["loggingEnabled"][
                "targetBucket"
            ]
            == "example-ds-ingest-logs",
            message="delivery bucket: access logs do not go to the log bucket",
        )
        logs = buckets.get("example-ds-ingest-logs")
        if logs is not None:
            log_allows = list(allowed(statements(spec(logs)["policy"])))
            c.check(
                ok=log_allows == [("s3:PutObject", f"{LOG_BUCKET_ARN}/s3-access/*")],
                message=f"log bucket: unexpected grants {log_allows}",
            )


def user_statements(docs: Sequence[Doc], name: str) -> list[Statement]:
    """All inline-policy statements of the IAM user `name` (empty if absent)."""
    user = by_kind(docs, "User").get(name)
    if user is None:
        return []
    inline = cast("dict[str, str]", spec(user)["inlinePolicies"])
    return [stmt for policy in inline.values() for stmt in statements(policy)]


def check_reader(
    c: Checker, docs: Sequence[Doc], *, allow_delete: bool, sqs: bool
) -> None:
    """Check the reader: list/get only, delete only if allowed."""
    stmts = user_statements(docs, "ds-ingest-reader")
    c.check(ok=bool(stmts), message="reader user missing")
    expected_resources = {
        BUCKET_ARN,
        f"{BUCKET_ARN}/rdm/*",
        LOG_BUCKET_ARN,
        f"{LOG_BUCKET_ARN}/s3-access/*",
    }
    s3_actions: set[str] = set()
    for action, resource in allowed(stmts):
        if action.startswith("s3:"):
            s3_actions.add(action)
            c.check(
                ok=resource in expected_resources,
                message=f"reader: {action} on unexpected {resource}",
            )
        else:
            c.check(
                ok=sqs
                and resource in {QUEUE_ARN, DLQ_ARN}
                and action.startswith("sqs:"),
                message=f"reader: unexpected grant {action} on {resource}",
            )
            c.check(
                ok=not (
                    resource == DLQ_ARN
                    and action in {"sqs:ReceiveMessage", "sqs:DeleteMessage"}
                ),
                message="reader: may consume the dead-letter queue",
            )
    allowed_s3 = READER_S3_ACTIONS | (DELETE_ACTIONS if allow_delete else set())
    c.check(
        ok=s3_actions <= allowed_s3,
        message=f"reader: unexpected S3 actions {sorted(s3_actions - allowed_s3)}",
    )
    granted = bool(s3_actions & DELETE_ACTIONS)
    c.check(
        ok=allow_delete == granted,
        message=f"reader: delete granted={granted}, expected {allow_delete}",
    )
    for stmt in stmts:
        if "s3:ListBucket" in as_list(stmt["Action"]):
            c.check(
                ok="Condition" in stmt,
                message="reader: ListBucket without an s3:prefix condition",
            )


def check_writer_iam_user(c: Checker, docs: Sequence[Doc]) -> None:
    """In iamUser mode the writer can only put under the delivery prefix."""
    grants = list(allowed(user_statements(docs, "ds-ingest-rdm-writer")))
    c.check(ok=bool(grants), message="writer user missing in iamUser mode")
    for action, resource in grants:
        c.check(
            ok=action in WRITER_ACTIONS and resource == f"{BUCKET_ARN}/rdm/*",
            message=f"writer: unexpected grant {action} on {resource}",
        )


def check_writer_cross_account(c: Checker, docs: Sequence[Doc]) -> None:
    """Check crossAccount mode: no writer user, the bucket policy only puts."""
    c.check(
        ok="ds-ingest-rdm-writer" not in by_kind(docs, "User"),
        message="writer user rendered in crossAccount mode",
    )
    bucket = by_kind(docs, "Bucket").get("example-ds-ingest")
    if bucket is None:
        c.check(ok=False, message="delivery bucket missing in crossAccount mode")
        return
    grants = [
        s for s in statements(spec(bucket)["policy"]) if s.get("Effect") == "Allow"
    ]
    c.check(
        ok=len(grants) == 1,
        message=f"crossAccount: expected one Allow, got {len(grants)}",
    )
    for stmt in grants:
        principal = cast("dict[str, list[str]]", stmt["Principal"])
        c.check(
            ok=principal == {"AWS": [PUBLISHER_ROLE]},
            message=f"crossAccount: principal {principal}",
        )
        c.check(
            ok=set(as_list(stmt["Action"])) <= WRITER_ACTIONS
            and as_list(stmt["Resource"]) == [f"{BUCKET_ARN}/rdm/*"],
            message=f"crossAccount: unexpected grant {stmt}",
        )


def check_sqs(c: Checker, docs: Sequence[Doc]) -> None:
    """Check the queue takes S3 events for this bucket only, with a DLQ."""
    queues = by_kind(docs, "Queue")
    c.check(
        ok=set(queues) == {"ds-ingest-events", "ds-ingest-events-dlq"},
        message=f"expected the queue and its DLQ, got {sorted(queues)}",
    )
    main = queues.get("ds-ingest-events")
    if main is None:
        return
    s = spec(main)
    c.check(ok=s.get("sqsManagedSSEEnabled") == "true", message="queue: SSE-SQS off")
    redrive = cast("dict[str, object]", json.loads(cast("str", s["redrivePolicy"])))
    c.check(
        ok=redrive.get("deadLetterTargetArn") == DLQ_ARN,
        message="queue: does not redrive to the DLQ",
    )
    allows = [st for st in statements(s["policy"]) if st.get("Effect") == "Allow"]
    expected_condition = {
        "ArnLike": {"aws:SourceArn": BUCKET_ARN},
        "StringEquals": {"aws:SourceAccount": "123456789012"},
    }
    c.check(
        ok=len(allows) == 1
        and allows[0].get("Principal") == {"Service": "s3.amazonaws.com"}
        and allows[0].get("Action") == "sqs:SendMessage"
        and allows[0].get("Condition") == expected_condition,
        message=f"queue: policy is not S3-only for this bucket: {allows}",
    )
    bucket = by_kind(docs, "Bucket").get("example-ds-ingest")
    if bucket is not None:
        notification = cast(
            "dict[str, list[dict[str, object]]]", spec(bucket).get("notification", {})
        )
        targets = [
            q.get("queueARN") for q in notification.get("queueConfigurations", [])
        ]
        c.check(
            ok=targets == [QUEUE_ARN], message=f"bucket: notification targets {targets}"
        )


def check_failures(c: Checker) -> None:
    """Bad values must fail to render."""
    cases: list[tuple[str, list[str]]] = [
        ("missing account", ["--set", "aws.accountId="]),
        ("dotted bucket name", ["--set", "bucket.name=a.b.c"]),
        ("prefix without a trailing slash", ["--set", "bucket.deliveryPrefix=rdm"]),
        ("crossAccount without principals", ["--set", "writer.mode=crossAccount"]),
        ("missing permissions boundary", ["--set", "iam.permissionsBoundaryArn="]),
        ("kms without a key", ["--set", "bucket.encryption.sseAlgorithm=aws:kms"]),
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
    check_reader(c, default, allow_delete=False, sqs=False)
    check_writer_iam_user(c, default)
    c.check(
        ok=not by_kind(default, "Queue"),
        message="a Queue rendered with notifications.sqs.enabled=false",
    )

    check_reader(
        c, c.docs("--set", "reader.allowDelete=true"), allow_delete=True, sqs=False
    )

    cross = c.docs(
        "--set",
        "writer.mode=crossAccount",
        "--set",
        f"writer.principalArns[0]={PUBLISHER_ROLE}",
    )
    check_writer_cross_account(c, cross)

    sqs = c.docs("--set", "notifications.sqs.enabled=true")
    check_sqs(c, sqs)
    check_reader(c, sqs, allow_delete=False, sqs=True)

    check_failures(c)

    for failure in c.failures:
        print(f"FAIL: {failure}")
    if not c.failures:
        print("ds-ingest-bucket chart: all checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
