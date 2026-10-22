"""Validate and safely import KRL station reference data from open-krl.json.

The default invocation validates the complete local file only. Use --plan for
a read-only database diff and --apply to write additions. Existing station
changes require the additional explicit --approve-updates flag.
"""

import argparse
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / 'open-krl.json'
CONNECTION_FILE = ROOT / 'data/automation/reference/reference_connection.json'
REFERENCE_LOGIN = 'moobby_reference_login'
COORDINATE_SCALE = Decimal(100000)


class StationImportError(ValueError):
    """Safe-to-display error that never includes source or connection values."""


def _nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise StationImportError('Invalid {} in station source.'.format(field))
    return value.strip()


def _parse_date(value, field):
    try:
        return date.fromisoformat(_nonempty_string(value, field))
    except (ValueError, TypeError) as error:
        raise StationImportError('Invalid {} in source metadata.'.format(field)) from error


def _parse_timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(_nonempty_string(value, field).replace('Z', '+00:00'))
    except (ValueError, TypeError) as error:
        raise StationImportError('Invalid {} in source metadata.'.format(field)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StationImportError('{} must include a timezone.'.format(field))
    return parsed


def _source_metadata(meta):
    if not isinstance(meta, dict):
        raise StationImportError('Source metadata is missing.')
    source_url = _nonempty_string(meta.get('source_url'), 'source_url')
    if not source_url.startswith('https://'):
        raise StationImportError('Source URL must use HTTPS.')
    attribution = _nonempty_string(meta.get('attribution'), 'attribution')
    license_text = _nonempty_string(meta.get('licence'), 'licence')
    license_lower = license_text.lower()
    if 'cc-by 4.0' not in license_lower or 'openstreetmap contributors' not in license_lower or 'odbl' not in license_lower:
        raise StationImportError('Dataset license or coordinate attribution is incomplete.')
    source_systems = meta.get('systems')
    krl_system = source_systems.get('krl') if isinstance(source_systems, dict) else None
    if not isinstance(krl_system, dict):
        raise StationImportError('KRL source metadata is missing.')
    if krl_system.get('name') != 'KRL Commuter Line':
        raise StationImportError('Unexpected KRL source identity.')

    return {
        'source_url': source_url,
        'weekday_capture': _parse_date(meta.get('weekday_capture'), 'weekday_capture'),
        'sunday_capture': _parse_date(meta.get('sunday_capture'), 'sunday_capture'),
        'built_at': _parse_timestamp(meta.get('built_at'), 'built_at'),
        'effective_since': _parse_date(krl_system.get('effective_since'), 'effective_since'),
        'checked_at': _parse_date(krl_system.get('checked_at'), 'checked_at'),
        'dataset_license': license_text.split(';', 1)[0].strip(),
        'source_attribution': attribution,
        'coordinate_source': 'OpenStreetMap',
        'coordinate_attribution': '© OpenStreetMap contributors',
        'coordinate_license': 'ODbL',
        'source_metadata': meta,
    }


def extract_stations(source_bytes):
    """Validate the whole JSON document, then return normalized KRL stations."""
    try:
        document = json.loads(source_bytes.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError, AttributeError) as error:
        raise StationImportError('Station source is not valid UTF-8 JSON.') from error
    if not isinstance(document, dict):
        raise StationImportError('Station source root must be an object.')
    lines = document.get('lines')
    stations = document.get('stations')
    if not isinstance(lines, list) or not isinstance(stations, list):
        raise StationImportError('Source lines and stations must be arrays.')
    metadata = _source_metadata(document.get('meta'))

    line_keys = set()
    krl_indexes = set()
    for index, line in enumerate(lines):
        if not isinstance(line, list) or len(line) != 5:
            raise StationImportError('A line record has an invalid structure.')
        key = _nonempty_string(line[0], 'line key')
        _nonempty_string(line[1], 'line name')
        _nonempty_string(line[2], 'line color')
        system = _nonempty_string(line[3], 'line system')
        _nonempty_string(line[4], 'line code')
        if key in line_keys:
            raise StationImportError('Duplicate line identifiers exist in the source.')
        line_keys.add(key)
        if system == 'krl':
            krl_indexes.add(index)
    if not krl_indexes:
        raise StationImportError('Source contains no KRL lines.')

    seen_codes = set()
    normalized = []
    for station in stations:
        if not isinstance(station, list) or len(station) != 5:
            raise StationImportError('A station record has an invalid structure.')
        code = _nonempty_string(station[0], 'station code')
        name = _nonempty_string(station[1], 'station name')
        if code in seen_codes:
            raise StationImportError('Duplicate station codes exist in the source.')
        seen_codes.add(code)

        longitude_raw, latitude_raw, memberships = station[2], station[3], station[4]
        if (isinstance(longitude_raw, bool) or not isinstance(longitude_raw, int)
                or isinstance(latitude_raw, bool) or not isinstance(latitude_raw, int)):
            raise StationImportError('Station coordinates must be integer source values.')
        if not isinstance(memberships, list) or not memberships:
            raise StationImportError('Station line memberships must be a nonempty array.')
        if any(isinstance(index, bool) or not isinstance(index, int)
               or index < 0 or index >= len(lines) for index in memberships):
            raise StationImportError('Station line membership references an unknown line.')
        if len(set(memberships)) != len(memberships):
            raise StationImportError('Duplicate station line memberships exist.')
        longitude = Decimal(longitude_raw) / COORDINATE_SCALE
        latitude = Decimal(latitude_raw) / COORDINATE_SCALE
        if not Decimal(-180) <= longitude <= Decimal(180) or not Decimal(-90) <= latitude <= Decimal(90):
            raise StationImportError('Station coordinates are outside valid geographic ranges.')

        selected_indexes = sorted(krl_indexes.intersection(memberships))
        if not selected_indexes:
            continue
        line_memberships = [lines[index][0] for index in selected_indexes]
        normalized.append({
            'station_id': 'open-krl:' + code,
            'source_station_code': code,
            'canonical_name': name,
            'aliases': [],
            'latitude': latitude,
            'longitude': longitude,
            'line_memberships': line_memberships,
            'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
        })

    if not normalized:
        raise StationImportError('Source contains no stations assigned to KRL lines.')
    ids = [row['station_id'] for row in normalized]
    names = [row['canonical_name'].casefold() for row in normalized]
    if len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise StationImportError('Duplicate KRL station identifiers or names exist.')
    return {
        'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
        'metadata': metadata,
        'stations': normalized,
    }


SOURCE_FIELDS = ('source_station_code', 'canonical_name', 'latitude', 'longitude', 'line_memberships')


def _normalize_db_station(row):
    if isinstance(row, dict):
        value = dict(row)
    else:
        value = dict(zip(('station_id', 'source_station_code', 'canonical_name', 'latitude',
                          'longitude', 'line_memberships'), row))
    value['latitude'] = Decimal(value['latitude'])
    value['longitude'] = Decimal(value['longitude'])
    value['line_memberships'] = list(value['line_memberships'])
    return value


def build_change_report(existing_rows, incoming_rows):
    """Compare source-owned fields without treating curated aliases as source data."""
    existing = {_normalize_db_station(row)['station_id']: _normalize_db_station(row)
                for row in existing_rows}
    incoming = {row['station_id']: row for row in incoming_rows}
    added = sorted(set(incoming) - set(existing))
    removed = sorted(set(existing) - set(incoming))
    changed = []
    unchanged = []
    for station_id in sorted(set(existing) & set(incoming)):
        fields = [field for field in SOURCE_FIELDS
                  if existing[station_id][field] != incoming[station_id][field]]
        if fields:
            changed.append({'station_id': station_id, 'fields': fields})
        else:
            unchanged.append(station_id)
    return {
        'added': added,
        'changed': changed,
        'removed_from_source': removed,
        'unchanged_count': len(unchanged),
        'incoming_count': len(incoming),
    }


def load_reference_connection(path=None):
    """Load separate 0600 reference-admin credentials; never show their values."""
    config_path = Path(path or os.environ.get('KRL_REFERENCE_DB_CONFIG') or CONNECTION_FILE)
    if config_path.is_symlink() or not config_path.is_file():
        raise StationImportError('Reference database credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise StationImportError('Reference database credential file permissions must be 0600.')
    try:
        value = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StationImportError('Reference database credential file is unreadable or malformed.') from error
    allowed = {'host', 'port', 'dbname', 'user', 'password'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not all(isinstance(value.get(key), str) and value[key]
                       for key in ('host', 'dbname', 'user', 'password'))):
        raise StationImportError('Reference database credential file has an invalid format.')
    if value['user'] != REFERENCE_LOGIN and not value['user'].startswith(REFERENCE_LOGIN + '.'):
        raise StationImportError('Reference database credentials must use the dedicated admin login.')
    if not value['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise StationImportError('Reference database host must be a Supabase PostgreSQL host.')
    if 'port' in value and not (isinstance(value['port'], int) and not isinstance(value['port'], bool) or isinstance(value['port'], str) and value['port'].isdigit()):
        raise StationImportError('Reference database port is invalid.')
    return dict(value, sslmode='require', connect_timeout=15)


def _db_station_rows(conn):
    rows = conn.execute(
        "SELECT station_id, source_station_code, canonical_name, latitude, longitude, line_memberships "
        "FROM reference.krl_stations ORDER BY station_id"
    ).fetchall()
    return rows


def _metadata_values(metadata):
    return (
        metadata['source_url'], metadata['weekday_capture'], metadata['sunday_capture'],
        metadata['built_at'], metadata['effective_since'], metadata['checked_at'],
        metadata['dataset_license'], metadata['source_attribution'],
        metadata['coordinate_source'], metadata['coordinate_attribution'],
        metadata['coordinate_license'], json.dumps(metadata['source_metadata'], sort_keys=True),
    )


def _store_dataset_metadata(conn, source):
    metadata = source['metadata']
    conn.execute(
        """INSERT INTO reference.krl_station_datasets (
             source_sha256, source_url, weekday_capture, sunday_capture, built_at,
             effective_since, checked_at, dataset_license, source_attribution,
             coordinate_source, coordinate_attribution, coordinate_license, source_metadata
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
           ON CONFLICT (source_sha256) DO NOTHING""",
        (source['source_sha256'],) + _metadata_values(metadata)[:-1]
        + (json.dumps(metadata['source_metadata'], sort_keys=True),),
    )
    existing = conn.execute(
        "SELECT source_url, weekday_capture, sunday_capture, built_at, effective_since, checked_at, "
        "dataset_license, source_attribution, coordinate_source, coordinate_attribution, "
        "coordinate_license, source_metadata::text "
        "FROM reference.krl_station_datasets WHERE source_sha256 = %s",
        (source['source_sha256'],),
    ).fetchone()
    expected = _metadata_values(metadata)
    if not existing or tuple(existing[:-1]) != expected[:-1]:
        raise StationImportError('Existing dataset provenance conflicts with this source hash.')
    try:
        existing_json = json.loads(existing[-1])
    except (TypeError, json.JSONDecodeError) as error:
        raise StationImportError('Existing dataset provenance is malformed.') from error
    if existing_json != metadata['source_metadata']:
        raise StationImportError('Existing dataset provenance conflicts with this source hash.')


def _write_changes(conn, source, report):
    _store_dataset_metadata(conn, source)
    incoming = {row['station_id']: row for row in source['stations']}
    current = {_normalize_db_station(row)['station_id']: _normalize_db_station(row)
               for row in _db_station_rows(conn)}
    for station_id in report['added']:
        row = incoming[station_id]
        conn.execute(
            """INSERT INTO reference.krl_stations (
                 station_id, source_station_code, canonical_name, aliases, latitude,
                 longitude, line_memberships, source_sha256
               ) VALUES (%s, %s, %s, '{}'::text[], %s, %s, %s, %s)""",
            (row['station_id'], row['source_station_code'], row['canonical_name'],
             row['latitude'], row['longitude'], row['line_memberships'], row['source_sha256']),
        )
    for change in report['changed']:
        row = incoming[change['station_id']]
        conn.execute(
            """UPDATE reference.krl_stations
               SET source_station_code = %s, canonical_name = %s, latitude = %s,
                   longitude = %s, line_memberships = %s, source_sha256 = %s
               WHERE station_id = %s""",
            (row['source_station_code'], row['canonical_name'], row['latitude'],
             row['longitude'], row['line_memberships'], row['source_sha256'], change['station_id']),
        )
    return len(report['added']) + len(report['changed'])


def database_change_report(source, db_params, apply=False, approve_updates=False):
    """Read/compare or apply under one short transaction; never removes rows."""
    try:
        import psycopg
    except ImportError as error:
        raise StationImportError('Install the repository PostgreSQL dependency first.') from error
    try:
        with psycopg.connect(**db_params, prepare_threshold=None) as conn:
            if apply:
                conn.execute("SET LOCAL statement_timeout = '20s'")
                conn.execute('LOCK TABLE reference.krl_stations IN SHARE ROW EXCLUSIVE MODE')
            else:
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout = '20s'")
            existing = _db_station_rows(conn)
            report = build_change_report(existing, source['stations'])
            report['source_sha256'] = source['source_sha256']
            if report['changed'] and not approve_updates:
                report['status'] = 'approval_required' if apply else 'planned'
                if apply:
                    return report
            if apply:
                _write_changes(conn, source, report)
                report['status'] = 'applied'
            else:
                report['status'] = 'planned'
            return report
    except StationImportError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise StationImportError('Reference database operation failed: {}{}'.format(type(error).__name__, suffix)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--plan', action='store_true', help='Connect read-only and print a change report.')
    parser.add_argument('--apply', action='store_true', help='Import additions and approved updates transactionally.')
    parser.add_argument('--approve-updates', action='store_true', help='Approve changes to existing station records.')
    args = parser.parse_args(argv)
    if args.plan and args.apply:
        parser.error('--plan and --apply cannot be used together.')
    if args.approve_updates and not args.apply:
        parser.error('--approve-updates requires --apply.')
    try:
        try:
            source_bytes = args.source.read_bytes()
        except OSError as error:
            raise StationImportError('Station source file is unavailable.') from error
        source = extract_stations(source_bytes)
        if args.plan or args.apply:
            report = database_change_report(
                source, load_reference_connection(), apply=args.apply,
                approve_updates=args.approve_updates,
            )
        else:
            report = {
                'status': 'source_validated', 'source_sha256': source['source_sha256'],
                'incoming_count': len(source['stations']),
            }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if report.get('status') != 'approval_required' else 2
    except StationImportError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
