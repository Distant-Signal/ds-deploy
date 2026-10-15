#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's ingest-architecture phase 0 switches.

  uv run scripts/check-ingest-phase0-chart.py [--helm HELM] [--baseline DIR]

Phase 0 added the per-service Postgres roles and the per-client Redis ACL
users (docs/postgres-app-role.md "Stage 0b", docs/redis-acl.md). Since
release B (ingest phase 5) every ingest producer writes Postgres as its own
role or XADDs to a Redis stream as its own user, so both are prerequisites
at their last rollout step rather than switches: every render here starts
from ci/ingest-prereqs.yaml.

Per-service Postgres roles (`postgresql.roles.perService`):
  - `roles.enabled`, `setupJob.enabled` or `perService.enabled` off refuses
    to render (the release B preflight names it); so does a non-numeric
    connectionLimit;
  - the setup Job's own (hook) ConfigMap carries files/postgres-roles.sql
    and files/postgres-grants.sql verbatim and is what the Job mounts, and
    the Job runs postgres-grants.sql after postgres-roles.sql with each
    role's computed CONNECTION LIMIT; on a new cluster (`initScript`) the
    initdb script and the Postgres pod do too;
  - `connect` moves exactly that Deployment's DATABASE_URL to its own role
    (with its own password key), the api's pool to perService.api's 16, and
    takes its pool out of the app role's computed limit; the api's
    migrations stay the owner's; the producers and readers always connect
    as theirs;
  - the generated Secret has one password per service;
  - the role-limit budget (spec §6.6): one more connection than the 197
    slots (max_connections 200) fails the render, the 197 pass.

Per-client Redis ACL users (`redis.acl`):
  - `enabled` off, stage open or `defaultUser: on` refuses to render;
  - bad values refuse to render (no existingSecret, an unknown stage,
    `defaultUser: off` while a client still uses `default`);
  - Redis runs with --aclfile, probes as ds-admin, and each client connects
    with REDIS_USERNAME and its own REDIS_PASSWORD key; REDIS_URL stays
    credential-free;
  - the ConfigMap equals scripts/render-redis-acl.py's output for stage
    narrow and each default-user setting, with and without redis.auth.

Security review render guards (2026-10-08):
  - M3: ACL users with `default` on refuse to render (without redis.auth
    too: the preflight refuses `default` on altogether);
  - H3: every stream producer (pollers ldbws, tfl, tocs, poller-incidents'
    incident-text-changed, full-coverage-consumer, an island-of-Ireland
    poller) refuses to render without its own user, and renders as it; a
    writer stream on `apply` refuses while `default` is on;
  - H2: every narrow-role component refuses to render without its
    `perService.<role>.connect`, and renders connecting as its own role;
  - M4: `ingestWriter.streams.tfl: apply` refuses without
    `perService.writer.connect`.

Release B: a retired `ingest.sink`, `internalReads.source` or writer
`shadow` set to anything but its one value refuses to render; set to that
value it renders.

--baseline DIR renders DIR (a copy of charts/distant-signal, e.g. the merge
base) and this chart with the same values, for the defaults (plus
ci/ingest-prereqs.yaml), values-example.yaml and the NetworkPolicies:
every document must be identical, with Secret values masked (randAlphaNum
differs per run; the keys are compared). CI doesn't run it.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import difflib
import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
EXAMPLE = "values-example.yaml"
# Every ingest producer writes Postgres or a Redis stream (release B, ingest
# phase 5, removed the /private HTTP paths), which needs the Postgres
# per-service roles, the ingest-writer and the Redis ACL, so these checks
# render on ci/ingest-prereqs.yaml, the least a fresh install adds: every
# phase 0 switch on, at its last rollout step.
BASE = (
    "-f",
    str(CHART / "ci" / "ingest-prereqs.yaml"),
    "--set",
    "trustConsumer.kafka.brokers=k:9094",
    "--set",
    "trustConsumer.kafka.topic=t",
    "--set",
    "trustConsumer.kafka.saslMechanism=PLAIN",
    "--set",
    "enricher.llm.baseUrl=http://l/v1",
    "--set",
    "enricher.llm.model=m",
    "--set",
    "api.sso.issuerUrl=https://sso.example.com",
    "--set",
    "api.sso.clientId=c",
    "--set",
    "api.sso.clientSecret=s",
    "--set",
    "api.sso.redirectUrl=https://app.example.com/cb",
    "--set",
    "api.sso.postLoginRedirectUrl=https://app.example.com/",
)


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


