import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
import uuid
from unittest.mock import Mock

from scripts.curate_incidents import (
    CurationError,
    CURATION_LOGIN,
    LIST_QUERY,
    fetch_candidates,
    format_detail,
    format_list,
    load_curation_connection,
    resolve_candidates,
    run_inspect,
)
from scripts.krl_station_resolver import KrlStationResolver


def entity(kind, value, quote=None):
    return {
        'entity_type': kind,
        'value': value,
        'evidence_type': 'explicit',
        'evidence_quote': quote or value,
    }


def station(code, name, aliases=()):
    return {
        'station_id': 'open-krl:' + code,
        'source_station_code': code,
        'canonical_name': name,
        'aliases': list(aliases),
        'latitude': '-6.17',
        'longitude': '106.82',
    }


def event_row(event_id, entity_rows, *, published_at=None, author='commuter',
              review='pending', incident_id=None, status='potential_disruption', index=1):
    return (
        event_id, 'snapshot-1', index, status, 'delay', 'moderate', entity_rows,
        None, None, 'yes', 'Some details are uncertain.', 'Evidence-based explanation.',
        ['perjalanan tertahan'], published_at, author, 'Commuter Name',
        'https://x.com/commuter/status/1', 1, review,
        incident_id, 'proposed' if incident_id else None,
        'supports' if incident_id else None, bool(incident_id),
    )


class IncidentCurationTests(unittest.TestCase):
    def setUp(self):
        self.resolver = KrlStationResolver([
            station('THB', 'Tanah Abang', ['Tanahabang']),
            station('PLM', 'Palmerah'),
        ])

    def test_query_is_scoped_to_positive_potential_events_and_read_only(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        result = fetch_candidates({'host': 'db'}, connect=Mock(return_value=context))
        self.assertEqual(result, [])
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(statements[0], 'SET TRANSACTION READ ONLY')
        self.assertIn("e.event_status = 'potential_disruption' AND e.event_index > 0", LIST_QUERY)
        self.assertIn('serving.incident_evidence', LIST_QUERY)

    def test_empty_candidate_list_is_clear(self):
        self.assertEqual(format_list([]), 'No potential-disruption candidates found.')

    def test_rows_preserve_author_publication_and_group_incident_associations(self):
        from scripts.curate_incidents import _group_rows
        rows = [
            event_row('event-1', [entity('station', 'Tanahabang', 'dekat Tanahabang')],
                      published_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                      incident_id='incident-1'),
            event_row('event-1', [entity('station', 'Tanahabang', 'dekat Tanahabang')],
                      published_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                      incident_id='incident-2'),
        ]
        event = _group_rows(rows)[0]
        resolve_candidates([event], self.resolver)
        output = format_list([event])
        self.assertIn('@commuter', output)
        self.assertIn('associated (2)', output)
        self.assertIn('2026-', output)
        self.assertEqual(event['author_handle'], 'commuter')
        self.assertEqual(len(event['incident_associations']), 2)
        self.assertEqual(event['station_resolutions'][0]['resolution_status'], 'resolved')

    def test_missing_publication_timestamp_and_no_review_are_rendered(self):
        from scripts.curate_incidents import _group_rows
        event = _group_rows([event_row(
            'event-2', [entity('station', 'Tanah Abang')], published_at=None, review=None,
        )])[0]
        resolve_candidates([event], self.resolver)
        output = format_list([event])
        self.assertIn('(unavailable)', output)
        self.assertIn('not_reviewed', output)

    def test_detail_includes_quotes_uncertainty_and_non_assertive_station_resolution(self):
        from scripts.curate_incidents import _group_rows
        event = _group_rows([event_row(
            'event-3', [entity('station', 'Tanah Abang', 'Tanah Abang tertahan')],
        )])[0]
        resolve_candidates([event], self.resolver)
        detail = format_detail(event)
        self.assertIn('Evidence: Tanah Abang tertahan', detail)
        self.assertIn('Uncertainty: yes', detail)
        self.assertIn('mentioned station only; not confirmed incident location', detail)
        self.assertIn('perjalanan tertahan', detail)

    def test_ambiguous_station_is_reported_without_selecting_candidate(self):
        ambiguous_resolver = KrlStationResolver([
            station('ONE', 'Station One', ['Central']),
            station('TWO', 'Station Two', ['Central']),
        ])
        from scripts.curate_incidents import _group_rows
        event = _group_rows([event_row('event-4', [entity('station', 'Central')])])[0]
        resolve_candidates([event], ambiguous_resolver)
        detail = format_detail(event)
        self.assertIn('[ambiguous]', detail)
        self.assertIn('2 exact station matches; choose manually', detail)
        self.assertNotIn('-> Station One', detail)

    def test_inspect_rejects_non_candidate_ids(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        with self.assertRaisesRegex(CurationError, 'No potential-disruption event'):
            run_inspect(str(uuid.uuid4()), {'host': 'db'}, resolver=self.resolver,
                        connect=Mock(return_value=context))

    def test_connection_file_requires_dedicated_login_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'curation.json'
            value = {
                'host': 'db.example.supabase.co', 'dbname': 'postgres',
                'user': CURATION_LOGIN, 'password': 'offline-test-secret',
            }
            path.write_text(json.dumps(value), encoding='utf-8')
            os.chmod(path, 0o600)
            loaded = load_curation_connection(path)
            self.assertEqual(loaded['user'], CURATION_LOGIN)
            self.assertEqual(loaded['sslmode'], 'require')
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(CurationError, 'permissions must be 0600'):
                load_curation_connection(path)


if __name__ == '__main__':
    unittest.main()
