-- Private, reviewable incident serving foundation.
-- Requires migrations 001, 006, and 008.
BEGIN;

CREATE SCHEMA serving;
REVOKE ALL ON SCHEMA serving FROM PUBLIC, anon, authenticated;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_incident_curator') THEN
    CREATE ROLE moobby_incident_curator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_incident_gateway') THEN
    CREATE ROLE moobby_incident_gateway NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA serving TO moobby_incident_curator, moobby_incident_gateway;

CREATE TABLE serving.incidents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text,
  -- Canonical internal categories map to processing.events.disruption_type;
  -- a gateway adapter must map these explicitly to the actual app enum.
  category text CHECK (category IS NULL OR category IN (
    'delay','service_suspension','route_change','signal_or_power_issue',
    'infrastructure_issue','accident_or_safety_issue','overcrowding',
    'flooding_or_weather','other','unknown'
  )),
  location_name text,
  location_station_id text REFERENCES reference.krl_stations(station_id) ON DELETE RESTRICT,
  latitude numeric(9,6),
  longitude numeric(9,6),
  event_start_at timestamptz,
  event_end_at timestamptz,
  activity_status text NOT NULL DEFAULT 'unknown'
    CHECK (activity_status IN ('unknown','active','resolved')),
  publication_status text NOT NULL DEFAULT 'draft'
    CHECK (publication_status IN ('draft','approved','withdrawn')),
  withdrawn_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
  CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
  CHECK ((latitude IS NULL) = (longitude IS NULL)),
  CHECK (event_start_at IS NULL OR event_end_at IS NULL OR event_end_at >= event_start_at),
  CHECK ((publication_status = 'withdrawn') = (withdrawn_at IS NOT NULL)),
  CHECK (publication_status <> 'approved' OR
    (NULLIF(btrim(title), '') IS NOT NULL AND category IS NOT NULL))
);

CREATE TABLE serving.incident_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES serving.incidents(id) ON DELETE RESTRICT,
  processing_event_id uuid NOT NULL
    REFERENCES processing.events(id) ON DELETE RESTRICT,
  event_review_revision integer NOT NULL CHECK (event_review_revision > 0),
  relationship text NOT NULL CHECK (relationship IN ('supports','contradicts')),
  link_status text NOT NULL CHECK (link_status IN ('proposed','accepted','rejected')),
  is_primary boolean NOT NULL DEFAULT false,
  correlation_rationale text NOT NULL DEFAULT '',
  reviewed_by text,
  reviewed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (processing_event_id, event_review_revision)
    REFERENCES processing.event_reviews(event_id, revision) ON DELETE RESTRICT,
  UNIQUE (incident_id, processing_event_id, event_review_revision),
  CHECK (NOT is_primary OR
    (link_status = 'accepted' AND relationship = 'supports'
      AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
  CHECK (link_status <> 'accepted' OR
    (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);

-- One event can support at most one currently accepted incident correlation.
CREATE UNIQUE INDEX incident_evidence_one_accepted_incident_per_event_idx
  ON serving.incident_evidence(processing_event_id)
  WHERE link_status = 'accepted';
-- A serving incident can have zero or one primary evidence row.
CREATE UNIQUE INDEX incident_evidence_one_primary_per_incident_idx
  ON serving.incident_evidence(incident_id)
  WHERE is_primary;
CREATE INDEX incident_evidence_incident_idx
  ON serving.incident_evidence(incident_id, created_at, id);
CREATE INDEX incident_evidence_event_idx
  ON serving.incident_evidence(processing_event_id);

CREATE TABLE serving.incident_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES serving.incidents(id) ON DELETE RESTRICT,
  revision integer NOT NULL CHECK (revision > 0),
  review_status text NOT NULL CHECK (review_status IN ('pending','accepted','rejected')),
  reviewer_id text NOT NULL,
  -- Freeze the exact title/category accepted by this review revision.
  reviewed_title text,
  reviewed_category text,
  review_notes text NOT NULL DEFAULT '',
  changes jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(changes) = 'object'),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, revision),
  CHECK (review_status <> 'accepted' OR
    (NULLIF(btrim(reviewed_title), '') IS NOT NULL AND reviewed_category IS NOT NULL))
);
CREATE INDEX incident_reviews_latest_idx
  ON serving.incident_reviews(incident_id, revision DESC);