SERVICES = ("api", "aggregator", "enricher", "notifier")
ALL_CONNECT = sets(*(f"postgresql.roles.perService.{s}.connect=true" for s in SERVICES))
AUTH = sets("redis.auth.enabled=true", "redis.auth.existingSecret=redis-auth")
# ci/ingest-prereqs.yaml's redis.acl clients: key -> (Deployment suffix, ACL user).
CLIENTS = {
    "enricher": ("-enricher", "enricher"),
    "movementRelay": ("-movement-relay", "movement-relay"),
    "trustConsumer": ("-trust-consumer", "trust-consumer"),
    "fullCoverageConsumer": ("-full-coverage-consumer", "full-coverage-consumer"),
    "trustBacklogConsumer": ("-trust-backlog-consumer", "trust-backlog-consumer"),
    "ingestWriter": ("-ingest-writer", "ingest-writer"),
}
# The clients of workloads off by default (the pollers, the island-of-Ireland
# pollers): defaultUser off still needs each of them on once enabled.
DORMANT_CLIENTS = (
    "pollerIncidents",
    "pollerLdbws",
    "pollerTfl",
    "pollerTocs",
    "pollerIrishRailGtfs",
    "pollerIrishRailLive",
    "pollerNirStations",
)
ALL_CLIENTS = sets(
    *(f"redis.acl.clients.{k}=true" for k in (*CLIENTS, *DORMANT_CLIENTS))
)
WRITER_LOOPS = sets("ingestWriter.enabled=true", "ingestWriter.loops.enabled=true")
SCHEDULE_FEED = sets(
    "scheduleFeed.enabled=true", "scheduleFeed.sftp.authMethod=password"
)


def poller(name: str, client: str | None = None) -> tuple[str, ...]:
    """Return the values that enable pollers.<name> (with its Redis user)."""
    pairs = [f"pollers.{name}.enabled=true", f"pollers.{name}.baseUrl=https://x/{name}"]
    if client:
        pairs.append(f"redis.acl.clients.{client}=true")
    return sets(*pairs)


BASELINE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ()),
    ("values-example.yaml", ("-f", EXAMPLE)),
    ("network policy", sets("networkPolicy.enabled=true")),
)

type Doc = dict[str, object]


class Checker:
    """Collects failed checks."""

    def __init__(self, helm: str) -> None:
        """Use `helm` to render."""
        self.helm = helm
        self.failures: list[str] = []

    def check(self, *, ok: bool, message: str) -> None:
        """Record `message` as a failure unless `ok`."""
        if not ok:
            self.failures.append(message)

    def render(self, *args: str, chart: pathlib.Path = CHART) -> tuple[int, str]:
        """Run `helm template` on `chart` with BASE and `args`."""
        args = tuple(str(chart / a) if a == EXAMPLE else a for a in args)
        result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "distant-signal", str(chart), *BASE, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def docs(self, *args: str, chart: pathlib.Path = CHART) -> list[Doc]:
        """Render and parse every non-empty YAML document."""
        code, out = self.render(*args, chart=chart)
        if code != 0:
            self.failures.append(f"render {list(args)} failed: {out.strip()}")
            return []
        return [cast("Doc", d) for d in yaml.safe_load_all(out) if d]

    def refuses(self, label: str, needle: str, *args: str) -> None:
        """Check that the render fails, naming `needle`."""
        code, out = self.render(*args)
        self.check(
            ok=code != 0 and needle in out,
            message=(
                f"{label}: must refuse to render naming {needle!r}: "
                f"{out.strip()[-300:]}"
            ),
        )


def name_of(doc: Doc) -> str:
    """Return `Kind/name` of a rendered resource."""
    return f"{doc.get('kind')}/{cast('dict[str, str]', doc['metadata'])['name']}"


def by_name(docs: Sequence[Doc]) -> dict[str, Doc]:
    """Index documents by `Kind/name`."""
    return {name_of(d): d for d in docs}


def pod(doc: Doc) -> dict[str, object]:
    """Return a workload's pod spec."""
    spec = cast("dict[str, object]", doc.get("spec") or {})
    if "jobTemplate" in spec:
        spec = cast("dict[str, object]", spec["jobTemplate"])
        spec = cast("dict[str, object]", spec.get("spec") or {})
    template = cast("dict[str, object]", spec.get("template") or {})
    return cast("dict[str, object]", template.get("spec") or {})


def containers(doc: Doc, key: str = "containers") -> list[dict[str, object]]:
    """Return a workload's containers (or initContainers)."""
    return cast("list[dict[str, object]]", pod(doc).get(key) or [])


