#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check every object the chart renders maps to a group in components.yaml.

  scripts/check-components-chart.py [--helm HELM]

charts/distant-signal/components.yaml maps each `app.kubernetes.io/component`
value to a group (ingest, schedule-feed, migrate, roles, ...): the one
source of truth for scripts/check-ingest-rollout.py and deploy-bringer's
render-diff classifier, which fails closed on anything it cannot map. So
here, over the renders in RENDERS (the chart's defaults, and every
workload, feature and CronJob mode switched on):

  - every rendered object has an `app.kubernetes.io/component` label, or
    is listed in `exempt` (by kind and name after the release fullname);
  - that label is a key of `components`, and its group is in `groups`;
  - every `components` key and `exempt` entry renders in some render
    (a stale entry hides a renamed component);
  - the file's shape is right: `groups` a map of names to descriptions,
    `components` a map of components to groups, `exempt` a list.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's `lint` dependency group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
COMPONENTS = CHART / "components.yaml"
PREREQS = CHART / "ci" / "ingest-prereqs.yaml"
RELEASE = "distant-signal"
LABEL = "app.kubernetes.io/component"

type Doc = Mapping[str, object]


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# What the chart requires of every render.
REQUIRED = (
    "-f",
    str(PREREQS),
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
# Every optional workload and object-adding feature on.
EVERYTHING = sets(
    "networkPolicy.enabled=true",
    "networkPolicy.egress.enabled=true",
    "metrics.enabled=true",
    "metrics.podMonitor.enabled=true",
    "metrics.prometheusRule.enabled=true",
    "ingress.enabled=true",
    "ingress.frontend.host=app.example.com",
    "ingress.api.host=api.example.com",
    "devAuthentik.enabled=true",
    "apiMaintenance.enabled=true",
    "migrate.job.enabled=true",
    "api.migrateOnStartup=false",
    "archive.enabled=true",
    "archive.s3.bucket=archive-bucket",
    "archive.s3.existingSecret=archive-creds",
    "archive.s3.lifecycleConfirmed=true",
    "archive.s3.prefix=cluster/distant-signal-archive",
    "postgresql.pgbackrest.enabled=true",
    "postgresql.pgbackrest.image.repository=registry.example.com/pgbackrest",
    "postgresql.pgbackrest.image.tag=test",
    "postgresql.pgbackrest.repo.path=/test/pgbackrest",
    "postgresql.pgbackrest.repo.s3.endpoint=s3.example.com",
    "postgresql.pgbackrest.repo.s3.bucket=test-bucket",
    "postgresql.pgbackrest.repo.s3.existingSecret=pgbackrest-creds",
    "enricher.serviceAccount.create=true",
    "scheduleFeed.enabled=true",
    "scheduleFeed.sftp.authMethod=password",
    "scheduleFeed.bucket.enabled=true",
    "scheduleFeed.bucket.name=example-ds-ingest",
    "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
    "scheduleFeed.serviceAccount.create=true",
    "ingestWriter.enabled=true",
    "darwinRelay.enabled=true",
    "redis.acl.clients.darwinRelay=true",
    "ingestWriter.streams.darwin=apply",
    "pollers.incidents.enabled=true",
    "pollers.incidents.baseUrl=https://incidents.example.com",
    "pollers.stations.enabled=true",
    "pollers.stations.baseUrl=https://stations.example.com",
    "pollers.ldbws.enabled=true",
    "pollers.ldbws.baseUrl=https://ldbws.example.com",
    "pollers.tocs.enabled=true",
    "pollers.tocs.baseUrl=https://tocs.example.com",
    "pollers.tfl.enabled=true",
    "pollers.tfl.stops.enabled=true",
    "ingestWriter.streams.tfl-stops=apply",
    "redis.acl.clients.pollerIncidents=true",
    "redis.acl.clients.pollerLdbws=true",
    "redis.acl.clients.pollerTfl=true",
    "redis.acl.clients.pollerTocs=true",
    "pollerIrishRailGtfs.enabled=true",
    "pollerIrishRailLive.enabled=true",
    "pollerNirStations.enabled=true",
    "redis.acl.clients.pollerIrishRailGtfs=true",
    "redis.acl.clients.pollerIrishRailLive=true",
    "redis.acl.clients.pollerNirStations=true",
    "ingestWriter.streams.ioi-gtfs=apply",
    "ingestWriter.streams.ioi-live=apply",
    "ingestWriter.streams.ioi-nir=apply",
    "pollerTransitSchedule.networks.metrolink.enabled=true",
    "redis.acl.clients.pollerTransitMetrolink=true",
    "ingestWriter.streams.transit-metrolink=apply",
)
# The workloads with a CronJob mode, as CronJobs.
CRONJOB_MODES = sets(
    "scheduleFeed.reference.mode=cronjob",
    "pollers.stations.mode=cronjob",
    "pollers.tocs.mode=cronjob",
    "pollers.tfl.stops.mode=cronjob",
)
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chart defaults", REQUIRED),
    ("everything", (*REQUIRED, *EVERYTHING)),
    ("everything, CronJob modes", (*REQUIRED, *EVERYTHING, *CRONJOB_MODES)),
)


