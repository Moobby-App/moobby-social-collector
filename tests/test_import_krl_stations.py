import copy
import hashlib
import json
from pathlib import Path
import unittest

from scripts.import_krl_stations import (
    StationImportError,
    build_change_report,
    extract_stations,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_BYTES = (ROOT / 'open-krl.json').read_bytes()


class KrlStationImportTests(unittest.TestCase):
    def setUp(self):
        self.source = extract_stations(SOURCE_BYTES)

    def test_extracts_only_krl_memberships_and_expected_station_count(self):
        self.assertEqual(len(self.source['stations']), 84)
        self.assertTrue(all(row['station_id'].startswith('open-krl:') for row in self.source['stations']))
        self.assertTrue(all(row['line_memberships'] for row in self.source['stations']))
        self.assertTrue(all(set(row['line_memberships']) <= {
            'Bogor', 'Rangkasbitung', 'Tangerang', 'Tanjung Priok', 'Cikarang'
        } for row in self.source['stations']))
        self.assertEqual(sum('Rangkasbitung' in row['line_memberships'] for row in self.source['stations']), 25)

    def test_integer_coordinates_convert_as_longitude_then_latitude(self):
        station = next(row for row in self.source['stations'] if row['source_station_code'] == 'AC')
        self.assertEqual(str(station['longitude']), '106.84514')
        self.assertEqual(str(station['latitude']), '-6.12811')

    def test_rejects_duplicate_station_codes_in_full_source(self):
        value = json.loads(SOURCE_BYTES)
        value['stations'][1][0] = value['stations'][0][0]
        with self.assertRaisesRegex(StationImportError, 'Duplicate station codes'):
            extract_stations(json.dumps(value).encode())

    def test_preserves_provenance_and_exact_file_hash(self):
        metadata = self.source['metadata']
        self.assertEqual(self.source['source_sha256'], hashlib.sha256(SOURCE_BYTES).hexdigest())
        self.assertEqual(metadata['source_url'], 'https://github.com/open-krl/pipeline')
        self.assertEqual(metadata['built_at'].isoformat(), '2026-10-12T11:15:13+00:00')
        self.assertEqual(metadata['effective_since'].isoformat(), '2026-09-21')
        self.assertIn('OpenStreetMap contributors', metadata['coordinate_attribution'])
        self.assertEqual(metadata['coordinate_license'], 'ODbL')
        self.assertIn('CC-BY 4.0', metadata['dataset_license'])

    def test_change_detection_reports_additions_changes_and_absences(self):
        rows = copy.deepcopy(self.source['stations'])
        existing = [dict(row) for row in rows]
        existing[0]['canonical_name'] = 'Older Name'
        existing.append({
            'station_id': 'open-krl:retired', 'source_station_code': 'retired',
            'canonical_name': 'Retired', 'latitude': rows[0]['latitude'],
            'longitude': rows[0]['longitude'], 'line_memberships': ['Bogor'],
        })
        rows.append({
            'station_id': 'open-krl:new', 'source_station_code': 'new',
            'canonical_name': 'New', 'aliases': [], 'latitude': rows[0]['latitude'],
            'longitude': rows[0]['longitude'], 'line_memberships': ['Bogor'],
            'source_sha256': self.source['source_sha256'],
        })
        report = build_change_report(existing, rows)
        self.assertEqual(report['added'], ['open-krl:new'])
        self.assertEqual(report['changed'], [{'station_id': rows[0]['station_id'], 'fields': ['canonical_name']}])
        self.assertEqual(report['removed_from_source'], ['open-krl:retired'])
        self.assertEqual(report['unchanged_count'], 83)

    def test_identical_second_import_is_idempotent(self):
        first_report = build_change_report([], self.source['stations'])
        second_report = build_change_report(self.source['stations'], self.source['stations'])
        self.assertEqual(len(first_report['added']), 84)
        self.assertEqual(second_report['added'], [])
        self.assertEqual(second_report['changed'], [])
        self.assertEqual(second_report['removed_from_source'], [])
        self.assertEqual(second_report['unchanged_count'], 84)


if __name__ == '__main__':
    unittest.main()
