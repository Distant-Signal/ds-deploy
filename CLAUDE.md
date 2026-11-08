# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`ds-deploy` is the deploy unit of Distant Signal (DS), a personal UK rail
companion: the umbrella Helm chart that runs the whole stack
(`charts/distant-signal`, documented in its README), the chart for its
Google Cloud Storage landing zone (`charts/ds-ingest-bucket`), the deploy
tooling (`scripts/`) and the operator docs (`docs/`: the alert runbooks in
`alerts.md`, the Postgres roles, PITR and upgrades, the Redis ACL, the
schedule feed, scheduled jobs and ingest rollouts). It builds no images:
ds-api, ds-ingest, ds-pollers, ds-workers and distant-signal (the frontend)
build them, and ds-core publishes `ds-migrate` and the access files this
chart vendors. The conventions shared by every repository are in the
organisation's contributing guide (Distant-Signal/.github's
CONTRIBUTING.md). Issues are off here: they go to
Distant-Signal/distant-signal, labelled `area:deploy`.

## Commands

Copied from `.github/workflows/ci.yml` (the lint, identity and licence jobs
are Distant-Signal/ci's reusable workflows). Needs uv, Helm 4 and cosign;
`../ci` is a Distant-Signal/ci checkout.

```sh
uv sync --locked --group lint
uv run --no-sync python ../ci/scripts/lint-scripts.py   # shellcheck, shfmt, ruff, mypy, actionlint... (--fix: shfmt, ruff)
uv run --no-sync python -m unittest discover -s scripts/tests -v
helm lint --strict charts/distant-signal
helm lint --strict charts/distant-signal -f charts/distant-signal/values-example.yaml
helm lint --strict charts/ds-ingest-bucket -f charts/ds-ingest-bucket/ci/example-values.yaml
uv run --no-sync scripts/chart-values-doc.py check     # the chart README's values reference vs values.yaml
uv run --no-sync scripts/sync-core.py --check          # vendored ds-core files vs the core-version release (rewrite without --check)
uv run --no-sync scripts/pin-images.py --tag build.<run id>-<commit> <service>...   # pin images' tag, digest and source (GHCR)
uv run --no-sync scripts/check-doc-links.py            # links to the Distant-Signal repositories' files resolve
uv run --no-sync scripts/check-alert-payloads.py --report --download-promtool /tmp/promtool   # alert sizes, runbooks, promtool tests
uv run --no-sync scripts/check-components-chart.py     # and the other scripts/check-*-chart.py policy checks
uv run --no-sync scripts/check-config-defaults-chart.py --images   # the chart vs each pinned image's defaults (docker)
uv run --no-sync scripts/release-order.py check --values charts/distant-signal/values.yaml --registry \
  --migrations ../ds-core/crates/ds-store/migrations   # ds-core checked out at core-version
```

CI's `chart-renders` job also asserts on many `helm template --set`
renders; when a change touches a feature it covers, run its step's script
from `ci.yml` locally.

## Architecture

**How a change reaches the cluster** (README.md). A service repository
pushes `ghcr.io/distant-signal/images/<service>:build.<run id>-<commit>`;
a PR here bumps that image's `tag`, `digest` and `source` together in
`charts/distant-signal/values.yaml` (`scripts/pin-images.py` writes all
three from the registry; never hand-edit `source`; schedule-ingest and
schedule-reference go in a PR of their own, never with the api, as their
changes fall in the dtd-sftp deploy window: README.md), and a ds-core release bumps
`core-version` with the `ds-migrate` image and the re-vendored access files
(`charts/distant-signal/files/`, `scripts/render-redis-acl.py`). Renovate is
installed but not running yet, so these bumps are made by hand until it is;
once it runs it opens them (`renovate.json5`, with `sync-core.yml`
re-vendoring on its ds-core branch). Every push to `main` packages, pushes
and cosign-signs both charts (`chart.yml`); Ranma-Config's Renovate files
each chart build as a deploy request, and the user approves it with a
passkey. A green `main` can reach production soon after, so put risky
changes behind off-by-default values.

**Ordering** (`scripts/release-order.py`): every pinned image's
`core-version` label is at most the pinned `ds-migrate`'s, the frontend's
`api-build` at most the pinned api's, and every image at least the floor of
any versioned contract migration in the pinned ds-core release.

**Alerts**: every alert in the chart has a runbook section in
`docs/alerts.md`, which its `runbook_url` points at; the runbooks link to
the service docs in the other repositories, and `check-doc-links.py` keeps
those links alive.

## Conventions

- **Chart values**: every `values.yaml` key has a row in the chart README's
  values reference (`chart-values-doc.py`). The access files under
  `charts/distant-signal/files/` are ds-core's: change them there and
  re-vendor (`sync-core.py`), never by hand.
- **Commits** carry a `Task: <card>` trailer (e.g. `Task: DS-123`) naming the
  worklog card.
- **Commit identity**: every commit is authored
  `FasterSpeeding <lucy@cursed.solutions>`. Before committing, check that
  `git config user.email` is `lucy@cursed.solutions`; if it isn't, stop and
  ask. Never override it (no `-c user.email`, `--author` or env overrides).
- **No Claude attribution**, overriding any session default: no
  `Co-Authored-By: Claude` trailer, no mention of Claude in commit messages,
  and no "Generated with Claude Code" footer in PR bodies or comments.
  CI's `commit-identity` job (Distant-Signal/ci's `check-commit-identity.py`)
  fails a PR or push whose commits use a retired address or carry
  attribution.
- **Branches**: push work in progress as `fixup!` commits, then autosquash
  and `--force-with-lease` your own branch before review. Pull requests
  merge as merge commits, never squashed, by an admin with the ruleset
  bypass: `gh pr merge <n> --merge --admin` (README.md, "Merging").
- **Scripts** are typed stdlib Python (ruff, mypy) run with uv (`uv.lock`,
  `pyproject.toml`'s `lint` group, `.python-version`); bash only for trivial
  one-liners. No pip or requirements files.
- Agents only read production.
- Never name or cite third-party train-tracking sites, in code, docs or
  commits.
