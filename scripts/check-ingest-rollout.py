#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the per-check report) is stdout
"""Check an ingest-affecting deploy lost nothing, from Prometheus only.

  scripts/check-ingest-rollout.py baseline --prometheus-url URL --out FILE
  scripts/check-ingest-rollout.py verify --prometheus-url URL
      (--baseline FILE | --before TIME) [--wait-minutes 15]

Read-only: it sends only instant queries (GET /api/v1/query) to the
Prometheus at --prometheus-url (or $PROMETHEUS_URL), with a bearer token
from --bearer-token-file (or $PROMETHEUS_BEARER_TOKEN) or basic auth from
--basic-auth-file (`user:password`), if the endpoint needs either.

`baseline`, before the deploy, records the levels the deploy should come
back to (each movement-events group's lag and pending entries, the
dead-letter lengths, movement-relay's Kafka lag) and when, and prints the
checks as they stand, so a problem that predates the deploy is not blamed
on it. `verify`, after the deploy, compares against that file (or against
Prometheus's own values at --before, for a deploy nobody baselined) and
polls every --interval-seconds until every check passes or --wait-minutes
runs out. What it checks, gap signals first:

  gaps        the consumers' stream-gap counters, a recreated
              movement-events group, darwin-relay's offset resets, and
              new dead letters (movement-feed's and the ingest streams'
              counters, the dead-letter stream lengths), all since the
              baseline;
  recovery    every ingest and poller Deployment (components.yaml's
              `ingest` and `pollers` groups) rolled out with exactly 1 pod;
              movement-relay Ready and publishing; each movement-events
              group's lag back near its baseline with its pending entries
              flat; full-coverage-consumer's startup replay complete; the
              oldest pending entry on every ds:ingest:* stream under 5
              minutes; darwin-relay's Kafka lag under its alert threshold;
              movement-relay's Kafka lag near baseline and its partitions
              all held (exported since ds-ingest 662fe534);
  pollers     each poller pod the deploy started has completed a
              successful cycle (a poller whose cycle is over 10 minutes
              apart is only reported); with the gap signals, no poller
              CronJob missed a scheduled start (a skipped run) or failed a
              run since the baseline;
  duplicates  information only: entries replayed by full-coverage-consumer
              and re-delivered entries since the baseline.

A check whose metric has no series is skipped ("not available"), never
failed: a workload that is off, or an image that does not export it yet.
Exit 0 when every check passed or was skipped, 1 when a gap was found or
something had not recovered by the deadline, 2 on a usage or Prometheus
error. --record FILE writes every query and answer as JSON; the unit tests
replay such files.
"""

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPONENTS = REPO / "charts" / "distant-signal" / "components.yaml"
# components.yaml's groups whose Deployments this checks.
GROUPS = ("ingest", "pollers")

# How far above its baseline a movement-events group's lag may stay:
# the larger of these entries and this fraction of the baseline.
LAG_SLACK_ENTRIES = 2000
LAG_SLACK_RATIO = 0.5
# Likewise for a group's pending (delivered, un-ACKed) entries.
PENDING_SLACK_ENTRIES = 100
# The oldest pending entry on a ds:ingest:* stream.
INGEST_OLDEST_PENDING_SECS = 300
# DistantSignalDarwinConsumerLagHigh's default threshold.
DARWIN_LAG_MAX = 50000
# Records of Kafka lag movement-relay may stay above its baseline.
RELAY_KAFKA_LAG_SLACK = 5000
# The TRUST topic's partitions (scripts/check-kafka-rollout-chart.py).
TRUST_PARTITIONS = 2
# A poller cycling at least this often must complete a cycle in its new
# pod within the wait; a slower one (a daily poller) is only reported.
POLLER_MAX_INTERVAL_SECS = 600
HOUR_SECS = 3600
# How late a poller CronJob's scheduled start may be before it counts as
# missed (the controller starts a run within seconds).
CRONJOB_START_SLACK_SECS = 600
# UTC hours during which movement-relay must be publishing (the
# DistantSignalMovementRelayNotPublishing default).
RAIL_HOURS_UTC = range(5, 23)
# A range selector's duration, as ReplayPrometheus ignores it.
RANGE = re.compile(r"\[\d+[smhd]\]")
STREAM_GAP_CONSUMERS = (
    "trust_consumer",
    "full_coverage_consumer",
    "trust_backlog_consumer",
)

type Status = Literal["ok", "fail", "wait", "skip", "info"]
# `wait`: not there yet, may still recover (a `fail` at the deadline).
# `fail`: a gap or loss that waiting will not undo.


@dataclass(frozen=True)
class Sample:
    """One series of an instant vector."""

    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True)
