from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    stt_engine: str
    languages: list[str]
    backend_url: str | None = None
    trunk_id: str | None = None


@dataclass(frozen=True)
class TenantRule:
    tenant: TenantConfig
    ingress_hosts: set[str]
    called_numbers: set[str]
    auth_users: set[str]


@dataclass(frozen=True)
class TenantResolution:
    tenant: TenantConfig
    match_reason: str


class TenantResolutionError(RuntimeError):
    pass


def _normalize_host(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_number(value: str | None) -> str:
    return (value or "").strip()


def _normalize_auth_user(value: str | None) -> str:
    return (value or "").strip().lower()


def _fallback_default_tenant() -> TenantConfig:
    tenant_id = os.getenv("SIP_TENANT_ID", "default").strip() or "default"
    stt_engine = os.getenv("SIP_STT_ENGINE", "azure").strip() or "azure"
    languages = [x.strip() for x in os.getenv("SIP_LANGUAGES", "en-US").split(",") if x.strip()]
    if not languages:
        languages = ["en-US"]
    return TenantConfig(
        tenant_id=tenant_id,
        stt_engine=stt_engine,
        languages=languages,
        backend_url=None,
        trunk_id=f"{tenant_id}-default-trunk",
    )


def _parse_rules_from_env() -> list[TenantRule]:
    raw = (os.getenv("SIP_TENANT_RULES_JSON") or "").strip()
    if not raw:
        return []

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TenantResolutionError("SIP_TENANT_RULES_JSON is not valid JSON.") from exc

    if not isinstance(rows, list):
        raise TenantResolutionError("SIP_TENANT_RULES_JSON must be a JSON array.")

    rules: list[TenantRule] = []
    seen_hosts: set[str] = set()
    seen_auth_users: set[str] = set()

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TenantResolutionError(f"Tenant rule #{idx} must be an object.")

        tenant_id = str(row.get("tenant_id", "")).strip()
        if not tenant_id:
            raise TenantResolutionError(f"Tenant rule #{idx} is missing tenant_id.")

        stt_engine = str(row.get("stt_engine") or "azure").strip() or "azure"
        languages = row.get("languages") or ["en-US"]
        if isinstance(languages, str):
            languages = [x.strip() for x in languages.split(",") if x.strip()]
        if not isinstance(languages, list) or not all(isinstance(x, str) and x.strip() for x in languages):
            raise TenantResolutionError(f"Tenant rule #{idx} has invalid languages.")

        ingress_hosts = {_normalize_host(x) for x in (row.get("ingress_hosts") or []) if str(x).strip()}
        called_numbers = {_normalize_number(x) for x in (row.get("called_numbers") or []) if str(x).strip()}
        auth_users = {_normalize_auth_user(x) for x in (row.get("auth_users") or []) if str(x).strip()}

        if not ingress_hosts and not called_numbers and not auth_users:
            raise TenantResolutionError(
                f"Tenant rule #{idx} for tenant '{tenant_id}' has no match keys "
                "(ingress_hosts/called_numbers/auth_users)."
            )

        overlap_hosts = ingress_hosts.intersection(seen_hosts)
        if overlap_hosts:
            raise TenantResolutionError(
                f"Duplicate ingress host keys found: {sorted(overlap_hosts)}. "
                "Hosts must be globally unique across tenants."
            )
        seen_hosts.update(ingress_hosts)

        overlap_auth_users = auth_users.intersection(seen_auth_users)
        if overlap_auth_users:
            raise TenantResolutionError(
                f"Duplicate auth_users keys found: {sorted(overlap_auth_users)}. "
                "Auth users must be globally unique across tenants."
            )
        seen_auth_users.update(auth_users)

        tenant = TenantConfig(
            tenant_id=tenant_id,
            stt_engine=stt_engine,
            languages=[x.strip() for x in languages if x.strip()],
            backend_url=(str(row.get("backend_url")).strip() if row.get("backend_url") else None),
            trunk_id=(str(row.get("trunk_id")).strip() if row.get("trunk_id") else None),
        )
        rules.append(
            TenantRule(
                tenant=tenant,
                ingress_hosts=ingress_hosts,
                called_numbers=called_numbers,
                auth_users=auth_users,
            )
        )

    return rules


def resolve_tenant(
    ingress_host: str | None,
    called_number: str | None,
    auth_user: str | None = None,
) -> TenantResolution:
    host = _normalize_host(ingress_host)
    auth = _normalize_auth_user(auth_user)

    default_tenant = _fallback_default_tenant()

    try:
        rules = _parse_rules_from_env()
    except TenantResolutionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback
        raise TenantResolutionError("Failed to parse SIP tenant rules.") from exc

    if not rules:
        return TenantResolution(tenant=default_tenant, match_reason="default:no-rules")

    if not host:
        raise TenantResolutionError("Ingress host is missing. Host is required for tenant resolution.")

    host_matches = [rule for rule in rules if host in rule.ingress_hosts]
    if len(host_matches) > 1:
        raise TenantResolutionError(
            f"Ambiguous tenant resolution for host '{host}': {len(host_matches)} matches."
        )
    if len(host_matches) == 0:
        strict = os.getenv("SIP_STRICT_TENANT_RESOLUTION", "false").strip().lower() in {"1", "true", "yes"}
        if strict:
            raise TenantResolutionError(f"No tenant mapping found for ingress host '{host}'.")
        logger.warning("Tenant resolution fallback to default for unknown ingress host='%s'.", host)
        return TenantResolution(tenant=default_tenant, match_reason="default:fallback-host")

    matched_rule = host_matches[0]

    # Optional security hardening: when auth users are configured for this host,
    # the call must present one of them.
    if matched_rule.auth_users:
        if not auth:
            raise TenantResolutionError(
                f"Missing auth_user for host '{host}'. This trunk requires auth_user validation."
            )
        if auth not in matched_rule.auth_users:
            raise TenantResolutionError(
                f"Invalid auth_user '{auth}' for host '{host}'."
            )
        return TenantResolution(tenant=matched_rule.tenant, match_reason="ingress_host+auth_user")

    return TenantResolution(tenant=matched_rule.tenant, match_reason="ingress_host")


def get_tenant_config(sip_domain_or_number: str) -> dict:
    """
    Backward-compatible helper used by the Twilio websocket path.
    """
    try:
        resolution = resolve_tenant(ingress_host=sip_domain_or_number, called_number=sip_domain_or_number)
    except TenantResolutionError:
        # Legacy Twilio webhook flow should remain permissive even when SIP bridge runs strict mode.
        tenant = _fallback_default_tenant()
        return {
            "tenant_id": tenant.tenant_id,
            "stt_engine": tenant.stt_engine,
            "languages": ",".join(tenant.languages),
        }
    return {
        "tenant_id": resolution.tenant.tenant_id,
        "stt_engine": resolution.tenant.stt_engine,
        "languages": ",".join(resolution.tenant.languages),
    }
