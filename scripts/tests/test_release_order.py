"""Tests for scripts/release-order.py.

  uv run python -m unittest discover -s scripts/tests

The fixtures are in fixtures/release-order: chart values pinning ds-migrate,
the api, the frontend and two services (plus non-first-party images the
check must skip), and Cargo.locks in the monorepo's shape and the split's.
Each check test hands `check` the labels a set of images would carry and
expects a finding only for the violation it plants.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any, override
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "release-order.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "release-order"
PIN = "ghcr.io/distant-signal/images/api:build.42-0123abcd@sha256:" + "a" * 64
REPO = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_order", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_order"] = module
    spec.loader.exec_module(module)
    return module


ro = _load()
CORE = ro.CORE
API_BUILD = ro.API_BUILD


def fixture_images() -> list[Any]:
    """Return the fixture values' first-party images."""
    values = json.loads((FIXTURES / "values.json").read_text("utf-8"))
    return list(ro.pinned_images(values))


def labels_by_service(
    overrides: Mapping[str, Mapping[str, str | None]] | None = None,
) -> dict[str, dict[str, str]]:
    """Every fixture image at core v1.0.0 and api build.10, with OVERRIDES.

    An override value of None removes that label.
    """
    labels: dict[str, dict[str, str]] = {
        "ds-migrate": {CORE: "v1.0.0"},
        "api": {CORE: "v1.0.0", API_BUILD: "build.10-0123abc"},
        "aggregator": {CORE: "v1.0.0"},
        "poller-tfl": {CORE: "v1.0.0"},
        "frontend": {API_BUILD: "build.10-0123abc"},
    }
    for service, changes in (overrides or {}).items():
        for key, value in changes.items():
            if value is None:
                labels[service].pop(key, None)
            else:
                labels[service][key] = value
    return labels


def run_check(
    overrides: Mapping[str, Mapping[str, str | None]] | None = None,
    floors: list[tuple[str, Any]] | None = None,
) -> list[str]:
    """`check` over the fixture images with these labels."""
    labels = labels_by_service(overrides)
    return list(
        ro.check(fixture_images(), lambda image: labels[image.service], floors or [])
    )


def version(text: str) -> Any:  # noqa: ANN401  # the loaded module's Version
    """TEXT parsed; fails the test when it doesn't parse."""
    parsed = ro.parse_version(text)
    if parsed is None:
        msg = f"{text!r} doesn't parse"
        raise AssertionError(msg)
    return parsed


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


class VersionTest(unittest.TestCase):
    """Label values and how they order."""

    def test_semver_orders_numerically(self) -> None:
        """v1.10.0 is newer than v1.9.3."""
        self.assertLess(version("v1.9.3"), version("v1.10.0"))
        self.assertEqual(version("v1.2.3"), version("v1.2.3"))

    def test_builds_order_by_run(self) -> None:
        """build.10 is newer than build.9, whatever the commits."""
        self.assertLess(version("build.9-fffffff"), version("build.10-0000000"))
        self.assertEqual(version("build.9-aaaaaaa"), version("build.9-bbbbbbb"))

    def test_schemes_dont_order(self) -> None:
        """A tag and a build are different schemes."""
        self.assertFalse(version("v1.0.0").comparable(version("build.1-abcdef0")))

    def test_rejects(self) -> None:
        """Anything else is not a version."""
        for text in ("1.0.0", "v1.0", "v1.0.0-rc1", "build.1", "build.x-abcdef0", ""):
            with self.subTest(text=text):
                self.assertIsNone(ro.parse_version(text))


