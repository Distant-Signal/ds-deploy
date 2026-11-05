#!/usr/bin/env python3
"""Pin services' images in the chart's values.yaml: tag, digest and source.

  uv run --no-sync scripts/pin-images.py --tag build.<run id>-<commit> SERVICE...

The hand bump while Renovate is idle (README.md, "How a change reaches the
cluster"): after a service repository's `main` build, pin that build's
services. For each SERVICE (an image under ghcr.io/distant-signal/images/,
the same name as the `repository:` line ends with), this reads TAG's
manifest anonymously from GHCR and writes, in the image map after that
`repository:` line:

- `tag:` TAG;
- `digest:` the manifest's content digest;
- `source:` the image's `org.opencontainers.image.source` label (from the
  linux/amd64 image's config, as Distant-Signal/ci's release-order.py reads
  it, so its source check passes), adding the line, with the comment the
  other pins carry, when the map has none.

An image with no source label (built before its repository stamped it)
falls back to its component's repository: its component's group in
charts/distant-signal/components.yaml, and GROUP_REPOSITORIES below. That
is what the chart names for it, but release-order.py still fails on the
missing label, so this warns. When the image has an
`org.opencontainers.image.revision` label, it must be the build tag's
commit: anything else means TAG is not the build it names, and nothing is
written. Only the `tag:`, `digest:` and `source:` lines change, so the
comments and layout stay.
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
VALUES = REPO / "charts" / "distant-signal" / "values.yaml"
COMPONENTS = REPO / "charts" / "distant-signal" / "components.yaml"
REGISTRY = "ghcr.io"
IMAGES = "distant-signal/images"
SOURCE_LABEL = "org.opencontainers.image.source"
REVISION_LABEL = "org.opencontainers.image.revision"

# The chart's pins: what tests/test_chart_pins.py accepts.
SOURCE = re.compile(r"^https://github\.com/Distant-Signal/[a-z-]+$")
BUILD_TAG = re.compile(r"^build\.\d+-([0-9a-f]{40})$")
REPOSITORY_LINE = re.compile(
    rf"^(?P<indent>\s*)repository: {re.escape(REGISTRY)}/{re.escape(IMAGES)}/"
    r"(?P<service>[a-z0-9-]+)\s*$"
)
KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z][A-Za-z0-9_-]*):")
SOURCE_COMMENT = "# -- Source repository. See api.image.source."

# The repository that builds each components.yaml group's images (the
# fallback for an image with no source label).
GROUP_REPOSITORIES = {
    "api": "https://github.com/Distant-Signal/ds-api",
    "schedule-feed": "https://github.com/Distant-Signal/ds-api",
    "frontend": "https://github.com/Distant-Signal/distant-signal",
    "ingest": "https://github.com/Distant-Signal/ds-ingest",
    "migrate": "https://github.com/Distant-Signal/ds-core",
    "pollers": "https://github.com/Distant-Signal/ds-pollers",
    "workers": "https://github.com/Distant-Signal/ds-workers",
}
# Images whose workload's component label is not the image's name.
IMAGE_COMPONENTS = {"ds-migrate": "migrate", "schedule-ingest": "schedulefeed"}

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)

Fetch = Callable[[str, Mapping[str, str]], tuple[bytes, Mapping[str, str]]]


class PinError(Exception):
    """A pin that can't be made; nothing is written."""


@dataclass(frozen=True)
class Image:
    """What a build tag resolves to."""

    digest: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Pin:
    """The three values written for one service."""

    tag: str
    digest: str
    source: str


# --- the registry ---------------------------------------------------------


def http_fetch(url: str, headers: Mapping[str, str]) -> tuple[bytes, Mapping[str, str]]:
    """GET URL; return the body and headers. HTTP errors raise."""
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310  # https registry URLs
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # same
        return response.read(), dict(response.headers)


