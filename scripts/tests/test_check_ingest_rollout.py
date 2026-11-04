"""Tests for scripts/check-ingest-rollout.py, on recorded Prometheus answers.

The fixtures in fixtures/ingest-rollout/ are in the script's own --record
format (each query, its `at`, and Prometheus's `data`), replayed in order
per query with the last answer repeating:

  healthy     every check passes on the first look;
  recovering  a surge pod, one TRUST partition held, lag, full-coverage's
              replay and an old pending ingest entry on the first look,
              all recovered on the second;
  gap         a stream gap, new dead letters and a darwin offset reset
              (fails at once);
  sparse      only kube-state-metrics' series (the rest is skipped);
  stuck       trust-consumer never available (fails at the deadline).

uv run python -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import ClassVar, override

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-ingest-rollout.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "ingest-rollout"
# The fixtures' clock: 2026-11-04 14:00 UTC, in rail hours.
T0 = 1793800800.0


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ingest_rollout", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_ingest_rollout"] = module
    spec.loader.exec_module(module)
    return module


rollout = _load()


def fixture(name: str) -> list[dict[str, object]]:
    """Return a recorded fixture."""
    data: list[dict[str, object]] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def scope() -> object:
    """Return the scope the fixtures were recorded with."""
    return rollout.Scope(
        "distant-signal",
        "distant-signal",
        rollout.load_components(rollout.COMPONENTS),
    )


class FakeClock:
    """A clock that sleeping advances."""

    def __init__(self) -> None:
        """Start at the fixtures' time."""
        self.now = T0
        self.sleeps = 0

    def time(self) -> float:
        """Return the current time."""
        return self.now

    def sleep(self, secs: float) -> None:
        """Advance the clock."""
        self.now += secs
        self.sleeps += 1


def run_verify(name: str, *, before: bool = True) -> tuple[int, str, FakeClock]:
    """Verify against a fixture, the baseline from its `at` answers."""
    prom = rollout.ReplayPrometheus(fixture(name))
    if before:
        baseline = rollout.take_baseline(prom, scope(), T0 - 600, T0)
    else:
        baseline = rollout.Baseline(T0 - 600, {}, {}, {}, None)
    clock = FakeClock()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = rollout.verify(
            prom,
            scope(),
            baseline,
            rollout.Polling(900, 30, clock.time, clock.sleep),
        )
    return status, out.getvalue(), clock


class VerifyTest(unittest.TestCase):
    """verify on each recorded scenario."""

    def test_healthy_passes_at_once(self) -> None:
        """Every check passes on the first look."""
        status, out, clock = run_verify("healthy")
        self.assertEqual(status, 0)
        self.assertEqual(clock.sleeps, 0)
        self.assertIn("OK   group trust-consumer: lag 310 (baseline 310)", out)
        self.assertIn(
            "OK   deployment distant-signal-movement-relay: rolled out, 1 pod", out
        )
        self.assertIn("OK   full-coverage replay: startup replay complete", out)
        self.assertTrue(out.rstrip().endswith("verify: ok"))

    def test_the_kafka_gauges_pass(self) -> None:
        """movement-relay's partitions and Kafka lag, darwin's resets."""
        _, out, _ = run_verify("healthy")
        self.assertIn("OK   movement-relay partitions: 2 of 2 held", out)
        self.assertIn("OK   movement-relay kafka lag: 50 records (baseline 50)", out)
        self.assertIn("OK   darwin offset reset: no reset", out)

    def test_gauges_an_older_image_lacks_are_skipped(self) -> None:
        """Gauges an image does not export yet are skipped, never failed."""
        _, out, _ = run_verify("sparse", before=False)
        self.assertIn(
            "SKIP movement-relay partitions: not available "
            "(no distant_signal_movement_relay_assigned_partitions series)",
            out,
        )
        self.assertIn("SKIP darwin offset reset: not available", out)
        self.assertIn("SKIP movement-relay kafka lag: not available", out)

    def test_duplicates_are_information_only(self) -> None:
        """Replayed entries are reported, not failed."""
        _, out, _ = run_verify("healthy")
        self.assertIn("INFO replayed by full-coverage: 48211 since the baseline", out)

    def test_recovering_waits_then_passes(self) -> None:
        """A surge pod, lag and a running replay wait, then pass."""
        status, out, clock = run_verify("recovering")
        self.assertEqual(status, 0)
        self.assertEqual(clock.sleeps, 1)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("waiting on 6 check(s):"), first)
        for name in (
            "deployment distant-signal-movement-relay",
            "movement-relay partitions",
            "group trust-consumer",
            "group full-coverage-consumer",
            "full-coverage replay",
            "ingest streams pending",
        ):
            self.assertIn(name, first)
        self.assertNotIn("group trust-event-backlog", first)

    def test_a_gap_fails_at_once(self) -> None:
        """A stream gap and new dead letters fail without waiting."""
        status, out, clock = run_verify("gap")
        self.assertEqual(status, 1)
        self.assertEqual(clock.sleeps, 0)
        self.assertIn(
            "FAIL stream gaps (trust_consumer): 3 gap(s) since the baseline", out
        )
        self.assertIn(
            "FAIL movement dead letters: new dead letters since the baseline "
            "(trust-consumer: +2)",
            out,
        )
        self.assertIn("FAIL darwin offset reset: darwin-relay reset its offset", out)
        self.assertIn("verify: 3 check(s) found a gap or loss", out)
        # Gap checks print first.
        self.assertLess(out.index("stream gaps"), out.index("deployment "))

    def test_sparse_metrics_skip_and_pass(self) -> None:
        """With only kube-state-metrics, the rest is skipped and it passes."""
        status, out, _ = run_verify("sparse", before=False)
        self.assertEqual(status, 0)
        self.assertIn(
            "SKIP movement-events lag: not available "
            "(no distant_signal_movement_relay_stream_lag series)",
            out,
        )
        self.assertNotIn("FAIL", out)

    def test_stuck_fails_at_the_deadline(self) -> None:
        """A Deployment that never recovers fails after the wait."""
        status, out, clock = run_verify("stuck")
        self.assertEqual(status, 1)
        self.assertEqual(clock.now, T0 + 900)
        self.assertIn("FAIL deployment distant-signal-trust-consumer: available 0", out)
        self.assertIn("verify: 1 check(s) had not recovered in 15 minutes", out)


