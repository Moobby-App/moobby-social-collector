import copy
import unittest
from prepare_batch import prepare, capture_hash


def sample():
    return {
        'profile_url': 'https://x.com/CommuterLine',
        'stop_reason': 'target_reached', 'target_count': 20,
        'posts': [{
            'id': '2112134311579406594',
            'source_url': 'https://x.com/CommuterLine/status/2112134311579406594/photo/1',
            'source_account': 'CommuterLine', 'raw_card_text': 'Teks asli\nTampilkan lebih banyak',
            'collected_at': '2026-10-20T10:00:00+07:00',
            'published_at': '2026-10-20T02:00:00Z', 'date_basis': 'dom_datetime',
            'published_label': '1j', 'verification_status': 'unverified', 'curation_status': 'not_reviewed',
        }],
    }


class CollectionContractTests(unittest.TestCase):
    def test_keeps_original_text_and_strips_curation_fields(self):
        value = sample(); batch = prepare(value, 'x:commuterline', 'member-a')
        row = batch['records'][0]
        self.assertEqual(row['raw_text'], value['posts'][0]['raw_card_text'])
        self.assertNotIn('curation_status', row)
        self.assertNotIn('verification_status', row)
        self.assertNotIn('/photo/', row['canonical_url'])

    def test_multilingual_truncation_and_media_not_invented(self):
        batch = prepare(sample(), 'x:commuterline', 'member-a')
        row = batch['records'][0]
        self.assertEqual(row['text_completeness'], 'truncated')
        self.assertEqual(row['media_capture_status'], 'not_attempted')
        self.assertIsNone(row['is_pinned'])
        self.assertEqual(row['post_type'], 'unknown')

    def test_no_marker_does_not_mean_full(self):
        value = sample(); value['posts'][0]['raw_card_text'] = 'Informasi tanpa tombol'
        self.assertEqual(prepare(value, 'x:commuterline', 'member-a')['records'][0]['text_completeness'], 'unknown')

    def test_unknown_date_never_replaced_with_collection_time(self):
        value = sample(); value['posts'][0]['date_basis'] = 'year_inferred_from_collection'
        row = prepare(value, 'x:commuterline', 'member-a')['records'][0]
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['published_at_basis'], 'unknown')

    def test_timezone_required_and_utc_output(self):
        value = sample()
        self.assertEqual(prepare(value, 'x:commuterline', 'member-a')['records'][0]['captured_at'], '2026-10-20T03:00:00Z')
        value['posts'][0]['collected_at'] = '2026-10-20T10:00:00'
        with self.assertRaises(ValueError): prepare(value, 'x:commuterline', 'member-a')

    def test_x_id_must_not_be_number_or_mismatch(self):
        value = sample(); value['posts'][0]['id'] = 2112134311579406594
        with self.assertRaises(ValueError): prepare(value, 'x:commuterline', 'member-a')
        value['posts'][0]['id'] = '123'
        with self.assertRaises(ValueError): prepare(value, 'x:commuterline', 'member-a')

    def test_monitored_profile_can_differ_from_author(self):
        value = sample(); value['profile_url'] = 'https://x.com/jalur5_'
        row = prepare(value, 'x:jalur5_', 'member-b')['records'][0]
        self.assertEqual(row['author_handle'], 'CommuterLine')

    def test_retry_keeps_run_id_and_hash_excludes_observation_time(self):
        value = sample(); first = prepare(value, 'x:commuterline', 'member-a')
        value['posts'][0]['collected_at'] = '2026-10-20T11:00:00+07:00'
        second = prepare(value, 'x:commuterline', 'member-a', first['run']['id'])
        self.assertEqual(first['run']['id'], second['run']['id'])
        self.assertEqual(first['records'][0]['capture_hash'], second['records'][0]['capture_hash'])
        row = copy.deepcopy(second['records'][0]); row['raw_text'] += ' changed'
        self.assertNotEqual(capture_hash(row), first['records'][0]['capture_hash'])

    def test_duplicate_ids_and_curated_input_rejected(self):
        value = sample(); value['posts'].append(copy.deepcopy(value['posts'][0]))
        with self.assertRaises(ValueError): prepare(value, 'x:commuterline', 'member-a')
        value = sample(); value['posts'][0]['curation_reason'] = 'not selected'
        with self.assertRaises(ValueError): prepare(value, 'x:commuterline', 'member-a')

    def test_profile_mismatch_is_not_silently_attributed(self):
        with self.assertRaises(ValueError): prepare(sample(), 'x:jalur5_', 'member-a')

    def test_empty_body_is_allowed_without_ocr_invention(self):
        value = sample(); value['posts'][0]['raw_card_text'] = ''
        row = prepare(value, 'x:commuterline', 'member-a')['records'][0]
        self.assertEqual(row['raw_text'], '')
        self.assertEqual(row['text_kind'], 'none')


if __name__ == '__main__':
    unittest.main()
