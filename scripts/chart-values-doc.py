#!/usr/bin/env python3
"""Keep charts/distant-signal/README.md's "Values reference" tables in step
with charts/distant-signal/values.yaml.

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
A `<name>` segment matches any one segment (`pollers.<name>.enabled`).
Needs PyYAML.
"""

import pathlib
import re
import sys

import yaml

CHART = pathlib.Path(__file__).resolve().parent.parent / "charts" / "distant-signal"
VALUES = CHART / "values.yaml"
README = CHART / "README.md"

# Maps documented as one row (their children are free-form).
OPAQUE = {
    "resources", "nodeSelector", "tolerations", "affinity", "podAnnotations",
    "podSecurityContext", "securityContext", "extraEnv", "annotations",
    "labels", "config", "extraRules", "hosts", "tls",
}

KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):(\s|$)")


def leaves(node, prefix=""):
    """Yield (dotted key, default) for every documentable key."""
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value and key not in OPAQUE:
            yield from leaves(value, path + ".")
        else:
            yield path, value


def comments():
    """Map dotted key -> the comment block directly above it in values.yaml."""
    out, stack, pending = {}, [], []
    for line in VALUES.read_text().splitlines():
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


def readme_keys(values):
    """First-cell backticked keys of README table rows that name a value."""
    keys = set()
    for line in README.read_text().splitlines():
        match = re.match(r"^\|\s*`([A-Za-z0-9_.<>-]+)`\s*\|", line)
        if match and match.group(1).split(".")[0] in values:
            keys.add(match.group(1))
    return keys


def pattern(key):
    parts = [r"[^.]+" if re.fullmatch(r"<[^>]+>", p) else re.escape(p) for p in key.split(".")]
    return re.compile(r"\.".join(parts))


def documented(key, patterns):
    parts = key.split(".")
    ancestors = [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return any(p.fullmatch(a) for p in patterns for a in ancestors)


def exists(key, values):
    def walk(node, parts):
        if not parts:
            return True
        if not isinstance(node, dict):
            return False
        if re.fullmatch(r"<[^>]+>", parts[0]):
            return any(walk(v, parts[1:]) for v in node.values())
        return parts[0] in node and walk(node[parts[0]], parts[1:])
    return walk(values, key.split("."))


def fmt_default(value):
    if value is None:
        return "`null`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, str):
        return f'`"{value}"`' if value == "" else f"`{value}`"
    if isinstance(value, dict) and set(value) <= {"requests", "limits"} and value:
        req, lim = value.get("requests", {}), value.get("limits", {})
        parts = []
        if req:
            parts.append("requests " + "/".join(f"`{req[k]}`" for k in ("cpu", "memory") if k in req))
        if lim:
            parts.append("limit " + "/".join(f"`{lim[k]}`" for k in ("cpu", "memory") if k in lim))
        return ", ".join(parts)
    if isinstance(value, (dict, list)) and not value:
        return f"`{'{}' if isinstance(value, dict) else '[]'}`"
    if isinstance(value, (dict, list)):
        return "see values.yaml"
    return f"`{value}`"


def first_sentence(text):
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text).replace("|", "\\|")


def main(argv):
    values = yaml.safe_load(VALUES.read_text())
    if len(argv) >= 2 and argv[1] == "check":
        keys = readme_keys(values)
        patterns = [pattern(k) for k in keys]
        missing = [k for k, _ in leaves(values) if not documented(k, patterns)]
        stale = sorted(k for k in keys if not exists(k, values))
        for k in missing:
            print(f"undocumented in README.md: {k}")
        for k in stale:
            print(f"README.md row for a key values.yaml does not have: {k}")
        if missing or stale:
            print(f"\n{len(missing)} undocumented, {len(stale)} stale. "
                  "Add or fix the rows in charts/distant-signal/README.md "
                  "(`scripts/chart-values-doc.py table <section>` drafts them).",
                  file=sys.stderr)
            return 1
        return 0
    if len(argv) >= 3 and argv[1] == "table":
        notes = comments()
        for section in argv[2:]:
            print(f"### {section}\n\n| Key | Default | Description |\n|---|---|---|")
            for key, value in leaves({section: values[section]}):
                print(f"| `{key}` | {fmt_default(value)} | {first_sentence(notes.get(key, ''))} |")
            print()
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
