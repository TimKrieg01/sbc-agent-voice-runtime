-- Config DB schema for external tenant/trunk control plane.
-- Compatible with PostgreSQL (preferred) and SQLite for local testing.

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbound_trunks (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL,
    stt_engine TEXT NOT NULL DEFAULT 'azure',
    languages_csv TEXT NOT NULL DEFAULT 'en-US',
    backend_url TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trunk_ingress_hosts (
    id TEXT PRIMARY KEY,
    trunk_id TEXT NOT NULL REFERENCES inbound_trunks(id),
    host TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_ingress_hosts_active_host
    ON trunk_ingress_hosts(host)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS trunk_auth_users (
    id TEXT PRIMARY KEY,
    trunk_id TEXT NOT NULL REFERENCES inbound_trunks(id),
    auth_user TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_auth_users_active_user
    ON trunk_auth_users(auth_user)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS routing_rules (
    id TEXT PRIMARY KEY,
    trunk_id TEXT NOT NULL REFERENCES inbound_trunks(id),
    pattern TEXT NOT NULL,
    backend_url TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Global + tenant CIDRs for NSG sync automation.
CREATE TABLE IF NOT EXISTS provider_global_source_cidrs (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    cidr TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_global_source_cidrs_active
    ON provider_global_source_cidrs(provider, cidr)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS trunk_source_cidrs (
    id TEXT PRIMARY KEY,
    trunk_id TEXT NOT NULL REFERENCES inbound_trunks(id),
    cidr TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_source_cidrs_active
    ON trunk_source_cidrs(trunk_id, cidr)
    WHERE is_active = TRUE;
