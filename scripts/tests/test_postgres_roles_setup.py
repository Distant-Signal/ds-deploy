"""Tests for charts/distant-signal/files/postgres-roles-setup.sh.

The role setup Job's entrypoint: `wait` polls pg_isready, `run` runs one
setup script with psql and retries it after a lock timeout and the like.
Each test runs it under /bin/sh with stub `psql` and `pg_isready` first on
PATH. The psql stub plays the outcomes listed in STUB_OUT/plan, one per
try, and answers the blockers query (`--expanded`) with a fixed row, so the
retry policy, the exit codes and the redaction are checked without
Postgres. scripts/test-postgres-roles.py --check-setup-job runs it against
a real one.
"""

import pathlib
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "charts/distant-signal/files/postgres-roles-setup.sh"

PSQL_STUB = """#!/bin/sh
for arg; do
    case ${arg} in
        --expanded)
            echo '-[ RECORD 1 ]----+---------------------'
            echo 'pid              | 42'
            echo 'state            | idle in transaction'
            exit 0
            ;;
        *) ;;
    esac
done
n=$(cat "${STUB_OUT}/tries" 2>/dev/null || echo 0)
n=$((n + 1))
echo "${n}" >"${STUB_OUT}/tries"
printf '%s\\n' "${PGOPTIONS:-}" >"${STUB_OUT}/pgoptions"
case $(sed -n "${n}p" "${STUB_OUT}/plan") in
    ok)
        echo '== postgres-grants.sql: done'
        exit 0
        ;;
    lock)
        echo 'psql:x.sql:9: ERROR:  55P03: canceling statement due to lock' >&2
        echo "STATEMENT:  SELECT set_config('ds_grants.api_password', 'hun''ter2')" >&2
        exit 3
        ;;
    statement)
        echo 'psql:x.sql:9: ERROR:  57014: canceling statement due to statement' >&2
        exit 3
        ;;
    connection)
        echo 'psql: error: connection to server failed: timeout expired' >&2
        exit 2
        ;;
    *)
        echo 'psql:x.sql:9: ERROR:  42501: permission denied for table t' >&2
        echo "CONTEXT:  ALTER ROLE r PASSWORD 'hunter3'" >&2
        exit 3
        ;;
esac
"""

PG_ISREADY_STUB = """#!/bin/sh
echo "server:5432 - ${STUB_READY_SAYS}"
exit "${STUB_READY_STATUS}"
"""


