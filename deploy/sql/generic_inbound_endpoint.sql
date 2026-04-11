-- Generic inbound PJSIP endpoint for host-based routing.
-- This lets Asterisk accept inbound SIP requests via the anonymous endpoint
-- and defer trunk/route matching to the dialplan + resolve_inbound_route(...).

BEGIN;

INSERT INTO ps_aors (id, max_contacts, remove_existing)
VALUES ('anonymous', 10, 'yes')
ON CONFLICT (id) DO UPDATE
SET max_contacts = EXCLUDED.max_contacts,
    remove_existing = EXCLUDED.remove_existing;

INSERT INTO ps_endpoints (
    id,
    aors,
    context,
    disallow,
    allow,
    direct_media,
    rtp_symmetric,
    rewrite_contact,
    force_rport
)
VALUES (
    'anonymous',
    'anonymous',
    'inbound-realtime',
    'all',
    'ulaw',
    'no',
    'yes',
    'yes',
    'yes'
)
ON CONFLICT (id) DO UPDATE
SET aors = EXCLUDED.aors,
    context = EXCLUDED.context,
    disallow = EXCLUDED.disallow,
    allow = EXCLUDED.allow,
    direct_media = EXCLUDED.direct_media,
    rtp_symmetric = EXCLUDED.rtp_symmetric,
    rewrite_contact = EXCLUDED.rewrite_contact,
    force_rport = EXCLUDED.force_rport;

-- Per-trunk identify rows are no longer required in the generic inbound model.
DELETE FROM ps_endpoint_id_ips;

COMMIT;
