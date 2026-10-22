-- Narrow read-only access for scripts/curate_incidents.py and the KRL resolver.
-- Requires migrations 001, 006, 008, and 009. No password is created here.
BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_incident_readonly') THEN
    CREATE ROLE moobby_incident_readonly NOLOGIN NOINHERIT
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_incident_readonly_login') THEN
    CREATE ROLE moobby_incident_readonly_login LOGIN INHERIT
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;

-- Refuse to reuse a role with broader attributes or memberships. The login
-- inherits exactly one NOLOGIN role; its password is provisioned separately.
-- Supabase may retain an explicit membership for a role administrator on a
-- managed role. That does not grant privileges to this login; allow only
-- superusers or roles with CREATEROLE, while rejecting ordinary grantees.
DO $$
DECLARE
  read_role oid := 'moobby_incident_readonly'::regrole;
  login_role oid := 'moobby_incident_readonly_login'::regrole;
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'moobby_incident_readonly' AND (rolcanlogin OR rolsuper OR rolcreatedb
      OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit)
  ) THEN
    RAISE EXCEPTION 'moobby_incident_readonly exists with unsafe role attributes';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'moobby_incident_readonly_login' AND (NOT rolcanlogin OR rolsuper OR rolcreatedb
      OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolinherit)
  ) THEN
    RAISE EXCEPTION 'moobby_incident_readonly_login exists with unsafe role attributes';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_auth_members
    WHERE member = login_role AND roleid <> read_role
  ) THEN
    RAISE EXCEPTION 'moobby_incident_readonly_login has unexpected role memberships';
  END IF;
  -- The read-only role itself must not inherit any other role's privileges.
  IF EXISTS (
    SELECT 1 FROM pg_auth_members
    WHERE member = read_role
  ) THEN
    RAISE EXCEPTION 'moobby_incident_readonly must not inherit another role';
  END IF;
  -- Other ordinary roles must not gain access through this role. Supabase's
  -- postgres administrator is not guaranteed to have SUPERUSER, so recognize
  -- explicit role-management capability as well. These memberships do not
  -- make the dedicated login a member of those administrative roles.
  IF EXISTS (
    SELECT 1
    FROM pg_auth_members AS membership
    JOIN pg_roles AS grantee ON grantee.oid = membership.member
    WHERE membership.roleid = read_role
      AND membership.member <> login_role
      AND NOT (grantee.rolsuper OR grantee.rolcreaterole)
  ) THEN
    RAISE EXCEPTION 'moobby_incident_readonly has an unexpected non-admin member';
  END IF;
END $$;

GRANT moobby_incident_readonly TO moobby_incident_readonly_login;

GRANT USAGE ON SCHEMA collection, processing, reference, serving
  TO moobby_incident_readonly;

-- Exact SELECT column sets used by curate_incidents.py and
-- krl_station_resolver.py; no full-table SELECT is granted.
GRANT SELECT (id, raw_content_id, published_at, author_handle, author_display_name)
  ON collection.raw_snapshots TO moobby_incident_readonly;
GRANT SELECT (id, canonical_url)
  ON collection.raw_contents TO moobby_incident_readonly;

GRANT SELECT (
  id, snapshot_id, event_index, event_status, disruption_type, severity,
  affected_entities, event_start_time, event_end_time, uncertainty_status,
  uncertainty_note, explanation, evidence_quotes
) ON processing.events TO moobby_incident_readonly;
GRANT SELECT (event_id, revision, review_status)
  ON processing.event_reviews TO moobby_incident_readonly;

GRANT SELECT (station_id, source_station_code, canonical_name, aliases, latitude, longitude)
  ON reference.krl_stations TO moobby_incident_readonly;

GRANT SELECT (id, processing_event_id, incident_id, link_status, relationship,
              is_primary, created_at)
  ON serving.incident_evidence TO moobby_incident_readonly;

-- RLS policies are additive and apply only to this new role. Candidate rows
-- and their source snapshots are visible; existing roles/policies are unchanged.
CREATE POLICY curation_readonly_events_select ON processing.events
  FOR SELECT TO moobby_incident_readonly
  USING (event_status = 'potential_disruption' AND event_index > 0);

