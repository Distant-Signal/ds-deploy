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
  - schedulefeed with neither source (`scheduleFeed.sftp.enabled=false`,
    bucket off) refuses to render, naming both switches;
  - SFTP only (the default): no bucket or source-switch env on any
    container, no `bucket-credentials` volume, containers sftp, ingest and
    reference;
  - both sources: the ingest env contract (source switches, precedence,
    bucket name, key path, expected keys, every BUCKET_* default as a plain
    integer; no BUCKET_BASE_URL or audit-log vars unless set), the reader
    key as an optional 0440 Secret volume mounted read-only on `ingest`
    only, and no bucket env on `sftp` or `reference`;
  - bucket only: containers ingest and reference, SFTP_SOURCE_ENABLED
    false, no SFTP Service, entrypoint ConfigMap, host-key Secret, volumes
    or checksum annotation, the PVC kept, and no sftp.authMethod needed;
  - bucket only with NetworkPolicy egress and internetPorts [443]: no SFTP
    (2022) or SFTP telemetry (9097) ingress, and the internet rule allows
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
    postgres policy `schedulefeed` ingress; an unknown sink refuses to
    render.

--baseline DIR renders DIR (a copy of charts/distant-signal from another
commit, e.g. the merge base) and this chart with the same flags, for the
defaults; schedulefeed on; schedulefeed on with the NetworkPolicy and its
egress rules; both sources (key auth); bucket only with the NetworkPolicy;
and values-example.yaml. Every document must be identical
after normalisation (the `data`/`stringData` of every Secret are dropped:
genPrivateKey and randAlphaNum differ between runs). A difference fails
with a unified diff per document. A one-off check for refactors; CI
doesn't run it (on main it would compare main with itself).

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
import difflib
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
EXAMPLE = "values-example.yaml"
# CI's required-value flags (the kafka/LLM/SSO settings only satisfy the
# chart's required-value checks).
BASE = (
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
SFTP_OFF = ("--set", "scheduleFeed.sftp.enabled=false")
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


def container(docs: Sequence[Doc], name: str) -> Container:
    """Return the schedulefeed Deployment's container `name` (empty if absent)."""
    deployment = schedulefeed(docs)
    found = [c for c in containers_of(deployment or {}) if c.get("name") == name]
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


def check_baseline(c: Checker, base: pathlib.Path) -> None:
    """Every BASELINE_SETS render of `base` equals this chart's."""
    for label, args in BASELINE_SETS:
        compare(c, label, c.docs(*args, chart=base), c.docs(*args))


def check_explicit_defaults(c: Checker) -> None:
    """`sftp.enabled=true` and `bucket.auth=key` render exactly the default."""
    for name, explicit in (
        ("sftp", ("--set", "scheduleFeed.sftp.enabled=true")),
        ("key auth", ("--set", "scheduleFeed.bucket.auth=key")),
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


def pod_spec(docs: Sequence[Doc]) -> dict[str, object]:
    """Return the schedulefeed Deployment's pod spec (empty if not rendered)."""
    spec = cast("dict[str, dict[str, object]]", (schedulefeed(docs) or {})["spec"])
    return cast("dict[str, object]", spec["template"]["spec"])


def volumes(docs: Sequence[Doc]) -> dict[str, dict[str, object]]:
    """Return the schedulefeed pod's volumes, by name."""
    vols = cast("list[dict[str, object]]", pod_spec(docs).get("volumes") or [])
    return {str(v["name"]): v for v in vols}


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
        ok=container_names(docs) == ["sftp", "ingest", "reference"],
        message=f"sftp only: containers {container_names(docs)}",
    )


def check_both(c: Checker) -> None:
    """Both sources: the ingest env contract and the reader key mount."""
    docs = c.docs(*ON, *BUCKET)
    c.check(
        ok=container_names(docs) == ["sftp", "ingest", "reference"],
        message=f"both: containers {container_names(docs)}",
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

    db = ("--set", "scheduleFeed.reference.ingest.sink=db")
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
    args = parser.parse_args(argv)
    c = Checker(args.helm)

    check_no_duplicate_env(c, "schedulefeed on", c.docs(*ON))
    check_extra_env(c)
    check_explicit_defaults(c)
    check_neither_source(c)
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
        check_baseline(c, args.baseline)

    for failure in c.failures:
        print(f"FAIL: {failure}")
    if not c.failures:
        print("schedulefeed chart: all checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
