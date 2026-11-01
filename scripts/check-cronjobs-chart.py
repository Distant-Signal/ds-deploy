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

Workloads with a mode (deployment or cronjob: `pollers.tocs.mode`,
`pollers.stations.mode`, and `timetableMode` of the tfl and metrolink
transit networks):

  - no loop of a workload (its pod template's `app.kubernetes.io/component`,
    with its transit network if any; a transit pod's loops are its
    `TRANSIT_LOOPS`) runs in two workloads, in any render;
  - each mode renders what it should under the workload's name: the
    Deployment, or the CronJob (and, for a transit network with its
    situations loop on, the Deployment again running `situations` alone,
    with its own small resources);
  - as a CronJob its container runs once (`RUN_ONCE=true`, the skip
    threshold; the transit timetable with `TRANSIT_LOOPS=timetable` and its
    /work volume) with `METRICS_ENABLED=false`, no probes and no ports, and
    keeps its component's pod labels (its NetworkPolicies select on them);
  - with alerts on, its DistantSignalPollerStale rule is gone and the
    scheduled-jobs rules name its CronJob;
  - a bad mode, `cronjob` for a workload that can't run once yet, or a
    transit network's timetable CronJob with its situations loop on but no
    `situationsResources`, refuses to render.

schedule-reference (`scheduleFeed.reference.mode`, CronJob phase 4) is a
container of the schedulefeed pod rather than a Deployment of its own, so
it has its own checks (`reference_mode_problems`): as a Deployment the
schedulefeed pod has its `reference` container, the in-pod alert rules
render and no CronJob does; as a CronJob the pod has no `reference`
container, the CronJob runs once with no metrics, ports or probes, mounts
the schedulefeed PVC read-only, keeps the schedule-reference component
label (Postgres admits it), and its alerts read the ingest-writer's
recorded gauges, with the scheduled-jobs rules naming it.

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
from dataclasses import dataclass, field
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
    "distant-signal-poller-stations",
    "distant-signal-poller-transit-schedule-tfl",
    "distant-signal-schedule-reference",
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
TRANSIT_TFL = sets(
    "pollerTransitSchedule.networks.tfl.enabled=true",
    "redis.acl.clients.pollerTransitTfl=true",
    "ingestWriter.streams.transit-tfl=apply",
)
TRANSIT_METROLINK = sets(
    "pollerTransitSchedule.networks.metrolink.enabled=true",
    "redis.acl.clients.pollerTransitMetrolink=true",
    "ingestWriter.streams.transit-metrolink=apply",
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
    "pollers.stations.enabled=true",
    "pollers.stations.baseUrl=https://stations.example.com",
    "pollers.stations.mode=cronjob",
    "pollerTransitSchedule.networks.tfl.timetableMode=cronjob",
    "pollerTransitSchedule.networks.metrolink.timetableMode=cronjob",
    "pollerTransitSchedule.networks.metrolink.situationsEnabled=true",
    "scheduleFeed.enabled=true",
    "scheduleFeed.sftp.authMethod=password",
    "scheduleFeed.reference.mode=cronjob",
)
EVERYTHING = (
    *EVERYTHING,
    *TRANSIT_TFL,
    # Metrolink's timetable CronJob beside its situations Deployment, and
    # tfl's CronJob: one component, three workloads, no loop twice.
    *TRANSIT_METROLINK,
)
ALERTS = sets("metrics.prometheusRule.enabled=true")


@dataclass(frozen=True)
class ModeWorkload:
    """A workload with a deployment/cronjob mode, and how to check it."""

    label: str
    name: str
    component: str
    enable: tuple[str, ...]
    mode_key: str
    # Text in the rendered rules only while it runs as a Deployment (its
    # DistantSignalPollerStale rule).
    stale_marker: str
    extra_env: Mapping[str, str] = field(default_factory=dict)
    network: str | None = None
    # What renders under its name in cron mode: the CronJob, plus the
    # situations-only Deployment for a transit network with situations on.
    cron_kinds: tuple[str, ...] = ("CronJob",)
    # Text in the rendered rules in both modes (the situations loop's
    # DistantSignalPollerStale rule, which its Deployment keeps).
    kept_marker: str | None = None


