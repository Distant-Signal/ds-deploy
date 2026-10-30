"""Tests for scripts/check-redis-netpol-chart.py, on synthetic rendered docs.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "check-redis-netpol-chart.py"
RELEASE = "distant-signal"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_redis_netpol_chart", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_redis_netpol_chart"] = module
    spec.loader.exec_module(module)
    return module


netpol = _load()

type Doc = dict[str, object]


def labels(component: str) -> dict[str, str]:
    """Return the chart's selector labels for `component`."""
    return {
        "app.kubernetes.io/name": "distant-signal",
        "app.kubernetes.io/instance": RELEASE,
        "app.kubernetes.io/component": component,
    }


def deployment(component: str, *, redis: bool, kind: str = "Deployment") -> Doc:
    """Return a workload of `component`, with REDIS_URL in its env when `redis`."""
    env = [{"name": "RUST_LOG", "value": "info"}]
    if redis:
        env.append({"name": "REDIS_URL", "value": "redis://r:6379"})
    pod = {
        "metadata": {"labels": labels(component)},
        "spec": {"containers": [{"name": component, "env": env}]},
    }
    spec: Doc = (
        {"jobTemplate": {"spec": {"template": pod}}}
        if kind == "CronJob"
        else {"template": pod}
    )
    return {"kind": kind, "metadata": {"name": f"ds-{component}"}, "spec": spec}


def redis_policy(*components: str, extra_peers: list[Doc] | None = None) -> Doc:
    """Return the Redis ingress policy admitting `components`."""
    peers: list[Doc] = [
        {
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
        },
        *(extra_peers or []),
    ]
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": "ds-redis"},
        "spec": {
            "podSelector": {"matchLabels": labels("redis")},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": peers, "ports": [{"port": 6379}]}],
        },
    }


def redis_service() -> Doc:
    """Return the Redis Service, on 6379."""
    return {
        "kind": "Service",
        "metadata": {"name": "ds-redis"},
        "spec": {"selector": labels("redis"), "ports": [{"port": 6379}]},
    }


def egress_policy(component: str, *, to_redis: bool) -> Doc:
    """`component`'s egress policy: DNS, plus Redis when `to_redis`."""
    rules: list[Doc] = [{"ports": [{"port": 53, "protocol": "UDP"}]}]
    if to_redis:
        rules.append(
            {
                "ports": [{"port": 6379, "protocol": "TCP"}],
                "to": [{"podSelector": {"matchLabels": labels("redis")}}],
            }
        )
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": f"ds-{component}"},
        "spec": {
            "podSelector": {"matchLabels": labels(component)},
            "policyTypes": ["Ingress", "Egress"],
            "egress": rules,
        },
    }


