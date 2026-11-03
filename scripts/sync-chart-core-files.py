#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the stale files) is stdout
"""Copy the core access files into the chart, or check the copies.

  uv run scripts/sync-chart-core-files.py [--check]

The chart reads files that `ds-core` owns (repo split spec, section 5.1):
the Postgres grants and roles and the Redis ACL template. Their sources are

  crates/ds-store/db-grants.yaml   (ds-store's build.rs bakes it into the
                                    schema gate; gen-db-grants.py renders it)
  access/postgres-grants.sql       (gen-db-grants.py render writes it)
  access/postgres-roles.sql        (the roles setup Job's SQL)
  access/postgres-roles-setup.sh   (the roles setup Job's entrypoint)
  access/redis-users.acl.tpl       (render-redis-acl.py renders it;
                                    crates/common/tests/redis_acl.rs checks it)

and charts/distant-signal/files/ holds byte-for-byte copies, which Helm's
`.Files.Get` can reach (it can't read outside the chart). Edit the source,
then run this script and commit both. With --check it writes nothing and
exits 1, naming each stale copy, when a copy differs from its source (CI's
scripts-lint job).

After the repo split, ds-deploy's sync-core.py replaces this: it writes the
same copies from a ds-core release's signed assets.

Stdlib only.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART_FILES = Path("charts/distant-signal/files")

# (source, chart copy), relative to the repository root.
COPIES = (
    (Path("crates/ds-store/db-grants.yaml"), CHART_FILES / "db-grants.yaml"),
    (Path("access/postgres-grants.sql"), CHART_FILES / "postgres-grants.sql"),
    (Path("access/postgres-roles.sql"), CHART_FILES / "postgres-roles.sql"),
    (Path("access/postgres-roles-setup.sh"), CHART_FILES / "postgres-roles-setup.sh"),
    (Path("access/redis-users.acl.tpl"), CHART_FILES / "redis-users.acl.tpl"),
)


def sync(root: Path, *, check: bool) -> list[str]:
    """Return the copies under `root` that differ from their sources.

    Unless `check`, also rewrite each of them from its source.
    """
    stale: list[str] = []
    for source, copy in COPIES:
        wanted = (root / source).read_bytes()
        target = root / copy
        if target.exists() and target.read_bytes() == wanted:
            continue
        stale.append(f"{copy} differs from its source {source}")
        if not check:
            target.write_bytes(wanted)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    """Copy (or, with --check, verify) the chart's core access files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any chart copy differs from its source",
    )
    args = parser.parse_args(argv)
    stale = sync(ROOT, check=args.check)
    if args.check and stale:
        for line in stale:
            print(line)
        print("edit the source, then run: uv run scripts/sync-chart-core-files.py")
        return 1
    for line in stale:
        print(f"rewrote: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
