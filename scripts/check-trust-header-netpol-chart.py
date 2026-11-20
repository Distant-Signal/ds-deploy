#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check that every client-IP header DS trusts is guarded by a NetworkPolicy.

  uv run scripts/check-trust-header-netpol-chart.py [--helm HELM]

A header a pod trusts is only as good as the set of callers that can reach
that pod: anything else that can connect picks its own rate-limit key (or its
own failed-authentication bucket). The trust surfaces of the chart:

  api X-Real-IP        the api keys anonymous clients on X-Real-IP while
                       API_RATE_LIMIT_TRUST_X_REAL_IP (api.rateLimit.trustXRealIp)
                       is on. Only the frontend pods (whose proxy sets it), the
                       helm-test probe and the networkPolicy.apiExtraIngressNamespaces
                       callers may reach the api's main port, and only those
                       callers its delegated port.
  api CF-Connecting-IP the personal-token listener keys failed authentication
                       on CF-Connecting-IP while
                       PERSONAL_TOKENS_TRUST_CF_CONNECTING_IP
                       (api.personalTokens.trustCfConnectingIp) is on. Only the
                       tunnel connector may reach that port.
  frontend             its proxy overwrites X-Real-IP from CF-Connecting-IP,
                       always. Only the tunnel connector, and the ingress
                       controller with ingress.frontend.enabled, may reach it.

Per surface, over the rendered manifests (`problems`), this checks:

  - a NetworkPolicy selects the pods, and the union of every policy that does
    admits, on each trusted port, EXACTLY the intended callers: no rule
    without `from` or `ports`, no ipBlock, no empty or namespace-wide
    selector where a narrower one is intended, no extra peer, no missing one;
  - no Service of type NodePort or LoadBalancer (or with externalIPs), no
    Ingress or route object, no hostNetwork and no hostPort reaches the pods
    around the NetworkPolicy (an Ingress to the frontend only when the
    intent has the ingress controller).

Each render below states its intent (Intent), so a change that widens a policy
fails even when the new peer looks harmless. Renders that must fail (the
policy off, widened, or a Service or Ingress exposing the pods) assert the
violation, or that the chart refuses to render. What this cannot see: values
that live outside this repository (the production values), and what a CNI or
the ingress controller does with a request.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast, override

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
RELEASE = "distant-signal"
COMPONENT = "app.kubernetes.io/component"
INSTANCE = "app.kubernetes.io/instance"
NAMESPACE_LABEL = "kubernetes.io/metadata.name"
POD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
EXPOSING_TYPES = ("NodePort", "LoadBalancer")
ROUTE_KIND = re.compile(r"Ingress|Route")

XREALIP_SURFACE = "api X-Real-IP trust"
TOKEN_SURFACE = "api CF-Connecting-IP trust (token listener)"  # noqa: S105  # a surface name
FRONTEND_SURFACE = "frontend CF-Connecting-IP trust"

type Doc = dict[str, object]
type Labels = Mapping[str, str]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


def name_of(doc: Mapping[str, object]) -> str:
    """Return `Kind/name` of a rendered resource."""
    return f"{doc.get('kind')}/{as_map(doc.get('metadata')).get('name')}"


def labels_of(template: Mapping[str, object]) -> dict[str, str]:
    """Return a pod template's (or Pod's) labels, as strings."""
    labels = as_map(as_map(template.get("metadata")).get("labels"))
    return {k: str(v) for k, v in labels.items()}


@dataclass(frozen=True)
class Pod:
    """One rendered pod template."""

    where: str
    labels: Labels
    spec: Mapping[str, object]

    @property
    def component(self) -> str:
        """The pod's component label."""
        return self.labels.get(COMPONENT, "")

    def container_ports(self) -> dict[str, int]:
        """Return each named container port's number (unnamed ones by number)."""
        ports: dict[str, int] = {}
        for container in map(as_map, as_list(self.spec.get("containers"))):
            for port in map(as_map, as_list(container.get("ports"))):
                number = int(str(port.get("containerPort")))
                ports[str(port.get("name") or number)] = number
        return ports

    def env(self, name: str) -> str | None:
        """Return the value of env var `name` on any container."""
        for container in map(as_map, as_list(self.spec.get("containers"))):
            for var in map(as_map, as_list(container.get("env"))):
                if var.get("name") == name:
                    return str(var.get("value"))
        return None


