import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from greenline_upload import connection_params, process_queue, read_batches, save_private
from prepare_greenline_batch import build_batches
from test_greenline_batch import fixture
from scripts.run_greenline_scheduled import upload_after_collection


class UploadQueueTests(unittest.TestCase):
    def setup_queue(self, root):
        manifest, posts = fixture()
        batch = build_batches(manifest, posts)[0]
        folder = root / 'run/supabase'
        folder.mkdir(parents=True)
        (folder / 'batch_001.json').write_text(json.dumps(batch))
        upload_manifest = {'batches': [{'path': 'batch_001.json', 'run_id': batch['run']['id']}]}
        (folder / 'manifest.json').write_text(json.dumps(upload_manifest))
        credential = root / 'connection.json'
        save_private(credential, {'host': 'example.supabase.co', 'port': '5432', 'user': 'test', 'dbname': 'postgres', 'password': 'test-secret'})
        return folder, credential

    def test_commit_failure_stays_pending_and_same_run_is_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder, credential = self.setup_queue(root)
            with patch('greenline_upload.commit_batches', side_effect=RuntimeError('test-secret')) as commit:
                summary = process_queue(root, credential)
            self.assertEqual(summary['status'], 'retry_pending')
            receipt = json.loads((folder / 'upload_status.json').read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertNotIn('test-secret', json.dumps(receipt))
            run_id = receipt['run_ids'][0]
            with patch('greenline_upload.commit_batches', return_value=[{'already_uploaded': True}]) as commit:
                summary = process_queue(root, credential)
                self.assertEqual(summary['status'], 'uploaded')
                self.assertEqual(commit.call_args[0][0][0]['run']['id'], run_id)
                again = process_queue(root, credential)
                self.assertEqual(again['status'], 'idle')
                self.assertEqual(commit.call_count, 1)
            receipt = json.loads((folder / 'upload_status.json').read_text())
            self.assertEqual(receipt['attempts'], 2)
            self.assertEqual(receipt['status'], 'uploaded')

    def test_missing_or_readable_credentials_never_connect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder, credential = self.setup_queue(root)
            credential.chmod(0o644)
            with self.assertRaises(ValueError):
                connection_params(credential)
            with patch('greenline_upload.commit_batches') as commit:
                summary = process_queue(root, credential)
                self.assertEqual(summary['status'], 'setup_required')
                commit.assert_not_called()
                credential.unlink()
                self.assertEqual(process_queue(root, credential)['status'], 'setup_required')

    def test_manifest_path_escape_and_changed_uploaded_batch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder, credential = self.setup_queue(root)
            with patch('greenline_upload.commit_batches', return_value=[]):
                process_queue(root, credential)
            batch_path = folder / 'batch_001.json'
            batch_path.write_text(batch_path.read_text() + '\n')
            with patch('greenline_upload.commit_batches') as commit:
                self.assertEqual(process_queue(root, credential)['status'], 'invalid_batch')
                commit.assert_not_called()
            path = folder / 'manifest.json'
            path.write_text(json.dumps({'batches': [{'path': '../batch_001.json'}]}))
            with self.assertRaises(ValueError):
                read_batches(path)

    def test_scheduler_prepares_then_requires_one_time_setup(self):
        with patch('prepare_greenline_batch.prepare_session') as prepare, patch('greenline_upload.process_queue') as queue:
            report = upload_after_collection({'auto_upload_requested': True, 'auto_upload': False}, '/local/session', {'unique_posts': 83}, 0)
            self.assertEqual(report['status'], 'setup_required')
            prepare.assert_called_once()
            queue.assert_not_called()

    def test_upload_failure_does_not_raise_into_collection_scheduler(self):
        with patch('greenline_upload.process_queue', side_effect=RuntimeError('do not expose this')):
            report = upload_after_collection({'auto_upload': True}, None, {}, 1)
            self.assertEqual(report['status'], 'retry_pending')
            self.assertNotIn('do not expose', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
