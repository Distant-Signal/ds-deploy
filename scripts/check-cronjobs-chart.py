#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check every CronJob charts/distant-signal renders against its safe defaults.

  uv run scripts/check-cronjobs-chart.py [--helm HELM]

Renders the chart with every CronJob on (api-maintenance, the pgBackRest
ones) and checks each CronJob (docs/scheduled-jobs.md):

  - `spec.timeZone` is set and names a zone in tzdata (without one the
    schedule follows the node's local zone: mine-bringer runs at UTC+2);
  - `concurrencyPolicy: Forbid` and a `startingDeadlineSeconds`;
  - its Job sets `backoffLimit`, `activeDeadlineSeconds` and
    `ttlSecondsAfterFinished`, and its pod `restartPolicy: Never`;
  - every container has CPU and memory requests and a memory limit (a pod
    without is BestEffort: the first the kubelet evicts, the first the
    host's earlyoom kills), and ephemeral-storage requests and limits where
    the CronJob runs one of this chart's own binaries;
  - a CronJob running this chart's own binaries also has
    `podReplacementPolicy: Failed` and the shared pod failure policy (exit
    137 and 3 fail the Job; a DisruptionTarget pod is ignored) for its
    container.

The pgBackRest CronJobs only `kubectl exec` into the Postgres pod, whose
exit codes are pgBackRest's, so they are held to everything but the last
two points.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
import zoneinfo
from collections.abc import Mapping, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"

# CronJobs that run something other than this chart's own binaries, by
# name prefix: held to the generic checks only.
FOREIGN = ("distant-signal-pgbackrest-",)
# Every CronJob the "everything on" render must contain, so a values rename
# can't silently shrink what this checks.
EXPECTED = (
    "distant-signal-api-maintenance",
    "distant-signal-pgbackrest-full",
    "distant-signal-pgbackrest-diff",
    "distant-signal-pgbackrest-check",
    "distant-signal-pgbackrest-verify",
)
FAIL_JOB_EXIT_CODES = [3, 137]


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


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
EVERYTHING = sets(
    "apiMaintenance.enabled=true",
    "postgresql.pgbackrest.enabled=true",
    "postgresql.pgbackrest.image.repository=registry.example.com/postgres-pgbackrest",
    "postgresql.pgbackrest.image.tag=pg18.6-pgbackrest2.59.1-tini0.19.0",
    "postgresql.pgbackrest.repo.path=/test/pgbackrest",
    "postgresql.pgbackrest.repo.s3.endpoint=s3.example.com",
    "postgresql.pgbackrest.repo.s3.bucket=test-bucket",
    "postgresql.pgbackrest.repo.s3.existingSecret=pgbackrest-creds",
)
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ()),
    ("values-example.yaml", ("-f", str(CHART / "values-example.yaml"))),
    ("every CronJob on", EVERYTHING),
)

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


def known_zone(name: str) -> bool:
    """Whether tzdata knows `name` (the API server rejects an unknown one)."""
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def resource_problems(container: Mapping[str, object], *, ephemeral: bool) -> list[str]:
    """Return what `container`'s resources lack."""
    resources = as_map(container.get("resources"))
    requests = as_map(resources.get("requests"))
    limits = as_map(resources.get("limits"))
    wanted = [("requests", requests, "cpu"), ("requests", requests, "memory")]
    wanted.append(("limits", limits, "memory"))
    if ephemeral:
        wanted += [
            ("requests", requests, "ephemeral-storage"),
            ("limits", limits, "ephemeral-storage"),
        ]
    name = container.get("name")
    return [
        f"container {name} has no resources.{kind}.{key}"
        for kind, values, key in wanted
        if key not in values
    ]