def pods(docs: Sequence[Mapping[str, object]]) -> Iterator[Pod]:
    """Yield every pod template (and bare Pod) of `docs`."""
    for doc in docs:
        kind = doc.get("kind")
        spec = as_map(doc.get("spec"))
        if kind == "Pod":
            yield Pod(name_of(doc), labels_of(doc), spec)
        elif kind in POD_KINDS:
            template = as_map(spec.get("template"))
            if template:
                yield Pod(
                    name_of(doc), labels_of(template), as_map(template.get("spec"))
                )
            job = as_map(
                as_map(as_map(spec.get("jobTemplate")).get("spec")).get("template")
            )
            if job:
                yield Pod(
                    f"{name_of(doc)} jobTemplate",
                    labels_of(job),
                    as_map(job.get("spec")),
                )


@dataclass(frozen=True)
class PodPeer:
    """A same-namespace podSelector peer: these components of this release."""

    components: frozenset[str]

    @override
    def __str__(self) -> str:
        """Describe the peer."""
        return f"pods of component {', '.join(sorted(self.components))}"


@dataclass(frozen=True)
class NsPeer:
    """A namespace named by `kubernetes.io/metadata.name`, narrowed to `pod_labels`."""

    namespace: str
    pod_labels: frozenset[tuple[str, str]] | None

    @override
    def __str__(self) -> str:
        """Describe the peer."""
        if self.pod_labels is None:
            return f"every pod in namespace {self.namespace}"
        labels = ", ".join(f"{k}={v}" for k, v in sorted(self.pod_labels))
        return f"pods labelled {labels} in namespace {self.namespace}"


type Peer = PodPeer | NsPeer


def ns_peer(namespace: str, pod_labels: Labels | None) -> NsPeer:
    """Return the peer for `namespace`, narrowed to `pod_labels` (None: all pods)."""
    return NsPeer(
        namespace, None if pod_labels is None else frozenset(pod_labels.items())
    )


@dataclass(frozen=True)
class Intent:
    """The callers a render is meant to admit (what a values file asks for).

    extra_api_namespaces: networkPolicy.apiExtraIngressNamespaces, each with
    its pod labels (None: the whole namespace). tunnel: the connector's
    namespace and pod labels (None: no tunnel). ingress_namespace: the
    ingress controller's namespace while ingress.frontend.enabled.
    """

    extra_api_namespaces: Mapping[str, Labels | None] = field(default_factory=dict)
    tunnel: tuple[str, Labels | None] | None = None
    ingress_namespace: str | None = None

    def extra_peers(self) -> set[Peer]:
        """Return the peers of the extra api callers."""
        return {ns_peer(n, labels) for n, labels in self.extra_api_namespaces.items()}

    def tunnel_peers(self) -> set[Peer]:
        """Return the tunnel connector's peer, if any."""
        return {ns_peer(*self.tunnel)} if self.tunnel else set()


def requirements(
    selector: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, set[str]], bool]:
    """Return a selector's matchLabels, `In` requirements and any others."""
    labels = {k: str(v) for k, v in as_map(selector.get("matchLabels")).items()}
    ins: dict[str, set[str]] = {}
    other = False
    for expr in map(as_map, as_list(selector.get("matchExpressions"))):
        if expr.get("operator") == "In":
            ins.setdefault(str(expr.get("key")), set()).update(
                str(v) for v in as_list(expr.get("values"))
            )
        else:
            other = True
    return labels, ins, other


def parse_namespace_peer(peer: Mapping[str, object]) -> Peer | str:
    """Return the peer of a namespaceSelector (and podSelector), or what is unsafe."""
    labels, ins, other = requirements(as_map(peer["namespaceSelector"]))
    if set(labels) != {NAMESPACE_LABEL} or ins or other:
        return (
            "a namespaceSelector that does not name one namespace"
            " (matches every or many namespaces)"
        )
    pod_labels, pod_ins, pod_other = requirements(as_map(peer.get("podSelector")))
    if pod_ins or pod_other:
        return "a podSelector with match expressions next to a namespaceSelector"
    return ns_peer(labels[NAMESPACE_LABEL], pod_labels or None)


