from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.core.config import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine | None:
    db_url = (settings.SIP_CONFIG_DATABASE_URL or "").strip()
    if not db_url:
        return None
    return create_engine(db_url, pool_pre_ping=True, future=True)


def has_database_config() -> bool:
    return bool((settings.SIP_CONFIG_DATABASE_URL or "").strip())


def fetch_effective_source_cidrs_from_db() -> list[str]:
    """
    Returns unique active source CIDRs from trunk_source_cidrs.
    """
    engine = get_engine()
    if engine is None:
        return []

    q = text(
        """
        SELECT cidr::text AS cidr
        FROM trunk_source_cidrs
        WHERE is_active = TRUE
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()

    cidrs = {str(row.get("cidr") or "").strip() for row in rows}
    return sorted(x for x in cidrs if x)
