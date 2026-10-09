#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the per-alert report) is stdout
"""Keep the chart's alerts small enough to deliver, and check them with promtool.

Renders charts/distant-signal with every alert group switched on and checks
each alert in its PrometheusRules:

  - `summary` (one line, at most 80 characters), `description` (at most 150)
    and `runbook_url` are all set, each `{{ ... }}` counted as a sample of
    what it renders to (24 characters for a label, 6 for a $value);
  - `runbook_url` is the alert's own `### <alert>` section of docs/alerts.md;
  - summary + description are at most 300 bytes;
  - `expr`, whitespace collapsed, is at most 300 characters. Alertmanager's
    `generatorURL` carries the URL-encoded expr, so a long expr is what made
    each alert about 1 KB on its own (the 2026-10-01 outage: a 3-alert
    DistantSignalPollerFailing group was 5.8 KB of webhook JSON, and ntfy
    refuses anything over 4,096 bytes). Long expressions go in a recording
    rule; the alert reads the recorded series.
  - an estimate of a 3-alert group's webhook JSON (labels, annotations,
    generatorURL) is at most 4,096 bytes.

  scripts/check-alert-payloads.py [--report] [--helm HELM]
      [--promtool PROMTOOL | --download-promtool DIR]

--report prints every alert's sizes. With a promtool (given, or downloaded
and checksum-verified into DIR), it also runs `promtool check rules` on the
rendered groups and `promtool test rules` on scripts/alert-rules-tests/*.yaml.
Those tests name the rendered file `rules.yaml`, rendered as release
`distant-signal` in namespace `distant-signal`.

Needs helm on PATH (or --helm) and PyYAML (pinned in pyproject.toml's `lint`
dependency group).
"""

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
TESTS = REPO / "scripts" / "alert-rules-tests"
RUNBOOK = REPO / "docs" / "alerts.md"
# GitHub's anchor for each `### DistantSignalX` heading is its lowercase name.
RUNBOOK_ANCHORS = {
    line.removeprefix("### ").strip().lower()
    for line in RUNBOOK.read_text().splitlines()
    if line.startswith("### DistantSignal")
}
RELEASE = "distant-signal"
NAMESPACE = "distant-signal"

MAX_SUMMARY = 80
MAX_DESCRIPTION = 150
MAX_ANNOTATIONS_BYTES = 300
MAX_EXPR = 300
NTFY_LIMIT = 4096
GROUP_SIZE = 3

# promtool for `--download-promtool`, verified against the release's
# sha256sums.txt. Not tracked by Renovate: bump both together by hand.
PROMETHEUS_VERSION = "3.15.0"
PROMETHEUS_SHA256 = "2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0"

