-- Apply AFTER 001/002 on the existing project. Keeps all five tables/data/RLS.
BEGIN;
ALTER TABLE collection.raw_snapshots
  ADD COLUMN IF NOT EXISTS extraction_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='collection.raw_snapshots'::regclass AND conname='snapshot_extraction_metadata_object') THEN
    ALTER TABLE collection.raw_snapshots ADD CONSTRAINT snapshot_extraction_metadata_object
      CHECK (jsonb_typeof(extraction_metadata) = 'object');
  END IF;
END $$;
COMMENT ON COLUMN collection.raw_snapshots.extraction_metadata IS
  'Extraction provenance only; not relevance or truth verification. OCR evidence lives within media JSON.';
COMMIT;