def env(container: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return a container's env by name."""
    entries = cast("list[dict[str, object]]", container.get("env") or [])
    return {str(e["name"]): e for e in entries}


def value(entry: dict[str, object] | None) -> str:
    """Return an env entry's plain value ("" if none)."""
    return str((entry or {}).get("value", ""))


def secret_ref(entry: dict[str, object] | None) -> tuple[str, str]:
    """Return an env entry's (Secret name, key)."""
    source = cast("dict[str, object]", (entry or {}).get("valueFrom") or {})
    ref = cast("dict[str, str]", source.get("secretKeyRef") or {})
    return ref.get("name", ""), ref.get("key", "")


def deployment(docs: Sequence[Doc], suffix: str) -> Doc:
    """Return the Deployment whose name ends with `suffix`."""
    for doc in docs:
        if doc.get("kind") == "Deployment" and name_of(doc).endswith(suffix):
            return doc
    return {}


def main_env(docs: Sequence[Doc], suffix: str) -> dict[str, dict[str, object]]:
    """Return the first container's env of the Deployment ending `suffix`."""
    found = containers(deployment(docs, suffix))
    return env(found[0]) if found else {}


def normalise(docs: Sequence[Doc]) -> dict[str, str]:
    """Each document as YAML by `Kind/name`, with Secret values masked."""
    out: dict[str, str] = {}
    for doc in docs:
        kept = dict(doc)
        if doc.get("kind") == "Secret":
            for field in ("data", "stringData"):
                if isinstance(kept.get(field), dict):
                    data = cast("dict[str, object]", kept[field])
                    kept[field] = dict.fromkeys(data, "<masked>")
        out[name_of(doc)] = yaml.safe_dump(kept, sort_keys=True)
    return out


def differing(old: Sequence[Doc], new: Sequence[Doc]) -> dict[str, str]:
    """Return {`Kind/name`: unified diff} for every document that differs."""
    before, after = normalise(old), normalise(new)
    diffs: dict[str, str] = {}
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key, ""), after.get(key, "")
        if a != b:
            diffs[key] = "".join(
                difflib.unified_diff(
                    a.splitlines(keepends=True),
                    b.splitlines(keepends=True),
                    f"{key} (before)",
                    f"{key} (after)",
                )
            )
    return diffs


def check_baseline(c: Checker, base: pathlib.Path) -> None:
    """Every BASELINE_SETS render of `base` equals this chart's."""
    for label, args in BASELINE_SETS:
        diffs = differing(c.docs(*args, chart=base), c.docs(*args))
        c.failures.extend(f"{label}: {k} differs\n{d}" for k, d in diffs.items())
        if not diffs:
            print(f"baseline {label}: identical")


def check_roles_required(c: Checker) -> None:
    """Since release B the per-service roles are a prerequisite, not a switch."""
    for key in (
        "postgresql.roles.enabled",
        "postgresql.roles.setupJob.enabled",
        "postgresql.roles.perService.enabled",
    ):
        c.refuses(f"{key} off", f"{key}: true", *sets(f"{key}=false"))


def check_roles_refusals(c: Checker) -> None:
    """Invalid perService values refuse to render."""
    c.refuses(
        "a non-numeric connectionLimit",
        "connectionLimit must be a whole number",
        *sets("postgresql.roles.perService.enricher.connectionLimit=lots"),
    )


def check_roles_setup(c: Checker) -> None:
    """Check that the setup Job runs postgres-grants.sql after postgres-roles.sql."""
    docs = by_name(c.docs())
    # The Job's own hook copy: as a pre-upgrade hook it runs before the
    # release ConfigMap is updated (docs/postgres-app-role.md "Hook order").
    cm = docs.get("ConfigMap/distant-signal-postgres-roles-setup", {})
    data = cast("dict[str, str]", cm.get("data") or {})
    for name in ("postgres-roles.sql", "postgres-grants.sql"):
        text = (CHART / "files" / name).read_text(encoding="utf-8")
        c.check(
            ok=data.get(name) == text,
            message=f"the setup Job's ConfigMap must carry files/{name} verbatim",
        )
    job = docs.get("Job/distant-signal-postgres-roles-setup", {})
    c.check(
        ok="name: distant-signal-postgres-roles-setup\n" in yaml.safe_dump(job),
        message="the setup Job must mount its own ConfigMap",
    )
    command = cast("list[object]", (containers(job) or [{}])[0].get("command") or [])
    script = "".join(str(x) for x in command)
    for needle in (
        "postgres-roles.sql",
        "postgres-grants.sql",
        "'--variable=app=distant_signal_app'",
        "'--variable=api_connection_limit=34'",
        "'--variable=aggregator_connection_limit=11'",
        "'--variable=enricher_connection_limit=6'",
        "'--variable=notifier_connection_limit=6'",
        "'--variable=app_connection_limit=75'",
        "'--variable=writer_connection_limit=7'",
        "'--variable=trust_consumer_connection_limit=3'",
        "'--variable=trust_backlog_connection_limit=4'",
        "'--variable=full_coverage_ro_connection_limit=3'",
    ):
        c.check(ok=needle in script, message=f"setup Job script lacks {needle}")
    if "postgres-roles.sql" in script and "postgres-grants.sql" in script:
        c.check(
            ok=script.index("postgres-roles.sql") < script.index("postgres-grants.sql"),
            message="postgres-grants.sql must run after postgres-roles.sql",
        )
    job_env = env((containers(job) or [{}])[0])
    for service in SERVICES:
        ref = secret_ref(job_env.get(f"DS_PG_{service.upper()}_PASSWORD"))
        c.check(
            ok=ref == ("distant-signal", f"postgres-{service}-password"),
            message=f"setup Job DS_PG_{service.upper()}_PASSWORD from {ref}",
        )
    secret = docs.get("Secret/distant-signal", {})
    keys = set(cast("dict[str, object]", secret.get("data") or {}))
    for service in SERVICES:
        c.check(
            ok=f"postgres-{service}-password" in keys,
            message=f"generated postgres-{service}-password missing",
        )


