from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.core.config import settings


@dataclass(frozen=True)
class DbTenantRuleRow:
    tenant_id: str
    trunk_id: str
    stt_engine: str
    languages: list[str]
    backend_url: str | None
    ingress_hosts: set[str]
    auth_users: set[str]


@lru_cache(maxsize=1)
def get_engine() -> Engine | None:
    db_url = (settings.SIP_CONFIG_DATABASE_URL or "").strip()
    if not db_url:
        return None
    return create_engine(db_url, pool_pre_ping=True, future=True)


def has_database_config() -> bool:
    return bool((settings.SIP_CONFIG_DATABASE_URL or "").strip())


def fetch_active_tenant_rules_from_db() -> list[DbTenantRuleRow]:
    """
    Expected schema (portable SQLite/Postgres style):
    - organizations(id, slug, is_active)
    - inbound_trunks(id, org_id, stt_engine, languages_csv, backend_url, is_active)
    - trunk_ingress_hosts(id, trunk_id, host, is_active)
    - trunk_auth_users(id, trunk_id, auth_user, is_active)
    """
    engine = get_engine()
    if engine is None:
        return []

    q = text(
        """
        SELECT
            o.slug AS tenant_id,
            t.id AS trunk_id,
            COALESCE(t.stt_engine, 'azure') AS stt_engine,
            COALESCE(t.languages_csv, 'en-US') AS languages_csv,
            t.backend_url AS backend_url,
            h.host AS ingress_host,
            a.auth_user AS auth_user
        FROM inbound_trunks t
        INNER JOIN organizations o
            ON o.id = t.org_id
        LEFT JOIN trunk_ingress_hosts h
            ON h.trunk_id = t.id
            AND (h.is_active IS NULL OR h.is_active = TRUE)
        LEFT JOIN trunk_auth_users a
            ON a.trunk_id = t.id
            AND (a.is_active IS NULL OR a.is_active = TRUE)
        WHERE
            (t.is_active IS NULL OR t.is_active = TRUE)
            AND (o.is_active IS NULL OR o.is_active = TRUE)
        """
    )

    grouped: dict[str, dict] = defaultdict(
        lambda: {
            "tenant_id": "",
            "trunk_id": "",
            "stt_engine": "azure",
            "languages": ["en-US"],
            "backend_url": None,
            "ingress_hosts": set(),
            "auth_users": set(),
        }
    )

    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()

    for row in rows:
        trunk_id = str(row.get("trunk_id") or "").strip()
        if not trunk_id:
            continue

        item = grouped[trunk_id]
        item["tenant_id"] = str(row.get("tenant_id") or "").strip()
        item["trunk_id"] = trunk_id
        item["stt_engine"] = str(row.get("stt_engine") or "azure").strip() or "azure"
        raw_languages = str(row.get("languages_csv") or "en-US")
        item["languages"] = [x.strip() for x in raw_languages.split(",") if x.strip()] or ["en-US"]
        backend_url = row.get("backend_url")
        item["backend_url"] = str(backend_url).strip() if backend_url else None

        host = str(row.get("ingress_host") or "").strip().lower()
        if host:
            item["ingress_hosts"].add(host)

        auth_user = str(row.get("auth_user") or "").strip().lower()
        if auth_user:
            item["auth_users"].add(auth_user)

    out: list[DbTenantRuleRow] = []
    for data in grouped.values():
        if not data["tenant_id"]:
            continue
        out.append(
            DbTenantRuleRow(
                tenant_id=data["tenant_id"],
                trunk_id=data["trunk_id"],
                stt_engine=data["stt_engine"],
                languages=data["languages"],
                backend_url=data["backend_url"],
                ingress_hosts=set(data["ingress_hosts"]),
                auth_users=set(data["auth_users"]),
            )
        )
    return out


def fetch_effective_source_cidrs_from_db() -> list[str]:
    """
    Returns unique active source CIDRs from:
    - provider_global_source_cidrs(cidr, is_active)
    - trunk_source_cidrs(cidr, is_active)
    """
    engine = get_engine()
    if engine is None:
        return []

    q = text(
        """
        SELECT cidr
        FROM provider_global_source_cidrs
        WHERE (is_active IS NULL OR is_active = TRUE)
        UNION
        SELECT cidr
        FROM trunk_source_cidrs
        WHERE (is_active IS NULL OR is_active = TRUE)
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()

    cidrs = {str(row.get("cidr") or "").strip() for row in rows}
    return sorted(x for x in cidrs if x)