class PinnedImagesTest(unittest.TestCase):
    """Which images the check reads."""

    def test_first_party_only(self) -> None:
        """Images with a digest field under /images/, wherever they sit."""
        images = {image.path: image for image in fixture_images()}
        self.assertEqual(
            sorted(images),
            [
                ".aggregator.image",
                ".api.image",
                ".frontend.image",
                ".migrate.image",
                ".pollers.tfl.image",
            ],
        )
        self.assertEqual(images[".migrate.image"].service, "ds-migrate")

    def test_reference(self) -> None:
        """Digest, else tag, else the appVersion."""
        images = {image.service: image for image in fixture_images()}
        self.assertEqual(
            images["api"].reference("sha-1"),
            "ghcr.io/distant-signal/images/api@sha256:aaaa",
        )
        self.assertEqual(
            images["poller-tfl"].reference("sha-1"),
            "ghcr.io/distant-signal/images/poller-tfl:build.7-abcdef0",
        )
        self.assertEqual(
            images["aggregator"].reference("sha-1"),
            "ghcr.io/distant-signal/images/aggregator:sha-1",
        )
        with self.assertRaises(ro.InputError):
            images["aggregator"].reference("")

    def test_the_chart_pins_every_image(self) -> None:
        """ds-deploy: every first-party image is pinned by tag and digest.

        The services at a build tag (build.<run id>-<commit>); ds-migrate at
        its ds-core release (vX.Y.Z), which is core-version.
        """
        values = ro.read_values(REPO / "charts" / "distant-signal" / "values.yaml")
        images = list(ro.pinned_images(values))
        self.assertEqual(sorted(image.service for image in images), sorted(PINNED))
        core = (REPO / "core-version").read_text(encoding="utf-8").strip()
        for image in images:
            with self.subTest(image.service):
                self.assertRegex(image.digest, r"^sha256:[0-9a-f]{64}$")
                if image.service == ro.MIGRATE:
                    self.assertEqual(image.tag, core)
                else:
                    self.assertRegex(image.tag, r"^build\.\d+-[0-9a-f]{40}$")


class LabelsTest(unittest.TestCase):
    """The labels each image is built with."""

    def labels(self, lock: str) -> dict[str, dict[str, str]]:
        """image_labels over a root with LOCK and two Dockerfiles."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(FIXTURES / lock, root / "Cargo.lock")
            (root / "docker").mkdir()
            for service in ("api", "poller-tfl"):
                (root / "docker" / f"{service}.Dockerfile").touch()
            return dict(ro.image_labels(root))

    def test_monorepo(self) -> None:
        """Workspace crates: v<version>; the api and frontend get api-build."""
        self.assertEqual(
            self.labels("Cargo.lock"),
            {
                "api": {CORE: "v0.1.0", API_BUILD: "v0.1.0"},
                "poller-tfl": {CORE: "v0.1.0"},
                "frontend": {API_BUILD: "v0.1.0"},
            },
        )

    def test_split(self) -> None:
        """A git dependency: its tag. No api crate: no api-build."""
        labels = self.labels("Cargo-split.lock")
        self.assertEqual(labels["poller-tfl"], {CORE: "v1.4.2"})
        self.assertNotIn("frontend", labels)
        self.assertNotIn(API_BUILD, labels["api"])

    def test_split_frontend_reads_the_api_pin(self) -> None:
        """No api crate: the frontend's api-build is its pinned api's build tag."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(FIXTURES / "Cargo-split.lock", root / "Cargo.lock")
            pin = root / "frontend" / "api-image.txt"
            pin.parent.mkdir(parents=True)
            pin.write_text(f"# comment\n{PIN}\n", "utf-8")
            self.assertEqual(
                ro.image_labels(root), {"frontend": {API_BUILD: "build.42-0123abcd"}}
            )
            (root / "Cargo.lock").unlink()
            self.assertEqual(
                ro.image_labels(root), {"frontend": {API_BUILD: "build.42-0123abcd"}}
            )
            pin.write_text(PIN.replace("build.42-0123abcd", "main"), "utf-8")
            with self.assertRaisesRegex(ro.InputError, "api-image.txt"):
                ro.image_labels(root)

    def test_no_lock_and_no_pin(self) -> None:
        """Nothing to label: no images."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ro.image_labels(Path(tmp)), {})

    def test_other_sources_are_refused(self) -> None:
        """A core crate from a registry isn't a ds-core release."""
        with self.assertRaisesRegex(ro.InputError, "not a git tag or the workspace"):
            self.labels("Cargo-registry.lock")