class Result:
    """One check's outcome."""

    name: str
    status: Status
    detail: str


class QueryError(Exception):
    """Prometheus refused a query or could not be reached."""


class Prometheus(Protocol):
    """What the checks need: instant queries, optionally at a past time."""

    def query(self, promql: str, at: float | None = None) -> list[Sample]:
        """Return the instant vector `promql` evaluates to (at `at`)."""
        ...


def parse_vector(data: object) -> list[Sample]:
    """Parse an /api/v1/query response body's `data` into samples."""
    body = cast("dict[str, object]", data) if isinstance(data, dict) else {}
    if body.get("resultType") not in {"vector", "scalar"}:
        msg = f"unexpected resultType {body.get('resultType')!r}"
        raise QueryError(msg)
    result = body.get("result")
    if body.get("resultType") == "scalar":
        pair = cast("list[object]", result)
        return [Sample({}, float(cast("str", pair[1])))]
    samples = []
    for item in cast("list[dict[str, object]]", result or []):
        metric = cast("dict[str, str]", item.get("metric") or {})
        pair = cast("list[object]", item.get("value"))
        samples.append(Sample(dict(metric), float(cast("str", pair[1]))))
    return samples


@dataclass
class HttpPrometheus:
    """Prometheus's HTTP API, read-only, with optional auth."""

    url: str
    authorization: str | None = None
    timeout: float = 30.0
    context: ssl.SSLContext | None = None
    record: list[dict[str, object]] | None = None

    def query(self, promql: str, at: float | None = None) -> list[Sample]:
        """GET /api/v1/query."""
        params = {"query": promql}
        if at is not None:
            params["time"] = f"{at:.3f}"
        url = f"{self.url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode(params)}"
        if not url.startswith(("http://", "https://")):
            msg = f"--prometheus-url must be http(s), not {self.url!r}"
            raise QueryError(msg)
        request = urllib.request.Request(url, method="GET")  # noqa: S310  # http(s) checked above
        if self.authorization:
            request.add_header("Authorization", self.authorization)
        try:
            with urllib.request.urlopen(  # noqa: S310  # http(s) checked above
                request, timeout=self.timeout, context=self.context
            ) as response:
                body = json.load(response)
        except (OSError, ValueError) as err:
            msg = f"{promql}: {err}"
            raise QueryError(msg) from err
        if body.get("status") != "success":
            msg = f"{promql}: {body.get('errorType')}: {body.get('error')}"
            raise QueryError(msg)
        if self.record is not None:
            self.record.append({"query": promql, "at": at, "data": body["data"]})
        return parse_vector(body["data"])


@dataclass
class ReplayPrometheus:
    """Answers from a --record file, in order per query; the last repeats."""

    recorded: Sequence[Mapping[str, object]]
    _served: dict[tuple[str, bool], int] = field(default_factory=dict)

    def query(self, promql: str, at: float | None = None) -> list[Sample]:
        """Return the next recorded answer to `promql` (empty if never recorded).

        Range durations are compared as `[_]`: the since-baseline window
        depends on the clock the recording ran at.
        """
        key = (RANGE.sub("[_]", promql), at is not None)
        answers = [
            r["data"]
            for r in self.recorded
            if RANGE.sub("[_]", str(r["query"])) == key[0]
            and (r.get("at") is not None) == key[1]
        ]
        if not answers:
            return []
        index = self._served.get(key, 0)
        self._served[key] = index + 1
        return parse_vector(answers[min(index, len(answers) - 1)])


def by(samples: Sequence[Sample], label: str) -> dict[str, float]:
    """Map each sample's `label` value to its value."""
    return {s.labels.get(label, ""): s.value for s in samples}


def scalar(samples: Sequence[Sample]) -> float | None:
    """Return the single value of a one-series vector, or None when empty."""
    return samples[0].value if samples else None


def not_available(metric: str) -> str:
    """Return a skipped check's detail."""
    return f"not available (no {metric} series)"


# --- what the deploy should come back to -------------------------------------