def parse_pod_peer(peer: Mapping[str, object], release: str) -> Peer | str:
    """Return the peer of a same-namespace podSelector, or what is unsafe."""
    labels, ins, other = requirements(as_map(peer["podSelector"]))
    if other:
        return "a podSelector with a requirement other than In"
    instances = {labels[INSTANCE]} if INSTANCE in labels else ins.get(INSTANCE, set())
    if instances != {release}:
        return "a podSelector not pinned to this release (matches pods of any release)"
    components = (
        {labels[COMPONENT]} if COMPONENT in labels else ins.get(COMPONENT, set())
    )
    if not components:
        return "a podSelector naming no component (matches every pod of the release)"
    return PodPeer(frozenset(components))


def parse_peer(peer: Mapping[str, object], release: str) -> Peer | str:
    """Return the peer a rendered NetworkPolicyPeer is, or why it is not a safe one."""
    if "ipBlock" in peer:
        return f"an ipBlock ({as_map(peer['ipBlock']).get('cidr')})"
    if "namespaceSelector" in peer:
        return parse_namespace_peer(peer)
    if "podSelector" in peer:
        return parse_pod_peer(peer, release)
    return "an empty peer (matches every source)"


def selector_matches(selector: Mapping[str, object], labels: Labels) -> bool:
    """Whether a rendered label selector selects `labels` (an empty one selects all)."""
    for key, value in as_map(selector.get("matchLabels")).items():
        if labels.get(key) != str(value):
            return False
    for expr in map(as_map, as_list(selector.get("matchExpressions"))):
        key = str(expr.get("key"))
        values = {str(v) for v in as_list(expr.get("values"))}
        match expr.get("operator"):
            case "In":
                ok = labels.get(key) in values
            case "NotIn":
                ok = labels.get(key) not in values
            case "Exists":
                ok = key in labels
            case "DoesNotExist":
                ok = key not in labels
            case _:
                ok = False
        if not ok:
            return False
    return True


def ingress_policies(
    docs: Sequence[Mapping[str, object]], labels: Labels
) -> list[Mapping[str, object]]:
    """Return the specs of the NetworkPolicies restricting ingress to `labels`."""
    found: list[Mapping[str, object]] = []
    for doc in docs:
        if doc.get("kind") != "NetworkPolicy":
            continue
        spec = as_map(doc.get("spec"))
        types = as_list(spec.get("policyTypes"))
        if types and "Ingress" not in types:
            continue
        if selector_matches(as_map(spec.get("podSelector")), labels):
            found.append(spec)
    return found


def rule_covers(rule: Mapping[str, object], number: int, name: str) -> bool:
    """Whether an ingress rule's ports include TCP port `number` (named `name`)."""
    ports = [as_map(p) for p in as_list(rule.get("ports"))]
    if not ports:
        return True
    for port in ports:
        if str(port.get("protocol", "TCP")) != "TCP":
            continue
        value = port.get("port")
        if value is None:
            return True
        if isinstance(value, str) and not value.isdigit():
            if value == name:
                return True
            continue
        low = int(str(value))
        high = int(str(port.get("endPort", low)))
        if low <= number <= high:
            return True
    return False


def admitted(
    policies: Sequence[Mapping[str, object]], number: int, name: str, release: str
) -> tuple[set[Peer], list[str]]:
    """Return the peers admitted to a port, and each unsafe peer or rule on it."""
    peers: set[Peer] = set()
    unsafe: list[str] = []
    for spec in policies:
        for rule in map(as_map, as_list(spec.get("ingress"))):
            if not rule_covers(rule, number, name):
                continue
            sources = as_list(rule.get("from"))
            if not sources:
                unsafe.append("a rule with no `from` admits every source")
            if not as_list(rule.get("ports")):
                unsafe.append("a rule with no `ports` admits every port")
            for source in map(as_map, sources):
                parsed = parse_peer(source, release)
                if isinstance(parsed, str):
                    unsafe.append(f"admits {parsed}")
                else:
                    peers.add(parsed)
    return peers, list(dict.fromkeys(unsafe))


def compare(
    surface: str,
    port: str,
    peers: set[Peer],
    expected: set[Peer],
    unsafe: Sequence[str],
) -> list[str]:
    """Return the violations of one port: unsafe rules, extra peers, missing peers."""
    found = [f"{surface}: {port}: {u}" for u in unsafe]
    found.extend(
        f"{surface}: {port}: the NetworkPolicy also admits {p},"
        " which is not an intended caller"
        for p in sorted(peers - expected, key=str)
    )
    found.extend(
        f"{surface}: {port}: the NetworkPolicy does not admit {p}, an intended caller"
        for p in sorted(expected - peers, key=str)
    )
    return found


