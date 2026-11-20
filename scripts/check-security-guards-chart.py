#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's security guards (chart README, "Production guards").

  uv run scripts/check-security-guards-chart.py [--helm HELM]

  - global.production (off by default, which changes nothing) fails the
    render, naming every gap, unless networkPolicy and its egress policy,
    a Redis password with the narrow ACL, and per-service Postgres roles
    are on; the production example values satisfy it.
  - With global.production, the SFTP receiver on a LoadBalancer with no
    allowedCidrs fails the render unless acknowledged; without it, it
    renders (a NOTES.txt warning only).
  - The api exposed by an Ingress with api.rateLimit.trustXRealIp, and a
    delegated client without a NetworkPolicy, fail the render unless
    acknowledged (neither is in the defaults or the production example).
  - Client-address trust (clientTrustGuards): trustXRealIp without a
    NetworkPolicy, an exposed (NodePort or LoadBalancer) api or frontend
    Service, a namespace admitted to the api with no pod labels, an api or
    frontend extraIngress rule and a tunnel with no pod labels each fail the
    render unless acknowledged by their own value; the live production
    layout (tunnel, token listener, ds-mcp) still renders.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm).
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "distant-signal"


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# The least a fresh install adds (release B's ingest prerequisites), as in
# the other chart checks.
BASE = (
    "-f",
    str(CHART / "ci" / "ingest-prereqs.yaml"),
    *sets(
        "trustConsumer.kafka.brokers=k:9094",
        "trustConsumer.kafka.topic=t",
        "trustConsumer.kafka.saslMechanism=PLAIN",
        "enricher.llm.baseUrl=http://l/v1",
        "enricher.llm.model=m",
        "api.sso.issuerUrl=https://sso.example.com",
        "api.sso.clientId=c",
        "api.sso.clientSecret=s",
        "api.sso.redirectUrl=https://app.example.com/cb",
        "api.sso.postLoginRedirectUrl=https://app.example.com/",
    ),
)
PRODUCTION_EXAMPLE = ("-f", str(CHART / "values-production-example.yaml"))
# BASE already has the narrow Redis ACL and the per-service Postgres roles
# on (ci/ingest-prereqs.yaml); this adds the rest of what global.production
# needs.
ISOLATED = sets(
    "global.production=true",
    "networkPolicy.enabled=true",
    "networkPolicy.egress.enabled=true",
    "redis.auth.enabled=true",
)
# The same with the Redis ACL and Postgres roles off, as a fresh install has them.
UNISOLATED = sets(
    "global.production=true",
    "redis.acl.enabled=false",
    "postgresql.roles.enabled=false",
    "postgresql.roles.perService.enabled=false",
)
EXTERNAL_REDIS = sets(
    "redis.enabled=false",
    "redis.externalUrl=rediss://redis.example.com:6380",
)
SFTP = sets(
    "scheduleFeed.enabled=true",
    "scheduleFeed.sftp.authMethod=password",
    "scheduleFeed.reference.mode=cronjob",
)
BUCKET_ONLY = sets(
    "scheduleFeed.sftp.enabled=false",
    "scheduleFeed.bucket.enabled=true",
    "scheduleFeed.bucket.name=example-ds-ingest",
    "scheduleFeed.bucket.existingSecret=distant-signal-schedulefeed-bucket",
)
INGRESS_API = sets(
    "ingress.enabled=true",
    "ingress.frontend.host=ds.example.com",
    "ingress.api.enabled=true",
    "ingress.api.host=api.example.com",
)
DELEGATED = sets(
    "api.delegated.clients[0].name=ds-mcp",
    "api.delegated.clients[0].displayName=Assistant",
    "api.delegated.clients[0].issuerUrl=https://sso.example.com/application/o/api/",
    "api.delegated.clients[0].audience=distant-signal-api",
    "api.delegated.clients[0].maxScopes={journeys:read}",
)


def set_json(key: str, value: str) -> tuple[str, ...]:
    """Return the `--set-json` arguments for `key=value` (JSON)."""
    return ("--set-json", f"{key}={value}")


