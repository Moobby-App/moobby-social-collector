import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.orvix_job_queue import (
    LeaseRejected,
    OrvixJobQueue,
    QueueClientError,
    WORKER_LOGIN,
    load_worker_connection,
)


class FakeConnection:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        response = self.responses.pop(0) if self.responses else []
        return FakeResult(response)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class OrvixJobQueueTests(unittest.TestCase):
    JOB_ID = '00000000-0000-0000-0000-000000000001'
    SNAPSHOT_ID = '00000000-0000-0000-0000-000000000002'
    TOKEN = '00000000-0000-0000-0000-000000000003'
    RUN_ID = '00000000-0000-0000-0000-000000000004'

    def make_queue(self, rows=None):
        conn = FakeConnection(rows)
        return OrvixJobQueue(
            db_params={'user': WORKER_LOGIN},
            connection_factory=lambda _params: conn,
        ), conn

    def test_claim_uses_database_function_and_returns_claim_tokens(self):
        rows = [(self.JOB_ID, self.SNAPSHOT_ID, self.TOKEN, 1, 'expiry')]
        queue, conn = self.make_queue([rows, [(False, None)]])
        claims = queue.claim_jobs(limit=5)
        self.assertEqual(claims[0]['claim_token'], self.TOKEN)
        self.assertIn('processing.claim_jobs', conn.calls[0][0])
        self.assertEqual(conn.calls[0][1], (5, '5 minutes'))
        self.assertTrue(conn.closed)

    def test_claim_retires_already_completed_automatic_job_without_returning_it(self):
        rows = [(self.JOB_ID, self.SNAPSHOT_ID, self.TOKEN, 2, 'expiry')]
        queue, conn = self.make_queue([rows, [(False, self.RUN_ID)], [(True,)]])
        self.assertEqual(queue.claim_jobs(), [])
        self.assertIn('processing.complete_job', conn.calls[2][0])

    def test_claim_keeps_explicit_manual_reprocess_even_if_snapshot_was_completed(self):
        rows = [(self.JOB_ID, self.SNAPSHOT_ID, self.TOKEN, 1, 'expiry')]
        queue, conn = self.make_queue([rows, [(True, self.RUN_ID)]])
        self.assertEqual(len(queue.claim_jobs()), 1)

    def test_claim_rejects_limit_above_database_cap(self):
        queue, conn = self.make_queue()
        with self.assertRaises(ValueError):
            queue.claim_jobs(limit=6)
        self.assertEqual(conn.calls, [])

    def test_empty_queue_returns_empty_list(self):
        queue, _conn = self.make_queue([[]])
        self.assertEqual(queue.claim_jobs(), [])

    def test_bad_token_is_rejected_locally(self):
        queue, conn = self.make_queue()
        with self.assertRaisesRegex(ValueError, 'claim_token'):
            queue.renew_lease(self.JOB_ID, 'bad-token')
        self.assertEqual(conn.calls, [])

    def test_database_rejection_for_stale_or_wrong_token_is_reported(self):
        queue, conn = self.make_queue([[(False,)]])
        with self.assertRaises(LeaseRejected):
            queue.renew_lease(
                self.JOB_ID, self.TOKEN,
            )
        self.assertIn('processing.renew_job_lease', conn.calls[0][0])
        self.assertTrue(conn.closed)

    def test_retry_uses_retry_function_with_sanitized_code_and_delay(self):
        queue, conn = self.make_queue([[(True,)]])
        self.assertTrue(queue.schedule_retry(
            self.JOB_ID, self.TOKEN,
            'orvix_timeout',
            '30 seconds',
        ))
        query, params = conn.calls[0]
        self.assertIn('processing.retry_job', query)
        self.assertEqual(params[2:], ('orvix_timeout', '30 seconds'))

    def test_retry_rejects_unsafe_failure_code_before_database_call(self):
        queue, conn = self.make_queue()
        with self.assertRaises(ValueError):
            queue.schedule_retry('bad', 'bad', 'Exception: secret', '1 minute')
        self.assertEqual(conn.calls, [])

    def test_completed_job_calls_token_fenced_database_function(self):
        queue, conn = self.make_queue([[(True,)]])
        self.assertTrue(queue.complete(self.JOB_ID, self.TOKEN, self.RUN_ID))
        self.assertIn('processing.complete_job', conn.calls[0][0])

    def test_enqueue_filters_completed_snapshots_and_prior_automatic_jobs(self):
        queue, conn = self.make_queue([[]])
        self.assertEqual(queue.enqueue_eligible(limit=3), [])
        query = conn.calls[0][0]
        self.assertIn("pr.manifest_status = 'completed'", query)
        self.assertIn('j.manual_reprocess = false', query)
        self.assertIn('ON CONFLICT DO NOTHING', query)
        self.assertEqual(conn.calls[0][1], (3,))

    def test_manual_reprocess_is_explicit_and_marks_job_manual(self):
        queue, conn = self.make_queue([[('job-1', 'snapshot-1')]])
        result = queue.enqueue_manual(self.SNAPSHOT_ID)
        self.assertEqual(result[0]['job_id'], 'job-1')
        query, params = conn.calls[0]
        self.assertIn('manual_reprocess) VALUES (%s, true)', query)
        self.assertEqual(params, (self.SNAPSHOT_ID,))

    def test_worker_config_requires_private_file_and_dedicated_login(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'worker.json'
            data = {
                'host': 'db.example.supabase.co', 'port': '5432', 'dbname': 'postgres',
                'user': WORKER_LOGIN, 'password': 'not-tested-or-printed',
            }
            path.write_text(json.dumps(data), encoding='utf-8')
            os.chmod(path, 0o600)
            params = load_worker_connection(path)
            self.assertEqual(params['user'], WORKER_LOGIN)
            self.assertEqual(params['sslmode'], 'require')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            data['user'] = 'collector_login'
            path.write_text(json.dumps(data), encoding='utf-8')
            os.chmod(path, 0o600)
            with self.assertRaises(QueueClientError):
                load_worker_connection(path)


if __name__ == '__main__':
    unittest.main()
