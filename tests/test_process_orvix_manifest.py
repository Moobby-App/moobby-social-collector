import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from scripts.process_orvix_csv import (
    MANIFEST_VERSION,
    MODEL,
    OUTPUT_FIELDS,
    PROCESSOR_VERSION,
    PROMPT_VERSION,
    ProcessingError,
    execute_run,
)


INPUT_BYTES = b'snapshot_id,raw_text\r\nsnapshot-1,"First line\r\nSecond line"\r\n'


class FakeClient:
    def close(self):
        pass


def output_row(snapshot_id, explanation='validated event'):
    row = {field: '' for field in OUTPUT_FIELDS}
    row.update({
        'schema_version': PROMPT_VERSION,
        'snapshot_id': snapshot_id,
        'event_index': 1,
        'event_status': 'potential_disruption',
        'disruption_type': 'delay',
        'explanation': explanation,
        'model': MODEL,
        'processor_version': PROCESSOR_VERSION,
        'prompt_version': PROMPT_VERSION,
        'review_status': 'pending',
        'review_notes': '',
    })
    return row


class OrvixManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / 'raw_snapshots.csv'
        self.source.write_bytes(INPUT_BYTES)
        self.runs_path = self.root / 'runs'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_manifest_lifecycle_and_file_hashes(self):
        observed_initial_state = []
        lease_checks = []

        def client_factory():
            run_dirs = list(self.runs_path.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            manifest = json.loads((run_dir / 'processing_manifest.json').read_text())
            self.assertEqual(manifest['status'], 'processing')
            self.assertIsNone(manifest['completed_at'])
            self.assertIsNone(manifest['output_csv']['sha256'])
            self.assertEqual((run_dir / 'input.csv').read_bytes(), INPUT_BYTES)
            observed_initial_state.append(manifest)
            return FakeClient()

        run_id, run_dir, snapshot_count, event_count = execute_run(
            self.source,
            1,
            client_factory=client_factory,
            processor=lambda client, row: [output_row(row['snapshot_id'])],
            runs_path=self.runs_path,
            before_snapshot=lambda: lease_checks.append('checked'),
        )

        manifest = json.loads((run_dir / 'processing_manifest.json').read_text())
        self.assertEqual(len(observed_initial_state), 1)
        self.assertEqual(lease_checks, ['checked'])
        self.assertEqual(str(uuid.UUID(run_id)), run_id)
        self.assertEqual(manifest['manifest_version'], MANIFEST_VERSION)
        self.assertEqual(manifest['run_id'], run_id)
        self.assertEqual(manifest['status'], 'completed')
        self.assertIsNotNone(manifest['started_at'])
        self.assertIsNotNone(manifest['completed_at'])
        self.assertEqual(manifest['model'], MODEL)
        self.assertEqual(manifest['processor_version'], PROCESSOR_VERSION)
        self.assertEqual(manifest['prompt_version'], PROMPT_VERSION)
        self.assertEqual(manifest['schema_version'], PROMPT_VERSION)
        self.assertEqual(manifest['max_tokens'], 10000)
        self.assertEqual(manifest['input_csv']['sha256'], hashlib.sha256(INPUT_BYTES).hexdigest())
        self.assertEqual((run_dir / 'input.csv').read_bytes(), INPUT_BYTES)
        self.assertEqual(
            manifest['output_csv']['sha256'],
            hashlib.sha256((run_dir / 'processed_events.csv').read_bytes()).hexdigest(),
        )
        self.assertEqual((snapshot_count, event_count), (1, 1))

    def test_failed_processing_is_never_import_ready(self):
        def fail(client, row):
            raise ProcessingError('invalid response')

        with self.assertRaises(ProcessingError):
            execute_run(
                self.source,
                1,
                client_factory=FakeClient,
                processor=fail,
                runs_path=self.runs_path,
            )

        run_dir = next(self.runs_path.iterdir())
        manifest = json.loads((run_dir / 'processing_manifest.json').read_text())
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['max_tokens'], 10000)
        self.assertIsNone(manifest['completed_at'])
        self.assertIsNone(manifest['output_csv']['sha256'])
        self.assertFalse((run_dir / 'processed_events.csv').exists())
        self.assertEqual((run_dir / 'input.csv').read_bytes(), INPUT_BYTES)

    def test_new_execution_does_not_change_previous_run_artifacts(self):
        processor = lambda client, row: [output_row(row['snapshot_id'])]
        first_id, first_dir, _, _ = execute_run(
            self.source, 1, client_factory=FakeClient, processor=processor, runs_path=self.runs_path
        )
        first_before = {
            path.name: path.read_bytes()
            for path in first_dir.iterdir()
        }

        second_id, second_dir, _, _ = execute_run(
            self.source, 1, client_factory=FakeClient, processor=processor, runs_path=self.runs_path
        )

        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first_dir, second_dir)
        self.assertEqual({path.name: path.read_bytes() for path in first_dir.iterdir()}, first_before)
        with (second_dir / 'processed_events.csv').open(encoding='utf-8', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['snapshot_id'], 'snapshot-1')


if __name__ == '__main__':
    unittest.main()
