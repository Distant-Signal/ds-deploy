#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the report, the drafted table) is stdout
"""Keep the chart README's "Values reference" tables in step with values.yaml.

The README is charts/distant-signal/README.md; the values are
charts/distant-signal/values.yaml.

  scripts/chart-values-doc.py check
      Exit 1, listing them, if any values.yaml key has no README table row
      or any README table row names a key values.yaml does not have.

  scripts/chart-values-doc.py table <section> [<section> ...]
      Print a starting-point Markdown table for the given top-level
      sections: key, default and the first sentence of the key's own
      values.yaml comment. The descriptions are a draft to edit, not
      generated text to paste unread.

A README row documents a key when its first cell is that key in backticks,
or an ancestor of it (so `api.resources` covers `api.resources.limits.memory`).
A `<name>` segment matches any one segment (`pollers.<name>.enabled`). A first
cell may name sibling keys as "`a.b.perMinute` / `.burst`": a key starting
with "." replaces the last segment of the key before it.
Needs PyYAML (pinned in pyproject.toml's `lint` dependency group).
"""

import pathlib
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence

import yaml

CHART = pathlib.Path(__file__).resolve().parent.parent / "charts" / "distant-signal"
VALUES = CHART / "values.yaml"
README = CHART / "README.md"

# Maps documented as one row (their children are free-form).
OPAQUE = {
    "resources",
    "nodeSelector",
    "tolerations",
    "affinity",
    "podAnnotations",
    "podSecurityContext",
    "securityContext",
    "extraEnv",
    "annotations",
    "labels",
    "config",
    "extraRules",
    "hosts",
    "tls",
}

KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):(\s|$)")
ROW_KEYS = re.compile(
    r"^\|\s*(`[A-Za-z0-9_.<>-]+`(?:\s*/\s*`[A-Za-z0-9_.<>-]+`)*)\s*\|"
)
BACKTICKED = re.compile(r"`([^`]+)`")
PLACEHOLDER = re.compile(r"<[^>]+>")
RESOURCE_KEYS = {"requests", "limits"}
MIN_ARGC = {"check": 2, "table": 3}

type Values = Mapping[object, object]


def leaves(node: Values, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yield (dotted key, default) for every documentable key."""
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value and key not in OPAQUE:
            yield from leaves(value, path + ".")
        else:
            yield path, value


def comments() -> dict[str, str]:
    """Map dotted key -> the comment block directly above it in values.yaml."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    for line in VALUES.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("#").strip())
            continue
        match = KEY_LINE.match(line)
        if not match or line.lstrip().startswith("-"):
            pending = []
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match.group(2)))
        text = " ".join(p for p in pending if p and not set(p) <= {"-"})
        out[".".join(k for _, k in stack)] = re.sub(r"^--\s*", "", text)
        pending = []
    return out


def row_keys(cell: str) -> list[str]:
    """Keys a README first cell names ("`a.b.c` / `.d`" names a.b.c and a.b.d)."""
    keys: list[str] = []
    for name in BACKTICKED.findall(cell):
        if name.startswith(".") and keys:
            keys.append(keys[-1].rsplit(".", 1)[0] + name)
        else:
            keys.append(name)
    return keys


def readme_keys(values: Values) -> set[str]:
    """First-cell backticked keys of README table rows that name a value."""
    keys: set[str] = set()
    for line in README.read_text(encoding="utf-8").splitlines():
        match = ROW_KEYS.match(line)
        if match:
            keys.update(
                k for k in row_keys(match.group(1)) if k.split(".")[0] in values
            )
    return keys


def pattern(key: str) -> re.Pattern[str]:
    """Regex for a README key, `<name>` matching any one segment."""
    parts = [
        r"[^.]+" if PLACEHOLDER.fullmatch(p) else re.escape(p) for p in key.split(".")
    ]
    return re.compile(r"\.".join(parts))


def documented(key: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    """Whether a README row covers the key or one of its ancestors."""
    parts = key.split(".")
    ancestors = [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return any(p.fullmatch(a) for p in patterns for a in ancestors)


def _walk(node: object, parts: Sequence[str]) -> bool:
    if not parts:
        return True
    if not isinstance(node, dict):
        return False
    if PLACEHOLDER.fullmatch(parts[0]):
        return any(_walk(v, parts[1:]) for v in node.values())
    return parts[0] in node and _walk(node[parts[0]], parts[1:])


def exists(key: str, values: Values) -> bool:
    """Whether values.yaml has the (README) key."""
    return _walk(values, key.split("."))


def fmt_resources(value: Mapping[object, object]) -> str:
    """Summarise a requests/limits map."""
    parts = []
    for name, label in (("requests", "requests"), ("limits", "limit")):
        part = value.get(name, {})
        if isinstance(part, dict) and part:
            parts.append(
                f"{label} "
                + "/".join(f"`{part[k]}`" for k in ("cpu", "memory") if k in part)
            )
    return ", ".join(parts)


def fmt_default(value: object) -> str:
    """Render a default for the Default column."""
    if value is None or isinstance(value, bool):
        return f"`{str(value).lower()}`" if value is not None else "`null`"
    if isinstance(value, str):
        return f'`"{value}"`' if value == "" else f"`{value}`"
    if isinstance(value, dict) and value and set(value) <= RESOURCE_KEYS:
        return fmt_resources(value)
    if isinstance(value, dict | list):
        if not value:
            return f"`{'{}' if isinstance(value, dict) else '[]'}`"
        return "see values.yaml"
    return f"`{value}`"


def first_sentence(text: str) -> str:
    """First sentence of a comment, escaped for a Markdown table cell."""
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text).replace("|", "\\|")


def check(values: Values) -> int:
    """Report undocumented and stale keys; 1 if there are any."""
    keys = readme_keys(values)
    patterns = [pattern(k) for k in keys]
    missing = [k for k, _ in leaves(values) if not documented(k, patterns)]
    stale = sorted(k for k in keys if not exists(k, values))
    for k in missing:
        print(f"undocumented in README.md: {k}")
    for k in stale:
        print(f"README.md row for a key values.yaml does not have: {k}")
    if missing or stale:
        print(
            f"\n{len(missing)} undocumented, {len(stale)} stale. "
            "Add or fix the rows in charts/distant-signal/README.md "
            "(`scripts/chart-values-doc.py table <section>` drafts them).",
            file=sys.stderr,
        )
        return 1
    return 0


def table(values: Values, sections: Iterable[str]) -> int:
    """Print draft README tables for the given top-level sections."""
    notes = comments()
    for section in sections:
        print(f"### {section}\n\n| Key | Default | Description |\n|---|---|---|")
        for key, value in leaves({section: values[section]}):
            print(
                f"| `{key}` | {fmt_default(value)} "
                f"| {first_sentence(notes.get(key, ''))} |"
            )
        print()
    return 0


def main(argv: Sequence[str]) -> int:
    """Run the `check` or `table` command; returns the exit status."""
    command = argv[1] if len(argv) > 1 else ""
    if len(argv) < MIN_ARGC.get(command, sys.maxsize):
        print(__doc__, file=sys.stderr)
        return 2
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        print(f"{VALUES}: not a mapping", file=sys.stderr)
        return 2
    if command == "check":
        return check(values)
    return table(values, argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