def selecting_services(
    docs: Sequence[Mapping[str, object]], labels: Labels
) -> list[Doc]:
    """Return the Services whose selector selects pods with `labels`."""
    found: list[Doc] = []
    for doc in docs:
        if doc.get("kind") != "Service":
            continue
        selector = as_map(as_map(doc.get("spec")).get("selector"))
        if selector and all(labels.get(k) == str(v) for k, v in selector.items()):
            found.append(cast("Doc", doc))
    return found


def strings_in(value: object) -> Iterator[str]:
    """Yield every string leaf of a parsed document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in cast("dict[str, object]", value).values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            yield from strings_in(item)


def exposure(
    docs: Sequence[Mapping[str, object]],
    surface: str,
    targets: Sequence[Pod],
    *,
    ingress_allowed: bool,
) -> list[str]:
    """Return how the pods are reachable around their NetworkPolicy."""
    found: list[str] = []
    for pod in targets:
        if pod.spec.get("hostNetwork") is True:
            found.append(
                f"{surface}: {pod.where} runs with hostNetwork,"
                " which bypasses its NetworkPolicy"
            )
        for container in map(as_map, as_list(pod.spec.get("containers"))):
            found.extend(
                f"{surface}: {pod.where} publishes container port"
                f" {port.get('containerPort')} as a hostPort"
                for port in map(as_map, as_list(container.get("ports")))
                if port.get("hostPort")
            )
    service_names: set[str] = set()
    for labels in {frozenset(t.labels.items()) for t in targets}:
        for service in selecting_services(docs, dict(labels)):
            service_names.add(str(as_map(service.get("metadata")).get("name")))
            spec = as_map(service.get("spec"))
            kind = str(spec.get("type", "ClusterIP"))
            if kind in EXPOSING_TYPES:
                found.append(
                    f"{surface}: {name_of(service)} is type {kind}, so it reaches the"
                    " pods from outside the cluster"
                )
            if as_list(spec.get("externalIPs")):
                found.append(f"{surface}: {name_of(service)} sets externalIPs")
    for doc in docs:
        kind = str(doc.get("kind"))
        if kind == "NetworkPolicy" or not ROUTE_KIND.search(kind) or ingress_allowed:
            continue
        routed = service_names & set(strings_in(doc.get("spec")))
        found.extend(
            f"{surface}: {name_of(doc)} routes to Service {s}, which selects the pods"
            for s in sorted(routed)
        )
    return found


def problems(
    docs: Sequence[Mapping[str, object]], intent: Intent, release: str = RELEASE
) -> list[str]:
    """Return every way a trusted client-IP header lacks its NetworkPolicy."""
    all_pods = list(pods(docs))
    api = [
        p
        for p in all_pods
        if p.component == "api" and p.where.startswith("Deployment/")
    ]
    frontend = [p for p in all_pods if p.component == "frontend"]
    found: list[str] = []
    if not api:
        found.append(f"{XREALIP_SURFACE}: no api Deployment rendered")
    if not frontend:
        found.append(f"{FRONTEND_SURFACE}: no frontend Deployment rendered")
    has_test_pod = any(p.component == "test" for p in all_pods)
    frontend_peers: set[Peer] = {
        PodPeer(frozenset({"frontend", "test"} if has_test_pod else {"frontend"}))
    }

    for pod in api[:1]:
        found.extend(_api_problems(docs, pod, intent, release, frontend_peers))
    for pod in frontend[:1]:
        found.extend(_frontend_problems(docs, pod, intent, release))
    return found


@dataclass(frozen=True)
class Listener:
    """A trusting listener of a pod: its surface, container port name and callers."""

    surface: str
    port_name: str
    expected: set[Peer]


def _port_problems(
    policies: Sequence[Mapping[str, object]],
    pod: Pod,
    listener: Listener,
    release: str,
) -> list[str]:
    """Return the violations of one named listener of `pod`."""
    ports = pod.container_ports()
    if listener.port_name not in ports:
        missing = f"{pod.where} has no container port named {listener.port_name}"
        return [f"{listener.surface}: {missing}"]
    number = ports[listener.port_name]
    peers, unsafe = admitted(policies, number, listener.port_name, release)
    return compare(
        listener.surface,
        f"{listener.port_name} port {number}",
        peers,
        listener.expected,
        unsafe,
    )


def _api_problems(
    docs: Sequence[Mapping[str, object]],
    pod: Pod,
    intent: Intent,
    release: str,
    callers: set[Peer],
) -> list[str]:
    """Return the violations of the api's two trust surfaces."""
    xrealip = (
        pod.env("API_RATE_LIMIT_ENABLED") == "true"
        and pod.env("API_RATE_LIMIT_TRUST_X_REAL_IP") == "true"
    )
    cf = pod.env("PERSONAL_TOKENS_TRUST_CF_CONNECTING_IP") == "true"
    surfaces = [s for s, on in ((XREALIP_SURFACE, xrealip), (TOKEN_SURFACE, cf)) if on]
    policies = ingress_policies(docs, pod.labels)
    listeners: list[Listener] = []
    found: list[str] = []
    if surfaces and not policies:
        found.extend(
            f"{s}: no NetworkPolicy restricts ingress to {pod.where}, so any pod"
            " can reach the trusting listener"
            for s in surfaces
        )
    elif xrealip:
        extra = intent.extra_peers()
        listeners.append(Listener(XREALIP_SURFACE, "http", callers | extra))
        if "delegated" in pod.container_ports():
            listeners.append(Listener(XREALIP_SURFACE, "delegated", extra))
    if cf and policies:
        if intent.tunnel:
            listeners.append(Listener(TOKEN_SURFACE, "tokens", intent.tunnel_peers()))
        else:
            found.append(
                f"{TOKEN_SURFACE}: the intent names no tunnel connector,"
                " so no caller may reach the token listener"
            )
    for listener in listeners:
        found.extend(_port_problems(policies, pod, listener, release))
    for surface in surfaces:
        found.extend(exposure(docs, surface, [pod], ingress_allowed=False))
    return found


