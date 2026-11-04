"""The umbrella chart pins every first-party image by tag and digest.

  uv run python -m unittest discover -s scripts/tests

Distant-Signal/ci's scripts/release-order.py reads these pins (an `image`
map with a `digest` key and a repository ending in /images/<service>);
this checks the chart itself: the services at a build tag
(build.<run id>-<commit>), ds-migrate at its ds-core release (vX.Y.Z),
which is core-version, and each with its own `source` repository (not
sourceCode.url's fallback): since the split the images come from several
repositories.
"""

import re
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
VALUES = REPO / "charts" / "distant-signal" / "values.yaml"
REPOSITORY = re.compile(r"/images/([a-z0-9][a-z0-9-]*)$")
MIGRATE = "ds-migrate"

# Every first-party image the chart pins: the services' and ds-migrate.
PINNED = (
    "aggregator",
    "api",
    "darwin-relay",
    "ds-migrate",
    "enricher",
    "frontend",
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


def pinned_images(
    values: Mapping[str, Any],
) -> Iterator[tuple[str, str, str, str]]:
    """Yield (service, tag, digest, source) for every first-party `image` map."""
    for key, value in values.items():
        if not isinstance(value, Mapping):
            continue
        repository = value.get("repository")
        if key == "image" and isinstance(repository, str) and "digest" in value:
            if match := REPOSITORY.search(repository):
                yield (
                    match.group(1),
                    str(value.get("tag") or ""),
                    str(value.get("digest") or ""),
                    str(value.get("source") or ""),
                )
            continue
        yield from pinned_images(value)


class ChartPinsTest(unittest.TestCase):
    """values.yaml's first-party image pins."""

    def test_the_chart_pins_every_image(self) -> None:
        """Every first-party image is pinned by tag and digest."""
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        images = list(pinned_images(values))
        self.assertEqual(sorted(service for service, *_ in images), sorted(PINNED))
        core = (REPO / "core-version").read_text(encoding="utf-8").strip()
        for service, tag, digest, source in images:
            with self.subTest(service):
                self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
                self.assertRegex(
                    source, r"^https://github\.com/Distant-Signal/[a-z-]+$"
                )
                if service == MIGRATE:
                    self.assertEqual(tag, core)
                else:
                    self.assertRegex(tag, r"^build\.\d+-[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
