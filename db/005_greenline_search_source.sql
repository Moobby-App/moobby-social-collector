-- Run after 001/004 in Supabase SQL Editor. Register a search source, not an author.
BEGIN;
INSERT INTO collection.sources
  (source_key, platform, source_url, display_name, handle, source_kind,
   enabled, priority, page_kind, target_count, purpose, setup_notes)
VALUES
  ('x:greenline_search', 'x', 'https://x.com/search?q=KRL%20Serpong&f=live',
   'Green Line - raw X searches', NULL, 'other', true, 2, 'page', 100,
   'Raw 24-hour search candidates: pilot accounts, route/station keywords, and operator mentions. Includes other lines and unverified reports.',
   'Search source, not a post author. Exact window and queries are in collection_runs.coverage; per-post query IDs and reply context are in extraction_metadata. Search completeness is not guaranteed; no event grouping.')
ON CONFLICT (source_key) DO NOTHING;
COMMIT;