CREATE TABLE serving.incident_verifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES serving.incidents(id) ON DELETE RESTRICT,
  revision integer NOT NULL CHECK (revision > 0),
  -- No row means unverified; records capture a specific factual assessment.
  verification_status text NOT NULL
    CHECK (verification_status IN ('corroborated','disputed','inconclusive')),
  verifier_id text NOT NULL,
  source_name text NOT NULL,
  source_url text,
  evidence_summary text NOT NULL,
  checked_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, revision),
  CHECK (source_url IS NULL OR source_url ~ '^https://')
);
CREATE INDEX incident_verifications_latest_idx
  ON serving.incident_verifications(incident_id, revision DESC);

-- Audit rows cannot be edited or removed after recording.
CREATE FUNCTION serving.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, serving
AS $$
BEGIN
  RAISE EXCEPTION 'incident audit records are append-only'
    USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER incident_reviews_append_only
  BEFORE UPDATE OR DELETE ON serving.incident_reviews
  FOR EACH ROW EXECUTE FUNCTION serving.reject_audit_mutation();
CREATE TRIGGER incident_reviews_no_truncate
  BEFORE TRUNCATE ON serving.incident_reviews
  FOR EACH STATEMENT EXECUTE FUNCTION serving.reject_audit_mutation();
CREATE TRIGGER incident_verifications_append_only
  BEFORE UPDATE OR DELETE ON serving.incident_verifications
  FOR EACH ROW EXECUTE FUNCTION serving.reject_audit_mutation();
CREATE TRIGGER incident_verifications_no_truncate
  BEFORE TRUNCATE ON serving.incident_verifications
  FOR EACH STATEMENT EXECUTE FUNCTION serving.reject_audit_mutation();

CREATE FUNCTION serving.reject_incident_id_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, serving
AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id THEN
    RAISE EXCEPTION 'incident id is immutable' USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER incidents_id_immutable
  BEFORE UPDATE OF id ON serving.incidents
  FOR EACH ROW EXECUTE FUNCTION serving.reject_incident_id_change();

-- Cross-table publication rule: an approved incident must have a latest
-- accepted human review and one accepted primary link to a potential event,
-- pinned to an accepted/corrected processing review revision.
CREATE FUNCTION serving.assert_publishable_incident(p_incident_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, serving, processing
AS $$
DECLARE
  current_publication_status text;
  current_title text;
  current_category text;
BEGIN
  SELECT i.publication_status, i.title, i.category
    INTO current_publication_status, current_title, current_category
  FROM serving.incidents AS i
  WHERE i.id = p_incident_id;

  IF NOT FOUND OR current_publication_status <> 'approved' THEN
    RETURN;
  END IF;

  IF NULLIF(btrim(current_title), '') IS NULL OR current_category IS NULL THEN
    RAISE EXCEPTION 'approved incident requires a reviewed title and category'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM serving.incident_reviews AS ir
    WHERE ir.incident_id = p_incident_id
      AND ir.revision = (
        SELECT max(ir_latest.revision)
        FROM serving.incident_reviews AS ir_latest
        WHERE ir_latest.incident_id = p_incident_id
      )
      AND ir.review_status = 'accepted'
      AND ir.reviewed_title = current_title
      AND ir.reviewed_category = current_category
  ) THEN
    RAISE EXCEPTION 'approved incident requires a latest accepted human review'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM serving.incident_evidence AS ie
    JOIN processing.events AS pe ON pe.id = ie.processing_event_id
    JOIN processing.event_reviews AS per
      ON per.event_id = ie.processing_event_id
     AND per.revision = ie.event_review_revision
    WHERE ie.incident_id = p_incident_id
      AND ie.is_primary
      AND ie.link_status = 'accepted'
      AND ie.relationship = 'supports'
      AND pe.event_status = 'potential_disruption'
      AND per.review_status IN ('accepted','corrected')
  ) THEN
    RAISE EXCEPTION 'approved incident requires accepted primary evidence'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

CREATE FUNCTION serving.assert_incident_evidence_reviewed(p_incident_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, serving, processing
AS $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM serving.incident_evidence AS ie
    JOIN processing.events AS pe ON pe.id = ie.processing_event_id
    JOIN processing.event_reviews AS per
      ON per.event_id = ie.processing_event_id
     AND per.revision = ie.event_review_revision
    WHERE ie.incident_id = p_incident_id
      AND (
        (ie.link_status = 'accepted' AND per.review_status NOT IN ('accepted','corrected'))
        OR (ie.is_primary AND (
          ie.link_status <> 'accepted'
          OR ie.relationship <> 'supports'
          OR pe.event_status <> 'potential_disruption'
          OR per.review_status NOT IN ('accepted','corrected')
        ))
      )
  ) THEN
    RAISE EXCEPTION 'accepted evidence must pin an accepted processing review; primary evidence must support a potential disruption'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

