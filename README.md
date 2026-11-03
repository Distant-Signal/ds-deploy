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
2. Renovate bumps the tag and digest in
   [`values.yaml`](charts/distant-signal/values.yaml): every service image
   in one `renovate/ds-images` PR, and `ds-core` (the
   [`core-version`](core-version) file and the `ds-migrate` release image)
   in its own `renovate/ds-core` PR, which merges first. On that PR,
   [`sync-core.yml`](.github/workflows/sync-core.yml) commits the
   re-vendored `ds-core` access files as the `ds-deploy-sync` GitHub App.
   Both PRs automerge when every check passes.
3. The merge pushes a new chart build. Ranma-Config's Renovate files that
   build as a deploy request, and a person approves it.

To hold one service back, revert its `tag` and `digest` together in a PR.

## Checks

[`ci.yml`](.github/workflows/ci.yml) runs on every pull request:

- `helm lint --strict` and the render assertions for both charts;
- the chart policy scripts (`scripts/check-*-chart.py`) and the values
  reference (`scripts/chart-values-doc.py`);
- the alert rules: payload sizes, a runbook section in
  [`docs/alerts.md`](docs/alerts.md) for every alert, and the promtool
  unit tests in `scripts/alert-rules-tests/`;
- the release ordering checks (`scripts/release-order.py`): every pinned
  image's `core-version` label is at most the pinned `ds-migrate`'s, the
  frontend's `api-build` at most the pinned api's, and every image at
  least the floor of any versioned contract migration in the pinned
  `ds-core` release;
- the config defaults contract (`scripts/check-config-defaults-chart.py
  --images`): the chart against each pinned image's
  `--print-config-defaults`;
- the vendored `ds-core` files against that release's signed assets, and
  `migrate.image.tag` against `core-version` (`scripts/sync-core.py --check`);
- licence, commit identity and the scripts lint, from
  [`Distant-Signal/ci`](https://github.com/Distant-Signal/ci).

Locally, with [uv](https://docs.astral.sh/uv/), Helm and cosign:

```sh
uv sync --locked
uv run scripts/sync-core.py --check
uv run scripts/check-config-defaults-chart.py --images   # needs docker
uv run python -m unittest discover -s scripts/tests
```

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
