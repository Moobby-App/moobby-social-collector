import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import process_orvix_csv
from scripts.process_orvix_csv import ApiKeyConfigurationError, load_api_key


class OrvixApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'orvix.env'

    def tearDown(self):
        self.temp.cleanup()

    def _write_private_key(self, value='ORVIX_API_KEY=file-secret\n'):
        self.path.write_text(value, encoding='utf-8')
        self.path.chmod(0o600)

    def test_environment_key_takes_precedence_over_private_file(self):
        self._write_private_key()

        with patch.dict(os.environ, {'ORVIX_API_KEY': ' environment-secret '}):
            key = load_api_key(path=self.path)

        self.assertEqual(key, 'environment-secret')

    def test_private_file_key_is_loaded_without_shell_evaluation(self):
        self._write_private_key("# local secret\nexport ORVIX_API_KEY='file-secret'\n")

        key = load_api_key(env_value='', path=self.path)

        self.assertEqual(key, 'file-secret')

    def test_local_secret_file_requires_exact_0600_permissions(self):
        self._write_private_key()
        self.path.chmod(0o640)

        with self.assertRaisesRegex(ApiKeyConfigurationError, 'exactly 0600'):
            load_api_key(env_value='', path=self.path)

    def test_missing_key_fails_without_prompt_when_unattended(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('scripts.process_orvix_csv.getpass.getpass') as prompt:
            with self.assertRaisesRegex(ApiKeyConfigurationError, 'not configured'):
                load_api_key(prompt=False, path=self.path)

        prompt.assert_not_called()

    def test_single_cycle_prompt_fallback_remains_hidden(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('scripts.process_orvix_csv.getpass.getpass', return_value='prompt-secret') as prompt:
            key = process_orvix_csv.get_api_key()

        self.assertEqual(key, 'prompt-secret')
        prompt.assert_called_once_with('Orvix API key (hidden): ')


if __name__ == '__main__':
    unittest.main()
