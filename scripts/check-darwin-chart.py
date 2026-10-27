#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Render charts/distant-signal with darwinRelay on and off and check it (DS-152).

  scripts/check-darwin-chart.py [--helm HELM]

Checks, on ci/ingest-prereqs.yaml plus the Kafka/LLM/SSO dummies every
render needs:

  - off (the default): no darwin-relay Deployment, no `darwin-relay` Redis
    ACL user, `darwin:off` in the writer's INGEST_WRITER_STREAMS and no
    Darwin alert;
  - on (with its prerequisites): the Deployment reads every Kafka value
    from the Secret `distant-signal-darwin-pushport` under exactly the keys
    Ranma deployed (username, password, consumer-group,
    kafka-bootstrap-server, sasl-mechanism, security-protocol, sasl-config,
    kafka-topic), sasl-config optional and nothing inline; it connects to
    Redis as `darwin-relay`; the ACL user can only XADD/XREVRANGE
    ds:ingest:darwin; the writer applies `darwin`; the PodMonitor scrapes it;
    its NetworkPolicy allows Redis and the broker port; and the
    PrometheusRule has the staleness, consumer-lag and freshness alerts;
  - the guards: on without `ingestWriter.streams.darwin: apply`, without
    `redis.acl.clients.darwinRelay`, with a batch shorter than the stream's
    budget, or `darwin: apply` without the writer's own role, fails.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import argparse
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
SECRET = "distant-signal-darwin-pushport"  # noqa: S105  # the Secret's name
# env var -> the Secret's key, as deployed (Ranma deploy tu99).
KAFKA_KEYS = {
    "KAFKA_BROKERS": "kafka-bootstrap-server",
    "KAFKA_TOPIC": "kafka-topic",
    "KAFKA_CONSUMER_GROUP": "consumer-group",
    "KAFKA_SASL_USERNAME": "username",
    "KAFKA_SASL_PASSWORD": "password",
    "KAFKA_SASL_MECHANISM": "sasl-mechanism",
    "KAFKA_SECURITY_PROTOCOL": "security-protocol",
    "KAFKA_SASL_JAAS_CONFIG": "sasl-config",
}
BASE = (
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
    "metrics.enabled=true",
    "--set",
    "metrics.podMonitor.enabled=true",
    "--set",
    "metrics.prometheusRule.enabled=true",
    "--set",
    "networkPolicy.enabled=true",
    "--set",
    "networkPolicy.egress.enabled=true",
    "--set",
    "networkPolicy.egress.internetPorts={443}",
)
WRITER_ROLE = ("--set", "postgresql.roles.perService.writer.connect=true")
ON = (
    "--set",
    "darwinRelay.enabled=true",
    "--set",
    "ingestWriter.streams.darwin=apply",
    "--set",
    "redis.acl.clients.darwinRelay=true",
    *WRITER_ROLE,
)
ALERTS = {"DistantSignalDarwinPushPortStale", "DistantSignalDarwinConsumerLagHigh"}

Doc = Mapping[str, object]