CREATE POLICY curation_readonly_event_reviews_select ON processing.event_reviews
  FOR SELECT TO moobby_incident_readonly
  USING (EXISTS (
    SELECT 1 FROM processing.events AS candidate
    WHERE candidate.id = event_reviews.event_id
      AND candidate.event_status = 'potential_disruption'
      AND candidate.event_index > 0
  ));

CREATE POLICY curation_readonly_snapshots_select ON collection.raw_snapshots
  FOR SELECT TO moobby_incident_readonly
  USING (EXISTS (
    SELECT 1 FROM processing.events AS candidate
    WHERE candidate.snapshot_id = raw_snapshots.id
      AND candidate.event_status = 'potential_disruption'
      AND candidate.event_index > 0
  ));

CREATE POLICY curation_readonly_raw_contents_select ON collection.raw_contents
  FOR SELECT TO moobby_incident_readonly
  USING (EXISTS (
    SELECT 1
    FROM collection.raw_snapshots AS snapshot
    JOIN processing.events AS candidate ON candidate.snapshot_id = snapshot.id
    WHERE snapshot.raw_content_id = raw_contents.id
      AND candidate.event_status = 'potential_disruption'
      AND candidate.event_index > 0
  ));

CREATE POLICY curation_readonly_station_reference_select ON reference.krl_stations
  FOR SELECT TO moobby_incident_readonly USING (true);

CREATE POLICY curation_readonly_incident_evidence_select ON serving.incident_evidence
  FOR SELECT TO moobby_incident_readonly
  USING (EXISTS (
    SELECT 1 FROM processing.events AS candidate
    WHERE candidate.id = incident_evidence.processing_event_id
      AND candidate.event_status = 'potential_disruption'
      AND candidate.event_index > 0
  ));

-- Existing migrations enable RLS on these relations. Reasserting ENABLE is
-- safe and ensures policies cannot be bypassed by an accidental table change.
ALTER TABLE collection.raw_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection.raw_contents ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing.event_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE reference.krl_stations ENABLE ROW LEVEL SECURITY;
ALTER TABLE serving.incident_evidence ENABLE ROW LEVEL SECURITY;

-- Fail deployment if this login has any effective record mutation privilege
-- or can execute a SECURITY DEFINER routine. No password is embedded here.
DO $$
DECLARE
  relation_row record;
  mutation text;
BEGIN
  IF EXISTS (
    SELECT 1
    FROM unnest(ARRAY['collection','processing','reference','serving']) AS schemas(name)
    WHERE has_schema_privilege('moobby_incident_readonly_login', schemas.name, 'CREATE')
  ) THEN
    RAISE EXCEPTION 'read-only login has CREATE privilege on a protected schema';
  END IF;

  FOR relation_row IN
    SELECT relation.oid
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname IN ('collection','processing','reference','serving')
      AND relation.relkind IN ('r','p','v','m','f')
  LOOP
    FOREACH mutation IN ARRAY ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
      IF has_table_privilege('moobby_incident_readonly_login', relation_row.oid, mutation)
         OR (mutation IN ('INSERT','UPDATE') AND
             has_any_column_privilege('moobby_incident_readonly_login', relation_row.oid, mutation)) THEN
        RAISE EXCEPTION 'read-only login has unexpected % privilege on %',
          mutation, relation_row.oid::regclass;
      END IF;
    END LOOP;
  END LOOP;

  IF EXISTS (
    SELECT 1 FROM pg_proc AS routine
    WHERE routine.prosecdef
      AND routine.pronamespace IN (
        'collection'::regnamespace, 'processing'::regnamespace,
        'serving'::regnamespace, 'reference'::regnamespace
      )
      AND has_function_privilege('moobby_incident_readonly_login', routine.oid, 'EXECUTE')
  ) THEN
    RAISE EXCEPTION 'read-only login can execute a SECURITY DEFINER routine';
  END IF;
END $$;

COMMENT ON ROLE moobby_incident_readonly IS
  'Column-scoped SELECT for the incident curation CLI; no mutation or privileged function access.';
COMMENT ON ROLE moobby_incident_readonly_login IS
  'Dedicated Supabase login inheriting only moobby_incident_readonly; password provisioned out of band.';

COMMIT;

