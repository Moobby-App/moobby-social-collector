-- Read-only inspection in Supabase SQL Editor.
SELECT source_key, display_name, enabled, priority, page_kind, target_count
FROM collection.sources ORDER BY priority, source_key;

SELECT c.id, c.platform, c.external_id, c.canonical_url,
       s.author_handle, s.raw_text, s.text_kind, s.text_completeness,
       s.published_at, s.captured_at
FROM collection.raw_contents c
JOIN collection.raw_snapshots s ON s.raw_content_id = c.id
ORDER BY s.captured_at DESC LIMIT 50;

SELECT src.source_key, r.collector_label, r.run_status, r.stop_reason,
       r.started_at, count(o.id) AS stored_observations
FROM collection.collection_runs r
JOIN collection.sources src ON src.id = r.source_id
LEFT JOIN collection.content_observations o ON o.run_id = r.id
GROUP BY r.id, src.source_key ORDER BY r.started_at DESC;

SELECT pg_size_pretty(pg_database_size(current_database())) AS current_database_size;