def failure_policy_problems(
    job_spec: Mapping[str, object], container: str
) -> list[str]:
    """Return how `job_spec` departs from the shared pod failure policy."""
    problems: list[str] = []
    if job_spec.get("podReplacementPolicy") != "Failed":
        problems.append("podReplacementPolicy is not Failed")
    rules = [
        as_map(r)
        for r in as_list(as_map(job_spec.get("podFailurePolicy")).get("rules"))
    ]
    fail_job = [
        as_map(r.get("onExitCodes")) for r in rules if r.get("action") == "FailJob"
    ]
    if not any(
        codes.get("containerName") == container
        and codes.get("operator") == "In"
        and sorted(cast("list[int]", as_list(codes.get("values"))))
        == FAIL_JOB_EXIT_CODES
        for codes in fail_job
    ):
        problems.append(
            f"podFailurePolicy has no FailJob on exit codes {FAIL_JOB_EXIT_CODES}"
            f" for container {container}"
        )
    disruption = [
        as_map(c).get("type")
        for r in rules
        if r.get("action") == "Ignore"
        for c in as_list(r.get("onPodConditions"))
    ]
    if "DisruptionTarget" not in disruption:
        problems.append("podFailurePolicy does not ignore DisruptionTarget pods")
    return problems


def cronjob_problems(doc: Mapping[str, object]) -> list[str]:
    """Return what is wrong with one rendered CronJob."""
    name = str(as_map(doc.get("metadata")).get("name"))
    own = not name.startswith(FOREIGN)
    spec = as_map(doc.get("spec"))
    job_spec = as_map(as_map(spec.get("jobTemplate")).get("spec"))
    pod_spec = as_map(as_map(job_spec.get("template")).get("spec"))
    problems: list[str] = []
    zone = spec.get("timeZone")
    if not isinstance(zone, str) or not zone:
        problems.append("has no spec.timeZone")
    elif not known_zone(zone):
        problems.append(f"timeZone {zone!r} is not a known zone")
    if spec.get("concurrencyPolicy") != "Forbid":
        problems.append(
            f"concurrencyPolicy is {spec.get('concurrencyPolicy')!r}, not Forbid"
        )
    if not isinstance(spec.get("startingDeadlineSeconds"), int):
        problems.append("has no startingDeadlineSeconds")
    problems.extend(
        f"its Job has no {key}"
        for key in ("backoffLimit", "activeDeadlineSeconds", "ttlSecondsAfterFinished")
        if not isinstance(job_spec.get(key), int)
    )
    if pod_spec.get("restartPolicy") != "Never":
        problems.append("its pod's restartPolicy is not Never")
    containers = [as_map(c) for c in as_list(pod_spec.get("containers"))]
    if not containers:
        problems.append("its pod has no containers")
    for container in containers:
        problems.extend(resource_problems(container, ephemeral=own))
    if own and containers:
        problems.extend(
            failure_policy_problems(job_spec, str(containers[0].get("name")))
        )
    return [f"CronJob {name}: {p}" for p in problems]


def render(helm: str, args: Sequence[str]) -> tuple[list[Doc], str]:
    """Render the chart with BASE and `args`; return its documents or the error."""
    result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
        [helm, "template", "distant-signal", str(CHART), *BASE, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [], result.stderr.strip()
    return [cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if d], ""


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    helm = cast("str", args.helm)
    failures: list[str] = []
    for label, flags in RENDERS:
        docs, error = render(helm, flags)
        if error:
            failures.append(f"{label}: render failed: {error}")
            continue
        cronjobs = [d for d in docs if d.get("kind") == "CronJob"]
        names = {str(as_map(d.get("metadata")).get("name")) for d in cronjobs}
        if label == "every CronJob on":
            failures.extend(
                f"{label}: CronJob {n} did not render"
                for n in EXPECTED
                if n not in names
            )
        failures.extend(f"{label}: {p}" for d in cronjobs for p in cronjob_problems(d))
        print(f"{label}: {len(cronjobs)} CronJobs checked")
    for failure in failures:
        print(failure)
    if not failures:
        print("ok: every CronJob has its time zone, resources and safe defaults")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
