#!/usr/bin/env python3
"""Pin services' images in the chart's values.yaml: tag, digest and source.

  uv run --no-sync scripts/pin-images.py --tag build.<run id>-<commit> SERVICE...
  uv run --no-sync scripts/pin-images.py --verify-pinned   # check, write nothing

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

The schedule-feed images (schedule-ingest and schedule-reference, the
components.yaml `schedule-feed` group) move only when they change (DS-403).
Any chart build that changes a schedule-feed object falls in the dtd-sftp
deploy window (components.yaml's header), and ds-api builds them with the
api from every commit, so:

- a schedule-feed image whose digest at TAG is the one already pinned keeps
  its pin, tag included (its images are reproducible: a build of unchanged
  inputs has the same digest);
- schedule-feed images are pinned in a run of their own, never with other
  services, so an api release never carries them. --with-schedule-feed
  allows the mix, for a release that needs both at once (a contract
  migration's floor, say).

A tag is mutable, so the digest this writes is whatever the registry says
the tag is now. --verify-signatures (off by default) also runs `cosign
verify` on each image by that digest, against the keyless identity of
Distant-Signal/ci's image.yml (IDENTITY_REGEXP below: the workflow at
refs/heads/main or at a full commit SHA, the two refs its callers use), and
writes nothing when one fails to verify. It needs cosign and network access
to the registry and Sigstore; --cosign names the binary. --verify-pinned
does that check alone, on every image values.yaml pins now (no tag, no
services, nothing written): the CI job's check, when cosign is installed.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
VERIFY_WORKERS = 8
VERIFY_ATTEMPTS = 3
VERIFY_BACKOFF_SECS = 5
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
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
# The components.yaml group whose images move on their own (the docstring).
SCHEDULE_FEED = "schedule-feed"

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)

# The signature check (--verify-signatures): the issuer and the certificate
# identity of Distant-Signal/ci's reusable image.yml, which signs every
# first-party image whichever repository calls it. The ref is main (its own
# self-test) or the full commit SHA a caller pins, nothing else.
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
IDENTITY_REGEXP = (
    r"^https://github\.com/Distant-Signal/ci/\.github/workflows/image\.yml"
    r"@(refs/heads/main|[0-9a-f]{40})$"
)

Fetch = Callable[[str, Mapping[str, str]], tuple[bytes, Mapping[str, str]]]
# Verify one image reference (`<repository>@<digest>`); raise PinError.
Verify = Callable[[str], None]


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
    # Leave the map alone when it already pins this digest.
    keep_if_unchanged: bool = False


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


# --- the signature --------------------------------------------------------


def cosign_verifier(cosign: str) -> Verify:
    """Return a check that `cosign verify` accepts an image reference."""

    def verify(reference: str) -> None:
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            try:
                result = subprocess.run(  # noqa: S603  # cosign, fixed flags
                    [
                        cosign,
                        "verify",
                        "--certificate-oidc-issuer",
                        OIDC_ISSUER,
                        "--certificate-identity-regexp",
                        IDENTITY_REGEXP,
                        reference,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
            except OSError as error:  # no such binary: retrying can't help
                msg = f"{reference}: cosign did not run: {error}"
                raise PinError(msg) from error
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                return
            # A failure may be the network (Sigstore, the registry): try again.
            if attempt < VERIFY_ATTEMPTS:
                time.sleep(attempt * VERIFY_BACKOFF_SECS)
        detail = (
            f"cosign exited {result.returncode}: {result.stderr.strip()[-400:]}"
            if result is not None
            else "cosign timed out"
        )
        msg = f"{reference}: no signature from {IDENTITY_REGEXP} ({detail})"
        raise PinError(msg)

    return verify


def pinned_images(lines: Sequence[str]) -> list[tuple[str, str]]:
    """Return (service, digest) for each first-party image LINES pin."""
    found: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        match = REPOSITORY_LINE.match(line)
        if not match:
            continue
        width = len(match.group("indent"))
        for later in lines[i + 1 :]:
            stripped = later.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(later) - len(later.lstrip()) < width:
                break
            key_line = KEY_LINE.match(later)
            if (
                key_line
                and len(key_line.group("indent")) == width
                and key_line.group("key") == "digest"
            ):
                digest = later.split(":", 1)[1].strip().strip("'\"")
                found.append((match.group("service"), digest))
                break
    return found


def verify_pinned(values: Path, verify: Verify) -> int:
    """Verify every image VALUES pins by its digest; return how many."""
    pinned = pinned_images(values.read_text("utf-8").splitlines())
    if not pinned:
        msg = f"{values}: no first-party image pins found"
        raise PinError(msg)
    references: list[str] = []
    for service, digest in pinned:
        if not DIGEST.match(digest):
            msg = f"{service}: digest {digest!r} is not a sha256 digest"
            raise PinError(msg)
        references.append(f"{REGISTRY}/{IMAGES}/{service}@{digest}")
    # One cosign run takes seconds (Sigstore and registry round trips), so
    # check the images side by side; the first failure is raised. The first
    # one goes alone: on a cold cache cosign fetches Sigstore's trusted root
    # then, and runs that start together race to write it and fail.
    verify(references[0])
    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
        for _ in pool.map(verify, references[1:]):
            pass
    return len(pinned)


# --- the source -----------------------------------------------------------


def group_of(service: str, components: Mapping[str, Any]) -> object:
    """Return SERVICE's components.yaml group (None when it has none)."""
    component = IMAGE_COMPONENTS.get(service, service)
    return (components.get("components") or {}).get(component)


