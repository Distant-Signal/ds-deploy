#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's ingest-architecture phase 0 switches.

  uv run scripts/check-ingest-phase0-chart.py [--helm HELM] [--baseline DIR]

Phase 0 adds two chart features, both OFF by default
(docs/postgres-app-role.md "Stage 0b", docs/redis-acl.md):

Per-service Postgres roles (`postgresql.roles.perService`):
  - off: nothing of it renders (no postgres-grants.sql, no per-service
    role, password or variable);
  - `enabled` without `roles.setupJob.enabled`, or a service's `connect`
    without `enabled` or without `roles.enabled`, refuses to render;
  - `enabled` alone changes no Deployment: the services stay on `app`;
  - the setup Job's ConfigMap carries files/postgres-grants.sql verbatim,
    and the Job runs it after postgres-roles.sql with each role's computed
    CONNECTION LIMIT; on a new cluster (`initScript`) the initdb script and
    the Postgres pod do too;
  - `connect` moves exactly that Deployment's DATABASE_URL to its own role
    (with its own password key), the api's pool to perService.api's 16, and
    takes its pool out of the app role's computed limit; the api's
    migrations stay the owner's;
  - the generated Secret gains one password per service;
  - the role-limit budget (spec §6.6): fails at 98 of the 97 slots, passes
    at 92.

Per-client Redis ACL users (`redis.acl`):
  - off: nothing of it renders (no REDIS_USERNAME, --aclfile, initContainer
    or ConfigMap);
  - bad values refuse to render (no existingSecret, an unknown stage, a
    client without `enabled`, `defaultUser: off` while a client still uses
    `default`);
  - step 1 (`enabled`, stage open) changes only Redis (the ConfigMap, the
    initContainer, --aclfile instead of --requirepass, probes as ds-admin)
    and no client;
  - step 2 (`clients.<x>`) changes only that client: REDIS_USERNAME and its
    own REDIS_PASSWORD key; REDIS_URL stays credential-free;
  - the ConfigMap equals scripts/render-redis-acl.py's output for every
    stage and default-user setting, with and without redis.auth.

--baseline DIR renders DIR (a copy of charts/distant-signal from before
phase 0, e.g. the merge base) and this chart with the same values, for the
defaults, values-example.yaml, redis.auth on, and postgresql.roles stages A
and B (what production runs or is rolling out): every document must be
identical, with Secret values masked (randAlphaNum differs per run; the
keys are compared). CI doesn't run it (on main it compares main with
itself).

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import difflib
import importlib.util
import pathlib
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
BASE = (
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


ROLES_A = sets(
    "postgresql.roles.setupJob.enabled=true",
    "postgresql.roles.initScript=false",
    *(
        f"postgresql.roles.{r}.existingSecret=distant-signal-postgres-roles"
        for r in ("owner", "app", "exporter", "dump", "backup")
    ),
)
ROLES_B = (*ROLES_A, *sets("postgresql.roles.enabled=true"))
PER_SERVICE = (*ROLES_B, *sets("postgresql.roles.perService.enabled=true"))
SERVICES = ("api", "aggregator", "enricher", "notifier")
ALL_CONNECT = (
    *PER_SERVICE,
    *sets(*(f"postgresql.roles.perService.{s}.connect=true" for s in SERVICES)),
)
AUTH = sets("redis.auth.enabled=true", "redis.auth.existingSecret=redis-auth")
ACL = (*AUTH, *sets("redis.acl.enabled=true", "redis.acl.existingSecret=redis-users"))
# redis.acl.clients key -> (Deployment suffix, ACL user).
CLIENTS = {
    "api": ("-api", "api"),
    "enricher": ("-enricher", "enricher"),
    "movementRelay": ("-movement-relay", "movement-relay"),
    "trustConsumer": ("-trust-consumer", "trust-consumer"),
    "fullCoverageConsumer": ("-full-coverage-consumer", "full-coverage-consumer"),
    "trustBacklogConsumer": ("-trust-backlog-consumer", "trust-backlog-consumer"),
}
# redis.acl.clients keys whose workload renders nothing with the defaults
# (the ingest-writer's streams, plan 3a.3; poller-incidents' db sink, plan
# 2c): no per-client Deployment check, but defaultUser off still needs
# each of them on.
DORMANT_CLIENTS = ("ingestWriter", "pollerIncidents")
ALL_CLIENTS = sets(
    *(f"redis.acl.clients.{k}=true" for k in (*CLIENTS, *DORMANT_CLIENTS))
)

BASELINE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ()),
    ("values-example.yaml", ("-f", EXAMPLE)),
    ("redis.auth on", AUTH),
    ("postgresql.roles stage A", ROLES_A),
    ("postgresql.roles stage B", ROLES_B),
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
            message=f"{label}: must refuse to render naming {needle!r}",
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


