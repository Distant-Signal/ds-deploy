#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Render charts/distant-signal's schedulefeed and check its env and sources.

  scripts/check-schedulefeed-chart.py [--helm HELM] [--baseline DIR]

Renders the chart with schedulefeed on (CI's required-value flags plus
`scheduleFeed.enabled=true scheduleFeed.sftp.authMethod=password`) and
checks:

  - extraEnv override: `scheduleFeed.ingest.extraEnv` replaces the chart's
    entry of the same name in its place (RUST_LOG, still right after
    PROGRESS_STALL_SECS), once, and an extra-only name follows the chart's
    entries;
  - no container in any rendered document repeats an env name;
  - explicit equals implicit (CI's permanent byte-identity check): setting
    `scheduleFeed.sftp.enabled=true` or `scheduleFeed.bucket.auth=key` (the
    defaults) renders exactly what leaving it unset does, for each of the
    --baseline value sets below;
  - SFTPGo's own Deployment (`scheduleFeed.sftp.separateDeployment`, the
    default): `<release>-schedulefeed-sftp` runs exactly the `sftp`
    container and volumes the one-pod layout (`separateDeployment=false`)
    runs, with Recreate, one replica, the schedulefeed PVC and pod-template
    labels free of chart/app versions; it renders identically under another
    chart version, app version and ingest/reference image tag; the SFTP
    Service selects it; its NetworkPolicy admits SFTP and telemetry and
    allows DNS egress only, and the schedulefeed policy no longer admits
    them; the PodMonitor selects it. With `separateDeployment=false`: no
    such Deployment or policy, and `sftp` back in the schedulefeed pod;
  - schedulefeed with neither source (`scheduleFeed.sftp.enabled=false`,
    bucket off) refuses to render, naming both switches;
  - SFTP only (the default): no bucket or source-switch env on any
    container, no `bucket-credentials` volume, containers ingest and
    reference in the schedulefeed pod and sftp in its own;
  - both sources: the ingest env contract (source switches, precedence,
    bucket name, key path, expected keys, every BUCKET_* default as a plain
    integer; no BUCKET_BASE_URL or audit-log vars unless set), the reader
    key as an optional 0440 Secret volume mounted read-only on `ingest`
    only, and no bucket env on `sftp` or `reference`;
  - bucket only: containers ingest and reference, SFTP_SOURCE_ENABLED
    false, no SFTP Deployment, Service, entrypoint ConfigMap, host-key
    Secret, volumes or checksum annotation, the PVC kept, and no
    sftp.authMethod needed;
  - bucket only with NetworkPolicy egress and internetPorts [443]: no SFTP
    (2022) or SFTP telemetry (9097) ingress and no schedulefeed-sftp
    policy, and the internet rule allows
    443 (and a custom baseUrl's port);
  - audit-log shipping adds its three vars; extraEnv overrides a BUCKET_*
    var once;
  - keyless (`scheduleFeed.bucket.auth=workloadIdentity`): ingest gets
    GOOGLE_APPLICATION_CREDENTIALS (the ConfigMap's credential config) and
    no GOOGLE_SERVICE_ACCOUNT_PATH; the ConfigMap is an optional 0440
    volume and the projected token (audience, 3600 s, nothing else in the
    projection) a second one, both mounted read-only on `ingest` only; no
    Secret volume; the pod runs as the dedicated, non-automounting
    `<release>-schedulefeed` ServiceAccount; the NetworkPolicy's internet
    rule allows 443; key mode never renders that ServiceAccount unasked;
    bad keyless values (no ConfigMap, a key-mode value, the shared
    ServiceAccount, an unknown auth) refuse to render;
  - every env var of the chart -> schedule-ingest contract renders, and no
    mode renders a private key (outside the generated SFTP host keys) or a
    Secret named like the reader key's existingSecret;
  - bad bucket values refuse to render, naming the key;
  - alerts: no distant-signal.schedule-bucket group with the bucket off or
    metrics.prometheusRule.scheduleBucket.enabled=false; six alerts with
    both sources; five (no SourcesDisagree) and no schedule-sftp group with
    the bucket only;
  - schedule-reference's sink (`scheduleFeed.reference.ingest.sink`,
    ingest architecture plan 2a): `http` (the default) gives `reference` no
    INGEST_SINK or database env and the pod no Postgres egress or postgres
    ingress; `db` gives `reference` (only) INGEST_SINK=db, DATABASE_URL
    (after PGPASSWORD), DATABASE_MAX_CONNECTIONS and the api's
    CORPUS_FALLBACK_ENABLED, the schedulefeed policy Postgres egress and the
    postgres policy `schedulefeed` ingress (and nothing for
    schedulefeed-sftp); an unknown sink refuses to render.

--baseline DIR renders DIR (a copy of charts/distant-signal from another
commit, e.g. the merge base) and this chart with the same flags, for the
defaults; schedulefeed on; schedulefeed on with the NetworkPolicy and its
egress rules; both sources (key auth); bucket only with the NetworkPolicy;
and values-example.yaml. --baseline-set KEY=VALUE (repeatable) adds
`--set KEY=VALUE` to this chart's renders only, e.g.
`scheduleFeed.sftp.separateDeployment=false` against a commit from before
the SFTP split. Every document must be identical after normalisation (the
`data`/`stringData` of every Secret are dropped: genPrivateKey and
randAlphaNum differ between runs). A difference fails
with a unified diff per document. A one-off check for refactors; CI
doesn't run it (on main it would compare main with itself).

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
import difflib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
EXAMPLE = "values-example.yaml"
# CI's required-value flags (the kafka/LLM/SSO settings only satisfy the
# chart's required-value checks).
# Release A (2026-10-09) put every ingest producer on its db/stream sink by
# default; these checks predate it and test other switches, so they render
# on ci/http-sinks.yaml (every producer back on http, the writer on app).
BASE = (
    "-f",
    str(CHART / "ci" / "http-sinks.yaml"),
    "--set",
    "trustConsumer.kafka.brokers=k:9094",
    "--set",
    "trustConsumer.kafka.topic=t",
    "--set",
    "trustConsumer.kafka.saslMechanism=PLAIN",
    "--set",
    "enricher.llm.baseUrl=http://l/v1",
    "--set",
    "enricher.llm.model=m",
    "--set",
    "api.sso.issuerUrl=https://sso.example.com",
    "--set",
    "api.sso.clientId=c",
    "--set",
    "api.sso.clientSecret=s",
    "--set",
    "api.sso.redirectUrl=https://app.example.com/cb",
    "--set",
    "api.sso.postLoginRedirectUrl=https://app.example.com/",
)
ON = (
    "--set",
    "scheduleFeed.enabled=true",
    "--set",
    "scheduleFeed.sftp.authMethod=password",
)
NETPOL = (
    "--set",
    "networkPolicy.enabled=true",
    "--set",
    "networkPolicy.egress.enabled=true",
)
BUCKET = (
    "--set",
    "scheduleFeed.bucket.enabled=true",
    "--set",
    "scheduleFeed.bucket.name=example-ds-ingest",
    "--set",
    "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
)
# schedule-reference's db sink as its narrow role (security review H2).
REFERENCE_ROLE = (
    "--set",
    "postgresql.roles.enabled=true",
    "--set",
    "postgresql.roles.setupJob.enabled=true",
    "--set",
    "postgresql.roles.perService.enabled=true",
    "--set",
    "postgresql.roles.perService.schedule_reference.connect=true",
)
SFTP_OFF = ("--set", "scheduleFeed.sftp.enabled=false")
ONE_POD = ("--set", "scheduleFeed.sftp.separateDeployment=false")
METRICS = (
    "--set",
    "metrics.enabled=true",
    "--set",
    "metrics.podMonitor.enabled=true",
)
SFTP_DEPLOYMENT = "distant-signal-schedulefeed-sftp"
SFTP_COMPONENT = "schedulefeed-sftp"
AUDIT = (
    "--set",
    "scheduleFeed.bucket.auditLogs.ship=true",
    "--set",
    "scheduleFeed.bucket.auditLogs.bucket=example-ds-ingest-audit",
)
KEY_DIR = "/var/run/secrets/distant-signal/gcs"
# Keyless (scheduleFeed.bucket.auth=workloadIdentity).
WIF = (
    "--set",
    "scheduleFeed.bucket.enabled=true",
    "--set",
    "scheduleFeed.bucket.name=example-ds-ingest",
    "--set",
    "scheduleFeed.bucket.auth=workloadIdentity",
    "--set",
    "scheduleFeed.bucket.workloadIdentity.credentialConfigMap=ds-ingest-gcp-wif",
    "--set",
    "scheduleFeed.serviceAccount.create=true",
)
WIF_CONFIG = f"{KEY_DIR}/credential-config.json"
WIF_TOKEN_DIR = "/var/run/secrets/distant-signal/gcs-token"  # noqa: S105  # a mount path, not a secret
WIF_SERVICE_ACCOUNT = "distant-signal-schedulefeed"
SOURCE_SWITCHES = {
    "SFTP_SOURCE_ENABLED",
    "BUCKET_SOURCE_ENABLED",
    "SOURCE_PRECEDENCE",
    "DISAGREEMENT_WINDOW_MINUTES",
}
# The ingest env with both sources on and every bucket value at its default.
BOTH_ENV = {
    "SFTP_SOURCE_ENABLED": "true",
    "BUCKET_SOURCE_ENABLED": "true",
    "SOURCE_PRECEDENCE": "bucket,sftp",
    "DISAGREEMENT_WINDOW_MINUTES": "120",
    "BUCKET_NAME": "example-ds-ingest",
    "GOOGLE_SERVICE_ACCOUNT_PATH": f"{KEY_DIR}/service-account.json",
    "BUCKET_EXPECTED_KEYS": "timetable_full.zip,CORPUSExtract.json.gz",
    "BUCKET_POLL_INTERVAL_SECS": "300",
    "BUCKET_DELETE_MIN_AGE_SECS": "3600",
    "BUCKET_ARCHIVE_KEEP": "5",
    "BUCKET_MAX_OBJECT_BYTES": "268435456",
    "BUCKET_MAX_DOWNLOADS_PER_POLL": "2",
    "BUCKET_MAX_DOWNLOAD_BYTES_PER_HOUR": "268435456",
    "BUCKET_MAX_DOWNLOAD_BYTES_PER_DAY": "1073741824",
    "BUCKET_MAX_BACKOFF_SECS": "3600",
}
AUDIT_ENV = {
    "BUCKET_AUDIT_LOGS_SHIP": "true",
    "BUCKET_AUDIT_LOGS_BUCKET": "example-ds-ingest-audit",
    "BUCKET_AUDIT_LOGS_POLL_INTERVAL_SECS": "600",
}
# Every env var of the chart -> schedule-ingest contract (the chart plan's
# "interface contract" table).
CONTRACT = {*BOTH_ENV, *AUDIT_ENV, "BUCKET_BASE_URL"}
SFTP_PORT = 2022
SFTP_TELEMETRY_PORT = 9097
HTTPS_PORT = 443
FAKE_GCS = "http://fake-gcs:4443"
FAKE_GCS_PORT = 4443

RULES = ("--set", "metrics.prometheusRule.enabled=true")
BUCKET_GROUP = "distant-signal.schedule-bucket"
SFTP_GROUP = "distant-signal.schedule-sftp"
BUCKET_ALERTS = {
    "DistantSignalScheduleBucketAccessRevoked",
    "DistantSignalScheduleBucketNoNewObject",
    "DistantSignalScheduleBucketReadErrors",
    "DistantSignalScheduleBucketUnexpectedObject",
    "DistantSignalScheduleBucketDownloadBudget",
}
DISAGREE = "DistantSignalScheduleFeedSourcesDisagree"

# The value sets --baseline compares; every one is rendered with BASE.
BASELINE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ()),
    ("schedulefeed on", ON),
    ("schedulefeed on, network policy", (*ON, *NETPOL)),
    ("both sources (key auth)", (*ON, *BUCKET)),
    (
        "bucket only, network policy",
        (
            *ON,
            *BUCKET,
            *SFTP_OFF,
            *NETPOL,
            "--set",
            "networkPolicy.egress.internetPorts={443}",
        ),
    ),
    ("values-example.yaml", ("-f", EXAMPLE)),
)

