-- Durable queue for Orvix snapshot processing.
-- Requires db/001_collection_schema.sql and db/006_processing_schema.sql.
BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'moobby_orvix_worker') THEN
    CREATE ROLE moobby_orvix_worker NOLOGIN;
  END IF;
END $$;

CREATE TABLE processing.jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_id uuid NOT NULL REFERENCES collection.raw_snapshots(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','claimed','retry_wait','completed','dead')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
  next_available_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  claim_token uuid UNIQUE,
  processing_run_id uuid REFERENCES processing.processing_runs(id) ON DELETE RESTRICT,
  manual_reprocess boolean NOT NULL DEFAULT false,
  failure_code text CHECK (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_claimed_at timestamptz,
  last_attempt_at timestamptz,
  finished_at timestamptz,
  CHECK (attempt_count <= max_attempts),
  CHECK (
    (status = 'claimed' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
      AND last_claimed_at IS NOT NULL)
    OR
    (status <> 'claimed' AND claim_token IS NULL AND lease_expires_at IS NULL)
  ),
  CHECK ((status IN ('completed','dead')) = (finished_at IS NOT NULL)),
  CHECK ((status = 'completed') = (processing_run_id IS NOT NULL)),
  CHECK ((status IN ('retry_wait','dead')) = (failure_code IS NOT NULL)),
  CHECK (status <> 'retry_wait' OR attempt_count < max_attempts)
);

-- A snapshot may have only one active job, including an explicitly requested rerun.
CREATE UNIQUE INDEX processing_jobs_one_active_snapshot_idx
  ON processing.jobs(snapshot_id)
  WHERE status IN ('queued','claimed','retry_wait');

-- Keep one automatic job record for the lifetime of a snapshot. In particular,
-- a dead automatic job cannot be silently recreated by a later poll.
CREATE UNIQUE INDEX processing_jobs_one_automatic_snapshot_idx
  ON processing.jobs(snapshot_id)
  WHERE manual_reprocess = false;

-- Supports bounded polling/claim queries and lease-expiry recovery.
CREATE INDEX processing_jobs_ready_idx
  ON processing.jobs(next_available_at, created_at, id)
  WHERE status IN ('queued','retry_wait');
CREATE INDEX processing_jobs_expired_lease_idx
  ON processing.jobs(lease_expires_at, id)
  WHERE status = 'claimed';
CREATE INDEX processing_jobs_run_idx
  ON processing.jobs(processing_run_id)
  WHERE processing_run_id IS NOT NULL;

ALTER TABLE processing.jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY processing_jobs_worker_read ON processing.jobs
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_jobs_worker_insert ON processing.jobs
  FOR INSERT TO moobby_orvix_worker
  WITH CHECK (
    status = 'queued' AND attempt_count = 0 AND processing_run_id IS NULL
    AND claim_token IS NULL AND lease_expires_at IS NULL AND failure_code IS NULL
    AND finished_at IS NULL
  );

-- The exporter reads all five collection tables and checks imported results.
-- The worker receives read-only access to collection data.
GRANT USAGE ON SCHEMA collection TO moobby_orvix_worker;
GRANT SELECT ON collection.sources, collection.collection_runs,
  collection.raw_contents, collection.raw_snapshots,
  collection.content_observations TO moobby_orvix_worker;
CREATE POLICY processing_worker_sources_read ON collection.sources
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_collection_runs_read ON collection.collection_runs
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_raw_contents_read ON collection.raw_contents
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_raw_snapshots_read ON collection.raw_snapshots
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_observations_read ON collection.content_observations
  FOR SELECT TO moobby_orvix_worker USING (true);

-- The exporter and idempotent importer read these tables; the importer inserts
-- one run, its event rows, and initial reviews in a single transaction.
GRANT SELECT, INSERT ON processing.processing_runs,
  processing.events, processing.event_reviews TO moobby_orvix_worker;
CREATE POLICY processing_worker_runs_read ON processing.processing_runs
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_runs_insert ON processing.processing_runs
  FOR INSERT TO moobby_orvix_worker WITH CHECK (true);
CREATE POLICY processing_worker_events_read ON processing.events
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_events_insert ON processing.events
  FOR INSERT TO moobby_orvix_worker WITH CHECK (true);
CREATE POLICY processing_worker_reviews_read ON processing.event_reviews
  FOR SELECT TO moobby_orvix_worker USING (true);
CREATE POLICY processing_worker_reviews_insert ON processing.event_reviews
  FOR INSERT TO moobby_orvix_worker WITH CHECK (true);

REVOKE ALL ON processing.jobs FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA processing TO moobby_orvix_worker;
GRANT SELECT ON processing.jobs TO moobby_orvix_worker;
GRANT INSERT (snapshot_id, manual_reprocess) ON processing.jobs TO moobby_orvix_worker;
REVOKE UPDATE ON processing.jobs FROM PUBLIC, anon, authenticated, moobby_orvix_worker;
REVOKE UPDATE (
  status, attempt_count, max_attempts, next_available_at, lease_expires_at,
  claim_token, processing_run_id, failure_code, updated_at,
  last_claimed_at, last_attempt_at, finished_at
) ON processing.jobs FROM moobby_orvix_worker;

-- All queue state changes happen through these SECURITY DEFINER functions.
-- Their predicates fence every claimed-job operation by the current token and
-- unexpired lease; callers cannot update queue state directly.
CREATE FUNCTION processing.claim_jobs(
  p_limit integer DEFAULT 5,
  p_lease_duration interval DEFAULT interval '5 minutes'
)
RETURNS TABLE (
  job_id uuid,
  snapshot_id uuid,
  claim_token uuid,
  attempt_count integer,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, processing
AS $$
BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 5 THEN
    RAISE EXCEPTION 'p_limit must be between 1 and 5';
  END IF;
  IF p_lease_duration IS NULL OR p_lease_duration <= interval '0'
     OR p_lease_duration > interval '1 hour' THEN
    RAISE EXCEPTION 'p_lease_duration must be positive and at most one hour';
  END IF;

  -- Expired claims at the retry ceiling are terminal and must not remain stuck.
  UPDATE processing.jobs AS j
  SET status = 'dead',
      failure_code = 'lease_expired_max_attempts',
      claim_token = NULL,
      lease_expires_at = NULL,
      finished_at = clock_timestamp(),
      updated_at = clock_timestamp()
  WHERE j.status = 'claimed'
    AND j.lease_expires_at <= clock_timestamp()
    AND j.attempt_count >= j.max_attempts;

  RETURN QUERY
  WITH candidates AS (
    SELECT j.id
    FROM processing.jobs AS j
    WHERE j.attempt_count < j.max_attempts
      AND (
        (j.status IN ('queued','retry_wait') AND j.next_available_at <= clock_timestamp())
        OR (j.status = 'claimed' AND j.lease_expires_at <= clock_timestamp())
      )
    ORDER BY j.next_available_at, j.created_at, j.id
    LIMIT p_limit
    FOR UPDATE OF j SKIP LOCKED
  )
  UPDATE processing.jobs AS j
  SET status = 'claimed',
      attempt_count = j.attempt_count + 1,
      claim_token = gen_random_uuid(),
      lease_expires_at = clock_timestamp() + p_lease_duration,
      last_claimed_at = clock_timestamp(),
      last_attempt_at = clock_timestamp(),
      failure_code = NULL,
      finished_at = NULL,
      updated_at = clock_timestamp()
  FROM candidates AS c
  WHERE j.id = c.id
  RETURNING j.id, j.snapshot_id, j.claim_token, j.attempt_count, j.lease_expires_at;
END;
$$;

CREATE FUNCTION processing.renew_job_lease(
  p_job_id uuid,
  p_claim_token uuid,
  p_lease_duration interval DEFAULT interval '5 minutes'
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, processing
AS $$
DECLARE changed integer;
BEGIN
  IF p_lease_duration IS NULL OR p_lease_duration <= interval '0'
     OR p_lease_duration > interval '1 hour' THEN
    RAISE EXCEPTION 'p_lease_duration must be positive and at most one hour';
  END IF;
  UPDATE processing.jobs AS j
  SET lease_expires_at = clock_timestamp() + p_lease_duration,
      updated_at = clock_timestamp()
  WHERE j.id = p_job_id AND j.status = 'claimed'
    AND j.claim_token = p_claim_token
    AND j.lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

CREATE FUNCTION processing.retry_job(
  p_job_id uuid,
  p_claim_token uuid,
  p_failure_code text,
  p_delay interval
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, processing
AS $$
DECLARE changed integer;
BEGIN
  IF p_failure_code IS NULL OR p_failure_code !~ '^[a-z][a-z0-9_]{0,63}$' THEN
    RAISE EXCEPTION 'p_failure_code must be a sanitized failure code';
  END IF;
  IF p_delay IS NULL OR p_delay < interval '0' OR p_delay > interval '7 days' THEN
    RAISE EXCEPTION 'p_delay must be between zero and seven days';
  END IF;
  UPDATE processing.jobs AS j
  SET status = CASE WHEN j.attempt_count >= j.max_attempts THEN 'dead' ELSE 'retry_wait' END,
      next_available_at = CASE WHEN j.attempt_count >= j.max_attempts
        THEN j.next_available_at ELSE clock_timestamp() + p_delay END,
      failure_code = p_failure_code,
      claim_token = NULL,
      lease_expires_at = NULL,
      finished_at = CASE WHEN j.attempt_count >= j.max_attempts
        THEN clock_timestamp() ELSE NULL END,
      updated_at = clock_timestamp()
  WHERE j.id = p_job_id AND j.status = 'claimed'
    AND j.claim_token = p_claim_token
    AND j.lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

CREATE FUNCTION processing.complete_job(
  p_job_id uuid,
  p_claim_token uuid,
  p_processing_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, processing
AS $$
DECLARE changed integer;
BEGIN
  UPDATE processing.jobs AS j
  SET status = 'completed',
      processing_run_id = p_processing_run_id,
      claim_token = NULL,
      lease_expires_at = NULL,
      failure_code = NULL,
      finished_at = clock_timestamp(),
      updated_at = clock_timestamp()
  WHERE j.id = p_job_id AND j.status = 'claimed'
    AND j.claim_token = p_claim_token
    AND j.lease_expires_at > clock_timestamp()
    AND EXISTS (
      SELECT 1
      FROM processing.processing_runs AS pr
      JOIN processing.events AS e ON e.run_id = pr.id
      WHERE pr.id = p_processing_run_id
        AND pr.manifest_status = 'completed'
        AND e.snapshot_id = j.snapshot_id
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION processing.claim_jobs(integer, interval)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION processing.renew_job_lease(uuid, uuid, interval)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION processing.retry_job(uuid, uuid, text, interval)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION processing.complete_job(uuid, uuid, uuid)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION processing.claim_jobs(integer, interval),
  processing.renew_job_lease(uuid, uuid, interval),
  processing.retry_job(uuid, uuid, text, interval),
  processing.complete_job(uuid, uuid, uuid) TO moobby_orvix_worker;

COMMENT ON TABLE processing.jobs IS
  'Durable Orvix work queue; manual_reprocess=true is required to create another job for a snapshot.';
COMMENT ON COLUMN processing.jobs.claim_token IS
  'Per-claim fencing token; workers must match it when renewing or completing a lease.';
COMMENT ON COLUMN processing.jobs.failure_code IS
  'Sanitized machine-readable failure category; never store exception text or post content.';
COMMENT ON FUNCTION processing.claim_jobs(integer, interval) IS
  'Atomically claims up to five ready jobs using row locks with SKIP LOCKED.';
COMMENT ON FUNCTION processing.complete_job(uuid, uuid, uuid) IS
  'Completes only a job with the matching current, unexpired claim token and an imported event for the snapshot.';

COMMIT;
