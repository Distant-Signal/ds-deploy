"""Tests for scripts/check-components-chart.py, on synthetic rendered docs.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "check-components-chart.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_components_chart", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_components_chart"] = module
    spec.loader.exec_module(module)
    return module


components = _load()

type Doc = dict[str, object]

MAP: Doc = {
    "groups": {"ingest": "the ingest path", "schedule-feed": "CIF delivery"},
    "components": {"movement-relay": "ingest", "schedulefeed": "schedule-feed"},
    "exempt": [{"kind": "ConfigMap", "name": "odd", "reason": "test"}],
}


def obj(kind: str, name: str, component: str | None) -> Doc:
    """Return a rendered object, with `component` as its label unless None."""
    labels = {} if component is None else {components.LABEL: component}
    return {
        "kind": kind,
        "metadata": {"name": f"distant-signal-{name}", "labels": labels},
    }


def parsed(data: Doc = MAP) -> object:
    """Return the parsed map, failing the test on any shape problem."""
    component_map, fail = components.parse_map(data)
    if fail:
        raise AssertionError(fail)
    return component_map


GOOD = [
    obj("Deployment", "movement-relay", "movement-relay"),
    obj("Service", "schedulefeed", "schedulefeed"),
    obj("ConfigMap", "odd", None),
]


class ParseTest(unittest.TestCase):
    """components.yaml's shape."""

    def test_the_chart_file_parses_cleanly(self) -> None:
        """The chart's own components.yaml has no shape problems."""
        _, fail = components.parse_map(
            yaml.safe_load(components.COMPONENTS.read_text())
        )
        self.assertEqual(fail, [])

    def test_the_dtd_sftp_groups_exist(self) -> None:
        """The groups deploy-bringer's window rule names exist."""
        component_map, _ = components.parse_map(
            yaml.safe_load(components.COMPONENTS.read_text())
        )
        for group in ("schedule-feed", "migrate", "roles", "ingest", "pollers"):
            self.assertIn(group, component_map.groups)
        self.assertEqual(component_map.components["schedulefeed-sftp"], "schedule-feed")
        self.assertEqual(component_map.components["postgres-roles"], "roles")

    def test_an_unknown_group_fails(self) -> None:
        """A component mapped to a group not in `groups` fails."""
        data = {**MAP, "components": {**MAP["components"], "x": "nowhere"}}  # type: ignore[dict-item]
        _, fail = components.parse_map(data)
        self.assertTrue(any("unknown group 'nowhere'" in f for f in fail))

    def test_an_empty_group_fails(self) -> None:
        """A group with no component fails."""
        data = {**MAP, "groups": {**MAP["groups"], "spare": "nothing"}}  # type: ignore[dict-item]
        _, fail = components.parse_map(data)
        self.assertTrue(any("group 'spare' has no component" in f for f in fail))

    def test_an_exempt_entry_without_a_reason_fails(self) -> None:
        """An exempt entry needs kind, name and reason."""
        data = {**MAP, "exempt": [{"kind": "ConfigMap", "name": "odd"}]}
        _, fail = components.parse_map(data)
        self.assertEqual(len(fail), 1)


class CheckTest(unittest.TestCase):
    """Rendered objects against the map."""

    def test_mapped_and_exempt_objects_pass(self) -> None:
        """Mapped and exempt objects pass."""
        self.assertEqual(components.check(parsed(), {"r": GOOD}), [])

    def test_an_unlabelled_object_fails(self) -> None:
        """An object with no component label, not exempt, fails."""
        fail = components.check(parsed(), {"r": [*GOOD, obj("Secret", "new", None)]})
        self.assertEqual(len(fail), 1)
        self.assertIn(
            "Secret/distant-signal-new (r): no app.kubernetes.io/component", fail[0]
        )

    def test_an_unmapped_component_fails(self) -> None:
        """A component missing from the map fails (fail closed)."""
        fail = components.check(parsed(), {"r": [*GOOD, obj("Deployment", "x", "x")]})
        self.assertEqual(len(fail), 1)
        self.assertIn("component 'x' is not in components.yaml", fail[0])

    def test_a_stale_component_fails(self) -> None:
        """A mapped component that renders nowhere fails."""
        fail = components.check(parsed(), {"r": GOOD[1:]})
        stale = "component 'movement-relay' renders in no render (stale?)"
        self.assertEqual(fail, [f"components.yaml: {stale}"])

    def test_a_stale_exempt_entry_fails(self) -> None:
        """An exempt object that renders nowhere fails."""
        fail = components.check(parsed(), {"r": GOOD[:2]})
        self.assertEqual(
            fail,
            ["components.yaml: exempt ConfigMap/odd renders in no render (stale?)"],
        )

    def test_renders_are_pooled(self) -> None:
        """A component needs to render in only one render."""
        renders = {"a": GOOD[:1] + GOOD[2:], "b": GOOD[1:2]}
        self.assertEqual(components.check(parsed(), renders), [])


if __name__ == "__main__":
    unittest.main()