type Doc = dict[str, object]
type Container = dict[str, object]
type EnvEntry = dict[str, object]


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

    def render(self, *args: str, chart: pathlib.Path = CHART) -> tuple[int, str]:
        """Run `helm template` on `chart` with BASE and `args`."""
        args = tuple(str(chart / a) if a == EXAMPLE else a for a in args)
        result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "distant-signal", str(chart), *BASE, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def docs(self, *args: str, chart: pathlib.Path = CHART) -> list[Doc]:
        """Render and parse every non-empty YAML document."""
        code, out = self.render(*args, chart=chart)
        if code != 0:
            self.failures.append(f"render {list(args)} failed: {out.strip()}")
            return []
        return [cast("Doc", d) for d in yaml.safe_load_all(out) if d]


def name_of(doc: Doc) -> str:
    """Return `Kind/name` of a rendered resource."""
    return f"{doc.get('kind')}/{cast('dict[str, str]', doc['metadata'])['name']}"


def containers_of(doc: Doc) -> list[Container]:
    """Return a workload resource's containers (none for anything else)."""
    spec = cast("dict[str, object]", doc.get("spec") or {})
    template = cast("dict[str, object]", spec.get("template") or {})
    pod = cast("dict[str, object]", template.get("spec") or {})
    return cast("list[Container]", pod.get("containers") or [])


def schedulefeed(docs: Sequence[Doc]) -> Doc | None:
    """Return the schedulefeed Deployment, if rendered."""
    for doc in docs:
        if doc.get("kind") == "Deployment" and name_of(doc).endswith("-schedulefeed"):
            return doc
    return None


def sftp_deployment(docs: Sequence[Doc]) -> Doc | None:
    """Return SFTPGo's own Deployment, if rendered."""
    for doc in docs:
        if name_of(doc) == f"Deployment/{SFTP_DEPLOYMENT}":
            return doc
    return None


