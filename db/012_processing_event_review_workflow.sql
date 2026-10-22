-- Append-only human review revisions for Orvix processing events.
-- Requires migrations 001, 006, and 009.
BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_processing_reviewer') THEN
    CREATE ROLE moobby_processing_reviewer NOLOGIN NOINHERIT
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_processing_reviewer_login') THEN
    CREATE ROLE moobby_processing_reviewer_login LOGIN INHERIT
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;

DO $$
DECLARE
  reviewer_role oid := 'moobby_processing_reviewer'::regrole;
  reviewer_login oid := 'moobby_processing_reviewer_login'::regrole;
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'moobby_processing_reviewer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
           OR rolbypassrls OR rolinherit)
  ) THEN
    RAISE EXCEPTION 'moobby_processing_reviewer exists with unsafe role attributes';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'moobby_processing_reviewer_login'
      AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
           OR rolbypassrls OR NOT rolinherit)
  ) THEN
    RAISE EXCEPTION 'moobby_processing_reviewer_login exists with unsafe role attributes';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_auth_members
    WHERE member = reviewer_login AND roleid <> reviewer_role
  ) THEN
    RAISE EXCEPTION 'processing reviewer login has unexpected role memberships';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = reviewer_role) THEN
    RAISE EXCEPTION 'processing reviewer role must not inherit other roles';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_auth_members AS membership
    JOIN pg_roles AS member_role ON member_role.oid = membership.member
    WHERE membership.roleid = reviewer_role
      AND membership.member <> reviewer_login
      AND NOT (member_role.rolsuper OR member_role.rolcreaterole)
  ) THEN
    RAISE EXCEPTION 'processing reviewer role has an unexpected non-admin member';
  END IF;
END $$;

GRANT moobby_processing_reviewer TO moobby_processing_reviewer_login;

ALTER TABLE processing.event_reviews
  ADD COLUMN reviewer_id text;

ALTER TABLE processing.event_reviews
  ADD CONSTRAINT processing_event_reviews_reviewer_id_nonblank
  CHECK (reviewer_id IS NULL OR length(btrim(reviewer_id)) > 0) NOT VALID;

COMMENT ON COLUMN processing.event_reviews.reviewer_id IS
  'Authenticated database identity for human revisions; initial importer-created pending revision 1 remains NULL.';

-- Serialize revision allocation per event inside the transaction. Revision 1
-- remains reserved for the Orvix importer's initial pending row. Later rows
-- must come from the dedicated human reviewer login.
CREATE FUNCTION processing.guard_event_review_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, processing
AS $$
DECLARE
  expected_revision integer;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.event_id::text, 8171901));

  SELECT COALESCE(max(review.revision), 0) + 1
    INTO expected_revision
  FROM processing.event_reviews AS review
  WHERE review.event_id = NEW.event_id;

  IF NEW.revision <> expected_revision THEN
    RAISE EXCEPTION 'event review revision must be the next revision (%)', expected_revision
      USING ERRCODE = '23514';
  END IF;

  IF NEW.revision = 1 THEN
    IF NEW.review_status <> 'pending' OR NEW.reviewer_id IS NOT NULL
       OR NEW.review_notes <> '' OR NEW.corrections <> '{}'::jsonb THEN
      RAISE EXCEPTION 'revision 1 must be the initial pending import review'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT pg_has_role(current_user, 'moobby_processing_reviewer', 'member') THEN
    RAISE EXCEPTION 'only the dedicated processing reviewer may append human decisions'
      USING ERRCODE = '42501';
  END IF;
  -- current_user is the effective role and may change with SET ROLE. Attribute
  -- human decisions to the authenticated connection identity instead.
  IF NEW.reviewer_id IS DISTINCT FROM session_user THEN
    RAISE EXCEPTION 'reviewer_id must match the authenticated database login'
      USING ERRCODE = '42501';
  END IF;
  IF NEW.review_status NOT IN ('accepted','corrected','rejected')
     OR NULLIF(btrim(NEW.review_notes), '') IS NULL THEN
    RAISE EXCEPTION 'human revisions require a decision and rationale'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.review_status = 'corrected' AND NEW.corrections = '{}'::jsonb THEN
    RAISE EXCEPTION 'corrected revisions require at least one correction'
      USING ERRCODE = '23514';
  ELSIF NEW.review_status <> 'corrected' AND NEW.corrections <> '{}'::jsonb THEN
    RAISE EXCEPTION 'only corrected revisions may contain corrections'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_object_keys(NEW.corrections) AS correction_keys(key)
    WHERE key NOT IN (
      'disruption_type','disruption_type_evidence','affected_entities',
      'severity','severity_evidence','event_start_time','event_start_time_evidence',
      'event_end_time','event_end_time_evidence','uncertainty_status',
      'uncertainty_note','explanation','evidence_quotes'
    )
  ) THEN
    RAISE EXCEPTION 'corrections contain unsupported processing-event fields'
      USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM processing.events AS event
    WHERE event.id = NEW.event_id
      AND event.event_status = 'potential_disruption'
      AND event.event_index > 0
  ) THEN
    RAISE EXCEPTION 'human reviews are limited to positive-index potential-disruption events'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER processing_event_reviews_insert_guard
  BEFORE INSERT ON processing.event_reviews
  FOR EACH ROW EXECUTE FUNCTION processing.guard_event_review_append();