class CheckTest(unittest.TestCase):
    """The three ordering rules, and the labels they need."""

    def assert_one_finding(self, findings: list[str], *parts: str) -> None:
        """Exactly one finding, containing every part."""
        self.assertEqual(len(findings), 1, findings)
        for part in parts:
            self.assertIn(part, findings[0])

    def test_consistent(self) -> None:
        """Older or equal versions everywhere: no finding."""
        self.assertEqual(run_check(), [])
        self.assertEqual(
            run_check(
                {
                    "aggregator": {CORE: "v0.9.9"},
                    "frontend": {API_BUILD: "build.9-0000000"},
                }
            ),
            [],
        )

    def test_service_newer_than_migrate(self) -> None:
        """Expand: a service built against a newer core than ds-migrate's."""
        self.assert_one_finding(
            run_check({"aggregator": {CORE: "v1.1.0"}}),
            ".aggregator.image",
            "v1.1.0 is newer than v1.0.0",
            "expand",
        )

    def test_api_newer_than_migrate(self) -> None:
        """The api's core-version counts like any service's."""
        self.assert_one_finding(
            run_check({"api": {CORE: "v1.0.1"}}), ".api.image", "newer than v1.0.0"
        )

    def test_frontend_newer_than_api(self) -> None:
        """api-build: a frontend built with a newer api's types."""
        self.assert_one_finding(
            run_check({"frontend": {API_BUILD: "build.11-0000000"}}),
            ".frontend.image",
            "build.11-0000000 is newer than build.10-0123abc",
            "§5.3",
        )

    def test_mixed_schemes(self) -> None:
        """A semver core against a build-tag core doesn't order."""
        self.assert_one_finding(
            run_check({"poller-tfl": {CORE: "build.3-abcdef0"}}),
            "don't order",
        )

    def test_missing_labels(self) -> None:
        """Every Rust image needs core-version; the frontend needs api-build."""
        for service, key in (
            ("aggregator", CORE),
            ("ds-migrate", CORE),
            ("frontend", API_BUILD),
        ):
            with self.subTest(service=service, key=key):
                self.assert_one_finding(
                    run_check({service: {key: None}}), f"({service}) has no {key} label"
                )

    def test_a_build_tag_api_build_reads_the_api_tag(self) -> None:
        """After the split the api's own api-build label isn't read."""
        self.assertEqual(run_check({"api": {API_BUILD: None}}), [])
        self.assertEqual(run_check({"api": {API_BUILD: "v0.1.0"}}), [])

    def test_a_build_tag_api_build_needs_a_build_tag_api(self) -> None:
        """The api pinned by digest alone can't be ordered against the frontend."""
        images = [
            ro.Pinned(i.path, i.service, i.repository, "", i.digest)
            if i.service == "api"
            else i
            for i in fixture_images()
        ]
        labels = labels_by_service()
        findings = list(ro.check(images, lambda image: labels[image.service], []))
        self.assert_one_finding(findings, "(api)", "must be build.<run>-<sha>")

    def test_semver_api_build_reads_the_api_label(self) -> None:
        """The monorepo: both are its api crate's version, from the labels."""
        same = {"api": {API_BUILD: "v0.1.0"}, "frontend": {API_BUILD: "v0.1.0"}}
        self.assertEqual(run_check(same), [])
        self.assert_one_finding(
            run_check(
                {"api": {API_BUILD: "v0.1.0"}, "frontend": {API_BUILD: "v0.2.0"}}
            ),
            "v0.2.0 is newer than v0.1.0",
        )
        self.assert_one_finding(
            run_check({"api": {API_BUILD: None}, "frontend": {API_BUILD: "v0.1.0"}}),
            "(api) has no org.distant-signal.api-build label",
        )

    def test_unparseable_label(self) -> None:
        """A label that isn't a version is a finding."""
        self.assert_one_finding(
            run_check({"aggregator": {CORE: "latest"}}), "'latest' is neither"
        )

    def test_frontend_has_no_core_version(self) -> None:
        """The frontend is not a Cargo build: no core-version needed."""
        self.assertNotIn(CORE, labels_by_service()["frontend"])
        self.assertEqual(run_check(), [])

    def test_no_migrate_image(self) -> None:
        """The values must pin ds-migrate."""
        images = [i for i in fixture_images() if i.service != "ds-migrate"]
        labels = labels_by_service()
        findings = ro.check(images, lambda image: labels[image.service], [])
        self.assert_one_finding(findings, "pin no ds-migrate image")

    def test_contract_floor_met(self) -> None:
        """Every image at or past the floor: no finding."""
        self.assertEqual(run_check(floors=[("x.sql", version("v1.0.0"))]), [])

    def test_contract_floor_violated(self) -> None:
        """Contract: an image still built against a core before the floor."""
        findings = run_check(
            {"poller-tfl": {CORE: "v0.9.0"}},
            floors=[("20990101000000_drop_x.sql", version("v0.9.5"))],
        )
        self.assert_one_finding(
            findings,
            "20990101000000_drop_x.sql is safe after core v0.9.5",
            ".pollers.tfl.image",
            "v0.9.0",
            "contract",
        )

    def test_contract_floor_ahead_of_everything(self) -> None:
        """A floor past every image: one finding per Rust image."""
        findings = run_check(floors=[("y.sql", version("v2.0.0"))])
        self.assertEqual(len(findings), 4, findings)


