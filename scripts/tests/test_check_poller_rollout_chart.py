"""Tests for scripts/check-poller-rollout-chart.py, on synthetic rendered docs.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from unittest import mock

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "check-poller-rollout-chart.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_poller_rollout_chart", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_poller_rollout_chart"] = module
    spec.loader.exec_module(module)
    return module


rollout = _load()

type Doc = dict[str, object]

POLLERS = frozenset(rollout.DEFAULTS)
SURGE_STRATEGY: Doc = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
}


def deployment(
    component: str,
    *,
    surge: bool | None = None,
    strategy: Doc | None = None,
    probe: bool = True,
    fallback: bool | None = None,
) -> Doc:
    """Return a poller Deployment, a surge or Recreate as DEFAULTS says."""
    if surge is None:
        surge = rollout.DEFAULTS.get(component) == rollout.SURGE
    if fallback is None:
        fallback = surge
    container: Doc = {"name": component, "env": []}
    if fallback:
        container["env"] = [{"name": "POLLER_READY_FALLBACK_SECS", "value": "300"}]
    if surge and probe:
        container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": 8090}}
    return {
        "kind": "Deployment",
        "metadata": {
            "name": f"distant-signal-{component}",
            "labels": {"app.kubernetes.io/component": component},
        },
        "spec": {
            "replicas": 1,
            "strategy": strategy or (SURGE_STRATEGY if surge else {"type": "Recreate"}),
            "template": {"spec": {"containers": [container]}},
        },
    }


def defaults(**override: Doc) -> list[Doc]:
    """Every poller Deployment at its default, with some replaced."""
    return [override.get(c.replace("-", "_"), deployment(c)) for c in rollout.DEFAULTS]


class CheckTest(unittest.TestCase):
    """Each poller Deployment's strategy, probe and env against DEFAULTS."""

    def test_the_defaults_pass(self) -> None:
        """The defaults pass."""
        self.assertEqual(rollout.check(defaults(), POLLERS), [])

    def test_a_surge_that_may_stop_the_old_pod_first_fails(self) -> None:
        """A surge that may stop the old pod first fails."""
        bad = deployment(
            "poller-tfl",
            strategy={
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 1},
            },
        )
        fail = rollout.check(defaults(poller_tfl=bad), POLLERS)
        self.assertEqual(len(fail), 1)
        self.assertIn("maxUnavailable 1, not 0", fail[0])

    def test_kubernetes_default_strategy_is_not_a_surge(self) -> None:
        """Kubernetes default strategy is not a surge."""
        bad = deployment("poller-tfl", strategy={"type": "RollingUpdate"})
        fail = rollout.check(defaults(poller_tfl=bad), POLLERS)
        self.assertTrue(any("maxUnavailable None" in f for f in fail), fail)

    def test_a_surge_without_readiness_fails(self) -> None:
        """A surge without readiness fails."""
        bad = deployment("poller-incidents", probe=False)
        fail = rollout.check(defaults(poller_incidents=bad), POLLERS)
        self.assertEqual(len(fail), 1)
        self.assertIn("no readiness probe on /healthz", fail[0])

    def test_transit_must_stay_recreate(self) -> None:
        """Transit must stay recreate."""
        bad = deployment("poller-transit-schedule", surge=True)
        fail = rollout.check(defaults(poller_transit_schedule=bad), POLLERS)
        self.assertTrue(any("must be Recreate" in f for f in fail), fail)

    def test_a_changed_default_fails(self) -> None:
        """A changed default fails."""
        changed = deployment("poller-stations", surge=True)
        fail = rollout.check(defaults(poller_stations=changed), POLLERS)
        self.assertEqual(
            fail,
            [
                (
                    "distant-signal-poller-stations: renders surge by default, "
                    "DEFAULTS says recreate"
                )
            ],
        )

    def test_an_unknown_or_missing_poller_fails(self) -> None:
        """An unknown or missing poller fails."""
        docs = [d for d in defaults() if "tocs" not in str(d["metadata"])]
        docs.append(deployment("poller-new", surge=False))
        fail = rollout.check(docs, POLLERS | {"poller-new"})
        self.assertIn(
            "distant-signal-poller-new: 'poller-new' is not in DEFAULTS; "
            "add its rollout",
            fail,
        )
        self.assertIn(
            "no Deployment rendered for poller component 'poller-tocs' "
            "(DEFAULTS is stale?)",
            fail,
        )

    def test_other_groups_are_ignored(self) -> None:
        """Other groups are ignored."""
        other = deployment("ingest-writer", surge=True, probe=False)
        self.assertEqual(rollout.check([*defaults(), other], POLLERS), [])


