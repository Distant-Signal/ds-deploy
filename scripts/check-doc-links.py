#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the broken links) is stdout
"""Check every link to a Distant-Signal repository's file still resolves.

  scripts/check-doc-links.py [--checkouts DIR]

Since the repo split, the chart README and docs/ link to docs that live in
the other Distant-Signal repositories (the alert runbooks in docs/alerts.md
land in Alertmanager as `runbook_url`s). A doc that moves leaves those links
dead, so this finds every `https://github.com/Distant-Signal/<repo>/blob/
<ref>/<path>[#anchor]` (and `/tree/`) link in the tracked text files and
checks that the path exists at that ref and, for a Markdown file, that the
anchor names one of its headings (GitHub's heading slugs) or an explicit
`id`/`name` anchor.

Each linked repository is a blobless, shallow clone of the linked ref into
a temporary directory (the repositories are public, so no token), or, with
--checkouts DIR, the checkout at DIR/<repo> as it stands (for working on
several repositories at once). Exit 0 when every link resolves, 1 when one
does not, 2 when a repository could not be fetched.
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORG = "Distant-Signal"
LINK = re.compile(
    r"https://github\.com/"
    + ORG
    + r"/(?P<repo>[A-Za-z0-9._-]+)/(?:blob|tree)/(?P<ref>[A-Za-z0-9._-]+)/"
    r"(?P<path>[^\s)\"'<>`\]#?]+)(?:#(?P<anchor>[^\s)\"'<>`\]]+))?"
)
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".tpl", ".txt", ".json5", ".toml", ".py"}
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"""<a\s[^>]*(?:name|id)=["']([^"']+)["']""")
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
GIT = shutil.which("git") or "git"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git with ``args``, capturing its output as text."""
    return subprocess.run(  # noqa: S603  # git from PATH, this script's own arguments
        [GIT, *args],
        capture_output=True,
        text=True,
        check=check,
    )


@dataclass(frozen=True)
class Link:
    """One link to a file in a Distant-Signal repository."""

    source: str
    line: int
    repo: str
    ref: str
    path: str
    anchor: str | None


def find_links(files: Iterable[pathlib.Path], root: pathlib.Path) -> Iterator[Link]:
    """Yield every Distant-Signal file link in ``files``."""
    for file in files:
        if file.suffix not in TEXT_SUFFIXES:
            continue
        text = file.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in LINK.finditer(line):
                yield Link(
                    source=str(file.relative_to(root)),
                    line=number,
                    repo=match["repo"],
                    ref=match["ref"],
                    path=match["path"].rstrip(".,;:"),
                    anchor=match["anchor"],
                )


def slug(heading: str) -> str:
    """Return GitHub's anchor for a Markdown heading's text."""
    text = MD_LINK.sub(r"\1", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    """Return every anchor a rendered Markdown file has."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        found.update(HTML_ANCHOR.findall(line))
        heading = HEADING.match(line)
        if heading is None:
            continue
        base = slug(heading["text"])
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        found.add(base if seen == 0 else f"{base}-{seen}")
    return found


class Source:
    """Reads files from one repository at one ref."""

    def __init__(self, directory: pathlib.Path, ref: str | None) -> None:
        """Read from the git repository at ``directory``.

        ``ref`` None reads the working tree as it stands.
        """
        self.directory = directory
        self.ref = ref

    def kind(self, path: str) -> str | None:
        """Return "file", "dir" or None (missing) for ``path``."""
        if self.ref is None:
            target = self.directory / path
            if target.is_dir():
                return "dir"
            return "file" if target.is_file() else None
        result = git(
            "-C",
            str(self.directory),
            "cat-file",
            "-t",
            f"{self.ref}:{path}",
            check=False,
        )
        if result.returncode != 0:
            return None
        return "dir" if result.stdout.strip() == "tree" else "file"

    def read(self, path: str) -> str:
        """Return the text of ``path``."""
        if self.ref is None:
            return (self.directory / path).read_text(encoding="utf-8")
        return git("-C", str(self.directory), "show", f"{self.ref}:{path}").stdout


def clone(repo: str, ref: str, into: pathlib.Path) -> Source:
    """Clone ``repo`` at ``ref`` (blobless, shallow) and return its source."""
    git(
        "clone",
        "--quiet",
        "--depth=1",
        "--filter=blob:none",
        "--no-checkout",
        f"--branch={ref}",
        f"https://github.com/{ORG}/{repo}",
        str(into),
    )
    return Source(into, "HEAD")


def check(
    links: Iterable[Link], source_for: dict[tuple[str, str], Source]
) -> list[str]:
    """Return a message for every link that does not resolve."""
    problems: list[str] = []
    anchor_cache: dict[tuple[str, str, str], set[str]] = {}
    for link in links:
        where = f"{link.source}:{link.line}"
        target = f"{link.repo}@{link.ref}:{link.path}"
        source = source_for[link.repo, link.ref]
        kind = source.kind(link.path)
        if kind is None:
            problems.append(f"{where}: {target} does not exist")
            continue
        if link.anchor is None or kind != "file" or not link.path.endswith(".md"):
            continue
        key = (link.repo, link.ref, link.path)
        if key not in anchor_cache:
            anchor_cache[key] = anchors(source.read(link.path))
        if link.anchor not in anchor_cache[key]:
            problems.append(f"{where}: {target} has no anchor #{link.anchor}")
    return problems


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return the repository's tracked files."""
    names = git("-C", str(root), "ls-files", "-z").stdout.split("\0")
    return [root / name for name in names if name and (root / name).is_file()]


def main() -> int:
    """Run the check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--checkouts",
        type=pathlib.Path,
        metavar="DIR",
        help="read each linked repository from DIR/<repo> as it stands, not a clone",
    )
    args = parser.parse_args()
    links = list(find_links(tracked_files(ROOT), ROOT))
    with tempfile.TemporaryDirectory() as scratch:
        source_for: dict[tuple[str, str], Source] = {}
        for repo, ref in sorted({(link.repo, link.ref) for link in links}):
            if args.checkouts is not None:
                source_for[repo, ref] = Source(args.checkouts / repo, None)
                continue
            try:
                source_for[repo, ref] = clone(
                    repo, ref, pathlib.Path(scratch) / f"{repo}@{ref}"
                )
            except subprocess.CalledProcessError:
                print(f"could not clone {ORG}/{repo} at {ref}", file=sys.stderr)
                return 2
        problems = check(links, source_for)
    for problem in problems:
        print(problem)
    print(f"{len(links)} links, {len(problems)} broken", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
