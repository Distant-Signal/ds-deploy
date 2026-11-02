"""Tests for scripts/sync-chart-core-files.py.

uv run python -m unittest discover -s scripts/tests
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "sync-chart-core-files.py"


def load_script() -> ModuleType:
    """Import the hyphenated script as a module."""
    spec = importlib.util.spec_from_file_location("sync_chart_core_files", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_chart_core_files"] = module
    spec.loader.exec_module(module)
    return module


sync = load_script()


class SyncTests(unittest.TestCase):
    """Each chart copy is its source, byte for byte."""

    def make_root(self, base: Path) -> None:
        """Write every source and an up-to-date copy under `base`."""
        for source, copy in sync.COPIES:
            for path in (base / source, base / copy):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"# " + source.name.encode() + b"\nline\n")

    def test_the_repository_copies_are_in_sync(self) -> None:
        """The checked-in chart copies equal their sources."""
        self.assertEqual(sync.sync(sync.ROOT, check=True), [])

    def test_in_sync_is_quiet(self) -> None:
        """Nothing to report or write when every copy matches."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            self.assertEqual(sync.sync(root, check=True), [])

    def test_check_reports_a_drifted_or_missing_copy_and_writes_nothing(self) -> None:
        """--check names each stale copy and leaves it as it was."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            (drifted_source, drifted), (_, missing) = sync.COPIES
            # A byte of whitespace is drift too: the copy must be exact.
            (root / drifted).write_bytes((root / drifted_source).read_bytes() + b" ")
            (root / missing).unlink()
            stale = sync.sync(root, check=True)
            self.assertEqual(len(stale), 2)
            self.assertIn(str(drifted), stale[0])
            self.assertIn(str(missing), stale[1])
            self.assertFalse((root / missing).exists())

    def test_sync_rewrites_stale_copies(self) -> None:
        """Without --check each stale copy becomes its source."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            source, copy = sync.COPIES[0]
            (root / source).write_bytes(b"changed\n")
            self.assertEqual(len(sync.sync(root, check=False)), 1)
            self.assertEqual((root / copy).read_bytes(), b"changed\n")
            self.assertEqual(sync.sync(root, check=True), [])


if __name__ == "__main__":
    unittest.main()
