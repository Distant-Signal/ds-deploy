"""Tests for scripts/pin-images.py, against a fake registry.

uv run python -m unittest discover -s scripts/tests
"""

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from collections.abc import Mapping
from email.message import Message
from types import ModuleType
from typing import override

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "pin-images.py"
REPO = SCRIPTS.parent


def _load(name: str, path: pathlib.Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pin_images = _load("pin_images", SCRIPT)
chart_pins = _load("chart_pins", SCRIPTS / "tests" / "test_chart_pins.py")

COMMIT = "a" * 40
TAG = f"build.123-{COMMIT}"
BASE = "https://ghcr.io/v2/distant-signal/images"
ZERO_DIGEST = "sha256:" + "0" * 64

VALUES = """\
pollers:
  tfl:
    image:
      repository: ghcr.io/distant-signal/images/poller-tfl
      tag: "build.1-0000000000000000000000000000000000000000"
      # -- Content digest override.
      digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      # -- Source repository. See api.image.source.
      source: https://github.com/Distant-Signal/distant-signal
      pullPolicy: IfNotPresent
    baseUrl: ""
api:
  image:
    repository: ghcr.io/distant-signal/images/api
    tag: "build.1-0000000000000000000000000000000000000000"
    digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    pullPolicy: IfNotPresent
  tag: not-the-image-tag
migrate:
  image:
    repository: ghcr.io/distant-signal/images/ds-migrate
    tag: v0.2.0
    digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    source: https://github.com/Distant-Signal/ds-core
scheduleFeed:
  ingest:
    image:
      repository: ghcr.io/distant-signal/images/schedule-ingest
      tag: "build.1-0000000000000000000000000000000000000000"
      digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      source: https://github.com/Distant-Signal/ds-api
"""


class FakeRegistry:
    """A registry serving one image per service, multi-platform, behind a token."""

    def __init__(self, labels: Mapping[str, Mapping[str, str]]) -> None:
        """Serve an image index for each service, with these config labels."""
        self.blobs: dict[str, bytes] = {}
        self.tags: dict[str, bytes] = {}
        self.urls: list[str] = []
        for service, image_labels in labels.items():
            config = json.dumps({"config": {"Labels": dict(image_labels)}}).encode()
            arm = self._store(service, json.dumps({"config": {"digest": "x"}}).encode())
            amd64 = self._store(
                service,
                json.dumps(
                    {"config": {"digest": self._store(service, config)}}
                ).encode(),
            )
            index = {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "digest": arm,
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                    {
                        "digest": amd64,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                ],
            }
            self.tags[f"{service}:{TAG}"] = json.dumps(index).encode()

    def _store(self, service: str, body: bytes) -> str:
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        self.blobs[f"{service}@{digest}"] = body
        return digest

    def digest(self, service: str) -> str:
        """Return the tag's index digest."""
        return f"sha256:{hashlib.sha256(self.tags[f'{service}:{TAG}']).hexdigest()}"

    def __call__(
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[bytes, Mapping[str, str]]:
        """Answer one GET."""
        self.urls.append(url)
        if url.startswith("https://ghcr.io/token?"):
            return json.dumps({"token": "t"}).encode(), {}
        if headers.get("Authorization") != "Bearer t":
            challenge = Message()
            challenge["WWW-Authenticate"] = (
                'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                'scope="repository:distant-signal/images/x:pull"'
            )
            raise urllib.error.HTTPError(url, 401, "Unauthorized", challenge, None)
        service, kind, ref = url.removeprefix(f"{BASE}/").split("/")
        body = self.tags.get(f"{service}:{ref}") or self.blobs.get(f"{service}@{ref}")
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        del kind
        return body, {}


def labels(source: str | None, revision: str | None = None) -> dict[str, str]:
    """Return an image's OCI labels."""
    found = {"org.opencontainers.image.licenses": "AGPL-3.0-or-later"}
    if source is not None:
        found[pin_images.SOURCE_LABEL] = source
    if revision is not None:
        found[pin_images.REVISION_LABEL] = revision
    return found


class PinImagesTest(unittest.TestCase):
    """scripts/pin-images.py."""

    @override
    def setUp(self) -> None:
        """Retry a failed cosign without waiting."""
        patcher = unittest.mock.patch.object(pin_images, "VERIFY_BACKOFF_SECS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pin(
        self, registry: FakeRegistry, *services: str, values_text: str = VALUES
    ) -> tuple[int, str]:
        """Pin SERVICES at TAG in a copy of VALUES; return the exit code and file."""
        with tempfile.TemporaryDirectory() as tmp:
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(values_text, "utf-8")
            argv = ["--values", str(values), "--tag", TAG, *services]
            with unittest.mock.patch("sys.stderr"):
                code = pin_images.main(argv, fetch=registry)
            return code, values.read_text("utf-8")

    def test_rewrites_an_existing_source_line_from_the_label(self) -> None:
        """Tag, digest and source change; the comments and other keys stay."""
        registry = FakeRegistry(
            {
                "poller-tfl": labels(
                    "https://github.com/Distant-Signal/ds-pollers", COMMIT
                )
            }
        )
        code, text = self.run_pin(registry, "poller-tfl")
        self.assertEqual(code, 0)
        expected = (
            VALUES.replace(
                'tag: "build.1-0000000000000000000000000000000000000000"\n      #',
                f'tag: "{TAG}"\n      #',
            )
            .replace(
                '"sha256:0000000000000000000000000000000000000000000000000000000000000000"\n'
                "      # -- Source",
                f'"{registry.digest("poller-tfl")}"\n      # -- Source',
            )
            .replace("Distant-Signal/distant-signal", "Distant-Signal/ds-pollers")
        )
        self.assertEqual(text, expected)

    def test_adds_a_missing_source_line_after_the_digest(self) -> None:
        """A map with no source gets the line and the comment the others carry."""
        registry = FakeRegistry(
            {"api": labels("https://github.com/Distant-Signal/ds-api")}
        )
        code, text = self.run_pin(registry, "api")
        self.assertEqual(code, 0)
        self.assertIn(
            "    repository: ghcr.io/distant-signal/images/api\n"
            f'    tag: "{TAG}"\n'
            f'    digest: "{registry.digest("api")}"\n'
            "    # -- Source repository. See api.image.source.\n"
            "    source: https://github.com/Distant-Signal/ds-api\n"
            "    pullPolicy: IfNotPresent\n"
            "  tag: not-the-image-tag\n",
            text,
        )

    def test_keeps_an_unquoted_tag_unquoted(self) -> None:
        """ds-migrate's release tag is written the way the line had it."""
        registry = FakeRegistry(
            {"ds-migrate": labels("https://github.com/Distant-Signal/ds-core")}
        )
        code, text = self.run_pin(registry, "ds-migrate")
        self.assertEqual(code, 0)
        self.assertIn(f"    tag: {TAG}\n", text)

    def test_falls_back_to_the_components_group_repository(self) -> None:
        """No source label: the component's group's repository, with a warning."""
        registry = FakeRegistry({"poller-tfl": labels(None)})
        code, text = self.run_pin(registry, "poller-tfl")
        self.assertEqual(code, 0)
        self.assertIn("source: https://github.com/Distant-Signal/ds-pollers\n", text)

    def test_fallback_maps_every_pinned_image(self) -> None:
        """Every image the chart pins has a fallback repository."""
        components = pin_images.yaml.safe_load(pin_images.COMPONENTS.read_text("utf-8"))
        values = pin_images.yaml.safe_load(pin_images.VALUES.read_text("utf-8"))
        for service, _tag, _digest, source in chart_pins.pinned_images(values):
            with self.subTest(service):
                self.assertEqual(
                    pin_images.fallback_source(service, components), source
                )

    def test_a_revision_other_than_the_tags_commit_writes_nothing(self) -> None:
        """The revision label must be the build tag's commit."""
        registry = FakeRegistry(
            {
                "poller-tfl": labels(
                    "https://github.com/Distant-Signal/ds-pollers", "b" * 40
                )
            }
        )
        code, text = self.run_pin(registry, "poller-tfl")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)

    def test_a_source_outside_the_organisation_writes_nothing(self) -> None:
        """The source must be a Distant-Signal repository."""
        registry = FakeRegistry({"poller-tfl": labels("https://example.com/fork")})
        code, text = self.run_pin(registry, "poller-tfl")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)

    def test_an_unknown_service_writes_nothing(self) -> None:
        """Every named service needs an image map; the others aren't written either."""
        registry = FakeRegistry(
            {
                "api": labels("https://github.com/Distant-Signal/ds-api"),
                "poller-nope": labels("https://github.com/Distant-Signal/ds-pollers"),
            }
        )
        code, text = self.run_pin(registry, "api", "poller-nope")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)

    def test_a_missing_tag_writes_nothing(self) -> None:
        """A tag the registry doesn't have fails without retrying."""
        registry = FakeRegistry({})
        code, text = self.run_pin(registry, "api")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)
        self.assertEqual(sum("/manifests/" in url for url in registry.urls), 2)

    def test_moves_a_schedule_feed_pin_whose_digest_changed(self) -> None:
        """A changed schedule-feed image is pinned as any other."""
        registry = FakeRegistry(
            {"schedule-ingest": labels("https://github.com/Distant-Signal/ds-api")}
        )
        code, text = self.run_pin(registry, "schedule-ingest")
        self.assertEqual(code, 0)
        self.assertIn(
            "      repository: ghcr.io/distant-signal/images/schedule-ingest\n"
            f'      tag: "{TAG}"\n'
            f'      digest: "{registry.digest("schedule-ingest")}"\n',
            text,
        )

    def test_keeps_a_schedule_feed_pin_whose_digest_is_unchanged(self) -> None:
        """The same digest at a newer build leaves the pin, tag included."""
        registry = FakeRegistry(
            {"schedule-ingest": labels("https://github.com/Distant-Signal/ds-api")}
        )
        digest = registry.digest("schedule-ingest")
        values = VALUES.replace(
            f'      digest: "{ZERO_DIGEST}"\n'
            "      source: https://github.com/Distant-Signal/ds-api\n",
            f'      digest: "{digest}"\n'
            "      source: https://github.com/Distant-Signal/ds-api\n",
        )
        self.assertNotEqual(values, VALUES)
        code, text = self.run_pin(registry, "schedule-ingest", values_text=values)
        self.assertEqual(code, 0)
        self.assertEqual(text, values)

    def test_moves_another_services_tag_even_when_its_digest_is_unchanged(
        self,
    ) -> None:
        """Only schedule-feed keeps its pin: the api's tag is release-order's rule 2."""
        registry = FakeRegistry(
            {"api": labels("https://github.com/Distant-Signal/ds-api")}
        )
        values = VALUES.replace(
            f'    digest: "{ZERO_DIGEST}"\n    pullPolicy',
            f'    digest: "{registry.digest("api")}"\n    pullPolicy',
        )
        self.assertNotEqual(values, VALUES)
        code, text = self.run_pin(registry, "api", values_text=values)
        self.assertEqual(code, 0)
        self.assertIn(f'    tag: "{TAG}"\n', text)

    def test_refuses_schedule_feed_with_other_services(self) -> None:
        """schedule-feed's images go in a run (and a PR) of their own."""
        registry = FakeRegistry(
            {
                "api": labels("https://github.com/Distant-Signal/ds-api"),
                "schedule-ingest": labels("https://github.com/Distant-Signal/ds-api"),
            }
        )
        code, text = self.run_pin(registry, "api", "schedule-ingest")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)
        self.assertEqual(registry.urls, [], "refused before any registry read")
        code, text = self.run_pin(
            registry, "--with-schedule-feed", "api", "schedule-ingest"
        )
        self.assertEqual(code, 0)
        self.assertIn(
            f'      tag: "{TAG}"\n'
            f'      digest: "{registry.digest("schedule-ingest")}"\n',
            text,
        )

    def test_reads_the_amd64_config_with_a_token(self) -> None:
        """The amd64 image's config is read, after the anonymous token."""
        registry = FakeRegistry(
            {"api": labels("https://github.com/Distant-Signal/ds-api")}
        )
        image = pin_images.resolve("api", TAG, registry)
        self.assertEqual(image.digest, registry.digest("api"))
        self.assertEqual(
            image.labels[pin_images.SOURCE_LABEL],
            "https://github.com/Distant-Signal/ds-api",
        )
        self.assertTrue(registry.urls[1].startswith("https://ghcr.io/token?"))
        self.assertNotIn(f"{BASE}/api/blobs/x", registry.urls)

    def run_with_cosign(
        self, exit_code: int, *services: str
    ) -> tuple[int, str, str, str]:
        """Pin SERVICES with --verify-signatures and a stub cosign.

        Returns the exit code, the file, the stub's recorded arguments and
        the api's digest.
        """
        registry = FakeRegistry(
            {
                "api": labels("https://github.com/Distant-Signal/ds-api"),
                "poller-tfl": labels("https://github.com/Distant-Signal/ds-pollers"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            stub = pathlib.Path(tmp) / "cosign"
            seen = pathlib.Path(tmp) / "seen"
            stub.write_text(
                f'#!/bin/sh\necho "$@" >>"{seen}"\nexit {exit_code}\n', "utf-8"
            )
            stub.chmod(0o755)
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(VALUES, "utf-8")
            argv = [
                "--values",
                str(values),
                "--tag",
                TAG,
                "--verify-signatures",
                "--cosign",
                str(stub),
                *services,
            ]
            with unittest.mock.patch("sys.stderr"):
                code = pin_images.main(argv, fetch=registry)
            recorded = seen.read_text("utf-8") if seen.exists() else ""
            return code, values.read_text("utf-8"), recorded, registry.digest("api")

    def test_verifies_each_image_by_digest_before_writing(self) -> None:
        """Cosign gets the digest reference, the issuer and the identity."""
        code, text, seen, digest = self.run_with_cosign(0, "api", "poller-tfl")
        self.assertEqual(code, 0)
        self.assertIn(f'tag: "{TAG}"', text)
        self.assertIn(f"ghcr.io/distant-signal/images/api@{digest}", seen)
        self.assertIn("images/poller-tfl@sha256:", seen)
        self.assertIn(f"--certificate-oidc-issuer {pin_images.OIDC_ISSUER}", seen)
        self.assertIn(
            f"--certificate-identity-regexp {pin_images.IDENTITY_REGEXP}", seen
        )

    def test_an_unverified_image_writes_nothing(self) -> None:
        """A failed `cosign verify` refuses the run and leaves values alone."""
        code, text, seen, _ = self.run_with_cosign(1, "api", "poller-tfl")
        self.assertEqual(code, 1)
        self.assertEqual(text, VALUES)
        self.assertEqual(
            len(seen.splitlines()), pin_images.VERIFY_ATTEMPTS, "one image, retried"
        )

    def test_signatures_are_not_checked_by_default(self) -> None:
        """Without the flag cosign is never run, even when it would fail."""
        registry = FakeRegistry(
            {"api": labels("https://github.com/Distant-Signal/ds-api")}
        )
        with tempfile.TemporaryDirectory() as tmp:
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(VALUES, "utf-8")
            argv = ["--values", str(values), "--tag", TAG, "--cosign", "/nonexistent"]
            with unittest.mock.patch("sys.stderr"):
                code = pin_images.main([*argv, "api"], fetch=registry)
        self.assertEqual(code, 0)

    def test_a_missing_cosign_is_an_error_not_a_pass(self) -> None:
        """--verify-signatures without a working cosign refuses the run."""
        registry = FakeRegistry(
            {"api": labels("https://github.com/Distant-Signal/ds-api")}
        )
        with tempfile.TemporaryDirectory() as tmp:
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(VALUES, "utf-8")
            argv = [
                "--values",
                str(values),
                "--tag",
                TAG,
                "--verify-signatures",
                "--cosign",
                "/nonexistent/cosign",
                "api",
            ]
            with unittest.mock.patch("sys.stderr"):
                code = pin_images.main(argv, fetch=registry)
            self.assertEqual(code, 1)
            self.assertEqual(values.read_text("utf-8"), VALUES)

    def test_verify_pinned_checks_every_pin_and_writes_nothing(self) -> None:
        """Each pinned image is checked by the digest values.yaml holds."""
        checked: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(VALUES, "utf-8")
            count = pin_images.verify_pinned(values, checked.append)
            self.assertEqual(values.read_text("utf-8"), VALUES)
        self.assertEqual(count, VALUES.count("repository: ghcr.io/"))
        self.assertEqual(len(checked), count)
        for service in ("api", "poller-tfl"):
            self.assertIn(
                f"ghcr.io/distant-signal/images/{service}@{ZERO_DIGEST}", checked
            )

    def test_verify_pinned_refuses_a_failure_and_a_malformed_digest(self) -> None:
        """One unverifiable image, or a digest that isn't sha256, is an error."""

        def refuse(reference: str) -> None:
            raise pin_images.PinError(reference)

        with tempfile.TemporaryDirectory() as tmp:
            values = pathlib.Path(tmp) / "values.yaml"
            values.write_text(VALUES, "utf-8")
            with self.assertRaises(pin_images.PinError):
                pin_images.verify_pinned(values, refuse)
            values.write_text(VALUES.replace(ZERO_DIGEST, "sha256:abc"), "utf-8")
            with self.assertRaises(pin_images.PinError):
                pin_images.verify_pinned(values, lambda _: None)

    def test_the_identity_is_exactly_the_image_workflow(self) -> None:
        """Only image.yml at main or a full commit SHA, in Distant-Signal/ci."""
        identity = re.compile(pin_images.IDENTITY_REGEXP)
        base = "https://github.com/Distant-Signal/ci/.github/workflows/image.yml"
        for good in (f"{base}@refs/heads/main", f"{base}@{'a' * 40}"):
            self.assertIsNotNone(identity.match(good), good)
        for bad in (
            f"{base}@refs/heads/other",
            f"{base}@refs/heads/main2",
            f"{base}@{'a' * 39}",
            f"{base}x@refs/heads/main",
            f"{base}@{'A' * 40}",
            f"{base.replace('/ci/', '/ci2/')}@refs/heads/main",
            f"{base.replace('Distant-Signal', 'Other-Org')}@refs/heads/main",
            f"{base.replace('image.yml', 'other.yml')}@refs/heads/main",
            f"x{base}@refs/heads/main",
        ):
            self.assertIsNone(identity.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