WORKLOADS = (
    ModeWorkload(
        "tocs",
        "distant-signal-poller-tocs",
        "poller-tocs",
        sets(
            "pollers.tocs.enabled=true",
            "pollers.tocs.baseUrl=https://tocs.example.com",
            "redis.acl.clients.pollerTocs=true",
        ),
        "pollers.tocs.mode",
        'cycle="tocs"',
    ),
    ModeWorkload(
        "stations",
        "distant-signal-poller-stations",
        "poller-stations",
        sets(
            "pollers.stations.enabled=true",
            "pollers.stations.baseUrl=https://stations.example.com",
        ),
        "pollers.stations.mode",
        'cycle="stations"',
    ),
    ModeWorkload(
        "transit tfl",
        "distant-signal-poller-transit-schedule-tfl",
        "poller-transit-schedule",
        TRANSIT_TFL,
        "pollerTransitSchedule.networks.tfl.timetableMode",
        'cycle="transit-timetable"',
        {"TRANSIT_LOOPS": "timetable"},
        "tfl",
    ),
    ModeWorkload(
        "transit metrolink, situations off",
        "distant-signal-poller-transit-schedule-metrolink",
        "poller-transit-schedule",
        TRANSIT_METROLINK,
        "pollerTransitSchedule.networks.metrolink.timetableMode",
        'cycle="transit-timetable"',
        {"TRANSIT_LOOPS": "timetable"},
        "metrolink",
    ),
    ModeWorkload(
        "transit metrolink, situations on",
        "distant-signal-poller-transit-schedule-metrolink",
        "poller-transit-schedule",
        (
            *TRANSIT_METROLINK,
            *sets("pollerTransitSchedule.networks.metrolink.situationsEnabled=true"),
        ),
        "pollerTransitSchedule.networks.metrolink.timetableMode",
        'cycle="transit-timetable"',
        {"TRANSIT_LOOPS": "timetable"},
        "metrolink",
        ("CronJob", "Deployment"),
        'cycle="transit-situations"',
    ),
)
# The situations-only Deployment's memory request (situationsResources).
SITUATIONS_MEMORY = "32Mi"
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


def pod_template_of(doc: Mapping[str, object]) -> dict[str, object]:
    """Return a Deployment's or CronJob's pod template."""
    spec = as_map(doc.get("spec"))
    if doc.get("kind") == "CronJob":
        spec = as_map(as_map(spec.get("jobTemplate")).get("spec"))
    return as_map(spec.get("template"))


def workload_of(doc: Mapping[str, object]) -> str | None:
    """Return a workload's identity: its pods' component, and transit network."""
    labels = as_map(as_map(pod_template_of(doc).get("metadata")).get("labels"))
    component = labels.get("app.kubernetes.io/component")
    if not isinstance(component, str):
        return None
    network = labels.get("distant-signal/transit-network")
    return f"{component}/{network}" if isinstance(network, str) else component


def loops_of(doc: Mapping[str, object]) -> tuple[str, ...]:
    """Return the loops a workload's pods run: a transit pod's TRANSIT_LOOPS."""
    containers = as_list(as_map(pod_template_of(doc).get("spec")).get("containers"))
    loops = {env_of(as_map(c)).get("TRANSIT_LOOPS") for c in containers} - {None}
    if not loops:
        return ("all",)
    if "both" in loops:
        return ("timetable", "situations")
    return tuple(sorted(str(loop) for loop in loops))


