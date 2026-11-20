"""Tests for scripts/check-trust-header-netpol-chart.py, on synthetic rendered docs.

The good state is a render with every trust flag on: the api behind a policy
for the frontend, the helm-test probe and ds-mcp, the token listener behind
the tunnel, the frontend behind the tunnel. Each mutation widens or removes
one guard and the checker must name the surface and the cause.

uv run python -m unittest discover -s scripts/tests
"""

import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPT = Path(__file__).resolve().parent.parent / "check-trust-header-netpol-chart.py"
RELEASE = "distant-signal"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_trust_header_netpol_chart", SCRIPT
    )
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_trust_header_netpol_chart"] = module
    spec.loader.exec_module(module)
    return module


check = _load()

type Doc = dict[str, Any]

CONNECTOR = {"app.kubernetes.io/name": "cloudflared"}
MCP = {
    "app.kubernetes.io/name": "distant-signal-mcp",
    "app.kubernetes.io/component": "mcp",
}
INTENT = check.Intent(
    extra_api_namespaces={"ds-mcp": MCP}, tunnel=("cloudflared", CONNECTOR)
)
INTENT_INGRESS = check.Intent(ingress_namespace="ingress-nginx")


def labels(component: str) -> dict[str, str]:
    """Return the chart's selector labels for `component`."""
    return {
        "app.kubernetes.io/name": "distant-signal",
        "app.kubernetes.io/instance": RELEASE,
        "app.kubernetes.io/component": component,
    }


def deployment(
    component: str, ports: dict[str, int], env: dict[str, str] | None = None
) -> Doc:
    """Return a Deployment of `component` with named container ports and env."""
    container = {
        "name": component,
        "ports": [{"name": n, "containerPort": p} for n, p in ports.items()],
        "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
    }
    return {
        "kind": "Deployment",
        "metadata": {"name": f"ds-{component}"},
        "spec": {
            "template": {
                "metadata": {"labels": labels(component)},
                "spec": {"containers": [container]},
            }
        },
    }


def service(component: str, name: str, port: int, kind: str | None = None) -> Doc:
    """Return a Service of `component` (no type: ClusterIP, as the chart renders)."""
    spec: Doc = {"selector": labels(component), "ports": [{"port": port}]}
    if kind:
        spec["type"] = kind
    return {"kind": "Service", "metadata": {"name": name}, "spec": spec}


def policy(component: str, rules: list[Doc]) -> Doc:
    """Return the ingress NetworkPolicy of `component`."""
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": f"ds-{component}"},
        "spec": {
            "podSelector": {"matchLabels": labels(component)},
            "policyTypes": ["Ingress"],
            "ingress": rules,
        },
    }


def pod_peer(*components: str) -> Doc:
    """Return the chart's podSelector peer for the release's `components`."""
    return {
        "podSelector": {
            "matchExpressions": [
                {
                    "key": "app.kubernetes.io/instance",
                    "operator": "In",
                    "values": [RELEASE],
                },
                {
                    "key": "app.kubernetes.io/component",
                    "operator": "In",
                    "values": list(components),
                },
            ]
        }
    }


def ns_peer(namespace: str, pod_labels: dict[str, str] | None = None) -> Doc:
    """Return a namespaceSelector peer, narrowed to `pod_labels`."""
    peer: Doc = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}}
    }
    if pod_labels:
        peer["podSelector"] = {"matchLabels": pod_labels}
    return peer


def rule(port: int, *peers: Doc) -> Doc:
    """Return an ingress rule admitting `peers` on TCP `port`."""
    return {"from": list(peers), "ports": [{"protocol": "TCP", "port": port}]}


