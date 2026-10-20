-- Moobby / collection only / 2026-10-20
-- Run once in Supabase SQL Editor on a NEW project. No curation/event tables.
-- Kept outside public: do not add collection to exposed Data API schemas.
BEGIN;

CREATE SCHEMA collection;
REVOKE ALL ON SCHEMA collection FROM PUBLIC, anon, authenticated;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_collection_team') THEN
    CREATE ROLE moobby_collection_team NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA collection TO moobby_collection_team;

CREATE TABLE collection.sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_key text NOT NULL UNIQUE,
  platform text NOT NULL CHECK (platform IN ('x','threads','rss','web')),
  source_url text NOT NULL CHECK (source_url LIKE 'https://%'),
  display_name text NOT NULL,
  handle text,
  source_kind text NOT NULL CHECK (source_kind IN ('operator','community','media','other')),
  enabled boolean NOT NULL DEFAULT false,
  priority smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
  page_kind text NOT NULL DEFAULT 'profile' CHECK (page_kind IN ('profile','replies','feed','page')),
  target_count integer NOT NULL DEFAULT 20 CHECK (target_count BETWEEN 1 AND 100),
  collection_mode text NOT NULL DEFAULT 'manual' CHECK (collection_mode IN ('manual','scheduled')),
  purpose text NOT NULL,
  setup_notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE collection.collection_runs (
  id uuid PRIMARY KEY, -- client UUID; reuse on retry of the same upload
  source_id uuid NOT NULL REFERENCES collection.sources(id),
  collector_label text NOT NULL, -- attribution only; NOT an authentication mechanism
  collector_version text NOT NULL,
  collection_method text NOT NULL CHECK (collection_method IN ('playwright','rss','manual')),
  started_at timestamptz NOT NULL,
  finished_at timestamptz,
  run_status text NOT NULL CHECK (run_status IN ('running','completed','partial','failed','unknown')),
  stop_reason text,
  coverage text NOT NULL DEFAULT 'loaded_items_only',
  complete_history boolean NOT NULL DEFAULT false,
  requested_count integer CHECK (requested_count BETWEEN 1 AND 100),
  captured_count integer NOT NULL DEFAULT 0 CHECK (captured_count >= 0),
  time_basis text NOT NULL CHECK (time_basis IN ('collector_clock','item_capture_range')),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE collection.raw_contents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL CHECK (platform IN ('x','threads','rss','web')),
  external_id text NOT NULL CHECK (length(btrim(external_id)) > 0),
  canonical_url text NOT NULL CHECK (canonical_url LIKE 'https://%'),
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  UNIQUE (platform, external_id),
  CHECK (last_seen_at >= first_seen_at)
);

CREATE TABLE collection.raw_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_content_id uuid NOT NULL REFERENCES collection.raw_contents(id),
  capture_hash text NOT NULL CHECK (capture_hash ~ '^[0-9a-f]{64}$'),
  author_handle text,
  author_display_name text,
  title text,
  raw_text text NOT NULL,
  text_kind text NOT NULL CHECK (text_kind IN ('post_text','article_text','feed_excerpt','card_text','none')),
  text_completeness text NOT NULL CHECK (text_completeness IN ('full','truncated','unknown')),
  published_at timestamptz,
  published_at_basis text NOT NULL CHECK (published_at_basis IN ('dom_datetime','feed_timestamp','page_timestamp','unknown')),
  media jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(media) = 'array'),
  media_capture_status text NOT NULL CHECK (media_capture_status IN ('captured','not_attempted','failed')),
  captured_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (raw_content_id, capture_hash),
  UNIQUE (id, raw_content_id),
  CHECK ((published_at IS NULL) = (published_at_basis = 'unknown')),
  CHECK (media_capture_status = 'captured' OR jsonb_array_length(media) = 0)
);

CREATE TABLE collection.content_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES collection.collection_runs(id),
  raw_content_id uuid NOT NULL REFERENCES collection.raw_contents(id),
  snapshot_id uuid NOT NULL,
  captured_at timestamptz NOT NULL,
  published_label text,
  position_in_run integer CHECK (position_in_run > 0),
  is_pinned boolean, -- NULL if collector did not inspect the pinned marker
  post_type text NOT NULL DEFAULT 'unknown' CHECK (post_type IN ('original','reply','repost','quote','unknown')),
  related_urls jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(related_urls) = 'array'),
  FOREIGN KEY (snapshot_id, raw_content_id)
    REFERENCES collection.raw_snapshots(id, raw_content_id),
  UNIQUE (run_id, raw_content_id)
);

CREATE INDEX runs_source_time ON collection.collection_runs(source_id, started_at DESC);
CREATE INDEX snapshots_content_time ON collection.raw_snapshots(raw_content_id, captured_at DESC);
CREATE INDEX observations_content_time ON collection.content_observations(raw_content_id, captured_at DESC);

-- RLS plus explicit grants: no anonymous/mobile access to raw material.
DO $$ DECLARE name text; BEGIN
  FOREACH name IN ARRAY ARRAY['sources','collection_runs','raw_contents','raw_snapshots','content_observations'] LOOP
    EXECUTE format('ALTER TABLE collection.%I ENABLE ROW LEVEL SECURITY', name);
    EXECUTE format('REVOKE ALL ON collection.%I FROM PUBLIC, anon, authenticated', name);
    EXECUTE format('CREATE POLICY team_read ON collection.%I FOR SELECT TO moobby_collection_team USING (true)', name);
    EXECUTE format('CREATE POLICY team_insert ON collection.%I FOR INSERT TO moobby_collection_team WITH CHECK (true)', name);
    EXECUTE format('GRANT SELECT, INSERT ON collection.%I TO moobby_collection_team', name);
  END LOOP;
END $$;
-- A writer may maintain identity timestamps and run completion, not edit raw captures.
CREATE POLICY team_update ON collection.raw_contents FOR UPDATE TO moobby_collection_team USING (true) WITH CHECK (true);
GRANT UPDATE(first_seen_at, last_seen_at, canonical_url) ON collection.raw_contents TO moobby_collection_team;
CREATE POLICY team_update ON collection.collection_runs FOR UPDATE TO moobby_collection_team USING (true) WITH CHECK (true);
GRANT UPDATE(finished_at, run_status, stop_reason, captured_count) ON collection.collection_runs TO moobby_collection_team;

COMMENT ON TABLE collection.raw_contents IS 'One platform content identity, not a verified news event.';
COMMENT ON TABLE collection.raw_snapshots IS 'Immutable captured representation; a different snapshot does not prove author edited the post.';
COMMENT ON TABLE collection.content_observations IS 'Where/by which run a content snapshot was seen. Source account may differ from post author.';
COMMENT ON COLUMN collection.raw_snapshots.media IS 'Only media visible to collector: type, URL, optional alt text/thumbnail. No cookies or request headers.';

COMMIT;