-- Manual post-deployment verification (all result sets should show the stated
-- expected values; run as an administrator without selecting credential data):
--
-- 1) Membership: exactly one row, moobby_incident_readonly.
-- SELECT parent.rolname AS inherited_role
-- FROM pg_auth_members AS membership
-- JOIN pg_roles AS parent ON parent.oid = membership.roleid
-- WHERE membership.member = 'moobby_incident_readonly_login'::regrole;
--
-- 2) Required column reads: every listed item should be true.
-- WITH required(relation_name, column_name) AS (VALUES
--   ('collection.raw_snapshots','id'), ('collection.raw_snapshots','raw_content_id'),
--   ('collection.raw_snapshots','published_at'), ('collection.raw_snapshots','author_handle'),
--   ('collection.raw_snapshots','author_display_name'), ('collection.raw_contents','id'),
--   ('collection.raw_contents','canonical_url'), ('processing.events','id'),
--   ('processing.events','snapshot_id'), ('processing.events','event_index'),
--   ('processing.events','event_status'), ('processing.events','disruption_type'),
--   ('processing.events','severity'), ('processing.events','affected_entities'),
--   ('processing.events','event_start_time'), ('processing.events','event_end_time'),
--   ('processing.events','uncertainty_status'), ('processing.events','uncertainty_note'),
--   ('processing.events','explanation'), ('processing.events','evidence_quotes'),
--   ('processing.event_reviews','event_id'), ('processing.event_reviews','revision'),
--   ('processing.event_reviews','review_status'), ('reference.krl_stations','station_id'),
--   ('reference.krl_stations','source_station_code'), ('reference.krl_stations','canonical_name'),
--   ('reference.krl_stations','aliases'), ('reference.krl_stations','latitude'),
--   ('reference.krl_stations','longitude'), ('serving.incident_evidence','id'),
--   ('serving.incident_evidence','processing_event_id'), ('serving.incident_evidence','incident_id'),
--   ('serving.incident_evidence','link_status'), ('serving.incident_evidence','relationship'),
--   ('serving.incident_evidence','is_primary'), ('serving.incident_evidence','created_at')
-- )
-- SELECT relation_name, column_name,
--        has_column_privilege('moobby_incident_readonly_login', relation_name, column_name, 'SELECT') AS can_select
-- FROM required ORDER BY relation_name, column_name;
--
-- 3) Exercise RLS as the login without displaying source/evidence text. Run
-- from an administrator session allowed to SET ROLE, or connect as the login.
-- BEGIN TRANSACTION READ ONLY;
-- SET LOCAL ROLE moobby_incident_readonly_login;
-- SELECT count(*) AS visible_candidates FROM processing.events
--   WHERE event_status = 'potential_disruption' AND event_index > 0;
-- SELECT count(*) AS visible_station_rows FROM reference.krl_stations;
-- SELECT count(*) AS visible_associations FROM serving.incident_evidence;
-- ROLLBACK;
--
-- 4) No effective writes: every row should have all false values.
-- SELECT schema_name,
--   has_schema_privilege('moobby_incident_readonly_login', schema_name, 'CREATE') AS can_create_objects
-- FROM unnest(ARRAY['collection','processing','reference','serving']) AS schemas(schema_name);
--
-- SELECT relation.oid::regclass AS relation_name,
--   has_table_privilege('moobby_incident_readonly_login', relation.oid, 'INSERT')
--     OR has_any_column_privilege('moobby_incident_readonly_login', relation.oid, 'INSERT') AS can_insert,
--   has_table_privilege('moobby_incident_readonly_login', relation.oid, 'UPDATE')
--     OR has_any_column_privilege('moobby_incident_readonly_login', relation.oid, 'UPDATE') AS can_update,
--   has_table_privilege('moobby_incident_readonly_login', relation.oid, 'DELETE') AS can_delete,
--   has_table_privilege('moobby_incident_readonly_login', relation.oid, 'TRUNCATE') AS can_truncate
-- FROM pg_class AS relation JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
-- WHERE namespace.nspname IN ('collection','processing','reference','serving')
--   AND relation.relkind IN ('r','p','v','m','f') ORDER BY relation.oid::regclass::text;
--
-- 5) No privileged function access: expected to return zero rows.
-- SELECT routine.oid::regprocedure AS callable_security_definer
-- FROM pg_proc AS routine
-- WHERE routine.prosecdef
--   AND routine.pronamespace IN ('collection'::regnamespace, 'processing'::regnamespace,
--                                'serving'::regnamespace, 'reference'::regnamespace)
--   AND has_function_privilege('moobby_incident_readonly_login', routine.oid, 'EXECUTE');