def container(docs: Sequence[Doc], name: str) -> Container:
    """Return container `name` of the schedulefeed or SFTP Deployment, or {}."""
    found = [
        c
        for deployment in (schedulefeed(docs), sftp_deployment(docs))
        for c in containers_of(deployment or {})
        if c.get("name") == name
    ]
    return found[0] if found else {}


def env_list(c: Container) -> list[EnvEntry]:
    """Return a container's env, in order."""
    return cast("list[EnvEntry]", c.get("env") or [])


def env(c: Container) -> dict[str, EnvEntry]:
    """Return a container's env, by name."""
    return {str(e["name"]): e for e in env_list(c)}


def normalise(docs: Sequence[Doc]) -> dict[str, str]:
    """Each document as YAML, by `Kind/name`, minus Secret data."""
    out: dict[str, str] = {}
    for doc in docs:
        kept = doc
        if doc.get("kind") == "Secret":
            kept = {k: v for k, v in doc.items() if k not in {"data", "stringData"}}
        out[name_of(doc)] = yaml.safe_dump(kept, sort_keys=True)
    return out


def compare(c: Checker, label: str, old: Sequence[Doc], new: Sequence[Doc]) -> None:
    """Fail with a unified diff for every document that differs."""
    before, after = normalise(old), normalise(new)
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key, ""), after.get(key, "")
        if a != b:
            diff = difflib.unified_diff(
                a.splitlines(keepends=True),
                b.splitlines(keepends=True),
                f"{label}: {key} (before)",
                f"{label}: {key} (after)",
            )
            c.failures.append(f"{label}: {key} differs\n{''.join(diff)}")


def check_baseline(c: Checker, base: pathlib.Path, extra: Sequence[str] = ()) -> None:
    """Every BASELINE_SETS render of `base` equals this chart's (plus `extra`)."""
    for label, args in BASELINE_SETS:
        compare(c, label, c.docs(*args, chart=base), c.docs(*args, *extra))


def check_explicit_defaults(c: Checker) -> None:
    """`sftp.enabled=true` and `bucket.auth=key` render exactly the default."""
    for name, explicit in (
        ("sftp", ("--set", "scheduleFeed.sftp.enabled=true")),
        ("key auth", ("--set", "scheduleFeed.bucket.auth=key")),
        ("separate sftp", ("--set", "scheduleFeed.sftp.separateDeployment=true")),
    ):
        for label, args in BASELINE_SETS:
            if name == "sftp" and SFTP_OFF[1] in args:
                continue  # the set itself turns SFTP off
            compare(
                c, f"explicit {name}, {label}", c.docs(*args), c.docs(*args, *explicit)
            )


def check_neither_source(c: Checker) -> None:
    """Schedulefeed on with no source enabled fails, naming both switches."""
    code, out = c.render(*ON, "--set", "scheduleFeed.sftp.enabled=false")
    c.check(
        ok=code != 0
        and "scheduleFeed.sftp.enabled" in out
        and "scheduleFeed.bucket.enabled" in out,
        message=f"neither source: rendered, or not naming both: {out[-300:]}",
    )


def is_bucket_env(name: str) -> bool:
    """Whether `name` belongs to the bucket source or the source switches."""
    return name.startswith(("BUCKET_", "GOOGLE_")) or name in SOURCE_SWITCHES


def container_names(docs: Sequence[Doc]) -> list[str]:
    """Return the schedulefeed Deployment's container names, in order."""
    return [str(ctr.get("name")) for ctr in containers_of(schedulefeed(docs) or {})]


def sftp_container_names(docs: Sequence[Doc]) -> list[str]:
    """Return the SFTP Deployment's container names (none if not rendered)."""
    return [str(ctr.get("name")) for ctr in containers_of(sftp_deployment(docs) or {})]


def template_of(deployment: Doc | None) -> dict[str, dict[str, object]]:
    """Return a Deployment's pod template (empty if None)."""
    spec = cast("dict[str, object]", (deployment or {}).get("spec") or {})
    return cast("dict[str, dict[str, object]]", spec.get("template") or {})


def deployment_pod_spec(deployment: Doc | None) -> dict[str, object]:
    """Return a Deployment's pod spec (empty if None)."""
    return template_of(deployment).get("spec") or {}


def pod_spec(docs: Sequence[Doc]) -> dict[str, object]:
    """Return the schedulefeed Deployment's pod spec (empty if not rendered)."""
    return deployment_pod_spec(schedulefeed(docs))