def good() -> list[Doc]:
    """Return rendered docs where every trusted header is guarded as intended."""
    api = deployment(
        "api",
        {"http": 8080, "delegated": 8082, "tokens": 8083, "metrics": 9091},
        {
            "API_RATE_LIMIT_ENABLED": "true",
            "API_RATE_LIMIT_TRUST_X_REAL_IP": "true",
            "PERSONAL_TOKENS_TRUST_CF_CONNECTING_IP": "true",
        },
    )
    probe = {
        "kind": "Pod",
        "metadata": {
            "name": "ds-test",
            "labels": labels("test"),
            "annotations": {"helm.sh/hook": "test"},
        },
        "spec": {"containers": [{"name": "probe"}]},
    }
    return [
        api,
        deployment("frontend", {"http": 3000}),
        probe,
        service("api", "ds-api", 8080),
        service("api", "ds-api-delegated", 8082),
        service("api", "ds-api-tokens", 8083),
        service("frontend", "ds-frontend", 3000),
        policy(
            "api",
            [
                rule(8080, pod_peer("frontend", "test"), ns_peer("ds-mcp", MCP)),
                rule(8082, ns_peer("ds-mcp", MCP)),
                rule(8083, ns_peer("cloudflared", CONNECTOR)),
                rule(9091, ns_peer("monitoring")),
            ],
        ),
        policy("frontend", [rule(3000, ns_peer("cloudflared", CONNECTOR))]),
    ]


def find(docs: list[Doc], kind: str, name: str) -> Doc:
    """Return the doc of `kind` and `name`."""
    for doc in docs:
        metadata = doc["metadata"]
        assert isinstance(metadata, dict)  # noqa: S101  # a test helper
        if doc["kind"] == kind and metadata["name"] == name:
            return doc
    raise KeyError(name)


def rules_of(docs: list[Doc], component: str) -> list[Doc]:
    """Return the ingress rules of `component`'s policy."""
    spec = find(docs, "NetworkPolicy", f"ds-{component}")["spec"]
    assert isinstance(spec, dict)  # noqa: S101  # a test helper
    rules: list[Doc] = spec["ingress"]
    return rules


def pod_spec(docs: list[Doc], component: str) -> Doc:
    """Return the pod spec of `component`'s Deployment."""
    template = find(docs, "Deployment", f"ds-{component}")["spec"]["template"]
    spec: Doc = template["spec"]
    return spec


def without(docs: list[Doc], kind: str, *names: str) -> list[Doc]:
    """Return `docs` minus the `kind` docs called `names`."""
    return [
        d for d in docs if not (d["kind"] == kind and d["metadata"]["name"] in names)
    ]


def with_service(docs: list[Doc], name: str, **fields: object) -> list[Doc]:
    """Return a copy of `docs` with `fields` set on the Service `name`."""
    docs = copy.deepcopy(docs)
    find(docs, "Service", name)["spec"].update(fields)
    return docs


