import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import process_orvix_csv, run_orvix_pipeline
from scripts.process_orvix_csv import MaxTokensError, resolve_max_tokens


class OrvixMaxTokensTests(unittest.TestCase):
    def test_default_environment_cli_precedence_and_invalid_values(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(resolve_max_tokens(), 10000)
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '4321'}):
            self.assertEqual(resolve_max_tokens(), 4321)
            self.assertEqual(resolve_max_tokens(7654), 7654)
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '0'}):
            with self.assertRaises(MaxTokensError):
                resolve_max_tokens()
            self.assertEqual(resolve_max_tokens(7654), 7654)
        for invalid in (0, -1, 'abc', '1.5', True):
            with self.subTest(value=invalid):
                with self.assertRaises(MaxTokensError):
                    resolve_max_tokens(invalid)

    def test_processor_cli_override_is_passed_to_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / 'input.csv'
            input_path.write_text('snapshot_id,raw_text\n', encoding='utf-8')
            run_directory = root / 'run'
            output = io.StringIO()
            with patch.object(process_orvix_csv, 'INPUT_PATH', input_path), \
                    patch.object(process_orvix_csv, 'ROOT', root), \
                    patch.object(process_orvix_csv, 'execute_run', return_value=(
                        'run-id', run_directory, 1, 1
                    )) as execute, \
                    contextlib.redirect_stdout(output):
                result = process_orvix_csv.main(['--max-tokens', '7654'])

        self.assertEqual(result, 0)
        execute.assert_called_once_with(input_path, 1, max_tokens=7654)

    def test_pipeline_cli_override_is_passed_to_pipeline(self):
        output = io.StringIO()
        summary = {
            'run_id': 'run-id', 'snapshot_count': 5,
            'event_count': 7, 'import_status': 'imported',
        }
        with patch.object(run_orvix_pipeline, 'run_pipeline', return_value=summary) as run, \
                contextlib.redirect_stdout(output):
            result = run_orvix_pipeline.main(['--max-tokens', '7654', '--include-processed'])

        self.assertEqual(result, 0)
        run.assert_called_once_with(5, max_tokens=7654, include_processed=True)


if __name__ == '__main__':
    unittest.main()
