-- Realtime SIP trunk schema (PostgreSQL recommended)
-- Single source of truth for runtime admission + routing decisions.

BEGIN;

CREATE TABLE IF NOT EXISTS organizations (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inbound_trunks (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    stt_engine      TEXT NOT NULL DEFAULT 'azure',
    languages_csv   TEXT NOT NULL DEFAULT 'en-US',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (stt_engine <> ''),
    CHECK (languages_csv <> ''),
    CHECK (position('|' in stt_engine) = 0),
    CHECK (position('|' in languages_csv) = 0)
);

CREATE TABLE IF NOT EXISTS trunk_ingress_hosts (
    id              BIGSERIAL PRIMARY KEY,
    trunk_id        TEXT NOT NULL REFERENCES inbound_trunks(id) ON DELETE CASCADE,
    host            TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (host <> ''),
    CHECK (position('|' in host) = 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_ingress_hosts_active_host
    ON trunk_ingress_hosts (lower(host))
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS trunk_auth_users (
    id              BIGSERIAL PRIMARY KEY,
    trunk_id        TEXT NOT NULL REFERENCES inbound_trunks(id) ON DELETE CASCADE,
    auth_user       TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (auth_user <> ''),
    CHECK (position('|' in auth_user) = 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_auth_users_active_user
    ON trunk_auth_users (lower(auth_user))
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS trunk_source_cidrs (
    id              BIGSERIAL PRIMARY KEY,
    trunk_id        TEXT NOT NULL REFERENCES inbound_trunks(id) ON DELETE CASCADE,
    cidr            CIDR NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trunk_source_cidrs_active
    ON trunk_source_cidrs (trunk_id, cidr)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS routing_rules (
    id                      TEXT PRIMARY KEY,
    trunk_id                TEXT NOT NULL REFERENCES inbound_trunks(id) ON DELETE CASCADE,
    priority                INTEGER NOT NULL DEFAULT 100,
    called_number_pattern   TEXT NOT NULL DEFAULT '.*',
    backend_url             TEXT NOT NULL,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (backend_url ~ '^https://'),
    CHECK (position('|' in backend_url) = 0),
    CHECK (position('|' in called_number_pattern) = 0)
);
CREATE INDEX IF NOT EXISTS ix_routing_rules_active_priority
    ON routing_rules (trunk_id, priority)
    WHERE is_active = TRUE;

-- -------------------------------------------------------------------------
-- Asterisk Sorcery realtime tables (res_pjsip + realtime)
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ps_endpoints (
    id                          VARCHAR(80) PRIMARY KEY,
    transport                   VARCHAR(80),
    aors                        VARCHAR(200),
    auth                        VARCHAR(80),
    context                     VARCHAR(80) NOT NULL DEFAULT 'inbound-realtime',
    disallow                    VARCHAR(200) NOT NULL DEFAULT 'all',
    allow                       VARCHAR(200) NOT NULL DEFAULT 'ulaw',
    direct_media                VARCHAR(10)  NOT NULL DEFAULT 'no',
    rtp_symmetric               VARCHAR(10)  NOT NULL DEFAULT 'yes',
    rewrite_contact             VARCHAR(10)  NOT NULL DEFAULT 'yes',
    force_rport                 VARCHAR(10)  NOT NULL DEFAULT 'yes',
    media_encryption            VARCHAR(20),
    media_use_received_transport VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS ps_aors (
    id              VARCHAR(80) PRIMARY KEY,
    max_contacts    INTEGER NOT NULL DEFAULT 10,
    remove_existing VARCHAR(10) NOT NULL DEFAULT 'yes'
);

CREATE TABLE IF NOT EXISTS ps_auths (
    id              VARCHAR(80) PRIMARY KEY,
    auth_type       VARCHAR(20) NOT NULL DEFAULT 'userpass',
    username        VARCHAR(80),
    password        VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS ps_endpoint_id_ips (
    id              BIGSERIAL PRIMARY KEY,
    endpoint        VARCHAR(80) NOT NULL REFERENCES ps_endpoints(id) ON DELETE CASCADE,
    match           VARCHAR(64),
    match_header    VARCHAR(255),
    srv_lookups     VARCHAR(10) NOT NULL DEFAULT 'no'
);
CREATE INDEX IF NOT EXISTS ix_ps_endpoint_id_ips_endpoint
    ON ps_endpoint_id_ips(endpoint);

CREATE TABLE IF NOT EXISTS ps_domain_aliases (
    id              VARCHAR(80) PRIMARY KEY,
    domain_alias    VARCHAR(80) NOT NULL
);

-- -------------------------------------------------------------------------
-- Runtime decision function consumed by func_odbc + dialplan precheck
-- Return format (caret-delimited):
-- decision^trunk_id^route_id^backend_url^tenant_id^stt_engine^languages_csv^reject_reason^reject_cause
-- -------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION resolve_inbound_route(
    p_ingress_host  TEXT,
    p_called_number TEXT,
    p_auth_user     TEXT,
    p_source_ip     TEXT
) RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    v_host          TEXT := lower(trim(COALESCE(p_ingress_host, '')));
    v_called        TEXT := trim(COALESCE(p_called_number, ''));
    v_auth          TEXT := lower(trim(COALESCE(p_auth_user, '')));
    v_source_inet   INET;

    v_trunk_id      TEXT;
    v_tenant_id     TEXT;
    v_stt_engine    TEXT;
    v_languages     TEXT;

    v_route_id      TEXT;
    v_backend_url   TEXT;

    v_host_count    INTEGER;
    v_auth_count    INTEGER;
    v_cidr_count    INTEGER;
    v_auth_ok       BOOLEAN;
    v_cidr_ok       BOOLEAN;
    v_route_count   INTEGER;
    v_delim         TEXT := '^';

    v_reason        TEXT := '';
    v_cause         INTEGER := NULL;
BEGIN
    IF v_host = '' THEN
        RETURN array_to_string(ARRAY['reject','','','','','','','missing_host','21'], v_delim);
    END IF;

    IF trim(COALESCE(p_source_ip, '')) <> '' THEN
        BEGIN
            v_source_inet := trim(COALESCE(p_source_ip, ''))::inet;
        EXCEPTION WHEN others THEN
            RETURN array_to_string(ARRAY['reject','','','','','','','invalid_source_ip','21'], v_delim);
        END;
    END IF;

    SELECT COUNT(*)
      INTO v_host_count
      FROM trunk_ingress_hosts h
      JOIN inbound_trunks t ON t.id = h.trunk_id
      JOIN organizations o ON o.id = t.org_id
     WHERE h.is_active = TRUE
       AND t.is_active = TRUE
       AND o.is_active = TRUE
       AND lower(h.host) = v_host;

    IF v_host_count = 0 THEN
        RETURN array_to_string(ARRAY['reject','','','','','','','unknown_host','1'], v_delim);
    ELSIF v_host_count > 1 THEN
        RETURN array_to_string(ARRAY['reject','','','','','','','ambiguous_host','21'], v_delim);
    END IF;

    SELECT t.id, o.slug, t.stt_engine, t.languages_csv
      INTO v_trunk_id, v_tenant_id, v_stt_engine, v_languages
      FROM trunk_ingress_hosts h
      JOIN inbound_trunks t ON t.id = h.trunk_id
      JOIN organizations o ON o.id = t.org_id
     WHERE h.is_active = TRUE
       AND t.is_active = TRUE
       AND o.is_active = TRUE
       AND lower(h.host) = v_host
     LIMIT 1;

    SELECT COUNT(*)
      INTO v_cidr_count
      FROM trunk_source_cidrs c
     WHERE c.trunk_id = v_trunk_id
       AND c.is_active = TRUE;

    IF v_cidr_count > 0 THEN
        IF v_source_inet IS NULL THEN
            RETURN array_to_string(ARRAY['reject', COALESCE(v_trunk_id, ''), '', '', '', '', '', 'missing_source_ip', '21'], v_delim);
        END IF;

        SELECT EXISTS (
            SELECT 1
              FROM trunk_source_cidrs c
             WHERE c.trunk_id = v_trunk_id
               AND c.is_active = TRUE
               AND v_source_inet <<= c.cidr
        ) INTO v_cidr_ok;

        IF NOT COALESCE(v_cidr_ok, FALSE) THEN
            RETURN array_to_string(ARRAY['reject', COALESCE(v_trunk_id, ''), '', '', '', '', '', 'source_ip_not_allowed', '21'], v_delim);
        END IF;
    END IF;

    SELECT COUNT(*)
      INTO v_auth_count
      FROM trunk_auth_users a
     WHERE a.trunk_id = v_trunk_id
       AND a.is_active = TRUE;

    IF v_auth_count > 0 THEN
        SELECT EXISTS (
            SELECT 1
              FROM trunk_auth_users a
             WHERE a.trunk_id = v_trunk_id
               AND a.is_active = TRUE
               AND lower(a.auth_user) = v_auth
        ) INTO v_auth_ok;

        IF NOT COALESCE(v_auth_ok, FALSE) THEN
            RETURN array_to_string(ARRAY['reject', COALESCE(v_trunk_id, ''), '', '', '', '', '', 'auth_user_not_allowed', '21'], v_delim);
        END IF;
    END IF;

    SELECT r.id, r.backend_url
      INTO v_route_id, v_backend_url
      FROM routing_rules r
     WHERE r.trunk_id = v_trunk_id
       AND r.is_active = TRUE
       AND v_called ~ r.called_number_pattern
     ORDER BY r.priority ASC, r.id ASC
     LIMIT 1;

    IF v_route_id IS NULL OR v_backend_url IS NULL OR v_backend_url = '' THEN
        SELECT COUNT(*)
          INTO v_route_count
          FROM routing_rules r
         WHERE r.trunk_id = v_trunk_id
           AND r.is_active = TRUE;

        IF v_route_count = 1 THEN
            SELECT r.id, r.backend_url
              INTO v_route_id, v_backend_url
              FROM routing_rules r
             WHERE r.trunk_id = v_trunk_id
               AND r.is_active = TRUE
             ORDER BY r.priority ASC, r.id ASC
             LIMIT 1;
        END IF;
    END IF;

    IF v_route_id IS NULL OR v_backend_url IS NULL OR v_backend_url = '' THEN
        RETURN array_to_string(ARRAY['reject', COALESCE(v_trunk_id, ''), '', '', '', '', '', 'no_matching_route', '1'], v_delim);
    END IF;

    RETURN array_to_string(ARRAY[
        'allow',
        COALESCE(v_trunk_id, ''),
        COALESCE(v_route_id, ''),
        COALESCE(v_backend_url, ''),
        COALESCE(v_tenant_id, ''),
        COALESCE(v_stt_engine, 'azure'),
        COALESCE(v_languages, 'en-US'),
        COALESCE(v_reason, ''),
        COALESCE(v_cause::text, '')
    ], v_delim);
END;
$$;

COMMIT;
