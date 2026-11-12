# Legal pages

The frontend's `/privacy`, `/terms`, `/cookies`, `/contact` and
`/accessibility` pages, and `/.well-known/security.txt`, carry the
operator's own details. Those come from chart values, not source: a fork
sets its own and the upstream repository holds no one's.

## Values

```yaml
frontend:
  legalPagesPublished: true # the existing gate; off by default
  legal:
    operatorName: "Example Operator"   # required, up to 100 characters
    contactEmail: "legal@example.com"  # required, a monitored inbox
    ssoLogRetention: "30 days"         # required, as the notice words it
    lastUpdated: "1 November 2026"     # required, "D Month YYYY"; set on go-live
    icoRegistration: ""                # optional; empty leaves the line out
    minimumAge: ""                     # optional, 13 to 21; empty means 18
    hostingProvider: ""                # optional; who hosts the servers, if a third party
```

The chart renders each non-empty value as an env var on the frontend
Deployment (`LEGAL_OPERATOR_NAME`, `LEGAL_CONTACT_EMAIL`,
`LEGAL_SSO_LOG_RETENTION`, `LEGAL_LAST_UPDATED`, `LEGAL_ICO_REGISTRATION`,
`LEGAL_MINIMUM_AGE`, `LEGAL_HOSTING_PROVIDER`) next to `LEGAL_PAGES_PUBLISHED`. The frontend reads them
per request, so a change needs a rollout of the frontend pods but no image
rebuild.

## Safe state

The pages are live only when `legalPagesPublished` is `true` AND the four
required values are set and valid. Otherwise they 404 and the footer hides
their links. A malformed value (an email without a domain, a date that is
not `D Month YYYY`, text over its length limit) counts as unset; the
frontend logs the field names once and never crashes. Defaults are empty, so
installing or upgrading the chart without these values leaves the pages off.

`lastUpdated` is the date the operator last reviewed the text: set it when
the pages go live and again when the text changes.

## Rolling it out

1. Set `frontend.legal.*` in the release values (an unused env is harmless,
   so this can precede the frontend that reads it).
2. Roll out a frontend image that reads the `LEGAL_*` env.
3. Only then remove any other source of the same details.
4. `legalPagesPublished: true` once the text has been reviewed.

The frontend side (validation rules, preview mode, tests) is in the
frontend repository's `docs/legal-pages.md`.
