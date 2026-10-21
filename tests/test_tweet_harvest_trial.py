import csv
from pathlib import Path
import tempfile
import unittest

from try_tweet_harvest import command, summary


class TweetHarvestTrialTests(unittest.TestCase):
    def test_command_has_search_and_limit_without_credentials(self):
        args = command('node', 'CommuterLine', 20)
        self.assertEqual(args[args.index('--search-keyword') + 1], 'from:CommuterLine -filter:replies -filter:retweets')
        self.assertIn('LATEST', args)
        self.assertEqual(args[args.index('--limit') + 1], '20')
        self.assertNotIn('--token', args)

    def test_invalid_account_and_limits_do_not_reach_shell(self):
        for account, limit in [('user --token bad', 20), ('commuterline', 0), ('commuterline', 101)]:
            with self.assertRaises(ValueError): command('node', account, limit)

    def test_csv_summary_preserves_large_ids_and_counts_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'trial.csv'
            with path.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['id_str', 'full_text', 'created_at', 'image_url'])
                writer.writeheader()
                for value in ['2112134311579406594', '2112134311579406595', '2112134311579406594']:
                    writer.writerow({'id_str': value, 'full_text': 'Teks, dengan\nbaris baru', 'created_at': '2026-10-20T00:00:00Z', 'image_url': ''})
            result = summary(path)
        self.assertEqual(result['rows'], 3)
        self.assertEqual(result['unique_ids'], 2)
        self.assertEqual(result['duplicate_ids'], 1)
        self.assertEqual(result['rows_with_text'], 3)

    def test_reply_and_repost_markers_remain_visible_for_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'trial.csv'
            with path.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['id_str', 'full_text', 'created_at', 'in_reply_to_screen_name'])
                writer.writeheader()
                writer.writerow({'id_str': '1', 'full_text': 'Pengumuman', 'created_at': '2026-10-20T00:00:00Z'})
                writer.writerow({'id_str': '2', 'full_text': 'Balasan', 'in_reply_to_screen_name': 'contoh', 'created_at': '2026-10-20T00:00:00Z'})
                writer.writerow({'id_str': '3', 'full_text': 'RT @contoh: berita', 'created_at': '2026-10-20T00:00:00Z'})
            result = summary(path)
        self.assertEqual(result['reply_markers'], 1)
        self.assertEqual(result['repost_text_markers'], 1)
        self.assertEqual(result['rows'], 3)  # Raw export is inspected, never silently altered.


if __name__ == '__main__':
    unittest.main()