class ContractFloorsTest(unittest.TestCase):
    """Reading `safe after core` from the migrations' headers."""

    @override
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()

    def write(self, name: str, sql: str) -> None:
        """Add a migration."""
        (self.migrations / name).write_text(sql, encoding="utf-8")

    def test_reads_versions(self) -> None:
        """The spec's header form, after `-- no-transaction` too."""
        self.write(
            "1_a.sql",
            "-- contract: users.email (code stopped using it in e44f98f3; "
            "safe after core v1.2.0)\nALTER TABLE users DROP COLUMN email;\n",
        )
        self.write(
            "2_b.sql",
            "-- no-transaction\n\n-- contract: x (code stopped using it in abc; "
            "safe after core v1.10.0)\nDROP TABLE x;\n",
        )
        floors, problems = ro.contract_floors(self.root, self.migrations)
        self.assertEqual(problems, [])
        self.assertEqual(
            [(name, v.text) for name, v in floors],
            [("1_a.sql", "v1.2.0"), ("2_b.sql", "v1.10.0")],
        )

    def test_unversioned_headers_add_no_floor(self) -> None:
        """Today's headers carry no version: nothing to check."""
        self.write(
            "1_a.sql",
            "-- contract: users.email (code stopped using it in e44f98f3)\nSELECT 1;\n",
        )
        self.write(
            "2_b.sql", "SELECT 1;\n-- contract: not leading (safe after core v9.0.0)\n"
        )
        self.assertEqual(ro.contract_floors(self.root, self.migrations), ([], []))

    def test_bad_version(self) -> None:
        """A `safe after core` that isn't vX.Y.Z is a finding."""
        for bad in ("1.2.0", "build.4-abcdef0", "soon"):
            with self.subTest(bad=bad):
                self.write(
                    "1_a.sql",
                    "-- contract: x (code stopped using it in a; "
                    f"safe after core {bad})\n",
                )
                floors, problems = ro.contract_floors(self.root, self.migrations)
                self.assertEqual(floors, [])
                self.assertEqual(len(problems), 1)
                self.assertIn("migrations/1_a.sql", problems[0])

    def test_this_repository(self) -> None:
        """The repository's migrations parse (none is versioned yet)."""
        _, problems = ro.contract_floors(REPO, REPO / ro.MIGRATIONS)
        self.assertEqual(problems, [])