# Every values switch an alert depends on, so every alert renders. The
# kafka/LLM/SSO settings only satisfy the chart's required-value checks.
RENDER_FLAGS = [
    "trustConsumer.kafka.brokers=kafka.example.com:9094",
    "trustConsumer.kafka.topic=test-topic",
    "trustConsumer.kafka.saslMechanism=PLAIN",
    # The Claude provider in its keyless mode, for
    # DistantSignalEnricherTokenExchangeFailing (rendered in every workload
    # identity mode, OpenAI's included), with batch mode for the
    # distant-signal.enricher-batches group.
    "enricher.llm.provider=anthropic",
    "enricher.llm.batch.sweepMode=batch",
    "enricher.llm.auth=anthropicWifAuthentik",
    "enricher.serviceAccount.create=true",
    "enricher.llm.workloadIdentity.anthropic.organizationId=org-test",
    "enricher.llm.workloadIdentity.anthropic.serviceAccountId=svac_test",
    "enricher.llm.workloadIdentity.anthropic.federationRuleId=fdrl_test",
    "enricher.llm.workloadIdentity.authentik.tokenUrl=https://sso.example.com/application/o/token/",
    "enricher.llm.workloadIdentity.authentik.clientId=test-enricher-client",
    "api.sso.issuerUrl=https://sso.example.com",
    "api.sso.clientId=test-client",
    "api.sso.clientSecret=test-secret",
    "api.sso.redirectUrl=https://app.example.com/callback",
    "api.sso.postLoginRedirectUrl=https://app.example.com/",
    "metrics.prometheusRule.enabled=true",
    # Retired /private routes, for DistantSignalApiPrivateRouteRetiredCalled
    # (ingest phase 5, step 5.1). Off by default.
    "api.privateRoutes.enabled=false",
    "pollers.ldbws.enabled=true",
    "pollers.ldbws.baseUrl=https://ldbws.example.com",
    "pollers.incidents.enabled=true",
    "pollers.incidents.baseUrl=https://incidents.example.com",
    # A daily poller, for DistantSignalPollerStale's per-interval threshold.
    "pollers.stations.enabled=true",
    "pollers.stations.baseUrl=https://stations.example.com",
    # Their own Redis users: release A's default sinks with default off.
    "redis.acl.clients.pollerLdbws=true",
    "redis.acl.clients.pollerIncidents=true",
    "archive.enabled=true",
    "archive.s3.bucket=archive-bucket",
    "archive.s3.existingSecret=archive-creds",
    "archive.s3.prefix=cluster/distant-signal-archive",
    "archive.s3.lifecycleConfirmed=true",
    "archive.expiry.enabled=true",
    "scheduleFeed.enabled=true",
    "scheduleFeed.sftp.authMethod=password",
    "scheduleFeed.corpus.enabled=true",
    "scheduleFeed.bucket.enabled=true",
    "scheduleFeed.bucket.name=example-ds-ingest",
    "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
    "fullCoverageConsumer.windowedStats.enabled=true",
    # DistantSignalIngestWriterDown; with loops, DistantSignalWriterLoopStale
    # and DistantSignalWriterLoopUnowned.
    "ingestWriter.enabled=true",
    "ingestWriter.loops.enabled=true",
    "postgresql.pgbackrest.enabled=true",
    "postgresql.pgbackrest.image.repository=registry.example.com/postgres-pgbackrest",
    "postgresql.pgbackrest.image.tag=pg16.15-pgbackrest2.59.1-tini0.19.0",
    "postgresql.pgbackrest.repo.path=/test/pgbackrest",
    "postgresql.pgbackrest.repo.s3.endpoint=s3.example.com",
    "postgresql.pgbackrest.repo.s3.bucket=test-bucket",
    "postgresql.pgbackrest.repo.s3.existingSecret=pgbackrest-creds",
]

# What a template expression in an annotation expands to, for the size
# estimate: a typical label value, or a formatted $value.
SAMPLE_LABEL = "x" * 24
SAMPLE_VALUE = "12.34%"
TEMPLATE_EXPR = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
LABEL_REF = re.compile(r"\$labels\.([A-Za-z_][A-Za-z0-9_]*)")
# A Prometheus external URL of typical length; the webhook's generatorURL is
# <this>/graph?g0.expr=<url-encoded expr>&g0.tab=1.
EXTERNAL_URL = "http://kube-prometheus-stack-prometheus.monitoring:9090"
# Labels every alert carries besides its own (external labels, the ones
# kube-prometheus-stack adds).
EXTRA_LABELS = {
    "prometheus": "monitoring/kube-prometheus-stack-prometheus",
    "namespace": NAMESPACE,
    "pod": "distant-signal-component-6b7c9d8f5-abcde",
}


@dataclass(frozen=True)
class Alert:
    """One alerting rule as rendered."""

    name: str
    expr: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]


def render(helm: str) -> list[dict[str, object]]:
    """Render the chart with every alert on; return the PrometheusRule documents."""
    # Release A's defaults (2026-10-09: every producer on its db or stream
    # sink) with the prerequisites they need, so the stream alerts render.
    prereqs = str(CHART / "ci" / "ingest-prereqs.yaml")
    cmd = [
        helm,
        "template",
        RELEASE,
        str(CHART),
        "--namespace",
        NAMESPACE,
        "-f",
        prereqs,
    ]
    for flag in RENDER_FLAGS:
        cmd += ["--set", flag]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # noqa: S603  # fixed argv, no shell
    docs = [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]
    return [d for d in docs if d.get("kind") == "PrometheusRule"]