class BaselineTest(unittest.TestCase):
    """The baseline: read from Prometheus, and its file round trip."""

    def test_baseline_reads_the_levels(self) -> None:
        """The baseline holds each group's lag and pending, and the DLQ lengths."""
        prom = rollout.ReplayPrometheus(fixture("healthy"))
        baseline = rollout.take_baseline(prom, scope(), T0 - 600, T0)
        self.assertEqual(baseline.at, T0 - 600)
        self.assertEqual(baseline.lag["trust-event-backlog"], 400)
        self.assertEqual(baseline.pending["trust-consumer"], 12)
        self.assertEqual(baseline.deadletter_length, {"movement-events-dlq": 5})
        self.assertEqual(baseline.relay_kafka_lag, 50)

    def test_the_file_round_trips(self) -> None:
        """A baseline survives its JSON file."""
        baseline = rollout.Baseline(T0, {"g": 1.0}, {"g": 2.0}, {"s": 3.0}, 4.0)
        self.assertEqual(rollout.Baseline.from_json(baseline.to_json()), baseline)


class OptionsTest(unittest.TestCase):
    """Command-line helpers."""

    def test_parse_time(self) -> None:
        """Unix and ISO times parse; a bare ISO time is UTC."""
        self.assertEqual(rollout.parse_time("1793800800"), T0)
        self.assertEqual(rollout.parse_time("2026-11-04T14:00:00"), T0)
        self.assertEqual(rollout.parse_time("2026-11-04T15:00:00+01:00"), T0)

    def test_components_are_the_ingest_and_poller_groups(self) -> None:
        """The checked Deployments come from components.yaml."""
        components = rollout.load_components(rollout.COMPONENTS)
        self.assertIn("movement-relay", components)
        self.assertIn("poller-tfl", components)
        self.assertNotIn("api", components)
        self.assertNotIn("schedulefeed", components)


class Handler(BaseHTTPRequestHandler):
    """A Prometheus stand-in answering from a recorded fixture."""

    prom: ClassVar[object]
    seen: ClassVar[list[tuple[str, str | None]]] = []

    def do_GET(self) -> None:
        """Answer /api/v1/query from the fixture."""
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        type(self).seen.append((url.path, self.headers.get("Authorization")))
        at = float(params["time"][0]) if "time" in params else None
        samples = self.prom.query(params["query"][0], at)  # type: ignore[attr-defined]
        result = [
            {"metric": dict(s.labels), "value": [T0, str(s.value)]} for s in samples
        ]
        body = json.dumps(
            {"status": "success", "data": {"resultType": "vector", "result": result}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


class HttpTest(unittest.TestCase):
    """The real client and main() against a local HTTP stand-in."""

    @override
    def setUp(self) -> None:
        """Serve the healthy fixture."""
        Handler.prom = rollout.ReplayPrometheus(fixture("healthy"))
        Handler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    @override
    def tearDown(self) -> None:
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def main(self, *argv: str) -> tuple[int, str]:
        """Return the script's exit status and stdout for `argv`."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = rollout.main(list(argv))
        return status, out.getvalue()

    def test_baseline_then_verify(self) -> None:
        """Baseline writes its file and verify passes, with the token sent."""
        token = self.dir / "token"
        token.write_text("s3cret\n")
        base = self.dir / "baseline.json"
        record = self.dir / "record.json"
        auth = ("--prometheus-url", self.url, "--bearer-token-file", str(token))
        status, out = self.main("baseline", *auth, "--out", str(base))
        self.assertEqual(status, 0, out)
        self.assertIn("lag", json.loads(base.read_text()))
        status, out = self.main(
            "verify", *auth, "--baseline", str(base), "--record", str(record)
        )
        self.assertEqual(status, 0, out)
        self.assertTrue(json.loads(record.read_text()))
        self.assertTrue(Handler.seen)
        for path, header in Handler.seen:
            self.assertEqual(path, "/api/v1/query")
            self.assertEqual(header, "Bearer s3cret")

    def test_an_unreachable_prometheus_is_a_usage_error(self) -> None:
        """A Prometheus that does not answer exits 2."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{sock.getsockname()[1]}"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _ = self.main(
                "verify", "--prometheus-url", closed, "--before", "1793800200"
            )
        self.assertEqual(status, 2)
        self.assertIn("Prometheus query failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
