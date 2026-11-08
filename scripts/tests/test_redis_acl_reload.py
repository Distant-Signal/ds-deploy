"""Tests for charts/distant-signal/files/redis-acl-reload.sh.

The script renders the Redis users file from the ACL template and the
password files (`render`) and, with `redis.acl.reload.mode: inPlace`,
reloads it into the running Redis (`watch`). Each test runs it under
/bin/sh in a temporary pod layout, with a stub `redis-cli` first on PATH
that records its calls and answers ACL LOAD as a real server would for the
password it holds (STUB_ADMIN) and a reply chosen by the test (STUB_REPLY).
"""

import os
import pathlib
import subprocess
import tempfile
import unittest
from typing import override

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "charts/distant-signal/files/redis-acl-reload.sh"

STUB = """#!/bin/sh
printf '%s|%s\\n' "${REDISCLI_AUTH}" "$*" >>"${STUB_OUT}/calls"
if [ "${REDISCLI_AUTH}" != "$(cat "${STUB_OUT}/admin")" ]; then
    echo "AUTH failed: WRONGPASS invalid username-password pair or user is disabled."
    exit 0
fi
cat "${STUB_OUT}/reply"
"""

TEMPLATE = (
    "user default reset on >${REDIS_ACL_PASSWORD_DEFAULT} ~* &* +@all\n"
    "user relay reset on >${REDIS_ACL_PASSWORD_RELAY}"
    " >${REDIS_ACL_PASSWORD_RELAY_PREVIOUS} ~movement-events +xadd\n"
    "user ds-admin reset on >${REDIS_ACL_PASSWORD_DS_ADMIN}"
    " >${REDIS_ACL_PASSWORD_DS_ADMIN_PREVIOUS} ~* &* +@all\n"
)


class Pod:
    """A temporary Redis pod layout: template, password map and Secret."""

    def __init__(self, root: pathlib.Path) -> None:
        """Lay out the files under `root`."""
        self.root = root
        self.template = root / "tpl" / "users.acl.tpl"
        self.passwords = root / "tpl" / "passwords"
        self.secret = root / "secret"
        self.live = root / "acl" / "users.acl"
        self.stub = root / "stub"
        for d in (self.template.parent, self.secret, self.live.parent, self.stub):
            d.mkdir()
        (root / "bin").mkdir()
        cli = root / "bin" / "redis-cli"
        cli.write_text(STUB)
        cli.chmod(0o755)
        self.template.write_text(TEMPLATE)
        lines = [f"REDIS_ACL_PASSWORD_DEFAULT {self.secret}/default-password"]
        for user in ("relay", "ds-admin"):
            var = "REDIS_ACL_PASSWORD_" + user.upper().replace("-", "_")
            lines += [
                f"{var} {self.secret}/{user}-password",
                f"{var}_PREVIOUS {self.secret}/{user}-password-previous",
            ]
        self.passwords.write_text("\n".join(lines) + "\n")
        self.set_secret("default-password", "d1")
        self.set_secret("relay-password", "r1")
        self.set_secret("ds-admin-password", "a1")
        self.server_admin("a1")
        self.reply("OK")

    def set_secret(self, key: str, value: str | None) -> None:
        """Write (or, for None, delete) a Secret key's file."""
        path = self.secret / key
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def server_admin(self, password: str) -> None:
        """Make the stub Redis accept ds-admin with `password`."""
        (self.stub / "admin").write_text(password)

    def reply(self, text: str) -> None:
        """Make the stub Redis answer ACL LOAD with `text`."""
        (self.stub / "reply").write_text(text + "\n")

    def calls(self) -> list[str]:
        """Return the stub's calls, as `password|arguments`."""
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def run(self, mode: str) -> subprocess.CompletedProcess[str]:
        """Run the script's `mode` once."""
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "ACL_TEMPLATE": str(self.template),
            "ACL_PASSWORDS": str(self.passwords),
            "ACL_FILE": str(self.live),
            "ACL_ADMIN_PASSWORD_FILE": str(self.secret / "ds-admin-password"),
            "ACL_ADMIN_PREVIOUS_FILE": str(self.secret / "ds-admin-password-previous"),
            "RELOAD_ONCE": "1",
            "STUB_OUT": str(self.stub),
        }
        return subprocess.run(  # noqa: S603  # /bin/sh on the chart's own script
            ["/bin/sh", str(SCRIPT), mode],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def render_live(self) -> None:
        """Render the live file, as the initContainer does at pod start."""
        result = self.run("render")
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        self.live.write_text(result.stdout)


class RenderTest(unittest.TestCase):
    """`render`: the initContainer's substitution and checks, from files."""

    @override
    def setUp(self) -> None:
        """Lay out a pod."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pod = Pod(pathlib.Path(tmp.name))

    def test_fills_passwords_and_drops_missing_previous(self) -> None:
        """Each placeholder becomes its file's value; no -previous, none."""
        result = self.pod.run("render")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "user default reset on >d1 ~* &* +@all\n"
            "user relay reset on >r1 ~movement-events +xadd\n"
            "user ds-admin reset on >a1 ~* &* +@all\n",
        )

    def test_keeps_a_previous_password(self) -> None:
        """A rotation's -previous key is rendered after the current one."""
        self.pod.set_secret("relay-password-previous", "r0")
        result = self.pod.run("render")
        self.assertIn("user relay reset on >r1 >r0 ~movement-events", result.stdout)

    def test_missing_password_fails(self) -> None:
        """A missing (not -previous) key fails, naming the variable."""
        self.pod.set_secret("relay-password", None)
        result = self.pod.run("render")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_ACL_PASSWORD_RELAY is empty", result.stderr)

    def test_whitespace_fails(self) -> None:
        """A trailing newline or a space in a password fails."""
        for value in ("r1\n", "r 1"):
            with self.subTest(value=value):
                self.pod.set_secret("relay-password", value)
                result = self.pod.run("render")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("contains whitespace", result.stderr)

    def test_placeholder_without_a_file_fails(self) -> None:
        """A template variable the password map does not name fails."""
        self.pod.template.write_text(TEMPLATE + "user x reset on >${REDIS_ACL_X}\n")
        result = self.pod.run("render")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REDIS_ACL_X is empty", result.stderr)

    def test_invalid_map_variable_fails(self) -> None:
        """A password map line whose variable is not [A-Z0-9_] fails."""
        with self.pod.passwords.open("a") as f:
            f.write("BAD-NAME /dev/null\n")
        result = self.pod.run("render")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid variable: BAD-NAME", result.stderr)


