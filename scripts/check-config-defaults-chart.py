#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal against what each service says it reads.

  uv run scripts/check-config-defaults-chart.py --bin-dir target/debug
  uv run scripts/check-config-defaults-chart.py --images
  uv run scripts/check-config-defaults-chart.py --defaults-dir DIR
  uv run scripts/check-config-defaults-chart.py --list-services

Every service binary prints the environment it reads, with each
variable's default, and the constants the chart must agree with, when run
as `<service> --print-config-defaults` (ds_runtime::service_args, repo
split spec §9 phase 3 item 7). It connects to nothing and needs no
settings. This script gets that JSON for every service in SERVICES and
compares it with the chart, so no crate test reads chart files:

  - `--bin-dir DIR` runs `DIR/<service>`: the monorepo's CI, on the
    binaries it has just built;
  - `--images` runs `docker run --rm <image> --print-config-defaults` on
    the image the chart renders for each service, i.e. the tag and digest
    values.yaml pins. This is the mode for ds-deploy once the services are
    extracted: the chart is then checked against the images it deploys;
  - `--defaults-dir DIR` reads `DIR/<service>.json`, saved earlier.

`--list-services` prints SERVICES, one per line, for CI to build.

The chart is rendered several ways (RENDERS); a container belongs to the
service whose name is its image's last path segment, and only containers
that run the image's own entrypoint (no `command:`) are compared. Checks:

  - stale: every env var a container sets is one its service reads, or a
    PROCESS_ENV variable (read by the runtime, libc or libpq, not by any
    one service's config). A var the service doesn't read silently does
    nothing (notifier's LOG_LEVEL was set as RUST_LOG for weeks);
  - unwired: every variable a service reads is set on its container in at
    least one render, unless UNWIRED lists it with the reason. Otherwise a
    chart value can never reach it (notifier's sweep intervals, 2026-09);
  - defaults: in the `chart defaults` render, a literal env value that
    differs from the service's own default must be listed in OVERRIDES,
    with both values. So a default changed on either side alone fails here
    and gets a decision;
  - contract: each `contract` key the services print has its check
    (CONTRACT_CHECKS): the operations DistantSignalConsumerApiCallsFailing
    sums, values.yaml's per-stream alert caps, each service's Postgres role
    under per-service roles, and the probe paths a service needs;
  - coverage: every service renders in the `every workload` render.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm),
PyYAML (pyproject.toml's lint group), and docker for --images.
"""

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
PREREQS = CHART / "ci" / "ingest-prereqs.yaml"
RELEASE = "distant-signal"
POD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
FLAG = "--print-config-defaults"

# Every service binary that prints its defaults: the image's entrypoint,
# and the image's name under images/.
SERVICES = (
    "aggregator",
    "api",
    "darwin-relay",
    "enricher",
    "full-coverage-consumer",
    "ingest-writer",
    "movement-relay",
    "notifier",
    "poller-incidents",
    "poller-irish-rail-gtfs",
    "poller-irish-rail-live",
    "poller-ldbws",
    "poller-nir-stations",
    "poller-stations",
    "poller-tfl",
    "poller-tocs",
    "poller-transit-schedule",
    "schedule-ingest",
    "schedule-reference",
    "trust-backlog-consumer",
    "trust-consumer",
)

# Read by the runtime or libc rather than a service's config: tracing's
# filter, the log format and glibc's malloc tunables. (A variable another
# env value expands as `$(NAME)`, such as the PGPASSWORD in DATABASE_URL,
# is Kubernetes' input, not the service's, and is never stale either.)
PROCESS_ENV = frozenset(
    {"RUST_LOG", "LOG_FORMAT", "MALLOC_ARENA_MAX", "MALLOC_MMAP_THRESHOLD_"}
)
EXPANDED = re.compile(r"\$\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)")

type Doc = dict[str, object]


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# What the chart requires of every render.
REQUIRED = sets(
    "trustConsumer.kafka.brokers=k:9094",
    "trustConsumer.kafka.topic=t",
    "trustConsumer.kafka.saslMechanism=PLAIN",
    "enricher.llm.baseUrl=http://l/v1",
    "enricher.llm.model=m",
    "api.sso.issuerUrl=https://sso.example.com",
    "api.sso.clientId=c",
    "api.sso.clientSecret=s",
    "api.sso.redirectUrl=https://app.example.com/cb",
    "api.sso.postLoginRedirectUrl=https://app.example.com/",
)
# Every workload on, with every switch that adds env (the ingest
# prerequisites, every poller and relay with its Redis user and stream,
# schedulefeed's bucket source, the cold archive, the alerts).
EVERY = (
    "-f",
    str(PREREQS),
    *REQUIRED,
    *sets(
        "metrics.prometheusRule.enabled=true",
        "postgresql.roles.perService.writer.connect=true",
        "postgresql.roles.perService.api.connect=true",
        "postgresql.roles.perService.aggregator.connect=true",
        "postgresql.roles.perService.enricher.connect=true",
        "postgresql.roles.perService.notifier.connect=true",
        "api.backgroundLoops=false",
        "api.migrateOnStartup=false",
        "migrate.job.enabled=true",
        "pollers.incidents.enabled=true",
        "pollers.incidents.baseUrl=https://incidents.example.com",
        "pollers.stations.enabled=true",
        "pollers.stations.baseUrl=https://stations.example.com",
        "pollers.ldbws.enabled=true",
        "pollers.ldbws.baseUrl=https://ldbws.example.com",
        "pollers.ldbws.hourlyRequestBudget=900",
        "pollers.ldbws.samplePinnedLinesOnly=true",
        "pollers.ldbws.sampleMaxStations=150",
        "pollers.ldbws.trackedStops.enabled=true",
        "pollers.ldbws.trackedStopArrivals.enabled=true",
        "pollers.ldbws.trackedStopArrivals.secretName=ldbws-arrivals",
        "pollers.tfl.enabled=true",
        "pollers.tfl.stops.enabled=true",
        "ingestWriter.streams.tfl-stops=apply",
        "pollers.tocs.enabled=true",
        "pollers.tocs.baseUrl=https://tocs.example.com",
        "redis.acl.clients.pollerIncidents=true",
        "redis.acl.clients.pollerLdbws=true",
        "redis.acl.clients.pollerTfl=true",
        "redis.acl.clients.pollerTocs=true",
        "pollerIrishRailGtfs.enabled=true",
        "pollerIrishRailLive.enabled=true",
        "pollerNirStations.enabled=true",
        "redis.acl.clients.pollerIrishRailGtfs=true",
        "redis.acl.clients.pollerIrishRailLive=true",
        "redis.acl.clients.pollerNirStations=true",
        "ingestWriter.streams.ioi-gtfs=apply",
        "ingestWriter.streams.ioi-live=apply",
        "ingestWriter.streams.ioi-nir=apply",
        "pollerTransitSchedule.networks.metrolink.enabled=true",
        "redis.acl.clients.pollerTransitMetrolink=true",
        "ingestWriter.streams.transit-metrolink=apply",
        "pollerTransitSchedule.networks.net.enabled=true",
        "pollerTransitSchedule.networks.net.adapter=txc",
        "pollerTransitSchedule.networks.net.txcUrl=https://txc.example.com/net.zip",
        "pollerTransitSchedule.networks.net.txcCredentials.existingSecret=tnds",
        "redis.acl.clients.pollerTransitNet=true",
        "ingestWriter.streams.transit-net=apply",
        "pollerTransitSchedule.networks.tfl.enabled=true",
        "redis.acl.clients.pollerTransitTfl=true",
        "ingestWriter.streams.transit-tfl=apply",
        "darwinRelay.enabled=true",
        "redis.acl.clients.darwinRelay=true",
        "ingestWriter.streams.darwin=apply",
        "scheduleFeed.enabled=true",
        "scheduleFeed.sftp.authMethod=password",
        "scheduleFeed.bucket.enabled=true",
        "scheduleFeed.bucket.name=example-ds-ingest",
        "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
        "scheduleFeed.bucket.baseUrl=http://gcs.example.com",
        "scheduleFeed.bucket.auditLogs.ship=true",
        "scheduleFeed.bucket.auditLogs.bucket=example-ds-ingest-audit",
        "scheduleFeed.bucket.notifications.pubsub.enabled=true",
        "scheduleFeed.bucket.notifications.pubsub.subscription=projects/example-project/subscriptions/ds-ingest-events",
        "scheduleFeed.bucket.notifications.pubsub.baseUrl=http://pubsub.example.com",
        "archive.enabled=true",
        "archive.s3.bucket=archive-bucket",
        "archive.s3.existingSecret=archive-creds",
        "archive.s3.lifecycleConfirmed=true",
        "archive.s3.prefix=cluster/distant-signal-archive",
        "archive.expiry.enabled=true",
    ),
)
# The enricher on Claude, in batch mode, with keyless auth: the LLM_* and
# ANTHROPIC_* settings the OpenAI default leaves out.
CLAUDE = (
    "-f",
    str(PREREQS),
    *REQUIRED,
    *sets(
        "enricher.llm.provider=anthropic",
        "enricher.llm.auth=anthropicWifAuthentik",
        "enricher.serviceAccount.create=true",
        "enricher.llm.workloadIdentity.anthropic.organizationId=org-uuid",
        "enricher.llm.workloadIdentity.anthropic.serviceAccountId=svac_1",
        "enricher.llm.workloadIdentity.anthropic.federationRuleId=fdrl_1",
        "enricher.llm.workloadIdentity.anthropic.workspaceId=wrkspc_1",
        "enricher.llm.workloadIdentity.authentik.tokenUrl=https://sso.example.com/token/",
        "enricher.llm.workloadIdentity.authentik.clientId=ds-enricher-anthropic",
        "enricher.llm.batch.sweepMode=batch",
        "enricher.llm.batch.minItems=5",
        "enricher.llm.batch.maxItems=300",
        "enricher.llm.batch.pollIntervalSecs=120",
    ),
)
# schedulefeed's bucket source with keyless auth: GOOGLE_APPLICATION_CREDENTIALS.
KEYLESS_BUCKET = (
    "-f",
    str(PREREQS),
    *REQUIRED,
    *sets(
        "scheduleFeed.enabled=true",
        "scheduleFeed.sftp.authMethod=password",
        "scheduleFeed.bucket.enabled=true",
        "scheduleFeed.bucket.name=example-ds-ingest",
        "scheduleFeed.bucket.auth=workloadIdentity",
        "scheduleFeed.bucket.workloadIdentity.credentialConfigMap=ds-ingest-gcp-wif",
        "scheduleFeed.serviceAccount.create=true",
    ),
)
DEFAULTS_RENDER = "chart defaults"
EVERY_RENDER = "every workload"
RENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # The least a fresh install adds (the chart refuses to render without
    # the ingest prerequisites since release B).
    (DEFAULTS_RENDER, ("-f", str(PREREQS), *REQUIRED)),
    (EVERY_RENDER, EVERY),
    ("enricher on Claude", CLAUDE),
    ("schedulefeed keyless bucket", KEYLESS_BUCKET),
)


def no_value(values_key: str) -> str:
    """Return the reason for a setting the chart has no value for."""
    return f"no chart value: the default applies; {values_key}.extraEnv overrides it"


def optional(value: str) -> str:
    """Return the reason for a setting rendered only with an optional value."""
    return f"rendered only when {value} is set"


LINES = "a local override: the image embeds the line catalogue (ds-lines)"
POOL = "the pool size stays the service's own default"
STANOX = "the image's bundled reference-data/stanox-crs.csv"
KAFKA_GONE = "R-101: kafka refuses to start; the Redis stream is the only backend"

# Variables a service reads that no render sets, with the reason. A new
# variable fails until it is wired or listed here.
UNWIRED: Mapping[str, Mapping[str, str]] = {
    "aggregator": {
        "ARCHIVE_PROTECTED_PREFIXES": optional("archive.expiry.protectedPrefixes"),
        "ARCHIVE_S3_ENDPOINT": optional("archive.s3.endpoint"),
        "DATABASE_MAX_CONNECTIONS": POOL,
        "FULL_COVERAGE_LINE_STATS_RETENTION_DAYS": no_value("aggregator"),
        "LINES_DIR": LINES,
        "SCHEDULE_DERIVED_PRODUCTS_RETENTION_DAYS": no_value("aggregator"),
        "SCHEDULE_LINE_POPULATION_RETENTION_DAYS": no_value("aggregator"),
    },
    "api": {
        "LINES_DIR": LINES,
        "SCHEDULE_MATCH_INTERVAL_SECS": "unwired on purpose (its doc in config.rs)",
    },
    "darwin-relay": {"DARWIN_STREAM_MAXLEN": no_value("darwinRelay")},
    "enricher": {
        "DATABASE_MAX_CONNECTIONS": POOL,
        "LLM_ADVERSARIAL_MODEL": optional("enricher.llm.adversarialModel"),
        "LLM_ADVERSARIAL_PROFILE": optional("enricher.llm.adversarialProfile"),
        "LLM_ANTHROPIC_VERSION": no_value("enricher"),
        "LLM_AUTHENTIK_SCOPE": optional(
            "enricher.llm.workloadIdentity.authentik.scope"
        ),
        "LLM_BATCH_SWEEP_INTERVAL_SECS": optional(
            "enricher.llm.batch.sweepIntervalSecs"
        ),
        "LLM_GATEWAY_RETRIES": no_value("enricher"),
        "LLM_MAX_IN_FLIGHT": no_value("enricher"),
        "LLM_MAX_TOKENS": no_value("enricher"),
        "LLM_MODE": optional("enricher.llm.batch.mode (not normal)"),
        "LLM_OPENAI_PROMPT_CACHE": optional("enricher.llm.openaiPromptCache"),
        "LLM_PROFILE": optional("enricher.llm.profile"),
        "LLM_PROMPTS_DIR": optional("enricher.llm.prompts.configMap"),
        "LLM_RATE_LIMIT_RETRIES": no_value("enricher"),
        "LLM_RATE_LIMIT_RETRY_SECS": no_value("enricher"),
        "LLM_REASONING_EFFORT": optional("enricher.llm.reasoningEffort"),
        "LLM_TEMPERATURE": optional("enricher.llm.temperature"),
        "LLM_THINKING": optional("enricher.llm.anthropic.thinking"),
        "LLM_TOKEN_REFRESH_SKEW_SECS": no_value("enricher"),
        "LLM_TOP_P": optional("enricher.llm.topP"),
        "OPENAI_IDENTITY_PROVIDER_ID": optional("enricher.llm.auth=openaiWif*"),
        "OPENAI_SERVICE_ACCOUNT_ID": optional("enricher.llm.auth=openaiWif*"),
    },
    "full-coverage-consumer": {
        "LINES_DIR": LINES,
        "MOVEMENT_FEED_BACKEND": KAFKA_GONE,
        "POPULATION_INITIAL_WAIT_SECS": no_value("fullCoverageConsumer"),
    },
    "ingest-writer": {
        "INGEST_WRITER_CANARY_INTERVAL_SECS": no_value("ingestWriter"),
        "INGEST_WRITER_CHANGED_ROWS_ONLY": optional("ingestWriter.changedRowsOnly"),
        "INGEST_WRITER_CORPUS_CROSSWALK_INTERVAL_SECS": no_value("ingestWriter"),
        "INGEST_WRITER_OUTBOX_MAX_ATTEMPTS": no_value("ingestWriter"),
        "INGEST_WRITER_OUTBOX_REJECTED_RETENTION_DAYS": no_value("ingestWriter"),
        "INGEST_WRITER_TRAIN_EVENT_OUTBOX_INTERVAL_SECS": no_value("ingestWriter"),
        "LINES_DIR": LINES,
        "SCHEDULE_MATCH_INTERVAL_SECS": "the api's variable, unwired as the api's is",
    },
    "notifier": {"DATABASE_MAX_CONNECTIONS": POOL},
    "poller-incidents": {
        "INCIDENTS_ROW_HEARTBEAT": optional(
            "pollers.incidents.ingest.rowHeartbeat=false"
        ),
        "LINES_DIR": LINES,
    },
    "poller-irish-rail-live": {
        "STATION_CODES_OVERRIDE": optional("pollerIrishRailLive.stationCodesOverride"),
    },
    "poller-ldbws": {"LINES_DIR": LINES},
    "schedule-ingest": {
        "MAX_EXTRACTED_BYTES": no_value("scheduleFeed.ingest"),
        "MAX_ZIP_ENTRIES": no_value("scheduleFeed.ingest"),
    },
    "schedule-reference": {"LINES_DIR": LINES},
    "trust-backlog-consumer": {"LINES_DIR": LINES, "STANOX_CRS_FILE": STANOX},
    "trust-consumer": {"MOVEMENT_FEED_BACKEND": KAFKA_GONE, "STANOX_CRS_FILE": STANOX},
}

# The Redis Service the chart names, where the code defaults to `redis`.
REDIS = ("redis://distant-signal-redis:6379", "redis://redis:6379")
# Chart values (in the `chart defaults` render, which includes the ingest
# prerequisites) that deliberately differ from the service's own default,
# as (rendered value, service default): either side changing alone fails.
OVERRIDES: Mapping[tuple[str, str], tuple[str, str]] = {
    ("aggregator", "FULL_COVERAGE_ENABLED_DEFAULT"): ("true", "false"),
    ("api", "FULL_COVERAGE_ENABLED_DEFAULT"): ("true", "false"),
    ("full-coverage-consumer", "FULL_COVERAGE_LINE_MEMBERSHIP"): ("scope", "legacy"),
    ("full-coverage-consumer", "REDIS_URL"): REDIS,
    ("ingest-writer", "INGEST_WRITER_LOOPS"): ("true", "false"),
    ("ingest-writer", "PROGRESS_STALL_SECS"): ("900", "1800"),
    ("movement-relay", "MOVEMENT_STREAM_MAXLEN"): ("1048576", "524288"),
    ("movement-relay", "REDIS_URL"): REDIS,
    ("notifier", "PROGRESS_STALL_SECS"): ("900", "1800"),
    ("trust-backlog-consumer", "REDIS_URL"): REDIS,
    ("trust-consumer", "REDIS_URL"): REDIS,
}

# The PrometheusRule record DistantSignalConsumerApiCallsFailing alerts on.
CONSUMER_API_ERRORS = "distant_signal:consumer_api_errors:increase"
OPERATIONS = re.compile(
    r'operation=~"(?P<ops>[^"]*)"\}[^\n]*?"consumer", "(?P<consumer>[^"]+)"'
)
DATABASE_USER = re.compile(r"^postgres(?:ql)?://(?P<user>[^:@/]+)[:@]")


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


@dataclass(frozen=True)
class Defaults:
    """One service's `--print-config-defaults` output."""

    service: str
    # Env var -> its default (None: no default, or none it can report).
    env: Mapping[str, str | None]
    contract: Mapping[str, object]


def parse_defaults(service: str, text: str) -> Defaults:
    """Parse `service`'s `--print-config-defaults` JSON."""
    data = as_map(json.loads(text))
    if data.get("service") != service:
        msg = f"{service}: --print-config-defaults names {data.get('service')!r}"
        raise ValueError(msg)
    env: dict[str, str | None] = {}
    for name, spec in as_map(data.get("env")).items():
        default = as_map(spec).get("default")
        env[name] = None if default is None else str(default)
    return Defaults(service, env, as_map(data.get("contract")))


@dataclass
class Container:
    """A rendered container running a service image's own entrypoint."""

    where: str
    service: str
    image: str
    # Env var -> its literal value (None: from a Secret or ConfigMap).
    env: dict[str, str | None] = field(default_factory=dict)
    # Probe kind (`livenessProbe`, ...) -> its httpGet path.
    probes: dict[str, str] = field(default_factory=dict)


def pod_templates(doc: Mapping[str, object]) -> Iterator[tuple[str, Doc]]:
    """Yield (where, pod template) for each pod template in `doc`."""
    if doc.get("kind") not in POD_KINDS:
        return
    name = f"{doc.get('kind')}/{as_map(doc.get('metadata')).get('name')}"
    spec = as_map(doc.get("spec"))
    if "template" in spec:
        yield name, as_map(spec["template"])
    job_spec = as_map(as_map(spec.get("jobTemplate")).get("spec"))
    if "template" in job_spec:
        yield f"{name} jobTemplate", as_map(job_spec["template"])


def image_service(image: str) -> str:
    """Return the service an image reference names: its last path segment."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def service_containers(docs: Sequence[Doc]) -> list[Container]:
    """Return every container that runs a SERVICES image's entrypoint."""
    found: list[Container] = []
    for doc in docs:
        for where, template in pod_templates(doc):
            spec = as_map(template.get("spec"))
            for raw in as_list(spec.get("containers")):
                c = as_map(raw)
                image = str(c.get("image", ""))
                service = image_service(image)
                # A `command:` runs another binary in the image (api's
                # maintenance, the migrate Job): not this service's config.
                if service not in SERVICES or "command" in c:
                    continue
                container = Container(f"{where} [{c.get('name')}]", service, image)
                for entry in as_list(c.get("env")):
                    e = as_map(entry)
                    value = e.get("value")
                    container.env[str(e.get("name"))] = (
                        None if value is None else str(value)
                    )
                for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
                    path = as_map(as_map(c.get(probe)).get("httpGet")).get("path")
                    if path is not None:
                        container.probes[probe] = str(path)
                found.append(container)
    return found


def stale(
    containers: Sequence[Container], defaults: Mapping[str, Defaults]
) -> Iterator[str]:
    """Yield each env var a container sets that its service doesn't read."""
    for c in containers:
        read = defaults[c.service].env
        expanded = {
            m["name"]
            for value in c.env.values()
            for m in EXPANDED.finditer(value or "")
        }
        for name in sorted(c.env):
            if name not in read and name not in PROCESS_ENV | expanded:
                yield (
                    f"{c.where}: sets {name}, which {c.service} does not read "
                    "(so its value does nothing)"
                )


def unwired(
    containers: Sequence[Container],
    defaults: Mapping[str, Defaults],
    allow: Mapping[str, Mapping[str, str]] = UNWIRED,
) -> Iterator[str]:
    """Yield each variable a service reads that no render ever sets.

    Unless `allow` lists it; and each `allow` entry that is no longer true.
    """
    rendered: dict[str, set[str]] = {}
    for c in containers:
        rendered.setdefault(c.service, set()).update(c.env)
    for service in sorted(rendered):
        allowed = allow.get(service, {})
        for name in sorted(defaults[service].env):
            if name in rendered[service]:
                if name in allowed:
                    yield f"{service}: UNWIRED lists {name}, but the chart now sets it"
            elif name not in allowed:
                yield (
                    f"{service}: reads {name}, but no render sets it, so no chart "
                    "value can reach it (wire it, or list it in UNWIRED with why)"
                )
        for name in sorted(set(allowed) - set(defaults[service].env)):
            yield f"{service}: UNWIRED lists {name}, which {service} no longer reads"


def overridden(
    containers: Sequence[Container],
    defaults: Mapping[str, Defaults],
    overrides: Mapping[tuple[str, str], tuple[str, str]] = OVERRIDES,
) -> Iterator[str]:
    """Yield each default-render value that differs from the code default.

    Unless `overrides` records exactly that (value, default) pair; and each
    `overrides` entry that is no longer true.
    """
    seen: set[tuple[str, str]] = set()
    for c in containers:
        read = defaults[c.service].env
        for name, value in sorted(c.env.items()):
            default = read.get(name)
            # No default (or an empty one, "none"): nothing to differ from.
            if value is None or not default:
                continue
            key = (c.service, name)
            seen.add(key)
            expected = overrides.get(key)
            if value == default:
                if expected is not None:
                    yield (
                        f"{c.where}: OVERRIDES lists {name}, but the chart now "
                        f"renders {c.service}'s own default {default!r}"
                    )
                continue
            if expected != (value, default):
                yield (
                    f"{c.where}: {name}={value!r} but {c.service}'s default is "
                    f"{default!r}; if the chart means to differ, record "
                    f"({value!r}, {default!r}) in OVERRIDES"
                )
    for key in sorted(set(overrides) - seen):
        if key[0] in {c.service for c in containers}:
            yield f"{key[0]}: OVERRIDES lists {key[1]}, which the render does not set"


def consumer_operations(docs: Sequence[Doc]) -> dict[str, set[str]]:
    """Return each consumer's operations in the CONSUMER_API_ERRORS record."""
    for doc in docs:
        if doc.get("kind") != "PrometheusRule":
            continue
        for group in as_list(as_map(doc.get("spec")).get("groups")):
            for rule in as_list(as_map(group).get("rules")):
                r = as_map(rule)
                if r.get("record") == CONSUMER_API_ERRORS:
                    return {
                        m["consumer"]: set(m["ops"].split("|"))
                        for m in OPERATIONS.finditer(str(r.get("expr", "")))
                    }
    return {}


def values_number_map(values: Mapping[str, object], *path: str) -> dict[str, int]:
    """Return the `{name: number}` map at `path` in values.yaml."""
    node: object = values
    for key in path:
        node = as_map(node).get(key)
    return {str(k): int(cast("int", v)) for k, v in as_map(node).items()}


@dataclass(frozen=True)
class Chart:
    """What the contract checks read: every render's docs and values.yaml."""

    renders: Mapping[str, Sequence[Doc]]
    values: Mapping[str, object]


def check_api_call_operations(
    service: str, value: object, chart: Chart
) -> Iterator[str]:
    """Check the consumer API-errors record sums exactly these operations."""
    ops = consumer_operations(chart.renders[EVERY_RENDER])
    want = {str(v) for v in as_list(value)}
    if ops.get(service) != want:
        yield (
            f"{service}: {CONSUMER_API_ERRORS} sums operations "
            f"{sorted(ops.get(service, set()))}, but {service}'s "
            f"API_CALL_OPERATIONS are {sorted(want)} (templates/prometheusrule.yaml)"
        )


def check_ingest_stream_maxlen(
    service: str, value: object, chart: Chart
) -> Iterator[str]:
    """Check values.yaml's stream alerts name exactly the budgeted streams."""
    want = {str(k): int(cast("int", v)) for k, v in as_map(value).items()}
    rule = ("metrics", "prometheusRule")
    maxlen = values_number_map(chart.values, *rule, "ingestStreamBacklog", "maxlen")
    for stream in sorted(set(maxlen) | set(want)):
        if maxlen.get(stream) != want.get(stream):
            yield (
                f"{service}: values.yaml metrics.prometheusRule.ingestStreamBacklog."
                f"maxlen[{stream!r}] is {maxlen.get(stream)}, but "
                f"ingest_stream::budget::INGEST_STREAMS caps it at {want.get(stream)}"
            )
    stall = values_number_map(
        chart.values, *rule, "ingestStreamStalled", "stallAfterSecs"
    )
    if set(stall) != set(want):
        yield (
            f"{service}: values.yaml metrics.prometheusRule.ingestStreamStalled."
            f"stallAfterSecs names {sorted(stall)}, but the budgeted streams are "
            f"{sorted(want)}"
        )
    yield from (
        f"{service}: ingestStreamStalled.stallAfterSecs.{s} must be positive"
        for s, secs in sorted(stall.items())
        if secs <= 0
    )


def check_db_role(service: str, value: object, chart: Chart) -> Iterator[str]:
    """Check the service connects as its own role under per-service roles."""
    user = f"distant_signal_{value}"
    seen = False
    for c in service_containers(chart.renders[EVERY_RENDER]):
        if c.service != service:
            continue
        seen = True
        match = DATABASE_USER.match(c.env.get("DATABASE_URL") or "")
        got = match["user"] if match else None
        if got != user:
            yield f"{c.where}: DATABASE_URL connects as {got}, not {user}"
    if not seen:
        yield f"{service}: not rendered, so its Postgres role is unchecked"


def check_probe_paths(service: str, value: object, chart: Chart) -> Iterator[str]:
    """Check the service's probes hit the health paths it needs."""
    want = {f"{kind}Probe": str(path) for kind, path in as_map(value).items()}
    for c in service_containers(chart.renders[EVERY_RENDER]):
        if c.service != service:
            continue
        for probe, path in sorted(want.items()):
            if c.probes.get(probe) != path:
                yield (
                    f"{c.where}: {probe} gets {c.probes.get(probe)}, but "
                    f"{service} needs {path}"
                )


CONTRACT_CHECKS: Mapping[str, Callable[[str, object, Chart], Iterator[str]]] = {
    "api_call_operations": check_api_call_operations,
    "ingest_stream_maxlen": check_ingest_stream_maxlen,
    "db_role": check_db_role,
    "probe_paths": check_probe_paths,
}


def contracts(defaults: Mapping[str, Defaults], chart: Chart) -> Iterator[str]:
    """Run every service's contract checks."""
    for service in sorted(defaults):
        for key, value in sorted(defaults[service].contract.items()):
            check = CONTRACT_CHECKS.get(key)
            if check is None:
                yield f"{service}: contract key {key!r} has no check in CONTRACT_CHECKS"
            else:
                yield from check(service, value, chart)


def check(defaults: Mapping[str, Defaults], chart: Chart) -> list[str]:
    """Return every failure."""
    failures: list[str] = []
    every = service_containers(chart.renders[EVERY_RENDER])
    failures.extend(
        f"{EVERY_RENDER}: {s} renders no container running its image"
        for s in SERVICES
        if s not in {c.service for c in every}
    )
    containers: list[Container] = []
    for name, docs in chart.renders.items():
        rendered = service_containers(docs)
        failures.extend(f"{name}: {f}" for f in stale(rendered, defaults))
        containers.extend(rendered)
    failures.extend(unwired(containers, defaults))
    failures.extend(
        f"{DEFAULTS_RENDER}: {f}"
        for f in overridden(
            service_containers(chart.renders[DEFAULTS_RENDER]), defaults
        )
    )
    failures.extend(contracts(defaults, chart))
    return failures


def render(helm: str, name: str, args: Sequence[str]) -> list[Doc]:
    """Render the chart with `args` (the render called `name`)."""
    done = subprocess.run(  # noqa: S603  # helm with the chart's own arguments
        [helm, "template", RELEASE, str(CHART), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if done.returncode:
        msg = f"render {name!r}: {done.stderr.strip()}"
        raise RuntimeError(msg)
    out = done.stdout
    return [as_map(d) for d in yaml.safe_load_all(out) if d]


def run_flag(command: Sequence[str]) -> str:
    """Run `command` (ending in FLAG) and return its stdout."""
    return subprocess.run(  # noqa: S603  # a service binary or docker, our own flag
        command, check=True, capture_output=True, text=True
    ).stdout


def load_defaults(
    args: argparse.Namespace, renders: Mapping[str, Sequence[Doc]]
) -> dict[str, Defaults]:
    """Get every service's defaults from the source `args` names."""
    images = {c.service: c.image for c in service_containers(renders[EVERY_RENDER])}
    out: dict[str, Defaults] = {}
    for service in SERVICES:
        if args.defaults_dir is not None:
            text = (args.defaults_dir / f"{service}.json").read_text(encoding="utf-8")
        elif args.bin_dir is not None:
            text = run_flag([str(args.bin_dir / service), FLAG])
        else:
            if service not in images:
                msg = f"{service}: the chart renders no image for it"
                raise ValueError(msg)
            text = run_flag([args.docker, "run", "--rm", images[service], FLAG])
        out[service] = parse_defaults(service, text)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Check the chart against every service's printed defaults."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--list-services", action="store_true", help="print SERVICES and exit"
    )
    source.add_argument("--bin-dir", type=pathlib.Path, help="run DIR/<service>")
    source.add_argument(
        "--images", action="store_true", help="docker run the images the chart pins"
    )
    source.add_argument(
        "--defaults-dir", type=pathlib.Path, help="read DIR/<service>.json"
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    parser.add_argument("--docker", default=shutil.which("docker") or "docker")
    args = parser.parse_args(argv)
    if args.list_services:
        print("\n".join(SERVICES))
        return 0

    renders = {name: render(args.helm, name, a) for name, a in RENDERS}
    defaults = load_defaults(args, renders)
    values = as_map(yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8")))
    failures = check(defaults, Chart(renders, values))
    for failure in failures:
        print(failure)
    if failures:
        return 1
    print(f"{len(SERVICES)} services agree with the chart in {len(RENDERS)} renders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
