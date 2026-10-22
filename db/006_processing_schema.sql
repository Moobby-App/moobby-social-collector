-- Add private storage for immutable Orvix runs, event rows, and review history.
-- Requires db/001_collection_schema.sql and the moobby_collection_team role.
BEGIN;

CREATE SCHEMA processing;
REVOKE ALL ON SCHEMA processing FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA processing TO moobby_collection_team;

CREATE TABLE processing.processing_runs (
  id uuid PRIMARY KEY, -- manifest run_id; reused for retries of the same import
  manifest_version text NOT NULL CHECK (manifest_version = 'orvix.processing_manifest.v1'),
  manifest_status text NOT NULL CHECK (manifest_status = 'completed'),
  schema_version text NOT NULL CHECK (schema_version = 'orvix.transport.v1'),
  model text NOT NULL,
  processor_version text NOT NULL,
  prompt_version text NOT NULL,
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  input_csv_sha256 text NOT NULL CHECK (input_csv_sha256 ~ '^[0-9a-f]{64}$'),
  output_csv_sha256 text NOT NULL CHECK (output_csv_sha256 ~ '^[0-9a-f]{64}$'),
  imported_at timestamptz NOT NULL DEFAULT now(),
  CHECK (completed_at >= started_at)
);

CREATE TABLE processing.events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES processing.processing_runs(id) ON DELETE RESTRICT,
  snapshot_id uuid NOT NULL REFERENCES collection.raw_snapshots(id) ON DELETE RESTRICT,
  event_index integer NOT NULL CHECK (event_index >= 0),
  event_status text NOT NULL CHECK (event_status IN ('potential_disruption','no_disruption','unclear')),
  disruption_type text NOT NULL CHECK (disruption_type IN (
    'delay','service_suspension','route_change','signal_or_power_issue',
    'infrastructure_issue','accident_or_safety_issue','overcrowding',
    'flooding_or_weather','other','unknown','not_applicable'
  )),
  disruption_type_evidence text NOT NULL CHECK (disruption_type_evidence IN (
    'explicit','inferred','unknown','not_applicable'
  )),
  affected_entities jsonb NOT NULL CHECK (jsonb_typeof(affected_entities) = 'array'),
  severity text NOT NULL CHECK (severity IN ('low','moderate','high','critical','unknown','not_applicable')),
  severity_evidence text NOT NULL CHECK (severity_evidence IN (
    'explicit','inferred','unknown','not_applicable'
  )),
  event_start_time timestamptz,
  event_start_time_evidence text NOT NULL CHECK (event_start_time_evidence IN (
    'explicit','inferred','unknown','not_applicable'
  )),
  event_end_time timestamptz,
  event_end_time_evidence text NOT NULL CHECK (event_end_time_evidence IN (
    'explicit','inferred','unknown','not_applicable'
  )),
  uncertainty_status text NOT NULL CHECK (uncertainty_status IN ('yes','no','unknown')),
  uncertainty_note text NOT NULL DEFAULT '',
  explanation text NOT NULL,
  evidence_quotes jsonb NOT NULL CHECK (jsonb_typeof(evidence_quotes) = 'array'),
  UNIQUE (run_id, snapshot_id, event_index),
  CHECK (
    (event_index = 0 AND event_status IN ('no_disruption','unclear')) OR
    (event_index > 0 AND event_status = 'potential_disruption')
  ),
  CHECK (
    (disruption_type = 'unknown' AND disruption_type_evidence = 'unknown') OR
    (disruption_type = 'not_applicable' AND event_status = 'no_disruption'
      AND disruption_type_evidence = 'not_applicable') OR
    (disruption_type NOT IN ('unknown','not_applicable')
      AND disruption_type_evidence IN ('explicit','inferred'))
  ),
  CHECK (
    (severity = 'unknown' AND severity_evidence = 'unknown') OR
    (severity = 'not_applicable' AND event_status = 'no_disruption'
      AND severity_evidence = 'not_applicable') OR
    (severity NOT IN ('unknown','not_applicable')
      AND severity_evidence IN ('explicit','inferred'))
  ),
  CHECK (
    (event_start_time IS NULL AND
      ((event_status = 'no_disruption' AND event_start_time_evidence = 'not_applicable') OR
       (event_status <> 'no_disruption' AND event_start_time_evidence = 'unknown'))) OR
    (event_start_time IS NOT NULL AND event_start_time_evidence IN ('explicit','inferred'))
  ),
  CHECK (
    (event_end_time IS NULL AND
      ((event_status = 'no_disruption' AND event_end_time_evidence = 'not_applicable') OR
       (event_status <> 'no_disruption' AND event_end_time_evidence = 'unknown'))) OR
    (event_end_time IS NOT NULL AND event_end_time_evidence IN ('explicit','inferred'))
  ),
  CHECK (
    event_status <> 'no_disruption' OR
    (disruption_type = 'not_applicable' AND disruption_type_evidence = 'not_applicable'
      AND affected_entities = '[]'::jsonb
      AND severity = 'not_applicable' AND severity_evidence = 'not_applicable'
      AND event_start_time IS NULL AND event_end_time IS NULL)
  )
);

CREATE TABLE processing.event_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES processing.events(id) ON DELETE RESTRICT,
  revision integer NOT NULL CHECK (revision > 0),
  review_status text NOT NULL CHECK (review_status IN ('pending','accepted','corrected','rejected')),
  review_notes text NOT NULL DEFAULT '',
  corrections jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(corrections) = 'object'),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, revision)
);

CREATE INDEX processing_runs_imported_at_idx
  ON processing.processing_runs(imported_at DESC);
CREATE INDEX processing_events_snapshot_idx
  ON processing.events(snapshot_id, event_index);

ALTER TABLE processing.processing_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing.event_reviews ENABLE ROW LEVEL SECURITY;

CREATE POLICY processing_runs_team_read ON processing.processing_runs
  FOR SELECT TO moobby_collection_team USING (true);
CREATE POLICY processing_runs_team_insert ON processing.processing_runs
  FOR INSERT TO moobby_collection_team WITH CHECK (true);
CREATE POLICY processing_events_team_read ON processing.events
  FOR SELECT TO moobby_collection_team USING (true);
CREATE POLICY processing_events_team_insert ON processing.events
  FOR INSERT TO moobby_collection_team WITH CHECK (true);
CREATE POLICY processing_event_reviews_team_read ON processing.event_reviews
  FOR SELECT TO moobby_collection_team USING (true);
CREATE POLICY processing_event_reviews_team_insert ON processing.event_reviews
  FOR INSERT TO moobby_collection_team WITH CHECK (true);

REVOKE ALL ON processing.processing_runs, processing.events, processing.event_reviews
  FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON processing.processing_runs, processing.events, processing.event_reviews
  TO moobby_collection_team;

COMMENT ON SCHEMA processing IS 'Private Orvix processing results; separate from immutable collection snapshots.';
COMMENT ON TABLE processing.processing_runs IS 'One finalized Orvix execution/import, identified by manifest run_id.';
COMMENT ON TABLE processing.events IS 'Immutable event-level model results linked to their source snapshot and processing run.';
COMMENT ON TABLE processing.event_reviews IS 'Append-only status, notes, and correction revisions for model event rows.';

COMMIT;
