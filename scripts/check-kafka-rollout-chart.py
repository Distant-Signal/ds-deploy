#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check no Kafka relay's rollout strategy can deadlock on its partitions.

  scripts/check-kafka-rollout-chart.py [--helm HELM]

Renders charts/distant-signal with every Kafka consumer on (movement-relay
by default, darwin-relay switched on) and checks each Deployment whose
container reads KAFKA_TOPIC:

  - it is in PARTITIONS, with its topic's partition count (a new Kafka
    Deployment must be added, so its strategy gets this check);
  - if it rolls out with maxUnavailable 0 (a RollingUpdate that keeps the
    old pods until the new ones are Ready), the pods a surge makes
    (replicas + maxSurge) are no more than the partitions.

Why: the relays' readiness is a confirmed partition assignment, and Kafka
gives each partition to one group member. With more members than
partitions a surge pod can be left with none, never become Ready, and
maxUnavailable 0 never stops an old pod: the rollout deadlocks. That
stalled build 833 (2026-10-31) on darwin-relay, whose Push Port topic has
one partition; it is Recreate now.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
import math
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
PREREQS = CHART / "ci" / "ingest-prereqs.yaml"

# Component -> its topic's partition count, as observed in production: the
# relay logs the count when it is the group's only member ("partitions": N
# on "partition(s) assigned").
PARTITIONS = {
    # The RDM Darwin Push Port JSON topic (darwin-relay, 2026-10-31).
    "darwin-relay": 1,
    # The RDM Train Movements (TRUST) topic (movement-relay, 2026-10-31).
    "movement-relay": 2,
}

ARGS = (
    "-f",
    str(PREREQS),
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
    "--set",
    "darwinRelay.enabled=true",
    "--set",
    "ingestWriter.streams.darwin=apply",
    "--set",
    "redis.acl.clients.darwinRelay=true",
    "--set",
    "postgresql.roles.perService.writer.connect=true",
)
COMPONENT = "app.kubernetes.io/component"
# Kubernetes' default Deployment strategy.
DEFAULT_ROLLING = {"maxSurge": "25%", "maxUnavailable": "25%"}

Doc = Mapping[str, object]


def render(helm: str) -> tuple[list[Doc], str]:
    """Return the rendered documents (none on failure) and stderr."""
    proc = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed args
        [helm, "template", "distant-signal", str(CHART), *ARGS],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return [], proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
    return cast("list[Doc]", docs), ""


def get(node: object, *path: str) -> object:
    """Walk dicts; None when a step is missing."""
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def items(node: object) -> Iterator[Mapping[str, object]]:
    """Yield the dict entries of a list (nothing for anything else)."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                yield cast("Mapping[str, object]", item)


def reads_kafka(deployment: Doc) -> bool:
    """Whether any container of the Deployment has a KAFKA_TOPIC env var."""
    containers = get(deployment, "spec", "template", "spec", "containers")
    return any(
        e.get("name") == "KAFKA_TOPIC"
        for c in items(containers)
        for e in items(c.get("env"))
    )


def scaled(value: object, replicas: int, *, round_up: bool) -> int:
    """Resolve an int-or-percentage as Kubernetes does.

    maxSurge rounds a percentage up, maxUnavailable down.
    """
    text = str(value)
    if text.endswith("%"):
        exact = replicas * int(text[:-1]) / 100
        return math.ceil(exact) if round_up else math.floor(exact)
    return int(text)


def deadlock(deployment: Doc, partitions: int) -> str | None:
    """Return why the Deployment's rollout can deadlock, or None if it cannot."""
    replicas = int(cast("int", get(deployment, "spec", "replicas") or 1))
    strategy = get(deployment, "spec", "strategy")
    kind = str(get(strategy, "type") or "RollingUpdate")
    if kind != "RollingUpdate":
        return None
    rolling = get(strategy, "rollingUpdate")
    rolling_map = cast(
        "Mapping[str, object]", rolling if isinstance(rolling, dict) else {}
    )
    surge = scaled(
        rolling_map.get("maxSurge", DEFAULT_ROLLING["maxSurge"]),
        replicas,
        round_up=True,
    )
    unavailable = scaled(
        rolling_map.get("maxUnavailable", DEFAULT_ROLLING["maxUnavailable"]),
        replicas,
        round_up=False,
    )
    if unavailable == 0 and replicas + surge > partitions:
        return (
            f"RollingUpdate with maxUnavailable 0 makes {replicas + surge} group "
            f"members for {partitions} partition(s): a surge pod can get none, "
            "never become Ready, and the rollout deadlocks (use Recreate)"
        )
    return None


def check(docs: Sequence[Doc]) -> list[str]:
    """Return one failure per Kafka Deployment that is unknown or can deadlock."""
    fail: list[str] = []
    seen: set[str] = set()
    for d in docs:
        if d.get("kind") != "Deployment" or not reads_kafka(d):
            continue
        name = str(get(d, "metadata", "name"))
        component = str(get(d, "metadata", "labels", COMPONENT))
        seen.add(component)
        partitions = PARTITIONS.get(component)
        if partitions is None:
            fail.append(
                f"{name}: reads Kafka but {component!r} is not in PARTITIONS; "
                "add its topic's partition count"
            )
            continue
        why = deadlock(d, partitions)
        if why:
            fail.append(f"{name}: {why}")
    fail.extend(
        f"no Kafka Deployment rendered for {c!r} (PARTITIONS is stale?)"
        for c in sorted(PARTITIONS.keys() - seen)
    )
    return fail


def main() -> int:
    """Run the check; 1 when any failed."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args()
    docs, err = render(cast("str", args.helm))
    fail = [f"render failed: {err.strip()}"] if err else check(docs)
    for line in fail:
        print(f"FAIL: {line}")
    if not fail:
        print("ok: Kafka relay rollout strategies")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