# BASE acknowledges trustXRealIp without a NetworkPolicy (ci/ingest-prereqs.yaml),
# so a case about the refusal clears it; POLICY turns the policy on.
NO_POLICY_ACK = "api.rateLimit.trustXRealIpWithoutNetworkPolicyAcknowledged"
NO_POLICY = sets(f"{NO_POLICY_ACK}=false")
POLICY = sets("networkPolicy.enabled=true")
# The live layout's pieces: ds-mcp admitted to the api (narrowed by the default
# apiExtraIngressPodLabels), the tunnel connector, the token listener.
MCP_NAMESPACE = sets("networkPolicy.apiExtraIngressNamespaces={ds-mcp}")
TUNNEL = sets("ingress.enabled=false", "networkPolicy.tunnel.enabled=true")
TOKENS = sets(
    "networkPolicy.tunnel.tokenApi=true",
    "api.personalTokens.enabled=true",
    "api.personalTokens.publicBaseUrl=https://api.example.com",
    "api.personalTokens.trustCfConnectingIp=true",
)
API_EXTRA_INGRESS = "networkPolicy.components.api.extraIngress"
FRONTEND_EXTRA_INGRESS = "networkPolicy.components.frontend.extraIngress"
EXTRA_RULE = '[{"from":[{"podSelector":{}}]}]'


@dataclass(frozen=True)
class Case:
    """One render: its arguments and what it must say (None: render fine)."""

    name: str
    args: tuple[str, ...]
    refusal: str | None = None


