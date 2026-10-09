#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check that charts/distant-signal's pod templates don't change per release.

  uv run scripts/check-pod-labels-chart.py [--helm HELM]

Any change to a pod template rolls its pods. CI packages the chart with a
new version (`<base>+build.<run>.sha.<sha>`) and appVersion (`sha-<sha>`)
on every push, so a pod template that carries either restarts on every
release, whether or not its image or settings changed: Redis (Recreate, an
outage window) and schedulefeed did, until pod templates moved to
`distant-signal.podLabels` (templates/_helpers.tpl).

For the default values, values-example.yaml, and a render with every
optional workload on (pollers, schedulefeed, ingest-writer, devAuthentik,
the migrate Job, the api-maintenance and pgBackRest CronJobs, the roles
setup Job), this checks every pod template (Deployments, StatefulSets,
Jobs, and CronJobs' jobTemplate):

  - no `helm.sh/chart` or `app.kubernetes.io/version` label;
  - it carries its controller's selector labels;
  - rendering a copy of the chart with a different version and appVersion
    changes no pod template except where the appVersion string itself
    appears (the image tag fallback, which must change). This catches a
    version reaching a pod template any other way, such as a checksum
    annotation over a rendered object whose labels carry the version.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
EXAMPLE = "values-example.yaml"
FORBIDDEN = ("helm.sh/chart", "app.kubernetes.io/version")
# The (version, appVersion) pairs the version-invariance renders put in
# Chart.yaml: CI's shapes, two runs apart.
VERSIONS = (
    ("9.9.9+build.1.sha.1111111", "sha-1111111"),
    ("9.9.9+build.2.sha.2222222", "sha-2222222"),
)
APP_VERSION_PLACEHOLDER = "<appVersion>"


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# Release A (2026-10-09) put every ingest producer on its db/stream sink by
# default; these checks predate it and test other switches, so they render
# on ci/http-sinks.yaml (every producer back on http, the writer on app).
BASE = (
    "-f",
    str(CHART / "ci" / "http-sinks.yaml"),
    *sets(
        "trustConsumer.kafka.brokers=k:9094",
        "trustConsumer.kafka.topic=t",
        "trustConsumer.kafka.saslMechanism=PLAIN",
        "enricher.llm.baseUrl=http://l/v1",
        "enricher.llm.model=m",
        "api.sso.issuerUrl=https://sso.example.com",
        "api.sso.clientId=c",
        "api.sso.clientSecret=s",
        "api.sso.redirectUrl=https://app.example.com/cb",
        "api.sso.postLoginRedirectUrl=https://app.example.com/",
    ),
)
EVERYTHING = (
    "-f",
    EXAMPLE,
    *sets(
        "scheduleFeed.enabled=true",
        "scheduleFeed.sftp.authMethod=password",
        "ingestWriter.enabled=true",
        "devAuthentik.enabled=true",
        "migrate.job.enabled=true",
        "apiMaintenance.enabled=true",
        "postgresql.roles.setupJob.enabled=true",
        "postgresql.roles.initScript=false",
        "postgresql.pgbackrest.enabled=true",
        "postgresql.pgbackrest.image.repository=registry.example.com/postgres-pgbackrest",
        "postgresql.pgbackrest.image.tag=pg16.15-pgbackrest2.59.1-tini0.19.0",
        "postgresql.pgbackrest.repo.path=/test/pgbackrest",
        "postgresql.pgbackrest.repo.s3.endpoint=s3.example.com",
        "postgresql.pgbackrest.repo.s3.bucket=test-bucket",
        "postgresql.pgbackrest.repo.s3.existingSecret=pgbackrest-creds",
    ),
)
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ()),
    ("values-example.yaml", ("-f", EXAMPLE)),
    ("everything on", EVERYTHING),
)
# Pod templates EVERYTHING must render, so a values rename can't silently
# shrink what this checks.
EXPECTED_IN_EVERYTHING = (
    "Deployment/distant-signal-redis",
    "Deployment/distant-signal-schedulefeed",
    "Deployment/distant-signal-schedulefeed-sftp",
    "Deployment/distant-signal-ingest-writer",
    "Deployment/distant-signal-devauthentik",
    "StatefulSet/distant-signal-postgres",
    "StatefulSet/distant-signal-devauthentik-postgres",
    "Job/distant-signal-migrate",
    "Job/distant-signal-postgres-roles-setup",
    "CronJob/distant-signal-api-maintenance jobTemplate",
    "CronJob/distant-signal-pgbackrest-full jobTemplate",
)

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def name_of(doc: Mapping[str, object]) -> str:
    """Return `Kind/name` of a rendered resource."""
    return f"{doc.get('kind')}/{as_map(doc.get('metadata')).get('name')}"


