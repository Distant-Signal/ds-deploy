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

Workloads with a `mode` (deployment or cronjob; today `pollers.tocs`):

  - no component (its pod template's `app.kubernetes.io/component`) is
    both a Deployment and a CronJob, in any render;
  - each mode renders exactly one of the two, named `<fullname>-<component>`;
  - as a CronJob its container runs once (`RUN_ONCE=true`, the skip
    threshold) with `METRICS_ENABLED=false`, no probes and no ports, and
    keeps its component's pod labels (its NetworkPolicies select on them);
  - with alerts on, its DistantSignalPollerStale rule is gone and the
    scheduled-jobs rules name its CronJob;
  - a bad mode, or `cronjob` for a poller that can't run once yet, refuses
    to render.

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
    "distant-signal-poller-tocs",
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
    "pollers.tocs.enabled=true",
    "pollers.tocs.baseUrl=https://tocs.example.com",
    "redis.acl.clients.pollerTocs=true",
    "pollers.tocs.mode=cronjob",
)
# poller-tocs on, in either mode, with its alerts.
TOCS = sets(
    "pollers.tocs.enabled=true",
    "pollers.tocs.baseUrl=https://tocs.example.com",
    "redis.acl.clients.pollerTocs=true",
    "metrics.prometheusRule.enabled=true",
)
TOCS_NAME = "distant-signal-poller-tocs"
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


def component_of(doc: Mapping[str, object]) -> str | None:
    """Return the `app.kubernetes.io/component` of a Deployment's or CronJob's pods."""
    spec = as_map(doc.get("spec"))
    if doc.get("kind") == "CronJob":
        spec = as_map(as_map(spec.get("jobTemplate")).get("spec"))
    labels = as_map(as_map(as_map(spec.get("template")).get("metadata")).get("labels"))
    component = labels.get("app.kubernetes.io/component")
    return component if isinstance(component, str) else None


def exclusivity_problems(docs: Sequence[Doc]) -> list[str]:
    """Each component that is both a Deployment and a CronJob."""
    by_kind: dict[str, set[str]] = {"Deployment": set(), "CronJob": set()}
    for doc in docs:
        kind = doc.get("kind")
        component = component_of(doc)
        if isinstance(kind, str) and kind in by_kind and component:
            by_kind[kind].add(component)
    return [
        f"component {c} renders both a Deployment and a CronJob"
        for c in sorted(by_kind["Deployment"] & by_kind["CronJob"])
    ]


def env_of(container: Mapping[str, object]) -> dict[str, object]:
    """Return a container's plain `value` env entries, by name."""
    return {
        str(e.get("name")): e.get("value")
        for e in (as_map(x) for x in as_list(container.get("env")))
    }


def cron_worker_problems(doc: Mapping[str, object]) -> list[str]:
    """Return what is wrong with a scheduled worker's (tocs') CronJob pod."""
    job_spec = as_map(as_map(as_map(doc.get("spec")).get("jobTemplate")).get("spec"))
    pod_spec = as_map(as_map(job_spec.get("template")).get("spec"))
    containers = [as_map(c) for c in as_list(pod_spec.get("containers"))]
    if len(containers) != 1:
        return [f"expected one container, found {len(containers)}"]
    container = containers[0]
    problems: list[str] = []
    env = env_of(container)
    want = {
        "RUN_ONCE": "true",
        "RUN_ONCE_SKIP_IF_FRESHER_THAN_SECS": "43200",
        "METRICS_ENABLED": "false",
    }
    problems.extend(
        f"env {k} is {env.get(k)!r}, not {v!r}"
        for k, v in want.items()
        if env.get(k) != v
    )
    names = [str(as_map(e).get("name")) for e in as_list(container.get("env"))]
    problems.extend(
        f"env {n} appears {names.count(n)} times"
        for n in sorted(set(names))
        if names.count(n) > 1
    )
    problems.extend(
        f"container has {key}"
        for key in ("ports", "livenessProbe", "readinessProbe", "startupProbe")
        if key in container
    )
    if component_of(doc) != "poller-tocs":
        problems.append(f"pod component is {component_of(doc)!r}, not poller-tocs")
    return problems


def check_modes(helm: str) -> list[str]:
    """Check pollers.tocs' two modes, its alerts, and refused modes."""
    failures: list[str] = []
    for mode, kind in (("deployment", "Deployment"), ("cronjob", "CronJob")):
        label = f"tocs mode {mode}"
        docs, error = render(helm, (*TOCS, *sets(f"pollers.tocs.mode={mode}")))
        if error:
            failures.append(f"{label}: render failed: {error}")
            continue
        named = [
            str(d.get("kind"))
            for d in docs
            if d.get("kind") in ("Deployment", "CronJob")
            and as_map(d.get("metadata")).get("name") == TOCS_NAME
        ]
        if named != [kind]:
            failures.append(f"{label}: {TOCS_NAME} renders as {named}, not [{kind}]")
        failures.extend(f"{label}: {p}" for p in exclusivity_problems(docs))
        rules = yaml.safe_dump([d for d in docs if d.get("kind") == "PrometheusRule"])
        stale = 'cycle="tocs"' in rules
        if stale == (mode == "cronjob"):
            failures.append(
                f"{label}: its DistantSignalPollerStale rule must"
                f" {'not ' if mode == 'cronjob' else ''}render"
            )
        named_in_rules = f'owner_name=~"{TOCS_NAME}"' in rules
        if named_in_rules != (mode == "cronjob"):
            failures.append(
                f"{label}: the scheduled-jobs rules must"
                f" {'' if mode == 'cronjob' else 'not '}name {TOCS_NAME}"
            )
        if mode == "cronjob":
            failures.extend(
                f"{label}: {p}"
                for d in docs
                if d.get("kind") == "CronJob"
                and as_map(d.get("metadata")).get("name") == TOCS_NAME
                for p in cron_worker_problems(d)
            )
    refused = (
        ("an unknown mode", (*TOCS, *sets("pollers.tocs.mode=sometimes"))),
        (
            "cronjob for a poller that can't run once",
            sets(
                "pollers.stations.enabled=true",
                "pollers.stations.baseUrl=https://stations.example.com",
                "pollers.stations.mode=cronjob",
            ),
        ),
    )
    for label, flags in refused:
        _, error = render(helm, flags)
        if "mode" not in error:
            failures.append(f"{label} must refuse to render (got {error!r})")
    print(f"tocs modes: {len(failures)} problems")
    return failures


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
        failures.extend(f"{label}: {p}" for p in exclusivity_problems(docs))
        print(f"{label}: {len(cronjobs)} CronJobs checked")
    failures.extend(check_modes(helm))
    for failure in failures:
        print(failure)
    if not failures:
        print(
            "ok: every CronJob has its time zone, resources and safe defaults;"
            " each workload renders as exactly one of Deployment or CronJob"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