def check_roles_connect(c: Checker) -> None:
    """`connect` moves exactly that service; the producers are always on theirs."""
    base = c.docs()
    docs = c.docs(*ALL_CONNECT)
    for service in SERVICES:
        url = value(main_env(docs, f"-{service}").get("DATABASE_URL"))
        c.check(
            ok=url.startswith(f"postgres://distant_signal_{service}:$(PGPASSWORD)@"),
            message=f"{service} must connect as distant_signal_{service}: {url}",
        )
        ref = secret_ref(main_env(docs, f"-{service}").get("PGPASSWORD"))
        c.check(
            ok=ref == ("distant-signal", f"postgres-{service}-password"),
            message=f"{service} PGPASSWORD from {ref}",
        )
    api = main_env(docs, "-api")
    c.check(
        ok=value(api.get("DATABASE_MAX_CONNECTIONS")) == "16",
        message="api pool must be perService.api.maxConnections (16)",
    )
    c.check(
        ok=value(api.get("MIGRATION_DATABASE_URL")).startswith(
            "postgres://distant_signal_owner:"
        ),
        message="api migrations must stay on the owner role",
    )
    _, out = c.render(*ALL_CONNECT)
    c.check(
        ok="'--variable=app_connection_limit=5'" in out,
        message="app's computed limit must drop to its slack (5) once all connect",
    )
    one = c.docs(*sets("postgresql.roles.perService.notifier.connect=true"))
    diffs = differing(base, one)
    moved = sorted(k for k in diffs if k.startswith("Deployment/"))
    c.check(
        ok=moved == ["Deployment/distant-signal-notifier"],
        message=f"notifier.connect must move only the notifier, moved {moved}",
    )
    c.check(
        ok=value(main_env(one, "-api").get("DATABASE_MAX_CONNECTIONS")) == "50",
        message="the api pool must stay 50 until the api connects as its own role",
    )
    for suffix, role in (
        ("-trust-consumer", "trust_consumer"),
        ("-trust-backlog-consumer", "trust_backlog"),
        ("-full-coverage-consumer", "full_coverage_ro"),
        ("-ingest-writer", "writer"),
    ):
        url = value(main_env(base, suffix).get("DATABASE_URL"))
        c.check(
            ok=url.startswith(f"postgres://distant_signal_{role}:"),
            message=f"{suffix[1:]} must connect as distant_signal_{role}: {url}",
        )


# An api CONNECTION LIMIT far over any budget, to read the other roles' sum
# off the render's error.
HUGE_API_LIMIT = 999


def check_roles_budget(c: Checker) -> None:
    """Role limits must fit the 197 non-superuser slots (max_connections 200)."""
    _, out = c.render(
        *ALL_CONNECT,
        *sets(f"postgresql.roles.perService.api.connectionLimit={HUGE_API_LIMIT}"),
    )
    found = re.search(r"sum to (\d+)", out)
    total = int(found.group(1)) if found else 0
    c.check(
        ok=total > HUGE_API_LIMIT,
        message=f"budget: an api limit of {HUGE_API_LIMIT} must fail: {out[-300:]}",
    )
    # The total less the api's: the largest api limit that still fits 197.
    rest = total - HUGE_API_LIMIT
    fits = 197 - rest
    for api_limit, ok in ((fits + 1, False), (fits, True)):
        code, out = c.render(
            *ALL_CONNECT,
            *sets(f"postgresql.roles.perService.api.connectionLimit={api_limit}"),
            "--show-only",
            "templates/api-deployment.yaml",
        )
        if ok:
            c.check(
                ok=code == 0, message=f"budget at 197/197 must render: {out[-300:]}"
            )
        else:
            c.check(
                ok=code != 0 and "sum to 198" in out,
                message=f"budget 198/197 must fail the render: {out[-300:]}",
            )


