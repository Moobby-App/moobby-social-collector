"""Read-only HTTP API for the approved Moobby CorridorIncident projection."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONNECTION_FILE = ROOT / 'data/automation/incident/gateway_connection.json'
GATEWAY_LOGIN = 'moobby_incident_gateway_login'
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
FIELDS = (
    'id', 'title', 'category', 'locationName', 'latitude', 'longitude',
    'nearestStationId', 'nearestStationName', 'distanceToStationKm',
    'publishedAt', 'sourceName', 'verificationStatus',
)

# These exact projection names are part of the serving contract. Keep all
# database reads inside this view; the view itself filters to eligible records.
INCIDENTS_SQL = '''
SELECT id, title, category, "locationName", latitude, longitude,
       "nearestStationId", "nearestStationName", "distanceToStationKm",
       "publishedAt", "sourceName", "verificationStatus"
FROM serving.gateway_corridor_incidents
ORDER BY "publishedAt" DESC NULLS LAST, id
LIMIT %s OFFSET %s
'''


class GatewayError(Exception):
    """Safe configuration or request error suitable for local display."""


def load_gateway_connection(path=None):
    """Read a private config for the dedicated read-only gateway login."""
    config_path = Path(path or os.environ.get('INCIDENT_GATEWAY_DB_CONFIG')
                       or DEFAULT_CONNECTION_FILE)
    if config_path.is_symlink() or not config_path.is_file():
        raise GatewayError('Gateway database credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
        raise GatewayError('Gateway credential file permissions must be exactly 0600.')
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GatewayError('Gateway credential file is unreadable or malformed.') from error
    required = {'host', 'dbname', 'user', 'password'}
    allowed = required | {'port'}
    if (not isinstance(config, dict) or set(config) - allowed or not required <= set(config)
            or not all(isinstance(config[key], str) and config[key] for key in required)):
        raise GatewayError('Gateway credential file has an invalid format.')
    username = config['user']
    if not (username == GATEWAY_LOGIN or username.startswith(GATEWAY_LOGIN + '.')):
        raise GatewayError('API must use the dedicated incident gateway database login.')
    if not config['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise GatewayError('Gateway database host must be a Supabase PostgreSQL host.')
    if 'port' in config and not str(config['port']).isdigit():
        raise GatewayError('Gateway database port is invalid.')
    params = dict(config, sslmode='require', connect_timeout=10)
    return params


def _connect_factory(connect=None):
    if connect is not None:
        return connect
    try:
        import psycopg
    except ImportError as error:
        raise GatewayError('Install the repository PostgreSQL dependency first.') from error
    return psycopg.connect


def _validate_page(value, *, name, default, maximum=None):
    if value is None:
        return default
    if not re.fullmatch(r'\d+', value):
        raise GatewayError('{} must be a non-negative integer.'.format(name))
    parsed = int(value)
    if name == 'limit' and (parsed < 1 or parsed > maximum):
        raise GatewayError('limit must be between 1 and {}.'.format(maximum))
    return parsed


def parse_pagination(query_string):
    values = parse_qs(query_string, keep_blank_values=True, strict_parsing=False)
    if set(values) - {'limit', 'offset'}:
        raise GatewayError('Only limit and offset pagination parameters are supported.')
    if any(len(items) != 1 for items in values.values()):
        raise GatewayError('Pagination parameters may only be provided once.')
    limit = _validate_page(values.get('limit', [None])[0], name='limit',
                           default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    offset = _validate_page(values.get('offset', [None])[0], name='offset', default=0)
    return limit, offset


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def serialize_incident(row):
    if isinstance(row, dict):
        values = row
    else:
        values = dict(zip(FIELDS, row))
    return {field: _json_value(values.get(field)) for field in FIELDS}


def fetch_incidents(limit, offset, *, db_params=None, connect=None):
    """Fetch one read-only page from the serving projection."""
    params = db_params if db_params is not None else load_gateway_connection()
    factory = _connect_factory(connect)
    try:
        with factory(**params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            rows = connection.execute(INCIDENTS_SQL, (limit, offset)).fetchall()
        return [serialize_incident(row) for row in rows]
    except GatewayError:
        raise
    except Exception as error:
        # Do not expose driver diagnostics, connection strings, or server details.
        raise GatewayError('Incident data is temporarily unavailable.') from error


def route_request(path, query_string='', *, fetcher=fetch_incidents):
    """Framework-free request routing used by the HTTP handler and offline tests."""
    if path == '/health':
        return HTTPStatus.OK, {'status': 'ok'}
    if path != '/api/v1/incidents':
        return HTTPStatus.NOT_FOUND, {'error': 'Not found.'}
    try:
        limit, offset = parse_pagination(query_string)
    except GatewayError as error:
        return HTTPStatus.BAD_REQUEST, {'error': str(error)}
    try:
        return HTTPStatus.OK, fetcher(limit, offset)
    except Exception:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            'error': 'Incident data is temporarily unavailable.'
        }


class IncidentGatewayHandler(BaseHTTPRequestHandler):
    """Minimal JSON HTTP handler; no mutation routes are implemented."""

    fetcher = staticmethod(fetch_incidents)

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        request = urlsplit(self.path)
        status, payload = route_request(request.path, request.query, fetcher=self.fetcher)
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        # BaseHTTPRequestHandler logs request data to stderr. Keep this service
        # quiet by default and never emit database exceptions or credentials.
        return


def build_parser():
    parser = argparse.ArgumentParser(description='Run the read-only Moobby incident API.')
    parser.add_argument('--host', default='127.0.0.1', help='Bind address (default: localhost).')
    parser.add_argument('--port', type=int, default=8080, help='HTTP port (default: 8080).')
    parser.add_argument('--config', help='Private gateway connection JSON path.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit('port must be between 1 and 65535')
    if args.config:
        os.environ['INCIDENT_GATEWAY_DB_CONFIG'] = args.config
    # Validate configuration at startup so a misconfigured login fails before
    # serving requests. This opens no database connection.
    try:
        load_gateway_connection()
    except GatewayError as error:
        raise SystemExit(str(error)) from error
    server = ThreadingHTTPServer((args.host, args.port), IncidentGatewayHandler)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.info('Incident gateway listening on %s:%s', args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
