#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the failures) is stdout
"""Check charts/distant-signal's enricher LLM provider settings.

  uv run scripts/check-enricher-provider-chart.py [--helm HELM]

`enricher.llm.provider` (docs/enricher-anthropic.md) picks the enricher's
LLM API. This renders the chart and checks:

  - the default (`openai`) renders the enricher exactly as before: no
    LLM_PROVIDER or Claude env, LLM_BASE_URL/LLM_MODEL from
    enricher.llm.baseUrl/model, LLM_API_KEY from llm-api-key;
  - `anthropic` renders LLM_PROVIDER, the Claude base URL and model, the
    key from enricher.llm.anthropic.existingSecret (never the chart's own
    Secret), LLM_PROMPT_CACHE, and no batch env unless batch mode is on;
  - batch mode renders LLM_SWEEP_MODE and the LLM_BATCH_* knobs;
  - with egress NetworkPolicies on, the enricher's internet rule opens the
    Claude base URL's port without enricher.llm.baseUrl being set;
  - every unusable combination refuses to render: no Claude key Secret, an
    OpenAI workload identity mode, batch mode on openai, an unknown
    provider, sweep mode or prompt-cache TTL;
  - keyless Claude auth (`anthropicWifAuthentik`) renders the Claude IDs,
    the generic projected-token path, an audience-bound token for the
    Authentik client, no API key anywhere, the token URLs' ports and the
    token-exchange alert, and refuses to render without its IDs, a
    dedicated ServiceAccount, with a key Secret, or on openai.

Exit 1 with one line per failed check. Needs helm on PATH (or --helm) and
PyYAML (pyproject.toml's lint group).
"""

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import yaml

CHART = "charts/distant-signal"
# Release A (2026-10-09) put every ingest producer on its db/stream sink by
# default; these checks predate it and test other switches, so they render
# on ci/http-sinks.yaml (every producer back on http, the writer on app).
HTTP_SINKS = f"{CHART}/ci/http-sinks.yaml"
ENRICHER = "distant-signal-enricher"


def sets(*pairs: str) -> tuple[str, ...]:
    """Return `--set` arguments for each `key=value`."""
    return tuple(a for p in pairs for a in ("--set", p))


# Everything a render needs except the enricher's LLM settings.
BASE = (
    "-f",
    HTTP_SINKS,
    *sets(
        "trustConsumer.kafka.brokers=k:9094",
        "trustConsumer.kafka.topic=t",
        "trustConsumer.kafka.saslMechanism=PLAIN",
        "api.sso.issuerUrl=https://sso.example.com",
        "api.sso.clientId=c",
        "api.sso.clientSecret=s",
        "api.sso.redirectUrl=https://app.example.com/cb",
        "api.sso.postLoginRedirectUrl=https://app.example.com/",
    ),
)
OPENAI = sets("enricher.llm.baseUrl=http://l/v1", "enricher.llm.model=m")
ANTHROPIC = sets(
    "enricher.llm.provider=anthropic",
    "enricher.llm.anthropic.existingSecret=enricher-anthropic",
)
BATCH = sets(
    "enricher.llm.batch.sweepMode=batch",
    "enricher.llm.batch.minItems=5",
    "enricher.llm.batch.maxItems=300",
    "enricher.llm.batch.pollIntervalSecs=120",
)
EGRESS = sets("networkPolicy.enabled=true", "networkPolicy.egress.enabled=true")
# Keyless Claude auth (enricher.llm.auth=anthropicWifAuthentik).
CLAUDE_WIF = sets(
    "enricher.llm.provider=anthropic",
    "enricher.llm.auth=anthropicWifAuthentik",
    "enricher.serviceAccount.create=true",
    "enricher.llm.workloadIdentity.anthropic.organizationId=org-uuid",
    "enricher.llm.workloadIdentity.anthropic.serviceAccountId=svac_1",
    "enricher.llm.workloadIdentity.anthropic.federationRuleId=fdrl_1",
    "enricher.llm.workloadIdentity.authentik.tokenUrl=https://sso.example.com:8443/application/o/token/",
    "enricher.llm.workloadIdentity.authentik.clientId=ds-enricher-anthropic",
)

type Doc = dict[str, object]


def as_map(value: object) -> dict[str, object]:
    """Return `value` if it is a mapping, else an empty one."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return `value` if it is a list, else an empty one."""
    return cast("list[object]", value) if isinstance(value, list) else []


