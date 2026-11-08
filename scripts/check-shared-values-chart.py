#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check the values that left `api` reach exactly their readers.

  uv run scripts/check-shared-values-chart.py [--helm HELM]

DS-403 moved seven `api.*` keys that other components read too (so editing
one rolled ingest, schedule-feed, workers or migrate objects) to keys owned
by what reads them: `shared.*`, `ingestWriter.loops.*` and `migrate.*`
(templates/_helpers.tpl, "Values that left `api`"). For each one in MOVES,
over a render with every reader on, this checks:

  - setting the new key changes exactly the objects of its readers (by
    `app.kubernetes.io/component`), so a new reader is a deliberate edit
    here and the README row that names the readers;
  - setting the old `api.*` key instead (the deprecated alias, kept for one
    release) renders exactly what setting the new key does, so production's
    values keep rendering unchanged until they move;
  - an old key set to its default's opposite wins over the new key, as the
    alias must while production's values still carry the old key.

Secrets' data is left out of every comparison (the chart generates random
passwords). Exit 1 with one line per failed check. Needs helm on PATH (or
--helm) and PyYAML (pyproject.toml's `lint` dependency group).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"
COMPONENT = "app.kubernetes.io/component"


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# Every reader on: the ingest prerequisites (ci/ingest-prereqs.yaml), the
# example values (the internal reads that give trust-consumer and
# full-coverage-consumer CORPUS_FALLBACK_ENABLED), schedulefeed, the
# ingest-writer and the migrate Job.
BASE = (
    "-f",
    str(CHART / "ci" / "ingest-prereqs.yaml"),
    "-f",
    str(CHART / "values-example.yaml"),
    *sets(
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
        "scheduleFeed.enabled=true",
        "scheduleFeed.sftp.authMethod=password",
        "ingestWriter.enabled=true",
        "migrate.job.enabled=true",
    ),
)
# schedule-reference as its own CronJob, not a container in schedulefeed.
REFERENCE_CRONJOB = sets("scheduleFeed.reference.mode=cronjob")


@dataclass(frozen=True)
class Move:
    """One value that left `api`, a non-default value, and its readers."""

    old: str
    new: str
    value: str
    readers: frozenset[str]
    extra: tuple[str, ...] = ()


CORPUS_READERS = {
    "api",
    "trust-consumer",
    "trust-backlog-consumer",
    "full-coverage-consumer",
}
MOVES = (
    Move(
        "api.corpusFallback.enabled",
        "shared.corpusFallback.enabled",
        "true",
        frozenset({*CORPUS_READERS, "schedulefeed"}),
    ),
    Move(
        "api.corpusFallback.enabled",
        "shared.corpusFallback.enabled",
        "true",
        frozenset({*CORPUS_READERS, "schedule-reference"}),
        REFERENCE_CRONJOB,
    ),
    Move(
        "api.journeyBreaks.enabled",
        "shared.journeyBreaks.enabled",
        "true",
        frozenset({"api", "notifier"}),
    ),
    Move(
        "api.reconciliationSweepIntervalSecs",
        "ingestWriter.loops.reconciliationSweepIntervalSecs",
        "301",
        frozenset({"api", "ingest-writer"}),
    ),
    Move(
        "api.scheduleEnrichmentGraceMinutes",
        "ingestWriter.loops.scheduleEnrichmentGraceMinutes",
        "31",
        frozenset({"api", "ingest-writer"}),
    ),
    Move(
        "api.backlogMatchSweepIntervalSecs",
        "ingestWriter.loops.backlogMatchSweepIntervalSecs",
        "302",
        frozenset({"api", "ingest-writer"}),
    ),
    Move(
        "api.migrations.lockTimeoutSecs",
        "migrate.lockTimeoutSecs",
        "11",
        frozenset({"api", "migrate"}),
    ),
    Move(
        "api.migrations.statementTimeoutSecs",
        "migrate.statementTimeoutSecs",
        "241",
        frozenset({"api", "migrate"}),
    ),
)

type Doc = dict[str, object]
type Rendered = dict[tuple[str, str], Doc]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def component_of(doc: Mapping[str, object]) -> str:
    """Return a rendered object's component label ("" when it has none)."""
    labels = as_map(as_map(doc.get("metadata")).get("labels"))
    return str(labels.get(COMPONENT, ""))


class Checker:
    """Renders the chart and collects failed checks."""

    def __init__(self, helm: str) -> None:
        """Use `helm` to render."""
        self.helm = helm
        self.failures: list[str] = []

    def render(self, *args: str) -> Rendered:
        """Render the chart with BASE and ARGS, keyed by (kind, name)."""
        result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "distant-signal", str(CHART), *BASE, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"helm template {' '.join(args)} failed:\n{result.stderr}"
            raise RuntimeError(msg)
        rendered: Rendered = {}
        for loaded in yaml.safe_load_all(result.stdout):
            doc = as_map(loaded)
            if not doc:
                continue
            if doc.get("kind") == "Secret":
                doc.pop("data", None)
                doc.pop("stringData", None)
            name = str(as_map(doc.get("metadata")).get("name"))
            rendered[str(doc.get("kind")), name] = doc
        return rendered

    def changed_components(self, before: Rendered, after: Rendered) -> set[str]:
        """Return the components of the objects that differ."""
        return {
            component_of(after.get(key) or before[key])
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }

    def check(self, move: Move) -> None:
        """Run MOVE's three checks."""
        where = move.new
        if move.extra:
            where += f" with {' '.join(move.extra[1::2])}"
        base = self.render(*move.extra)
        new = self.render(*move.extra, *sets(f"{move.new}={move.value}"))
        changed = self.changed_components(base, new)
        if changed != move.readers:
            self.failures.append(
                f"{where}: changes {sorted(changed)}, expected its readers "
                f"{sorted(move.readers)}"
            )
        old = self.render(*move.extra, *sets(f"{move.old}={move.value}"))
        if old != new:
            differ = sorted(
                f"{kind}/{name}"
                for kind, name in old.keys() | new.keys()
                if old.get((kind, name)) != new.get((kind, name))
            )
            self.failures.append(
                f"{move.old}: the alias renders differently from {move.new}: {differ}"
            )
        both = self.render(
            *move.extra,
            *sets(f"{move.new}={move.value}", f"{move.old}={default_of(move)}"),
        )
        if both != base:
            self.failures.append(
                f"{move.old}: set beside {move.new}, it doesn't win (the alias must)"
            )


def default_of(move: Move) -> str:
    """Return the new key's values.yaml default, as a --set string."""
    node: object = yaml.safe_load((CHART / "values.yaml").read_text("utf-8"))
    for key in move.new.split("."):
        node = as_map(node).get(key)
    if isinstance(node, bool):
        return str(node).lower()
    return str(node)


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; exit 1 on any failure."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    checker = Checker(args.helm)
    for move in MOVES:
        checker.check(move)
    for failure in checker.failures:
        print(f"FAIL: {failure}")
    if checker.failures:
        return 1
    print(f"ok: {len(MOVES)} moved values reach exactly their readers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