@dataclass(frozen=True)
class Map:
    """components.yaml, parsed."""

    groups: Mapping[str, str]
    components: Mapping[str, str]
    # (kind, name after the release fullname) of each exempt object.
    exempt: frozenset[tuple[str, str]]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def parse_map(data: object) -> tuple[Map, list[str]]:
    """Parse components.yaml's content; the map and its shape problems."""
    top = as_map(data)
    fail = [
        f"components.yaml: unknown top-level key {key!r}"
        for key in sorted(top.keys() - {"groups", "components", "exempt"})
    ]
    groups = {str(k): str(v) for k, v in as_map(top.get("groups")).items()}
    if not groups:
        fail.append("components.yaml: `groups` is empty or not a map")
    components = {str(k): str(v) for k, v in as_map(top.get("components")).items()}
    if not components:
        fail.append("components.yaml: `components` is empty or not a map")
    fail.extend(
        f"components.yaml: component {c!r} maps to unknown group {g!r}"
        for c, g in sorted(components.items())
        if g not in groups
    )
    fail.extend(
        f"components.yaml: group {g!r} has no component"
        for g in sorted(groups.keys() - set(components.values()))
    )
    exempt: set[tuple[str, str]] = set()
    raw_exempt = top.get("exempt", [])
    if not isinstance(raw_exempt, list):
        fail.append("components.yaml: `exempt` is not a list")
        raw_exempt = []
    for entry in cast("list[object]", raw_exempt):
        item = as_map(entry)
        kind, name, reason = item.get("kind"), item.get("name"), item.get("reason")
        if not (isinstance(kind, str) and isinstance(name, str) and reason):
            fail.append(
                f"components.yaml: exempt entry {entry!r} needs kind, name, reason"
            )
            continue
        exempt.add((kind, name))
    return Map(groups, components, frozenset(exempt)), fail


def short_name(name: str) -> str:
    """Return an object's name after the release fullname."""
    return name.removeprefix(f"{RELEASE}-") if name != RELEASE else ""


def check(component_map: Map, renders: Mapping[str, Sequence[Doc]]) -> list[str]:
    """Return one failure per unmapped object and per stale map entry."""
    fail: list[str] = []
    used: set[str] = set()
    exempt_seen: set[tuple[str, str]] = set()
    for render_name, docs in renders.items():
        for doc in docs:
            kind = str(doc.get("kind"))
            metadata = as_map(doc.get("metadata"))
            name = str(metadata.get("name"))
            where = f"{kind}/{name} ({render_name})"
            key = (kind, short_name(name))
            if key in component_map.exempt:
                exempt_seen.add(key)
                continue
            component = as_map(metadata.get("labels")).get(LABEL)
            if not isinstance(component, str):
                fail.append(f"{where}: no {LABEL} label and not in `exempt`")
                continue
            used.add(component)
            if component not in component_map.components:
                fail.append(
                    f"{where}: component {component!r} is not in components.yaml"
                )
    fail.extend(
        f"components.yaml: component {c!r} renders in no render (stale?)"
        for c in sorted(component_map.components.keys() - used)
    )
    fail.extend(
        f"components.yaml: exempt {k}/{n} renders in no render (stale?)"
        for k, n in sorted(component_map.exempt - exempt_seen)
    )
    return fail


def render(helm: str, args: Iterable[str]) -> tuple[list[Doc], str]:
    """Return the rendered documents (none on failure) and stderr."""
    proc = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed args
        [helm, "template", RELEASE, str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return [], proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
    return cast("list[Doc]", docs), ""


def main() -> int:
    """Run the checks; 1 when any failed."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args()
    helm = cast("str", args.helm)
    component_map, fail = parse_map(yaml.safe_load(COMPONENTS.read_text()))
    renders: dict[str, list[Doc]] = {}
    for name, flags in RENDERS:
        docs, err = render(helm, flags)
        if err:
            fail.append(f"render {name!r} failed: {err.strip()}")
        renders[name] = docs
    if all(renders.values()):
        fail.extend(check(component_map, renders))
    for line in fail:
        print(f"FAIL: {line}")
    if not fail:
        count = sum(len(d) for d in renders.values())
        print(f"ok: {count} rendered objects map to a component group")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
