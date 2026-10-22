-- psql integration check for db/011_fix_serving_publication_trigger.sql.
-- Run only with an administrator connection and ON_ERROR_STOP enabled.
-- All test rows and the temporary function replacement are rolled back.
\set ON_ERROR_STOP on

BEGIN;

-- Capture the deployed definition so the post-rollback query can verify that
-- this transaction left the function unchanged.
SELECT md5(pg_get_functiondef(
  'serving.check_publication_after_change()'::regprocedure
)) AS original_function_md5
\gset

-- Use an existing pending revision-1 review. No post text is selected.
\unset test_event_id
\unset test_event_revision
SELECT e.id AS test_event_id, er.revision AS test_event_revision
FROM processing.events AS e
JOIN processing.event_reviews AS er ON er.event_id = e.id
WHERE e.event_status = 'potential_disruption'
  AND er.revision = 1
  AND er.review_status = 'pending'
ORDER BY e.id
LIMIT 1
\gset

\if :{?test_event_id}

-- This is the same corrective definition as migration 011. It is deliberately
-- applied inside this transaction and is undone by the ROLLBACK below.
CREATE OR REPLACE FUNCTION serving.check_publication_after_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, serving, processing
AS $$
DECLARE
  target_incident_id uuid;
BEGIN
  IF TG_TABLE_NAME = 'incidents' THEN
    IF TG_OP = 'DELETE' THEN
      target_incident_id := OLD.id;
    ELSIF TG_OP IN ('INSERT', 'UPDATE') THEN
      target_incident_id := NEW.id;
    ELSE
      RAISE EXCEPTION 'unsupported operation % for serving.%', TG_OP, TG_TABLE_NAME
        USING ERRCODE = '0A000';
    END IF;
  ELSIF TG_TABLE_NAME = 'incident_evidence' THEN
    IF TG_OP = 'UPDATE' AND OLD.incident_id IS DISTINCT FROM NEW.incident_id THEN
      PERFORM serving.assert_incident_evidence_reviewed(OLD.incident_id);
      PERFORM serving.assert_publishable_incident(OLD.incident_id);
    END IF;

    IF TG_OP = 'DELETE' THEN
      target_incident_id := OLD.incident_id;
    ELSIF TG_OP IN ('INSERT', 'UPDATE') THEN
      target_incident_id := NEW.incident_id;
    ELSE
      RAISE EXCEPTION 'unsupported operation % for serving.%', TG_OP, TG_TABLE_NAME
        USING ERRCODE = '0A000';
    END IF;
  ELSIF TG_TABLE_NAME = 'incident_reviews' THEN
    IF TG_OP <> 'INSERT' THEN
      RAISE EXCEPTION 'unsupported operation % for serving.%', TG_OP, TG_TABLE_NAME
        USING ERRCODE = '0A000';
    END IF;
    target_incident_id := NEW.incident_id;
  ELSE
    RAISE EXCEPTION 'unsupported table serving.%', TG_TABLE_NAME
      USING ERRCODE = '0A000';
  END IF;

  PERFORM serving.assert_incident_evidence_reviewed(target_incident_id);
  PERFORM serving.assert_publishable_incident(target_incident_id);
  RETURN NULL;
END;
$$;

SELECT gen_random_uuid() AS test_incident_one \gset
SELECT gen_random_uuid() AS test_incident_two \gset

-- Incident INSERT and UPDATE; both remain drafts and are never approved.
INSERT INTO serving.incidents (id, publication_status)
VALUES (:'test_incident_one'::uuid, 'draft');
INSERT INTO serving.incidents (id, publication_status)
VALUES (:'test_incident_two'::uuid, 'draft');
UPDATE serving.incidents
SET title = 'trigger integration rollback check'
WHERE id = :'test_incident_one'::uuid;

-- Evidence INSERT, then pending incident-review INSERT. A proposed, non-primary
-- link intentionally cannot satisfy publication eligibility.
INSERT INTO serving.incident_evidence
  (incident_id, processing_event_id, event_review_revision,
   relationship, link_status, correlation_rationale)
VALUES
  (:'test_incident_one'::uuid, :'test_event_id'::uuid,
   :'test_event_revision'::integer, 'supports', 'proposed',
   'trigger integration rollback check');

INSERT INTO serving.incident_reviews
  (incident_id, revision, review_status, reviewer_id, review_notes)
VALUES
  (:'test_incident_one'::uuid, 1, 'pending', 'rollback-test',
   'trigger integration rollback check');

-- Flush deferred triggers for the incident and child INSERT/UPDATE records.
SET CONSTRAINTS ALL IMMEDIATE;

-- Evidence UPDATE/reparenting checks both old and new incident IDs. DELETE then
-- exercises OLD.incident_id dispatch. Constraints are immediate from above.
UPDATE serving.incident_evidence
SET incident_id = :'test_incident_two'::uuid
WHERE incident_id = :'test_incident_one'::uuid
  AND processing_event_id = :'test_event_id'::uuid
  AND event_review_revision = :'test_event_revision'::integer;

DELETE FROM serving.incident_evidence
WHERE incident_id = :'test_incident_two'::uuid
  AND processing_event_id = :'test_event_id'::uuid
  AND event_review_revision = :'test_event_revision'::integer;

ROLLBACK;

-- Read-only post-rollback checks: all test rows are absent and the deployed
-- function definition matches the definition captured before replacement.
SELECT
  (SELECT count(*) FROM serving.incidents
   WHERE id IN (:'test_incident_one'::uuid, :'test_incident_two'::uuid)) AS incident_residue,
  (SELECT count(*) FROM serving.incident_evidence
   WHERE incident_id IN (:'test_incident_one'::uuid, :'test_incident_two'::uuid)) AS evidence_residue,
  (SELECT count(*) FROM serving.incident_reviews
   WHERE incident_id IN (:'test_incident_one'::uuid, :'test_incident_two'::uuid)) AS review_residue,
  md5(pg_get_functiondef(
    'serving.check_publication_after_change()'::regprocedure
  )) = :'original_function_md5' AS function_unchanged;

\else
  \echo 'No pending revision-1 potential-disruption event review found; rolling back without applying the function.'
  ROLLBACK;
  \quit 3
\endif