def _frontend_problems(
    docs: Sequence[Mapping[str, object]], pod: Pod, intent: Intent, release: str
) -> list[str]:
    """Return the violations of the frontend's CF-Connecting-IP trust."""
    found: list[str] = []
    expected = intent.tunnel_peers()
    if intent.ingress_namespace:
        expected.add(ns_peer(intent.ingress_namespace, None))
    ports = pod.container_ports()
    policies = ingress_policies(docs, pod.labels)
    if not policies:
        found.append(
            f"{FRONTEND_SURFACE}: no NetworkPolicy restricts ingress to"
            f" {pod.where}, so any pod can reach it"
        )
    elif "http" not in ports:
        found.append(
            f"{FRONTEND_SURFACE}: {pod.where} has no container port named http"
        )
    else:
        peers, unsafe = admitted(policies, ports["http"], "http", release)
        found.extend(
            compare(FRONTEND_SURFACE, f"port {ports['http']}", peers, expected, unsafe)
        )
        extra_ports = sorted(p for n, p in ports.items() if n != "http")
        for number in extra_ports:
            other, other_unsafe = admitted(policies, number, str(number), release)
            if other or other_unsafe:
                found.append(
                    f"{FRONTEND_SURFACE}: port {number} of {pod.where} is reachable too"
                )
    found.extend(
        exposure(
            docs,
            FRONTEND_SURFACE,
            [pod],
            ingress_allowed=bool(intent.ingress_namespace),
        )
    )
    return found


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


def set_json(key: str, value: str) -> tuple[str, ...]:
    """Return the `--set-json` arguments for `key=value` (JSON)."""
    return ("--set-json", f"{key}={value}")