@dataclass(frozen=True)
class Baseline:
    """Levels before the deploy, and when they were read."""

    at: float
    lag: Mapping[str, float]
    pending: Mapping[str, float]
    deadletter_length: Mapping[str, float]
    relay_kafka_lag: float | None
    # Each poller's cycles in the hour before the baseline (its cadence).
    poller_cycles_per_hour: Mapping[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        """Return the baseline file's content."""
        return {
            "at": self.at,
            "at_iso": dt.datetime.fromtimestamp(self.at, dt.UTC).isoformat(),
            "lag": dict(self.lag),
            "pending": dict(self.pending),
            "deadletter_length": dict(self.deadletter_length),
            "relay_kafka_lag": self.relay_kafka_lag,
            "poller_cycles_per_hour": dict(self.poller_cycles_per_hour),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "Baseline":
        """Read a baseline file's content."""

        def floats(key: str) -> dict[str, float]:
            raw = cast("dict[str, object]", data.get(key) or {})
            return {k: float(cast("float", v)) for k, v in raw.items()}

        kafka = data.get("relay_kafka_lag")
        return cls(
            at=float(cast("float", data["at"])),
            lag=floats("lag"),
            pending=floats("pending"),
            deadletter_length=floats("deadletter_length"),
            relay_kafka_lag=None if kafka is None else float(cast("float", kafka)),
            poller_cycles_per_hour=floats("poller_cycles_per_hour"),
        )


@dataclass(frozen=True)
class Scope:
    """Where to look: the namespace and the Deployments to check."""

    namespace: str
    fullname: str
    components: Sequence[str]

    def sel(self, extra: str = "") -> str:
        """Return a label selector scoped to the namespace."""
        return f'namespace="{self.namespace}"' + (f", {extra}" if extra else "")

    def deployments(self) -> str:
        """Return a selector matching the checked Deployments by name."""
        names = "|".join(self.components)
        return self.sel(f'deployment=~"{self.fullname}-({names})(-.+)?"')

    def cronjob_names(self) -> str:
        """Return a regex matching the checked components' CronJobs by name."""
        return f"{self.fullname}-({'|'.join(self.components)})(-.+)?"


def lag_query(scope: Scope) -> str:
    """Return each movement-events group's lag query."""
    return f"max by (group) (distant_signal_movement_relay_stream_lag{{{scope.sel()}}})"


def pending_query(scope: Scope) -> str:
    """Return each movement-events group's pending-entries query."""
    metric = "distant_signal_movement_relay_stream_pending"
    return f"max by (group) ({metric}{{{scope.sel()}}})"


def deadletter_length_query(scope: Scope) -> str:
    """Return each dead-letter stream's length query (movement-relay's read)."""
    return (
        "max by (stream) "
        f"(distant_signal_movement_relay_deadletter_length{{{scope.sel()}}})"
    )


def relay_kafka_lag_query(scope: Scope) -> str:
    """Return movement-relay's Kafka lag query, summed over partitions."""
    return (
        "sum (max by (partition) "
        f"(distant_signal_movement_relay_consumer_lag{{{scope.sel()}}}))"
    )


def poller_cycles_query(scope: Scope) -> str:
    """Return each poller's cycles over the last hour (its cadence)."""
    return (
        "sum by (poller) "
        f"(increase(distant_signal_poller_cycle_total{{{scope.sel()}}}[1h]))"
    )


def take_baseline(
    prom: Prometheus, scope: Scope, at: float | None, now: float
) -> Baseline:
    """Read the levels the deploy should come back to (now, or at `at`)."""
    return Baseline(
        at=at if at is not None else now,
        lag=by(prom.query(lag_query(scope), at), "group"),
        pending=by(prom.query(pending_query(scope), at), "group"),
        deadletter_length=by(prom.query(deadletter_length_query(scope), at), "stream"),
        relay_kafka_lag=scalar(prom.query(relay_kafka_lag_query(scope), at)),
        poller_cycles_per_hour=by(prom.query(poller_cycles_query(scope), at), "poller"),
    )


# --- the checks ---------------------------------------------------------------


def since(baseline: Baseline, now: float) -> str:
    """Return a range covering the time since the baseline, for increase()."""
    return f"{max(60, int(now - baseline.at) + 60)}s"


def check_gaps(
    prom: Prometheus, scope: Scope, baseline: Baseline, now: float
) -> list[Result]:
    """Counters that only rise when data was skipped or trimmed unread."""
    window = since(baseline, now)
    results = []
    for consumer in STREAM_GAP_CONSUMERS:
        metric = f"distant_signal_{consumer}_stream_gap_detected_total"
        got = scalar(prom.query(f"sum (increase({metric}{{{scope.sel()}}}[{window}]))"))
        name = f"stream gaps ({consumer})"
        if got is None:
            results.append(Result(name, "skip", not_available(metric)))
        elif got > 0:
            results.append(
                Result(
                    name,
                    "fail",
                    f"{got:g} gap(s) since the baseline: entries were trimmed unread",
                )
            )
        else:
            results.append(Result(name, "ok", "none since the baseline"))
    metric = "distant_signal_movement_feed_group_recreated_total"
    got = scalar(prom.query(f"sum (increase({metric}{{{scope.sel()}}}[{window}]))"))
    if got is None:
        results.append(Result("group recreated", "skip", not_available(metric)))
    elif got > 0:
        results.append(
            Result(
                "group recreated",
                "fail",
                "a movement-events group was recreated: Redis lost the stream or group",
            )
        )
    else:
        results.append(Result("group recreated", "ok", "no group recreated"))
    metric = "distant_signal_darwin_relay_offset_resets_total"
    got = scalar(prom.query(f"sum (increase({metric}{{{scope.sel()}}}[{window}]))"))
    if got is None:
        results.append(Result("darwin offset reset", "skip", not_available(metric)))
    elif got > 0:
        results.append(
            Result(
                "darwin offset reset",
                "fail",
                "darwin-relay reset its offset: its committed one was gone (outage?)",
            )
        )
    else:
        results.append(Result("darwin offset reset", "ok", "no reset"))
    return results


def check_dead_letters(
    prom: Prometheus, scope: Scope, baseline: Baseline, now: float
) -> list[Result]:
    """Dead letters added since the baseline (counters, and the streams' lengths)."""
    window = since(baseline, now)
    results = []
    for name, metric, label in (
        (
            "movement dead letters",
            "distant_signal_movement_feed_deadlettered_total",
            "group",
        ),
        (
            "ingest dead letters",
            "distant_signal_ingest_stream_dead_lettered_total",
            "stream",
        ),
    ):
        samples = prom.query(
            f"sum by ({label}) (increase({metric}{{{scope.sel()}}}[{window}]))"
        )
        if not samples:
            results.append(Result(name, "skip", not_available(metric)))
            continue
        grown = {k: v for k, v in by(samples, label).items() if v > 0}
        if grown:
            what = ", ".join(f"{k}: +{v:g}" for k, v in sorted(grown.items()))
            results.append(
                Result(name, "fail", f"new dead letters since the baseline ({what})")
            )
        else:
            results.append(Result(name, "ok", "unchanged"))
    lengths = by(prom.query(deadletter_length_query(scope)), "stream")
    if not lengths:
        results.append(
            Result(
                "dead-letter streams",
                "skip",
                not_available("distant_signal_movement_relay_deadletter_length"),
            )
        )
    else:
        grown = {
            k: v - baseline.deadletter_length.get(k, 0.0)
            for k, v in lengths.items()
            if v > baseline.deadletter_length.get(k, 0.0)
        }
        if grown:
            what = ", ".join(f"{k}: +{v:g}" for k, v in sorted(grown.items()))
            results.append(
                Result(
                    "dead-letter streams",
                    "fail",
                    f"longer than at the baseline ({what})",
                )
            )
        else:
            results.append(
                Result("dead-letter streams", "ok", "no longer than at the baseline")
            )
    return results


DEPLOYMENT_SERIES = {
    "spec": "kube_deployment_spec_replicas",
    "pods": "kube_deployment_status_replicas",
    "available": "kube_deployment_status_replicas_available",
    "updated": "kube_deployment_status_replicas_updated",
    "observed": "kube_deployment_status_observed_generation",
    "generation": "kube_deployment_metadata_generation",
}


def check_deployments(prom: Prometheus, scope: Scope) -> list[Result]:
    """Every ingest and poller Deployment rolled out with exactly 1 pod."""
    values = {
        key: by(
            prom.query(f"max by (deployment) ({metric}{{{scope.deployments()}}})"),
            "deployment",
        )
        for key, metric in DEPLOYMENT_SERIES.items()
    }
    if not values["spec"]:
        return [
            Result(
                "deployments", "skip", not_available("kube_deployment_spec_replicas")
            )
        ]
    results = []
    for name in sorted(values["spec"]):
        got = {key: values[key].get(name) for key in DEPLOYMENT_SERIES}
        if got["spec"] == 0:
            results.append(Result(f"deployment {name}", "info", "scaled to 0"))
            continue
        problems = []
        if got["spec"] != 1:
            problems.append(f"{got['spec']:g} replicas desired, not 1")
        for key in ("pods", "available", "updated"):
            if got[key] != 1:
                shown = "none" if got[key] is None else f"{got[key]:g}"
                problems.append(f"{key} {shown}")
        if (
            got["observed"] is not None
            and got["generation"] is not None
            and got["observed"] < got["generation"]
        ):
            problems.append("new spec not yet observed")
        if problems:
            results.append(Result(f"deployment {name}", "wait", "; ".join(problems)))
        else:
            results.append(Result(f"deployment {name}", "ok", "rolled out, 1 pod"))
    return results


def check_pollers(
    prom: Prometheus, scope: Scope, baseline: Baseline, now: float
) -> list[Result]:
    """Each poller restarted since the baseline has completed a cycle since.

    A poller's upstream can't be replayed, so a pod that never completes a
    cycle is a gap that grows until someone acts. "Restarted" is a pod
    created after the baseline exporting the poller's cycle counter (a
    CronJob run exports none).
    """
    metric = "distant_signal_poller_cycle_total"
    new_pods = (
        f"on (namespace, pod) (kube_pod_created{{{scope.sel()}}} > {baseline.at:.0f})"
    )
    restarted = by(
        prom.query(f"max by (poller) ({metric}{{{scope.sel()}}} and {new_pods})"),
        "poller",
    )
    if not restarted:
        if prom.query(f"max by (poller) ({metric}{{{scope.sel()}}})"):
            return [
                Result("pollers", "info", "no poller pod started since the baseline")
            ]
        return [Result("pollers", "skip", not_available(metric))]
    window = since(baseline, now)
    success = scope.sel('result="success"')
    succeeded = by(
        prom.query(
            f"sum by (poller) "
            f"(increase({metric}{{{success}}}[{window}]) and {new_pods})"
        ),
        "poller",
    )
    results = []
    for poller in sorted(restarted):
        name = f"poller {poller}"
        cycles = baseline.poller_cycles_per_hour.get(poller)
        if succeeded.get(poller, 0.0) > 0:
            results.append(
                Result(name, "ok", "a successful cycle in a pod the deploy started")
            )
        elif cycles is not None and cycles * POLLER_MAX_INTERVAL_SECS < HOUR_SECS:
            results.append(
                Result(
                    name,
                    "info",
                    f"no successful cycle yet; it ran {cycles:.1f} cycles an hour, "
                    "so its next may be after the wait",
                )
            )
        else:
            results.append(
                Result(name, "wait", "no successful cycle yet in its new pod")
            )
    return results


def check_poller_cronjobs(
    prom: Prometheus, scope: Scope, baseline: Baseline
) -> list[Result]:
    """No poller CronJob skipped a scheduled start or failed a run since the baseline.

    A missed start (kube-state-metrics' next schedule time left in the past:
    the controller missed its starting deadline, or Forbid held it) is a run
    that never happened; a failed run gave up. Either way that day's data
    waits for the next run.
    """
    sel = scope.sel(f'cronjob=~"{scope.cronjob_names()}"')
    overdue = by(
        prom.query(
            f"max by (cronjob) (time() - kube_cronjob_next_schedule_time{{{sel}}})"
        ),
        "cronjob",
    )
    if not overdue:
        return [
            Result(
                "poller cronjobs",
                "skip",
                not_available("kube_cronjob_next_schedule_time"),
            )
        ]
    suspended = by(
        prom.query(f"max by (cronjob) (kube_cronjob_spec_suspend{{{sel}}})"), "cronjob"
    )
    active = by(
        prom.query(f"max by (cronjob) (kube_cronjob_status_active{{{sel}}})"), "cronjob"
    )
    owned = scope.sel(f'owner_kind="CronJob", owner_name=~"{scope.cronjob_names()}"')
    failed_job = f'kube_job_failed{{{scope.sel()}, condition="true"}} == 1'
    started = f"kube_job_status_start_time{{{scope.sel()}}} > {baseline.at:.0f}"
    # Each failed Job started since the baseline, with its CronJob's name.
    failed = prom.query(
        f"max by (owner_name, job_name) (({failed_job})"
        f" * on (namespace, job_name) group_left (owner_name) kube_job_owner{{{owned}}}"
        f" and on (namespace, job_name) ({started}))"
    )
    failed_by = {
        s.labels.get("owner_name", ""): s.labels.get("job_name", "") for s in failed
    }
    results = []
    for cronjob in sorted(overdue):
        name = f"cronjob {cronjob}"
        late = overdue[cronjob]
        if suspended.get(cronjob, 0.0) >= 1:
            results.append(Result(name, "info", "suspended"))
        elif cronjob in failed_by:
            results.append(
                Result(
                    name,
                    "fail",
                    f"run {failed_by[cronjob]} failed since the baseline: its data "
                    "waits for the next run (DistantSignalScheduledJobFailed)",
                )
            )
        elif late > CRONJOB_START_SLACK_SECS and active.get(cronjob, 0.0) > 0:
            results.append(
                Result(
                    name,
                    "wait",
                    f"a scheduled start is {late / 60:.0f} min late; "
                    "a run is still going",
                )
            )
        elif late > CRONJOB_START_SLACK_SECS:
            results.append(
                Result(
                    name,
                    "fail",
                    f"missed a scheduled start {late / 60:.0f} min ago: that run "
                    "never happened",
                )
            )
        else:
            results.append(Result(name, "ok", "no missed start, no failed run"))
    return results


def check_relay(prom: Prometheus, scope: Scope, now: float) -> list[Result]:
    """movement-relay Ready, publishing, holding every partition, its Kafka lag."""
    results = []
    metric = "distant_signal_movement_relay_ready"
    ready = scalar(prom.query(f"max ({metric}{{{scope.sel()}}})"))
    if ready is None:
        results.append(Result("movement-relay ready", "skip", not_available(metric)))
    elif ready >= 1:
        results.append(
            Result("movement-relay ready", "ok", "partition assignment confirmed")
        )
    else:
        results.append(
            Result(
                "movement-relay ready",
                "wait",
                "no pod has a confirmed partition assignment",
            )
        )
    metric = "distant_signal_movement_relay_events_published_total"
    published = scalar(prom.query(f"sum (increase({metric}{{{scope.sel()}}}[5m]))"))
    hour = dt.datetime.fromtimestamp(now, dt.UTC).hour
    if published is None:
        results.append(
            Result("movement-relay publishing", "skip", not_available(metric))
        )
    elif published > 0:
        results.append(
            Result(
                "movement-relay publishing",
                "ok",
                f"{published:.0f} events in the last 5m",
            )
        )
    elif hour in RAIL_HOURS_UTC:
        results.append(
            Result(
                "movement-relay publishing", "wait", "nothing published in the last 5m"
            )
        )
    else:
        results.append(
            Result(
                "movement-relay publishing",
                "info",
                "nothing published in the last 5m (overnight, may be quiet)",
            )
        )
    metric = "distant_signal_movement_relay_assigned_partitions"
    held = scalar(prom.query(f"sum ({metric}{{{scope.sel()}}})"))
    if held is None:
        results.append(
            Result("movement-relay partitions", "skip", not_available(metric))
        )
    elif held >= TRUST_PARTITIONS:
        results.append(
            Result(
                "movement-relay partitions",
                "ok",
                f"{held:g} of {TRUST_PARTITIONS} held",
            )
        )
    else:
        results.append(
            Result(
                "movement-relay partitions",
                "wait",
                f"{held:g} of {TRUST_PARTITIONS} held",
            )
        )
    return results


def check_relay_kafka_lag(prom: Prometheus, scope: Scope, baseline: Baseline) -> Result:
    """Check movement-relay's Kafka lag is near its baseline."""
    lag = scalar(prom.query(relay_kafka_lag_query(scope)))
    if lag is None:
        return Result(
            "movement-relay kafka lag",
            "skip",
            not_available("distant_signal_movement_relay_consumer_lag"),
        )
    base = baseline.relay_kafka_lag or 0.0
    if lag <= base + RELAY_KAFKA_LAG_SLACK:
        return Result(
            "movement-relay kafka lag", "ok", f"{lag:g} records (baseline {base:g})"
        )
    return Result(
        "movement-relay kafka lag", "wait", f"{lag:g} records, baseline {base:g}"
    )


def check_groups(prom: Prometheus, scope: Scope, baseline: Baseline) -> list[Result]:
    """Each movement-events group's lag near baseline and its pending entries flat."""
    lag = by(prom.query(lag_query(scope)), "group")
    if not lag:
        return [
            Result(
                "movement-events lag",
                "skip",
                not_available("distant_signal_movement_relay_stream_lag"),
            )
        ]
    pending = by(prom.query(pending_query(scope)), "group")
    results = []
    for group in sorted(lag):
        base_lag = baseline.lag.get(group, 0.0)
        limit = base_lag + max(LAG_SLACK_ENTRIES, LAG_SLACK_RATIO * base_lag)
        problems = []
        if lag[group] > limit:
            problems.append(f"lag {lag[group]:g}, baseline {base_lag:g}")
        if group in pending:
            base_pending = baseline.pending.get(group, 0.0)
            if pending[group] > base_pending + max(
                PENDING_SLACK_ENTRIES, LAG_SLACK_RATIO * base_pending
            ):
                problems.append(
                    f"pending {pending[group]:g}, baseline {base_pending:g}"
                )
        name = f"group {group}"
        if problems:
            results.append(Result(name, "wait", "; ".join(problems)))
        else:
            shown = f", pending {pending[group]:g}" if group in pending else ""
            results.append(
                Result(name, "ok", f"lag {lag[group]:g} (baseline {base_lag:g}){shown}")
            )
    return results


def check_streams(prom: Prometheus, scope: Scope) -> list[Result]:
    """full-coverage-consumer's replay, the ds:ingest:* streams, Darwin's lag."""
    results = []
    metric = "distant_signal_full_coverage_consumer_startup_complete"
    done = scalar(prom.query(f"min ({metric}{{{scope.sel()}}})"))
    if done is None:
        results.append(Result("full-coverage replay", "skip", not_available(metric)))
    elif done >= 1:
        results.append(Result("full-coverage replay", "ok", "startup replay complete"))
    else:
        results.append(
            Result("full-coverage replay", "wait", "startup replay still running")
        )
    metric = "distant_signal_ingest_stream_oldest_pending_age_seconds"
    ages = by(prom.query(f"max by (stream) ({metric}{{{scope.sel()}}})"), "stream")
    if not ages:
        results.append(Result("ingest streams pending", "skip", not_available(metric)))
    else:
        old = {k: v for k, v in ages.items() if v >= INGEST_OLDEST_PENDING_SECS}
        if old:
            what = ", ".join(f"{k}: {v:.0f}s" for k, v in sorted(old.items()))
            results.append(
                Result(
                    "ingest streams pending",
                    "wait",
                    f"oldest pending entry over {INGEST_OLDEST_PENDING_SECS}s ({what})",
                )
            )
        else:
            results.append(
                Result(
                    "ingest streams pending",
                    "ok",
                    f"every oldest pending entry under {INGEST_OLDEST_PENDING_SECS}s",
                )
            )
    metric = "distant_signal_darwin_relay_consumer_lag"
    lag = scalar(prom.query(f"max ({metric}{{{scope.sel()}}})"))
    if lag is None:
        results.append(Result("darwin lag", "skip", not_available(metric)))
    elif lag <= DARWIN_LAG_MAX:
        results.append(Result("darwin lag", "ok", f"{lag:g} messages"))
    else:
        results.append(
            Result("darwin lag", "wait", f"{lag:g} messages, over {DARWIN_LAG_MAX}")
        )
    return results


def check_duplicates(
    prom: Prometheus, scope: Scope, baseline: Baseline, now: float
) -> list[Result]:
    """Duplicates the deploy caused: information only, never a failure."""
    window = since(baseline, now)
    results = []
    for name, metric in (
        (
            "replayed by full-coverage",
            "distant_signal_full_coverage_consumer_startup_replay_entries_total",
        ),
        ("re-delivered entries", "distant_signal_movement_feed_long_pending_total"),
    ):
        got = scalar(prom.query(f"sum (increase({metric}{{{scope.sel()}}}[{window}]))"))
        if got is None:
            results.append(Result(name, "skip", not_available(metric)))
        else:
            results.append(
                Result(
                    name,
                    "info",
                    f"{got:.0f} since the baseline (duplicates are harmless)",
                )
            )
    return results


def run_checks(
    prom: Prometheus, scope: Scope, baseline: Baseline, now: float
) -> list[Result]:
    """Every check, gap signals first."""
    return [
        *check_gaps(prom, scope, baseline, now),
        *check_dead_letters(prom, scope, baseline, now),
        *check_poller_cronjobs(prom, scope, baseline),
        *check_deployments(prom, scope),
        *check_pollers(prom, scope, baseline, now),
        *check_relay(prom, scope, now),
        check_relay_kafka_lag(prom, scope, baseline),
        *check_groups(prom, scope, baseline),
        *check_streams(prom, scope),
        *check_duplicates(prom, scope, baseline, now),
    ]


def report(results: Sequence[Result], *, final: bool) -> None:
    """Print one line per check; a `wait` at the deadline prints as FAIL."""
    labels = {
        "ok": "OK  ",
        "fail": "FAIL",
        "wait": "WAIT",
        "skip": "SKIP",
        "info": "INFO",
    }
    for r in results:
        label = "FAIL" if final and r.status == "wait" else labels[r.status]
        print(f"{label} {r.name}: {r.detail}")


@dataclass(frozen=True)
class Polling:
    """How long verify waits, how often it looks, and its clock."""

    wait_secs: float
    interval_secs: float
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def verify(prom: Prometheus, scope: Scope, baseline: Baseline, poll: Polling) -> int:
    """Poll the checks until all pass, a gap shows, or the wait runs out."""
    deadline = poll.clock() + poll.wait_secs
    while True:
        now = poll.clock()
        results = run_checks(prom, scope, baseline, now)
        failed = [r for r in results if r.status == "fail"]
        waiting = [r for r in results if r.status == "wait"]
        if failed or not waiting or now >= deadline:
            report(results, final=True)
            if failed:
                print(f"verify: {len(failed)} check(s) found a gap or loss")
                return 1
            if waiting:
                minutes = f"{poll.wait_secs / 60:g} minutes"
                print(f"verify: {len(waiting)} check(s) had not recovered in {minutes}")
                return 1
            print("verify: ok")
            return 0
        names = ", ".join(r.name for r in waiting)
        print(f"waiting on {len(waiting)} check(s): {names}", flush=True)
        poll.sleep(min(poll.interval_secs, max(0.0, deadline - now)))


# --- command line ---------------------------------------------------------------


def parse_time(text: str) -> float:
    """Parse a unix timestamp or an ISO 8601 time (UTC unless it says otherwise)."""
    try:
        return float(text)
    except ValueError:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.timestamp()


def load_components(path: pathlib.Path) -> list[str]:
    """Return the components of GROUPS, from components.yaml."""
    data = cast("dict[str, object]", yaml.safe_load(path.read_text()) or {})
    mapping = cast("dict[str, str]", data.get("components") or {})
    return sorted(c for c, g in mapping.items() if g in GROUPS)


def authorization(args: argparse.Namespace) -> str | None:
    """Return the Authorization header from the options or the environment."""
    token_file = cast("pathlib.Path | None", args.bearer_token_file)
    basic_file = cast("pathlib.Path | None", args.basic_auth_file)
    if token_file is not None:
        return f"Bearer {token_file.read_text().strip()}"
    if basic_file is not None:
        pair = basic_file.read_text().strip().encode()
        return f"Basic {base64.b64encode(pair).decode()}"
    token = os.environ.get("PROMETHEUS_BEARER_TOKEN")
    return f"Bearer {token}" if token else None


def parser() -> argparse.ArgumentParser:
    """Return the command line parser."""
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    sub = p.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL"))
    auth = common.add_mutually_exclusive_group()
    auth.add_argument("--bearer-token-file", type=pathlib.Path)
    auth.add_argument(
        "--basic-auth-file", type=pathlib.Path, help="a file holding user:password"
    )
    common.add_argument(
        "--ca-file", type=pathlib.Path, help="CA bundle for an https Prometheus"
    )
    common.add_argument("--namespace", default="distant-signal")
    common.add_argument(
        "--fullname",
        default="distant-signal",
        help="the release's resource-name prefix",
    )
    common.add_argument("--components", type=pathlib.Path, default=COMPONENTS)
    common.add_argument(
        "--record", type=pathlib.Path, help="write every query and answer here (JSON)"
    )
    base = sub.add_parser("baseline", parents=[common], help="before the deploy")
    base.add_argument("--out", type=pathlib.Path, required=True)
    ver = sub.add_parser("verify", parents=[common], help="after the deploy")
    when = ver.add_mutually_exclusive_group(required=True)
    when.add_argument("--baseline", type=pathlib.Path, help="a file from `baseline`")
    when.add_argument(
        "--before",
        type=parse_time,
        help="read the baseline from Prometheus at this time",
    )
    ver.add_argument("--wait-minutes", type=float, default=15.0)
    ver.add_argument("--interval-seconds", type=float, default=30.0)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = parser().parse_args(argv)
    url = cast("str | None", args.prometheus_url)
    if not url:
        print("--prometheus-url (or PROMETHEUS_URL) is required", file=sys.stderr)
        return 2
    ca_file = cast("pathlib.Path | None", args.ca_file)
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else None
    record: list[dict[str, object]] | None = [] if args.record else None
    prom = HttpPrometheus(url, authorization(args), context=context, record=record)
    scope = Scope(
        namespace=cast("str", args.namespace),
        fullname=cast("str", args.fullname),
        components=load_components(cast("pathlib.Path", args.components)),
    )
    try:
        if args.mode == "baseline":
            now = time.time()
            baseline = take_baseline(prom, scope, None, now)
            out = cast("pathlib.Path", args.out)
            out.write_text(json.dumps(baseline.to_json(), indent=2) + "\n")
            print(f"baseline written to {out}; the checks as they stand:")
            report(run_checks(prom, scope, baseline, now), final=False)
            status = 0
        else:
            baseline_file = cast("pathlib.Path | None", args.baseline)
            if baseline_file is not None:
                baseline = Baseline.from_json(json.loads(baseline_file.read_text()))
            else:
                baseline = take_baseline(
                    prom, scope, cast("float", args.before), time.time()
                )
            status = verify(
                prom,
                scope,
                baseline,
                Polling(
                    wait_secs=cast("float", args.wait_minutes) * 60,
                    interval_secs=cast("float", args.interval_seconds),
                ),
            )
    except QueryError as err:
        print(f"Prometheus query failed: {err}", file=sys.stderr)
        status = 2
    if record is not None:
        cast("pathlib.Path", args.record).write_text(
            json.dumps(record, indent=1) + "\n"
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