def exclusivity_problems(docs: Sequence[Doc]) -> list[str]:
    """Return each loop of a workload that runs in more than one workload."""
    runners: dict[tuple[str, str], list[str]] = {}
    for doc in docs:
        workload = workload_of(doc)
        name = str(as_map(doc.get("metadata")).get("name"))
        # The pgBackRest CronJobs are one component, four different jobs.
        if (
            doc.get("kind") not in ("Deployment", "CronJob")
            or not workload
            or name.startswith(FOREIGN)
        ):
            continue
        for loop in loops_of(doc):
            runners.setdefault((workload, loop), []).append(str(doc.get("kind")))
    return [
        f"workload {w} runs its {loop} loop in {len(kinds)} workloads ({kinds})"
        for (w, loop), kinds in sorted(runners.items())
        if len(kinds) > 1
    ]


def env_of(container: Mapping[str, object]) -> dict[str, object]:
    """Return a container's plain `value` env entries, by name."""
    return {
        str(e.get("name")): e.get("value")
        for e in (as_map(x) for x in as_list(container.get("env")))
    }


def cron_worker_problems(doc: Mapping[str, object], w: ModeWorkload) -> list[str]:
    """Return what is wrong with a scheduled worker's CronJob pod."""
    pod_spec = as_map(pod_template_of(doc).get("spec"))
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
        **w.extra_env,
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
    expected = f"{w.component}/{w.network}" if w.network else w.component
    if workload_of(doc) != expected:
        problems.append(f"pod labels say {workload_of(doc)!r}, not {expected}")
    if w.network:
        mounts = [
            as_map(m).get("mountPath") for m in as_list(container.get("volumeMounts"))
        ]
        volumes = [as_map(v) for v in as_list(pod_spec.get("volumes"))]
        if "/work" not in mounts or not any(
            as_map(v.get("emptyDir")).get("sizeLimit") for v in volumes
        ):
            problems.append("no size-limited /work emptyDir")
    return problems


def situations_deployment_problems(doc: Mapping[str, object]) -> list[str]:
    """Return what is wrong with a transit network's situations-only Deployment."""
    containers = [
        as_map(c)
        for c in as_list(as_map(pod_template_of(doc).get("spec")).get("containers"))
    ]
    if len(containers) != 1:
        return [
            f"situations Deployment: expected one container, found {len(containers)}"
        ]
    container = containers[0]
    env = env_of(container)
    problems: list[str] = []
    if env.get("TRANSIT_LOOPS") != "situations":
        problems.append(
            f"situations Deployment: TRANSIT_LOOPS is {env.get('TRANSIT_LOOPS')!r}"
        )
    if "RUN_ONCE" in env:
        problems.append("situations Deployment: must not run once")
    requests = as_map(as_map(container.get("resources")).get("requests"))
    if requests.get("memory") != SITUATIONS_MEMORY:
        problems.append(
            f"situations Deployment: memory request {requests.get('memory')!r},"
            f" not situationsResources' {SITUATIONS_MEMORY}"
        )
    if "livenessProbe" not in container:
        problems.append("situations Deployment: no liveness probe")
    return problems


def mode_problems(helm: str, w: ModeWorkload, mode: str) -> list[str]:
    """Return what is wrong with workload `w` rendered in `mode`."""
    kind = "CronJob" if mode == "cronjob" else "Deployment"
    docs, error = render(helm, (*w.enable, *ALERTS, *sets(f"{w.mode_key}={mode}")))
    if error:
        return [f"render failed: {error}"]
    problems: list[str] = []
    mine = [
        d
        for d in docs
        if d.get("kind") in ("Deployment", "CronJob")
        and as_map(d.get("metadata")).get("name") == w.name
    ]
    named = sorted(str(d.get("kind")) for d in mine)
    cron = mode == "cronjob"
    want = sorted(w.cron_kinds) if cron else [kind]
    if named != want:
        problems.append(f"{w.name} renders as {named}, not {want}")
    problems.extend(exclusivity_problems(docs))
    if cron:
        problems.extend(
            p
            for d in mine
            if d.get("kind") == "Deployment"
            for p in situations_deployment_problems(d)
        )
    rules = yaml.safe_dump([d for d in docs if d.get("kind") == "PrometheusRule"])
    if (w.stale_marker in rules) == cron:
        problems.append(
            f"its DistantSignalPollerStale rule must {'not ' if cron else ''}render"
        )
    if w.kept_marker and w.kept_marker not in rules:
        problems.append(f"its {w.kept_marker} DistantSignalPollerStale rule must stay")
    if (f'owner_name=~"{w.name}"' in rules) != cron:
        problems.append(
            f"the scheduled-jobs rules must {'' if cron else 'not '}name {w.name}"
        )
    if cron:
        problems.extend(
            p
            for d in docs
            if d.get("kind") == "CronJob"
            and as_map(d.get("metadata")).get("name") == w.name
            for p in cron_worker_problems(d, w)
        )
    return problems