class FallbackTest(unittest.TestCase):
    """POLLER_READY_FALLBACK_SECS lands on the surge pollers only."""

    def test_on_every_surge_poller_and_no_other_passes(self) -> None:
        """On every surge poller and no other passes."""
        self.assertEqual(rollout.check_fallback(defaults(), POLLERS), [])

    def test_a_surge_poller_without_it_fails(self) -> None:
        """A surge poller without it fails."""
        bad = deployment("poller-tfl", fallback=False)
        fail = rollout.check_fallback(defaults(poller_tfl=bad), POLLERS)
        self.assertEqual(len(fail), 1)
        self.assertIn("distant-signal-poller-tfl: POLLER_READY_FALLBACK_SECS", fail[0])

    def test_a_recreate_poller_with_it_fails(self) -> None:
        """A recreate poller with it fails."""
        bad = deployment("poller-tocs", fallback=True)
        fail = rollout.check_fallback(defaults(poller_tocs=bad), POLLERS)
        self.assertEqual(
            fail,
            [
                (
                    "distant-signal-poller-tocs: sets POLLER_READY_FALLBACK_SECS "
                    "but is Recreate"
                )
            ],
        )


class ComponentsTest(unittest.TestCase):
    """The `pollers` group of components.yaml."""

    def test_reads_the_pollers_group(self) -> None:
        """Reads the pollers group."""
        data = {"components": {"poller-tfl": "pollers", "api": "api"}}
        self.assertEqual(rollout.poller_components(data), frozenset({"poller-tfl"}))
        self.assertEqual(rollout.poller_components({}), frozenset())

    def test_the_chart_maps_every_default(self) -> None:
        """The chart maps every default."""
        pollers = rollout.poller_components(
            yaml.safe_load(rollout.COMPONENTS.read_text())
        )
        self.assertEqual(pollers, frozenset(rollout.DEFAULTS))


class RefusalsTest(unittest.TestCase):
    """The overrides the chart must refuse, with helm faked."""

    def run_refusals(self, answers: dict[str, tuple[list[Doc], str]]) -> list[str]:
        """Run check_refusals with each override's render answered from `answers`."""

        def fake(_helm: str, extra: Sequence[str] = ()) -> tuple[list[Doc], str]:
            return answers.get(extra[1], ([], "Error: something else"))

        with mock.patch.object(rollout, "render", fake):
            result: list[str] = rollout.check_refusals("helm")
        return result

    def test_every_refusal_with_its_message_passes(self) -> None:
        """Every refusal with its message passes."""
        answers: dict[str, tuple[list[Doc], str]] = {
            extra[1]: ([], f"Error: {message}") for extra, message in rollout.REFUSALS
        }
        self.assertEqual(self.run_refusals(answers), [])

    def test_a_render_that_succeeds_fails(self) -> None:
        """A render that succeeds fails."""
        answers: dict[str, tuple[list[Doc], str]] = {
            extra[1]: ([], f"Error: {message}") for extra, message in rollout.REFUSALS
        }
        answers["pollers.tfl.rollout=rolling"] = ([deployment("poller-tfl")], "")
        fail = self.run_refusals(answers)
        self.assertEqual(
            fail,
            [
                (
                    "--set pollers.tfl.rollout=rolling: "
                    "rendered, but the chart must refuse it"
                )
            ],
        )

    def test_a_refusal_for_another_reason_fails(self) -> None:
        """A refusal for another reason fails."""
        fail = self.run_refusals({})
        self.assertEqual(len(fail), len(rollout.REFUSALS))
        self.assertTrue(all("refused without" in f for f in fail), fail)


if __name__ == "__main__":
    unittest.main()
