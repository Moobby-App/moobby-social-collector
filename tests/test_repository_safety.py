import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_repository import forbidden_path, has_credential


class RepositorySafetyTests(unittest.TestCase):
    def test_generated_and_private_paths_are_blocked(self):
        for name in ['data/raw_batches/batch.json', 'playwright/.auth/x.json', '.env.local',
                     'nested/x_reused_session.json', 'debug_x.png', '.venv/bin/python']:
            self.assertTrue(forbidden_path(name), name)

    def test_code_schema_and_docs_are_shareable(self):
        for name in ['raw_batch.schema.json', 'db/001_collection_schema.sql',
                     'README.md', 'test_x_existing_session.py', '.env.example']:
            self.assertFalse(forbidden_path(name), name)

    def test_database_password_is_detected_without_blocking_placeholder(self):
        template = 'postgresql://collector.ref:'
        self.assertTrue(has_credential(template + 'not-a-real-' + 'credential@host/postgres'))
        self.assertFalse(has_credential(template + '[YOUR-PASSWORD]@host/postgres'))

    def test_token_pattern_is_detected(self):
        self.assertTrue(has_credential('ghp_' + 'A' * 35))
        self.assertTrue(has_credential('sb_secret_' + 'A' * 25))
        self.assertFalse(has_credential('auth_token = getpass("Cookie: ")'))


if __name__ == '__main__':
    unittest.main()
