import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import Mock

from scripts.incident_gateway_api import (
    FIELDS,
    INCIDENTS_SQL,
    GatewayError,
    fetch_incidents,
    load_gateway_connection,
    parse_pagination,
    route_request,
    serialize_incident,
)


class IncidentGatewayApiTests(unittest.TestCase):
    def test_empty_page_returns_empty_array(self):
        status, body = route_request('/api/v1/incidents', fetcher=lambda *_args: [])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, [])

    def test_exact_projection_field_names_and_timestamp_serialization(self):
        row = {
            'id': uuid4(), 'title': 'Escalator repair', 'category': 'other',
            'locationName': 'Sudirman', 'latitude': Decimal('-6.208800'),
            'longitude': Decimal('106.820000'), 'nearestStationId': 'open-krl:ST',
            'nearestStationName': 'Sudirman', 'distanceToStationKm': Decimal('0.125'),
            'publishedAt': datetime(2026, 10, 22, 1, 2, 3, tzinfo=timezone.utc),
            'sourceName': 'Commuterline', 'verificationStatus': 'unverified',
        }
        result = serialize_incident(row)
        self.assertEqual(tuple(result), FIELDS)
        self.assertEqual(len(result), 12)
        self.assertIsInstance(result['id'], str)
        self.assertEqual(result['publishedAt'], '2026-10-22T01:02:03Z')
        self.assertEqual(result['latitude'], -6.2088)
        self.assertEqual(result['verificationStatus'], 'unverified')

    def test_unknown_values_remain_null(self):
        result = serialize_incident({field: None for field in FIELDS})
        for field in ('locationName', 'latitude', 'longitude', 'nearestStationId',
                      'nearestStationName', 'distanceToStationKm', 'publishedAt', 'sourceName'):
            self.assertIsNone(result[field])

    def test_pagination_defaults_and_bounds(self):
        self.assertEqual(parse_pagination(''), (20, 0))
        self.assertEqual(parse_pagination('limit=100&offset=15'), (100, 15))
        for query in ('limit=0', 'limit=101', 'limit=-1', 'offset=-1',
                      'offset=abc', 'limit=1&limit=2', 'page=2'):
            with self.subTest(query=query), self.assertRaises(GatewayError):
                parse_pagination(query)

    def test_invalid_pagination_is_bad_request_without_database_access(self):
        called = []
        status, body = route_request(
            '/api/v1/incidents', 'limit=1000',
            fetcher=lambda *_args: called.append(True),
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn('limit', body['error'])
        self.assertEqual(called, [])

    def test_query_reads_only_projection_and_uses_parameters_in_read_only_transaction(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        connect = Mock(return_value=context)
        self.assertEqual(fetch_incidents(10, 20, db_params={'user': 'gateway'}, connect=connect), [])
        self.assertEqual(connection.execute.call_args_list[0].args[0], 'SET TRANSACTION READ ONLY')
        query, params = connection.execute.call_args_list[1].args
        self.assertEqual(params, (10, 20))
        self.assertIn('FROM serving.gateway_corridor_incidents', query)
        self.assertNotIn('processing.', query)
        self.assertNotIn('collection.', query)
        self.assertNotIn('serving.incidents ', query)
        self.assertIn('LIMIT %s OFFSET %s', query)

    def test_database_failure_is_generic_and_does_not_leak_driver_message(self):
        status, body = route_request(
            '/api/v1/incidents', fetcher=lambda *_args: (_ for _ in ()).throw(
                RuntimeError('password=secret host=db.internal')),
        )
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(body, {'error': 'Incident data is temporarily unavailable.'})
        self.assertNotIn('secret', json.dumps(body))
        self.assertNotIn('db.internal', json.dumps(body))

    def test_health_is_liveness_only_and_unknown_paths_are_404(self):
        self.assertEqual(route_request('/health'), (HTTPStatus.OK, {'status': 'ok'}))
        self.assertEqual(route_request('/api/other'), (HTTPStatus.NOT_FOUND, {'error': 'Not found.'}))

    def test_connection_file_requires_0600_and_gateway_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gateway.json'
            config = {
                'host': 'aws-0-pooler.supabase.com', 'port': '5432',
                'dbname': 'postgres', 'user': 'moobby_incident_gateway_login.projectref',
                'password': 'offline-only',
            }
            path.write_text(json.dumps(config), encoding='utf-8')
            os.chmod(path, 0o600)
            loaded = load_gateway_connection(path)
            self.assertEqual(loaded['sslmode'], 'require')
            self.assertEqual(loaded['connect_timeout'], 10)
            config['user'] = 'postgres'
            path.write_text(json.dumps(config), encoding='utf-8')
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(GatewayError, 'dedicated incident gateway'):
                load_gateway_connection(path)
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(GatewayError, '0600'):
                load_gateway_connection(path)


if __name__ == '__main__':
    unittest.main()