def check_roles_init_script(c: Checker) -> None:
    """Check that a new cluster's initdb script runs postgres-grants.sql."""
    docs = by_name(c.docs())
    cm = docs.get("ConfigMap/distant-signal-postgres-roles", {})
    init = str(
        cast("dict[str, str]", cm.get("data") or {}).get("10-distant-signal-roles.sh")
    )
    c.check(
        ok="postgres-grants.sql" in init and init.count("exec psql") == 1,
        message="the initdb script must run postgres-grants.sql last (one exec)",
    )
    sts = docs.get("StatefulSet/distant-signal-postgres", {})
    text = yaml.safe_dump(sts)
    c.check(
        ok="path: postgres-grants.sql" in text and "DS_PG_API_PASSWORD" in text,
        message="the Postgres pod must mount postgres-grants.sql and get the passwords",
    )


def load_render_acl() -> ModuleType:
    """Import scripts/render-redis-acl.py."""
    path = REPO / "scripts" / "render-redis-acl.py"
    spec = importlib.util.spec_from_file_location("render_redis_acl", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_redis_acl"] = module
    spec.loader.exec_module(module)
    return module


def check_redis_required(c: Checker) -> None:
    """Since release B the Redis ACL at its last step is a prerequisite."""
    c.refuses(
        "redis.acl off", "redis.acl.enabled: true", *sets("redis.acl.enabled=false")
    )
    c.refuses(
        "redis.acl stage open", "redis.acl.stage: narrow", *sets("redis.acl.stage=open")
    )
    c.refuses(
        "redis.acl defaultUser on",
        "redis.acl.defaultUser: off",
        *AUTH,
        *sets("redis.acl.defaultUser=on"),
    )


def check_redis_refusals(c: Checker) -> None:
    """Invalid redis.acl values refuse to render."""
    c.refuses(
        "acl without existingSecret",
        "redis.acl.existingSecret",
        *sets("redis.acl.existingSecret="),
    )
    c.refuses("an unknown stage", "redis.acl.stage", *sets("redis.acl.stage=wide"))
    c.refuses(
        "default off with a client on default",
        "redis.acl.clients.enricher",
        *sets("redis.acl.defaultUser=off", "redis.acl.clients.enricher=false"),
    )


def check_redis_users(c: Checker) -> None:
    """Redis reads the ACL file; each client connects as its own user."""
    docs = c.docs()
    redis = deployment(docs, "-redis")
    main = (containers(redis) or [{}])[0]
    args = [str(a) for a in cast("list[object]", main.get("args") or [])]
    c.check(
        ok="--aclfile" in args and "--requirepass" not in args,
        message=f"Redis args: {args}",
    )
    c.check(
        ok=secret_ref(env(main).get("REDISCLI_AUTH"))
        == ("example-redis-users", "ds-admin-password"),
        message="Redis probes must authenticate as ds-admin",
    )
    inits = containers(redis, "initContainers")
    init_env = env(inits[0]) if inits else {}
    c.check(
        ok=secret_ref(init_env.get("REDIS_ACL_PASSWORD_DEFAULT"))
        == ("example-redis-users", "default-password"),
        message="defaultUser on-unshared: default gets its own default-password",
    )
    c.check(
        ok=secret_ref(init_env.get("REDIS_ACL_PASSWORD_MOVEMENT_RELAY"))
        == ("example-redis-users", "movement-relay-password"),
        message="the initContainer must read each user's password key",
    )
    volumes = cast("list[dict[str, object]]", pod(redis).get("volumes") or [])
    acl_volume = next((v for v in volumes if v.get("name") == "acl"), {})
    c.check(
        ok=cast("dict[str, str]", acl_volume.get("emptyDir") or {}).get("medium")
        == "Memory",
        message="the rendered users.acl must live in a memory emptyDir",
    )
    for key, (suffix, user) in CLIENTS.items():
        e = main_env(docs, suffix)
        c.check(
            ok=value(e.get("REDIS_USERNAME")) == user
            and secret_ref(e.get("REDIS_PASSWORD"))
            == ("example-redis-users", f"{user}-password")
            and "@" not in value(e.get("REDIS_URL")),
            message=f"clients.{key}: REDIS_USERNAME {user} and its own password",
        )
    # Ingest phase 5, step 5.4: the api has no Redis client. A leftover
    # `redis.acl.clients.api` (on or off) is ignored, even with default off.
    api_env = main_env(docs, "-api")
    c.check(
        ok=not any(k.startswith("REDIS_") for k in api_env),
        message=f"the api must render no REDIS_* env: {sorted(api_env)}",
    )
    for on in ("true", "false"):
        renders(
            c,
            f"a leftover redis.acl.clients.api={on}",
            *ALL_CLIENTS,
            *sets("redis.acl.defaultUser=off", f"redis.acl.clients.api={on}"),
        )


def check_redis_acl_file(c: Checker) -> None:
    """Check the ConfigMap against scripts/render-redis-acl.py."""
    acl = load_render_acl()
    text = (CHART / "files" / "redis-users.acl.tpl").read_text(encoding="utf-8")
    # Stage narrow only: a stream producer refuses stage open. Default
    # on-unshared (its own password, with or without redis.auth) or off.
    for default_mode in ("on-unshared", "off"):
        for with_auth in (True, False):
            args = (
                *(AUTH if with_auth else ()),
                *sets(f"redis.acl.defaultUser={default_mode}"),
                *(ALL_CLIENTS if default_mode == "off" else ()),
            )
            cm = by_name(c.docs(*args)).get("ConfigMap/distant-signal-redis-acl", {})
            got = cast("dict[str, str]", cm.get("data") or {}).get("users.acl.tpl")
            want = acl.render(
                text,
                stage="narrow",
                default_user=default_mode != "off",
                default_password=True,
            )
            c.check(
                ok=got == want,
                message=(
                    f"ACL ConfigMap (stage narrow, default {default_mode}, "
                    f"auth {with_auth}) differs from scripts/render-redis-acl.py"
                ),
            )


def check_optional_acl_users(c: Checker) -> None:
    """Check an `optional` user renders only while its client is on (transit §0).

    poller-transit-metrolink ships off: by default neither its ACL line nor
    its password env (a Secret key Redis would need) renders, so merging it
    restarts nothing. With its client on, both render, and the ConfigMap
    still equals scripts/render-redis-acl.py's with `--client`.
    """
    acl = load_render_acl()
    text = (CHART / "files" / "redis-users.acl.tpl").read_text(encoding="utf-8")
    var = "REDIS_ACL_PASSWORD_POLLER_TRANSIT_METROLINK"
    for on in (False, True):
        args = sets(f"redis.acl.clients.pollerTransitMetrolink={str(on).lower()}")
        docs = c.docs(*args)
        cm = by_name(docs).get("ConfigMap/distant-signal-redis-acl", {})
        got = cast("dict[str, str]", cm.get("data") or {}).get("users.acl.tpl") or ""
        want = acl.render(
            text,
            stage="narrow",
            default_user=True,
            default_password=True,
            clients=["pollerTransitMetrolink"] if on else [],
        )
        c.check(
            ok=got == want and ("user poller-transit-metrolink " in got) == on,
            message=f"optional ACL user with its client {on}: ConfigMap {got!r:.80}",
        )
        inits = containers(deployment(docs, "-redis"), "initContainers")
        init_env = env(inits[0]) if inits else {}
        c.check(
            ok=(var in init_env) == on
            and (
                not on
                or secret_ref(init_env.get(var))
                == ("example-redis-users", "poller-transit-metrolink-password")
            ),
            message=f"optional ACL user with its client {on}: Redis env {var}",
        )
    # Not deployed while its network is off, so defaultUser off does not
    # need its client on.
    renders(
        c,
        "defaultUser off with the transit client off (no producer)",
        *ALL_CLIENTS,
        *sets("redis.acl.defaultUser=off"),
    )


REDIS_PORT = 6379
HTTPS_PORT = 443
POSTGRES_PORT = 5432
TRANSIT_ON = sets(
    "pollerTransitSchedule.networks.metrolink.enabled=true",
    "redis.acl.clients.pollerTransitMetrolink=true",
    "ingestWriter.streams.transit-metrolink=apply",
)


def check_transit_producer(c: Checker) -> None:
    """Check poller-transit-schedule renders per enabled network (transit §13)."""
    name = "Deployment/distant-signal-poller-transit-schedule-metrolink"
    c.check(
        ok=name not in by_name(c.docs()),
        message="poller-transit-schedule must not render by default",
    )
    docs = by_name(c.docs(*TRANSIT_ON))
    deployment = docs.get(name, {})
    spec = cast("dict[str, object]", deployment.get("spec") or {})
    c.check(
        ok=cast("dict[str, str]", spec.get("strategy") or {}).get("type") == "Recreate",
        message=f"{name}: strategy Recreate",
    )
    main = (containers(deployment) or [{}])[0]
    e = env(main)
    c.check(
        ok=value(e.get("TRANSIT_NETWORK")) == "metrolink"
        and value(e.get("REDIS_USERNAME")) == "poller-transit-metrolink"
        and secret_ref(e.get("REDIS_PASSWORD"))
        == ("example-redis-users", "poller-transit-metrolink-password")
        and value(e.get("TRANSIT_WORK_DIR")) == "/work",
        message=f"{name}: its network, Redis user and work dir",
    )
    # The one-shot timetable refresh: off (empty) by default, passed
    # through as set, a token outside its grammar refused at render.
    c.check(
        ok=value(e.get("TRANSIT_TIMETABLE_REFRESH")) == "",
        message=f"{name}: no timetable refresh request by default",
    )
    refreshed = by_name(
        c.docs(
            *TRANSIT_ON,
            *sets(
                "pollerTransitSchedule.networks.metrolink.timetableRefresh=2026-10-15"
            ),
        )
    )
    refreshed_env = env((containers(refreshed.get(name, {})) or [{}])[0])
    c.check(
        ok=value(refreshed_env.get("TRANSIT_TIMETABLE_REFRESH")) == "2026-10-15",
        message=f"{name}: timetableRefresh reaches TRANSIT_TIMETABLE_REFRESH",
    )
    c.refuses(
        "a timetable refresh token outside its grammar",
        "timetableRefresh must be 1-64",
        *TRANSIT_ON,
        *sets("pollerTransitSchedule.networks.metrolink.timetableRefresh=has space"),
    )
    volumes = cast("list[dict[str, object]]", pod(deployment).get("volumes") or [])
    c.check(
        ok=any(v.get("name") == "work" and "emptyDir" in v for v in volumes),
        message=f"{name}: an emptyDir at /work",
    )
    # NetworkPolicy (transit spec §13.3): Redis and 443 out, no Postgres.
    egress_on = sets(
        "networkPolicy.enabled=true",
        "networkPolicy.egress.enabled=true",
        "networkPolicy.egress.internetPorts={443}",
    )
    policies = by_name(c.docs(*TRANSIT_ON, *egress_on))
    policy = policies.get("NetworkPolicy/distant-signal-poller-transit-schedule", {})
    rules = cast(
        "list[dict[str, object]]",
        cast("dict[str, object]", policy.get("spec") or {}).get("egress") or [],
    )
    ports = {
        cast("int", p.get("port"))
        for r in rules
        for p in cast("list[dict[str, object]]", r.get("ports") or [])
    }
    c.check(
        ok={REDIS_PORT, HTTPS_PORT} <= ports and POSTGRES_PORT not in ports,
        message=f"transit producer egress: Redis and 443, no Postgres: {sorted(ports)}",
    )
    redis_policy = str(policies.get("NetworkPolicy/distant-signal-redis", {}))
    c.check(
        ok="poller-transit-schedule" in redis_policy,
        message="Redis ingress must admit poller-transit-schedule",
    )
    c.refuses(
        "the transit producer without its stream applied",
        "ingestWriter.streams.transit-metrolink: apply",
        *TRANSIT_ON,
        *sets("ingestWriter.streams.transit-metrolink=off"),
    )
    c.refuses(
        "the transit producer without its Redis user",
        "redis.acl.clients.pollerTransitMetrolink",
        *TRANSIT_ON,
        *sets("redis.acl.clients.pollerTransitMetrolink=false"),
    )


def renders(c: Checker, label: str, *args: str) -> None:
    """Check that the render succeeds."""
    code, out = c.render(*args)
    c.check(ok=code == 0, message=f"{label}: must render: {out.strip()[-400:]}")


def check_redis_user_guards(c: Checker) -> None:
    """Security review H3 and M3: stream producers need their own users."""
    # M3 (`default` on without redis.auth) is a prerequisite failure now:
    # the preflight refuses `default` on whatever redis.auth says.
    c.refuses(
        "M3: ACL users with default on and no redis.auth",
        "redis.acl.defaultUser: off",
        *sets("redis.acl.defaultUser=on"),
    )
    # (label, the producer's values, its redis.acl.clients key)
    producers: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("pollers.ldbws", poller("ldbws"), "pollerLdbws"),
        ("pollers.tfl", poller("tfl"), "pollerTfl"),
        ("pollers.tocs", poller("tocs"), "pollerTocs"),
        ("pollers.incidents", poller("incidents"), "pollerIncidents"),
        ("fullCoverageConsumer", (), "fullCoverageConsumer"),
        (
            "pollerIrishRailLive",
            sets("pollerIrishRailLive.enabled=true"),
            "pollerIrishRailLive",
        ),
    )
    for label, args, client in producers:
        needle = f"redis.acl.clients.{client}"
        c.refuses(
            f"H3: {label} without its user",
            needle,
            *args,
            *sets(f"redis.acl.clients.{client}=false"),
        )
        renders(
            c,
            f"H3: {label} as its own narrow user",
            *args,
            *sets(f"redis.acl.clients.{client}=true"),
        )
    c.refuses(
        "H3: a writer stream on apply with default on",
        "redis.acl.defaultUser: off",
        *AUTH,
        *sets("redis.acl.defaultUser=on"),
    )


