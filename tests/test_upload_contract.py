import copy
import json
from pathlib import Path
import unittest
from prepare_batch import prepare
from upload_collection_batch import BatchError, validate_batch, same_run, observation_signature
from test_prepare_batch import sample
from datetime import datetime


class UploadContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(Path(__file__).resolve().parents[1].joinpath('raw_batch.schema.json').read_text())
        value = sample(); value['target_count'] = 1
        self.batch = prepare(value, 'x:commuterline', 'member-a')

    def test_real_adapter_contract_is_accepted(self):
        validate_batch(self.batch, self.schema)

    def test_unknown_fields_are_rejected(self):
        for key in ('cookies', 'verification_status', 'curation_status'):
            modified = copy.deepcopy(self.batch)
            modified['records'][0][key] = 'must not be stored'
            with self.assertRaises(BatchError): validate_batch(modified, self.schema)

    def test_content_tampering_or_inconsistent_counts_are_rejected(self):
        changed = copy.deepcopy(self.batch); changed['records'][0]['raw_text'] += ' changed'
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)
        changed = copy.deepcopy(self.batch); changed['run']['captured_count'] += 1
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)

    def test_duplicate_content_in_one_run_rejected(self):
        changed = copy.deepcopy(self.batch)
        changed['records'].append(copy.deepcopy(changed['records'][0]))
        changed['run']['captured_count'] = 2
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)

    def test_unfinished_run_and_out_of_range_capture_are_rejected(self):
        changed = copy.deepcopy(self.batch); changed['run']['finished_at'] = None
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)
        changed = copy.deepcopy(self.batch); changed['records'][0]['captured_at'] = '2026-10-21T00:00:00Z'
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)

    def test_media_capture_consistency(self):
        changed = copy.deepcopy(self.batch)
        changed['records'][0]['media'] = [{'type': 'image', 'url': 'https://example.com/image.jpg', 'alt_text': None, 'thumbnail_url': None}]
        with self.assertRaises(BatchError): validate_batch(changed, self.schema)

    def test_same_run_compares_normalized_database_time_and_metadata(self):
        r = self.batch['run']; source = 'e71ee149-8300-4cfe-ab74-85882314ca42'
        stored = [source, r['collector_label'], r['collector_version'], r['collection_method'],
                  datetime.fromisoformat(r['started_at'].replace('Z', '+00:00')),
                  datetime.fromisoformat(r['finished_at'].replace('Z', '+00:00')),
                  r['run_status'], r['stop_reason'], r['coverage'], r['complete_history'],
                  r['requested_count'], r['captured_count'], r['time_basis']]
        self.assertTrue(same_run(stored, r, source))
        stored[1] = 'someone-else'
        self.assertFalse(same_run(stored, r, source))

    def test_observation_signature_is_timezone_independent(self):
        a = observation_signature('x', '123', 'hash', '2026-10-20T10:00:00+07:00', '1j', 1, None, 'unknown', [])
        b = observation_signature('x', '123', 'hash', datetime.fromisoformat('2026-10-20T03:00:00+00:00'), '1j', 1, None, 'unknown', [])
        self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main()