def bearer_token(fetch: Fetch, challenge: str) -> str:
    """Return an anonymous token for a `Bearer realm=...` challenge."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    if not realm.startswith("https://"):
        msg = f"unexpected registry auth challenge: {challenge!r}"
        raise PinError(msg)
    query = "&".join(
        f"{k}={urllib.parse.quote(v, safe=':/')}" for k, v in params.items()
    )
    body, _ = fetch(f"{realm}?{query}", {})
    reply = json.loads(body)
    token = reply.get("token") or reply.get("access_token")
    if not isinstance(token, str):
        msg = f"no token from {realm}"
        raise PinError(msg)
    return token


def resolve(service: str, tag: str, fetch: Fetch = http_fetch) -> Image:
    """Return TAG's manifest digest and its linux/amd64 config's labels."""
    base = f"https://{REGISTRY}/v2/{IMAGES}/{service}"
    headers = {"Accept": MANIFEST_TYPES}
    try:
        body, _ = fetch(f"{base}/manifests/{tag}", headers)
    except urllib.error.HTTPError as error:
        if error.code != 401:  # noqa: PLR2004  # Unauthorized: get a token
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
        headers["Authorization"] = f"Bearer {bearer_token(fetch, challenge)}"
        body, _ = fetch(f"{base}/manifests/{tag}", headers)
    # The digest of the bytes the tag points at (what `<repo>@<digest>`
    # pulls), not a header a proxy could rewrite.
    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    manifest: dict[str, Any] = json.loads(body)
    if "manifests" in manifest:
        chosen = [
            m
            for m in manifest["manifests"]
            if m.get("platform", {}).get("os") == "linux"
            and m.get("platform", {}).get("architecture") == "amd64"
        ]
        if not chosen:
            msg = f"{service}:{tag}: no linux/amd64 image in its index"
            raise PinError(msg)
        body, _ = fetch(f"{base}/manifests/{chosen[0]['digest']}", headers)
        manifest = json.loads(body)
    blob_headers = {k: v for k, v in headers.items() if k != "Accept"}
    body, _ = fetch(f"{base}/blobs/{manifest['config']['digest']}", blob_headers)
    labels = json.loads(body).get("config", {}).get("Labels") or {}
    return Image(digest, {str(k): str(v) for k, v in labels.items()})


def resolve_with_retries(
    service: str, tag: str, fetch: Fetch = http_fetch, attempts: int = 3
) -> Image:
    """Resolve, retried after a network or server error."""
    for attempt in range(1, attempts + 1):
        try:
            return resolve(service, tag, fetch)
        except (urllib.error.URLError, TimeoutError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code < 500:  # noqa: PLR2004
                msg = (
                    f"{service}:{tag}: HTTP {error.code} "
                    "(no such tag, or the package isn't public)"
                )
                raise PinError(msg) from error
            if attempt == attempts:
                msg = f"{service}:{tag}: {error}"
                raise PinError(msg) from error
            time.sleep(5 * attempt)
    raise AssertionError  # unreachable


# --- the source -----------------------------------------------------------


def fallback_source(service: str, components: Mapping[str, Any]) -> str:
    """Return the repository components.yaml's group says builds SERVICE."""
    component = IMAGE_COMPONENTS.get(service, service)
    group = (components.get("components") or {}).get(component)
    if group not in GROUP_REPOSITORIES:
        msg = (
            f"{service}: no {SOURCE_LABEL} label, and component {component!r} "
            f"(group {group!r}) has no repository in GROUP_REPOSITORIES"
        )
        raise PinError(msg)
    return GROUP_REPOSITORIES[group]


def pin_for(
    service: str, tag: str, image: Image, components: Mapping[str, Any]
) -> tuple[Pin, list[str]]:
    """Return SERVICE's pin from its image, and any warnings."""
    warnings: list[str] = []
    build = BUILD_TAG.match(tag)
    revision = image.labels.get(REVISION_LABEL)
    if build and revision and revision != build.group(1):
        msg = (
            f"{service}:{tag}: its {REVISION_LABEL} label is {revision}, "
            f"not the tag's commit {build.group(1)}"
        )
        raise PinError(msg)
    source = image.labels.get(SOURCE_LABEL)
    if source is None:
        source = fallback_source(service, components)
        warnings.append(
            f"{service}: no {SOURCE_LABEL} label; wrote {source} from "
            "components.yaml, but release-order.py check will fail on it"
        )
    if not SOURCE.match(source):
        msg = f"{service}: source {source!r} is not a Distant-Signal repository"
        raise PinError(msg)
    return Pin(tag, image.digest, source), warnings