def pod_volumes(pod: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return a pod spec's volumes, by name."""
    vols = cast("list[dict[str, object]]", pod.get("volumes") or [])
    return {str(v["name"]): v for v in vols}


def volumes(docs: Sequence[Doc]) -> dict[str, dict[str, object]]:
    """Return the schedulefeed pod's volumes, by name."""
    return pod_volumes(pod_spec(docs))


def mounts(c: Container) -> dict[str, dict[str, object]]:
    """Return a container's volumeMounts, by volume name."""
    found = cast("list[dict[str, object]]", c.get("volumeMounts") or [])
    return {str(m["name"]): m for m in found}


def check_sftp_only(c: Checker) -> None:
    """Check the default (SFTP only) renders nothing bucket-related."""
    docs = c.docs(*ON)
    for doc in docs:
        for ctr in containers_of(doc):
            leaked = sorted(n for n in env(ctr) if is_bucket_env(n))
            c.check(
                ok=not leaked,
                message=f"sftp only: {name_of(doc)} {ctr.get('name')} has {leaked}",
            )
    c.check(
        ok="bucket-credentials" not in volumes(docs),
        message="sftp only: a bucket-credentials volume",
    )
    c.check(
        ok=container_names(docs) == ["ingest", "reference"]
        and sftp_container_names(docs) == ["sftp"],
        message=f"sftp only: containers {container_names(docs)}, "
        f"sftp pod {sftp_container_names(docs)}",
    )


def labels_of(meta: object) -> dict[str, str]:
    """Return an object's or pod template's metadata.labels."""
    found = cast("dict[str, dict[str, str]]", meta or {}).get("labels")
    return found or {}


def podmonitor_components(docs: Sequence[Doc]) -> set[str]:
    """Return the components the PodMonitor selects (none if not rendered)."""
    for doc in docs:
        if doc.get("kind") != "PodMonitor":
            continue
        spec = cast("dict[str, dict[str, object]]", doc["spec"])
        exprs = cast(
            "list[dict[str, object]]", spec["selector"].get("matchExpressions") or []
        )
        for expr in exprs:
            if expr.get("key") == "app.kubernetes.io/component":
                return set(cast("list[str]", expr.get("values") or []))
    return set()


def sftp_service_selector(docs: Sequence[Doc]) -> dict[str, str]:
    """Return the SFTP Service's selector (empty if not rendered)."""
    for doc in docs:
        if name_of(doc) == "Service/distant-signal-schedulefeed":
            spec = cast("dict[str, dict[str, str]]", doc["spec"])
            return spec.get("selector") or {}
    return {}


def check_split_policies(c: Checker, docs: Sequence[Doc]) -> None:
    """SFTP and telemetry ingress on the SFTP pod only; its egress is DNS."""
    policy = sftp_policy(docs)
    spec = cast("dict[str, object]", policy.get("spec") or {})
    selector = cast("dict[str, dict[str, str]]", spec.get("podSelector") or {})
    c.check(
        ok=selector.get("matchLabels", {}).get("app.kubernetes.io/component")
        == SFTP_COMPONENT,
        message=f"split: schedulefeed-sftp NetworkPolicy selector {selector}",
    )
    c.check(
        ok=ingress_ports(policy) == {SFTP_PORT, SFTP_TELEMETRY_PORT},
        message=f"split: schedulefeed-sftp ingress {ingress_ports(policy)}",
    )
    egress = cast("list[dict[str, object]]", spec.get("egress") or [])
    egress_ports = {
        port.get("port")
        for rule in egress
        for port in cast("list[dict[str, object]]", rule.get("ports") or [])
    }
    c.check(
        ok=len(egress) == 1 and not egress[0].get("to") and egress_ports == {53},
        message=f"split: schedulefeed-sftp egress is not DNS only: {egress}",
    )
    left = ingress_ports(schedulefeed_policy(docs)) & {SFTP_PORT, SFTP_TELEMETRY_PORT}
    c.check(ok=not left, message=f"split: schedulefeed still admits {left}")


def check_sftp_split(c: Checker) -> None:
    """SFTPGo's own Deployment runs the one-pod layout's `sftp`, intact."""
    docs = c.docs(*ON, *NETPOL, *METRICS)
    one_pod = c.docs(*ON, *NETPOL, *METRICS, *ONE_POD)
    deployment = sftp_deployment(docs)
    c.check(ok=deployment is not None, message="split: no SFTP Deployment")
    if deployment is None:
        return
    spec = cast("dict[str, object]", deployment["spec"])
    c.check(
        ok=spec.get("replicas") == 1 and spec.get("strategy") == {"type": "Recreate"},
        message=f"split: replicas {spec.get('replicas')}, {spec.get('strategy')}",
    )
    selector = cast("dict[str, dict[str, str]]", spec["selector"])["matchLabels"]
    labels = labels_of(template_of(deployment).get("metadata"))
    c.check(
        ok=selector.get("app.kubernetes.io/component") == SFTP_COMPONENT
        # distant-signal.podLabels: the selector plus the two static labels.
        and labels
        == {
            **selector,
            "app.kubernetes.io/managed-by": "Helm",
            "app.kubernetes.io/part-of": "distant-signal",
        },
        message=f"split: selector {selector}, pod labels {labels}",
    )
    legacy = [
        x for x in containers_of(schedulefeed(one_pod) or {}) if x["name"] == "sftp"
    ]
    c.check(
        ok=bool(legacy) and containers_of(deployment) == legacy,
        message="split: the sftp container differs from the one-pod layout's",
    )
    pod = deployment_pod_spec(deployment)
    sftp_vols = ("data", "host-key", "sftp-entrypoint", "sftp-bootstrap")
    want = {n: v for n, v in volumes(one_pod).items() if n in sftp_vols}
    c.check(
        ok=len(want) == len(sftp_vols) and pod_volumes(pod) == want,
        message=f"split: volumes {sorted(pod_volumes(pod))}, want the one-pod "
        "layout's data (the same PVC), host-key, sftp-entrypoint, sftp-bootstrap",
    )
    c.check(
        ok=pod.get("securityContext") == pod_spec(docs).get("securityContext")
        and pod.get("automountServiceAccountToken") is False,
        message="split: pod securityContext differs from schedulefeed's, "
        "or the API token is mounted",
    )
    leftover = set(volumes(docs)) & set(sftp_vols[1:])
    sf_meta = cast(
        "dict[str, dict[str, str]]", template_of(schedulefeed(docs)).get("metadata")
    )
    annotations = sf_meta.get("annotations") or {}
    c.check(
        ok=not leftover and "checksum/sftp-entrypoint" not in annotations,
        message=f"split: schedulefeed keeps sftp volumes {leftover} or checksum",
    )
    service = sftp_service_selector(docs)
    c.check(
        ok=service.get("app.kubernetes.io/component") == SFTP_COMPONENT
        and service.items() <= labels.items(),
        message=f"split: SFTP Service selector {service}",
    )
    check_split_policies(c, docs)
    c.check(
        ok=SFTP_COMPONENT in podmonitor_components(docs),
        message="split: the PodMonitor does not select schedulefeed-sftp",
    )


def check_sftp_one_pod(c: Checker) -> None:
    """separateDeployment=false: `sftp` back in the schedulefeed pod."""
    docs = c.docs(*ON, *NETPOL, *METRICS, *ONE_POD)
    c.check(
        ok=container_names(docs) == ["sftp", "ingest", "reference"],
        message=f"one pod: containers {container_names(docs)}",
    )
    c.check(
        ok=sftp_deployment(docs) is None and not sftp_policy(docs),
        message="one pod: the SFTP Deployment or its NetworkPolicy rendered",
    )
    ports = ingress_ports(schedulefeed_policy(docs))
    c.check(
        ok={SFTP_PORT, SFTP_TELEMETRY_PORT} <= ports,
        message=f"one pod: schedulefeed ingress {ports}",
    )
    service = sftp_service_selector(docs)
    c.check(
        ok=service.get("app.kubernetes.io/component") == "schedulefeed",
        message=f"one pod: SFTP Service selector {service}",
    )
    c.check(
        ok=SFTP_COMPONENT not in podmonitor_components(docs),
        message="one pod: the PodMonitor selects schedulefeed-sftp",
    )


def check_sftp_stable_across_releases(c: Checker) -> None:
    """Check an app release (chart/app version, image tags) leaves SFTPGo alone."""
    release = (
        "--set",
        "scheduleFeed.ingest.image.tag=9.9.9",
        "--set",
        "scheduleFeed.reference.image.tag=9.9.9",
    )
    with tempfile.TemporaryDirectory() as tmp:
        chart = pathlib.Path(tmp) / "distant-signal"
        shutil.copytree(CHART, chart)
        meta = chart / "Chart.yaml"
        text = re.sub(r"(?m)^version: .*$", "version: 9.9.9", meta.read_text())
        text = re.sub(r"(?m)^appVersion: .*$", 'appVersion: "9.9.9"', text)
        meta.write_text(text)
        before = c.docs(*ON, *NETPOL, *METRICS)
        after = c.docs(*ON, *NETPOL, *METRICS, *release, chart=chart)
    changed = (schedulefeed(before) or {}).get("spec") != (
        schedulefeed(after) or {}
    ).get("spec")
    c.check(ok=changed, message="release: the schedulefeed pod did not change")
    old, new = sftp_deployment(before), sftp_deployment(after)
    c.check(
        ok=old is not None and (old or {}).get("spec") == (new or {}).get("spec"),
        message="release: the SFTP Deployment's spec changed with the release",
    )


def check_both(c: Checker) -> None:
    """Both sources: the ingest env contract and the reader key mount."""
    docs = c.docs(*ON, *BUCKET)
    c.check(
        ok=container_names(docs) == ["ingest", "reference"]
        and sftp_container_names(docs) == ["sftp"],
        message=f"both: containers {container_names(docs)}, "
        f"sftp pod {sftp_container_names(docs)}",
    )
    ingest = env(container(docs, "ingest"))
    for name, value in BOTH_ENV.items():
        got = ingest.get(name, {}).get("value")
        c.check(ok=got == value, message=f"both: ingest {name}={got!r}, want {value!r}")
    extra = sorted(n for n in ingest if n == "BUCKET_BASE_URL" or n in AUDIT_ENV)
    c.check(ok=not extra, message=f"both: unrequested ingest env {extra}")
    vol = volumes(docs).get("bucket-credentials", {})
    secret = cast("dict[str, object]", vol.get("secret") or {})
    c.check(
        ok=secret
        == {
            "secretName": "distant-signal-schedulefeed-bucket",
            "optional": True,
            "defaultMode": 0o440,
            "items": [{"key": "service-account.json", "path": "service-account.json"}],
        },
        message=f"both: bucket-credentials volume {vol}",
    )
    mount = mounts(container(docs, "ingest")).get("bucket-credentials", {})
    c.check(
        ok=mount.get("mountPath") == KEY_DIR and mount.get("readOnly") is True,
        message=f"both: ingest's bucket-credentials mount {mount}",
    )
    for other in ("sftp", "reference"):
        ctr = container(docs, other)
        c.check(
            ok="bucket-credentials" not in mounts(ctr),
            message=f"both: {other} mounts the reader key",
        )
        leaked = sorted(n for n in env(ctr) if n.startswith(("BUCKET_", "GOOGLE_")))
        c.check(ok=not leaked, message=f"both: {other} has {leaked}")


def check_bucket_only(c: Checker) -> None:
    """Bucket only: no SFTP receiver, the PVC kept."""
    docs = c.docs(*ON, *BUCKET, *SFTP_OFF)
    c.check(
        ok=container_names(docs) == ["ingest", "reference"],
        message=f"bucket only: containers {container_names(docs)}",
    )
    got = env(container(docs, "ingest")).get("SFTP_SOURCE_ENABLED", {}).get("value")
    c.check(ok=got == "false", message=f"bucket only: SFTP_SOURCE_ENABLED={got!r}")
    for doc in docs:
        name = name_of(doc)
        meta = cast("dict[str, dict[str, str]]", doc["metadata"])
        component = meta.get("labels", {}).get("app.kubernetes.io/component")
        c.check(
            ok=not (
                (
                    doc.get("kind") in {"Service", "Secret"}
                    and component == "schedulefeed"
                )
                or component == SFTP_COMPONENT
                or name.endswith("-sftp-entrypoint")
            ),
            message=f"bucket only: {name} rendered",
        )
    sftp_volumes = {"host-key", "sftp-entrypoint", "sftp-bootstrap"} & set(
        volumes(docs)
    )
    c.check(ok=not sftp_volumes, message=f"bucket only: volumes {sftp_volumes}")
    template = cast("dict[str, dict[str, object]]", (schedulefeed(docs) or {})["spec"])
    meta = cast("dict[str, dict[str, str]]", template["template"]["metadata"])
    c.check(
        ok="checksum/sftp-entrypoint" not in (meta.get("annotations") or {}),
        message="bucket only: checksum/sftp-entrypoint annotation",
    )
    c.check(
        ok=any(d.get("kind") == "PersistentVolumeClaim" for d in docs),
        message="bucket only: no PVC",
    )
    no_auth = ("--set", "scheduleFeed.enabled=true", *BUCKET, *SFTP_OFF)
    code, out = c.render(*no_auth)
    c.check(ok=code == 0, message=f"bucket only without authMethod: {out[-300:]}")


def internet_ports(policy: Doc) -> list[object]:
    """Return the ports of a NetworkPolicy's 0.0.0.0/0 egress rule."""
    spec = cast("dict[str, list[dict[str, object]]]", policy["spec"])
    for rule in spec.get("egress") or []:
        peers = cast("list[dict[str, dict[str, str]]]", rule.get("to") or [])
        if any(p.get("ipBlock", {}).get("cidr") == "0.0.0.0/0" for p in peers):
            ports = cast("list[dict[str, object]]", rule.get("ports") or [])
            return [port.get("port") for port in ports]
    return []


def schedulefeed_policy(docs: Sequence[Doc]) -> Doc:
    """Return the schedulefeed NetworkPolicy (empty if not rendered)."""
    for doc in docs:
        if doc.get("kind") == "NetworkPolicy" and name_of(doc).endswith(
            "-schedulefeed"
        ):
            return doc
    return {}


def sftp_policy(docs: Sequence[Doc]) -> Doc:
    """Return the schedulefeed-sftp NetworkPolicy (empty if not rendered)."""
    for doc in docs:
        if name_of(doc) == f"NetworkPolicy/{SFTP_DEPLOYMENT}":
            return doc
    return {}


def ingress_ports(policy: Doc) -> set[object]:
    """Return every port a NetworkPolicy's ingress rules name."""
    spec = cast("dict[str, list[dict[str, object]]]", policy.get("spec") or {})
    return {
        port.get("port")
        for rule in spec.get("ingress") or []
        for port in cast("list[dict[str, object]]", rule.get("ports") or [])
    }


def check_network_policy(c: Checker) -> None:
    """Bucket only: no SFTP ingress; the internet rule allows the GCS port."""
    netpol = (*NETPOL, "--set", "networkPolicy.egress.internetPorts={443}")
    docs = c.docs(*ON, *BUCKET, *SFTP_OFF, *netpol)
    policy = schedulefeed_policy(docs)
    c.check(ok=bool(policy), message="bucket only: no schedulefeed NetworkPolicy")
    if not policy:
        return
    spec = cast("dict[str, list[dict[str, object]]]", policy["spec"])
    ingress_ports = {
        port.get("port")
        for rule in spec.get("ingress") or []
        for port in cast("list[dict[str, object]]", rule.get("ports") or [])
    }
    c.check(
        ok=not ingress_ports & {SFTP_PORT, SFTP_TELEMETRY_PORT},
        message=f"bucket only: SFTP ingress ports {sorted(map(str, ingress_ports))}",
    )
    c.check(
        ok=not sftp_policy(docs),
        message="bucket only: a schedulefeed-sftp NetworkPolicy",
    )
    ports = internet_ports(policy)
    c.check(ok=HTTPS_PORT in ports, message=f"bucket only: internet ports {ports}")
    fake = ("--set", f"scheduleFeed.bucket.baseUrl={FAKE_GCS}")
    ports = internet_ports(
        schedulefeed_policy(c.docs(*ON, *BUCKET, *SFTP_OFF, *netpol, *fake))
    )
    c.check(
        ok=FAKE_GCS_PORT in ports, message=f"custom baseUrl: internet ports {ports}"
    )


def postgres_clients(docs: Sequence[Doc]) -> set[str]:
    """Return the components the postgres NetworkPolicy admits."""
    for doc in docs:
        if doc.get("kind") == "NetworkPolicy" and name_of(doc).endswith("-postgres"):
            spec = cast("dict[str, list[dict[str, object]]]", doc["spec"])
            found: set[str] = set()
            for rule in spec.get("ingress") or []:
                for peer in cast("list[dict[str, object]]", rule.get("from") or []):
                    selector = cast("dict[str, object]", peer.get("podSelector") or {})
                    for expr in cast(
                        "list[dict[str, object]]",
                        selector.get("matchExpressions") or [],
                    ):
                        if expr.get("key") == "app.kubernetes.io/component":
                            found |= set(cast("list[str]", expr.get("values") or []))
            return found
    return set()


def postgres_egress(policy: Doc) -> bool:
    """Whether a NetworkPolicy has an egress rule to the postgres pods."""
    spec = cast("dict[str, list[dict[str, object]]]", policy.get("spec") or {})
    for rule in spec.get("egress") or []:
        for peer in cast("list[dict[str, object]]", rule.get("to") or []):
            selector = cast("dict[str, dict[str, str]]", peer.get("podSelector") or {})
            labels = selector.get("matchLabels") or {}
            if labels.get("app.kubernetes.io/component") == "postgres":
                return True
    return False


DB_ENV = {"INGEST_SINK", "PGPASSWORD", "DATABASE_URL", "DATABASE_MAX_CONNECTIONS"}


def check_reference_sink(c: Checker) -> None:
    """schedule-reference's sink: http renders nothing new; db wires Postgres."""
    docs = c.docs(*ON, *NETPOL)
    reference = env(container(docs, "reference"))
    c.check(
        ok=not (DB_ENV | {"CORPUS_FALLBACK_ENABLED"}) & set(reference),
        message=f"sink http: reference has {sorted(DB_ENV & set(reference))}",
    )
    c.check(
        ok=not postgres_egress(schedulefeed_policy(docs)),
        message="sink http: schedulefeed may reach postgres",
    )
    c.check(
        ok="schedulefeed" not in postgres_clients(docs),
        message="sink http: postgres admits schedulefeed",
    )

    db: tuple[str, ...] = ("--set", "scheduleFeed.reference.ingest.sink=db")
    # Security review H2: the db sink connects only as its narrow role. Its
    # connect defaults to true since release A: off explicitly, and without
    # the roles at all (connect's default needs perService.enabled).
    no_connect = "postgresql.roles.perService.schedule_reference.connect=false"
    code, out = c.render(*ON, *db, "--set", no_connect)
    c.check(
        ok=code != 0 and "perService.schedule_reference.connect" in out,
        message="sink db without its role: rendered, or failed without naming it",
    )
    code, out = c.render(*ON, *db)
    c.check(
        ok=code != 0 and "postgresql.roles.perService.enabled: true" in out,
        message="sink db with no per-service roles: rendered, or failed unnamed",
    )
    db = (*db, *REFERENCE_ROLE)
    docs = c.docs(*ON, *NETPOL, *db)
    check_no_duplicate_env(c, "sink db", docs)
    entries = env_list(container(docs, "reference"))
    names = [str(e["name"]) for e in entries]
    reference = env(container(docs, "reference"))
    c.check(
        ok=set(reference) >= DB_ENV,
        message=f"sink db: reference lacks {sorted(DB_ENV - set(reference))}",
    )
    c.check(
        ok=reference.get("INGEST_SINK", {}).get("value") == "db",
        message=f"sink db: INGEST_SINK={reference.get('INGEST_SINK')}",
    )
    c.check(
        ok=reference.get("DATABASE_MAX_CONNECTIONS", {}).get("value") == "3",
        message="sink db: DATABASE_MAX_CONNECTIONS is not the default 3",
    )
    c.check(
        ok=reference.get("CORPUS_FALLBACK_ENABLED", {}).get("value") == "false",
        message="sink db: CORPUS_FALLBACK_ENABLED is not the api's (false)",
    )
    c.check(
        ok="PGPASSWORD" in names
        and "DATABASE_URL" in names
        and names.index("PGPASSWORD") < names.index("DATABASE_URL"),
        message=f"sink db: PGPASSWORD must precede DATABASE_URL: {names}",
    )
    for other in ("ingest", "sftp"):
        leaked = DB_ENV & set(env(container(docs, other)))
        c.check(ok=not leaked, message=f"sink db: {other} has {sorted(leaked)}")
    c.check(
        ok=postgres_egress(schedulefeed_policy(docs)),
        message="sink db: schedulefeed has no postgres egress",
    )
    c.check(
        ok="schedulefeed" in postgres_clients(docs),
        message="sink db: postgres does not admit schedulefeed",
    )
    c.check(
        ok=bool(sftp_policy(docs)) and not postgres_egress(sftp_policy(docs)),
        message="sink db: no schedulefeed-sftp policy, or it may reach postgres",
    )
    c.check(
        ok=SFTP_COMPONENT not in postgres_clients(docs),
        message="sink db: postgres admits schedulefeed-sftp",
    )

    code, out = c.render(*ON, "--set", "scheduleFeed.reference.ingest.sink=redis")
    c.check(
        ok=code != 0 and "scheduleFeed.reference.ingest.sink" in out,
        message="sink redis: rendered, or failed without naming the value",
    )


def check_audit_and_override(c: Checker) -> None:
    """Audit-log shipping adds its vars; extraEnv overrides a BUCKET_* var."""
    ingest = env(container(c.docs(*ON, *BUCKET, *AUDIT), "ingest"))
    for name, value in AUDIT_ENV.items():
        got = ingest.get(name, {}).get("value")
        c.check(ok=got == value, message=f"audit logs: {name}={got!r}, want {value!r}")
    override = (
        "--set",
        "scheduleFeed.ingest.extraEnv[0].name=BUCKET_POLL_INTERVAL_SECS",
        "--set-string",
        "scheduleFeed.ingest.extraEnv[0].value=120",
    )
    entries = env_list(container(c.docs(*ON, *BUCKET, *override), "ingest"))
    found = [
        e.get("value") for e in entries if e["name"] == "BUCKET_POLL_INTERVAL_SECS"
    ]
    c.check(ok=found == ["120"], message=f"extraEnv BUCKET_POLL_INTERVAL_SECS: {found}")


def check_contract_and_secrets(c: Checker) -> None:
    """Every contract var renders; no mode renders a key or the reader Secret."""
    fake = ("--set", f"scheduleFeed.bucket.baseUrl={FAKE_GCS}")
    full = c.docs(*ON, *BUCKET, *AUDIT, *fake)
    missing = sorted(CONTRACT - set(env(container(full, "ingest"))))
    c.check(ok=not missing, message=f"contract: ingest lacks {missing}")
    modes = {
        "sftp only": ON,
        "both": (*ON, *BUCKET),
        "bucket only": (*ON, *BUCKET, *SFTP_OFF),
        "everything": (*ON, *BUCKET, *AUDIT, *fake, *NETPOL),
    }
    for label, args in modes.items():
        for doc in c.docs(*args):
            meta = cast("dict[str, dict[str, str]]", doc["metadata"])
            host_keys = (
                doc.get("kind") == "Secret"
                and meta.get("labels", {}).get("app.kubernetes.io/component")
                == "schedulefeed"
            )
            text = yaml.safe_dump(doc)
            c.check(
                ok=host_keys
                or ("private_key" not in text and "BEGIN PRIVATE KEY" not in text),
                message=f"{label}: {name_of(doc)} holds a private key",
            )
            c.check(
                ok=name_of(doc) != "Secret/distant-signal-schedulefeed-bucket",
                message=f"{label}: renders the reader key's Secret",
            )


# Bad values (on top of ON and BUCKET) and what the error must name.
FAILURES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "empty existingSecret",
        ("--set", "scheduleFeed.bucket.existingSecret="),
        "scheduleFeed.bucket.existingSecret",
    ),
    ("empty name", ("--set", "scheduleFeed.bucket.name="), "scheduleFeed.bucket.name"),
    (
        "dotted name",
        ("--set", "scheduleFeed.bucket.name=Example.Bucket"),
        "scheduleFeed.bucket.name",
    ),
    (
        "no expectedKeys",
        ("--set", "scheduleFeed.bucket.expectedKeys=null"),
        "scheduleFeed.bucket.expectedKeys",
    ),
    (
        "expectedKeys with /",
        ("--set", "scheduleFeed.bucket.expectedKeys[0]=a/b"),
        "scheduleFeed.bucket.expectedKeys",
    ),
    (
        "maxObjectBytes over the hour cap",
        ("--set", "scheduleFeed.bucket.maxObjectBytes=300000000"),
        "maxObjectBytes",
    ),
    (
        "hour cap over the day cap",
        ("--set", "scheduleFeed.bucket.maxDownloadBytesPerHour=2000000000"),
        "maxDownloadBytesPerDay",
    ),
    (
        "sourcePrecedence {sftp}",
        ("--set", "scheduleFeed.sourcePrecedence={sftp}"),
        "scheduleFeed.sourcePrecedence",
    ),
    (
        "sourcePrecedence {bucket,bucket}",
        ("--set", "scheduleFeed.sourcePrecedence={bucket,bucket}"),
        "scheduleFeed.sourcePrecedence",
    ),
    (
        "provider s3",
        ("--set", "scheduleFeed.bucket.provider=s3"),
        "scheduleFeed.bucket.provider",
    ),
    (
        "audit logs without a bucket",
        ("--set", "scheduleFeed.bucket.auditLogs.ship=true"),
        "scheduleFeed.bucket.auditLogs.bucket",
    ),
    (
        "ftp baseUrl",
        ("--set", "scheduleFeed.bucket.baseUrl=ftp://x"),
        "scheduleFeed.bucket.baseUrl",
    ),
    (
        "deleteMinAgeSecs of 7 days",
        ("--set", "scheduleFeed.bucket.deleteMinAgeSecs=604800"),
        "scheduleFeed.bucket.deleteMinAgeSecs",
    ),
    (
        "pollIntervalSecs under 60",
        ("--set", "scheduleFeed.bucket.pollIntervalSecs=10"),
        "scheduleFeed.bucket.pollIntervalSecs",
    ),
)


