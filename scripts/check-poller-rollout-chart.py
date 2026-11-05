#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check every poller Deployment rolls out without a gap.

  scripts/check-poller-rollout-chart.py [--helm HELM]

Renders charts/distant-signal with every poller on (the five in
`pollers`, the three island-of-Ireland ones and a transit network) and
checks each Deployment of components.yaml's `pollers` group:

  - it is Recreate, or a surge: RollingUpdate with maxSurge 1 and
    maxUnavailable 0, and a readiness probe on /healthz. A surge pod only
    becomes Ready after its first successful cycle, or its readiness
    fallback (ds-pollers' poller-lifecycle), so the old pod keeps polling
    until then. Without the probe it would be Ready at once and the surge
    would protect nothing; with maxUnavailable above 0 the old pod could
    stop first;
  - poller-transit-schedule is Recreate: two timetable producers must never
    XADD at once, and its binary has no first-cycle readiness;
  - the rollout each poller gets by default is the one in DEFAULTS, so a
    change of default is deliberate;
  - with workerHealth.pollerReadyFallbackSecs set, every surge Deployment
    carries it as POLLER_READY_FALLBACK_SECS and no Recreate one does
    (unset, the binary's own 480 s applies).

It also renders the overrides the chart must refuse (an unknown rollout, a
readiness fallback outside 1 to 540 s, a surge poller whose explicit
Postgres connection limit cannot hold both pods) and checks each fails
with its own message.

Why: ds-pollers docs/redeploy-safety.md. Under Recreate a new poller pod
that crashes or never completes a cycle leaves nothing polling, and for
incidents and TfL line status what is missed is never recovered.

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
COMPONENTS = CHART / "components.yaml"
PREREQS = CHART / "ci" / "ingest-prereqs.yaml"

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
)
# Every poller on, each with what its render requires.
ALL_POLLERS = (
    *("--set", "pollers.incidents.enabled=true"),
    *("--set", "pollers.incidents.baseUrl=https://rdm.example.com/incidents"),
    *("--set", "redis.acl.clients.pollerIncidents=true"),
    *("--set", "pollers.stations.enabled=true"),
    *("--set", "pollers.stations.baseUrl=https://rdm.example.com/stations"),
    *("--set", "pollers.tocs.enabled=true"),
    *("--set", "pollers.tocs.baseUrl=https://rdm.example.com/tocs"),
    *("--set", "redis.acl.clients.pollerTocs=true"),
    *("--set", "pollers.ldbws.enabled=true"),
    *("--set", "pollers.ldbws.baseUrl=https://rdm.example.com/ldbws"),
    *("--set", "redis.acl.clients.pollerLdbws=true"),
    *("--set", "pollers.tfl.enabled=true"),
    *("--set", "redis.acl.clients.pollerTfl=true"),
    *("--set", "pollerIrishRailGtfs.enabled=true"),
    *("--set", "redis.acl.clients.pollerIrishRailGtfs=true"),
    *("--set", "pollerIrishRailLive.enabled=true"),
    *("--set", "redis.acl.clients.pollerIrishRailLive=true"),
    *("--set", "pollerNirStations.enabled=true"),
    *("--set", "redis.acl.clients.pollerNirStations=true"),
    *("--set", "ingestWriter.streams.ioi-gtfs=apply"),
    *("--set", "ingestWriter.streams.ioi-live=apply"),
    *("--set", "ingestWriter.streams.ioi-nir=apply"),
    *("--set", "pollerTransitSchedule.networks.metrolink.enabled=true"),
    *("--set", "redis.acl.clients.pollerTransitMetrolink=true"),
    *("--set", "ingestWriter.streams.transit-metrolink=apply"),
)
COMPONENT = "app.kubernetes.io/component"
SURGE = "surge"
RECREATE = "recreate"
# Each poller Deployment's rollout at the chart's defaults, by component.
DEFAULTS = {
    "poller-incidents": SURGE,
    "poller-ldbws": SURGE,
    "poller-tfl": SURGE,
    "poller-irish-rail-live": SURGE,
    "poller-stations": RECREATE,
    "poller-tocs": RECREATE,
    "poller-irish-rail-gtfs": RECREATE,
    "poller-nir-stations": RECREATE,
    "poller-transit-schedule": RECREATE,
}
# Pollers whose binary has no first-cycle readiness: never a surge.
RECREATE_ONLY = frozenset({"poller-transit-schedule"})
# Overrides the render must refuse, each with a fragment of its message.
REFUSALS = (
    (("--set", "pollers.tfl.rollout=rolling"), "must be surge or recreate"),
    (
        ("--set", "workerHealth.pollerReadyFallbackSecs=0"),
        "pollerReadyFallbackSecs must be whole seconds from 1 to 540",
    ),
    (
        ("--set", "workerHealth.pollerReadyFallbackSecs=600"),
        "pollerReadyFallbackSecs must be whole seconds from 1 to 540",
    ),
    (
        ("--set", "postgresql.roles.perService.incidents.connectionLimit=3"),
        "pollers.incidents.rollout is surge",
    ),
    (
        ("--set", "postgresql.roles.perService.ldbws_ro.connectionLimit=1"),
        "pollers.ldbws.rollout is surge",
    ),
)

FALLBACK_ENV = "POLLER_READY_FALLBACK_SECS"
# A fallback to render with, to check where it lands.
FALLBACK_SECS = "300"

Doc = Mapping[str, object]


def render(helm: str, extra: Sequence[str] = ()) -> tuple[list[Doc], str]:
    """Return the rendered documents (none on failure) and stderr."""
    proc = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed args
        [helm, "template", "distant-signal", str(CHART), *ARGS, *ALL_POLLERS, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return [], proc.stderr or "render failed"
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


def poller_components(components: object) -> frozenset[str]:
    """Return the components components.yaml maps to the `pollers` group."""
    mapping = get(components, "components")
    if not isinstance(mapping, dict):
        return frozenset()
    return frozenset(str(c) for c, g in mapping.items() if g == "pollers")


def rollout_of(deployment: Doc) -> tuple[str, list[str]]:
    """Return the Deployment's rollout (surge or recreate) and its problems."""
    strategy = get(deployment, "spec", "strategy")
    kind = str(get(strategy, "type") or "RollingUpdate")
    if kind == "Recreate":
        return RECREATE, []
    problems = []
    surge = get(strategy, "rollingUpdate", "maxSurge")
    unavailable = get(strategy, "rollingUpdate", "maxUnavailable")
    if str(unavailable) != "0":
        problems.append(
            f"RollingUpdate with maxUnavailable {unavailable!r}, not 0: "
            "the old pod can stop before the new one has polled"
        )
    if str(surge) != "1":
        problems.append(f"RollingUpdate with maxSurge {surge!r}, not 1")
    containers = list(items(get(deployment, "spec", "template", "spec", "containers")))
    if not any(
        get(c, "readinessProbe", "httpGet", "path") == "/healthz" for c in containers
    ):
        problems.append(
            "no readiness probe on /healthz: the surge pod is Ready at once, "
            "so the old pod stops before the new one has polled"
        )
    return SURGE, problems


def fallback_of(deployment: Doc) -> str | None:
    """Return the Deployment's POLLER_READY_FALLBACK_SECS, None when unset."""
    for c in items(get(deployment, "spec", "template", "spec", "containers")):
        for e in items(c.get("env")):
            if e.get("name") == FALLBACK_ENV:
                return str(e.get("value"))
    return None


def check_fallback(docs: Sequence[Doc], pollers: frozenset[str]) -> list[str]:
    """With FALLBACK_SECS set: on every surge poller, and on no other."""
    fail = []
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        if str(get(d, "metadata", "labels", COMPONENT)) not in pollers:
            continue
        name = str(get(d, "metadata", "name"))
        rollout, _ = rollout_of(d)
        got = fallback_of(d)
        if rollout == SURGE and got != FALLBACK_SECS:
            fail.append(
                f"{name}: {FALLBACK_ENV} is {got!r} with "
                f"workerHealth.pollerReadyFallbackSecs={FALLBACK_SECS}"
            )
        elif rollout == RECREATE and got is not None:
            fail.append(f"{name}: sets {FALLBACK_ENV} but is Recreate")
    return fail


def check(docs: Sequence[Doc], pollers: frozenset[str]) -> list[str]:
    """Return one failure per poller Deployment that can leave a gap."""
    fail: list[str] = []
    seen: set[str] = set()
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        component = str(get(d, "metadata", "labels", COMPONENT))
        if component not in pollers:
            continue
        seen.add(component)
        name = str(get(d, "metadata", "name"))
        rollout, problems = rollout_of(d)
        fail.extend(f"{name}: {p}" for p in problems)
        if rollout == SURGE and component in RECREATE_ONLY:
            fail.append(f"{name}: must be Recreate (two producers must never XADD)")
        expected = DEFAULTS.get(component)
        if expected is None:
            fail.append(f"{name}: {component!r} is not in DEFAULTS; add its rollout")
        elif rollout != expected:
            fail.append(
                f"{name}: renders {rollout} by default, DEFAULTS says {expected}"
            )
    fail.extend(
        f"no Deployment rendered for poller component {c!r} (DEFAULTS is stale?)"
        for c in sorted(DEFAULTS.keys() - seen)
    )
    return fail


def check_refusals(helm: str) -> list[str]:
    """Return one failure per override the chart rendered instead of refusing."""
    fail = []
    for extra, message in REFUSALS:
        docs, err = render(helm, extra)
        if docs or not err:
            fail.append(f"{' '.join(extra)}: rendered, but the chart must refuse it")
        elif message not in err:
            fail.append(
                f"{' '.join(extra)}: refused without {message!r}: {err.strip()}"
            )
    return fail


def main() -> int:
    """Run the check; 1 when any failed."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args()
    helm = cast("str", args.helm)
    pollers = poller_components(yaml.safe_load(COMPONENTS.read_text()))
    docs, err = render(helm)
    fail = [f"render failed: {err.strip()}"] if err else check(docs, pollers)
    with_fallback, err = render(
        helm, ("--set", f"workerHealth.pollerReadyFallbackSecs={FALLBACK_SECS}")
    )
    if err:
        fail.append(f"render with a fallback failed: {err.strip()}")
    else:
        fail.extend(check_fallback(with_fallback, pollers))
    fail.extend(check_refusals(helm))
    for line in fail:
        print(f"FAIL: {line}")
    if not fail:
        print("ok: poller rollout strategies")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
