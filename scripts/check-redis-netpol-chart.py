#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check that charts/distant-signal's Redis NetworkPolicy admits its clients.

  uv run scripts/check-redis-netpol-chart.py [--helm HELM]

The Redis NetworkPolicy (templates/networkpolicy.yaml) lists, by
`app.kubernetes.io/component`, EXACTLY the workloads whose container env
includes REDIS_URL. A client missing from that list can't connect at all
with `networkPolicy.enabled`: darwin-relay (DS-152) shipped without its
entry, and every XADD to ds:ingest:darwin was refused (ECONNREFUSED).

For each render below (networkPolicy on, the ingest prerequisites of
ci/ingest-prereqs.yaml, and nothing else, movement-relay off, or every
Redis client on), this checks:

  - forward: every rendered pod template (Deployments, StatefulSets,
    DaemonSets, Jobs, CronJobs' jobTemplate) whose containers or init
    containers set REDIS_URL is selected by a podSelector peer of the
    Redis policy's ingress rules;
  - reverse: every component the policy lists names a rendered pod
    template that sets REDIS_URL (no stale or over-broad entries), and
    `networkPolicy.redisClients` peers are not counted for either;
  - egress (with networkPolicy.egress on): every such pod template that a
    policy restricts egress for may reach the Redis pod on the Redis port.

The "every client on" render must also render each workload in
EXPECTED_CLIENTS, so a values rename can't silently shrink what this checks.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
PREREQS = CHART / "ci" / "ingest-prereqs.yaml"
RELEASE = "distant-signal"
COMPONENT = "app.kubernetes.io/component"
INSTANCE = "app.kubernetes.io/instance"
REDIS_ENV = "REDIS_URL"
POD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")

type Doc = dict[str, object]


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# networkPolicy on, and the values the chart requires of every render (the
# kafka/LLM/SSO settings only satisfy its required-value checks).
BASE = (
    "-f",
    str(PREREQS),
    *sets(
        "networkPolicy.enabled=true",
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
# Every Redis client on, each with its own Redis user and (for a stream
# producer) its stream applied, as scripts/check-alert-payloads.py does.
# Add a new Redis client here and to EXPECTED_CLIENTS.
EVERYTHING = sets(
    "postgresql.roles.perService.writer.connect=true",
    "pollers.incidents.enabled=true",
    "pollers.incidents.baseUrl=https://incidents.example.com",
    "pollers.stations.enabled=true",
    "pollers.stations.baseUrl=https://stations.example.com",
    "pollers.ldbws.enabled=true",
    "pollers.ldbws.baseUrl=https://ldbws.example.com",
    "pollers.ldbws.trackedStops.enabled=true",
    "pollers.ldbws.trackedStopArrivals.enabled=true",
    "pollers.ldbws.trackedStopArrivals.secretName=ldbws-arrivals",
    "pollers.tfl.enabled=true",
    "pollers.tfl.stops.enabled=true",
    "ingestWriter.streams.tfl-stops=apply",
    "pollers.tocs.enabled=true",
    "pollers.tocs.baseUrl=https://tocs.example.com",
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
    # Nottingham NET, a txc network the values file doesn't define.
    "pollerTransitSchedule.networks.net.enabled=true",
    "pollerTransitSchedule.networks.net.adapter=txc",
    "pollerTransitSchedule.networks.net.txcUrl=https://txc.example.com/net.zip",
    "redis.acl.clients.pollerTransitNet=true",
    "ingestWriter.streams.transit-net=apply",
    "pollerTransitSchedule.networks.tfl.enabled=true",
    "redis.acl.clients.pollerTransitTfl=true",
    "ingestWriter.streams.transit-tfl=apply",
    "darwinRelay.enabled=true",
    "redis.acl.clients.darwinRelay=true",
    "ingestWriter.streams.darwin=apply",
    "scheduleFeed.enabled=true",
    "scheduleFeed.sftp.authMethod=password",
    "migrate.job.enabled=true",
    "apiMaintenance.enabled=true",
)
EGRESS = sets(
    "networkPolicy.egress.enabled=true", "networkPolicy.egress.internetPorts={443}"
)
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ingest prerequisites", ()),
    # Each optional client the prerequisites turn on, turned off again.
    ("movement-relay off", sets("movementRelay.enabled=false")),
    ("every Redis client on", EVERYTHING),
    ("every Redis client on, egress on", (*EVERYTHING, *EGRESS)),
)
# Components EVERYTHING must render with REDIS_URL.
EXPECTED_CLIENTS = frozenset(
    {
        "darwin-relay",
        "enricher",
        "full-coverage-consumer",
        "ingest-writer",
        "movement-relay",
        "poller-incidents",
        "poller-irish-rail-gtfs",
        "poller-irish-rail-live",
        "poller-ldbws",
        "poller-nir-stations",
        "poller-tfl",
        "poller-tocs",
        "poller-transit-schedule",
        "trust-backlog-consumer",
        "trust-consumer",
    }
)


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


def name_of(doc: Mapping[str, object]) -> str:
    """Return `Kind/name` of a rendered resource."""
    return f"{doc.get('kind')}/{as_map(doc.get('metadata')).get('name')}"


def pod_templates(doc: Mapping[str, object]) -> Iterator[tuple[str, Doc]]:
    """Yield (where, pod template) for each pod template in `doc`."""
    if doc.get("kind") not in POD_KINDS:
        return
    spec = as_map(doc.get("spec"))
    if "template" in spec:
        yield name_of(doc), as_map(spec["template"])
    job_spec = as_map(as_map(spec.get("jobTemplate")).get("spec"))
    if "template" in job_spec:
        yield f"{name_of(doc)} jobTemplate", as_map(job_spec["template"])


def labels_of(template: Mapping[str, object]) -> dict[str, str]:
    """Return a pod template's labels, as strings."""
    labels = as_map(as_map(template.get("metadata")).get("labels"))
    return {k: str(v) for k, v in labels.items()}


def uses_redis(template: Mapping[str, object]) -> bool:
    """Whether any container or init container of `template` sets REDIS_URL."""
    spec = as_map(template.get("spec"))
    containers = [
        *as_list(spec.get("containers")),
        *as_list(spec.get("initContainers")),
    ]
    return any(
        as_map(env).get("name") == REDIS_ENV
        for c in containers
        for env in as_list(as_map(c).get("env"))
    )


@dataclass(frozen=True)
class Requirement:
    """One matchExpressions entry of a label selector."""

    key: str
    operator: str
    values: tuple[str, ...]

    def matches(self, labels: Mapping[str, str]) -> bool:
        """Whether `labels` satisfy this requirement."""
        match self.operator:
            case "In":
                return labels.get(self.key) in self.values
            case "NotIn":
                return labels.get(self.key) not in self.values
            case "Exists":
                return self.key in labels
            case "DoesNotExist":
                return self.key not in labels
            case _:
                msg = f"unknown selector operator {self.operator!r}"
                raise ValueError(msg)


@dataclass(frozen=True)
class Selector:
    """A Kubernetes label selector (matchLabels and matchExpressions)."""

    match_labels: Mapping[str, str]
    expressions: tuple[Requirement, ...]

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> "Selector":
        """Parse a rendered podSelector."""
        return cls(
            {k: str(v) for k, v in as_map(raw.get("matchLabels")).items()},
            tuple(
                Requirement(
                    str(e.get("key")),
                    str(e.get("operator")),
                    tuple(str(v) for v in as_list(e.get("values"))),
                )
                for e in map(as_map, as_list(raw.get("matchExpressions")))
            ),
        )

    def matches(self, labels: Mapping[str, str]) -> bool:
        """Whether `labels` are selected."""
        return all(labels.get(k) == v for k, v in self.match_labels.items()) and all(
            r.matches(labels) for r in self.expressions
        )

    def components(self) -> frozenset[str]:
        """Return the components this selector names (matchLabels or an In list)."""
        named = (
            {self.match_labels[COMPONENT]} if COMPONENT in self.match_labels else set()
        )
        for r in self.expressions:
            if r.key == COMPONENT and r.operator == "In":
                named.update(r.values)
        return frozenset(named)


def redis_policy(docs: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """Return the NetworkPolicy whose podSelector is the chart's Redis pod."""
    for doc in docs:
        if doc.get("kind") != "NetworkPolicy":
            continue
        selector = as_map(as_map(doc.get("spec")).get("podSelector"))
        if as_map(selector.get("matchLabels")).get(COMPONENT) == "redis":
            return doc
    return None


def chart_selectors(policy: Mapping[str, object], release: str) -> list[Selector]:
    """Return the policy's same-namespace podSelector peers for this release.

    A peer with a namespaceSelector, or one not pinned to this release's
    instance label (`networkPolicy.redisClients`, outside the chart), is
    skipped: neither direction of the check is about those.
    """
    selectors: list[Selector] = []
    for rule in map(as_map, as_list(as_map(policy.get("spec")).get("ingress"))):
        for peer in map(as_map, as_list(rule.get("from"))):
            if "podSelector" not in peer or "namespaceSelector" in peer:
                continue
            selector = Selector.parse(as_map(peer["podSelector"]))
            pinned = selector.match_labels.get(INSTANCE) == release or any(
                r.key == INSTANCE and r.operator == "In" and release in r.values
                for r in selector.expressions
            )
            if pinned:
                selectors.append(selector)
    return selectors


def problems(docs: Sequence[Mapping[str, object]], release: str = RELEASE) -> list[str]:
    """Return every mismatch between the Redis policy and REDIS_URL workloads."""
    policy = redis_policy(docs)
    if policy is None:
        return ["no Redis NetworkPolicy rendered"]
    selectors = chart_selectors(policy, release)
    clients: dict[str, dict[str, str]] = {
        where: labels_of(template)
        for doc in docs
        for where, template in pod_templates(doc)
        if uses_redis(template)
    }
    found = [
        f"{where} (component {labels.get(COMPONENT)!r}) sets {REDIS_ENV} but the"
        " Redis NetworkPolicy does not admit it"
        for where, labels in sorted(clients.items())
        if not any(s.matches(labels) for s in selectors)
    ]
    rendered = {labels.get(COMPONENT) for labels in clients.values()}
    listed = frozenset().union(*(s.components() for s in selectors))
    found.extend(
        f"the Redis NetworkPolicy admits component {c!r}, but no rendered"
        f" workload of that component sets {REDIS_ENV}"
        for c in sorted(listed - rendered)
    )
    return found


def redis_target(docs: Sequence[Mapping[str, object]]) -> tuple[dict[str, str], int]:
    """Return the Redis pod's labels and the Redis Service's port."""
    labels: dict[str, str] = {}
    port = 0
    for doc in docs:
        for _, template in pod_templates(doc):
            if labels_of(template).get(COMPONENT) == "redis":
                labels = labels_of(template)
        spec = as_map(doc.get("spec"))
        if (
            doc.get("kind") == "Service"
            and as_map(spec.get("selector")).get(COMPONENT) == "redis"
        ):
            port = int(str(as_map(as_list(spec.get("ports"))[0]).get("port")))
    return labels, port


def reaches(rule: Mapping[str, object], labels: Mapping[str, str], port: int) -> bool:
    """Whether egress `rule` allows a same-namespace pod with `labels` on `port`."""
    ports = [as_map(p) for p in as_list(rule.get("ports"))]
    if ports and not any(str(p.get("port")) == str(port) for p in ports):
        return False
    peers = [as_map(p) for p in as_list(rule.get("to"))]
    return not peers or any(
        "podSelector" in peer
        and "namespaceSelector" not in peer
        and Selector.parse(as_map(peer["podSelector"])).matches(labels)
        for peer in peers
    )


def egress_problems(docs: Sequence[Mapping[str, object]]) -> list[str]:
    """Return each REDIS_URL pod template whose egress policies block Redis."""
    redis_labels, port = redis_target(docs)
    if not redis_labels or not port:
        return ["no Redis Deployment or Service rendered"]
    policies = [
        as_map(d.get("spec"))
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and "Egress" in as_list(as_map(d.get("spec")).get("policyTypes"))
    ]
    found: list[str] = []
    for doc in docs:
        for where, template in pod_templates(doc):
            if not uses_redis(template):
                continue
            labels = labels_of(template)
            mine = [
                p
                for p in policies
                if Selector.parse(as_map(p.get("podSelector"))).matches(labels)
            ]
            if mine and not any(
                reaches(as_map(rule), redis_labels, port)
                for p in mine
                for rule in as_list(p.get("egress"))
            ):
                found.append(
                    f"{where} sets {REDIS_ENV} but its egress NetworkPolicy"
                    f" does not allow the Redis pod on port {port}"
                )
    return found


def redis_components(docs: Sequence[Mapping[str, object]]) -> frozenset[str]:
    """Return the components of every rendered pod template with REDIS_URL."""
    return frozenset(
        labels_of(template).get(COMPONENT, "")
        for doc in docs
        for _, template in pod_templates(doc)
        if uses_redis(template)
    )


def render(helm: str, *args: str) -> list[Doc]:
    """Render the chart with BASE and `args`; parse every document."""
    result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
        [helm, "template", RELEASE, str(CHART), *BASE, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run every render's checks; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    helm = cast("str", args.helm)
    failures: list[str] = []
    for label, extra in RENDERS:
        try:
            docs = render(helm, *extra)
        except subprocess.CalledProcessError as e:
            failures.append(f"{label}: render failed: {str(e.stderr).strip()}")
            continue
        failures.extend(f"{label}: {p}" for p in problems(docs))
        failures.extend(f"{label}: {p}" for p in egress_problems(docs))
        components = redis_components(docs)
        if extra[: len(EVERYTHING)] == EVERYTHING:
            failures.extend(
                f"{label}: no {c} workload with {REDIS_ENV} rendered"
                for c in sorted(EXPECTED_CLIENTS - components)
            )
        print(f"{label}: {len(components)} Redis client components checked")
    for failure in failures:
        print(failure)
    if not failures:
        print("ok: the Redis NetworkPolicy admits exactly the REDIS_URL workloads")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
