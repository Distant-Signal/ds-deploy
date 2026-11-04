"""Tests for scripts/check-doc-links.py, on checkouts in a temporary directory.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from types import ModuleType

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-doc-links.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_links", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


links = _load()

DOC = """# Alerts

## `DsApiDown` (api)

<a id="custom-anchor"></a>

## Retention: deleted after 24 hours

```sh
# not a heading
```

## [Linked](https://example.com) heading

## Retention: deleted after 24 hours
"""


class AnchorTests(unittest.TestCase):
    """Anchors in rendered Markdown."""

    def test_slugs_match_github(self) -> None:
        """Headings get GitHub's slugs, numbered when repeated; fences skipped."""
        self.assertEqual(
            links.anchors(DOC),
            {
                "alerts",
                "dsapidown-api",
                "custom-anchor",
                "retention-deleted-after-24-hours",
                "retention-deleted-after-24-hours-1",
                "linked-heading",
            },
        )


class CheckTests(unittest.TestCase):
    """Links checked against a checkout."""

    def test_reports_missing_files_and_anchors(self) -> None:
        """A moved file and an unknown anchor are reported; the rest pass."""
        with tempfile.TemporaryDirectory() as scratch:
            root = pathlib.Path(scratch)
            (root / "ds-api" / "docs").mkdir(parents=True)
            (root / "ds-api" / "docs" / "alerts.md").write_text(DOC, encoding="utf-8")
            (root / "here").mkdir()
            source = root / "here" / "README.md"
            base = "https://github.com/Distant-Signal"
            source.write_text(
                f"[ok]({base}/ds-api/blob/main/docs/alerts.md#dsapidown-api).\n"
                f"[dir]({base}/ds-api/tree/main/docs)\n"
                f"[gone]({base}/ds-api/blob/main/docs/moved.md)\n"
                f"[bad anchor]({base}/ds-api/blob/main/docs/alerts.md#nope)\n",
                encoding="utf-8",
            )
            found = list(links.find_links([source], root / "here"))
            self.assertEqual(len(found), 4)
            self.assertEqual(found[0].path, "docs/alerts.md")
            sources = {("ds-api", "main"): links.Source(root / "ds-api", None)}
            self.assertEqual(
                links.check(found, sources),
                [
                    "README.md:3: ds-api@main:docs/moved.md does not exist",
                    "README.md:4: ds-api@main:docs/alerts.md has no anchor #nope",
                ],
            )


if __name__ == "__main__":
    unittest.main()