CREATE FUNCTION serving.check_publication_after_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, serving, processing
AS $$
DECLARE target_incident_id uuid;
BEGIN
  IF TG_OP = 'UPDATE' AND TG_TABLE_NAME <> 'incidents'
     AND OLD.incident_id IS DISTINCT FROM NEW.incident_id THEN
    PERFORM serving.assert_incident_evidence_reviewed(OLD.incident_id);
    PERFORM serving.assert_publishable_incident(OLD.incident_id);
  END IF;
  IF TG_TABLE_NAME = 'incidents' THEN
    IF TG_OP = 'DELETE' THEN target_incident_id := OLD.id;
    ELSE target_incident_id := NEW.id;
    END IF;
  ELSE
    IF TG_OP = 'DELETE' THEN target_incident_id := OLD.incident_id;
    ELSE target_incident_id := NEW.incident_id;
    END IF;
  END IF;
  PERFORM serving.assert_incident_evidence_reviewed(target_incident_id);
  PERFORM serving.assert_publishable_incident(target_incident_id);
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER incidents_publication_requirements
  AFTER INSERT OR UPDATE ON serving.incidents
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION serving.check_publication_after_change();
CREATE CONSTRAINT TRIGGER evidence_publication_requirements
  AFTER INSERT OR UPDATE OR DELETE ON serving.incident_evidence
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION serving.check_publication_after_change();
CREATE CONSTRAINT TRIGGER reviews_publication_requirements
  AFTER INSERT ON serving.incident_reviews
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION serving.check_publication_after_change();

