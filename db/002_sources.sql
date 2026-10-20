-- Execute AFTER 001. Proposed pilot registry; no content filtering or truth scoring.
BEGIN;
INSERT INTO collection.sources
  (source_key, platform, source_url, display_name, handle, source_kind,
   enabled, priority, page_kind, target_count, purpose, setup_notes)
VALUES
  ('x:commuterline','x','https://x.com/CommuterLine','KAI Commuter','CommuterLine','operator',
   true,1,'profile',20,
   'Primary operator account for service announcements; collect all loaded posts before downstream filtering.',
   'Existing authenticated collector works. Profile tab may omit replies; it is not complete coverage.'),
  ('x:jalur5_','x','https://x.com/jalur5_','Jalur5 Media','jalur5_','media',
   true,2,'profile',20,
   'Pilot supplementary station and travel reports, including Green Line context.',
   'Previously collected locally. Refresh with authenticated session; reliability and speed are not certified here.'),
  ('x:jalurserpong','x','https://x.com/JalurSerpong','Jalur Serpong','JalurSerpong','community',
   false,2,'profile',20,
   'Candidate community source focused on the Serpong corridor.',
   'Disabled until authenticated sampling confirms recent reachable posts. Old public cards do not prove account inactivity.')
ON CONFLICT (source_key) DO NOTHING;
COMMIT;