class Checker:
    """Renders the chart and collects failed checks."""

    def __init__(self, helm: str) -> None:
        """Use `helm` to render."""
        self.helm = helm
        self.failures: list[str] = []

    def render(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run `helm template` with BASE and `args`."""
        return subprocess.run(  # noqa: S603  # helm from PATH or --helm, fixed arguments
            [self.helm, "template", "distant-signal", CHART, *BASE, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def docs(self, label: str, *args: str) -> list[Doc]:
        """Render and parse every document; record a failed render."""
        result = self.render(*args)
        if result.returncode != 0:
            self.failures.append(f"{label}: render failed: {result.stderr.strip()}")
            return []
        return [cast("Doc", d) for d in yaml.safe_load_all(result.stdout) if d]

    def refuses(self, label: str, needle: str, *args: str) -> None:
        """Check a render fails with `needle` in its error."""
        result = self.render(*args)
        if result.returncode == 0:
            self.failures.append(f"{label}: rendered, but must refuse")
        elif needle not in result.stderr:
            self.failures.append(
                f"{label}: refused without {needle!r}: {result.stderr.strip()}"
            )


def find(docs: Sequence[Doc], kind: str, name: str) -> Doc:
    """Return the document of `kind` named `name` (empty if absent)."""
    for doc in docs:
        if doc.get("kind") == kind and as_map(doc.get("metadata")).get("name") == name:
            return doc
    return {}


def enricher_env(docs: Sequence[Doc]) -> dict[str, object]:
    """Return the enricher container's env: name -> value, or valueFrom."""
    spec = as_map(
        as_map(find(docs, "Deployment", ENRICHER).get("spec")).get("template")
    )
    containers = as_list(as_map(spec.get("spec")).get("containers"))
    env: dict[str, object] = {}
    for container in containers:
        for entry in as_list(as_map(container).get("env")):
            item = as_map(entry)
            env[str(item.get("name"))] = item.get("value", item.get("valueFrom"))
    return env


def secret_ref(value: object) -> tuple[object, object]:
    """(name, key) of a `valueFrom.secretKeyRef`."""
    ref = as_map(as_map(value).get("secretKeyRef"))
    return ref.get("name"), ref.get("key")


def expect(
    c: Checker, label: str, env: dict[str, object], want: dict[str, object]
) -> None:
    """Check `env` holds every `want` entry (None: absent)."""
    for name, value in want.items():
        if value is None:
            if name in env:
                c.failures.append(f"{label}: {name} must not render, got {env[name]!r}")
        elif env.get(name) != value:
            c.failures.append(
                f"{label}: {name} must be {value!r}, got {env.get(name)!r}"
            )


def check_openai_default(c: Checker) -> None:
    """Check the default renders the enricher as before the provider switch."""
    env = enricher_env(c.docs("openai", *OPENAI))
    expect(
        c,
        "openai",
        env,
        {
            "LLM_BASE_URL": "http://l/v1",
            "LLM_MODEL": "m",
            "LLM_PROVIDER": None,
            "LLM_PROMPT_CACHE": None,
            "LLM_SWEEP_MODE": None,
        },
    )
    if secret_ref(env.get("LLM_API_KEY")) != ("distant-signal", "llm-api-key"):
        c.failures.append(
            "openai: LLM_API_KEY must come from llm-api-key, "
            f"got {env.get('LLM_API_KEY')!r}"
        )


def check_anthropic(c: Checker) -> None:
    """Check provider anthropic: the Claude env and key, no batch env by default."""
    env = enricher_env(c.docs("anthropic", *ANTHROPIC))
    expect(
        c,
        "anthropic",
        env,
        {
            "LLM_PROVIDER": "anthropic",
            "LLM_BASE_URL": "https://api.anthropic.com/v1",
            "LLM_MODEL": "claude-haiku-5-5",
            "LLM_PROMPT_CACHE": "1h",
            "LLM_THINKING": None,
            "LLM_AUTH": None,
            "LLM_SWEEP_MODE": None,
        },
    )
    if secret_ref(env.get("LLM_API_KEY")) != (
        "enricher-anthropic",
        "anthropic-api-key",
    ):
        c.failures.append(
            "anthropic: LLM_API_KEY must come from "
            "enricher-anthropic/anthropic-api-key, "
            f"got {env.get('LLM_API_KEY')!r}"
        )
    env = enricher_env(
        c.docs(
            "anthropic tuned",
            *ANTHROPIC,
            *BATCH,
            *sets(
                "enricher.llm.anthropic.model=claude-sonnet-5-5",
                "enricher.llm.anthropic.promptCache=5m",
                "enricher.llm.anthropic.thinking=between_tools",
            ),
        )
    )
    expect(
        c,
        "anthropic batch",
        env,
        {
            "LLM_MODEL": "claude-sonnet-5-5",
            "LLM_PROMPT_CACHE": "5m",
            "LLM_THINKING": "between_tools",
            "LLM_SWEEP_MODE": "batch",
            "LLM_BATCH_MIN_ITEMS": "5",
            "LLM_BATCH_MAX_ITEMS": "300",
            "LLM_BATCH_POLL_SECS": "120",
        },
    )


def internet_ports(policy: Doc) -> list[int]:
    """Ports of the egress rules that allow the public internet."""
    ports: list[int] = []
    for rule in as_list(as_map(policy.get("spec")).get("egress")):
        to = as_list(as_map(rule).get("to"))
        cidrs = {as_map(as_map(peer).get("ipBlock")).get("cidr") for peer in to}
        if cidrs & {"0.0.0.0/0", "::/0"}:
            ports.extend(
                cast("int", as_map(p).get("port"))
                for p in as_list(as_map(rule).get("ports"))
            )
    return sorted(set(ports))


def check_egress(c: Checker) -> None:
    """Check the internet rule opens the Claude base URL's port."""
    docs = c.docs(
        "anthropic egress",
        *ANTHROPIC,
        *EGRESS,
        *sets(
            "enricher.llm.anthropic.baseUrl=https://claude-proxy.example.com:8443/v1"
        ),
    )
    ports = internet_ports(find(docs, "NetworkPolicy", ENRICHER))
    # 443 is always open; the custom URL adds its own port.
    if ports != [443, 8443]:
        c.failures.append(
            "anthropic egress: enricher internet ports must be [443, 8443], "
            f"got {ports}"
        )
    docs = c.docs("anthropic egress default", *ANTHROPIC, *EGRESS)
    ports = internet_ports(find(docs, "NetworkPolicy", ENRICHER))
    if ports != [443]:
        c.failures.append(f"anthropic egress default: want [443], got {ports}")


def check_refusals(c: Checker) -> None:
    """Every unusable combination refuses to render."""
    c.refuses(
        "anthropic without a key Secret",
        "needs enricher.llm.anthropic.existingSecret",
        *sets("enricher.llm.provider=anthropic"),
    )
    c.refuses(
        "anthropic with an OpenAI workload identity mode",
        "supports enricher.llm.auth=apiKey or anthropicWifAuthentik",
        *ANTHROPIC,
        *sets(
            "enricher.llm.auth=openaiWifKubernetes",
            "enricher.serviceAccount.create=true",
            "enricher.llm.workloadIdentity.identityProviderId=i",
            "enricher.llm.workloadIdentity.serviceAccountId=s",
            "enricher.llm.workloadIdentity.tokenAudience=a",
        ),
    )
    c.refuses(
        "batch on openai", "needs enricher.llm.provider=anthropic", *OPENAI, *BATCH
    )
    c.refuses(
        "unknown provider",
        "is not one of openai, anthropic",
        *OPENAI,
        *sets("enricher.llm.provider=gemini"),
    )
    c.refuses(
        "unknown sweep mode",
        "is not one of sync, batch",
        *ANTHROPIC,
        *sets("enricher.llm.batch.sweepMode=nightly"),
    )
    c.refuses(
        "unknown prompt cache",
        "is not one of 1h, 5m, off",
        *ANTHROPIC,
        *sets("enricher.llm.anthropic.promptCache=2h"),
    )


def wif_without(key: str) -> tuple[str, ...]:
    """CLAUDE_WIF with the `--set` for `key` (a values path suffix) left out."""
    pairs = [CLAUDE_WIF[i + 1] for i in range(0, len(CLAUDE_WIF), 2)]
    return sets(*(p for p in pairs if not p.split("=")[0].endswith(key)))


def check_claude_wif(c: Checker) -> None:
    """Check keyless Claude auth: its env, mount and audience, no key, guards."""
    docs = c.docs(
        "claude wif",
        *CLAUDE_WIF,
        *sets("enricher.llm.workloadIdentity.anthropic.workspaceId=wrkspc_1"),
    )
    env = enricher_env(docs)
    expect(
        c,
        "claude wif",
        env,
        {
            "LLM_PROVIDER": "anthropic",
            "LLM_AUTH": "anthropic-wif-authentik",
            "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1",
            "ANTHROPIC_ORGANIZATION_ID": "org-uuid",
            "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1",
            "ANTHROPIC_WORKSPACE_ID": "wrkspc_1",
            "LLM_IDENTITY_TOKEN_FILE": "/var/run/secrets/llm-identity/token",
            "LLM_TOKEN_EXCHANGE_URL": "https://api.anthropic.com/v1/oauth/token",
            "LLM_AUTHENTIK_TOKEN_URL": "https://sso.example.com:8443/application/o/token/",
            "LLM_AUTHENTIK_CLIENT_ID": "ds-enricher-anthropic",
            "LLM_API_KEY": None,
            "OPENAI_IDENTITY_PROVIDER_ID": None,
        },
    )
    deployment = find(docs, "Deployment", ENRICHER)
    pod = as_map(as_map(as_map(deployment.get("spec")).get("template")).get("spec"))
    if pod.get("serviceAccountName") != ENRICHER:
        c.failures.append("claude wif: the pod must use the dedicated ServiceAccount")
    volumes = as_list(pod.get("volumes"))
    sources = [
        as_map(as_map(s).get("serviceAccountToken"))
        for v in volumes
        for s in as_list(as_map(as_map(v).get("projected")).get("sources"))
    ]
    if [s.get("audience") for s in sources] != ["ds-enricher-anthropic"]:
        c.failures.append(
            f"claude wif: want one token for ds-enricher-anthropic, got {sources}"
        )
    secret = find(docs, "Secret", "distant-signal")
    if "llm-api-key" in as_map(secret.get("data")):
        c.failures.append("claude wif: no llm-api-key Secret entry may render")
    ports = internet_ports(
        find(
            c.docs("claude wif egress", *CLAUDE_WIF, *EGRESS), "NetworkPolicy", ENRICHER
        )
    )
    if ports != [443, 8443]:
        c.failures.append(f"claude wif egress: want [443, 8443], got {ports}")
    rule = c.docs(
        "claude wif alert", *CLAUDE_WIF, *sets("metrics.prometheusRule.enabled=true")
    )
    if "DistantSignalEnricherTokenExchangeFailing" not in yaml.safe_dump(rule):
        c.failures.append("claude wif: the token-exchange alert must render")

    for key, needle in [
        ("organizationId", "workloadIdentity.anthropic.organizationId"),
        ("serviceAccountId", "workloadIdentity.anthropic.serviceAccountId"),
        ("federationRuleId", "workloadIdentity.anthropic.federationRuleId"),
        ("authentik.clientId", "workloadIdentity.authentik.clientId"),
        ("serviceAccount.create", "needs a dedicated ServiceAccount"),
    ]:
        c.refuses(f"claude wif without {key}", needle, *wif_without(key))
    c.refuses(
        "claude wif with a key Secret",
        "is keyless: unset enricher.llm.anthropic.existingSecret",
        *CLAUDE_WIF,
        *sets("enricher.llm.anthropic.existingSecret=enricher-anthropic"),
    )
    c.refuses(
        "claude wif on openai",
        "needs enricher.llm.provider=anthropic",
        *wif_without("provider"),
        *OPENAI,
    )


def check_profiles(c: Checker) -> None:
    """Check the profile settings and prompt overrides render and are guarded."""
    expect(
        c,
        "profile defaults",
        enricher_env(c.docs("profile defaults", *OPENAI)),
        {"LLM_PROFILE": None, "LLM_TEMPERATURE": None, "LLM_TOP_P": None},
    )
    docs = c.docs(
        "profile set",
        *ANTHROPIC,
        *sets(
            "enricher.llm.profile=claude-default",
            "enricher.llm.temperature=omit",
            "enricher.llm.topP=0.9",
            "enricher.llm.prompts.configMap=enricher-prompts",
        ),
    )
    expect(
        c,
        "profile set",
        enricher_env(docs),
        {
            "LLM_PROFILE": "claude-default",
            "LLM_TEMPERATURE": "omit",
            "LLM_TOP_P": "0.9",
            "LLM_PROMPTS_DIR": "/etc/enricher/prompts",
        },
    )
    pod = as_map(
        as_map(
            as_map(find(docs, "Deployment", ENRICHER).get("spec")).get("template")
        ).get("spec")
    )
    maps = [
        as_map(as_map(v).get("configMap")).get("name")
        for v in as_list(pod.get("volumes"))
    ]
    if maps != ["enricher-prompts"]:
        c.failures.append(f"profile set: want the prompts ConfigMap volume, got {maps}")
    c.refuses(
        "unknown profile",
        "is not one of openai-gpt-6-luna",
        *OPENAI,
        *sets("enricher.llm.profile=fast"),
    )
    c.refuses(
        "Claude profile on openai",
        "is a Claude profile",
        *OPENAI,
        *sets("enricher.llm.profile=claude-default"),
    )
    c.refuses(
        "OpenAI profile on anthropic",
        "is an OpenAI profile",
        *ANTHROPIC,
        *sets("enricher.llm.profile=openai-default"),
    )
    c.refuses(
        "bad temperature",
        "must be a number",
        *OPENAI,
        *sets("enricher.llm.temperature=warm"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check; print failures."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    args = parser.parse_args(argv)
    c = Checker(cast("str", args.helm))
    check_openai_default(c)
    check_anthropic(c)
    check_egress(c)
    check_refusals(c)
    check_claude_wif(c)
    check_profiles(c)
    for failure in c.failures:
        print(failure)
    if not c.failures:
        print("ok: enricher provider settings render as documented")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