-- Defense in depth for the append-only contract; ordinary reviewer, worker,
-- and collection-team roles also receive no UPDATE or DELETE grants.
CREATE FUNCTION processing.reject_event_review_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, processing
AS $$
BEGIN
  RAISE EXCEPTION 'processing event review revisions are append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER processing_event_reviews_append_only
  BEFORE UPDATE OR DELETE ON processing.event_reviews
  FOR EACH ROW EXECUTE FUNCTION processing.reject_event_review_mutation();
CREATE TRIGGER processing_event_reviews_no_truncate
  BEFORE TRUNCATE ON processing.event_reviews
  FOR EACH STATEMENT EXECUTE FUNCTION processing.reject_event_review_mutation();

ALTER TABLE processing.event_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection.raw_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection.raw_contents ENABLE ROW LEVEL SECURITY;

-- Start from no direct access for these newly dedicated roles, then add back
-- only the columns and operations needed by the reviewer CLI.
REVOKE ALL ON SCHEMA processing, collection
  FROM moobby_processing_reviewer, moobby_processing_reviewer_login;
REVOKE ALL ON processing.events, processing.event_reviews,
  collection.raw_snapshots, collection.raw_contents
  FROM moobby_processing_reviewer, moobby_processing_reviewer_login;
REVOKE UPDATE, DELETE, TRUNCATE ON processing.event_reviews
  FROM moobby_processing_reviewer, moobby_processing_reviewer_login;

GRANT USAGE ON SCHEMA processing, collection TO moobby_processing_reviewer;
GRANT SELECT (
  id, snapshot_id, event_index, event_status, disruption_type,
  disruption_type_evidence, affected_entities, severity, severity_evidence,
  event_start_time, event_start_time_evidence, event_end_time,
  event_end_time_evidence, uncertainty_status, uncertainty_note,
  explanation, evidence_quotes
) ON processing.events TO moobby_processing_reviewer;
GRANT SELECT (
  event_id, revision, review_status, reviewer_id, review_notes,
  corrections, recorded_at
) ON processing.event_reviews TO moobby_processing_reviewer;
GRANT INSERT (
  event_id, revision, review_status, reviewer_id, review_notes, corrections
) ON processing.event_reviews TO moobby_processing_reviewer;
GRANT SELECT (id, raw_content_id, author_handle, author_display_name, title, published_at, raw_text)
  ON collection.raw_snapshots TO moobby_processing_reviewer;
GRANT SELECT (id, canonical_url)
  ON collection.raw_contents TO moobby_processing_reviewer;

CREATE POLICY processing_events_reviewer_read ON processing.events
  FOR SELECT TO moobby_processing_reviewer
  USING (event_status = 'potential_disruption' AND event_index > 0);
CREATE POLICY processing_event_reviews_reviewer_read ON processing.event_reviews
  FOR SELECT TO moobby_processing_reviewer
  USING (EXISTS (
    SELECT 1 FROM processing.events AS event
    WHERE event.id = event_reviews.event_id
      AND event.event_status = 'potential_disruption'
      AND event.event_index > 0
  ));
CREATE POLICY processing_event_reviews_reviewer_insert ON processing.event_reviews
  FOR INSERT TO moobby_processing_reviewer
  WITH CHECK (
    revision > 1 AND reviewer_id = session_user
    AND review_status IN ('accepted','corrected','rejected')
    AND NULLIF(btrim(review_notes), '') IS NOT NULL
  );
CREATE POLICY processing_snapshots_reviewer_read ON collection.raw_snapshots
  FOR SELECT TO moobby_processing_reviewer
  USING (EXISTS (
    SELECT 1 FROM processing.events AS event
    WHERE event.snapshot_id = raw_snapshots.id
      AND event.event_status = 'potential_disruption'
      AND event.event_index > 0
  ));
CREATE POLICY processing_raw_contents_reviewer_read ON collection.raw_contents
  FOR SELECT TO moobby_processing_reviewer
  USING (EXISTS (
    SELECT 1
    FROM collection.raw_snapshots AS snapshot
    JOIN processing.events AS event ON event.snapshot_id = snapshot.id
    WHERE snapshot.raw_content_id = raw_contents.id
      AND event.event_status = 'potential_disruption'
      AND event.event_index > 0
  ));

REVOKE ALL ON FUNCTION processing.guard_event_review_append() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION processing.reject_event_review_mutation() FROM PUBLIC, anon, authenticated;
COMMENT ON ROLE moobby_processing_reviewer IS
  'Narrow read access to potential processing events and append-only human review insertion.';
COMMENT ON ROLE moobby_processing_reviewer_login IS
  'Dedicated processing-event reviewer login; provision its password out of band.';
COMMENT ON TABLE processing.event_reviews IS
  'Append-only Orvix processing reviews; revision 1 is the initial pending import and later revisions are human decisions.';

-- Refuse to build the active-link uniqueness rule over ambiguous existing data.
DO $$ BEGIN
  IF EXISTS (
    SELECT 1
    FROM serving.incident_evidence
    WHERE link_status IN ('proposed','accepted')
    GROUP BY incident_id, processing_event_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'duplicate active incident evidence links exist; review them before deploying migration 012';
  END IF;
END $$;

CREATE UNIQUE INDEX incident_evidence_one_active_event_per_incident_idx
  ON serving.incident_evidence(incident_id, processing_event_id)
  WHERE link_status IN ('proposed','accepted');

COMMIT;
