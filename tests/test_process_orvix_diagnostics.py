import csv
import contextlib
import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import openai

from scripts.process_orvix_csv import ProcessingError, execute_run, main


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request_kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.request_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        pass


class FakeAuthenticationError(openai.AuthenticationError):
    def __init__(self):
        Exception.__init__(self, 'synthetic private authentication detail')


class FakeUnsupportedParameterError(openai.BadRequestError):
    def __init__(self):
        Exception.__init__(self, 'synthetic private provider error')
        self.param = 'reasoning_effort'


class FakeBadRequestError(openai.BadRequestError):
    def __init__(self):
        Exception.__init__(self, 'synthetic private provider error')
        self.param = 'response_format'


class OrvixFailureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'raw_snapshots.csv'
        with self.source.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('snapshot_id', 'raw_text'))
            writer.writeheader()
            writer.writerow({'snapshot_id': 'snapshot-test', 'raw_text': 'synthetic source post'})
        self.runs_path = self.root / 'data/orvix/runs'

    def tearDown(self):
        self.temp.cleanup()

    def _run_failure(self, client, max_tokens=None):
        with self.assertRaises(ProcessingError) as caught:
            execute_run(
                self.source, 1, client_factory=lambda: client,
                runs_path=self.runs_path, max_tokens=max_tokens,
            )
        run_dir = caught.exception.failure_manifest_path.parent
        manifest = json.loads((run_dir / 'processing_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'failed')
        self.assertIsNone(manifest['completed_at'])
        self.assertIsNone(manifest['output_csv']['sha256'])
        self.assertFalse((run_dir / 'processed_events.csv').exists())
        return caught.exception, manifest

    def _response(self, content, finish_reason='stop', refusal=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )],
            usage=SimpleNamespace(
                prompt_tokens=41,
                completion_tokens=9,
                total_tokens=50,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
            ),
        )

    def test_empty_content_records_safe_response_metadata(self):
        error, manifest = self._run_failure(FakeClient(response=self._response(None)))

        self.assertEqual(error.failure_code, 'empty_response')
        self.assertIn('no usable message content', str(error))
        self.assertEqual(manifest['failure_code'], 'empty_response')
        diagnostic = manifest['response_diagnostics'][0]
        self.assertEqual(diagnostic['choices_count'], 1)
        self.assertEqual(diagnostic['finish_reason'], 'stop')
        self.assertFalse(diagnostic['content_present'])
        self.assertFalse(diagnostic['content_nonempty'])
        self.assertFalse(diagnostic['refusal_present'])
        self.assertEqual(diagnostic['token_usage'], {
            'prompt_tokens': 41, 'completion_tokens': 9,
            'total_tokens': 50, 'reasoning_tokens': 3,
        })

    def test_length_finish_reason_is_classified_as_truncated(self):
        client = FakeClient(response=self._response('{"events":', finish_reason='length'))
        error, manifest = self._run_failure(
            client
        )

        self.assertEqual(client.request_kwargs['max_tokens'], 10000)
        self.assertEqual(client.request_kwargs['reasoning_effort'], 'low')
        self.assertEqual(error.failure_code, 'truncated_response')
        self.assertEqual(manifest['failure_code'], 'truncated_response')
        self.assertEqual(manifest['response_diagnostics'][0]['finish_reason'], 'length')

    def test_environment_and_override_values_reach_chat_request_and_manifest(self):
        client = FakeClient(response=self._response('{"events":', finish_reason='length'))
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '4321'}):
            _, manifest = self._run_failure(client)
        self.assertEqual(client.request_kwargs['max_tokens'], 4321)
        self.assertEqual(manifest['max_tokens'], 4321)

        client = FakeClient(response=self._response('{"events":', finish_reason='length'))
        with patch.dict('os.environ', {'ORVIX_MAX_TOKENS': '4321'}):
            _, manifest = self._run_failure(client, max_tokens=7654)
        self.assertEqual(client.request_kwargs['max_tokens'], 7654)
        self.assertEqual(manifest['max_tokens'], 7654)

    def test_unsupported_reasoning_effort_is_distinct_from_other_api_errors(self):
        error, manifest = self._run_failure(FakeClient(error=FakeUnsupportedParameterError()))

        self.assertEqual(error.failure_code, 'unsupported_parameter')
        self.assertIn('rejected reasoning_effort', str(error))
        self.assertEqual(manifest['failure_code'], 'unsupported_parameter')
        self.assertNotIn('synthetic private provider error', json.dumps(manifest))

        error, manifest = self._run_failure(FakeClient(error=FakeBadRequestError()))
        self.assertEqual(error.failure_code, 'api_error')
        self.assertEqual(manifest['failure_code'], 'api_error')

    def test_authentication_error_is_actionable_and_does_not_persist_exception_text(self):
        error, manifest = self._run_failure(FakeClient(error=FakeAuthenticationError()))

        self.assertEqual(error.failure_code, 'authentication_error')
        self.assertIn('valid and has ai:invoke scope', str(error))
        self.assertEqual(manifest['failure_code'], 'authentication_error')
        self.assertNotIn('synthetic private authentication detail', json.dumps(manifest))
        self.assertNotIn('response_diagnostics', manifest)

    def test_terminal_summary_points_to_safe_failed_manifest(self):
        output = io.StringIO()
        with patch('scripts.process_orvix_csv.INPUT_PATH', self.source), \
                patch('scripts.process_orvix_csv.RUNS_PATH', self.runs_path), \
                patch('scripts.process_orvix_csv.ROOT', self.root), \
                patch('scripts.process_orvix_csv.create_orvix_client',
                      return_value=FakeClient(error=FakeAuthenticationError())):
            with contextlib.redirect_stderr(output):
                result = main([])

        self.assertEqual(result, 1)
        self.assertIn('authentication_error', output.getvalue())
        self.assertIn('Safe diagnostics: data/orvix/runs/', output.getvalue())
        self.assertIn('processing_manifest.json', output.getvalue())
        self.assertNotIn('synthetic private authentication detail', output.getvalue())

    def test_invalid_json_records_metadata_without_response_content(self):
        marker = 'synthetic private response content'
        error, manifest = self._run_failure(FakeClient(response=self._response(marker)))

        self.assertEqual(error.failure_code, 'invalid_json')
        self.assertEqual(manifest['failure_code'], 'invalid_json')
        self.assertTrue(manifest['response_diagnostics'][0]['content_present'])
        self.assertTrue(manifest['response_diagnostics'][0]['content_nonempty'])
        self.assertNotIn(marker, json.dumps(manifest))

    def test_empty_choices_and_reasoning_only_content_are_handled(self):
        no_choices = SimpleNamespace(choices=[], usage=None)
        error, manifest = self._run_failure(FakeClient(response=no_choices))
        self.assertEqual(error.failure_code, 'empty_response')
        self.assertEqual(manifest['response_diagnostics'][0]['choices_count'], 0)

        reasoning_only = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason='stop',
                message=SimpleNamespace(content=None, refusal=None, reasoning='not persisted'),
            )],
            usage=None,
        )
        error, manifest = self._run_failure(FakeClient(response=reasoning_only))
        self.assertEqual(error.failure_code, 'empty_response')
        self.assertNotIn('not persisted', json.dumps(manifest))


if __name__ == '__main__':
    unittest.main()
