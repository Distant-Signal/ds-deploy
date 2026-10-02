"""Tests for charts/distant-signal/files/schedule-sftp-entrypoint.sh.

The script writes SFTPGo's --loaddata-from JSON for the push account and
then execs `sftpgo serve`. Each test runs it under /bin/sh with a stub
`sftpgo` first on PATH, which saves its arguments and the loaddata file
(the real SFTPGo deletes it, --loaddata-clean), so the account policy can be
checked without SFTPGo.
"""

import json
import pathlib
import subprocess
import tempfile
import unittest
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "charts/distant-signal/files/schedule-sftp-entrypoint.sh"

STUB = """#!/bin/sh
printf '%s\\n' "$@" >"${STUB_OUT}/args"
cp "$3" "${STUB_OUT}/loaddata.json"
umask >"${STUB_OUT}/umask"
stat -c %a "$3" >"${STUB_OUT}/loaddata-mode"
"""

PASSWORD = "Abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105  # a test fixture, 32 characters like the chart's


class Run:
    """One run of the entrypoint: its exit status, stderr and output."""

    def __init__(self, env: dict[str, str]) -> None:
        """Run the script with `env` added to a minimal environment."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "sftpgo"
            stub.write_text(STUB)
            stub.chmod(0o755)
            out = root / "out"
            out.mkdir()
            boot = root / "boot"
            boot.mkdir()
            full_env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "STUB_OUT": str(out),
                "SCHEDULE_SFTP_USERNAME": "dtd-push",
                "SCHEDULE_SFTP_HOME_DIR": "/data/schedule-feed/incoming",
                "SCHEDULE_SFTP_LOADDATA_DIR": str(boot),
                **env,
            }
            result = subprocess.run(  # noqa: S603  # fixed argv, the repo's own script
                ["/bin/sh", str(ENTRYPOINT)],
                env=full_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.returncode = result.returncode
            self.stdout = result.stdout
            self.stderr = result.stderr
            args = out / "args"
            self.args = args.read_text().splitlines() if args.exists() else []
            loaddata = out / "loaddata.json"
            umask = out / "umask"
            self.umask = umask.read_text().strip() if umask.exists() else None
            mode = out / "loaddata-mode"
            self.loaddata_mode = mode.read_text().strip() if mode.exists() else None
            self.loaddata: dict[str, Any] | None = (
                json.loads(loaddata.read_text()) if loaddata.exists() else None
            )

    def user(self) -> dict[str, Any]:
        """Return the one user in the loaddata file."""
        if self.loaddata is None:
            msg = f"no loaddata written; stderr: {self.stderr}"
            raise AssertionError(msg)
        users: list[dict[str, Any]] = self.loaddata["users"]
        if len(users) != 1:
            msg = f"expected one user, got {len(users)}"
            raise AssertionError(msg)
        return users[0]


class FileModeTest(unittest.TestCase):
    """Uploads must be readable by schedule-ingest; the credential must not."""

    def test_sftpgo_runs_with_a_group_readable_umask(self) -> None:
        """SFTPGo inherits umask 027, so new uploads are 0640 for ingest's group."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.umask, "0027")

    def test_the_loaddata_file_stays_owner_only(self) -> None:
        """The file holding the push credential is written 0600."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.loaddata_mode, "600")


class AccountPolicyTest(unittest.TestCase):
    """The push account gets least privilege."""

    def test_defaults_are_least_privilege(self) -> None:
        """Upload, overwrite and list only; two sessions; no size cap."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertEqual(run.returncode, 0, run.stderr)
        user = run.user()
        self.assertEqual(user["permissions"], {"/": ["upload", "overwrite", "list"]})
        self.assertEqual(user["max_sessions"], 2)
        self.assertEqual(user["filters"]["max_upload_file_size"], 0)
        self.assertEqual(user["filters"]["denied_protocols"], ["FTP", "DAV", "HTTP"])
        self.assertEqual(user["password"], PASSWORD)

    def test_password_mode_keeps_keyboard_interactive(self) -> None:
        """DTD's JSch client logs in with keyboard-interactive."""
        user = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD}).user()
        denied = user["filters"]["denied_login_methods"]
        self.assertNotIn("keyboard-interactive", denied)
        self.assertNotIn("password", denied)
        self.assertIn("publickey", denied)

    def test_public_key_mode_denies_passwords(self) -> None:
        """With a public key, no password-based method is allowed."""
        user = Run({"SCHEDULE_SFTP_PUBLIC_KEY": "ssh-ed25519 AAAA test\n"}).user()
        denied = user["filters"]["denied_login_methods"]
        for method in ("password", "password-over-SSH", "keyboard-interactive"):
            self.assertIn(method, denied)
        self.assertNotIn("publickey", denied)
        self.assertEqual(user["public_keys"], ["ssh-ed25519 AAAA test"])
        self.assertNotIn("password", user)

    def test_configured_policy_is_written(self) -> None:
        """The chart's values reach the account."""
        user = Run(
            {
                "SCHEDULE_SFTP_PASSWORD": PASSWORD,
                "SCHEDULE_SFTP_PERMISSIONS": "upload,overwrite",
                "SCHEDULE_SFTP_MAX_SESSIONS": "1",
                "SCHEDULE_SFTP_MAX_UPLOAD_FILE_SIZE": "536870912",
            }
        ).user()
        self.assertEqual(user["permissions"], {"/": ["upload", "overwrite"]})
        self.assertEqual(user["max_sessions"], 1)
        self.assertEqual(user["filters"]["max_upload_file_size"], 536870912)

    def test_bad_permissions_refuse_to_start(self) -> None:
        """'*', unknown names, JSON injection and an empty list all fail."""
        for permissions in ("*", "upload,*", "admin", 'upload"],"x":["', ",", " "):
            with self.subTest(permissions=permissions):
                run = Run(
                    {
                        "SCHEDULE_SFTP_PASSWORD": PASSWORD,
                        "SCHEDULE_SFTP_PERMISSIONS": permissions,
                    }
                )
                self.assertEqual(run.returncode, 1)
                self.assertIsNone(run.loaddata)
                self.assertIn("SCHEDULE_SFTP_PERMISSIONS", run.stderr)

    def test_bad_limits_refuse_to_start(self) -> None:
        """Limits must be plain non-negative integers."""
        for name in (
            "SCHEDULE_SFTP_MAX_SESSIONS",
            "SCHEDULE_SFTP_MAX_UPLOAD_FILE_SIZE",
        ):
            for value in ("-1", "2x", '1, "x": 1'):
                with self.subTest(name=name, value=value):
                    run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD, name: value})
                    self.assertEqual(run.returncode, 1)
                    self.assertIn(name, run.stderr)

    def test_password_is_json_escaped(self) -> None:
        """A quote or backslash can't break out of the password string."""
        password = 'abc"def\\ghi", "permissions": {"/": ["*"]}, "x": "yzABCDEFGH'  # noqa: S105  # a test fixture
        user = Run({"SCHEDULE_SFTP_PASSWORD": password}).user()
        self.assertEqual(user["password"], password)
        self.assertEqual(user["permissions"], {"/": ["upload", "overwrite", "list"]})

    def test_sftpgo_updates_the_account_and_deletes_the_file(self) -> None:
        """Mode 0 (update an existing account) and --loaddata-clean."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertEqual(run.args[0], "serve")
        self.assertEqual(run.args[1], "--loaddata-from")
        self.assertEqual(run.args[3:], ["--loaddata-mode", "0", "--loaddata-clean"])

    def test_no_safelist_writes_no_ip_lists(self) -> None:
        """The default loads no IP list entries."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertIsNotNone(run.loaddata)
        if run.loaddata is not None:
            self.assertEqual(run.loaddata["ip_lists"], [])

    def test_safelist_becomes_defender_and_rate_limiter_allow_entries(self) -> None:
        """Each entry is safe from the defender (type 2) and limiter (type 3)."""
        run = Run(
            {
                "SCHEDULE_SFTP_PASSWORD": PASSWORD,
                "SCHEDULE_SFTP_SAFELIST": "192.0.2.0/24 2001:db8::1",
            }
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        if run.loaddata is None:
            self.fail(run.stderr)
        entries = [
            (e["ipornet"], e["type"], e["mode"], e["protocols"])
            for e in run.loaddata["ip_lists"]
        ]
        self.assertEqual(
            entries,
            [
                ("192.0.2.0/24", 2, 1, 1),
                ("192.0.2.0/24", 3, 1, 1),
                ("2001:db8::1", 2, 1, 1),
                ("2001:db8::1", 3, 1, 1),
            ],
        )

    def test_bad_safelist_entries_refuse_to_start(self) -> None:
        """Only IP/CIDR characters; a glob or JSON can't get through."""
        for safelist in ('1.2.3.4"}, {"x', "*", "10.0.0.0/8,10.1.0.0/16"):
            with self.subTest(safelist=safelist):
                run = Run(
                    {
                        "SCHEDULE_SFTP_PASSWORD": PASSWORD,
                        "SCHEDULE_SFTP_SAFELIST": safelist,
                    }
                )
                self.assertEqual(run.returncode, 1)
                self.assertIsNone(run.loaddata)

    def test_short_password_refuses_to_start_without_printing_it(self) -> None:
        """Under 24 characters fails by default; only the length is shown."""
        short = "Sh0rtButNotTiny-123"  # a test fixture, 19 characters
        run = Run({"SCHEDULE_SFTP_PASSWORD": short})
        self.assertEqual(run.returncode, 1)
        self.assertIsNone(run.loaddata)
        self.assertIn("19 characters", run.stderr)
        self.assertNotIn(short, run.stdout + run.stderr)

    def test_short_password_only_warns_when_not_enforced(self) -> None:
        """passwordPolicy.enforce=false starts, with a warning."""
        short = "Sh0rtButNotTiny-123"  # a test fixture
        run = Run(
            {
                "SCHEDULE_SFTP_PASSWORD": short,
                "SCHEDULE_SFTP_PASSWORD_POLICY": "warn",
            }
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("WARNING", run.stderr)
        self.assertNotIn(short, run.stdout + run.stderr)
        self.assertEqual(run.user()["password"], short)

    def test_password_length_boundary(self) -> None:
        """Exactly the minimum passes; one less fails; the minimum is settable."""
        self.assertEqual(Run({"SCHEDULE_SFTP_PASSWORD": "x" * 24}).returncode, 0)
        self.assertEqual(Run({"SCHEDULE_SFTP_PASSWORD": "x" * 23}).returncode, 1)
        run = Run(
            {
                "SCHEDULE_SFTP_PASSWORD": PASSWORD,
                "SCHEDULE_SFTP_PASSWORD_MIN_LENGTH": "40",
            }
        )
        self.assertEqual(run.returncode, 1)

    def test_bad_password_policy_settings_refuse_to_start(self) -> None:
        """An unknown policy or a non-numeric minimum fails closed."""
        for env in (
            {"SCHEDULE_SFTP_PASSWORD_POLICY": "ignore"},
            {"SCHEDULE_SFTP_PASSWORD_MIN_LENGTH": "24x"},
        ):
            with self.subTest(env=env):
                run = Run({"SCHEDULE_SFTP_PASSWORD": "short", **env})
                self.assertEqual(run.returncode, 1)

    def test_public_key_mode_skips_the_password_check(self) -> None:
        """No password, no length check."""
        run = Run({"SCHEDULE_SFTP_PUBLIC_KEY": "ssh-ed25519 AAAA test"})
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_nothing_secret_reaches_the_output(self) -> None:
        """The script never prints the credential."""
        run = Run({"SCHEDULE_SFTP_PASSWORD": PASSWORD})
        self.assertNotIn(PASSWORD, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