PRODUCTION = ("-f", str(CHART / "values-production-example.yaml"))
# The public site through a tunnel connector instead of the ingress controller.
TUNNEL = sets("ingress.enabled=false", "networkPolicy.tunnel.enabled=true")
TOKENS = sets(
    "networkPolicy.tunnel.tokenApi=true",
    "api.personalTokens.enabled=true",
    "api.personalTokens.publicBaseUrl=https://api.example.com",
    "api.personalTokens.trustCfConnectingIp=true",
)
DELEGATED = sets(
    "api.delegated.clients[0].name=ds-mcp",
    "api.delegated.clients[0].displayName=Assistant",
    "api.delegated.clients[0].issuerUrl=https://sso.example.com/application/o/api/",
    "api.delegated.clients[0].audience=distant-signal-api",
    "api.delegated.clients[0].maxScopes={journeys:read}",
    "networkPolicy.apiExtraIngressNamespaces={ds-mcp}",
)
INGRESS_API = sets("ingress.api.enabled=true", "ingress.api.host=api.example.com")
# The values the chart defaults to: networkPolicy.enabled is false.
OFF = sets("global.production=false", "networkPolicy.enabled=false")

CONNECTOR = {"app.kubernetes.io/name": "cloudflared"}
MCP = {
    "app.kubernetes.io/name": "distant-signal-mcp",
    "app.kubernetes.io/component": "mcp",
}
INTENT_INGRESS = Intent(ingress_namespace="ingress-nginx")
INTENT_TUNNEL = Intent(tunnel=("cloudflared", CONNECTOR))
INTENT_ALL = Intent(
    extra_api_namespaces={"ds-mcp": MCP}, tunnel=("cloudflared", CONNECTOR)
)


@dataclass(frozen=True)
class Case:
    """One render: its arguments, its intent and what must come of it.

    Exactly one of: `refusal` (helm template must fail with this text),
    `violation` (a violation containing this text is reported), or neither
    (no violation).
    """

    name: str
    args: tuple[str, ...]
    intent: Intent
    violation: str | None = None
    refusal: str | None = None