class Run:
    """One run of the script: its exit status, output and psql tries."""

    def __init__(
        self, args: list[str], plan: list[str], env: dict[str, str] | None = None
    ) -> None:
        """Run the script with `args`, the psql stub playing `plan`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, body in (("psql", PSQL_STUB), ("pg_isready", PG_ISREADY_STUB)):
                stub = bin_dir / name
                stub.write_text(body)
                stub.chmod(0o755)
            out = root / "out"
            out.mkdir()
            (out / "plan").write_text("".join(f"{step}\n" for step in plan))
            full_env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "TMPDIR": str(root),
                "STUB_OUT": str(out),
                "STUB_READY_STATUS": "0",
                "STUB_READY_SAYS": "accepting connections",
                "PGOPTIONS": "-c lock_timeout=10s",
                "DS_SETUP_BACKOFF_SECONDS": "0",
                **(env or {}),
            }
            result = subprocess.run(  # noqa: S603  # fixed argv, the repo's own script
                ["/bin/sh", str(SCRIPT), *args],
                env=full_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.status = result.returncode
            self.output = result.stdout + result.stderr
            tries = out / "tries"
            self.tries = int(tries.read_text()) if tries.exists() else 0
            pgoptions = out / "pgoptions"
            self.pgoptions = pgoptions.read_text() if pgoptions.exists() else ""
            # The work directory is the script's own, removed on exit.
            self.leftovers = sorted(
                p.name for p in root.iterdir() if p.name not in {"bin", "out"}
            )


RUN = ["run", "--variable=app=a", "--file=/etc/x/postgres-grants.sql"]


class RunTest(unittest.TestCase):
    """`run`: which failures are retried, and how it ends."""

    def test_a_clean_run_is_one_try(self) -> None:
        """Exit 0 after one try, with psql's output and the script's name."""
        run = Run(RUN, ["ok"])
        self.assertEqual(run.status, 0, run.output)
        self.assertEqual(run.tries, 1)
        self.assertIn("postgres-grants.sql: try 1 of 5", run.output)
        self.assertIn("== postgres-grants.sql: done", run.output)
        self.assertEqual(run.leftovers, [])

    def test_a_lock_timeout_is_retried_after_listing_the_blockers(self) -> None:
        """2026-10-14: a 55P03 prints who is in the way and tries again."""
        run = Run(RUN, ["lock", "lock", "ok"])
        self.assertEqual(run.status, 0, run.output)
        self.assertEqual(run.tries, 3)
        self.assertIn("try 1 failed: lock timeout (55P03)", run.output)
        self.assertIn("sessions that may be in the way", run.output)
        self.assertIn("idle in transaction", run.output)
        self.assertIn("try 3 of 5", run.output)
        # The caller's PGOPTIONS reach psql unchanged.
        self.assertEqual(run.pgoptions.strip(), "-c lock_timeout=10s")

    def test_passwords_in_psql_output_are_redacted(self) -> None:
        """An echoed set_config or ALTER ROLE ... PASSWORD shows no password."""
        run = Run(RUN, ["lock", "sql"])
        self.assertEqual(run.status, 3, run.output)
        self.assertNotIn("ter2", run.output)
        self.assertNotIn("hunter3", run.output)
        self.assertIn("'ds_grants.api_password', '<redacted>'", run.output)
        self.assertIn("PASSWORD '<redacted>'", run.output)

    def test_a_statement_timeout_and_a_lost_connection_are_retried(self) -> None:
        """57014 and psql's exit 2 are worth another try too."""
        run = Run(RUN, ["statement", "connection", "ok"])
        self.assertEqual(run.status, 0, run.output)
        self.assertIn("statement timeout (57014)", run.output)
        self.assertIn("connection failed or lost (psql exit 2)", run.output)

    def test_any_other_error_is_final(self) -> None:
        """A permission error fails at once with psql's own status."""
        run = Run(RUN, ["sql", "ok"])
        self.assertEqual(run.status, 3, run.output)
        self.assertEqual(run.tries, 1)
        self.assertIn("not retried", run.output)

    def test_it_gives_up_after_the_last_try(self) -> None:
        """EX_TEMPFAIL (75) once DS_SETUP_ATTEMPTS tries have failed."""
        run = Run(RUN, ["lock"] * 5, {"DS_SETUP_ATTEMPTS": "3"})
        self.assertEqual(run.status, 75, run.output)
        self.assertEqual(run.tries, 3)
        self.assertIn("giving up on postgres-grants.sql after 3 tries", run.output)

    def test_no_try_starts_too_close_to_the_deadline(self) -> None:
        """The pod ends by itself before the Job's deadline kills it."""
        deadline = str(int(time.time()) + 10)
        run = Run(RUN, ["ok"], {"DS_SETUP_DEADLINE": deadline})
        self.assertEqual(run.status, 75, run.output)
        self.assertEqual(run.tries, 0)
        self.assertIn("too little for try 1", run.output)

    def test_no_backoff_runs_past_the_deadline(self) -> None:
        """A retry that could not finish in time is not started."""
        deadline = str(int(time.time()) + 40)
        run = Run(
            RUN,
            ["lock", "ok"],
            {"DS_SETUP_DEADLINE": deadline, "DS_SETUP_BACKOFF_SECONDS": "30"},
        )
        self.assertEqual(run.status, 75, run.output)
        self.assertEqual(run.tries, 1)
        self.assertIn("no time left before the deadline", run.output)

    def test_bad_settings_and_usage_are_rejected(self) -> None:
        """EX_USAGE (64), before any try."""
        for args, env in (
            (["run"], {}),
            ([], {}),
            (RUN, {"DS_SETUP_ATTEMPTS": "many"}),
            (RUN, {"DS_SETUP_DEADLINE": "-1"}),
        ):
            with self.subTest(args=args, env=env):
                run = Run(args, ["ok"], env)
                self.assertEqual(run.status, 64, run.output)
                self.assertEqual(run.tries, 0)


class WaitTest(unittest.TestCase):
    """`wait`: a bounded wait for Postgres."""

    def test_ready_postgres_returns_at_once(self) -> None:
        """Exit 0 and pg_isready's answer in the log."""
        run = Run(["wait"], [])
        self.assertEqual(run.status, 0, run.output)
        self.assertIn("Postgres is ready", run.output)

    def test_it_gives_up_when_postgres_never_answers(self) -> None:
        """EX_UNAVAILABLE (69) after DS_SETUP_WAIT_SECONDS."""
        run = Run(
            ["wait"],
            [],
            {
                "STUB_READY_STATUS": "2",
                "STUB_READY_SAYS": "no response",
                "DS_SETUP_WAIT_SECONDS": "0",
            },
        )
        self.assertEqual(run.status, 69, run.output)
        self.assertIn("pg_isready exit 2: server:5432 - no response", run.output)


if __name__ == "__main__":
    unittest.main()
