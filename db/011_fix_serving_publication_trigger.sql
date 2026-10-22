-- Fix table-specific record-field access in the deferred serving publication trigger.
-- Requires db/009_incident_serving.sql.
BEGIN;

CREATE OR REPLACE FUNCTION serving.check_publication_after_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, serving, processing
AS $$
DECLARE
  target_incident_id uuid;
BEGIN
  -- The incidents table uses id; child tables use incident_id. Keep OLD/NEW
  -- field access inside the matching table branch because trigger records are
  -- table-shaped and do not have the other table's columns.
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
      -- Reparenting evidence affects publication eligibility on both incidents.
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

COMMIT;