REVOKE ALL ON FUNCTION serving.reject_audit_mutation() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION serving.reject_incident_id_change() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION serving.assert_publishable_incident(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION serving.assert_incident_evidence_reviewed(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION serving.check_publication_after_change() FROM PUBLIC, anon, authenticated;

ALTER TABLE serving.incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE serving.incident_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE serving.incident_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE serving.incident_verifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY incidents_curator_read ON serving.incidents
  FOR SELECT TO moobby_incident_curator USING (true);
CREATE POLICY incidents_curator_insert ON serving.incidents
  FOR INSERT TO moobby_incident_curator WITH CHECK (true);
CREATE POLICY incidents_curator_update ON serving.incidents
  FOR UPDATE TO moobby_incident_curator USING (true) WITH CHECK (true);
CREATE POLICY evidence_curator_read ON serving.incident_evidence
  FOR SELECT TO moobby_incident_curator USING (true);
CREATE POLICY evidence_curator_insert ON serving.incident_evidence
  FOR INSERT TO moobby_incident_curator WITH CHECK (true);
CREATE POLICY evidence_curator_update ON serving.incident_evidence
  FOR UPDATE TO moobby_incident_curator USING (true) WITH CHECK (true);
CREATE POLICY reviews_curator_read ON serving.incident_reviews
  FOR SELECT TO moobby_incident_curator USING (true);
CREATE POLICY reviews_curator_insert ON serving.incident_reviews
  FOR INSERT TO moobby_incident_curator WITH CHECK (true);
CREATE POLICY verifications_curator_read ON serving.incident_verifications
  FOR SELECT TO moobby_incident_curator USING (true);
CREATE POLICY verifications_curator_insert ON serving.incident_verifications
  FOR INSERT TO moobby_incident_curator WITH CHECK (true);

REVOKE ALL ON serving.incidents, serving.incident_evidence,
  serving.incident_reviews, serving.incident_verifications
  FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON serving.incidents, serving.incident_evidence
  TO moobby_incident_curator;
GRANT SELECT, INSERT ON serving.incident_reviews, serving.incident_verifications
  TO moobby_incident_curator;
-- Curators cannot delete incident or evidence history; audit tables are insert-only.
REVOKE UPDATE, DELETE ON serving.incident_reviews, serving.incident_verifications
  FROM PUBLIC, anon, authenticated, moobby_incident_curator;
REVOKE DELETE ON serving.incidents, serving.incident_evidence
  FROM PUBLIC, anon, authenticated, moobby_incident_curator;

-- A narrow future gateway projection: no raw text, quotes, or internal evidence.
-- sourceName is the author identity on the primary source snapshot; publishedAt
-- is that source's post timestamp. Both may be NULL when unavailable.
CREATE VIEW serving.gateway_corridor_incidents
WITH (security_barrier = true)
AS
SELECT
  i.id AS id,
  i.title AS title,
  i.category AS category,
  i.location_name AS "locationName",
  i.latitude AS latitude,
  i.longitude AS longitude,
  nearest_station.station_id AS "nearestStationId",
  nearest_station.canonical_name AS "nearestStationName",
  nearest_station.distance_to_station_km AS "distanceToStationKm",
  snapshot.published_at AS "publishedAt",
  COALESCE(snapshot.author_display_name, snapshot.author_handle) AS "sourceName",
  COALESCE(verification.verification_status, 'unverified') AS "verificationStatus"
FROM serving.incidents AS i
JOIN serving.incident_evidence AS primary_evidence
  ON primary_evidence.incident_id = i.id
 AND primary_evidence.is_primary
 AND primary_evidence.link_status = 'accepted'
 AND primary_evidence.relationship = 'supports'
JOIN processing.events AS event
  ON event.id = primary_evidence.processing_event_id
 AND event.event_status = 'potential_disruption'
JOIN processing.event_reviews AS event_review
  ON event_review.event_id = primary_evidence.processing_event_id
 AND event_review.revision = primary_evidence.event_review_revision
 AND event_review.review_status IN ('accepted','corrected')
JOIN collection.raw_snapshots AS snapshot ON snapshot.id = event.snapshot_id
LEFT JOIN LATERAL (
  SELECT
    candidate.station_id,
    candidate.canonical_name,
    round(candidate.distance_km::numeric, 3) AS distance_to_station_km
  FROM (
    SELECT
      station.station_id,
      station.canonical_name,
      2.0 * 6371.0088 * asin(sqrt(least(1.0, greatest(0.0,
        power(sin(radians((station.latitude::double precision - i.latitude::double precision) / 2.0)), 2)
        + cos(radians(i.latitude::double precision))
          * cos(radians(station.latitude::double precision))
          * power(sin(radians((station.longitude::double precision - i.longitude::double precision) / 2.0)), 2)
      )))) AS distance_km
    FROM reference.krl_stations AS station
    WHERE i.latitude IS NOT NULL AND i.longitude IS NOT NULL
  ) AS candidate
  ORDER BY candidate.distance_km, candidate.station_id
  LIMIT 1
) AS nearest_station ON true
LEFT JOIN LATERAL (
  SELECT v.verification_status
  FROM serving.incident_verifications AS v
  WHERE v.incident_id = i.id
  ORDER BY v.revision DESC
  LIMIT 1
) AS verification ON true
WHERE i.publication_status = 'approved'
  AND EXISTS (
    SELECT 1
    FROM serving.incident_reviews AS current_review
    WHERE current_review.incident_id = i.id
      AND current_review.revision = (
        SELECT max(latest_review.revision)
        FROM serving.incident_reviews AS latest_review
        WHERE latest_review.incident_id = i.id
      )
      AND current_review.review_status = 'accepted'
      AND current_review.reviewed_title = i.title
      AND current_review.reviewed_category = i.category
  );

REVOKE ALL ON serving.gateway_corridor_incidents FROM PUBLIC, anon, authenticated;
GRANT SELECT ON serving.gateway_corridor_incidents TO moobby_incident_gateway;

COMMENT ON SCHEMA serving IS
  'Private, reviewed incident records and a narrow read-only gateway projection.';
COMMENT ON TABLE serving.incidents IS
  'Stable UUID incident identity; publication and activity are independent. Human review is the latest incident_reviews revision; verification is the latest incident_verifications revision.';
COMMENT ON COLUMN serving.incidents.category IS
  'Internal vocabulary is aligned to Orvix disruption_type (excluding not_applicable); client enums require an explicit adapter mapping.';
COMMENT ON COLUMN serving.incidents.location_station_id IS
  'A curated incident-location station only. A station mention alone does not establish incident location.';
COMMENT ON TABLE serving.incident_evidence IS
  'Explicit correlation between an incident and a processing event, pinned to one event-review revision.';
COMMENT ON TABLE serving.incident_reviews IS
  'Append-only human review history; latest accepted review is required for publication.';
COMMENT ON TABLE serving.incident_verifications IS
  'Append-only factual verification history, independent of model and human incident review. No row means unverified.';
COMMENT ON VIEW serving.gateway_corridor_incidents IS
  'Read-only CorridorIncident-shaped projection; contains no post text or evidence fields. category and verificationStatus use internal serving values and need explicit client-enum mapping.';

COMMIT;