SCHEDULE_FEED = sets(
    "scheduleFeed.enabled=true", "scheduleFeed.sftp.authMethod=password"
)
REFERENCE_CRONJOB = "distant-signal-schedule-reference"
REFERENCE_SCHEDULE = ("*/30 * * * *", "Europe/London")


def named(docs: Sequence[Doc], kind: str, name: str) -> list[Doc]:
    """Return the documents of `kind` named `name`."""
    return [
        d
        for d in docs
        if d.get("kind") == kind and as_map(d.get("metadata")).get("name") == name
    ]


def reference_cron_problems(doc: Mapping[str, object]) -> list[str]:
    """Return what is wrong with schedule-reference's CronJob."""
    spec = as_map(doc.get("spec"))
    problems: list[str] = []
    if (spec.get("schedule"), spec.get("timeZone")) != REFERENCE_SCHEDULE:
        problems.append(
            f"schedule {spec.get('schedule')!r} in {spec.get('timeZone')!r},"
            f" not {REFERENCE_SCHEDULE}"
        )
    pod_spec = as_map(pod_template_of(doc).get("spec"))
    containers = [as_map(c) for c in as_list(pod_spec.get("containers"))]
    if len(containers) != 1:
        return [*problems, f"expected one container, found {len(containers)}"]
    container = containers[0]
    env = env_of(container)
    want = {"RUN_ONCE": "true", "METRICS_ENABLED": "false"}
    problems.extend(
        f"env {k} is {env.get(k)!r}, not {v!r}"
        for k, v in want.items()
        if env.get(k) != v
    )
    if "DATABASE_URL" not in env:
        problems.append("no DATABASE_URL")
    problems.extend(
        f"container has {key}"
        for key in ("ports", "livenessProbe", "readinessProbe", "startupProbe")
        if key in container
    )
    if workload_of(doc) != "schedule-reference":
        problems.append(f"pod labels say {workload_of(doc)!r}, not schedule-reference")
    mounts = [as_map(m) for m in as_list(container.get("volumeMounts"))]
    if not any(
        m.get("mountPath") == "/data/schedule-feed" and m.get("readOnly") is True
        for m in mounts
    ):
        problems.append("the schedulefeed PVC is not mounted read-only")
    claims = [
        as_map(as_map(v).get("persistentVolumeClaim"))
        for v in as_list(pod_spec.get("volumes"))
    ]
    if not any(
        c.get("claimName") == "distant-signal-schedulefeed"
        and c.get("readOnly") is True
        for c in claims
    ):
        problems.append("the schedulefeed PVC volume is not readOnly")
    return problems


