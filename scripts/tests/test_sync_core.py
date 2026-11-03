"""Tests for scripts/sync-core.py, with a stub cosign and local assets."""

import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import override

SCRIPT = Path(__file__).resolve().parents[1] / "sync-core.py"


def load() -> ModuleType:
    """Import scripts/sync-core.py."""
    spec = importlib.util.spec_from_file_location("sync_core", SCRIPT)
    if spec is None or spec.loader is None:
        msg = f"cannot load {SCRIPT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_core"] = module
    spec.loader.exec_module(module)
    return module


sc = load()

# A cosign that accepts every bundle whose first line is "ok", and records
# the identity it was asked for.
STUB = """#!/bin/sh
while [ $# -gt 0 ]; do
    case $1 in
        --bundle) bundle=$2; shift ;;
        --certificate-identity) echo "$2" >"$(dirname "$0")/identity" ;;
    esac
    shift
done
[ "$(head -n 1 "${bundle}")" = ok ]
"""


class SyncCoreTest(unittest.TestCase):
    """Vendoring from a release's assets."""

    @override
    def setUp(self) -> None:
        """Make a repository root, an assets directory and a stub cosign."""
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        (self.root / "charts/distant-signal/files").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / "core-version").write_text("v0.1.0\n", encoding="utf-8")
        self.assets = self.tmp / "assets"
        self.assets.mkdir()
        for name, _ in sc.ASSETS:
            (self.assets / name).write_text(f"{name} at v0.1.0\n", encoding="utf-8")
            (self.assets / f"{name}.sigstore.json").write_text("ok\n", encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cosign = self.bin / "cosign"
        self.cosign.write_text(STUB, encoding="utf-8")
        self.cosign.chmod(0o755)

    def run_main(self, *args: str) -> tuple[int, str]:
        """Run main against the temporary root; return its code and output."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = sc.main(
                [
                    "--root",
                    str(self.root),
                    "--assets-dir",
                    str(self.assets),
                    "--cosign",
                    str(self.cosign),
                    *args,
                ]
            )
        return code, out.getvalue()

    def test_writes_then_checks_clean(self) -> None:
        """A sync writes every copy, after which --check passes."""
        self.assertEqual(self.run_main()[0], 0)
        for name, copy in sc.ASSETS:
            self.assertEqual(
                (self.root / copy).read_text(encoding="utf-8"), f"{name} at v0.1.0\n"
            )
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_check_names_stale_copies(self) -> None:
        """--check fails, names the stale copy and writes nothing."""
        self.run_main()
        stale = self.root / "charts/distant-signal/files/postgres-grants.sql"
        stale.write_text("edited\n", encoding="utf-8")
        code, out = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("charts/distant-signal/files/postgres-grants.sql", out)
        self.assertEqual(stale.read_text(encoding="utf-8"), "edited\n")

    def test_unverified_asset_writes_nothing(self) -> None:
        """A bundle that doesn't verify is exit 2, and no copy is written."""
        (self.assets / "redis-users.acl.tpl.sigstore.json").write_text(
            "forged\n", encoding="utf-8"
        )
        code, out = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("redis-users.acl.tpl", out)
        self.assertFalse(
            (self.root / "charts/distant-signal/files/db-grants.yaml").exists()
        )

    def test_identity_is_the_release_workflow_at_the_tag(self) -> None:
        """Assets verify against ds-core's release.yml at core-version's tag."""
        self.run_main("--check")
        self.assertEqual(
            (self.bin / "identity").read_text(encoding="utf-8").strip(),
            "https://github.com/Distant-Signal/ds-core/.github/workflows/release.yml"
            "@refs/tags/v0.1.0",
        )

    def test_missing_asset(self) -> None:
        """A missing asset is exit 2."""
        (self.assets / "postgres-roles.sql").unlink()
        self.assertEqual(self.run_main()[0], 2)

    def test_pins_must_agree(self) -> None:
        """Cargo.toml's ds-core tags and migrate.image.tag are core-version."""
        (self.root / "Cargo.toml").write_text(
            "[workspace.dependencies]\n"
            f'common = {{ git = "{sc.CORE_GIT}", tag = "v0.1.0" }}\n'
            f'ds-store = {{ git = "{sc.CORE_GIT}", tag = "v0.0.9" }}\n'
            'serde = "1"\n',
            encoding="utf-8",
        )
        values = self.root / "charts/distant-signal/values.yaml"
        values.write_text(
            "api:\n  image:\n    tag: v9\nmigrate:\n  # The migrator.\n  image:\n"
            "    repository: ghcr.io/distant-signal/images/ds-migrate\n"
            "    tag: v0.1.0\n",
            encoding="utf-8",
        )
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ds-store is on 'v0.0.9'", out)
        self.assertNotIn("common is on", out)
        self.assertNotIn("migrate.image.tag", out)
        values.write_text(
            values.read_text(encoding="utf-8").replace("tag: v0.1.0", 'tag: "v0.2.0"'),
            encoding="utf-8",
        )
        self.assertIn("migrate.image.tag is 'v0.2.0'", self.run_main("--check")[1])

    def test_bad_version(self) -> None:
        """core-version must be one vX.Y.Z."""
        (self.root / "core-version").write_text("main\n", encoding="utf-8")
        self.assertEqual(self.run_main()[0], 2)


if __name__ == "__main__":
    unittest.main()