CASES: tuple[Case, ...] = (
    Case(
        "production example (ingress controller in front)", PRODUCTION, INTENT_INGRESS
    ),
    Case("production example behind the tunnel", (*PRODUCTION, *TUNNEL), INTENT_TUNNEL),
    Case(
        "tunnel, token listener and a delegated client",
        (*PRODUCTION, *TUNNEL, *TOKENS, *DELEGATED),
        INTENT_ALL,
    ),
    Case(
        "ingress controller, token listener and a delegated client",
        (*PRODUCTION, *TOKENS, *DELEGATED, *sets("networkPolicy.tunnel.enabled=true")),
        Intent(
            extra_api_namespaces={"ds-mcp": MCP},
            tunnel=("cloudflared", CONNECTOR),
            ingress_namespace="ingress-nginx",
        ),
    ),
    # The chart refuses these itself (_helpers.tpl's guards); the render must fail.
    Case(
        "policy off with global.production refuses to render",
        (*PRODUCTION, *sets("networkPolicy.enabled=false")),
        INTENT_INGRESS,
        refusal="networkPolicy.enabled is false",
    ),
    Case(
        "trustCfConnectingIp without the tunnel policy refuses to render",
        (*PRODUCTION, *TUNNEL, *TOKENS, *sets("networkPolicy.tunnel.tokenApi=false")),
        INTENT_TUNNEL,
        refusal="api.personalTokens.trustCfConnectingIp is true",
    ),
    Case(
        "tunnel.api with trustXRealIp refuses to render",
        (*PRODUCTION, *TUNNEL, *sets("networkPolicy.tunnel.api=true")),
        INTENT_TUNNEL,
        refusal="networkPolicy.tunnel.api is true while api.rateLimit.trustXRealIp",
    ),
    Case(
        "ingress.api with trustXRealIp refuses to render",
        (*PRODUCTION, *INGRESS_API),
        INTENT_INGRESS,
        refusal="ingress.api.enabled is true while api.rateLimit.trustXRealIp is true",
    ),
    # Renders the chart accepts but leaves a trusted header unguarded.
    Case(
        "policy off (the chart defaults) renders but guards nothing",
        (*PRODUCTION, *OFF),
        INTENT_INGRESS,
        violation="no NetworkPolicy restricts ingress",
    ),
    Case(
        "ingress.api acknowledged renders but exposes the api",
        (
            *PRODUCTION,
            *INGRESS_API,
            *sets("api.rateLimit.trustXRealIpIngressAcknowledged=true"),
        ),
        INTENT_INGRESS,
        violation="routes to Service distant-signal-api",
    ),
    Case(
        "ingress.api acknowledged also admits the ingress controller to the api",
        (
            *PRODUCTION,
            *INGRESS_API,
            *sets("api.rateLimit.trustXRealIpIngressAcknowledged=true"),
        ),
        INTENT_INGRESS,
        violation="also admits every pod in namespace ingress-nginx",
    ),
    Case(
        "a NodePort frontend Service",
        (*PRODUCTION, *sets("frontend.service.type=NodePort")),
        INTENT_INGRESS,
        violation="is type NodePort",
    ),
    Case(
        "a LoadBalancer api Service",
        (*PRODUCTION, *sets("api.service.type=LoadBalancer")),
        INTENT_INGRESS,
        violation="is type LoadBalancer",
    ),
    Case(
        "a frontend extraIngress rule from every pod",
        (
            *PRODUCTION,
            *set_json(
                "networkPolicy.components.frontend.extraIngress",
                '[{"from":[{"podSelector":{}}]}]',
            ),
        ),
        INTENT_INGRESS,
        violation="admits a podSelector not pinned to this release",
    ),
    Case(
        "an api extraIngress rule from every pod",
        (
            *PRODUCTION,
            *set_json(
                "networkPolicy.components.api.extraIngress",
                '[{"from":[{"podSelector":{}}],"ports":[{"port":8080}]}]',
            ),
        ),
        INTENT_INGRESS,
        violation="admits a podSelector not pinned to this release",
    ),
    Case(
        "an api extraIngress rule from an ipBlock",
        (
            *PRODUCTION,
            *set_json(
                "networkPolicy.components.api.extraIngress",
                '[{"from":[{"ipBlock":{"cidr":"0.0.0.0/0"}}],"ports":[{"port":8080}]}]',
            ),
        ),
        INTENT_INGRESS,
        violation="admits an ipBlock (0.0.0.0/0)",
    ),
    Case(
        "an api extraIngress rule with no ports",
        (
            *PRODUCTION,
            *set_json(
                "networkPolicy.components.api.extraIngress",
                '[{"from":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"other"}}}]}]',
            ),
        ),
        INTENT_INGRESS,
        violation="a rule with no `ports` admits every port",
    ),
    Case(
        "the whole ds-mcp namespace admitted to the api",
        (
            *PRODUCTION,
            *DELEGATED,
            *sets("networkPolicy.apiExtraIngressPodLabels.ds-mcp=null"),
        ),
        Intent(extra_api_namespaces={"ds-mcp": MCP}, ingress_namespace="ingress-nginx"),
        violation="also admits every pod in namespace ds-mcp",
    ),
    Case(
        "the whole tunnel namespace admitted to the frontend",
        (*PRODUCTION, *TUNNEL, *sets("networkPolicy.tunnel.podLabels=null")),
        INTENT_TUNNEL,
        violation="also admits every pod in namespace cloudflared",
    ),
    Case(
        "the whole tunnel namespace admitted to the token listener",
        (*PRODUCTION, *TUNNEL, *TOKENS, *sets("networkPolicy.tunnel.podLabels=null")),
        INTENT_TUNNEL,
        violation="tokens port 8083: the NetworkPolicy also admits every pod in"
        " namespace cloudflared",
    ),
)


def render(helm: str, *args: str) -> list[Doc]:
    """Render the chart with `args`; parse every document."""
    result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
        [helm, "template", RELEASE, str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)
    ]


def run_case(helm: str, case: Case) -> list[str]:
    """Return what is wrong with how `case` came out."""
    try:
        docs = render(helm, *case.args)
    except subprocess.CalledProcessError as e:
        stderr = str(e.stderr).strip()
        if case.refusal and case.refusal in stderr:
            return []
        return [f"{case.name}: render failed: {stderr}"]
    if case.refusal:
        return [f"{case.name}: rendered, but must refuse with {case.refusal!r}"]
    found = problems(docs, case.intent)
    if case.violation is None:
        return [f"{case.name}: {p}" for p in found]
    if any(case.violation in p for p in found):
        return []
    return [f"{case.name}: no violation containing {case.violation!r} (got {found})"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run every case; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    helm = cast("str", args.helm)
    failures: list[str] = []
    for case in CASES:
        failed = run_case(helm, case)
        failures.extend(failed)
        print(f"{'FAIL' if failed else 'ok'}: {case.name}")
    for failure in failures:
        print(failure)
    if not failures:
        print("ok: every trusted client-IP header is guarded by its NetworkPolicy")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
