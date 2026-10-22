import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collect_greenline import export_csv
from prepare_greenline_batch import build_batches, prepare_session
from upload_collection_batch import validate_batch


def fixture():
    query = '"Serpong" KRL since:2026-10-20 until:2026-10-22'
    manifest = {'schema_version': 'greenline.raw.v1', 'collector_label': 'tester', 'status': 'collected',
                'window_start': '2026-10-20T12:00:00Z', 'window_end': '2026-10-21T12:00:00Z',
                'unique_posts': 1, 'queries': [{'id': 'stations_3', 'query': query, 'status': 'no_new_matching_posts', 'count': 1}]}
    snapshot = {'id_str': '2112134311579406594', 'username': 'penumpang', 'author_display_name': None,
                'tweet_url': 'https://x.com/penumpang/status/2112134311579406594',
                'published_at': '2026-10-21T11:00:00Z', 'captured_at': '2026-10-21T12:01:00Z',
                'full_text': 'KRL Serpong tertahan', 'text_completeness': 'unknown',
                'text_extraction_method': 'legacy.full_text', 'post_type': 'reply',
                'conversation_id': '12', 'in_reply_to_status_id': '13', 'in_reply_to_screen_name': 'commuterline',
                'media': [], 'media_extraction_basis': 'not_available',
                'related_urls': ['http://example.com/report'],
                'query_matches': [{'id': 'stations_3', 'query': query}]}
    posts = [{'platform': 'x', 'external_id': snapshot['id_str'], 'snapshots': [snapshot]}]
    return manifest, posts


class GreenLineBatchTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(Path(__file__).resolve().parents[1].joinpath('raw_batch_v2.schema.json').read_text())
        self.manifest, self.posts = fixture()

    def test_valid_search_source_preserves_reply_queries_and_retry(self):
        a = build_batches(self.manifest, self.posts)
        self.assertEqual(a, build_batches(self.manifest, self.posts))
        validate_batch(a[0], self.schema)
        self.assertEqual(a[0]['source_key'], 'x:greenline_search')
        row = a[0]['records'][0]
        self.assertEqual(row['author_handle'], 'penumpang')
        self.assertEqual(row['post_type'], 'reply')
        self.assertEqual(row['extraction_metadata']['in_reply_to_status_id'], '13')
        self.assertEqual(row['extraction_metadata']['search_query_ids'], ['stations_3'])
        self.assertEqual(row['extraction_metadata']['original_related_urls'], ['http://example.com/report'])
        coverage = json.loads(a[0]['run']['coverage'])
        self.assertEqual(coverage['queries'], self.manifest['queries'])
        self.assertIsNone(a[0]['run']['requested_count'])

    def test_multiple_versions_and_more_than_100_do_not_drop_records(self):
        for i in range(101):
            post = copy.deepcopy(self.posts[0])
            post_id = str(2112134311579406595 + i)
            post['external_id'] = post_id
            post['snapshots'][0].update(id_str=post_id, tweet_url='https://x.com/penumpang/status/' + post_id)
            self.posts.append(post)
        changed = copy.deepcopy(self.posts[0]['snapshots'][0])
        changed['full_text'] = 'KRL sudah berjalan'
        self.posts[0]['snapshots'].append(changed)
        self.manifest['unique_posts'] = len(self.posts)
        batches = build_batches(self.manifest, self.posts)
        self.assertEqual(sum(len(batch['records']) for batch in batches), 103)
        for batch in batches:
            validate_batch(batch, self.schema)
            self.assertLessEqual(len(batch['records']), 100)

    def test_reject_wrong_window_author_or_query(self):
        for field, value in [('published_at', '2026-10-19T01:00:00Z'), ('username', 'other')]:
            posts = copy.deepcopy(self.posts)
            posts[0]['snapshots'][0][field] = value
            with self.assertRaises(ValueError):
                build_batches(self.manifest, posts)
        self.posts[0]['snapshots'][0]['query_matches'][0]['query'] = 'different query'
        with self.assertRaises(ValueError):
            build_batches(self.manifest, self.posts)

    def test_ocr_maps_to_image_without_leaking_local_paths(self):
        post = self.posts[0]['snapshots'][0]
        url = 'https://pbs.twimg.com/media/sample.jpg'
        post['media'] = [{'type': 'photo', 'image_url': url, 'media_index': 1, 'media_id': '7'}]
        proof = {'model_sha256': {'ind': 'a' * 64, 'eng': 'b' * 64}, 'records': [
            {'post_id': post['id_str'], 'source_url': post['tweet_url'], 'images': [
                {'media_url': url, 'media_id': '7', 'media_index': 1, 'ocr_status': 'completed',
                 'ocr_text': 'Gangguan perjalanan', 'ocr_engine': 'tesseract 5', 'ocr_languages': 'ind+eng',
                 'ocr_psm': 11, 'ocr_processed_at': '2026-10-21T12:05:00Z', 'image_hash': 'c' * 64,
                 'local_image': '/private/local/image.jpg'}]}]}
        batch = build_batches(self.manifest, self.posts, proof)[0]
        validate_batch(batch, self.schema)
        self.assertNotIn('/private/local', json.dumps(batch))
        self.assertEqual(batch['records'][0]['media'][0]['ocr']['raw_text'], 'Gangguan perjalanan')
        proof['records'][0]['images'][0]['media_url'] = 'https://pbs.twimg.com/media/wrong.jpg'
        with self.assertRaises(ValueError):
            build_batches(self.manifest, self.posts, proof)

    def test_prepared_files_are_reusable_but_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'manifest.json').write_text(json.dumps(self.manifest))
            (folder / 'raw_posts.json').write_text(json.dumps(self.posts))
            output, _ = prepare_session(folder)
            self.assertEqual(output, prepare_session(folder)[0])
            batch_file = output.parent / 'batch_001.json'
            batch_file.write_text('do not overwrite')
            with self.assertRaises(ValueError):
                prepare_session(folder)
            self.assertEqual(batch_file.read_text(), 'do not overwrite')

    def test_ocr_source_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            ocr = folder / 'ocr'
            ocr.mkdir()
            manifest = dict(self.manifest, ocr_output='ocr/raw_posts_ocr.csv')
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            (folder / 'raw_posts.json').write_text(json.dumps(self.posts))
            export_csv(self.posts, folder / 'raw_posts.csv')
            (ocr / 'ocr_results.json').write_text(json.dumps({'input_sha256': '0' * 64}))
            with patch('prepare_greenline_batch.ROOT', folder):
                with self.assertRaisesRegex(ValueError, 'CSV sumber berubah'):
                    prepare_session(folder)


if __name__ == '__main__':
    unittest.main()