def check_failures(c: Checker) -> None:
    """Bad bucket values refuse to render, naming the key."""
    for label, args, key in FAILURES:
        code, out = c.render(*ON, *BUCKET, *args)
        c.check(
            ok=code != 0 and key in out,
            message=f"{label}: rendered, or the error doesn't name {key}: "
            f"{out.strip()[-300:]}",
        )


# Bad keyless values (on top of ON and WIF) and what the error must name.
WIF_FAILURES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "no credential ConfigMap",
        ("--set", "scheduleFeed.bucket.workloadIdentity.credentialConfigMap="),
        "scheduleFeed.bucket.workloadIdentity.credentialConfigMap",
    ),
    (
        "a key Secret too",
        (
            "--set",
            "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
        ),
        "scheduleFeed.bucket.existingSecret",
    ),
    (
        "a key name too",
        ("--set", "scheduleFeed.bucket.serviceAccountKey=other.json"),
        "scheduleFeed.bucket.serviceAccountKey",
    ),
    (
        "the shared ServiceAccount",
        ("--set", "scheduleFeed.serviceAccount.create=false"),
        "scheduleFeed.serviceAccount.create",
    ),
    (
        "the shared ServiceAccount by name",
        (
            "--set",
            "scheduleFeed.serviceAccount.create=false",
            "--set",
            "scheduleFeed.serviceAccount.name=distant-signal",
        ),
        "dedicated ServiceAccount",
    ),
    (
        "an unknown auth",
        ("--set", "scheduleFeed.bucket.auth=wif"),
        "scheduleFeed.bucket.auth",
    ),
    (
        "no audience",
        ("--set", "scheduleFeed.bucket.workloadIdentity.audience="),
        "scheduleFeed.bucket.workloadIdentity.audience",
    ),
    (
        "a bad ConfigMap key",
        ("--set", "scheduleFeed.bucket.workloadIdentity.credentialConfigKey=a/b"),
        "scheduleFeed.bucket.workloadIdentity.credentialConfigKey",
    ),
)