def check_narrow_role_guards(c: Checker) -> None:
    """Security review H2 and M4: narrow components connect only as their role."""
    # (label, the values, the perService key)
    components: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("pollers.stations", poller("stations"), "stations"),
        ("pollers.incidents", poller("incidents", "pollerIncidents"), "incidents"),
        ("trustBacklogConsumer", (), "trust_backlog"),
        ("trustConsumer", (), "trust_consumer"),
        ("fullCoverageConsumer reads", (), "full_coverage_ro"),
        ("pollers.ldbws reads", poller("ldbws", "pollerLdbws"), "ldbws_ro"),
        ("scheduleFeed.ingest", SCHEDULE_FEED, "schedule_ingest"),
        ("scheduleFeed.reference", SCHEDULE_FEED, "schedule_reference"),
    )
    for label, args, service in components:
        needle = f"perService.{service}.connect"
        off = sets(f"postgresql.roles.perService.{service}.connect=false")
        c.refuses(f"H2: {label} not as its role", needle, *args, *off)
        docs = c.docs(*args)
        role = f"distant_signal_{service}"
        urls = [
            value(env(container).get("DATABASE_URL"))
            for doc in docs
            if doc.get("kind") == "Deployment"
            for container in containers(doc)
        ]
        db_urls = [u for u in urls if u.startswith("postgres://")]
        c.check(
            ok=any(u.startswith(f"postgres://{role}:") for u in db_urls),
            message=f"H2: {label}: no container connects as {role}",
        )
    c.refuses(
        "M4: tfl apply as the app role",
        "perService.writer.connect",
        *sets("postgresql.roles.perService.writer.connect=false"),
    )
    # The same for a transit network's stream (transit spec §0): the
    # writer's ingest_freshness row policy binds its own role only.
    c.refuses(
        "M4: transit-metrolink apply as the app role",
        "ingestWriter.streams.transit-metrolink: apply needs",
        *sets(
            "ingestWriter.streams.tfl=off",
            "ingestWriter.streams.transit-metrolink=apply",
            "postgresql.roles.perService.writer.connect=false",
        ),
    )
    renders(
        c,
        "transit-metrolink apply as the writer's role",
        *sets("ingestWriter.streams.transit-metrolink=apply"),
    )


