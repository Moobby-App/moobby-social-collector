import csv
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import export_orvix_input
from scripts.export_orvix_input import CSV_FIELDS, csv_row, deduplicate_rows, write_csv


def make_record(raw_text='Train service update', snapshot_id='snapshot-1', provenance=None):
    return {
        'snapshot_id': snapshot_id,
        'raw_content_id': 'content-1',
        'platform': 'x',
        'external_id': 'post-123',
        'canonical_url': 'https://x.com/example/status/123',
        'author_handle': 'example',
        'raw_text': raw_text,
        'text_kind': 'post_text',
        'text_completeness': 'full',
        'published_at': datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        'published_at_basis': 'dom_datetime',
        'captured_at': datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
        'capture_hash': 'a' * 64,
        'extraction_metadata': {'source': 'dom'},
        'observation_provenance': provenance or [],
    }


class OrvixInputExportTests(unittest.TestCase):
    def _export_with_fake_database(self, records, include_processed=False):
        statements = []

        class Cursor:
            def fetchone(self):
                return (1,)

            def fetchall(self):
                return records

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params=None):
                statements.append((statement, params))
                return Cursor()

        psycopg = SimpleNamespace(connect=Mock(return_value=Connection()))
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / 'raw_snapshots.csv'
            with patch.dict(sys.modules, {'psycopg': psycopg}), \
                    patch.object(export_orvix_input, 'OUTPUT_PATH', output_path):
                count = export_orvix_input.export(
                    2, db_params={'host': 'offline.test'}, include_processed=include_processed
                )
                output = output_path.read_text(encoding='utf-8')
        return count, statements, output

    def test_unprocessed_query_filters_imported_completed_runs_before_limit(self):
        count, statements, _ = self._export_with_fake_database([make_record()])
        query, params = next((sql, params) for sql, params in statements if 'SELECT\n    s.id' in sql)

        self.assertEqual(count, 1)
        self.assertEqual(params, (2,))
        self.assertIn('NOT EXISTS', query)
        self.assertIn('FROM processing.events AS e', query)
        self.assertIn('JOIN processing.processing_runs AS pr ON pr.id = e.run_id', query)
        self.assertIn("pr.manifest_status = 'completed'", query)
        self.assertIn('pr.imported_at IS NOT NULL', query)
        filter_position = query.index('WHERE NOT EXISTS')
        ordering_position = query.rindex('ORDER BY s.captured_at DESC, s.id ASC')
        limit_position = query.index('LIMIT %s')
        self.assertLess(filter_position, ordering_position)
        self.assertLess(ordering_position, limit_position)

    def test_selection_order_is_deterministic(self):
        _, statements, _ = self._export_with_fake_database([])
        query = next(sql for sql, _ in statements if 'SELECT\n    s.id' in sql)

        self.assertIn('ORDER BY s.captured_at DESC, s.id ASC', query)

    def test_explicit_include_processed_disables_filter(self):
        _, statements, _ = self._export_with_fake_database([], include_processed=True)
        query = next(sql for sql, _ in statements if 'SELECT\n    s.id' in sql)

        self.assertNotIn('NOT EXISTS', query)
        self.assertIn('ORDER BY s.captured_at DESC, s.id ASC', query)

    def test_claimed_snapshot_export_uses_only_claim_ids_and_requested_path(self):
        records = [make_record(snapshot_id='snapshot-claimed')]
        statements = []

        class Cursor:
            def fetchone(self):
                return (1,)

            def fetchall(self):
                return records

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params=None):
                statements.append((statement, params))
                return Cursor()

        snapshot_id = 'e60acb2c-4a7e-40fb-8b60-0d2b48702022'
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'claim-input.csv'
            psycopg = SimpleNamespace(connect=Mock(return_value=Connection()))
            with patch.dict(sys.modules, {'psycopg': psycopg}):
                count = export_orvix_input.export(
                    1, db_params={'host': 'offline.test'}, snapshot_ids=[snapshot_id],
                    output_path=destination,
                )

            query, params = next(
                (sql, params) for sql, params in statements if 'SELECT\n    s.id' in sql
            )
            self.assertEqual(count, 1)
            self.assertEqual(params, ([snapshot_id], 1))
            self.assertIn('WHERE s.id = ANY(%s::uuid[])', query)
            self.assertNotIn('FROM processing.events AS e', query)
            with destination.open(encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['snapshot_id'], 'snapshot-claimed')

    def test_empty_selection_writes_header_only_csv(self):
        count, _, output = self._export_with_fake_database([])

        self.assertEqual(count, 0)
        self.assertEqual(next(csv.reader(io.StringIO(output))), list(CSV_FIELDS))

    def test_export_cli_can_explicitly_include_processed_snapshots(self):
        with patch.object(export_orvix_input, 'export', return_value=2) as export, \
                patch('sys.stdout', new_callable=io.StringIO):
            result = export_orvix_input.main(['--limit', '3', '--include-processed'])

        self.assertEqual(result, 0)
        export.assert_called_once_with(3, include_processed=True)

    def test_csv_field_mapping_and_raw_text_are_preserved(self):
        record = make_record('  line one\nline two  ')

        output = csv_row(record)

        self.assertEqual(tuple(output), CSV_FIELDS)
        self.assertEqual(output['snapshot_id'], 'snapshot-1')
        self.assertEqual(output['raw_content_id'], 'content-1')
        self.assertEqual(output['external_id'], 'post-123')
        self.assertEqual(output['raw_text'], '  line one\nline two  ')
        self.assertEqual(output['published_at'], '2026-01-02T03:04:00+00:00')
        self.assertEqual(output['extraction_metadata_json'], '{"source":"dom"}')

    def test_duplicate_snapshot_rows_merge_provenance_once(self):
        first = make_record(provenance=[{'run_id': 'run-1', 'observed_at': '2026-01-01'}])
        repeated = make_record(provenance=[{'run_id': 'run-2', 'observed_at': '2026-01-02'}])

        rows = deduplicate_rows([first, repeated])

        self.assertEqual(len(rows), 1)
        self.assertEqual([item['run_id'] for item in rows[0]['observation_provenance']], ['run-1', 'run-2'])

    def test_multiline_text_round_trips_through_utf8_csv(self):
        text = 'Disruption update, first line\nSecond line with "quoted text"\n'
        stream = io.StringIO(newline='')

        count = write_csv(stream, [make_record(text)])

        self.assertEqual(count, 1)
        stream.seek(0)
        result = next(csv.DictReader(stream))
        self.assertEqual(result['raw_text'], text)
        self.assertIn('"Disruption update, first line\nSecond line with ""quoted text""\n"', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
