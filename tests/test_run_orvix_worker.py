import csv
import threading
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import process_orvix_csv
from scripts.export_orvix_input import CSV_FIELDS
from scripts.import_orvix_results import ImportJobCompletionError
from scripts.orvix_job_queue import LeaseRejected, QueueClientError
from scripts.process_orvix_csv import ProcessingError
from scripts.run_orvix_worker import (
    LeaseKeeper,
    LeaseLostError,
    WorkerCycleError,
    run_worker_cycle,
    run_watch,
    main,
)


SNAPSHOT_ID = str(uuid.UUID('e60acb2c-4a7e-40fb-8b60-0d2b48702022'))
JOB_ID = str(uuid.UUID('d2f22373-f2e6-4e42-9b2c-4231102fe6b0'))
CLAIM_TOKEN = str(uuid.UUID('01c58184-4ce4-4c41-b585-3ef038687bc7'))
RUN_ID = str(uuid.UUID('53443f16-c455-4107-8efb-27c7f5bbdfe5'))


class FakeQueue:
    def __init__(self, claims=None):
        self.claims = list(claims or [])
        self.calls = []
        self.renew_result = True

    def enqueue_eligible(self, limit):
        self.calls.append(('enqueue', limit))
        return []

    def claim_jobs(self, limit, lease_duration):
        self.calls.append(('claim', limit, lease_duration))
        return list(self.claims)

    def renew_lease(self, job_id, token, duration):
        self.calls.append(('renew', job_id, token, duration))
        if self.renew_result is False:
            raise LeaseRejected('rejected')
        return True

    def schedule_retry(self, job_id, token, failure_code, delay):
        self.calls.append(('retry', job_id, token, failure_code, delay))
        return True


class FakeKeeper:
    def __init__(self, queue, claims, lease_duration, interval_seconds, events):
        self.queue = queue
        self.claims = claims
        self.events = events
        self.failed = False
        self.check_count = 0

    def start(self):
        self.events.append('heartbeat_start')

    def renew_once(self):
        self.events.append('heartbeat_renew')

    def check(self):
        self.check_count += 1
        self.events.append('lease_check')
        if self.failed:
            raise LeaseLostError()

    def stop(self):
        self.events.append('heartbeat_stop')


class OrvixWorkerCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.claim = {
            'job_id': JOB_ID,
            'snapshot_id': SNAPSHOT_ID,
            'claim_token': CLAIM_TOKEN,
            'attempt_count': 1,
        }
        self.queue = FakeQueue([self.claim])
        self.params = {'user': 'moobby_orvix_worker_login', 'password': 'not-used'}
        self.events = []
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.payload = {
            'run': {'id': uuid.UUID(RUN_ID)},
            'input_snapshot_count': 1,
            'input_snapshot_ids': [uuid.UUID(SNAPSHOT_ID)],
            'events': [{'snapshot_id': uuid.UUID(SNAPSHOT_ID), 'event_index': 0}],
        }

        def heartbeat_factory(queue, claims, lease_duration, interval_seconds):
            return FakeKeeper(queue, claims, lease_duration, interval_seconds, self.events)
        self.heartbeat_factory = heartbeat_factory

        def export_fn(limit, db_params, snapshot_ids, output_path):
            self.events.append('export')
            self.assertEqual(snapshot_ids, [SNAPSHOT_ID])
            self.assertEqual(limit, 1)
            row = {field: '' for field in CSV_FIELDS}
            row.update({'snapshot_id': SNAPSHOT_ID, 'raw_text': 'synthetic local text'})
            with Path(output_path).open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerow(row)
            return 1
        self.export_fn = export_fn

        def process_fn(input_path, limit, max_tokens, before_snapshot):
            self.events.append('process')
            before_snapshot()
            return RUN_ID, self.run_dir, 1, 1
        self.process_fn = process_fn

        def validate_fn(run_directory):
            self.events.append('validate')
            return self.payload
        self.validate_fn = validate_fn

        def import_fn(run_directory, claims, db_params):
            self.events.append('import')
            return self.payload, 'imported'
        self.import_fn = import_fn

    def tearDown(self):
        self.temp.cleanup()

    def run_cycle(self, **overrides):
        options = {
            'db_params_provider': lambda: self.params,
            'queue_factory': lambda params: self.queue,
            'export_fn': self.export_fn,
            'process_fn': self.process_fn,
            'validate_fn': self.validate_fn,
            'import_fn': self.import_fn,
            'heartbeat_factory': self.heartbeat_factory,
            'staging_parent': self.root / 'staging',
        }
        options.update(overrides)
        return run_worker_cycle(limit=1, **options)

    def test_normal_cycle_runs_stages_and_imports_atomically(self):
        summary = self.run_cycle()

        self.assertEqual(self.events[0:5], [
            'heartbeat_start', 'heartbeat_renew', 'lease_check', 'export', 'process'
        ])
        self.assertEqual(self.events.count('heartbeat_renew'), 2)
        self.assertLess(self.events.index('validate'), self.events.index('heartbeat_stop'))
        self.assertLess(self.events.index('heartbeat_stop'), self.events.index('import'))
        self.assertEqual(summary, {
            'run_id': RUN_ID, 'snapshot_count': 1,
            'event_count': 1, 'import_status': 'imported',
        })
        self.assertEqual(self.queue.calls[:2], [('enqueue', 1), ('claim', 1, '5 minutes')])

    def test_empty_queue_skips_export_orvix_validation_and_import(self):
        self.queue.claims = []

        summary = self.run_cycle()

        self.assertEqual(summary['import_status'], 'skipped_no_jobs')
        self.assertEqual(self.events, [])
        self.assertEqual([call[0] for call in self.queue.calls], ['enqueue', 'claim'])

    def test_lease_keeper_renews_every_claim(self):
        second = dict(self.claim, job_id=str(uuid.uuid4()), claim_token=str(uuid.uuid4()),
                      snapshot_id=str(uuid.uuid4()))
        queue = FakeQueue([self.claim, second])
        keeper = LeaseKeeper(queue, [self.claim, second], interval_seconds=1)

        self.assertTrue(keeper.renew_once())

        renewals = [call for call in queue.calls if call[0] == 'renew']
        self.assertEqual(len(renewals), 2)
        self.assertEqual([call[1] for call in renewals], [self.claim['job_id'], second['job_id']])

    def test_api_failure_schedules_retry_and_skips_import(self):
        def fail_process(input_path, limit, max_tokens, before_snapshot):
            before_snapshot()
            raise ProcessingError('safe API failure', failure_code='api_timeout')
        self.process_fn = fail_process

        with self.assertRaises(WorkerCycleError):
            self.run_cycle()

        self.assertNotIn('import', self.events)
        retry = [call for call in self.queue.calls if call[0] == 'retry']
        self.assertEqual(retry[0][3], 'api_timeout')

    def test_heartbeat_setup_failure_attempts_token_checked_retry(self):
        def fail_heartbeat(*args, **kwargs):
            raise RuntimeError('heartbeat setup failed')

        with self.assertRaises(WorkerCycleError):
            self.run_cycle(heartbeat_factory=fail_heartbeat)

        self.assertNotIn('export', self.events)
        retry = [call for call in self.queue.calls if call[0] == 'retry']
        self.assertEqual(len(retry), 1)

    def test_shutdown_before_next_request_retries_claim_and_stops_cycle(self):
        stop_event = threading.Event()

        def shutdown_process(input_path, limit, max_tokens, before_snapshot):
            stop_event.set()
            before_snapshot()

        self.process_fn = shutdown_process
        with self.assertRaises(WorkerCycleError):
            self.run_cycle(shutdown_event=stop_event)

        self.assertNotIn('validate', self.events)
        self.assertNotIn('import', self.events)
        self.assertEqual([call[0] for call in self.queue.calls].count('retry'), 1)

    def test_stale_claim_during_processing_prevents_validation_and_import(self):
        def lease_lost_process(input_path, limit, max_tokens, before_snapshot):
            before_snapshot()
            keeper = next(item for item in active_keepers)
            keeper.failed = True
            return RUN_ID, self.run_dir, 1, 1
        active_keepers = []

        def heartbeat_factory(queue, claims, lease_duration, interval_seconds):
            keeper = FakeKeeper(queue, claims, lease_duration, interval_seconds, self.events)
            active_keepers.append(keeper)
            return keeper
        self.process_fn = lease_lost_process
        self.heartbeat_factory = heartbeat_factory

        with self.assertRaises(LeaseLostError):
            self.run_cycle()

        self.assertNotIn('validate', self.events)
        self.assertNotIn('import', self.events)

    def test_atomic_import_failure_schedules_retry(self):
        def fail_import(run_directory, claims, db_params):
            self.events.append('import')
            raise ImportJobCompletionError('atomic completion failed')
        self.import_fn = fail_import

        with self.assertRaises(WorkerCycleError):
            self.run_cycle()

        retry = [call for call in self.queue.calls if call[0] == 'retry']
        self.assertEqual(retry[0][3], 'atomic_import_failed')

    def test_worker_rejects_mismatched_validated_snapshot_set(self):
        self.payload['events'] = []

        with self.assertRaises(WorkerCycleError):
            self.run_cycle()

        self.assertNotIn('import', self.events)
        retry = [call for call in self.queue.calls if call[0] == 'retry']
        self.assertEqual(retry[0][3], 'result_mismatch')

    def test_watch_polls_empty_then_successful_cycle(self):
        stop_event = threading.Event()
        calls = []
        waits = []
        logs = []
        summaries = [
            {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
             'import_status': 'skipped_no_jobs'},
            {'run_id': RUN_ID, 'snapshot_count': 1, 'event_count': 2,
             'import_status': 'imported'},
        ]

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            calls.append((limit, max_tokens))
            summary = summaries.pop(0)
            if not summaries:
                shutdown_event.set()
            return summary

        run_watch(
            3, 0.25, max_tokens=7000, api_key='offline-worker-key',
            stop_event=stop_event, cycle_fn=cycle,
            wait_fn=lambda interval: waits.append(interval) or stop_event.is_set(),
            log_fn=logs.append,
        )

        self.assertEqual(calls, [(3, 7000), (3, 7000)])
        self.assertEqual(waits, [0.25])
        self.assertIn('No jobs available', logs[0])
        self.assertIn(RUN_ID, logs[1])

    def test_watch_shutdown_waits_for_current_cycle_then_exits(self):
        stop_event = threading.Event()
        calls = []
        waits = []

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            calls.append('cycle')
            shutdown_event.set()
            return {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                    'import_status': 'skipped_no_jobs'}

        run_watch(
            stop_event=stop_event,
            api_key='offline-worker-key',
            cycle_fn=cycle,
            wait_fn=lambda interval: waits.append(interval) or False,
            log_fn=lambda message: None,
        )

        self.assertEqual(calls, ['cycle'])
        self.assertEqual(waits, [])

    def test_watch_recovers_from_transient_connection_failure(self):
        stop_event = threading.Event()
        calls = []
        logs = []

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            calls.append('cycle')
            if len(calls) == 1:
                raise QueueClientError('private connection detail')
            shutdown_event.set()
            return {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                    'import_status': 'skipped_no_jobs'}

        run_watch(
            stop_event=stop_event, api_key='offline-worker-key', cycle_fn=cycle,
            wait_fn=lambda interval: False, log_fn=logs.append,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn('database_failed', logs[0])
        self.assertNotIn('private connection detail', logs[0])

    def test_watch_continues_after_transient_api_failure(self):
        stop_event = threading.Event()
        calls = []
        logs = []

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            calls.append('cycle')
            if len(calls) == 1:
                raise WorkerCycleError('safe failure summary', 'api_timeout')
            shutdown_event.set()
            return {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                    'import_status': 'skipped_no_jobs'}

        run_watch(
            stop_event=stop_event, api_key='offline-worker-key', cycle_fn=cycle,
            wait_fn=lambda interval: False, log_fn=logs.append,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn('api_timeout', logs[0])
        self.assertNotIn('safe failure summary', logs[0])

    def test_watch_cycles_do_not_overlap(self):
        stop_event = threading.Event()
        state = {'active': False, 'count': 0}

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            self.assertFalse(state['active'])
            state['active'] = True
            try:
                state['count'] += 1
                if state['count'] == 3:
                    shutdown_event.set()
                return {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                        'import_status': 'skipped_no_jobs'}
            finally:
                state['active'] = False

        run_watch(
            stop_event=stop_event, api_key='offline-worker-key', cycle_fn=cycle,
            wait_fn=lambda interval: False, log_fn=lambda message: None,
        )

        self.assertEqual(state['count'], 3)

    def test_watch_loads_api_key_once_and_reuses_it_across_cycles(self):
        stop_event = threading.Event()
        seen_keys = []

        def cycle(limit, max_tokens, shutdown_event, api_key=None):
            seen_keys.append(api_key)
            if len(seen_keys) == 3:
                shutdown_event.set()
            return {'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                    'import_status': 'skipped_no_jobs'}

        with patch.object(process_orvix_csv, 'load_api_key', return_value='offline-key') as load_key:
            run_watch(
                stop_event=stop_event, cycle_fn=cycle,
                wait_fn=lambda interval: False, log_fn=lambda message: None,
            )

        load_key.assert_called_once_with(prompt=False)
        self.assertEqual(seen_keys, ['offline-key'] * 3)

    def test_watch_missing_api_key_fails_before_polling(self):
        from scripts.process_orvix_csv import ApiKeyConfigurationError

        with patch.object(process_orvix_csv, 'load_api_key',
                          side_effect=ApiKeyConfigurationError('key missing')) as load_key, \
                patch('sys.stderr'), \
                patch('scripts.run_orvix_worker.run_worker_cycle') as cycle:
            result = main(['--watch'])

        self.assertEqual(result, 1)
        load_key.assert_called_once_with(prompt=False)
        cycle.assert_not_called()

    def test_interval_must_be_positive_and_finite(self):
        for value in (0, -1, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_watch(interval_seconds=value, stop_event=threading.Event())

    def test_cli_keeps_single_cycle_as_default_and_parses_watch_interval(self):
        with patch('scripts.run_orvix_worker.run_worker_cycle', return_value={
                'run_id': None, 'snapshot_count': 0, 'event_count': 0,
                'import_status': 'skipped_no_jobs'}) as cycle, \
                patch('builtins.print'):
            self.assertEqual(main([]), 0)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.args, (5,))
        self.assertIn('shutdown_event', cycle.call_args.kwargs)

        with patch('scripts.run_orvix_worker.run_watch') as watch:
            self.assertEqual(main(['--watch', '--limit', '2', '--interval', '0.5']), 0)
        self.assertEqual(watch.call_args.args, (2, 0.5))


if __name__ == '__main__':
    unittest.main()
