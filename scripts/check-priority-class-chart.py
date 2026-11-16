#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's pod priority values (chart README, "Pod priority").

  uv run scripts/check-priority-class-chart.py [--helm HELM]

  - With the defaults (every `priorityClassName` empty), no pod template
    names a PriorityClass, in the default, values-example.yaml and
    "everything on" renders: the pods keep the cluster's default priority.
  - With `global.priorityClassName` set, every pod template of the
    "everything on" render names it except the `helm test` pods.
  - A workload's own value wins over the global one, on its pods only:
    `scheduleFeed.priorityClassName` on the schedulefeed and SFTP pods and
    schedule-reference's CronJob, `scheduleFeed.reference.cron` and
    `scheduleFeed.sftp` over that, `pollers.tocs` on its CronJob.
  - Every template that renders a pod's `containers` includes
    `distant-signal.priorityClassName`, so a workload none of the renders
    turn on is covered too.
  - A name that isn't a DNS subdomain, and a built-in `system-*` class,
    refuse to render.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
HELPER = "distant-signal.priorityClassName"


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# The least a fresh install adds (release B's ingest prerequisites), as in
# the other chart checks.
BASE = (
    "-f",
    str(CHART / "ci" / "ingest-prereqs.yaml"),
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
    *BASE,
    *sets(
        "scheduleFeed.enabled=true",
        "scheduleFeed.sftp.authMethod=password",
        "scheduleFeed.reference.mode=cronjob",
        "devAuthentik.enabled=true",
        "migrate.job.enabled=true",
        "apiMaintenance.enabled=true",
        "api.bankHolidays.enabled=true",
        "api.bankHolidays.cronjob.enabled=true",
        "postgresql.roles.setupJob.enabled=true",
        "postgresql.roles.initScript=false",
        "postgresql.pgbackrest.enabled=true",
        "postgresql.pgbackrest.image.repository=registry.example.com/postgres-pgbackrest",
        "postgresql.pgbackrest.image.tag=pg18.6-pgbackrest2.59.1-tini0.19.0",
        "postgresql.pgbackrest.repo.path=/test/pgbackrest",
        "postgresql.pgbackrest.repo.s3.endpoint=s3.example.com",
        "postgresql.pgbackrest.repo.s3.bucket=test-bucket",
        "postgresql.pgbackrest.repo.s3.existingSecret=pgbackrest-creds",
        "pollers.tocs.enabled=true",
        "pollers.tocs.baseUrl=https://tocs.example.com",
        "redis.acl.clients.pollerTocs=true",
        "pollers.tocs.mode=cronjob",
        "pollerTransitSchedule.networks.tfl.enabled=true",
        "redis.acl.clients.pollerTransitTfl=true",
        "ingestWriter.streams.transit-tfl=apply",
        "pollerTransitSchedule.networks.tfl.timetableMode=cronjob",
    ),
)
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", BASE),
    ("values-example.yaml", ("-f", str(CHART / "values-example.yaml"))),
    ("everything on", EVERYTHING),
)
# Pod templates EVERYTHING must render, so a values rename can't silently
# shrink what this checks.
EXPECTED = (
    "Deployment/distant-signal-api",
    "Deployment/distant-signal-redis",
    "Deployment/distant-signal-ingest-writer",
    "Deployment/distant-signal-schedulefeed",
    "Deployment/distant-signal-schedulefeed-sftp",
    "StatefulSet/distant-signal-postgres",
    "Job/distant-signal-migrate",
    "Job/distant-signal-postgres-roles-setup",
    "CronJob/distant-signal-schedule-reference",
    "CronJob/distant-signal-poller-tocs",
    "CronJob/distant-signal-poller-transit-schedule-tfl",
    "CronJob/distant-signal-pgbackrest-full",
    "CronJob/distant-signal-bank-holidays",
)
GLOBAL = "ds-global"
FEED = "ds-feed"
PUBLISH = "ds-publish"
SFTP = "ds-sftp"
TOCS = "ds-tocs"
OVERRIDES = sets(
    f"global.priorityClassName={GLOBAL}",
    f"scheduleFeed.priorityClassName={FEED}",
    f"scheduleFeed.reference.cron.priorityClassName={PUBLISH}",
    f"scheduleFeed.sftp.priorityClassName={SFTP}",
    f"pollers.tocs.priorityClassName={TOCS}",
)
WANT_OVERRIDDEN = {
    "Deployment/distant-signal-schedulefeed": FEED,
    "Deployment/distant-signal-schedulefeed-sftp": SFTP,
    "CronJob/distant-signal-schedule-reference": PUBLISH,
    "CronJob/distant-signal-poller-tocs": TOCS,
}
REFUSED = (
    ("a system-* class", sets("api.priorityClassName=system-cluster-critical")),
    ("a name that isn't a DNS subdomain", sets("global.priorityClassName=Not_A_Name")),
    ("a name with an empty label", sets("api.priorityClassName=ds..critical")),
)

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def render(helm: str, args: Sequence[str]) -> tuple[list[Doc], str]:
    """Render the chart; return its documents, or the error."""
    result = subprocess.run(  # noqa: S603  # helm, with this script's own args
        [helm, "template", "distant-signal", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return [], result.stderr.strip()
    return [as_map(d) for d in yaml.safe_load_all(result.stdout) if d], ""


def pod_specs(docs: Sequence[Doc]) -> dict[str, Mapping[str, object]]:
    """Return every pod spec, keyed `<kind>/<name>`."""
    pods: dict[str, Mapping[str, object]] = {}
    for d in docs:
        kind = str(d.get("kind"))
        spec = as_map(d.get("spec"))
        if kind == "CronJob":
            template = as_map(
                as_map(as_map(spec.get("jobTemplate")).get("spec")).get("template")
            )
        elif kind in ("Deployment", "StatefulSet", "Job"):
            template = as_map(spec.get("template"))
        elif kind == "Pod":
            template = d
        else:
            continue
        name = as_map(d.get("metadata")).get("name")
        pods[f"{kind}/{name}"] = as_map(template.get("spec"))
    return pods


def render_problems(helm: str) -> list[str]:
    """Check the default renders, then the global and override ones."""
    failures: list[str] = []
    for label, flags in RENDERS:
        docs, error = render(helm, flags)
        if error:
            failures.append(f"{label}: render failed: {error}")
            continue
        pods = pod_specs(docs)
        failures.extend(
            f"{label}: {key} names {spec['priorityClassName']!r} with every value empty"
            for key, spec in pods.items()
            if "priorityClassName" in spec
        )
        print(f"{label}: {len(pods)} pod templates checked")
    for label, flags, want in (
        ("global only", (*EVERYTHING, *sets(f"global.priorityClassName={GLOBAL}")), {}),
        ("global and overrides", (*EVERYTHING, *OVERRIDES), WANT_OVERRIDDEN),
    ):
        docs, error = render(helm, flags)
        if error:
            failures.append(f"{label}: render failed: {error}")
            continue
        pods = pod_specs(docs)
        failures.extend(
            f"{label}: {key} did not render" for key in EXPECTED if key not in pods
        )
        for key, spec in pods.items():
            expected = None if key.startswith("Pod/") else want.get(key, GLOBAL)
            if spec.get("priorityClassName") != expected:
                failures.append(
                    f"{label}: {key} names {spec.get('priorityClassName')!r},"
                    f" not {expected!r}"
                )
        print(f"{label}: {len(pods)} pod templates checked")
    for label, flags in REFUSED:
        _, error = render(helm, (*BASE, *flags))
        if "PriorityClass" not in error and "system-*" not in error:
            failures.append(f"{label} must refuse to render (got {error!r})")
    return failures


def template_problems() -> list[str]:
    """Return the templates that place a pod but name no PriorityClass."""
    # templates/*.yaml only: the helm test pods (templates/tests/) are exempt.
    return [
        f"templates/{path.name} renders containers but no {HELPER}"
        for path in sorted((CHART / "templates").glob("*.yaml"))
        if "containers:" in (text := path.read_text()) and HELPER not in text
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    failures = [*render_problems(cast("str", args.helm)), *template_problems()]
    for failure in failures:
        print(failure)
    if not failures:
        print(
            "ok: no pod names a PriorityClass by default; the global value and"
            " each workload's own reach exactly their pods"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