def render(helm: str, *args: str) -> tuple[list[Doc], str]:
    """Return the rendered documents (none on failure) and stderr."""
    proc = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed args
        [helm, "template", "distant-signal", str(CHART), *BASE, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return [], proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
    return cast("list[Doc]", docs), ""


def get(node: object, *path: str | int) -> object:
    """Walk dicts and lists; None when a step is missing."""
    for step in path:
        if isinstance(step, int) and isinstance(node, list) and step < len(node):
            node = node[step]
        elif isinstance(step, str) and isinstance(node, dict):
            node = node.get(step)
        else:
            return None
    return node


def items(node: object) -> Iterator[Mapping[str, object]]:
    """Yield the dict entries of a list (nothing for anything else)."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                yield cast("Mapping[str, object]", item)


def of_kind(docs: Sequence[Doc], kind: str, name_part: str) -> list[Doc]:
    """Return the documents of `kind` whose metadata.name contains `name_part`."""
    return [
        d
        for d in docs
        if d.get("kind") == kind and name_part in str(get(d, "metadata", "name"))
    ]


def container_env(deployment: Doc, container: str) -> dict[str, Mapping[str, object]]:
    """Return the named container's env entries by name."""
    for c in items(get(deployment, "spec", "template", "spec", "containers")):
        if c.get("name") == container:
            return {str(e.get("name")): e for e in items(c.get("env"))}
    return {}


def alert_names(docs: Sequence[Doc]) -> set[str]:
    """Return every alert in every PrometheusRule."""
    names: set[str] = set()
    for rule in (d for d in docs if d.get("kind") == "PrometheusRule"):
        for group in items(get(rule, "spec", "groups")):
            names.update(
                str(r["alert"]) for r in items(group.get("rules")) if "alert" in r
            )
    return names


def acl_text(docs: Sequence[Doc]) -> str:
    """Return the rendered Redis ACL template(s), joined."""
    return "\n".join(
        str(v)
        for d in docs
        if d.get("kind") == "ConfigMap"
        for v in cast("Mapping[str, object]", d.get("data") or {}).values()
    )


def writer_streams(docs: Sequence[Doc]) -> str:
    """Return the ingest-writer's INGEST_WRITER_STREAMS."""
    writers = of_kind(docs, "Deployment", "ingest-writer")
    env = container_env(writers[0], "ingest-writer") if writers else {}
    return str(get(env, "INGEST_WRITER_STREAMS", "value"))


def check_off(helm: str, fail: list[str]) -> None:
    """Check the default render has nothing of darwin-relay."""
    docs, err = render(helm)
    if err:
        fail.append(f"default render failed: {err.strip()}")
        return
    if of_kind(docs, "Deployment", "darwin-relay"):
        fail.append("off: a darwin-relay Deployment rendered")
    if "user darwin-relay" in acl_text(docs):
        fail.append("off: the darwin-relay ACL user rendered (it is optional)")
    if ALERTS & alert_names(docs):
        fail.append("off: a Darwin alert rendered")
    streams = writer_streams(docs)
    if "darwin:off" not in streams:
        fail.append(f"off: INGEST_WRITER_STREAMS lacks darwin:off ({streams})")


def check_kafka_env(env: Mapping[str, Mapping[str, object]], fail: list[str]) -> None:
    """Check every Kafka value comes from the Push Port Secret's own key."""
    for var, key in KAFKA_KEYS.items():
        ref = get(env.get(var), "valueFrom", "secretKeyRef")
        if not isinstance(ref, dict):
            fail.append(f"on: {var} is not from a Secret")
            continue
        if ref.get("name") != SECRET or ref.get("key") != key:
            got = f"{ref.get('name')}/{ref.get('key')}"
            fail.append(f"on: {var} reads {got}, not {SECRET}/{key}")
        if (ref.get("optional") is True) != (var == "KAFKA_SASL_JAAS_CONFIG"):
            fail.append(f"on: {var} optional={ref.get('optional')}")
        if "value" in (env.get(var) or {}):
            fail.append(f"on: {var} has an inline value")
    if get(env.get("REDIS_USERNAME"), "value") != "darwin-relay":
        fail.append("on: darwin-relay does not connect to Redis as user darwin-relay")
    if get(env.get("KAFKA_AUTO_OFFSET_RESET"), "value") != "latest":
        fail.append("on: KAFKA_AUTO_OFFSET_RESET is not latest")


def check_wiring(docs: Sequence[Doc], fail: list[str]) -> None:
    """Check the ACL user, the writer, the PodMonitor, the NetworkPolicy, the alerts."""
    acl_line = next(
        (line for line in acl_text(docs).splitlines() if "user darwin-relay " in line),
        "",
    )
    if not acl_line.endswith(
        "~ds:ingest:darwin +xadd +xrevrange +ping +hello +auth +client|setname "
        "+client|setinfo +client|id"
    ):
        fail.append(f"on: unexpected darwin-relay ACL rights: {acl_line!r}")
    streams = writer_streams(docs)
    if "darwin:apply" not in streams:
        fail.append(f"on: the writer does not apply darwin ({streams})")
    monitors = [d for d in docs if d.get("kind") == "PodMonitor"]
    if not any("darwin-relay" in yaml.safe_dump(dict(m)) for m in monitors):
        fail.append("on: the PodMonitor does not select darwin-relay")
    policies = of_kind(docs, "NetworkPolicy", "darwin-relay")
    if len(policies) != 1:
        fail.append(f"on: expected one darwin-relay NetworkPolicy, not {len(policies)}")
    else:
        egress = yaml.safe_dump(get(policies[0], "spec", "egress"))
        if "9092" not in egress:
            fail.append("on: the NetworkPolicy does not open the broker port 9092")
        if "redis" not in egress:
            fail.append("on: the NetworkPolicy does not allow Redis")
    missing = ALERTS - alert_names(docs)
    if missing:
        fail.append(f"on: missing alerts {sorted(missing)}")
    rules = yaml.safe_dump([dict(d) for d in docs if d.get("kind") == "PrometheusRule"])
    if 'source="darwin-pushport"' not in rules:
        fail.append("on: no DistantSignalIngestSourceStale for darwin-pushport")


def check_on(helm: str, fail: list[str]) -> None:
    """Check the render with darwin-relay on and its prerequisites met."""
    docs, err = render(helm, *ON)
    if err:
        fail.append(f"enabled render failed: {err.strip()}")
        return
    deployments = of_kind(docs, "Deployment", "darwin-relay")
    if len(deployments) != 1:
        fail.append(f"on: expected one darwin-relay Deployment, not {len(deployments)}")
        return
    check_kafka_env(container_env(deployments[0], "darwin-relay"), fail)
    check_wiring(docs, fail)


def check_guards(helm: str, fail: list[str]) -> None:
    """Check each missing prerequisite fails the render."""
    cases: dict[str, tuple[str, ...]] = {
        "no streams.darwin apply": (
            "--set",
            "darwinRelay.enabled=true",
            "--set",
            "redis.acl.clients.darwinRelay=true",
        ),
        "no Redis user": (
            "--set",
            "darwinRelay.enabled=true",
            "--set",
            "ingestWriter.streams.darwin=apply",
            *WRITER_ROLE,
        ),
        "a batch shorter than the budget": (*ON, "--set", "darwinRelay.batchMaxSecs=5"),
        "darwin apply without the writer's role": (
            "--set",
            "ingestWriter.streams.darwin=apply",
            "--set",
            "postgresql.roles.perService.writer.connect=false",
        ),
    }
    for what, args in cases.items():
        docs, _ = render(helm, *args)
        if docs:
            fail.append(f"guard: rendered with {what}")


def main() -> int:
    """Run every check; 1 when any failed."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args()
    helm = cast("str", args.helm)
    fail: list[str] = []
    check_off(helm, fail)
    check_on(helm, fail)
    check_guards(helm, fail)
    for line in fail:
        print(f"FAIL: {line}")
    if not fail:
        print("ok: darwin-relay chart checks")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
