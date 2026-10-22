-- Private KRL station reference data imported from versioned Open-KRL captures.
BEGIN;

CREATE SCHEMA reference;
REVOKE ALL ON SCHEMA reference FROM PUBLIC, anon, authenticated;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_reference_admin') THEN
    CREATE ROLE moobby_reference_admin NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA reference TO moobby_reference_admin;

CREATE TABLE reference.krl_station_datasets (
  source_sha256 text PRIMARY KEY CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  source_url text NOT NULL CHECK (source_url LIKE 'https://%'),
  weekday_capture date NOT NULL,
  sunday_capture date NOT NULL,
  built_at timestamptz NOT NULL,
  effective_since date NOT NULL,
  checked_at date NOT NULL,
  dataset_license text NOT NULL,
  source_attribution text NOT NULL,
  coordinate_source text NOT NULL,
  coordinate_attribution text NOT NULL,
  coordinate_license text NOT NULL,
  source_metadata jsonb NOT NULL CHECK (jsonb_typeof(source_metadata) = 'object'),
  imported_at timestamptz NOT NULL DEFAULT now(),
  CHECK (sunday_capture >= weekday_capture)
);

CREATE TABLE reference.krl_stations (
  station_id text PRIMARY KEY CHECK (station_id LIKE 'open-krl:%'),
  source_station_code text NOT NULL UNIQUE CHECK (length(btrim(source_station_code)) > 0),
  canonical_name text NOT NULL CHECK (length(btrim(canonical_name)) > 0),
  aliases text[] NOT NULL DEFAULT '{}',
  latitude numeric(9,5) NOT NULL CHECK (latitude BETWEEN -90 AND 90),
  longitude numeric(9,5) NOT NULL CHECK (longitude BETWEEN -180 AND 180),
  line_memberships text[] NOT NULL CHECK (
    cardinality(line_memberships) > 0 AND array_position(line_memberships, NULL) IS NULL
  ),
  source_sha256 text NOT NULL REFERENCES reference.krl_station_datasets(source_sha256)
    ON DELETE RESTRICT,
  CHECK (array_position(aliases, NULL) IS NULL)
);

CREATE INDEX krl_stations_name_idx ON reference.krl_stations (lower(canonical_name));
CREATE INDEX krl_stations_lines_idx ON reference.krl_stations USING gin (line_memberships);

ALTER TABLE reference.krl_station_datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE reference.krl_stations ENABLE ROW LEVEL SECURITY;

CREATE POLICY reference_datasets_admin_read ON reference.krl_station_datasets
  FOR SELECT TO moobby_reference_admin USING (true);
CREATE POLICY reference_datasets_admin_insert ON reference.krl_station_datasets
  FOR INSERT TO moobby_reference_admin WITH CHECK (true);
CREATE POLICY reference_stations_admin_read ON reference.krl_stations
  FOR SELECT TO moobby_reference_admin USING (true);
CREATE POLICY reference_stations_admin_insert ON reference.krl_stations
  FOR INSERT TO moobby_reference_admin WITH CHECK (true);
CREATE POLICY reference_stations_admin_update ON reference.krl_stations
  FOR UPDATE TO moobby_reference_admin USING (true) WITH CHECK (true);

REVOKE ALL ON reference.krl_station_datasets, reference.krl_stations
  FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON reference.krl_station_datasets TO moobby_reference_admin;
GRANT SELECT, INSERT, UPDATE ON reference.krl_stations TO moobby_reference_admin;

COMMENT ON SCHEMA reference IS 'Private, versioned reference data for the Moobby serving layer.';
COMMENT ON TABLE reference.krl_station_datasets IS
  'Immutable source metadata keyed by the SHA-256 of the exact source JSON file.';
COMMENT ON TABLE reference.krl_stations IS
  'KRL-only station reference; Open-KRL codes are source identifiers, not asserted KAI codes.';
COMMENT ON COLUMN reference.krl_stations.latitude IS
  'Coordinate from OpenStreetMap contributors via Open-KRL; attribution/license recorded in dataset metadata.';

COMMIT;