# --- values.yaml ----------------------------------------------------------


def set_value(line: str, pad: str, key: str, value: str) -> str:
    """Return LINE with KEY's new VALUE, quoted the way the old one was."""
    old = line.split(":", 1)[1].strip()
    quote = old[0] if old[:1] in {'"', "'"} else ""
    return f"{pad}{key}: {quote}{value}{quote}\n"


def edit_block(lines: list[str], start: int, service: str, pin: Pin) -> int:
    """Rewrite one image map's lines after its repository line (at START).

    Returns the index of the first line after the map.
    """
    repository = REPOSITORY_LINE.match(lines[start])
    assert repository is not None  # noqa: S101  # the caller matched it
    width = len(repository.group("indent"))
    found: dict[str, int] = {}
    end = start + 1
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if len(line) - len(line.lstrip()) < width:
                break
            key_line = KEY_LINE.match(line)
            if key_line and len(key_line.group("indent")) == width:
                found.setdefault(key_line.group("key"), end)
        end += 1
    missing = {"tag", "digest"} - found.keys()
    if missing:
        msg = f"{service}: no {' or '.join(sorted(missing))} line in its image map"
        raise PinError(msg)
    pad = " " * width
    for key, value in (
        ("tag", pin.tag),
        ("digest", pin.digest),
        ("source", pin.source),
    ):
        if key in found:
            lines[found[key]] = set_value(lines[found[key]], pad, key, value)
    if "source" in found:
        return end
    at = found["digest"] + 1
    lines[at:at] = [f"{pad}{SOURCE_COMMENT}\n", f"{pad}source: {pin.source}\n"]
    return end + 2


def apply_pins(lines: list[str], pins: Mapping[str, Pin]) -> None:
    """Write each service's pin into LINES (values.yaml's), in place."""
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        match = REPOSITORY_LINE.match(lines[i])
        if match and match.group("service") in pins:
            service = match.group("service")
            if service in seen:
                msg = f"{service}: more than one image map pins it"
                raise PinError(msg)
            seen.add(service)
            i = edit_block(lines, i, service, pins[service])
        else:
            i += 1
    missing = sorted(pins.keys() - seen)
    if missing:
        msg = f"no image map for {', '.join(missing)}"
        raise PinError(msg)


# --- the CLI --------------------------------------------------------------


def run(
    values: Path,
    components_path: Path,
    tag: str,
    services: Sequence[str],
    fetch: Fetch = http_fetch,
) -> list[str]:
    """Pin SERVICES at TAG in VALUES; return the warnings. Raise PinError."""
    components = yaml.safe_load(components_path.read_text("utf-8")) or {}
    pins: dict[str, Pin] = {}
    warnings: list[str] = []
    for service in dict.fromkeys(services):
        image = resolve_with_retries(service, tag, fetch)
        pins[service], found = pin_for(service, tag, image, components)
        warnings += found
    lines = values.read_text("utf-8").splitlines(keepends=True)
    apply_pins(lines, pins)
    values.write_text("".join(lines), "utf-8")
    return warnings


def main(argv: Sequence[str] | None = None, fetch: Fetch = http_fetch) -> int:
    """Pin the services; exit 1 (writing nothing) when one can't be pinned."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tag", required=True, help="build.<run id>-<commit>")
    parser.add_argument("--values", type=Path, default=VALUES)
    parser.add_argument("--components", type=Path, default=COMPONENTS)
    parser.add_argument("services", nargs="+", metavar="SERVICE")
    args = parser.parse_args(argv)
    try:
        warnings = run(args.values, args.components, args.tag, args.services, fetch)
    except PinError as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201  # CLI output
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)  # noqa: T201  # CLI output
    count = len(dict.fromkeys(args.services))
    print(f"pinned {count} image(s) at {args.tag}", file=sys.stderr)  # noqa: T201  # CLI output
    return 0


if __name__ == "__main__":
    sys.exit(main())
