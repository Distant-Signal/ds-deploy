"""Tests for scripts/check-kafka-rollout-chart.py, on synthetic rendered docs.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "check-kafka-rollout-chart.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_kafka_rollout_chart", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_kafka_rollout_chart"] = module
    spec.loader.exec_module(module)
    return module


rollout = _load()

type Doc = dict[str, object]

RECREATE: Doc = {"type": "Recreate", "rollingUpdate": None}
NEW_BEFORE_OLD: Doc = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
}


def deployment(
    component: str,
    strategy: Doc | None,
    *,
    replicas: int = 1,
    kafka: bool = True,
) -> Doc:
    """Return a Deployment of `component`, reading Kafka unless `kafka` is off."""
    env = [{"name": "KAFKA_TOPIC", "value": "t"}] if kafka else []
    spec: Doc = {
        "replicas": replicas,
        "template": {"spec": {"containers": [{"name": component, "env": env}]}},
    }
    if strategy is not None:
        spec["strategy"] = strategy
    return {
        "kind": "Deployment",
        "metadata": {
            "name": f"distant-signal-{component}",
            "labels": {"app.kubernetes.io/component": component},
        },
        "spec": spec,
    }


class CheckTest(unittest.TestCase):
    """The chart's relays as they are, and the strategies that deadlock."""

    def test_the_chart_as_it_is_passes(self) -> None:
        """The chart as it is passes."""
        docs = [
            deployment("darwin-relay", RECREATE),
            deployment("movement-relay", NEW_BEFORE_OLD),
        ]
        self.assertEqual(rollout.check(docs), [])

    def test_single_partition_new_before_old_fails(self) -> None:
        """Single partition new before old fails."""
        docs = [
            deployment("darwin-relay", NEW_BEFORE_OLD),
            deployment("movement-relay", NEW_BEFORE_OLD),
        ]
        fail = rollout.check(docs)
        self.assertEqual(len(fail), 1)
        self.assertIn("distant-signal-darwin-relay", fail[0])
        self.assertIn("deadlocks", fail[0])

    def test_kubernetes_default_strategy_at_one_replica_fails(self) -> None:
        """Kubernetes default strategy at one replica fails."""
        # 25%/25% at 1 replica: surge rounds up to 1, unavailable down to 0.
        docs = [
            deployment("darwin-relay", None),
            deployment("movement-relay", NEW_BEFORE_OLD),
        ]
        self.assertEqual(len(rollout.check(docs)), 1)

    def test_more_members_than_partitions_fails(self) -> None:
        """More members than partitions fails."""
        docs = [
            deployment("darwin-relay", RECREATE),
            deployment("movement-relay", NEW_BEFORE_OLD, replicas=2),
        ]
        self.assertEqual(len(rollout.check(docs)), 1)

    def test_rolling_update_that_stops_the_old_pod_first_passes(self) -> None:
        """Rolling update that stops the old pod first passes."""
        old_first: Doc = {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
        }
        docs = [
            deployment("darwin-relay", old_first),
            deployment("movement-relay", NEW_BEFORE_OLD),
        ]
        self.assertEqual(rollout.check(docs), [])

    def test_unknown_kafka_deployment_fails(self) -> None:
        """Unknown kafka deployment fails."""
        docs = [
            deployment("darwin-relay", RECREATE),
            deployment("movement-relay", NEW_BEFORE_OLD),
            deployment("new-relay", RECREATE),
        ]
        fail = rollout.check(docs)
        self.assertEqual(len(fail), 1)
        self.assertIn("not in PARTITIONS", fail[0])

    def test_non_kafka_deployment_is_ignored(self) -> None:
        """Non kafka deployment is ignored."""
        docs = [
            deployment("darwin-relay", RECREATE),
            deployment("movement-relay", NEW_BEFORE_OLD),
            deployment("api", NEW_BEFORE_OLD, kafka=False),
        ]
        self.assertEqual(rollout.check(docs), [])

    def test_a_relay_missing_from_the_render_fails(self) -> None:
        """A relay missing from the render fails."""
        fail = rollout.check([deployment("movement-relay", NEW_BEFORE_OLD)])
        self.assertEqual(len(fail), 1)
        self.assertIn("darwin-relay", fail[0])


if __name__ == "__main__":
    unittest.main()