class TrustHeaderNetpolTests(unittest.TestCase):
    """The checker accepts the intended state and names each way it can drift."""

    def violations(self, docs: list[Doc], intent: object = INTENT) -> list[str]:
        """Return the checker's violations of `docs`."""
        return list(check.problems(docs, intent, RELEASE))

    def assert_violation(
        self, docs: list[Doc], surface: str, cause: str, intent: object = INTENT
    ) -> None:
        """Assert one violation names `surface` and contains `cause`."""
        found = self.violations(docs, intent)
        self.assertTrue(
            any(v.startswith(surface) and cause in v for v in found),
            f"no {surface!r} violation with {cause!r} in {found}",
        )

    def test_good_state_passes(self) -> None:
        """The intended policies produce no violation."""
        self.assertEqual(self.violations(good()), [])

    def test_good_state_with_ingress_controller_passes(self) -> None:
        """The ingress controller may reach the frontend when the intent has it."""
        docs = good()
        rules_of(docs, "frontend")[0]["from"] = [ns_peer("ingress-nginx")]
        rules_of(docs, "api")[0]["from"] = [pod_peer("frontend", "test")]
        rules_of(docs, "api").pop(2)
        rules_of(docs, "api").pop(1)
        docs.append(
            {
                "kind": "Ingress",
                "metadata": {"name": "i"},
                "spec": {"rules": [{"backend": "ds-frontend"}]},
            }
        )
        agent = deployment_env_off(docs)
        self.assertEqual(self.violations(agent, INTENT_INGRESS), [])

    def test_no_trust_flags_no_api_violation(self) -> None:
        """With both api flags off, a missing api policy is not reported."""
        docs = good()
        env = pod_spec(docs, "api")["containers"][0]["env"]
        for var in env:
            var["value"] = "false"
        docs.remove(find(docs, "NetworkPolicy", "ds-api"))
        self.assertEqual(self.violations(docs), [])

    def test_policy_removed(self) -> None:
        """Every surface fails with no NetworkPolicy."""
        docs = without(good(), "NetworkPolicy", "ds-api", "ds-frontend")
        self.assert_violation(
            docs, check.XREALIP_SURFACE, "no NetworkPolicy restricts ingress"
        )
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "no NetworkPolicy restricts ingress"
        )
        self.assert_violation(
            docs, check.FRONTEND_SURFACE, "no NetworkPolicy restricts ingress"
        )

    def test_policy_selecting_other_pods(self) -> None:
        """A policy whose podSelector no longer selects the api does not count."""
        docs = good()
        find(docs, "NetworkPolicy", "ds-api")["spec"]["podSelector"] = {
            "matchLabels": labels("other")
        }
        self.assert_violation(
            docs, check.XREALIP_SURFACE, "no NetworkPolicy restricts ingress"
        )

    def test_egress_only_policy_does_not_count(self) -> None:
        """A policy without Ingress in its policyTypes restricts nothing inbound."""
        docs = good()
        find(docs, "NetworkPolicy", "ds-frontend")["spec"]["policyTypes"] = ["Egress"]
        self.assert_violation(
            docs, check.FRONTEND_SURFACE, "no NetworkPolicy restricts ingress"
        )

    def test_empty_pod_selector_peer(self) -> None:
        """`podSelector: {}` admits every pod of the namespace."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            rules_of(docs, component)[0]["from"].append({"podSelector": {}})
            self.assert_violation(docs, surface, "not pinned to this release")

    def test_pod_selector_of_another_release(self) -> None:
        """A podSelector pinned to another release is not this release's."""
        docs = good()
        peer = pod_peer("frontend", "test")
        peer["podSelector"]["matchExpressions"][0]["values"] = ["other"]
        rules_of(docs, "api")[0]["from"] = [peer]
        self.assert_violation(docs, check.XREALIP_SURFACE, "not pinned to this release")

    def test_pod_selector_naming_another_component(self) -> None:
        """Admitting another component alongside the frontend is an extra peer."""
        docs = good()
        rules_of(docs, "api")[0]["from"][0] = pod_peer("frontend", "test", "notifier")
        self.assert_violation(
            docs,
            check.XREALIP_SURFACE,
            "also admits pods of component frontend, notifier, test",
        )

    def test_empty_namespace_selector(self) -> None:
        """`namespaceSelector: {}` admits every namespace."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            rules_of(docs, component)[0]["from"].append({"namespaceSelector": {}})
            self.assert_violation(docs, surface, "does not name one namespace")

    def test_ip_block(self) -> None:
        """An ipBlock of 0.0.0.0/0 admits everyone."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            rules_of(docs, component)[0]["from"].append(
                {"ipBlock": {"cidr": "0.0.0.0/0"}}
            )
            self.assert_violation(docs, surface, "an ipBlock (0.0.0.0/0)")

    def test_extra_from_entry(self) -> None:
        """An extra namespace in a rule's `from` is not an intended caller."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            rules_of(docs, component)[0]["from"].append(
                ns_peer("elsewhere", {"a": "b"})
            )
            self.assert_violation(
                docs, surface, "also admits pods labelled a=b in namespace elsewhere"
            )

    def test_extra_rule_on_the_port(self) -> None:
        """A second rule for the same port (an extraIngress) counts too."""
        docs = good()
        rules_of(docs, "frontend").append(rule(3000, ns_peer("elsewhere")))
        self.assert_violation(
            docs, check.FRONTEND_SURFACE, "also admits every pod in namespace elsewhere"
        )

    def test_rule_without_from_allows_all(self) -> None:
        """A rule with no `from` admits every source on its ports."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            rules_of(docs, component).append(
                {
                    "ports": [
                        {
                            "port": 8080 if component == "api" else 3000,
                            "protocol": "TCP",
                        }
                    ]
                }
            )
            self.assert_violation(
                docs, surface, "a rule with no `from` admits every source"
            )

    def test_rule_without_ports_allows_every_port(self) -> None:
        """A rule with no `ports` reaches the token and delegated ports too."""
        docs = good()
        rules_of(docs, "api").append({"from": [ns_peer("elsewhere")]})
        self.assert_violation(
            docs, check.XREALIP_SURFACE, "a rule with no `ports` admits every port"
        )
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "also admits every pod in namespace elsewhere"
        )

    def test_empty_ingress_list_is_default_deny_not_allow_all(self) -> None:
        """An empty ingress list denies everything, so intended callers are missing."""
        docs = good()
        find(docs, "NetworkPolicy", "ds-frontend")["spec"]["ingress"] = []
        self.assert_violation(
            docs,
            check.FRONTEND_SURFACE,
            "does not admit pods labelled app.kubernetes.io/name=cloudflared",
        )

    def test_empty_rule_is_allow_all(self) -> None:
        """A rule that is `{}` admits every source on every port."""
        docs = good()
        rules_of(docs, "frontend").append({})
        self.assert_violation(
            docs, check.FRONTEND_SURFACE, "a rule with no `from` admits every source"
        )

    def test_wrong_port(self) -> None:
        """A rule for another port leaves the trusted port with no caller."""
        docs = good()
        rules_of(docs, "api")[0]["ports"] = [{"protocol": "TCP", "port": 9999}]
        self.assert_violation(
            docs,
            check.XREALIP_SURFACE,
            "does not admit pods of component frontend, test",
        )

    def test_port_range_covering_the_port_counts(self) -> None:
        """An `endPort` range that includes a trusted port admits its peers."""
        docs = good()
        rules_of(docs, "api").append(
            {
                "from": [ns_peer("elsewhere")],
                "ports": [{"protocol": "TCP", "port": 8000, "endPort": 8100}],
            }
        )
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "also admits every pod in namespace elsewhere"
        )

    def test_named_port_counts(self) -> None:
        """A rule naming the container port admits its peers."""
        docs = good()
        rules_of(docs, "api").append(
            {"from": [ns_peer("elsewhere")], "ports": [{"port": "tokens"}]}
        )
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "also admits every pod in namespace elsewhere"
        )

    def test_udp_rule_does_not_count(self) -> None:
        """A UDP rule does not reach a TCP listener."""
        docs = good()
        rules_of(docs, "api").append(
            {
                "from": [ns_peer("elsewhere")],
                "ports": [{"port": 8083, "protocol": "UDP"}],
            }
        )
        self.assertEqual(self.violations(docs), [])

    def test_token_listener_open_to_the_namespace(self) -> None:
        """The whole tunnel namespace is wider than the connector."""
        docs = good()
        rules_of(docs, "api")[2]["from"] = [ns_peer("cloudflared")]
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "also admits every pod in namespace cloudflared"
        )

    def test_token_listener_open_to_the_frontend(self) -> None:
        """Only the tunnel may reach the token port, not the frontend."""
        docs = good()
        rules_of(docs, "api")[2]["from"].append(pod_peer("frontend"))
        self.assert_violation(
            docs, check.TOKEN_SURFACE, "also admits pods of component frontend"
        )

    def test_token_listener_unreachable_is_reported(self) -> None:
        """The tunnel missing from the token rule is a drift too."""
        docs = good()
        rules_of(docs, "api").pop(2)
        self.assert_violation(
            docs,
            check.TOKEN_SURFACE,
            "does not admit pods labelled app.kubernetes.io/name=cloudflared",
        )

    def test_token_flag_without_tunnel_intent(self) -> None:
        """The trust flag on while the intent has no tunnel is reported."""
        self.assert_violation(
            good(), check.TOKEN_SURFACE, "names no tunnel connector", check.Intent()
        )

    def test_delegated_port_open_to_the_frontend(self) -> None:
        """The delegated port admits only the extra callers."""
        docs = good()
        rules_of(docs, "api")[1]["from"].append(pod_peer("frontend"))
        self.assert_violation(
            docs,
            check.XREALIP_SURFACE,
            "delegated port 8082: the NetworkPolicy also admits pods of component"
            " frontend",
        )

    def test_whole_extra_namespace(self) -> None:
        """An extra namespace admitted without its pod labels is wider than intended."""
        docs = good()
        rules_of(docs, "api")[0]["from"][1] = ns_peer("ds-mcp")
        self.assert_violation(
            docs, check.XREALIP_SURFACE, "also admits every pod in namespace ds-mcp"
        )

    def test_test_probe_only_when_rendered(self) -> None:
        """The `test` component is admitted only while the helm-test Pod renders."""
        docs = without(good(), "Pod", "ds-test")
        self.assert_violation(
            docs, check.XREALIP_SURFACE, "also admits pods of component frontend, test"
        )

    def test_node_port_and_load_balancer_services(self) -> None:
        """A NodePort or LoadBalancer Service reaches the pods around the policy."""
        for kind in ("NodePort", "LoadBalancer"):
            for name, surface in (
                ("ds-api", check.XREALIP_SURFACE),
                ("ds-frontend", check.FRONTEND_SURFACE),
            ):
                docs = with_service(good(), name, type=kind)
                self.assert_violation(docs, surface, f"Service/{name} is type {kind}")

    def test_other_api_services_are_checked_too(self) -> None:
        """The delegated and token Services must stay ClusterIP as well."""
        for name in ("ds-api-delegated", "ds-api-tokens"):
            docs = with_service(good(), name, type="LoadBalancer")
            self.assert_violation(
                docs, check.TOKEN_SURFACE, f"Service/{name} is type LoadBalancer"
            )

    def test_external_ips(self) -> None:
        """A Service with externalIPs is reachable from outside."""
        docs = with_service(good(), "ds-frontend", externalIPs=["192.0.2.1"])
        self.assert_violation(docs, check.FRONTEND_SURFACE, "sets externalIPs")

    def test_host_network(self) -> None:
        """A hostNetwork pod bypasses the pod's NetworkPolicy."""
        for component, surface in (
            ("api", check.XREALIP_SURFACE),
            ("frontend", check.FRONTEND_SURFACE),
        ):
            docs = good()
            pod_spec(docs, component)["hostNetwork"] = True
            self.assert_violation(docs, surface, "runs with hostNetwork")

    def test_host_port(self) -> None:
        """A hostPort publishes the container on the node."""
        docs = good()
        pod_spec(docs, "frontend")["containers"][0]["ports"][0]["hostPort"] = 80
        self.assert_violation(docs, check.FRONTEND_SURFACE, "as a hostPort")

    def test_ingress_to_the_api(self) -> None:
        """An Ingress routing to the api Service is a violation, whatever its kind."""
        for kind in ("Ingress", "HTTPRoute", "IngressRoute"):
            docs = good()
            docs.append(
                {
                    "kind": kind,
                    "metadata": {"name": "r"},
                    "spec": {"rules": [{"backend": {"service": {"name": "ds-api"}}}]},
                }
            )
            self.assert_violation(
                docs, check.XREALIP_SURFACE, f"{kind}/r routes to Service ds-api"
            )

    def test_ingress_to_the_frontend_needs_the_ingress_intent(self) -> None:
        """An Ingress to the frontend needs the ingress controller in the intent."""
        docs = good()
        docs.append(
            {
                "kind": "Ingress",
                "metadata": {"name": "i"},
                "spec": {"rules": [{"backend": {"service": {"name": "ds-frontend"}}}]},
            }
        )
        self.assert_violation(
            docs, check.FRONTEND_SURFACE, "Ingress/i routes to Service ds-frontend"
        )
        found = self.violations(
            docs,
            check.Intent(
                extra_api_namespaces={"ds-mcp": MCP},
                tunnel=("cloudflared", CONNECTOR),
                ingress_namespace="ingress-nginx",
            ),
        )
        self.assertFalse([v for v in found if "routes to Service" in v], found)

    def test_unrelated_ingress_is_fine(self) -> None:
        """An Ingress that routes to something else is not this checker's concern."""
        docs = good()
        docs.append(
            {
                "kind": "Ingress",
                "metadata": {"name": "other"},
                "spec": {"rules": [{"backend": {"service": {"name": "grafana"}}}]},
            }
        )
        self.assertEqual(self.violations(docs), [])

    def test_missing_workloads_are_reported(self) -> None:
        """No api or frontend Deployment means the render cannot be trusted."""
        found = self.violations([])
        self.assertIn(f"{check.XREALIP_SURFACE}: no api Deployment rendered", found)
        self.assertIn(
            f"{check.FRONTEND_SURFACE}: no frontend Deployment rendered", found
        )


def deployment_env_off(docs: list[Doc]) -> list[Doc]:
    """Return `docs` with the token trust flag off (no tunnel in that variant)."""
    env = pod_spec(docs, "api")["containers"][0]["env"]
    for var in env:
        if var["name"] == "PERSONAL_TOKENS_TRUST_CF_CONNECTING_IP":
            var["value"] = "false"
    return docs


if __name__ == "__main__":
    unittest.main()
