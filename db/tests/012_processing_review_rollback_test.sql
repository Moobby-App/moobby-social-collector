-- Supplement to db/012_processing_event_review_workflow.sql.
-- Run after migration 012's BEGIN/COMMIT lines are stripped, in the same
-- explicit transaction. The final ROLLBACK removes the migration and test rows.
\set ON_ERROR_STOP on

\unset test_event_id
SELECT e.id AS test_event_id
FROM processing.events AS e
JOIN processing.event_reviews AS initial_review
  ON initial_review.event_id = e.id AND initial_review.revision = 1
WHERE e.event_status = 'potential_disruption'
  AND e.event_index > 0
  AND initial_review.review_status = 'pending'
ORDER BY e.id
LIMIT 1
\gset

\if :{?test_event_id}

SELECT gen_random_uuid() AS test_incident_id \gset
SELECT set_config('stage812.test_event_id', :'test_event_id', true);
SELECT set_config('stage812.test_incident_id', :'test_incident_id', true);

-- Demonstrate that effective role changes do not change reviewer attribution.
SET LOCAL ROLE moobby_processing_reviewer;
DO $$
DECLARE
  target_event uuid := current_setting('stage812.test_event_id')::uuid;
  next_revision integer;
BEGIN
  IF current_user = session_user THEN
    RAISE EXCEPTION 'test requires SET ROLE to change current_user';
  END IF;

  SELECT COALESCE(max(revision), 0) + 1 INTO next_revision
  FROM processing.event_reviews WHERE event_id = target_event;

  INSERT INTO processing.event_reviews
    (event_id, revision, review_status, reviewer_id, review_notes, corrections)
  VALUES
    (target_event, next_revision, 'accepted', session_user,
     'rollback-only reviewer identity test', '{}'::jsonb);
  PERFORM set_config('stage812.accepted_revision', next_revision::text, true);

  INSERT INTO processing.event_reviews
    (event_id, revision, review_status, reviewer_id, review_notes, corrections)
  VALUES
    (target_event, next_revision + 1, 'accepted', session_user,
     'rollback-only alternate pinned revision', '{}'::jsonb);
  PERFORM set_config('stage812.second_revision', (next_revision + 1)::text, true);

  BEGIN
    INSERT INTO processing.event_reviews
      (event_id, revision, review_status, reviewer_id, review_notes, corrections)
    VALUES
      (target_event, next_revision + 2, 'accepted', current_user,
       'must be rejected because current_user is a switched role', '{}'::jsonb);
    RAISE EXCEPTION 'reviewer identity spoof was unexpectedly accepted';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END;
$$;

RESET ROLE;

-- The worker's existing INSERT omits reviewer_id and remains valid for the
-- initial pending revision (reviewer_id stays NULL by default).
DO $$
DECLARE
  target_event uuid := current_setting('stage812.test_event_id')::uuid;
  latest_revision integer;
BEGIN
  SELECT max(revision) INTO latest_revision
  FROM processing.event_reviews WHERE event_id = target_event;
  IF EXISTS (
    SELECT 1 FROM processing.event_reviews
    WHERE event_id = target_event AND revision = 1
      AND (review_status <> 'pending' OR reviewer_id IS NOT NULL
           OR review_notes <> '' OR corrections <> '{}'::jsonb)
  ) THEN
    RAISE EXCEPTION 'importer-created revision 1 is incompatible';
  END IF;
  IF latest_revision < 3 THEN
    RAISE EXCEPTION 'human review test revisions were not recorded';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM processing.event_reviews
    WHERE event_id = target_event
      AND revision = current_setting('stage812.accepted_revision')::integer
      AND reviewer_id = session_user
      AND review_status = 'accepted'
  ) THEN
    RAISE EXCEPTION 'reviewer attribution did not preserve session_user';
  END IF;
END;
$$;

INSERT INTO serving.incidents (id, publication_status)
VALUES (current_setting('stage812.test_incident_id')::uuid, 'draft');

INSERT INTO serving.incident_evidence
  (incident_id, processing_event_id, event_review_revision,
   relationship, link_status, correlation_rationale)
VALUES
  (current_setting('stage812.test_incident_id')::uuid,
   current_setting('stage812.test_event_id')::uuid,
   current_setting('stage812.accepted_revision')::integer,
   'supports', 'proposed', 'rollback-only active evidence uniqueness test');

-- The same event/incident cannot have a second proposed/accepted association,
-- even when it pins another processing review revision.
DO $$
BEGIN
  BEGIN
    INSERT INTO serving.incident_evidence
      (incident_id, processing_event_id, event_review_revision,
       relationship, link_status, correlation_rationale)
    VALUES
      (current_setting('stage812.test_incident_id')::uuid,
       current_setting('stage812.test_event_id')::uuid,
       current_setting('stage812.second_revision')::integer,
       'supports', 'proposed', 'must fail under active-link uniqueness');
    RAISE EXCEPTION 'duplicate active evidence association was unexpectedly accepted';
  EXCEPTION WHEN unique_violation THEN
    NULL;
  END;
END;
$$;

SET CONSTRAINTS ALL IMMEDIATE;
ROLLBACK;

-- These checks should report false/zero after the transaction rollback.
SELECT
  EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'processing' AND table_name = 'event_reviews'
      AND column_name = 'reviewer_id'
  ) AS reviewer_column_remains,
  to_regclass('serving.incident_evidence_one_active_event_per_incident_idx') IS NOT NULL
    AS active_evidence_index_remains,
  (SELECT count(*) FROM serving.incidents
   WHERE id = :'test_incident_id'::uuid) AS test_incident_residue;

\else
  \echo 'No eligible pending revision-1 potential-disruption event found; stopping.'
  ROLLBACK;
  \quit 3
\endif