def check_retired_switches(c: Checker) -> None:
    """Release B: the sink and source switches accept only their one value."""
    for key, bad in (
        ("pollers.tfl.ingest.sink", "db"),
        ("pollers.stations.ingest.sink", "http"),
        ("trustConsumer.ingest.sink", "http"),
        ("fullCoverageConsumer.internalReads.source", "http"),
        ("scheduleFeed.reference.ingest.sink", "http"),
        ("ingestWriter.streams.tfl", "shadow"),
    ):
        c.refuses(f"retired {key}={bad}", f"{key}: {bad}", *sets(f"{key}={bad}"))
    renders(
        c,
        "a retired switch pinned to its one value",
        *SCHEDULE_FEED,
        *sets(
            "scheduleFeed.reference.ingest.sink=db",
            "trustConsumer.ingest.sink=db",
            "fullCoverageConsumer.ingest.sink=stream",
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    parser.add_argument(
        "--baseline", type=pathlib.Path, help="an earlier copy of the chart"
    )
    args = parser.parse_args(argv)
    c = Checker(cast("str", args.helm))
    if args.baseline is not None:
        check_baseline(c, cast("pathlib.Path", args.baseline))
    check_roles_required(c)
    check_roles_refusals(c)
    check_roles_setup(c)
    check_roles_connect(c)
    check_roles_budget(c)
    check_roles_init_script(c)
    check_redis_required(c)
    check_redis_refusals(c)
    check_redis_users(c)
    check_redis_acl_file(c)
    check_optional_acl_users(c)
    check_transit_producer(c)
    check_redis_user_guards(c)
    check_narrow_role_guards(c)
    check_retired_switches(c)
    for failure in c.failures:
        print(failure)
    if not c.failures:
        print("ok: ingest phase 0 chart checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