def check_roles_off(c: Checker) -> None:
    """Nothing of perService renders by default or with stages A/B."""
    for label, args in (("defaults", ()), ("stage B", ROLES_B)):
        _, out = c.render(*args)
        for needle in (
            "postgres-grants",
            "distant_signal_api",
            "distant_signal_aggregator",
            "DS_PG_API",
            "_connection_limit=34",
        ):
            c.check(ok=needle not in out, message=f"perService off ({label}): {needle}")


def check_roles_refusals(c: Checker) -> None:
    """Invalid perService combinations refuse to render."""
    c.refuses(
        "perService without the setup Job",
        "postgresql.roles.setupJob.enabled",
        *sets("postgresql.roles.perService.enabled=true"),
    )
    c.refuses(
        "connect without perService.enabled",
        "postgresql.roles.perService.enabled",
        *ROLES_B,
        *sets("postgresql.roles.perService.api.connect=true"),
    )
    c.refuses(
        "connect without roles.enabled",
        "postgresql.roles.enabled",
        *ROLES_A,
        *sets(
            "postgresql.roles.perService.enabled=true",
            "postgresql.roles.perService.notifier.connect=true",
        ),
    )
    c.refuses(
        "a non-numeric connectionLimit",
        "connectionLimit must be a whole number",
        *PER_SERVICE,
        *sets("postgresql.roles.perService.enricher.connectionLimit=lots"),
    )


def check_roles_enabled_only(c: Checker) -> None:
    """Check that `enabled` creates roles but moves no service."""
    diffs = differing(c.docs(*ROLES_B), c.docs(*PER_SERVICE))
    moved = [k for k in diffs if k.startswith("Deployment/")]
    c.check(ok=not moved, message=f"perService.enabled alone changed {moved}")
    expected = {
        "ConfigMap/distant-signal-postgres-roles",
        "Job/distant-signal-postgres-roles-setup",
        "Secret/distant-signal",
    }
    c.check(
        ok=set(diffs) <= expected,
        message=f"perService.enabled changed {sorted(diffs)}",
    )
    docs = by_name(c.docs(*PER_SERVICE))
    cm = docs.get("ConfigMap/distant-signal-postgres-roles", {})
    data = cast("dict[str, str]", cm.get("data") or {})
    grants = (CHART / "files" / "postgres-grants.sql").read_text(encoding="utf-8")
    c.check(
        ok=data.get("postgres-grants.sql") == grants,
        message="the roles ConfigMap must carry files/postgres-grants.sql verbatim",
    )
    job = docs.get("Job/distant-signal-postgres-roles-setup", {})
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
    ):
        c.check(ok=needle in script, message=f"setup Job script lacks {needle}")
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
    """`connect` moves exactly that service."""
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
    one = c.docs(
        *PER_SERVICE, *sets("postgresql.roles.perService.notifier.connect=true")
    )
    diffs = differing(c.docs(*PER_SERVICE), one)
    moved = sorted(k for k in diffs if k.startswith("Deployment/"))
    c.check(
        ok=moved == ["Deployment/distant-signal-notifier"],
        message=f"notifier.connect must move only the notifier, moved {moved}",
    )
    c.check(
        ok=value(main_env(one, "-api").get("DATABASE_MAX_CONNECTIONS")) == "50",
        message="the api pool must stay 50 until the api connects as its own role",
    )


def check_roles_budget(c: Checker) -> None:
    """Role limits must fit the 97 non-superuser slots."""
    # All four connecting: owner 3 + app 5 + exporter 3 + dump 2 + backup 4
    # + aggregator 11 + enricher 6 + notifier 6 = 40, plus the api's.
    for api_limit, total, ok in ((58, 98, False), (52, 92, True)):
        code, out = c.render(
            *ALL_CONNECT,
            *sets(f"postgresql.roles.perService.api.connectionLimit={api_limit}"),
            "--show-only",
            "templates/api-deployment.yaml",
        )
        if ok:
            c.check(
                ok=code == 0, message=f"budget {total}/97 must render: {out[-300:]}"
            )
        else:
            c.check(
                ok=code != 0 and f"sum to {total}" in out,
                message=f"budget {total}/97 must fail the render",
            )


def check_roles_init_script(c: Checker) -> None:
    """Check that a new cluster's initdb script runs postgres-grants.sql."""
    args = sets(
        "postgresql.roles.enabled=true",
        "postgresql.roles.setupJob.enabled=true",
        "postgresql.roles.perService.enabled=true",
    )
    docs = by_name(c.docs(*args))
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


def check_redis_off(c: Checker) -> None:
    """Nothing of redis.acl renders by default or with redis.auth."""
    for label, args in (("defaults", ()), ("redis.auth", AUTH)):
        _, out = c.render(*args)
        for needle in ("REDIS_USERNAME", "aclfile", "redis-acl", "REDIS_ACL_PASSWORD"):
            c.check(ok=needle not in out, message=f"redis.acl off ({label}): {needle}")