def fallback_source(service: str, components: Mapping[str, Any]) -> str:
    """Return the repository components.yaml's group says builds SERVICE."""
    component = IMAGE_COMPONENTS.get(service, service)
    group = group_of(service, components)
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
    keep = group_of(service, components) == SCHEDULE_FEED
    return Pin(tag, image.digest, source, keep_if_unchanged=keep), warnings


def check_schedule_feed_alone(
    services: Sequence[str], components: Mapping[str, Any]
) -> None:
    """Refuse a run that pins schedule-feed images with other services."""
    feed = [s for s in services if group_of(s, components) == SCHEDULE_FEED]
    others = [s for s in services if s not in feed]
    if feed and others:
        msg = (
            f"{', '.join(feed)} (the {SCHEDULE_FEED} group) and "
            f"{', '.join(others)} in one run: a chart build that changes "
            f"{SCHEDULE_FEED} falls in the dtd-sftp deploy window, so pin its "
            "images in a PR of their own (or pass --with-schedule-feed)"
        )
        raise PinError(msg)


# --- values.yaml ----------------------------------------------------------


def set_value(line: str, pad: str, key: str, value: str) -> str:
    """Return LINE with KEY's new VALUE, quoted the way the old one was."""
    old = line.split(":", 1)[1].strip()
    quote = old[0] if old[:1] in {'"', "'"} else ""
    return f"{pad}{key}: {quote}{value}{quote}\n"


def edit_block(
    lines: list[str], start: int, service: str, pin: Pin, kept: list[str]
) -> int:
    """Rewrite one image map's lines after its repository line (at START).

    Returns the index of the first line after the map. A map that already
    pins the digest of a `keep_if_unchanged` pin is left alone, and SERVICE
    added to KEPT.
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
    pinned = lines[found["digest"]].split(":", 1)[1].strip().strip("'\"")
    if pin.keep_if_unchanged and pinned == pin.digest:
        kept.append(service)
        return end
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


def apply_pins(lines: list[str], pins: Mapping[str, Pin]) -> list[str]:
    """Write each service's pin into LINES (values.yaml's), in place.

    Returns the services left alone because their digest was unchanged.
    """
    seen: set[str] = set()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        match = REPOSITORY_LINE.match(lines[i])
        if match and match.group("service") in pins:
            service = match.group("service")
            if service in seen:
                msg = f"{service}: more than one image map pins it"
                raise PinError(msg)
            seen.add(service)
            i = edit_block(lines, i, service, pins[service], kept)
        else:
            i += 1
    missing = sorted(pins.keys() - seen)
    if missing:
        msg = f"no image map for {', '.join(missing)}"
        raise PinError(msg)
    return kept


# --- the CLI --------------------------------------------------------------


def run(  # noqa: PLR0913  # the CLI's options, each a keyword
    values: Path,
    components_path: Path,
    tag: str,
    services: Sequence[str],
    fetch: Fetch = http_fetch,
    *,
    with_schedule_feed: bool = False,
    verify: Verify | None = None,
) -> list[str]:
    """Pin SERVICES at TAG in VALUES; return the warnings. Raise PinError.

    With VERIFY, each image is checked by its digest first, and nothing is
    written when one fails.
    """
    components = yaml.safe_load(components_path.read_text("utf-8")) or {}
    if not with_schedule_feed:
        check_schedule_feed_alone(services, components)
    pins: dict[str, Pin] = {}
    warnings: list[str] = []
    for service in dict.fromkeys(services):
        image = resolve_with_retries(service, tag, fetch)
        if verify is not None:
            verify(f"{REGISTRY}/{IMAGES}/{service}@{image.digest}")
        pins[service], found = pin_for(service, tag, image, components)
        warnings += found
    lines = values.read_text("utf-8").splitlines(keepends=True)
    kept = apply_pins(lines, pins)
    values.write_text("".join(lines), "utf-8")
    warnings += [
        f"{service}: digest unchanged at {tag}; kept its pin" for service in kept
    ]
    return warnings


def main(argv: Sequence[str] | None = None, fetch: Fetch = http_fetch) -> int:
    """Pin the services; exit 1 (writing nothing) when one can't be pinned."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tag", help="build.<run id>-<commit>")
    parser.add_argument("--values", type=Path, default=VALUES)
    parser.add_argument("--components", type=Path, default=COMPONENTS)
    parser.add_argument(
        "--with-schedule-feed",
        action="store_true",
        help="allow schedule-feed images in a run with other services",
    )
    parser.add_argument(
        "--verify-signatures",
        action="store_true",
        help="cosign-verify each image by its digest first (off by default)",
    )
    parser.add_argument(
        "--verify-pinned",
        action="store_true",
        help="cosign-verify every image values.yaml pins; write nothing",
    )
    parser.add_argument("--cosign", default=shutil.which("cosign") or "cosign")
    parser.add_argument("services", nargs="*", metavar="SERVICE")
    args = parser.parse_args(argv)
    if args.verify_pinned:
        if args.tag or args.services:
            parser.error("--verify-pinned takes no --tag or SERVICE")
        try:
            count = verify_pinned(args.values, cosign_verifier(args.cosign))
        except PinError as error:
            print(f"error: {error}", file=sys.stderr)  # noqa: T201  # CLI output
            return 1
        print(f"verified {count} pinned image(s)", file=sys.stderr)  # noqa: T201  # CLI output
        return 0
    if not args.tag or not args.services:
        parser.error("--tag and at least one SERVICE are required")
    try:
        warnings = run(
            args.values,
            args.components,
            args.tag,
            args.services,
            fetch,
            with_schedule_feed=args.with_schedule_feed,
            verify=cosign_verifier(args.cosign) if args.verify_signatures else None,
        )
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