def reference_mode_problems(helm: str, mode: str) -> list[str]:
    """Return what is wrong with schedule-reference rendered in `mode`."""
    flags = (
        *SCHEDULE_FEED,
        *ALERTS,
        *sets("networkPolicy.enabled=true", f"scheduleFeed.reference.mode={mode}"),
    )
    docs, error = render(helm, flags)
    if error:
        return [f"render failed: {error}"]
    cron = mode == "cronjob"
    problems: list[str] = []
    feed = named(docs, "Deployment", "distant-signal-schedulefeed")
    containers = [
        str(as_map(c).get("name"))
        for d in feed
        for c in as_list(as_map(pod_template_of(d).get("spec")).get("containers"))
    ]
    if ("reference" in containers) == cron:
        problems.append(
            f"the schedulefeed pod must {'not ' if cron else ''}run `reference`"
            f" (containers {containers})"
        )
    cronjobs = named(docs, "CronJob", REFERENCE_CRONJOB)
    if len(cronjobs) != int(cron):
        problems.append(
            f"{len(cronjobs)} {REFERENCE_CRONJOB} CronJobs, not {int(cron)}"
        )
    problems.extend(p for d in cronjobs for p in reference_cron_problems(d))
    rules = yaml.safe_dump([d for d in docs if d.get("kind") == "PrometheusRule"])
    in_pod = "distant_signal_schedule_reference_seeded" in rules
    recorded = (
        "distant_signal_schedule_reference_recorded_signal_timestamp_seconds" in rules
    )
    if in_pod == cron or recorded != cron:
        problems.append(
            f"the alerts must read the {'recorded' if cron else 'in-pod'} gauges"
            f" (in-pod {in_pod}, recorded {recorded})"
        )
    if (f'owner_name=~"{REFERENCE_CRONJOB}"' in rules) != cron:
        problems.append(
            f"the scheduled-jobs rules must {'' if cron else 'not '}"
            f"name {REFERENCE_CRONJOB}"
        )
    postgres = yaml.safe_dump(named(docs, "NetworkPolicy", "distant-signal-postgres"))
    if ("- schedule-reference" in postgres) != cron:
        problems.append(
            f"the postgres NetworkPolicy must {'' if cron else 'not '}"
            "admit schedule-reference"
        )
    return problems


REFUSED = (
    ("an unknown mode", (*WORKLOADS[0].enable, *sets("pollers.tocs.mode=sometimes"))),
    (
        "an unknown schedule-reference mode",
        (*SCHEDULE_FEED, *sets("scheduleFeed.reference.mode=sometimes")),
    ),
    (
        "cronjob for a poller that can't run once (tfl)",
        sets(
            "pollers.tfl.enabled=true",
            "redis.acl.clients.pollerTfl=true",
            "ingestWriter.streams.tfl=apply",
            "pollers.tfl.mode=cronjob",
        ),
    ),
    (
        "a timetable CronJob for a network that can't run one (net)",
        sets(
            "pollerTransitSchedule.networks.net.enabled=true",
            "redis.acl.clients.pollerTransitNet=true",
            "ingestWriter.streams.transit-net=apply",
            "pollerTransitSchedule.networks.net.txcCredentials.existingSecret=tnds",
            "pollerTransitSchedule.networks.net.timetableMode=cronjob",
        ),
    ),
    (
        "a timetable CronJob with situations on but no situationsResources (tfl)",
        (
            *TRANSIT_TFL,
            *sets(
                "pollerTransitSchedule.networks.tfl.timetableMode=cronjob",
                "pollerTransitSchedule.networks.tfl.situationsEnabled=true",
            ),
        ),
    ),
)


def check_modes(helm: str) -> list[str]:
    """Check each mode workload's two modes and alerts, and refused values."""
    failures: list[str] = []
    for w in WORKLOADS:
        for mode in ("deployment", "cronjob"):
            failures.extend(
                f"{w.label} mode {mode}: {p}" for p in mode_problems(helm, w, mode)
            )
    for mode in ("deployment", "cronjob"):
        failures.extend(
            f"schedule-reference mode {mode}: {p}"
            for p in reference_mode_problems(helm, mode)
        )
    for label, flags in REFUSED:
        _, error = render(helm, flags)
        if "cronjob" not in error and "deployment or cronjob" not in error:
            failures.append(f"{label} must refuse to render (got {error!r})")
    print(f"modes: {len(WORKLOADS) + 1} workloads, {len(failures)} problems")
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
            " each loop of a workload runs in exactly one Deployment or CronJob"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
