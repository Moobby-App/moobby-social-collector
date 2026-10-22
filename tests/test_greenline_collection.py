import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from collect_greenline import export_csv, in_window, merge_results, search_plan, STATIONS


class GreenLineTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 10, 21, 10, 16, 14, tzinfo=timezone.utc)
        self.plan = search_plan(self.end)

    def test_window_is_rolling_24_hours_and_queries_keep_replies(self):
        self.assertEqual(datetime.fromisoformat(self.plan['window_start']), self.end - timedelta(hours=24))
        self.assertEqual(datetime.fromisoformat(self.plan['window_end']), self.end)
        for query in self.plan['queries']:
            self.assertIn('since:2026-10-20 until:2026-10-22', query['query'])
            self.assertNotIn('-filter:replies', query['query'])
        self.assertEqual(len(STATIONS), 6)
        self.assertEqual(len({q['id'] for q in self.plan['queries']}), len(self.plan['queries']))

    def test_precise_window_rejects_unknown_and_future_timestamps(self):
        start = self.end - timedelta(hours=24)
        self.assertTrue(in_window(start.isoformat(), start, self.end))
        self.assertFalse(in_window(self.end.isoformat(), start, self.end))
        self.assertFalse(in_window((start - timedelta(seconds=1)).isoformat(), start, self.end))
        for invalid in (None, '', 'invalid', '2026-10-21T10:00:00'):
            self.assertFalse(in_window(invalid, start, self.end))

    def test_merge_keeps_query_evidence_versions_and_large_string_ids(self):
        post = {'id_str': '2112134311579406594', 'published_at': '2026-10-21T09:00:00Z',
                'captured_at': '2026-10-21T10:00:00Z', 'username': 'penumpang',
                'tweet_url': 'https://x.com/penumpang/status/2112134311579406594',
                'full_text': 'KRL tertahan', 'post_type': 'reply', 'media': [],
                'text_completeness': 'unknown', 'media_extraction_basis': 'extended_entities'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / (self.plan['queries'][0]['id'] + '.json')).write_text(json.dumps({'posts': [post]}))
            repeat = dict(post, captured_at='2026-10-21T10:01:00Z')
            changed = dict(post, full_text='KRL sudah berjalan')
            outside = dict(post, id_str='99', published_at='2026-10-19T09:00:00Z')
            (folder / (self.plan['queries'][1]['id'] + '.json')).write_text(json.dumps({'posts': [repeat, changed, outside]}))
            records = merge_results(self.plan, folder)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['external_id'], post['id_str'])
            self.assertEqual(len(records[0]['snapshots']), 2)
            self.assertEqual(len(records[0]['snapshots'][0]['query_matches']), 2)
            self.assertEqual(records[0]['snapshots'][0]['post_type'], 'reply')
            export_csv(records, folder / 'raw.csv')
            self.assertIn('query_matches_json', (folder / 'raw.csv').read_text())


if __name__ == '__main__':
    unittest.main()
