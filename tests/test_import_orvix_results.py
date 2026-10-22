import contextlib
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from scripts.export_orvix_input import CSV_FIELDS as INPUT_FIELDS
from scripts.process_orvix_csv import OUTPUT_FIELDS, PROMPT_VERSION
from scripts.import_orvix_results import (
    EVENT_COLUMNS,
    RUN_COLUMNS,
    ImportConflictError,
    ImportJobCompletionError,
    ImportValidationError,
    compare_existing_import,
    import_claimed_payload,
    main,
    validate_run,
)


SNAPSHOT_ID = str(uuid.UUID('e60acb2c-4a7e-40fb-8b60-0d2b48702022'))
RUN_ID = str(uuid.UUID('53443f16-c455-4107-8efb-27c7f5bbdfe5'))
JOB_ID = str(uuid.UUID('d2f22373-f2e6-4e42-9b2c-4231102fe6b0'))
CLAIM_TOKEN = str(uuid.UUID('01c58184-4ce4-4c41-b585-3ef038687bc7'))
RAW_TEXT = 'KRL Jabodetabek beroperasi normal hari ini.'


def _csv_bytes(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def input_row(snapshot_id=SNAPSHOT_ID, raw_text=RAW_TEXT):
    row = {field: '' for field in INPUT_FIELDS}
    row.update({'snapshot_id': snapshot_id, 'raw_text': raw_text})
    return row


def output_row(snapshot_id=SNAPSHOT_ID, **updates):
    row = {field: '' for field in OUTPUT_FIELDS}
    row.update({
        'schema_version': PROMPT_VERSION,
        'snapshot_id': snapshot_id,
        'event_index': '0',
        'event_status': 'no_disruption',
        'disruption_type': 'not_applicable',
        'disruption_type_evidence': 'not_applicable',
        'affected_entities_json': '[]',
        'severity': 'not_applicable',
        'severity_evidence': 'not_applicable',
        'event_start_time': '',
        'event_start_time_evidence': 'not_applicable',
        'event_end_time': '',
        'event_end_time_evidence': 'not_applicable',
        'uncertainty_status': 'no',
        'uncertainty_note': '',
        'explanation': 'The text describes normal service.',
        'evidence_quotes_json': '[]',
        'model': 'orvix/deepseek-v4.1-flash',
        'processor_version': '1.0.0',
        'prompt_version': PROMPT_VERSION,
        'review_status': 'pending',
        'review_notes': '',
    })
    row.update(updates)
    return row


class FakeDbResult:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeImportConnection:
    """Minimal transaction and SQL recorder for offline importer tests."""

    def __init__(self, payload, existing=False, completion_results=None, job_rows=None):
        self.payload = payload
        self.existing = existing
        self.completion_results = list(completion_results or [])
        self.job_rows = list(job_rows or [])
        self.calls = []
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if 'SELECT current_user AS role_name' in query:
            return FakeDbResult({'role_name': 'moobby_orvix_worker_login'})
        if 'INSERT INTO processing.processing_runs' in query:
            return FakeDbResult(None if self.existing else {'id': self.payload['run']['id']})
        if 'SELECT manifest_version,manifest_status' in query:
            return FakeDbResult({key: self.payload['run'][key] for key in RUN_COLUMNS})
        if 'SELECT snapshot_id,status,processing_run_id' in query:
            return FakeDbResult(self.job_rows.pop(0))
        if query.startswith('SELECT') and 'FROM processing.events WHERE run_id=' in query:
            rows = [{key: event[key] for key in EVENT_COLUMNS} for event in self.payload['events']]
            return FakeDbResult(rows=rows)
        if 'LEFT JOIN processing.event_reviews' in query:
            rows = [{
                'snapshot_id': event['snapshot_id'], 'event_index': event['event_index'],
                'review_status': 'pending', 'review_notes': '', 'corrections': {},
            } for event in self.payload['events']]
            return FakeDbResult(rows=rows)
        if 'INSERT INTO processing.events' in query:
            return FakeDbResult({'id': uuid.uuid4()})
        if 'processing.complete_job' in query:
            completed = self.completion_results.pop(0)
            return FakeDbResult({'completed': completed})
        return FakeDbResult()


class OrvixImportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name) / RUN_ID
        self.run_dir.mkdir()
        self.input_bytes = _csv_bytes(INPUT_FIELDS, [input_row()])
        self.output_bytes = _csv_bytes(OUTPUT_FIELDS, [output_row()])
        (self.run_dir / 'input.csv').write_bytes(self.input_bytes)
        (self.run_dir / 'processed_events.csv').write_bytes(self.output_bytes)
        manifest = {
            'manifest_version': 'orvix.processing_manifest.v1',
            'run_id': RUN_ID,
            'status': 'completed',
            'started_at': '2026-10-22T06:22:12.272456+00:00',
            'completed_at': '2026-10-22T06:22:24.798398+00:00',
            'model': 'orvix/deepseek-v4.1-flash',
            'schema_version': PROMPT_VERSION,
            'processor_version': '1.0.0',
            'prompt_version': PROMPT_VERSION,
            'input_csv': {'path': 'input.csv', 'sha256': hashlib.sha256(self.input_bytes).hexdigest()},
            'output_csv': {'path': 'processed_events.csv', 'sha256': hashlib.sha256(self.output_bytes).hexdigest()},
        }
        (self.run_dir / 'processing_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def _rewrite_output(self, rows):
        self.output_bytes = _csv_bytes(OUTPUT_FIELDS, rows)
        (self.run_dir / 'processed_events.csv').write_bytes(self.output_bytes)
        manifest_path = self.run_dir / 'processing_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['output_csv']['sha256'] = hashlib.sha256(self.output_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    def test_successful_validation_maps_manifest_and_event_fields(self):
        payload = validate_run(self.run_dir)

        self.assertEqual(payload['run']['id'], uuid.UUID(RUN_ID))
        self.assertEqual(payload['run']['input_csv_sha256'], hashlib.sha256(self.input_bytes).hexdigest())
        self.assertEqual(payload['run']['output_csv_sha256'], hashlib.sha256(self.output_bytes).hexdigest())
        self.assertEqual(payload['input_snapshot_count'], 1)
        self.assertEqual(len(payload['events']), 1)
        self.assertEqual(payload['events'][0]['event_index'], 0)
        self.assertEqual(payload['events'][0]['event_status'], 'no_disruption')
        self.assertEqual(payload['events'][0]['affected_entities'], [])
        self.assertIsNone(payload['events'][0]['event_start_time'])

    def test_optional_max_tokens_manifest_metadata_is_validated(self):
        manifest_path = self.run_dir / 'processing_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['max_tokens'] = 10000
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        validate_run(self.run_dir)

        manifest['max_tokens'] = 0
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ImportValidationError):
            validate_run(self.run_dir)

    def test_valid_disruption_parses_json_and_timezone_timestamps(self):
        event = output_row(
            event_index='1', event_status='potential_disruption',
            disruption_type='delay', disruption_type_evidence='explicit',
            affected_entities_json=json.dumps([{
                'entity_type': 'line', 'value': 'KRL', 'evidence_type': 'explicit',
                'evidence_quote': 'KRL Jabodetabek',
            }]),
            severity='unknown', severity_evidence='unknown',
            event_start_time='2026-10-22T10:00:00+07:00',
            event_start_time_evidence='explicit',
            event_end_time_evidence='unknown',
            uncertainty_status='yes', uncertainty_note='The end time is not stated.',
            evidence_quotes_json=json.dumps(['beroperasi normal']),
        )
        self._rewrite_output([event])

        payload = validate_run(self.run_dir)
        parsed = payload['events'][0]
        self.assertEqual(parsed['event_index'], 1)
        self.assertEqual(parsed['affected_entities'][0]['value'], 'KRL')
        self.assertEqual(parsed['evidence_quotes'], ['beroperasi normal'])
        self.assertEqual(parsed['event_start_time'].utcoffset().total_seconds(), 7 * 60 * 60)

    def test_validate_only_never_requests_database_credentials(self):
        output = io.StringIO()
        with patch('scripts.import_orvix_results.connection_parameters', side_effect=AssertionError('called')):
            with contextlib.redirect_stdout(output):
                result = main([str(self.run_dir), '--validate-only'])
        self.assertEqual(result, 0)
        self.assertIn('validated run', output.getvalue())

    def test_malformed_enum_and_time_values_are_rejected(self):
        self._rewrite_output([output_row(disruption_type='invalid')])
        with self.assertRaises(ImportValidationError):
            validate_run(self.run_dir)

        self._rewrite_output([output_row(event_start_time='2026-10-22', event_start_time_evidence='explicit')])
        with self.assertRaises(ImportValidationError):
            validate_run(self.run_dir)

    def test_non_completed_manifest_is_rejected(self):
        manifest_path = self.run_dir / 'processing_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['status'] = 'processing'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ImportValidationError):
            validate_run(self.run_dir)

    def test_empty_completed_run_is_valid_only_with_empty_input(self):
        self.input_bytes = _csv_bytes(INPUT_FIELDS, [])
        (self.run_dir / 'input.csv').write_bytes(self.input_bytes)
        manifest_path = self.run_dir / 'processing_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['input_csv']['sha256'] = hashlib.sha256(self.input_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        self._rewrite_output([])

        payload = validate_run(self.run_dir)
        self.assertEqual(payload['input_snapshot_count'], 0)
        self.assertEqual(payload['events'], [])

    def test_hash_mismatch_is_rejected(self):
        (self.run_dir / 'input.csv').write_bytes(self.input_bytes + b' ')
        with self.assertRaisesRegex(ImportValidationError, 'SHA-256'):
            validate_run(self.run_dir)

    def test_duplicate_snapshot_event_identity_is_rejected(self):
        self._rewrite_output([output_row(), output_row()])
        with self.assertRaisesRegex(ImportValidationError, 'duplicate snapshot/event'):
            validate_run(self.run_dir)

    def test_duplicate_input_snapshot_identity_is_rejected(self):
        self.input_bytes = _csv_bytes(INPUT_FIELDS, [input_row(), input_row()])
        (self.run_dir / 'input.csv').write_bytes(self.input_bytes)
        manifest_path = self.run_dir / 'processing_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['input_csv']['sha256'] = hashlib.sha256(self.input_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ImportValidationError, 'duplicate snapshot identities'):
            validate_run(self.run_dir)

    def test_evidence_quote_must_match_original_input_text(self):
        self._rewrite_output([output_row(
            event_index='1', event_status='potential_disruption',
            disruption_type='delay', disruption_type_evidence='explicit',
            affected_entities_json='[]', severity='unknown', severity_evidence='unknown',
            event_start_time_evidence='unknown', event_end_time_evidence='unknown',
            uncertainty_status='yes', uncertainty_note='The text is uncertain.',
            evidence_quotes_json='["The train is delayed"]',
        )])
        with self.assertRaisesRegex(ImportValidationError, 'not present'):
            validate_run(self.run_dir)

    def test_identical_retry_is_idempotent_and_changed_retry_conflicts(self):
        payload = validate_run(self.run_dir)
        event = payload['events'][0]
        existing_run = {key: payload['run'][key] for key in RUN_COLUMNS}
        existing_events = [{key: event[key] for key in EVENT_COLUMNS}]
        existing_reviews = [{
            'snapshot_id': event['snapshot_id'], 'event_index': event['event_index'],
            'review_status': 'pending', 'review_notes': '', 'corrections': {},
        }]

        self.assertIsNone(compare_existing_import(existing_run, existing_events, existing_reviews, payload))
        conflicting = dict(existing_events[0], explanation='changed output')
        with self.assertRaises(ImportConflictError):
            compare_existing_import(existing_run, [conflicting], existing_reviews, payload)

    def test_worker_import_inserts_and_completes_in_one_transaction(self):
        payload = validate_run(self.run_dir)
        claim = {'job_id': JOB_ID, 'snapshot_id': SNAPSHOT_ID, 'claim_token': CLAIM_TOKEN}
        connection = FakeImportConnection(
            payload,
            completion_results=[True],
            job_rows=[{'snapshot_id': uuid.UUID(SNAPSHOT_ID), 'status': 'claimed',
                       'processing_run_id': None}],
        )

        with patch('psycopg.connect', return_value=connection):
            result = import_claimed_payload(payload, {'user': 'worker'}, [claim])

        self.assertEqual(result, 'imported')
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        query_text = [query for query, _params in connection.calls]
        insert_event = next(i for i, query in enumerate(query_text) if 'INSERT INTO processing.events' in query)
        insert_review = next(i for i, query in enumerate(query_text) if 'INSERT INTO processing.event_reviews' in query)
        complete = next(i for i, query in enumerate(query_text) if 'processing.complete_job' in query)
        self.assertLess(insert_event, insert_review)
        self.assertLess(insert_review, complete)
        self.assertEqual(sum('processing.complete_job' in query for query in query_text), 1)

    def test_stale_claim_token_rolls_back_new_processing_rows(self):
        payload = validate_run(self.run_dir)
        claim = {'job_id': JOB_ID, 'snapshot_id': SNAPSHOT_ID, 'claim_token': CLAIM_TOKEN}
        connection = FakeImportConnection(
            payload,
            completion_results=[False],
            job_rows=[{'snapshot_id': uuid.UUID(SNAPSHOT_ID), 'status': 'claimed',
                       'processing_run_id': None}],
        )

        with patch('psycopg.connect', return_value=connection):
            with self.assertRaises(ImportJobCompletionError):
                import_claimed_payload(payload, {'user': 'worker'}, [claim])

        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)

    def test_partial_completion_failure_rolls_back_entire_batch(self):
        payload = validate_run(self.run_dir)
        second_snapshot = uuid.UUID('ecdaefb4-60b2-4f1f-8a66-95a74645b148')
        second_event = copy.deepcopy(payload['events'][0])
        second_event['snapshot_id'] = second_snapshot
        payload['events'].append(second_event)
        payload['input_snapshot_ids'].append(second_snapshot)
        payload['input_snapshot_count'] = 2
        claims = [
            {'job_id': JOB_ID, 'snapshot_id': SNAPSHOT_ID, 'claim_token': CLAIM_TOKEN},
            {'job_id': '9f62a7b4-2b80-4c1e-9cbb-8fd15f3b5eea',
             'snapshot_id': str(second_snapshot),
             'claim_token': 'fdde472b-0d38-448a-9d79-19f606e840ac'},
        ]
        job_rows = [
            {'snapshot_id': uuid.UUID(SNAPSHOT_ID), 'status': 'claimed', 'processing_run_id': None},
            {'snapshot_id': second_snapshot, 'status': 'claimed', 'processing_run_id': None},
        ]
        connection = FakeImportConnection(
            payload, completion_results=[True, False], job_rows=job_rows,
        )

        with patch('psycopg.connect', return_value=connection):
            with self.assertRaises(ImportJobCompletionError):
                import_claimed_payload(payload, {'user': 'worker'}, claims)

        self.assertTrue(connection.rolled_back)
        self.assertEqual(sum('processing.complete_job' in query for query, _ in connection.calls), 2)

    def test_identical_worker_retry_does_not_duplicate_events_or_reviews(self):
        payload = validate_run(self.run_dir)
        claim = {'job_id': JOB_ID, 'snapshot_id': SNAPSHOT_ID, 'claim_token': CLAIM_TOKEN}
        connection = FakeImportConnection(
            payload,
            existing=True,
            job_rows=[{'snapshot_id': uuid.UUID(SNAPSHOT_ID), 'status': 'completed',
                       'processing_run_id': uuid.UUID(RUN_ID)}],
        )

        with patch('psycopg.connect', return_value=connection):
            result = import_claimed_payload(payload, {'user': 'worker'}, [claim])

        self.assertEqual(result, 'already_imported')
        self.assertTrue(connection.committed)
        self.assertFalse(any(
            'INSERT INTO processing.events' in query or 'INSERT INTO processing.event_reviews' in query
            for query, _params in connection.calls
        ))
        self.assertFalse(any('processing.complete_job' in query for query, _params in connection.calls))

    def test_worker_import_rejects_missing_result_for_claimed_snapshot(self):
        payload = validate_run(self.run_dir)
        payload['events'] = []
        claim = {'job_id': JOB_ID, 'snapshot_id': SNAPSHOT_ID, 'claim_token': CLAIM_TOKEN}
        with self.assertRaises(ImportJobCompletionError):
            import_claimed_payload(payload, {'user': 'worker'}, [claim])


if __name__ == '__main__':
    unittest.main()