CASES: tuple[Case, ...] = (
    Case("defaults render with global.production off", BASE),
    Case(
        "global.production refuses the defaults, naming every gap",
        (*BASE, *UNISOLATED),
        "networkPolicy.enabled is false",
    ),
    Case(
        "global.production names the Postgres gap too",
        (*BASE, *UNISOLATED),
        "postgresql.roles.enabled is false",
    ),
    Case(
        "global.production names the Redis gap too",
        (*BASE, *UNISOLATED),
        "redis.acl.enabled is false",
    ),
    Case("global.production passes with every control on", (*BASE, *ISOLATED)),
    Case(
        "global.production needs the egress policy",
        (*BASE, *ISOLATED, *sets("networkPolicy.egress.enabled=false")),
        "networkPolicy.egress.enabled is false",
    ),
    Case(
        "global.production needs the narrow ACL stage",
        (*BASE, *ISOLATED, *sets("redis.acl.stage=open")),
        "redis.acl.stage is",
    ),
    Case(
        "global.production needs a Redis password",
        (
            *BASE,
            *ISOLATED,
            *sets("redis.auth.enabled=false", "redis.acl.defaultUser=on"),
        ),
        "Redis has no password",
    ),
    Case(
        "an unshared default user is a Redis password",
        (
            *BASE,
            *ISOLATED,
            *sets("redis.auth.enabled=false", "redis.acl.defaultUser=on-unshared"),
        ),
    ),
    Case(
        "global.production needs a password on an external Redis",
        (*BASE, *ISOLATED, *EXTERNAL_REDIS, *sets("redis.auth.enabled=false")),
        "redis.externalUrl carries no credentials",
    ),
    Case(
        "an external Redis with redis.auth renders",
        (*BASE, *ISOLATED, *EXTERNAL_REDIS),
    ),
    Case(
        "an external Redis with credentials in its URL is accepted",
        (
            *BASE,
            *ISOLATED,
            *sets(
                "redis.enabled=false",
                "redis.auth.enabled=false",
                "redis.externalUrl=rediss://user:pw@redis.example.com:6380",
            ),
        ),
    ),
    Case(
        "global.production needs per-service Postgres roles",
        (*BASE, *ISOLATED, *sets("postgresql.roles.perService.enabled=false")),
        "postgresql.roles.perService.enabled is false",
    ),
    Case(
        "ingress.api with trustXRealIp refuses to render",
        (*BASE, *INGRESS_API),
        "ingress.api.enabled is true while api.rateLimit.trustXRealIp is true",
    ),
    Case(
        "ingress.api with trustXRealIp renders when acknowledged",
        (
            *BASE,
            *INGRESS_API,
            *sets("api.rateLimit.trustXRealIpIngressAcknowledged=true"),
        ),
    ),
    Case(
        "ingress.api renders without trustXRealIp",
        (*BASE, *INGRESS_API, *sets("api.rateLimit.trustXRealIp=false")),
    ),
    Case(
        "a delegated client without a NetworkPolicy refuses to render",
        (*BASE, *DELEGATED),
        "api.delegated.clients has an enabled client while networkPolicy.enabled",
    ),
    Case(
        "a delegated client renders when acknowledged",
        (*BASE, *DELEGATED, *sets("api.delegated.allowWithoutNetworkPolicy=true")),
    ),
    Case(
        "a delegated client renders with the NetworkPolicy on",
        (*BASE, *DELEGATED, *sets("networkPolicy.enabled=true")),
    ),
    Case(
        "a disabled delegated client needs no NetworkPolicy",
        (*BASE, *DELEGATED, *sets("api.delegated.clients[0].enabled=false")),
    ),
    # Client-address trust (clientTrustGuards): each refusal, its
    # acknowledgement, and the value that makes it moot.
    Case(
        "trustXRealIp without a NetworkPolicy refuses to render",
        (*BASE, *NO_POLICY),
        "api.rateLimit.trustXRealIp is true while networkPolicy.enabled is false",
    ),
    Case(
        "trustXRealIp without a NetworkPolicy renders when acknowledged",
        (*BASE, *sets(f"{NO_POLICY_ACK}=true")),
    ),
    Case(
        "trustXRealIp off needs no NetworkPolicy",
        (*BASE, *NO_POLICY, *sets("api.rateLimit.trustXRealIp=false")),
    ),
    Case(
        "trustXRealIp with the NetworkPolicy on renders",
        (*BASE, *NO_POLICY, *POLICY),
    ),
    Case(
        "a LoadBalancer api Service with trustXRealIp refuses to render",
        (*BASE, *POLICY, *sets("api.service.type=LoadBalancer")),
        "api.service.type is LoadBalancer while api.rateLimit.trustXRealIp is true",
    ),
    Case(
        "a NodePort api Service with trustXRealIp refuses to render",
        (*BASE, *POLICY, *sets("api.service.type=NodePort")),
        "api.service.type is NodePort while api.rateLimit.trustXRealIp is true",
    ),
    Case(
        "an exposed api Service renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *sets(
                "api.service.type=NodePort", "api.service.exposedTypeAcknowledged=true"
            ),
        ),
    ),
    Case(
        "an exposed api Service renders without trustXRealIp",
        (
            *BASE,
            *POLICY,
            *sets("api.service.type=NodePort", "api.rateLimit.trustXRealIp=false"),
        ),
    ),
    Case(
        "a NodePort frontend Service refuses to render",
        (*BASE, *POLICY, *sets("frontend.service.type=NodePort")),
        "frontend.service.type is NodePort",
    ),
    Case(
        "a LoadBalancer frontend Service refuses to render",
        (*BASE, *POLICY, *sets("frontend.service.type=LoadBalancer")),
        "frontend.service.type is LoadBalancer",
    ),
    Case(
        "an exposed frontend Service renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *sets(
                "frontend.service.type=LoadBalancer",
                "frontend.service.exposedTypeAcknowledged=true",
            ),
        ),
    ),
    Case(
        "a namespace with no pod labels refuses to render (null entry)",
        (
            *BASE,
            *POLICY,
            *MCP_NAMESPACE,
            *sets("networkPolicy.apiExtraIngressPodLabels.ds-mcp=null"),
        ),
        "with no pod labels in networkPolicy.apiExtraIngressPodLabels",
    ),
    Case(
        "a namespace with no pod labels refuses to render (not in the labels)",
        (*BASE, *POLICY, *sets("networkPolicy.apiExtraIngressNamespaces={other}")),
        "with no pod labels in networkPolicy.apiExtraIngressPodLabels",
    ),
    Case(
        "a whole namespace renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *MCP_NAMESPACE,
            *sets(
                "networkPolicy.apiExtraIngressPodLabels.ds-mcp=null",
                "networkPolicy.apiExtraIngressWholeNamespaceAcknowledged=true",
            ),
        ),
    ),
    Case(
        "a whole namespace renders without trustXRealIp",
        (
            *BASE,
            *POLICY,
            *MCP_NAMESPACE,
            *sets(
                "networkPolicy.apiExtraIngressPodLabels.ds-mcp=null",
                "api.rateLimit.trustXRealIp=false",
            ),
        ),
    ),
    Case(
        "a namespace narrowed to its pods renders",
        (*BASE, *POLICY, *MCP_NAMESPACE),
    ),
    Case(
        "an api extraIngress rule refuses to render",
        (*BASE, *POLICY, *set_json(API_EXTRA_INGRESS, EXTRA_RULE)),
        "networkPolicy.components.api.extraIngress has rules",
    ),
    Case(
        "an api extraIngress rule renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *set_json(API_EXTRA_INGRESS, EXTRA_RULE),
            *sets("networkPolicy.trustedExtraIngressAcknowledged=true"),
        ),
    ),
    Case(
        "an api extraIngress rule renders without trustXRealIp",
        (
            *BASE,
            *POLICY,
            *set_json(API_EXTRA_INGRESS, EXTRA_RULE),
            *sets("api.rateLimit.trustXRealIp=false"),
        ),
    ),
    Case(
        "a frontend extraIngress rule refuses to render",
        (*BASE, *POLICY, *set_json(FRONTEND_EXTRA_INGRESS, EXTRA_RULE)),
        "networkPolicy.components.frontend.extraIngress has rules",
    ),
    Case(
        "a frontend extraIngress rule renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *set_json(FRONTEND_EXTRA_INGRESS, EXTRA_RULE),
            *sets("networkPolicy.trustedExtraIngressAcknowledged=true"),
        ),
    ),
    Case(
        "a tunnel with no pod labels refuses to render",
        (*BASE, *POLICY, *TUNNEL, *sets("networkPolicy.tunnel.podLabels=null")),
        "networkPolicy.tunnel.podLabels is empty",
    ),
    Case(
        "a tunnel with no pod labels renders when acknowledged",
        (
            *BASE,
            *POLICY,
            *TUNNEL,
            *sets(
                "networkPolicy.tunnel.podLabels=null",
                "networkPolicy.tunnel.wholeNamespaceAcknowledged=true",
            ),
        ),
    ),
    Case(
        "a tunnel narrowed to the connector's pods renders", (*BASE, *POLICY, *TUNNEL)
    ),
    Case(
        "the live production layout renders (tunnel, token listener, ds-mcp)",
        (*PRODUCTION_EXAMPLE, *TUNNEL, *TOKENS, *DELEGATED, *MCP_NAMESPACE),
    ),
    Case("an open SFTP LoadBalancer renders without global.production", (*BASE, *SFTP)),
    Case(
        "global.production refuses an open SFTP LoadBalancer",
        (*BASE, *ISOLATED, *SFTP),
        "scheduleFeed.sftp.allowedCidrs is empty",
    ),
    Case(
        "global.production allows an SFTP LoadBalancer with allowedCidrs",
        (
            *BASE,
            *ISOLATED,
            *SFTP,
            *sets("scheduleFeed.sftp.allowedCidrs[0]=192.0.2.0/24"),
        ),
    ),
    Case(
        "global.production allows an acknowledged open SFTP LoadBalancer",
        (*BASE, *ISOLATED, *SFTP, *sets("scheduleFeed.sftp.allowAnySource=true")),
    ),
    Case(
        "global.production allows bucket-only delivery",
        (*BASE, *ISOLATED, *SFTP, *BUCKET_ONLY),
    ),
    Case(
        "global.production allows a NodePort SFTP Service",
        (*BASE, *ISOLATED, *SFTP, *sets("scheduleFeed.service.type=NodePort")),
    ),
    Case("the production example values satisfy global.production", PRODUCTION_EXAMPLE),
    Case(
        "the production example fails without its NetworkPolicy",
        (*PRODUCTION_EXAMPLE, *sets("networkPolicy.enabled=false")),
        "networkPolicy.enabled is false",
    ),
)


def render(helm: str, args: Sequence[str]) -> tuple[int, str]:
    """Return `helm template`'s exit status and its stderr."""
    result = subprocess.run(  # noqa: S603  # helm, with this script's own args
        [helm, "template", "distant-signal", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stderr


def problems(helm: str) -> list[str]:
    """Return one line for each case that doesn't behave."""
    found: list[str] = []
    for case in CASES:
        status, error = render(helm, case.args)
        if case.refusal is None:
            if status != 0:
                found.append(f"{case.name}: refused to render: {error.strip()[:300]}")
        elif status == 0:
            found.append(f"{case.name}: rendered, expected a refusal")
        elif case.refusal not in error:
            found.append(
                f"{case.name}: refused without {case.refusal!r}: {error.strip()[:300]}"
            )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Run every case; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    failures = problems(args.helm)
    for failure in failures:
        print(failure)
    if not failures:
        print(f"ok: {len(CASES)} security guard renders behave")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
