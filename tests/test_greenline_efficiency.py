import copy
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collect_greenline import incremental_plan, query_metrics, search_plan
from ocr_tweet_images import enrich, read_cache, seed_cache
from prepare_greenline_batch import build_batches
from test_greenline_batch import fixture
from upload_collection_batch import validate_batch


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 10, 21, 13, tzinfo=timezone.utc)

    def checkpoint(self, folder, end, status='collected', ocr_status='exported'):
        folder.mkdir()
        report = search_plan(end)
        report.update(status=status, ocr_status=ocr_status)
        for query in report['queries']:
            query['status'] = 'no_new_matching_posts'
        (folder / 'manifest.json').write_text(json.dumps(report))
        (folder / 'raw_posts.json').write_text('[]')
        return report

    def test_initial_window_and_full_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = incremental_plan(root, self.end)
            self.assertEqual(datetime.fromisoformat(plan['window_start']), self.end - timedelta(hours=24))
            self.checkpoint(root / 'old', self.end - timedelta(hours=1))
            self.assertEqual(incremental_plan(root, self.end, full_window=True)['window_mode'], 'full_24h')

    def test_uses_last_success_and_overlap_without_skipping_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.end - timedelta(hours=3)
            self.checkpoint(root / 'success', previous)
            self.checkpoint(root / 'partial', self.end - timedelta(hours=1), status='partial')
            self.checkpoint(root / 'ocr_failed', self.end - timedelta(minutes=30), ocr_status='failed')
            self.checkpoint(root / 'ocr_pending', self.end - timedelta(minutes=20), ocr_status='pending')
            plan = incremental_plan(root, self.end)
            self.assertEqual(plan['window_mode'], 'incremental')
            self.assertEqual(datetime.fromisoformat(plan['window_start']), previous - timedelta(minutes=30))
            self.assertEqual(datetime.fromisoformat(plan['window_end']), self.end)

    def test_downtime_not_truncated_and_query_changes_invalidate_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / 'old'
            report = self.checkpoint(old, self.end - timedelta(days=3))
            plan = incremental_plan(root, self.end)
            self.assertGreater((datetime.fromisoformat(plan['window_end']) - datetime.fromisoformat(plan['window_start'])).total_seconds(), 86400)
            report['queries'][0]['query'] = 'different query'
            (old / 'manifest.json').write_text(json.dumps(report))
            self.assertEqual(incremental_plan(root, self.end)['window_mode'], 'full_24h')

    def test_incremental_batch_contract_and_checkpoint_validation(self):
        manifest, posts = fixture()
        manifest.update(window_mode='incremental', window_start='2026-10-21T10:30:00Z',
                        checkpoint_window_end='2026-10-21T11:00:00Z', overlap_minutes=30)
        batch = build_batches(manifest, posts)[0]
        schema = json.loads(Path(__file__).resolve().parents[1].joinpath('raw_batch_v2.schema.json').read_text())
        validate_batch(batch, schema)
        manifest['overlap_minutes'] = 10
        with self.assertRaises(ValueError):
            build_batches(manifest, posts)

    def test_query_contributions_do_not_count_same_post_twice(self):
        records = [
            {'snapshots': [{'query_matches': [{'id': 'a'}, {'id': 'b'}]}, {'query_matches': [{'id': 'a'}]}]},
            {'snapshots': [{'query_matches': [{'id': 'a'}]}]},
        ]
        self.assertEqual(query_metrics(records), {'a': {'unique_posts': 2, 'exclusive_posts': 1}, 'b': {'unique_posts': 1, 'exclusive_posts': 0}})


class PersistentOCRTests(unittest.TestCase):
    def test_reuses_ocr_between_runs_but_invalidates_changed_model_and_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for language in ('ind', 'eng'):
                (root / (language + '.traineddata')).write_bytes(b'model')
            source = root / 'input.csv'
            media = [{'type': 'photo', 'media_index': 1, 'image_url': 'https://pbs.twimg.com/media/a.jpg'}]
            with source.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['id_str', 'full_text', 'media_json', 'media_extraction_basis', 'tweet_url'])
                writer.writeheader()
                writer.writerow({'id_str': '1', 'full_text': 'Caption', 'media_json': json.dumps(media),
                                 'media_extraction_basis': 'extended_entities', 'tweet_url': 'https://x.com/test/status/1'})
            cache = root / 'cache'
            with patch('ocr_tweet_images.shutil.which', return_value='tesseract'), patch('ocr_tweet_images.subprocess.check_output', return_value='tesseract test\n'), patch('ocr_tweet_images.download', return_value=b'image'), patch('ocr_tweet_images.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='Teks poster')) as engine:
                first = enrich(source, tessdata=root, cache_dir=cache)
                second = enrich(source, tessdata=root, cache_dir=cache)
                self.assertEqual(engine.call_count, 1)
                report = json.loads(second.with_name('ocr_results.json').read_text())
                self.assertEqual(report['cache_stats']['persistent_hits'], 1)
                first_report = json.loads(first.with_name('ocr_results.json').read_text())
                self.assertEqual(report['records'][0]['images'][0]['ocr_processed_at'], first_report['records'][0]['images'][0]['ocr_processed_at'])
                (root / 'ind.traineddata').write_bytes(b'changed model')
                enrich(source, tessdata=root, cache_dir=cache)
                self.assertEqual(engine.call_count, 2)
                with patch('ocr_tweet_images.download', return_value=b'changed image'):
                    enrich(source, tessdata=root, cache_dir=cache)
                self.assertEqual(engine.call_count, 3)
                with patch('ocr_tweet_images.subprocess.check_output', return_value='tesseract updated\n'):
                    enrich(source, tessdata=root, cache_dir=cache)
                self.assertEqual(engine.call_count, 4)
            imported = root / 'imported'
            self.assertEqual(seed_cache(first.with_name('ocr_results.json'), imported), 1)
            result = first_report['records'][0]['images'][0]
            self.assertIsNotNone(read_cache(imported, result['image_hash'], result['ocr_engine'], first_report['model_sha256']))
            next(imported.glob('*.json')).write_text('{invalid')
            self.assertIsNone(read_cache(imported, result['image_hash'], result['ocr_engine'], first_report['model_sha256']))
            (first.parent / result['local_image']).write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                seed_cache(first.with_name('ocr_results.json'), root / 'invalid')


if __name__ == '__main__':
    unittest.main()
