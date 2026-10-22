import unittest
from unittest.mock import Mock

from scripts.krl_station_resolver import (
    KrlStationResolver,
    StationResolutionError,
    load_reference_stations,
    normalize_station_name,
)


def station(code, name, aliases=(), latitude='-6.17', longitude='106.82'):
    return {
        'station_id': 'open-krl:' + code,
        'source_station_code': code,
        'canonical_name': name,
        'aliases': list(aliases),
        'latitude': latitude,
        'longitude': longitude,
    }


def entity(kind, value, quote=None, evidence_type='explicit'):
    return {
        'entity_type': kind,
        'value': value,
        'evidence_type': evidence_type,
        'evidence_quote': quote or value,
    }


class KrlStationResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = KrlStationResolver([
            station('THB', 'Tanah Abang', aliases=['Tanahabang', 'St. Tanah Abang']),
            station('PLM', 'Palmerah', aliases=['Palmerah Station'], latitude='-6.207', longitude='106.797'),
        ])

    def test_exact_normalized_canonical_match_preserves_evidence(self):
        original = entity('station', '  STASIUN TANAH-ABANG! ', 'di Stasiun Tanah-Abang')
        result = self.resolver.resolve_entity(original)
        self.assertEqual(result['resolution_status'], 'resolved')
        self.assertEqual(result['resolution_reason'], 'exact_canonical_match')
        self.assertEqual(result['station_id'], 'open-krl:THB')
        self.assertEqual(result['source_station_code'], 'THB')
        self.assertEqual(result['canonical_name'], 'Tanah Abang')
        self.assertEqual(str(result['latitude']), '-6.17')
        self.assertEqual(result['value'], original['value'])
        self.assertEqual(result['evidence_quote'], original['evidence_quote'])
        self.assertEqual(result['location_semantics'], 'mentioned_station_only')

    def test_curated_alias_matches_without_fuzzy_matching(self):
        result = self.resolver.resolve_entity(entity('station', 'TANAHABANG'))
        self.assertEqual(result['resolution_status'], 'resolved')
        self.assertEqual(result['resolution_reason'], 'exact_alias_match')
        self.assertEqual(result['station_id'], 'open-krl:THB')
        self.assertEqual(self.resolver.resolve_entity(entity('station', 'Tanah Abanggg'))['resolution_status'], 'unresolved')

    def test_unknown_station_is_unresolved_without_station_details(self):
        result = self.resolver.resolve_entity(entity('station', 'Stasiun Fiktif'))
        self.assertEqual(result['resolution_status'], 'unresolved')
        self.assertEqual(result['resolution_reason'], 'no_exact_match')
        self.assertFalse({'station_id', 'source_station_code', 'canonical_name', 'latitude', 'longitude'} & set(result))

    def test_multiple_station_mentions_resolve_independently(self):
        results = self.resolver.resolve_affected_entities([
            entity('station', 'Tanah Abang'), entity('station', 'Palmerah'),
        ])
        self.assertEqual([result['resolution_status'] for result in results], ['resolved', 'resolved'])
        self.assertEqual([result['station_id'] for result in results], ['open-krl:THB', 'open-krl:PLM'])
        self.assertNotIn('nearest_station_id', results[0])

    def test_line_and_route_mentions_do_not_resolve_as_stations(self):
        results = self.resolver.resolve_affected_entities([
            entity('line', 'Tanah Abang'), entity('route', 'Palmerah'),
            entity('other', 'Tanah Abang'),
        ])
        for result in results:
            self.assertEqual(result['resolution_status'], 'unresolved')
            self.assertEqual(result['resolution_reason'], 'non_station_entity')
            self.assertNotIn('latitude', result)
            self.assertNotIn('station_id', result)

    def test_alias_conflict_is_ambiguous_and_exposes_no_candidate_coordinates(self):
        resolver = KrlStationResolver([
            station('ONE', 'Station One', aliases=['Central']),
            station('TWO', 'Station Two', aliases=['Central'], latitude='-6.22'),
        ])
        result = resolver.resolve_entity(entity('station', 'Central'))
        self.assertEqual(result['resolution_status'], 'ambiguous')
        self.assertEqual(result['candidate_count'], 2)
        self.assertFalse({'station_id', 'source_station_code', 'canonical_name', 'latitude', 'longitude'} & set(result))

    def test_normalization_is_stable_and_limited_to_surface_form(self):
        self.assertEqual(normalize_station_name('  STASIUN  Tanah-Abang! '), 'tanah abang')
        self.assertNotEqual(normalize_station_name('Parung Panjang'), normalize_station_name('Parungpanjang'))

    def test_database_loader_starts_read_only_before_select(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [
            ('open-krl:THB', 'THB', 'Tanah Abang', ['Tanahabang'], '-6.17', '106.82'),
        ]
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        connector = Mock(return_value=context)
        resolver = load_reference_stations({'host': 'db.local'}, connect=connector)
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(statements[0], 'SET TRANSACTION READ ONLY')
        self.assertIn('SELECT station_id', statements[2])
        self.assertEqual(resolver.resolve_entity(entity('station', 'Tanahabang'))['resolution_status'], 'resolved')

    def test_bad_entity_contract_is_rejected(self):
        with self.assertRaises(StationResolutionError):
            self.resolver.resolve_entity({'entity_type': 'station', 'value': 'Tanah Abang'})


if __name__ == '__main__':
    unittest.main()
