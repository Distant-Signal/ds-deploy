#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI: the rendered file goes to stdout
r"""Render charts/distant-signal/files/redis-users.acl.tpl as a users.acl.

  uv run scripts/render-redis-acl.py [--stage open|narrow]
      [--default-user on|off|on-unshared] [--no-default-password]
      [--passwords-from-env]

Prints exactly the `users.acl.tpl` data the chart's redis-acl ConfigMap
holds for the same `redis.acl.stage` and `redis.acl.defaultUser`
(scripts/check-ingest-phase0-chart.py checks the two agree), with
`${REDIS_ACL_PASSWORD_<USER>}` placeholders. `on` and `on-unshared` render
the same `default` line: they differ only in where the chart takes
REDIS_ACL_PASSWORD_DEFAULT from (redis.auth's password, or the users
Secret's own `default-password`). --no-default-password renders the
`default` user as `nopass` (`on` only), as the chart does when redis.auth
is off.

--passwords-from-env fills the placeholders from the environment, as the
Redis pod's initContainer does (an empty *_PREVIOUS is dropped), so a
staging Redis can be started with the file:

  REDIS_ACL_PASSWORD_DEFAULT=... REDIS_ACL_PASSWORD_API=... \
    uv run scripts/render-redis-acl.py --stage narrow --passwords-from-env \
    > users.acl && redis-server --aclfile users.acl

The template's format is described at its top; the per-user rights are
checked against a real Redis by crates/common/tests/redis_acl.rs.
"""

import argparse
import os
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "charts"
    / "distant-signal"
    / "files"
    / "redis-users.acl.tpl"
)
KINDS = ("client", "final", "admin")
OPEN_RIGHTS = "~* &* +@all"
PLACEHOLDER = re.compile(r"^>\$\{([A-Z0-9_]+)\}$")


class AclError(Exception):
    """The template or the passwords are invalid."""


def password_var(user: str) -> str:
    """Return the env var holding `user`'s password."""
    return "REDIS_ACL_PASSWORD_" + user.upper().replace("-", "_")


def users(text: str) -> list[tuple[str, str, str]]:
    """Return (user, kind, rules) per template line."""
    result: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[1] not in KINDS:  # noqa: PLR2004  # user and kind
            msg = f"bad template line: {line!r}"
            raise AclError(msg)
        result.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))  # noqa: PLR2004  # the rules
    return result


def render(text: str, *, stage: str, default_user: bool, default_password: bool) -> str:
    """Return users.acl with placeholders, as the chart renders it."""
    if default_user:
        auth = ">${REDIS_ACL_PASSWORD_DEFAULT}" if default_password else "nopass"
        lines = [f"user default reset on {auth} {OPEN_RIGHTS}"]
    else:
        lines = ["user default reset off"]
    for user, kind, rules in users(text):
        open_rights = kind == "admin" or (kind == "client" and stage == "open")
        rights = OPEN_RIGHTS if open_rights else rules
        var = password_var(user)
        lines.append(
            f"user {user} reset on >${{{var}}} >${{{var}_PREVIOUS}} {rights}".rstrip()
        )
    return "\n".join(lines) + "\n"


def fill(acl: str, env: Mapping[str, str]) -> str:
    """Replace the placeholders as the initContainer does."""
    out: list[str] = []
    for line in acl.splitlines():
        tokens: list[str] = []
        for token in line.split():
            match = PLACEHOLDER.match(token)
            if not match:
                tokens.append(token)
                continue
            value = env.get(match.group(1), "")
            if not value:
                if match.group(1).endswith("_PREVIOUS"):
                    continue
                msg = f"{match.group(1)} is empty"
                raise AclError(msg)
            if any(c.isspace() for c in value):
                msg = f"{match.group(1)} contains whitespace"
                raise AclError(msg)
            tokens.append(">" + value)
        out.append(" ".join(tokens))
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the rendered file."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--stage", choices=("open", "narrow"), default="open")
    parser.add_argument(
        "--default-user", choices=("on", "off", "on-unshared"), default="on"
    )
    parser.add_argument("--no-default-password", action="store_true")
    parser.add_argument("--passwords-from-env", action="store_true")
    args = parser.parse_args(argv)
    if args.default_user == "on-unshared" and args.no_default_password:
        print(
            "render-redis-acl: --default-user on-unshared always has its own password",
            file=sys.stderr,
        )
        return 1
    try:
        acl = render(
            TEMPLATE.read_text(encoding="utf-8"),
            stage=args.stage,
            default_user=args.default_user != "off",
            default_password=not args.no_default_password,
        )
        if args.passwords_from_env:
            acl = fill(acl, os.environ)
    except AclError as err:
        print(f"render-redis-acl: {err}", file=sys.stderr)
        return 1
    sys.stdout.write(acl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
