"""Tests for scripts/check-config-defaults-chart.py, on synthetic renders.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "check-config-defaults-chart.py"
IMAGE = "ghcr.io/distant-signal/images/{}:build.1-abc@sha256:00"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_defaults_chart", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_config_defaults_chart"] = module
    spec.loader.exec_module(module)
    return module


cdc = _load()

type Doc = dict[str, object]


def deployment(
    service: str,
    env: Mapping[str, str | None],
    *,
    command: bool = False,
    probes: Mapping[str, str] | None = None,
) -> Doc:
    """Return a Deployment running `service`'s image with `env`."""
    container: Doc = {
        "name": service,
        "image": IMAGE.format(service),
        "env": [
            {"name": k, "value": v}
            if v is not None
            else {"name": k, "valueFrom": {"secretKeyRef": {"name": "s", "key": k}}}
            for k, v in env.items()
        ],
    }
    if command:
        container["command"] = ["/usr/local/bin/other"]
    for kind, path in (probes or {}).items():
        container[kind] = {"httpGet": {"path": path, "port": "health"}}
    return {
        "kind": "Deployment",
        "metadata": {"name": f"distant-signal-{service}"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    }


def defaults(
    service: str,
    env: Mapping[str, str | None],
    contract: Mapping[str, object] | None = None,
) -> object:
    """Return `service`'s defaults as the script parses them."""
    printed = {
        "service": service,
        "env": {
            k: {"flag": None, "default": v, "required": False} for k, v in env.items()
        },
        "contract": dict(contract or {}),
    }
    return cdc.parse_defaults(service, json.dumps(printed))


class ParseDefaultsTest(unittest.TestCase):
    """`--print-config-defaults` JSON."""

    def test_defaults_are_strings_or_none(self) -> None:
        """Each default is kept as text; null stays None."""
        parsed = cdc.parse_defaults(
            "notifier",
            json.dumps(
                {
                    "service": "notifier",
                    "env": {
                        "LOG_LEVEL": {"flag": "--log-level", "default": "info"},
                        "DATABASE_URL": {"flag": "--database-url", "default": None},
                    },
                    "contract": {},
                }
            ),
        )
        self.assertEqual(parsed.env, {"LOG_LEVEL": "info", "DATABASE_URL": None})

    def test_another_services_output_is_refused(self) -> None:
        """A binary printing another service's name is a wiring mistake."""
        with self.assertRaises(ValueError):
            cdc.parse_defaults("notifier", json.dumps({"service": "api", "env": {}}))


class ContainersTest(unittest.TestCase):
    """Which rendered containers are compared."""

    def test_a_service_image_with_its_own_entrypoint_is_compared(self) -> None:
        """The image's last path segment names the service."""
        found = cdc.service_containers([deployment("notifier", {"LOG_LEVEL": "info"})])
        self.assertEqual([c.service for c in found], ["notifier"])
        self.assertEqual(found[0].env, {"LOG_LEVEL": "info"})

    def test_a_command_override_is_skipped(self) -> None:
        """The api maintenance CronJob runs another binary from the api image."""
        found = cdc.service_containers([deployment("api", {}, command=True)])
        self.assertEqual(found, [])

    def test_other_images_are_skipped(self) -> None:
        """Redis, Postgres and the frontend print no defaults."""
        doc = deployment("frontend", {})
        self.assertEqual(cdc.service_containers([doc]), [])


class StaleTest(unittest.TestCase):
    """A chart env var the service does not read."""

    def test_an_unread_var_fails(self) -> None:
        """A var the service does not read fails: its value does nothing."""
        containers = cdc.service_containers(
            [deployment("notifier", {"COOLDOWN_MINUTES": "20"})]
        )
        failures = list(
            cdc.stale(containers, {"notifier": defaults("notifier", {"X": None})})
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("COOLDOWN_MINUTES", failures[0])

    def test_process_env_and_expanded_vars_pass(self) -> None:
        """RUST_LOG is the runtime's; PGPASSWORD is DATABASE_URL's input."""
        env = {
            "RUST_LOG": "info",
            "PGPASSWORD": None,
            "DATABASE_URL": "postgres://u:$(PGPASSWORD)@db:5432/d",
        }
        containers = cdc.service_containers([deployment("notifier", env)])
        read = {"notifier": defaults("notifier", {"DATABASE_URL": None})}
        self.assertEqual(list(cdc.stale(containers, read)), [])


class UnwiredTest(unittest.TestCase):
    """A variable the service reads that no render sets."""

    def test_a_read_but_unset_var_fails(self) -> None:
        """The 2026-09 notifier sweep intervals had working defaults only."""
        containers = cdc.service_containers([deployment("notifier", {"A": "1"})])
        read = {"notifier": defaults("notifier", {"A": "1", "B": "2"})}
        failures = list(cdc.unwired(containers, read, {}))
        self.assertEqual(len(failures), 1)
        self.assertIn("B", failures[0])

    def test_unwired_entries_must_stay_true(self) -> None:
        """An UNWIRED entry the chart now sets, or nothing reads, fails."""
        containers = cdc.service_containers(
            [deployment("aggregator", {"LINES_DIR": "/x"})]
        )
        read = {"aggregator": defaults("aggregator", {"LINES_DIR": None})}
        allow = {"aggregator": {"LINES_DIR": "why", "GONE": "why"}}
        failures = list(cdc.unwired(containers, read, allow))
        self.assertTrue(any("now sets it" in f for f in failures), failures)
        self.assertTrue(any("no longer reads" in f for f in failures), failures)


class OverriddenTest(unittest.TestCase):
    """A chart default that differs from the code default."""

    def test_an_unrecorded_difference_fails(self) -> None:
        """A changed code default shows up against the chart's value."""
        containers = cdc.service_containers(
            [deployment("notifier", {"COOLDOWN_MINUTES": "20"})]
        )
        read = {"notifier": defaults("notifier", {"COOLDOWN_MINUTES": "30"})}
        failures = list(cdc.overridden(containers, read, {}))
        self.assertEqual(len(failures), 1)
        self.assertIn("'20'", failures[0])
        self.assertIn("'30'", failures[0])

    def test_recorded_override_passes_and_either_side_changing_fails(self) -> None:
        """OVERRIDES pins both values."""
        pinned = {("movement-relay", "MAXLEN"): ("1048576", "524288")}
        containers = cdc.service_containers(
            [deployment("movement-relay", {"MAXLEN": "1048576"})]
        )
        same = {"movement-relay": defaults("movement-relay", {"MAXLEN": "524288"})}
        self.assertEqual(list(cdc.overridden(containers, same, pinned)), [])
        moved = {"movement-relay": defaults("movement-relay", {"MAXLEN": "1"})}
        self.assertEqual(len(list(cdc.overridden(containers, moved, pinned))), 1)
        back = {"movement-relay": defaults("movement-relay", {"MAXLEN": "1048576"})}
        self.assertEqual(len(list(cdc.overridden(containers, back, pinned))), 1)

    def test_no_or_empty_default_and_secrets_are_skipped(self) -> None:
        """Nothing to compare against."""
        containers = cdc.service_containers(
            [deployment("notifier", {"A": "x", "B": "y", "C": None})]
        )
        read = {"notifier": defaults("notifier", {"A": None, "B": "", "C": "z"})}
        self.assertEqual(list(cdc.overridden(containers, read, {})), [])


def chart(every: list[Doc], values: Mapping[str, object] | None = None) -> object:
    """Return a Chart whose `every workload` render is `every`."""
    return cdc.Chart({cdc.EVERY_RENDER: every}, dict(values or {}))


def prometheus_rule(expr: str) -> Doc:
    """Return a PrometheusRule holding the consumer API-errors record."""
    return {
        "kind": "PrometheusRule",
        "spec": {
            "groups": [{"rules": [{"record": cdc.CONSUMER_API_ERRORS, "expr": expr}]}]
        },
    }


EXPR = (
    "label_replace(sum by (namespace) (increase(distant_signal_trust_consumer_errors"
    '_total{namespace="ns", operation=~"a|b"}[30m])), "consumer", "trust-consumer",'
    ' "", "")\n'
)


class ContractTest(unittest.TestCase):
    """The constants the chart must agree with."""

    def test_api_call_operations(self) -> None:
        """The record sums exactly the consumer's operations, in any order."""
        c = chart([prometheus_rule(EXPR)])
        ok = cdc.check_api_call_operations("trust-consumer", ["b", "a"], c)
        self.assertEqual(list(ok), [])
        extra = cdc.check_api_call_operations("trust-consumer", ["a", "b", "c"], c)
        self.assertEqual(len(list(extra)), 1)

    def test_ingest_stream_maxlen(self) -> None:
        """Both alert maps name the budgeted streams; the caps match."""
        rule = {
            "ingestStreamBacklog": {"maxlen": {"s1": 10, "s2": 20}},
            "ingestStreamStalled": {"stallAfterSecs": {"s1": 60, "s2": 0}},
        }
        c = chart([], {"metrics": {"prometheusRule": rule}})
        failures = list(cdc.check_ingest_stream_maxlen("w", {"s1": 10, "s2": 21}, c))
        self.assertEqual(len(failures), 2, failures)
        self.assertIn("'s2'", failures[0])
        self.assertIn("must be positive", failures[1])

    def test_db_role(self) -> None:
        """DATABASE_URL's user is the service's role."""
        url = "postgres://distant_signal_writer:$(PGPASSWORD)@db:5432/d"
        c = chart([deployment("ingest-writer", {"DATABASE_URL": url})])
        self.assertEqual(list(cdc.check_db_role("ingest-writer", "writer", c)), [])
        self.assertEqual(len(list(cdc.check_db_role("ingest-writer", "app", c))), 1)

    def test_probe_paths(self) -> None:
        """movement-relay's liveness must not be its readiness."""
        good = {"livenessProbe": "/livez", "readinessProbe": "/healthz"}
        want = {"liveness": "/livez", "readiness": "/healthz"}
        c = chart([deployment("movement-relay", {}, probes=good)])
        self.assertEqual(list(cdc.check_probe_paths("movement-relay", want, c)), [])
        bad = {"livenessProbe": "/healthz", "readinessProbe": "/healthz"}
        c = chart([deployment("movement-relay", {}, probes=bad)])
        self.assertEqual(len(list(cdc.check_probe_paths("movement-relay", want, c))), 1)

    def test_an_unknown_contract_key_fails(self) -> None:
        """A new contract needs its check."""
        read = {"api": defaults("api", {}, {"new_thing": 1})}
        failures = list(cdc.contracts(read, chart([])))
        self.assertEqual(len(failures), 1)
        self.assertIn("new_thing", failures[0])


if __name__ == "__main__":
    unittest.main()