# The keyless pod's two bucket volumes, exactly.
WIF_VOLUMES = {
    "bucket-credentials": {
        "name": "bucket-credentials",
        "configMap": {
            "name": "ds-ingest-gcp-wif",
            "optional": True,
            "defaultMode": 0o440,
            "items": [
                {"key": "credential-config.json", "path": "credential-config.json"}
            ],
        },
    },
    "bucket-identity-token": {
        "name": "bucket-identity-token",
        "projected": {
            "sources": [
                {
                    "serviceAccountToken": {
                        "path": "token",
                        "audience": "gcp-ds-ingest",
                        "expirationSeconds": 3600,
                    }
                }
            ]
        },
    },
}


def check_wif_pod(c: Checker, docs: Sequence[Doc]) -> None:
    """Keyless: the ingest env, the two volumes and mounts, the ServiceAccount."""
    ingest = env(container(docs, "ingest"))
    got = ingest.get("GOOGLE_APPLICATION_CREDENTIALS", {}).get("value")
    c.check(
        ok=got == WIF_CONFIG,
        message=f"keyless: GOOGLE_APPLICATION_CREDENTIALS={got!r}",
    )
    for name, value in BOTH_ENV.items():
        want = None if name == "GOOGLE_SERVICE_ACCOUNT_PATH" else value
        got = ingest.get(name, {}).get("value")
        c.check(
            ok=got == want, message=f"keyless: ingest {name}={got!r}, want {want!r}"
        )
    vols = {n: v for n, v in volumes(docs).items() if n.startswith("bucket")}
    c.check(ok=vols == WIF_VOLUMES, message=f"keyless: bucket volumes {vols}")
    ingest_mounts = mounts(container(docs, "ingest"))
    for vol, path in (
        ("bucket-credentials", KEY_DIR),
        ("bucket-identity-token", WIF_TOKEN_DIR),
    ):
        mount = ingest_mounts.get(vol, {})
        c.check(
            ok=mount.get("mountPath") == path and mount.get("readOnly") is True,
            message=f"keyless: ingest's {vol} mount {mount}",
        )
    for other in ("sftp", "reference"):
        ctr = container(docs, other)
        leaked = sorted(n for n in mounts(ctr) if n.startswith("bucket"))
        leaked += sorted(n for n in env(ctr) if n.startswith(("BUCKET_", "GOOGLE_")))
        c.check(ok=not leaked, message=f"keyless: {other} has {leaked}")
    pod = pod_spec(docs)
    c.check(
        ok=pod.get("serviceAccountName") == WIF_SERVICE_ACCOUNT
        and pod.get("automountServiceAccountToken") is False,
        message=f"keyless: pod ServiceAccount {pod.get('serviceAccountName')}",
    )
    accounts = [
        d for d in docs if name_of(d) == f"ServiceAccount/{WIF_SERVICE_ACCOUNT}"
    ]
    c.check(
        ok=len(accounts) == 1
        and accounts[0].get("automountServiceAccountToken") is False,
        message=f"keyless: ServiceAccount/{WIF_SERVICE_ACCOUNT} {accounts}",
    )


