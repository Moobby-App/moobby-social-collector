import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from scripts.run_orvix_pipeline import PipelineError, run_pipeline


class OrvixPipelineTests(unittest.TestCase):
    def setUp(self):
        self.db_params = {'host': 'db.example.invalid', 'password': 'not-a-real-secret'}
        self.run_directory = Path('/tmp/orvix-test-run')
        self.failing_stage = None
        self.payload = {
            'run': {'id': 'run-123'},
            'input_snapshot_count': 2,
            'events': [
                {'snapshot_id': 'snapshot-1', 'event_index': 0},
                {'snapshot_id': 'snapshot-2', 'event_index': 1},
                {'snapshot_id': 'snapshot-2', 'event_index': 2},
            ],
        }
        self.events = []
        self.provider = Mock(side_effect=lambda: self._record(
            'credentials', None, return_value=self.db_params
        ))
        self.exporter = Mock(side_effect=lambda limit, db_params, include_processed: self._record(
            'export', (limit, db_params, include_processed), return_value=2
        ))
        self.processor = Mock(side_effect=lambda path, limit, max_tokens: self._record(
            'process', (path, limit, max_tokens), return_value=('run-123', self.run_directory, 2, 3)
        ))
        self.validator = Mock(side_effect=lambda path: self._record(
            'validate', path, return_value=self.payload
        ))
        self.importer = Mock(side_effect=lambda payload, db_params: self._record(
            'import', (payload, db_params), return_value='imported'
        ))

    def _record(self, name, args, return_value=None):
        self.events.append(name)
        if name == self.failing_stage:
            raise RuntimeError('synthetic failure')
        return return_value

    def _run(self, max_tokens=None):
        return run_pipeline(
            2,
            max_tokens=max_tokens,
            db_params_provider=self.provider,
            export_fn=self.exporter,
            process_fn=self.processor,
            validate_fn=self.validator,
            import_fn=self.importer,
        )

    def test_success_runs_stages_in_order_and_reports_counts(self):
        summary = self._run()

        self.assertEqual(self.events, ['credentials', 'export', 'process', 'validate', 'import'])
        self.provider.assert_called_once_with()
        self.exporter.assert_called_once_with(
            2, db_params=self.db_params, include_processed=False
        )
        self.processor.assert_called_once()
        self.assertEqual(self.processor.call_args.kwargs['max_tokens'], 10000)
        self.validator.assert_called_once_with(self.run_directory)
        self.importer.assert_called_once_with(self.payload, self.db_params)
        self.assertEqual(summary, {
            'run_id': 'run-123', 'snapshot_count': 2,
            'event_count': 3, 'import_status': 'imported',
        })

    def test_stops_when_credentials_or_any_stage_fails(self):
        cases = (
            ('credentials', ['credentials']),
            ('export', ['credentials', 'export']),
            ('process', ['credentials', 'export', 'process']),
            ('validate', ['credentials', 'export', 'process', 'validate']),
            ('import', ['credentials', 'export', 'process', 'validate', 'import']),
        )
        for stage, expected_events in cases:
            with self.subTest(stage=stage):
                self.setUp()
                self.failing_stage = stage
                with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                    self._run()
                self.assertEqual(self.events, expected_events)

    def test_rejects_incomplete_snapshot_coverage_before_import(self):
        self.payload['events'] = [{'snapshot_id': 'snapshot-1', 'event_index': 0}]

        with self.assertRaisesRegex(PipelineError, 'every exported snapshot'):
            self._run()

        self.importer.assert_not_called()

    def test_environment_and_explicit_value_reach_processor(self):
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '4321'}):
            self._run()
        self.assertEqual(self.processor.call_args.kwargs['max_tokens'], 4321)

        self.setUp()
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '4321'}):
            self._run(max_tokens=7654)
        self.assertEqual(self.processor.call_args.kwargs['max_tokens'], 7654)

    def test_no_exported_snapshots_skip_processing_validation_and_import(self):
        self.exporter.side_effect = lambda limit, db_params, include_processed: self._record(
            'export', (limit, db_params, include_processed), return_value=0
        )

        summary = self._run()

        self.assertEqual(summary, {
            'run_id': None, 'snapshot_count': 0, 'event_count': 0,
            'import_status': 'skipped_no_snapshots',
        })
        self.processor.assert_not_called()
        self.validator.assert_not_called()
        self.importer.assert_not_called()

    def test_explicit_reprocessing_flag_reaches_exporter(self):
        run_pipeline(
            2, include_processed=True,
            db_params_provider=self.provider,
            export_fn=self.exporter,
            process_fn=self.processor,
            validate_fn=self.validator,
            import_fn=self.importer,
        )

        self.assertTrue(self.exporter.call_args.kwargs['include_processed'])


if __name__ == '__main__':
    unittest.main()