class ProblemsTests(unittest.TestCase):
    """The ingress check, both directions."""

    def test_admitted_clients_pass(self) -> None:
        """Every REDIS_URL workload admitted, nothing else listed: no problems."""
        docs = [
            deployment("enricher", redis=True),
            deployment("api", redis=False),
            redis_policy("enricher"),
        ]
        self.assertEqual(netpol.problems(docs), [])

    def test_missing_client_fails(self) -> None:
        """A REDIS_URL workload missing from the list fails (the DS-152 bug)."""
        docs = [
            deployment("enricher", redis=True),
            deployment("darwin-relay", redis=True),
            redis_policy("enricher"),
        ]
        found = netpol.problems(docs)
        self.assertEqual(len(found), 1)
        self.assertIn("Deployment/ds-darwin-relay", found[0])
        self.assertIn("does not admit", found[0])

    def test_cronjob_client_checked(self) -> None:
        """A CronJob's job template is checked too."""
        docs = [deployment("sweeper", redis=True, kind="CronJob"), redis_policy()]
        found = netpol.problems(docs)
        self.assertEqual(len(found), 1)
        self.assertIn("CronJob/ds-sweeper jobTemplate", found[0])

    def test_init_container_counts(self) -> None:
        """REDIS_URL in an init container counts."""
        doc = deployment("waiter", redis=False)
        pod = doc["spec"]["template"]  # type: ignore[index]
        pod["spec"]["initContainers"] = [
            {"name": "wait", "env": [{"name": "REDIS_URL", "value": "x"}]}
        ]
        self.assertEqual(len(netpol.problems([doc, redis_policy()])), 1)

    def test_stale_entry_fails(self) -> None:
        """A listed component without REDIS_URL fails."""
        docs = [deployment("api", redis=False), redis_policy("api")]
        found = netpol.problems(docs)
        self.assertEqual(len(found), 1)
        self.assertIn("admits component 'api'", found[0])

    def test_other_release_not_admitted(self) -> None:
        """A peer pinned to another release admits nothing."""
        policy = redis_policy("enricher")
        expressions = policy["spec"]["ingress"][0]["from"][0]["podSelector"][  # type: ignore[index]
            "matchExpressions"
        ]
        expressions[0]["values"] = ["other"]
        found = netpol.problems([deployment("enricher", redis=True), policy])
        self.assertIn("does not admit", found[0])

    def test_redis_clients_peers_ignored(self) -> None:
        """networkPolicy.redisClients peers count for neither direction."""
        exporter: Doc = {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/component": "darwin-relay"}
            }
        }
        docs = [
            deployment("darwin-relay", redis=True),
            redis_policy(extra_peers=[exporter]),
        ]
        self.assertEqual(len(netpol.problems(docs)), 1)

    def test_no_policy(self) -> None:
        """No Redis policy rendered is itself a failure."""
        found = netpol.problems([deployment("enricher", redis=True)])
        self.assertEqual(found, ["no Redis NetworkPolicy rendered"])


class EgressTests(unittest.TestCase):
    """The egress check."""

    def docs(self, *, to_redis: bool) -> list[Doc]:
        """Return Redis, its Service and darwin-relay with an egress policy."""
        return [
            deployment("redis", redis=False),
            redis_service(),
            deployment("darwin-relay", redis=True),
            egress_policy("darwin-relay", to_redis=to_redis),
        ]

    def test_allowed(self) -> None:
        """An egress rule to the Redis pod on its port passes."""
        self.assertEqual(netpol.egress_problems(self.docs(to_redis=True)), [])

    def test_blocked(self) -> None:
        """An egress policy without Redis fails."""
        found = netpol.egress_problems(self.docs(to_redis=False))
        self.assertEqual(len(found), 1)
        self.assertIn("Deployment/ds-darwin-relay", found[0])

    def test_unrestricted_egress_passes(self) -> None:
        """A workload no egress policy selects passes."""
        docs = [
            deployment("redis", redis=False),
            redis_service(),
            deployment("enricher", redis=True),
        ]
        self.assertEqual(netpol.egress_problems(docs), [])


class SelectorTests(unittest.TestCase):
    """Label selector matching."""

    def test_operators(self) -> None:
        """Match on matchLabels and each matchExpressions operator."""
        sel = netpol.Selector.parse(
            {
                "matchLabels": {"a": "1"},
                "matchExpressions": [
                    {"key": "b", "operator": "NotIn", "values": ["x"]},
                    {"key": "c", "operator": "Exists"},
                    {"key": "d", "operator": "DoesNotExist"},
                ],
            }
        )
        self.assertTrue(sel.matches({"a": "1", "b": "y", "c": ""}))
        self.assertFalse(sel.matches({"a": "1", "b": "x", "c": ""}))
        self.assertFalse(sel.matches({"a": "1", "c": "", "d": ""}))
        self.assertFalse(sel.matches({"a": "2", "c": ""}))

    def test_unknown_operator(self) -> None:
        """An unknown operator raises."""
        sel = netpol.Selector.parse(
            {"matchExpressions": [{"key": "a", "operator": "Gt", "values": []}]}
        )
        with self.assertRaises(ValueError):
            sel.matches({})


if __name__ == "__main__":
    unittest.main()