def check_redis_refusals(c: Checker) -> None:
    """Invalid redis.acl values refuse to render."""
    c.refuses(
        "acl without existingSecret",
        "redis.acl.existingSecret",
        *sets("redis.acl.enabled=true"),
    )
    c.refuses(
        "an unknown stage", "redis.acl.stage", *ACL, *sets("redis.acl.stage=wide")
    )
    c.refuses(
        "a client without acl.enabled",
        "redis.acl.clients.api needs redis.acl.enabled",
        *AUTH,
        *sets("redis.acl.clients.api=true"),
    )
    c.refuses(
        "default off with a client on default",
        "redis.acl.clients.enricher",
        *ACL,
        *sets("redis.acl.defaultUser=off"),
        *sets(
            *(
                f"redis.acl.clients.{k}=true"
                for k in (*CLIENTS, *DORMANT_CLIENTS)
                if k != "enricher"
            )
        ),
    )


def check_redis_steps(c: Checker) -> None:
    """Step 1 changes only Redis; step 2 only the client it names."""
    auth = c.docs(*AUTH)
    step1 = c.docs(*ACL)
    diffs = differing(auth, step1)
    c.check(
        ok=set(diffs)
        == {"Deployment/distant-signal-redis", "ConfigMap/distant-signal-redis-acl"},
        message=f"step 1 must change only Redis and its ACL ConfigMap: {sorted(diffs)}",
    )
    redis = deployment(step1, "-redis")
    main = (containers(redis) or [{}])[0]
    args = [str(a) for a in cast("list[object]", main.get("args") or [])]
    c.check(
        ok="--aclfile" in args and "--requirepass" not in args,
        message=f"step 1 Redis args: {args}",
    )
    c.check(
        ok=secret_ref(env(main).get("REDISCLI_AUTH"))
        == ("redis-users", "ds-admin-password"),
        message="Redis probes must authenticate as ds-admin",
    )
    inits = containers(redis, "initContainers")
    init_env = env(inits[0]) if inits else {}
    c.check(
        ok=secret_ref(init_env.get("REDIS_ACL_PASSWORD_DEFAULT"))
        == ("redis-auth", "redis-password"),
        message="step 1: default keeps the redis.auth password",
    )
    c.check(
        ok=secret_ref(init_env.get("REDIS_ACL_PASSWORD_MOVEMENT_RELAY"))
        == ("redis-users", "movement-relay-password"),
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
        step2 = c.docs(*ACL, *sets(f"redis.acl.clients.{key}=true"))
        diffs = differing(step1, step2)
        c.check(
            ok=list(diffs) == [f"Deployment/distant-signal{suffix}"],
            message=f"clients.{key} must change only its Deployment: {sorted(diffs)}",
        )
        e = main_env(step2, suffix)
        c.check(
            ok=value(e.get("REDIS_USERNAME")) == user
            and secret_ref(e.get("REDIS_PASSWORD"))
            == ("redis-users", f"{user}-password")
            and "@" not in value(e.get("REDIS_URL")),
            message=f"clients.{key}: REDIS_USERNAME {user} and its own password",
        )


def check_redis_acl_file(c: Checker) -> None:
    """Check the ConfigMap against scripts/render-redis-acl.py."""
    acl = load_render_acl()
    text = (CHART / "files" / "redis-users.acl.tpl").read_text(encoding="utf-8")
    for stage in ("open", "narrow"):
        for default_on in (True, False):
            for with_auth in (True, False):
                base = (
                    ACL
                    if with_auth
                    else sets(
                        "redis.acl.enabled=true", "redis.acl.existingSecret=redis-users"
                    )
                )
                args = (
                    *base,
                    *sets(f"redis.acl.stage={stage}"),
                    *sets(f"redis.acl.defaultUser={'on' if default_on else 'off'}"),
                    *(ALL_CLIENTS if not default_on else ()),
                )
                cm = by_name(c.docs(*args)).get(
                    "ConfigMap/distant-signal-redis-acl", {}
                )
                got = cast("dict[str, str]", cm.get("data") or {}).get("users.acl.tpl")
                want = acl.render(
                    text,
                    stage=stage,
                    default_user=default_on,
                    default_password=with_auth,
                )
                c.check(
                    ok=got == want,
                    message=(
                        f"ACL ConfigMap (stage {stage}, default "
                        f"{'on' if default_on else 'off'}, auth {with_auth}) "
                        "differs from scripts/render-redis-acl.py"
                    ),
                )


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    parser.add_argument(
        "--baseline", type=pathlib.Path, help="a pre-phase-0 copy of the chart"
    )
    args = parser.parse_args(argv)
    c = Checker(cast("str", args.helm))
    if args.baseline is not None:
        check_baseline(c, cast("pathlib.Path", args.baseline))
    check_roles_off(c)
    check_roles_refusals(c)
    check_roles_enabled_only(c)
    check_roles_connect(c)
    check_roles_budget(c)
    check_roles_init_script(c)
    check_redis_off(c)
    check_redis_refusals(c)
    check_redis_steps(c)
    check_redis_acl_file(c)
    for failure in c.failures:
        print(failure)
    if not c.failures:
        print("ok: ingest phase 0 chart checks passed")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