def check_workload_identity(c: Checker) -> None:
    """Keyless: pod, egress, key mode untouched, and the guards."""
    netpol = (*NETPOL, "--set", "networkPolicy.egress.internetPorts={443}")
    docs = c.docs(*ON, *WIF, *netpol)
    check_wif_pod(c, docs)
    ports = internet_ports(schedulefeed_policy(docs))
    c.check(ok=HTTPS_PORT in ports, message=f"keyless: internet ports {ports}")
    c.check(
        ok=not any(
            name_of(d) == f"ServiceAccount/{WIF_SERVICE_ACCOUNT}"
            for d in c.docs(*ON, *BUCKET)
        ),
        message="key mode: the schedulefeed ServiceAccount rendered unasked",
    )
    code, out = c.render(
        *ON,
        *BUCKET,
        "--set",
        "scheduleFeed.bucket.workloadIdentity.credentialConfigMap=ds-ingest-gcp-wif",
    )
    c.check(
        ok=code != 0 and "scheduleFeed.bucket.auth" in out,
        message=f"key mode with a credential ConfigMap: {out.strip()[-300:]}",
    )
    for label, args, key in WIF_FAILURES:
        code, out = c.render(*ON, *WIF, *args)
        c.check(
            ok=code != 0 and key in out,
            message=f"keyless {label}: rendered, or the error doesn't name {key}: "
            f"{out.strip()[-300:]}",
        )