class FakeRegistry:
    """A registry answering from a dict of URL -> JSON, with a token dance."""

    TOKEN_URL = "https://ghcr.io/token"  # noqa: S105  # a URL, not a secret

    def __init__(self, responses: Mapping[str, object]) -> None:
        """Answer from RESPONSES."""
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(  # noqa: D102  # the Fetch signature
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[bytes, Mapping[str, str]]:
        self.calls.append((url, dict(headers)))
        if url.startswith(self.TOKEN_URL):
            return json.dumps({"token": "t0k"}).encode(), {}
        if "/manifests/" in url and headers.get("Authorization") != "Bearer t0k":
            challenge = Message()
            challenge["WWW-Authenticate"] = (
                f'Bearer realm="{self.TOKEN_URL}",service="ghcr.io",'
                'scope="repository:distant-signal/images/api:pull"'
            )
            raise urllib.error.HTTPError(
                url, 401, "Unauthorized", challenge, io.BytesIO()
            )
        if url not in self.responses:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO())
        return json.dumps(self.responses[url]).encode(), {}


BASE = "https://ghcr.io/v2/distant-signal/images/api"
CONFIG = {"config": {"Labels": {CORE: "v0.1.0", API_BUILD: "v0.1.0"}}}


class RegistryTest(unittest.TestCase):
    """Reading labels from an image config."""

    def test_manifest(self) -> None:
        """A plain manifest, by digest, after an anonymous token."""
        fake = FakeRegistry(
            {
                f"{BASE}/manifests/sha256:aaaa": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"digest": "sha256:cfg"},
                },
                f"{BASE}/blobs/sha256:cfg": CONFIG,
            }
        )
        labels = ro.registry_labels(
            "ghcr.io/distant-signal/images/api@sha256:aaaa", fake
        )
        self.assertEqual(labels, CONFIG["config"]["Labels"])
        token_calls = [url for url, _ in fake.calls if url.startswith(fake.TOKEN_URL)]
        self.assertEqual(len(token_calls), 1)
        self.assertIn("scope=repository:distant-signal/images/api:pull", token_calls[0])

    def test_index_picks_linux_amd64(self) -> None:
        """An index (by tag): the linux/amd64 manifest's config."""
        fake = FakeRegistry(
            {
                f"{BASE}/manifests/main": {
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {
                            "digest": "sha256:arm",
                            "platform": {"os": "linux", "architecture": "arm64"},
                        },
                        {
                            "digest": "sha256:amd",
                            "platform": {"os": "linux", "architecture": "amd64"},
                        },
                    ],
                },
                f"{BASE}/manifests/sha256:amd": {"config": {"digest": "sha256:cfg"}},
                f"{BASE}/blobs/sha256:cfg": CONFIG,
            }
        )
        labels = ro.registry_labels("ghcr.io/distant-signal/images/api:main", fake)
        self.assertEqual(labels[CORE], "v0.1.0")

    def test_missing_image_is_input_error(self) -> None:
        """A 4xx (e.g. a private package) fails at once, naming the image."""
        with self.assertRaisesRegex(ro.InputError, "HTTP 404 .*public"):
            ro.with_retries("ghcr.io/distant-signal/images/api:nope", FakeRegistry({}))


class MainTest(unittest.TestCase):
    """The CLI."""

    def main(self, *argv: str) -> tuple[int, str]:
        """Run main; return its status and stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = ro.main(list(argv))
        return status, out.getvalue()

    def test_check_registry_fails_on_a_violation(self) -> None:
        """The pre-push check, against a registry whose service is too new."""
        images = fixture_images()
        configs = {
            image.reference("x"): labels_by_service({"aggregator": {CORE: "v2.0.0"}})[
                image.service
            ]
            for image in images
        }

        def fake(reference: str, _fetch: object) -> dict[str, str]:
            return configs[reference]

        with mock.patch.object(ro, "with_retries", fake):
            status, out = self.main(
                "--root",
                str(REPO),
                "check",
                "--values",
                str(FIXTURES / "values.json"),
                "--registry",
                "--app-version",
                "x",
            )
        self.assertEqual(status, 1)
        self.assertIn("v2.0.0 is newer than v1.0.0", out)

    def test_bad_values(self) -> None:
        """Unreadable values exit 2."""
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values.json"
            values.write_text("[1, 2]", encoding="utf-8")
            status, out = self.main("check", "--values", str(values), "--source")
        self.assertEqual(status, 2)
        self.assertIn("is not a map", out)


if __name__ == "__main__":
    unittest.main()