class WatchTest(unittest.TestCase):
    """`watch`: one round of render, compare, swap and ACL LOAD."""

    @override
    def setUp(self) -> None:
        """Lay out a pod whose live file is the current render."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pod = Pod(pathlib.Path(tmp.name))
        self.pod.render_live()

    def test_unchanged_users_send_nothing(self) -> None:
        """No change: no ACL LOAD."""
        result = self.pod.run("watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pod.calls(), [])

    def test_changed_password_reloads(self) -> None:
        """A rotated password is swapped in and loaded as ds-admin."""
        self.pod.set_secret("relay-password-previous", "r1")
        self.pod.set_secret("relay-password", "r2")
        result = self.pod.run("watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(">r2 >r1", self.pod.live.read_text())
        self.assertEqual(
            self.pod.calls(),
            ["a1|-h 127.0.0.1 -p 6379 --user ds-admin ACL LOAD"],
        )
        self.assertIn("reloaded the ACL users (3 users)", result.stderr)
        self.assertEqual(
            sorted(p.name for p in self.pod.live.parent.iterdir()), ["users.acl"]
        )

    def test_changed_template_reloads(self) -> None:
        """A new users file (a ds-core change) is loaded."""
        self.pod.template.write_text(TEMPLATE.replace("+xadd", "+xadd +xtrim"))
        result = self.pod.run("watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+xadd +xtrim", self.pod.live.read_text())
        self.assertEqual(len(self.pod.calls()), 1)

    def test_refused_load_restores_the_previous_file(self) -> None:
        """Redis refusing the file leaves the live file as it was."""
        before = self.pod.live.read_text()
        self.pod.template.write_text(TEMPLATE.replace("+xadd", "+nosuchcommand"))
        self.pod.reply("ERR Error in applying operation '+nosuchcommand'")
        result = self.pod.run("watch")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pod.live.read_text(), before)
        self.assertIn("ACL LOAD failed: ERR", result.stderr)
        self.assertIn("kept the previous users", result.stderr)
        self.assertEqual(
            sorted(p.name for p in self.pod.live.parent.iterdir()), ["users.acl"]
        )

    def test_unrenderable_users_keep_the_live_file(self) -> None:
        """A missing Secret key: no swap, no ACL LOAD, a logged reason."""
        before = self.pod.live.read_text()
        self.pod.set_secret("relay-password", None)
        result = self.pod.run("watch")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pod.live.read_text(), before)
        self.assertEqual(self.pod.calls(), [])
        self.assertIn("REDIS_ACL_PASSWORD_RELAY is empty", result.stderr)

    def test_admin_rotation_without_previous(self) -> None:
        """ds-admin's new password reloads with the one Redis still has."""
        self.pod.set_secret("ds-admin-password", "a2")
        result = self.pod.run("watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("user ds-admin reset on >a2 ", self.pod.live.read_text())
        # The password in the file Redis runs (a1) goes first.
        self.assertEqual(
            [c.split("|")[0] for c in self.pod.calls()],
            ["a1"],
        )

    def test_admin_password_falls_back_to_the_secret(self) -> None:
        """If Redis refuses the running file's password, the Secret's is next."""
        self.pod.set_secret("ds-admin-password", "a2")
        self.pod.server_admin("a2")
        self.pod.set_secret("relay-password", "r2")
        result = self.pod.run("watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [c.split("|")[0] for c in self.pod.calls()],
            ["a1", "a2"],
        )

    def test_no_working_admin_password(self) -> None:
        """Every candidate refused: the previous file is kept."""
        before = self.pod.live.read_text()
        self.pod.server_admin("elsewhere")
        self.pod.set_secret("relay-password", "r2")
        result = self.pod.run("watch")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pod.live.read_text(), before)
        self.assertIn("WRONGPASS", result.stderr)


if __name__ == "__main__":
    unittest.main()
