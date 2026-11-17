#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's egress policies scope their DNS rule (IAC-44).

  uv run scripts/check-dns-netpol-chart.py [--helm HELM]

With `networkPolicy.egress.enabled`, every egress policy the chart renders
(`distant-signal.egressRules`) allows UDP and TCP 53. A rule with ports and
no `to` allows them to every address, the node's resolver and private
ranges included. For values-example.yaml, values-production-example.yaml
and values-minimal.yaml, each with egress on, this checks:

  - no egress rule on port 53 lacks a `to`, or has an empty or ipBlock-only
    `to` that reaches every address (0.0.0.0/0 or ::/0);
  - by default every such rule has exactly one peer, the pods labelled
    `k8s-app: kube-dns` in `kube-system` (one peer, both selectors, so they
    are ANDed), on UDP and TCP;
  - `networkPolicy.egress.dns.namespace` and `.podLabels` replace them, and
    `networkPolicy.egress.dnsPeers` replaces the peer wholesale;
  - at least one policy is checked in each render, so a values rename can't
    silently empty the check.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
EGRESS_ON = (
    "--set",
    "networkPolicy.enabled=true",
    "--set",
    "networkPolicy.egress.enabled=true",
)
VALUES = (
    "values-example.yaml",
    "values-production-example.yaml",
    "values-minimal.yaml",
)
DEFAULT_PEER: dict[str, object] = {
    "namespaceSelector": {
        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
    },
    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
}
CUSTOM_PEER: dict[str, object] = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dns-system"}},
    "podSelector": {"matchLabels": {"app": "resolver"}},
}
NODE_LOCAL: dict[str, object] = {"ipBlock": {"cidr": "169.254.20.10/32"}}
ANY_CIDRS = ("0.0.0.0/0", "::/0")

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


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


def dns_rules(docs: Sequence[Doc]) -> list[tuple[str, dict[str, object]]]:
    """Return (policy name, rule) for every egress rule that names port 53."""
    found: list[tuple[str, dict[str, object]]] = []
    for doc in docs:
        if doc.get("kind") != "NetworkPolicy":
            continue
        name = str(as_map(doc.get("metadata")).get("name"))
        for raw in as_list(as_map(doc.get("spec")).get("egress")):
            rule = as_map(raw)
            ports = [as_map(p) for p in as_list(rule.get("ports"))]
            if any(str(p.get("port")) == "53" for p in ports):
                found.append((name, rule))
    return found


def reaches_everything(peer: object) -> bool:
    """Report whether one `to` peer is an ipBlock for every address."""
    block = as_map(as_map(peer).get("ipBlock"))
    return str(block.get("cidr")) in ANY_CIDRS


def rule_problems(
    label: str, docs: Sequence[Doc], peer: dict[str, object]
) -> list[str]:
    """Check every port-53 rule is scoped, and (when `peer`) is exactly it."""
    rules = dns_rules(docs)
    failures: list[str] = []
    if not rules:
        failures.append(f"{label}: no egress policy with a port 53 rule rendered")
    for name, rule in rules:
        to = as_list(rule.get("to"))
        if not to:
            failures.append(f"{label}: {name} allows port 53 to any address (no `to`)")
        elif any(reaches_everything(p) for p in to):
            failures.append(f"{label}: {name} allows port 53 to 0.0.0.0/0 or ::/0")
        elif to != [peer]:
            failures.append(f"{label}: {name} DNS peers are {to!r}, want {[peer]!r}")
        protocols = sorted(
            str(as_map(p).get("protocol")) for p in as_list(rule.get("ports"))
        )
        if protocols != ["TCP", "UDP"]:
            failures.append(
                f"{label}: {name} DNS rule ports are {protocols}, want TCP and UDP"
            )
    print(f"{label}: {len(rules)} DNS rules checked")
    return failures


def checks(helm: str) -> list[str]:
    """Run every render below; return the failures."""
    failures: list[str] = []
    for values in VALUES:
        base = ("-f", str(CHART / values), *EGRESS_ON)
        for label, extra, peer in (
            ("defaults", (), DEFAULT_PEER),
            (
                "dns.namespace and dns.podLabels",
                (
                    "--set",
                    "networkPolicy.egress.dns.namespace=dns-system",
                    "--set",
                    "networkPolicy.egress.dns.podLabels.app=resolver",
                    "--set",
                    "networkPolicy.egress.dns.podLabels.k8s-app=null",
                ),
                CUSTOM_PEER,
            ),
            (
                "dnsPeers",
                (
                    "--set",
                    "networkPolicy.egress.dnsPeers[0].ipBlock.cidr=169.254.20.10/32",
                ),
                NODE_LOCAL,
            ),
        ):
            name = f"{values} {label}"
            docs, error = render(helm, (*base, *extra))
            if error:
                failures.append(f"{name}: render failed: {error}")
                continue
            failures.extend(rule_problems(name, docs, peer))
    return failures


def main() -> int:
    """Run the checks."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    failures = checks(parser.parse_args().helm)
    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
