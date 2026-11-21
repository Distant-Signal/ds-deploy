#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check an image pin change never restarts the api through Reloader.

  uv run scripts/check-reloader-chart.py [--helm HELM]

Production runs Stakater Reloader with `reloader.stakater.com/auto` on the
chart's workloads (set from `podAnnotations` in the production values), which
restarts a workload when any ConfigMap or Secret it references changes. The
source-images ConfigMap (`images.json`, the api's /public/version list)
changes with every image pin, so it must not restart the api: the api re-reads
the file per request and the kubelet updates the mounted ConfigMap in place.
This renders the chart (with `reloader.stakater.com/auto` on the api, as in
production) and checks:

  - the source-images ConfigMap carries `reloader.stakater.com/ignore: "true"`
    (Reloader never restarts any workload for it; `auto` still covers the
    api's other ConfigMaps and Secrets);
  - the api container mounts it as a directory: no `subPath` (the kubelet
    never updates a subPath mount, so the list would go stale) and the file
    is the one API_SOURCE_IMAGES_FILE names;
  - no Deployment, StatefulSet or DaemonSet but the api references it, and
    nothing restarts the api for it by name: no `*.reloader.stakater.com/reload`
    annotation lists it and no pod template checksum covers it;
  - the production-style `auto` annotation is still on the api's pod template
    (the marker, not the removal of `auto`, is what stops the restarts).

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from typing import cast

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = str(REPO / "charts" / "distant-signal")
API = "distant-signal-api"
SOURCE_IMAGES = "distant-signal-source-images"
IGNORE = "reloader.stakater.com/ignore"
AUTO = "reloader.stakater.com/auto"
IMAGES_FILE = "/etc/distant-signal/source/images.json"
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")

# The least a fresh install adds (see check-enricher-provider-chart.py), plus
# the production-style Reloader annotation on the api pod template.
ARGS = (
    "-f",
    str(REPO / "charts" / "distant-signal" / "ci" / "ingest-prereqs.yaml"),
    "--set",
    "trustConsumer.kafka.brokers=k:9094",
    "--set",
    "trustConsumer.kafka.topic=t",
    "--set",
    "trustConsumer.kafka.saslMechanism=PLAIN",
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
    "--set",
    "enricher.llm.baseUrl=http://l/v1",
    "--set",
    "enricher.llm.model=m",
    "--set-string",
    r"api.podAnnotations.reloader\.stakater\.com/auto=true",
)

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


def name_of(doc: Doc) -> str:
    """Return the document's metadata.name."""
    return str(as_map(doc.get("metadata")).get("name"))


def render(helm: str) -> list[Doc]:
    """Render the chart; exit 1 when it fails."""
    result = subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
        [helm, "template", "distant-signal", CHART, *ARGS],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"render failed: {result.stderr.strip()}")
        sys.exit(1)
    return [cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if d]


def pod_spec(doc: Doc) -> dict[str, object]:
    """Return a workload's pod spec."""
    template = as_map(as_map(doc.get("spec")).get("template"))
    return as_map(template.get("spec"))


def mentions(doc: Doc) -> bool:
    """Whether the workload names the source-images ConfigMap anywhere."""
    return SOURCE_IMAGES in json.dumps(doc)


def mounts(doc: Doc) -> Iterator[dict[str, object]]:
    """Yield the volume mounts of the workload's containers."""
    spec = pod_spec(doc)
    containers = [
        *as_list(spec.get("containers")),
        *as_list(spec.get("initContainers")),
    ]
    for container in containers:
        for mount in as_list(as_map(container).get("volumeMounts")):
            yield as_map(mount)


def check_mount(api: Doc) -> list[str]:
    """Check the api's mount of the ConfigMap updates in place."""
    failures: list[str] = []
    volumes = [
        as_map(v).get("name")
        for v in as_list(pod_spec(api).get("volumes"))
        if as_map(as_map(v).get("configMap")).get("name") == SOURCE_IMAGES
    ]
    source_mounts = [m for m in mounts(api) if m.get("name") in volumes]
    if len(source_mounts) != 1:
        failures.append(
            f"{API} must mount {SOURCE_IMAGES} once, got {len(source_mounts)}"
        )
    for mount in source_mounts:
        if "subPath" in mount or "subPathExpr" in mount:
            failures.append(
                "the source-images mount must not use subPath (never updated)"
            )
        if not IMAGES_FILE.startswith(f"{mount.get('mountPath')}/"):
            failures.append(
                f"{IMAGES_FILE} is not under the mount at {mount.get('mountPath')}"
            )
    env = {
        str(as_map(e).get("name")): as_map(e).get("value")
        for c in as_list(pod_spec(api).get("containers"))
        for e in as_list(as_map(c).get("env"))
    }
    if env.get("API_SOURCE_IMAGES_FILE") != IMAGES_FILE:
        failures.append(
            f"API_SOURCE_IMAGES_FILE must be {IMAGES_FILE}, "
            f"got {env.get('API_SOURCE_IMAGES_FILE')!r}"
        )
    return failures


def check_annotations(api: Doc) -> list[str]:
    """Check nothing names the ConfigMap as a restart trigger and `auto` stays."""
    failures: list[str] = []
    template = as_map(as_map(api.get("spec")).get("template"))
    pod_annotations = as_map(as_map(template.get("metadata")).get("annotations"))
    deployment_annotations = as_map(as_map(api.get("metadata")).get("annotations"))
    for where, found in (
        ("pod template", pod_annotations),
        ("Deployment", deployment_annotations),
    ):
        for key, value in found.items():
            if key.endswith(".reloader.stakater.com/reload") and SOURCE_IMAGES in str(
                value
            ):
                failures.append(f"{API} {where}: {key} must not list {SOURCE_IMAGES}")
            if key.startswith("checksum/") and key.endswith("source-images"):
                failures.append(
                    f"{API} {where}: {key} would restart the api on every pin"
                )
    if pod_annotations.get(AUTO) != "true":
        failures.append(
            f"{API} pod template must keep {AUTO}, got {pod_annotations.get(AUTO)!r}"
        )
    return failures


def check(docs: Sequence[Doc]) -> list[str]:
    """Return the failed checks."""
    config_maps = [
        d for d in docs if d.get("kind") == "ConfigMap" and name_of(d) == SOURCE_IMAGES
    ]
    if len(config_maps) != 1:
        return [f"want one {SOURCE_IMAGES} ConfigMap, got {len(config_maps)}"]
    failures: list[str] = []
    annotations = as_map(as_map(config_maps[0].get("metadata")).get("annotations"))
    if annotations.get(IGNORE) != "true":
        failures.append(
            f'{SOURCE_IMAGES} must carry {IGNORE}: "true", '
            f"got {annotations.get(IGNORE)!r}"
        )
    consumers = [name_of(d) for d in docs if d.get("kind") in WORKLOADS and mentions(d)]
    if consumers != [API]:
        failures.append(f"only {API} may reference {SOURCE_IMAGES}, got {consumers}")
    api = next(
        (d for d in docs if d.get("kind") == "Deployment" and name_of(d) == API), {}
    )
    if not api:
        return [*failures, f"no {API} Deployment rendered"]
    return [*failures, *check_mount(api), *check_annotations(api)]


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    failures = check(render(cast("str", args.helm)))
    for failure in failures:
        print(failure)
    if not failures:
        print("ok: image pins do not restart the api through Reloader")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
