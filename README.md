# ds-deploy

The deploy unit of [Distant Signal](https://github.com/Distant-Signal/distant-signal):
one umbrella Helm chart that runs the whole stack, and the chart for its
Google Cloud Storage landing zone.

| Chart | Published at |
|---|---|
| [`charts/distant-signal`](charts/distant-signal) | `oci://ghcr.io/distant-signal/charts/distant-signal` |
| [`charts/ds-ingest-bucket`](charts/ds-ingest-bucket) | `oci://ghcr.io/distant-signal/charts/ds-ingest-bucket` |

Every push to `main` packages both charts as
`<version>+build.<run>.sha.<commit>`, pushes them and signs them with
cosign keyless signing ([`chart.yml`](.github/workflows/chart.yml)). The
signing identity is
`https://github.com/Distant-Signal/ds-deploy/.github/workflows/chart.yml@refs/heads/main`.

## How a change reaches the cluster

1. A service repository builds its image on `main` and pushes
   `ghcr.io/distant-signal/images/<service>:build.<run id>-<commit>`.
2. A PR bumps the tag and digest in
   [`values.yaml`](charts/distant-signal/values.yaml). Renovate is
   installed here but not running yet, so until it is, these bumps are
   made by hand: a PR that changes an image's `tag`, `digest` and `source`
   together, written by
   [`scripts/pin-images.py`](scripts/pin-images.py) from the registry
   (`uv run --no-sync scripts/pin-images.py --tag build.<run id>-<commit>
   <service>...`, one repository's services at its build tag; `source` is
   the image's `org.opencontainers.image.source` label, which CI checks),
   and for `ds-core` one that changes the [`core-version`](core-version)
   file and the `ds-migrate` release image together and re-vendors its
   access files (`uv run scripts/sync-core.py`). Once Renovate runs, it
   opens them instead: every service image in one `renovate/ds-images` PR,
   and `ds-core` in its own `renovate/ds-core` PR, which merges first. On
   that PR, [`sync-core.yml`](.github/workflows/sync-core.yml) commits the
   re-vendored `ds-core` access files as the `ds-deploy-sync` GitHub App,
   and both PRs are set to automerge when every check passes.
3. The merge pushes a new chart build. Ranma-Config's Renovate files that
   build as a deploy request, and a person approves it.

To hold one service back, revert its `tag` and `digest` together in a PR.

Around a deploy that restarts an ingest workload (a relay, a movement
consumer, the ingest-writer or a poller), Ranma runs
`scripts/check-ingest-rollout.py`: `baseline` before approving it, `verify`
after Flux applies it. It reads only Prometheus, and it fails on any sign
of a gap (data skipped or trimmed unread). See
[docs/ingest-rollout.md](docs/ingest-rollout.md).

## Merging

Pull requests merge as merge commits, by an admin, with the bypass:

```sh
gh pr merge <number> --merge --admin
```

Two repository rulesets apply. "Protect main" stops deletion and force
pushes to `main`. "Sync bot: Renovate branches only" restricts creating,
updating and deleting every branch except `renovate/**`, so the
`ds-deploy-sync` App's token can push only to Renovate's branches. That
rule covers `main` too, so a merge is an update only the bypass list may
make: the Repository admin role and the Renovate app (whose automerge
needs it), not `ds-deploy-sync`. Without `--admin`, GitHub refuses the
merge.

## Checks

[`ci.yml`](.github/workflows/ci.yml) runs on every pull request:

- `helm lint --strict` and the render assertions for both charts;
- the chart policy scripts (`scripts/check-*-chart.py`), among them the
  component map (`charts/distant-signal/components.yaml`, every rendered
  object mapped to a group), and the values reference
  (`scripts/chart-values-doc.py`);
- the alert rules: payload sizes, a runbook section in
  [`docs/alerts.md`](docs/alerts.md) for every alert, and the promtool
  unit tests in `scripts/alert-rules-tests/`;
- the release ordering checks (Distant-Signal/ci's
  `scripts/release-order.py`, run from its workflow pin): every pinned
  image's `core-version` label is at most the pinned `ds-migrate`'s, the
  frontend's `api-build` at most the pinned api's, and every image at
  least the floor of any versioned contract migration in the pinned
  `ds-core` release;
- the config defaults contract (`scripts/check-config-defaults-chart.py
  --images`): the chart against each pinned image's
  `--print-config-defaults`;
- the vendored `ds-core` files against that release's signed assets, and
  `migrate.image.tag` against `core-version` (`scripts/sync-core.py --check`);
- every link to a file in a Distant-Signal repository resolves, anchors
  included (`scripts/check-doc-links.py`): the docs the chart README and
  [`docs/alerts.md`](docs/alerts.md)'s runbooks link to live in the
  service repositories, and a doc that moves fails here;
- licence, commit identity and the scripts lint, from
  [`Distant-Signal/ci`](https://github.com/Distant-Signal/ci).

Locally, with [uv](https://docs.astral.sh/uv/), Helm and cosign:

```sh
uv sync --locked
uv run scripts/sync-core.py --check
uv run scripts/check-doc-links.py            # --checkouts .. reads sibling checkouts instead
uv run scripts/check-config-defaults-chart.py --images   # needs docker
uv run python -m unittest discover -s scripts/tests
```

## Issues

File them in
[Distant-Signal/distant-signal](https://github.com/Distant-Signal/distant-signal/issues),
with the `area:deploy` label: this repository has Issues turned off. The
contributing guide for every repository is the organisation's
[CONTRIBUTING.md](https://github.com/Distant-Signal/.github/blob/main/CONTRIBUTING.md);
[CLAUDE.md](CLAUDE.md) lists this repository's commands and conventions.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