def pod_templates(doc: Mapping[str, object]) -> Iterator[tuple[str, Doc]]:
    """Yield (where, pod template) for each pod template in `doc`."""
    spec = as_map(doc.get("spec"))
    if "template" in spec:
        yield name_of(doc), as_map(spec["template"])
    job_template = as_map(spec.get("jobTemplate"))
    job_spec = as_map(job_template.get("spec"))
    if "template" in job_spec:
        yield f"{name_of(doc)} jobTemplate", as_map(job_spec["template"])


def labels_of(template: Mapping[str, object]) -> dict[str, object]:
    """Return a pod template's labels."""
    return as_map(as_map(template.get("metadata")).get("labels"))


def label_problems(doc: Mapping[str, object]) -> list[str]:
    """Return what is wrong with `doc`'s pod-template labels."""
    problems: list[str] = []
    selector = as_map(
        as_map(as_map(doc.get("spec")).get("selector")).get("matchLabels")
    )
    for where, template in pod_templates(doc):
        labels = labels_of(template)
        problems.extend(
            f"{where}: pod template carries {key}" for key in FORBIDDEN if key in labels
        )
        missing = {k: v for k, v in selector.items() if labels.get(k) != v}
        if missing:
            problems.append(f"{where}: pod template lacks selector labels {missing}")
    return problems


class Checker:
    """Renders the chart and collects failed checks."""

    def __init__(self, helm: str) -> None:
        """Use `helm` to render."""
        self.helm = helm
        self.failures: list[str] = []

    def docs(self, *args: str, chart: pathlib.Path = CHART) -> list[Doc]:
        """Render `chart` with BASE and `args`; parse every document."""
        args = tuple(str(chart / a) if a == EXAMPLE else a for a in args)
        result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "distant-signal", str(chart), *BASE, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.failures.append(f"render {list(args)} failed: {result.stderr.strip()}")
            return []
        return [cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if d]


def templates_by_name(docs: Sequence[Doc], app_version: str) -> dict[str, str]:
    """Each pod template as YAML, appVersion replaced by a placeholder."""
    return {
        where: yaml.safe_dump(template, sort_keys=True).replace(
            app_version, APP_VERSION_PLACEHOLDER
        )
        for doc in docs
        for where, template in pod_templates(doc)
    }


def chart_copy(tmp: pathlib.Path, version: str, app_version: str) -> pathlib.Path:
    """Copy the chart under `tmp` with Chart.yaml's version and appVersion set."""
    chart = tmp / app_version / "distant-signal"
    shutil.copytree(CHART, chart)
    chart_yaml = chart / "Chart.yaml"
    text = chart_yaml.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^version: .*$", f"version: {version}", text)
    text = re.sub(r"(?m)^appVersion: .*$", f'appVersion: "{app_version}"', text)
    chart_yaml.write_text(text, encoding="utf-8")
    return chart


def check_labels(c: Checker) -> None:
    """Check no pod template carries a per-release label, in any render."""
    for label, args in RENDERS:
        docs = c.docs(*args)
        found = {where for d in docs for where, _ in pod_templates(d)}
        if label == "everything on":
            c.failures.extend(
                f"everything on: {name} did not render"
                for name in EXPECTED_IN_EVERYTHING
                if name not in found
            )
        c.failures.extend(f"{label}: {p}" for d in docs for p in label_problems(d))
        print(f"{label}: {len(found)} pod templates checked")


def check_version_invariance(c: Checker) -> None:
    """Check a new chart version/appVersion changes only pod-template images."""
    with tempfile.TemporaryDirectory() as tmp:
        renders = [
            templates_by_name(
                c.docs(*EVERYTHING, chart=chart_copy(pathlib.Path(tmp), *versions)),
                versions[1],
            )
            for versions in VERSIONS
        ]
    before, after = renders
    c.failures.extend(
        f"{where}: pod template changes with the chart version or appVersion"
        for where in sorted(before.keys() | after.keys())
        if before.get(where) != after.get(where)
    )
    print(f"version invariance: {len(before)} pod templates compared")


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    c = Checker(cast("str", args.helm))
    check_labels(c)
    check_version_invariance(c)
    for failure in c.failures:
        print(failure)
    if not c.failures:
        print("ok: no pod template changes per release")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