def alert_groups(docs: Sequence[Doc]) -> dict[str, set[str]]:
    """Return every PrometheusRule group's alert names, by group name."""
    out: dict[str, set[str]] = {}
    for doc in docs:
        if doc.get("kind") != "PrometheusRule":
            continue
        spec = cast("dict[str, list[dict[str, object]]]", doc["spec"])
        for group in spec["groups"]:
            rules = cast("list[dict[str, str]]", group["rules"])
            out[str(group["name"])] = {r["alert"] for r in rules if "alert" in r}
    return out


def check_alerts(c: Checker) -> None:
    """Check the bucket alert group renders only with the bucket, and right."""
    off = alert_groups(c.docs(*ON, *RULES))
    c.check(ok=BUCKET_GROUP not in off, message="bucket off: bucket alerts")
    both = alert_groups(c.docs(*ON, *BUCKET, *RULES)).get(BUCKET_GROUP)
    c.check(
        ok=both == {*BUCKET_ALERTS, DISAGREE},
        message=f"both: bucket alerts {sorted(both or [])}",
    )
    only = alert_groups(c.docs(*ON, *BUCKET, *SFTP_OFF, *RULES))
    c.check(
        ok=only.get(BUCKET_GROUP) == BUCKET_ALERTS and SFTP_GROUP not in only,
        message=f"bucket only: groups {sorted(only)}, "
        f"bucket alerts {sorted(only.get(BUCKET_GROUP, []))}",
    )
    disabled = (
        "--set",
        "metrics.prometheusRule.scheduleBucket.enabled=false",
    )
    groups = alert_groups(c.docs(*ON, *BUCKET, *RULES, *disabled))
    c.check(
        ok=BUCKET_GROUP not in groups,
        message="scheduleBucket.enabled=false: bucket alerts",
    )


def check_no_duplicate_env(c: Checker, label: str, docs: Sequence[Doc]) -> None:
    """No container in any document repeats an env name."""
    for doc in docs:
        for ctr in containers_of(doc):
            names = [str(e["name"]) for e in env_list(ctr)]
            dupes = sorted({n for n in names if names.count(n) > 1})
            c.check(
                ok=not dupes,
                message=f"{label}: {name_of(doc)} {ctr.get('name')} repeats {dupes}",
            )


def check_extra_env(c: Checker) -> None:
    """scheduleFeed.ingest.extraEnv replaces by name, in place; extras follow."""
    docs = c.docs(
        *ON,
        "--set",
        "scheduleFeed.ingest.extraEnv[0].name=RUST_LOG",
        "--set-string",
        "scheduleFeed.ingest.extraEnv[0].value=trace",
        "--set",
        "scheduleFeed.ingest.extraEnv[1].name=EXTRA_ONLY",
        "--set-string",
        "scheduleFeed.ingest.extraEnv[1].value=x",
    )
    check_no_duplicate_env(c, "ingest extraEnv", docs)
    names = [str(e["name"]) for e in env_list(container(docs, "ingest"))]
    values = env(container(docs, "ingest"))
    c.check(
        ok=names.count("RUST_LOG") == 1 and values["RUST_LOG"].get("value") == "trace",
        message=f"ingest extraEnv: RUST_LOG not replaced: {values.get('RUST_LOG')}",
    )
    at = names.index("RUST_LOG") if "RUST_LOG" in names else 0
    c.check(
        ok=at > 0 and names[at - 1] == "PROGRESS_STALL_SECS",
        message=f"ingest extraEnv: RUST_LOG moved: {names}",
    )
    c.check(
        ok=bool(names) and names[-1] == "EXTRA_ONLY",
        message=f"ingest extraEnv: the extra-only entry isn't last: {names}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; return the exit status."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--helm", default=shutil.which("helm") or "helm", help="helm binary"
    )
    parser.add_argument(
        "--baseline",
        type=pathlib.Path,
        metavar="DIR",
        help="also check DIR (another commit's charts/distant-signal) renders "
        "identically",
    )
    parser.add_argument(
        "--baseline-set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="with --baseline, --set KEY=VALUE on this chart's renders only",
    )
    args = parser.parse_args(argv)
    c = Checker(args.helm)

    check_no_duplicate_env(c, "schedulefeed on", c.docs(*ON))
    check_extra_env(c)
    check_explicit_defaults(c)
    check_neither_source(c)
    check_sftp_split(c)
    check_sftp_one_pod(c)
    check_sftp_stable_across_releases(c)
    check_sftp_only(c)
    check_both(c)
    check_bucket_only(c)
    check_network_policy(c)
    check_audit_and_override(c)
    check_contract_and_secrets(c)
    check_failures(c)
    check_workload_identity(c)
    check_alerts(c)
    check_reference_sink(c)
    for label, values in (
        ("both", (*ON, *BUCKET)),
        ("bucket only", (*ON, *BUCKET, *SFTP_OFF, *AUDIT)),
        ("keyless", (*ON, *WIF)),
    ):
        check_no_duplicate_env(c, label, c.docs(*values))
    if args.baseline is not None:
        extra = [a for kv in args.baseline_set for a in ("--set", kv)]
        check_baseline(c, args.baseline, extra)

    for failure in c.failures:
        print(f"FAIL: {failure}")
    if not c.failures:
        print("schedulefeed chart: all checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