def groups_of(rules: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Every rule group of every PrometheusRule, in order."""
    out: list[dict[str, object]] = []
    for rule in rules:
        spec = cast("Mapping[str, object]", rule.get("spec") or {})
        out.extend(cast("list[dict[str, object]]", spec.get("groups") or []))
    return out


def alerts_of(groups: Sequence[Mapping[str, object]]) -> Iterator[Alert]:
    """Every alerting rule (recording rules are skipped)."""
    for group in groups:
        for rule in cast("list[dict[str, object]]", group.get("rules") or []):
            if "alert" not in rule:
                continue
            yield Alert(
                name=str(rule["alert"]),
                expr=str(rule.get("expr", "")),
                labels={
                    str(k): str(v)
                    for k, v in cast(
                        "dict[str, object]", rule.get("labels") or {}
                    ).items()
                },
                annotations={
                    str(k): str(v)
                    for k, v in cast(
                        "dict[str, object]", rule.get("annotations") or {}
                    ).items()
                },
            )


def collapse(expr: str) -> str:
    """Collapse every whitespace run in `expr` to one space."""
    return " ".join(expr.split())


def expand(text: str) -> str:
    """Replace each `{{ ... }}` with a sample of what it renders to."""

    def sample(match: re.Match[str]) -> str:
        return SAMPLE_VALUE if "$value" in match.group(1) else SAMPLE_LABEL

    return TEMPLATE_EXPR.sub(sample, text)


def generator_url(expr: str) -> str:
    """Alertmanager's generatorURL for an alert with this expr."""
    encoded = urllib.parse.quote_plus(collapse(expr))
    return f"{EXTERNAL_URL}/graph?g0.expr={encoded}&g0.tab=1"


def webhook_estimate(alert: Alert) -> int:
    """Bytes of Alertmanager's webhook JSON for a group of GROUP_SIZE of this alert.

    Each alert gets distinct label values (so nothing but alertname, severity,
    namespace and the runbook is common to the group), the worst case.
    """
    referenced = sorted(set(LABEL_REF.findall(" ".join(alert.annotations.values()))))
    alerts: list[dict[str, object]] = []
    common_labels: dict[str, str] = {}
    for i in range(GROUP_SIZE):
        labels = {"alertname": alert.name, **alert.labels, **EXTRA_LABELS}
        for name in referenced:
            labels.setdefault(name, f"{SAMPLE_LABEL[:-1]}{i}")
        labels["pod"] = f"{EXTRA_LABELS['pod'][:-1]}{i}"
        common_labels = {
            k: v
            for k, v in labels.items()
            if k in {"alertname", "severity", "namespace"}
        }
        annotations = {
            k: expand(v) + str(i)
            for k, v in alert.annotations.items()
            if k != "runbook_url"
        }
        if "runbook_url" in alert.annotations:
            annotations["runbook_url"] = alert.annotations["runbook_url"]
        alerts.append(
            {
                "status": "firing",
                "labels": labels,
                "annotations": annotations,
                "startsAt": "2026-10-01T10:16:00.000Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": generator_url(alert.expr),
                "fingerprint": "0123456789abcdef",
            }
        )
    payload = {
        "receiver": "ntfy",
        "status": "firing",
        "alerts": alerts,
        "groupLabels": {"alertname": alert.name, "namespace": NAMESPACE},
        "commonLabels": common_labels,
        "commonAnnotations": {"runbook_url": alert.annotations.get("runbook_url", "")},
        "externalURL": "http://kube-prometheus-stack-alertmanager.monitoring:9093",
        "version": "4",
        "groupKey": '{}:{alertname="'
        + alert.name
        + '", namespace="'
        + NAMESPACE
        + '"}',
        "truncatedAlerts": 0,
    }
    return len(json.dumps(payload, separators=(",", ":")).encode())


def annotation_problems(alert: Alert) -> list[str]:
    """Every annotation rule this alert breaks."""
    out: list[str] = []
    summary = expand(alert.annotations.get("summary", ""))
    description = expand(alert.annotations.get("description", ""))
    if not summary or "\n" in summary.strip():
        out.append("summary must be one non-empty line")
    if len(summary) > MAX_SUMMARY:
        out.append(f"summary is {len(summary)} chars (max {MAX_SUMMARY})")
    if not description:
        out.append("description is empty")
    if len(description) > MAX_DESCRIPTION:
        out.append(f"description is {len(description)} chars (max {MAX_DESCRIPTION})")
    size = len((summary + description).encode())
    if size > MAX_ANNOTATIONS_BYTES:
        out.append(f"summary+description is {size} B (max {MAX_ANNOTATIONS_BYTES})")
    anchor = alert.name.lower()
    runbook = alert.annotations.get("runbook_url", "")
    if not runbook.endswith(f"/docs/alerts.md#{anchor}"):
        out.append(f"runbook_url must end /docs/alerts.md#{anchor}")
    elif anchor not in RUNBOOK_ANCHORS:
        out.append(f"docs/alerts.md has no '### {alert.name}' section")
    return out


def size_problems(alert: Alert) -> list[str]:
    """Every expr or webhook size limit this alert breaks."""
    out: list[str] = []
    expr = len(collapse(alert.expr))
    if expr > MAX_EXPR:
        out.append(f"expr is {expr} chars (max {MAX_EXPR}): use a recording rule")
    webhook = webhook_estimate(alert)
    if webhook > NTFY_LIMIT:
        out.append(
            f"a {GROUP_SIZE}-alert group is about {webhook} B of webhook JSON"
            f" (ntfy max {NTFY_LIMIT})"
        )
    return out


def report_line(alert: Alert) -> str:
    """One alert's sizes, for --report."""
    summary = expand(alert.annotations.get("summary", ""))
    description = expand(alert.annotations.get("description", ""))
    return (
        f"{alert.name:48} expr={len(collapse(alert.expr)):4}"
        f" url={len(generator_url(alert.expr)):4}"
        f" annotations={len((summary + description).encode()):4}"
        f" group{GROUP_SIZE}={webhook_estimate(alert):5}"
    )


def download_promtool(directory: pathlib.Path) -> pathlib.Path:
    """Fetch PROMETHEUS_VERSION's promtool into `directory`, checksum-verified."""
    target = directory / "promtool"
    if target.exists():
        return target
    name = f"prometheus-{PROMETHEUS_VERSION}.linux-amd64"
    url = (
        "https://github.com/prometheus/prometheus/releases/download"
        f"/v{PROMETHEUS_VERSION}/{name}.tar.gz"
    )
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{name}.tar.gz"
    with urllib.request.urlopen(url) as response, archive.open("wb") as out:  # noqa: S310  # a fixed https URL
        shutil.copyfileobj(response, out)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != PROMETHEUS_SHA256:
        archive.unlink()
        msg = f"{archive.name}: sha256 {digest}, expected {PROMETHEUS_SHA256}"
        raise SystemExit(msg)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"{name}/promtool")
        source = tar.extractfile(member)
        if source is None:
            msg = f"{archive.name} has no promtool"
            raise SystemExit(msg)
        target.write_bytes(source.read())
    target.chmod(0o755)
    archive.unlink()
    return target


def run_promtool(promtool: pathlib.Path, groups: Sequence[Mapping[str, object]]) -> int:
    """`promtool check rules` on the rendered groups, then `test rules` on TESTS."""
    status = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        (work / "rules.yaml").write_text(
            yaml.safe_dump({"groups": list(groups)}, sort_keys=False)
        )
        tests = sorted(TESTS.glob("*.yaml"))
        for test in tests:
            shutil.copy(test, work / test.name)
        commands = [[str(promtool), "check", "rules", "rules.yaml"]]
        if tests:
            commands.append([str(promtool), "test", "rules", *(t.name for t in tests)])
        for cmd in commands:
            print("== " + " ".join(cmd[1:]))
            result = subprocess.run(  # noqa: S603  # fixed argv, no shell
                cmd, cwd=work, check=False, capture_output=True, text=True
            )
            print(result.stdout + result.stderr, end="")
            status |= result.returncode != 0
    return status


def main(argv: Sequence[str]) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--report", action="store_true", help="print every alert's sizes"
    )
    parser.add_argument(
        "--helm", default="helm", help="the helm binary (default: helm on PATH)"
    )
    tool = parser.add_mutually_exclusive_group()
    tool.add_argument(
        "--promtool",
        type=pathlib.Path,
        help="run promtool check/test rules with this binary",
    )
    tool.add_argument(
        "--download-promtool",
        type=pathlib.Path,
        metavar="DIR",
        help="download promtool into DIR",
    )
    args = parser.parse_args(argv)

    groups = groups_of(render(args.helm))
    alerts = list(alerts_of(groups))
    if not alerts:
        print("no alerts rendered", file=sys.stderr)
        return 1
    status = 0
    for alert in alerts:
        if args.report:
            print(report_line(alert))
        for problem in annotation_problems(alert) + size_problems(alert):
            print(f"{alert.name}: {problem}", file=sys.stderr)
            status = 1
    print(f"{len(alerts)} alerts checked")
    promtool: pathlib.Path | None = args.promtool
    if args.download_promtool is not None:
        promtool = download_promtool(args.download_promtool)
    if promtool is not None:
        status |= run_promtool(promtool, groups)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
