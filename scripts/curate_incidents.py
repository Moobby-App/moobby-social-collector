"""Manual terminal workflow for incident curation.

Candidate listing and event inspection use the dedicated read-only login at
data/automation/incident/curation_connection.json (0600). Serving-schema writes
use a separate curator login at data/automation/incident/curator_connection.json
(0600), default to a rollback dry-run, and never run automatically.
"""

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from scripts.krl_station_resolver import (
    KrlStationResolver,
    StationResolutionError,
    load_reference_stations,
)


ROOT = Path(__file__).resolve().parents[1]
CONNECTION_FILE = ROOT / 'data/automation/incident/curation_connection.json'
CURATION_LOGIN = 'moobby_incident_readonly_login'
LIST_QUERY = """
SELECT e.id AS event_id, e.snapshot_id, e.event_index, e.event_status,
       e.disruption_type, e.severity, e.affected_entities,
       e.event_start_time, e.event_end_time, e.uncertainty_status,
       e.uncertainty_note, e.explanation, e.evidence_quotes,
       s.published_at, s.author_handle, s.author_display_name,
       rc.canonical_url AS source_post_url,
       latest_review.review_status,
       ie.incident_id, ie.link_status, ie.relationship, ie.is_primary
FROM processing.events AS e
JOIN collection.raw_snapshots AS s ON s.id = e.snapshot_id
JOIN collection.raw_contents AS rc ON rc.id = s.raw_content_id
LEFT JOIN LATERAL (
  SELECT r.revision, r.review_status
  FROM processing.event_reviews AS r
  WHERE r.event_id = e.id
  ORDER BY r.revision DESC
  LIMIT 1
) AS latest_review ON true
LEFT JOIN serving.incident_evidence AS ie ON ie.processing_event_id = e.id
WHERE e.event_status = 'potential_disruption' AND e.event_index > 0
ORDER BY s.published_at DESC NULLS LAST, e.id, ie.created_at, ie.id
"""


class CurationError(Exception):
    """Safe-to-display error for incident curation reads."""


def load_curation_connection(path=None):
    """Load a private, dedicated read-only Supabase login configuration."""
    config_path = Path(
        path or os.environ.get('INCIDENT_CURATION_DB_CONFIG') or CONNECTION_FILE
    )
    if config_path.is_symlink() or not config_path.is_file():
        raise CurationError('Incident curation database credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise CurationError('Curation credential file permissions must be 0600.')
    try:
        value = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CurationError('Curation credential file is unreadable or malformed.') from error
    allowed = {'host', 'port', 'dbname', 'user', 'password'}
    required = {'host', 'dbname', 'user', 'password'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not required <= set(value)
            or not all(isinstance(value[key], str) and value[key] for key in required)):
        raise CurationError('Curation credential file has an invalid format.')
    username = value['user']
    if username != CURATION_LOGIN and not username.startswith(CURATION_LOGIN + '.'):
        raise CurationError('Curation must use the dedicated read-only database login.')
    if not value['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise CurationError('Curation database host must be a Supabase PostgreSQL host.')
    if 'port' in value and not str(value['port']).isdigit():
        raise CurationError('Curation database port is invalid.')
    return dict(value, sslmode='require', connect_timeout=15)


def fetch_candidates(db_params, connect=None, event_id=None):
    """Read all event candidates and associations in one read-only transaction."""
    if connect is None:
        try:
            import psycopg
        except ImportError as error:
            raise CurationError('Install the repository PostgreSQL dependency first.') from error
        connect = psycopg.connect
    query = LIST_QUERY
    params = ()
    if event_id is not None:
        try:
            normalized_event_id = str(uuid.UUID(str(event_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise CurationError('Event ID must be a UUID.') from error
        query = LIST_QUERY.replace(
            "WHERE e.event_status = 'potential_disruption' AND e.event_index > 0",
            "WHERE e.event_status = 'potential_disruption' AND e.event_index > 0 AND e.id = %s::uuid",
        )
        params = (normalized_event_id,)
    try:
        with connect(**db_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            connection.execute("SET LOCAL statement_timeout = '15s'")
            rows = connection.execute(query, params).fetchall() if params else connection.execute(query).fetchall()
        return _group_rows(rows)
    except CurationError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise CurationError(
            'Read-only incident query failed: {}{}'.format(type(error).__name__, suffix)
        ) from error


def _group_rows(rows):
    events = {}
    for row in rows:
        event_id = str(row[0])
        event = events.get(event_id)
        if event is None:
            event = {
                'event_id': event_id,
                'snapshot_id': str(row[1]),
                'event_index': row[2],
                'event_status': row[3],
                'disruption_type': row[4],
                'severity': row[5],
                'affected_entities': row[6] or [],
                'event_start_time': row[7],
                'event_end_time': row[8],
                'uncertainty_status': row[9],
                'uncertainty_note': row[10],
                'explanation': row[11],
                'evidence_quotes': row[12] or [],
                'published_at': row[13],
                'author_handle': row[14],
                'author_display_name': row[15],
                'source_post_url': row[16],
                'event_review_revision': row[17],
                'review_status': row[18] or 'not_reviewed',
                'incident_associations': [],
            }
            events[event_id] = event
        if row[19] is not None:
            association = {
                'incident_id': str(row[19]),
                'link_status': row[20],
                'relationship': row[21],
                'is_primary': row[22],
            }
            if association not in event['incident_associations']:
                event['incident_associations'].append(association)
    return list(events.values())


def resolve_candidates(events, resolver):
    """Attach exact-match station resolutions without asserting incident location."""
    for event in events:
        event['station_resolutions'] = resolver.resolve_affected_entities(
            event['affected_entities']
        )
    return events


def _display_author(event):
    handle = event.get('author_handle')
    name = event.get('author_display_name')
    if handle:
        return '@' + str(handle).lstrip('@')
    return str(name) if name else '(author unavailable)'


def _display_time(value):
    return value.isoformat(sep=' ', timespec='seconds') if value else '(unavailable)'


def _display_entities(entities, resolutions=None):
    if not entities:
        return '(none extracted)'
    if resolutions is not None:
        return '; '.join(
            '{}: {} [{}]'.format(item['entity_type'], item['value'], item['resolution_status'])
            for item in resolutions
        )
    return '; '.join('{}: {}'.format(item.get('entity_type', '?'), item.get('value', '?'))
                     for item in entities)


def _display_associations(associations):
    if not associations:
        return 'no serving incident association'
    return 'associated ({})'.format(len(associations))


def format_list(events):
    """Render compact candidate rows without raw post text."""
    if not events:
        return 'No potential-disruption candidates found.'
    headings = ('EVENT ID', 'TYPE', 'SEVERITY', 'AUTHOR', 'PUBLISHED', 'ENTITIES', 'REVIEW', 'INCIDENT')
    rows = []
    for event in events:
        rows.append((
            event['event_id'], event['disruption_type'], event['severity'],
            _display_author(event), _display_time(event.get('published_at')),
            _display_entities(event['affected_entities'], event.get('station_resolutions')),
            '{} (r{})'.format(
                event['review_status'], event.get('event_review_revision') or '?'
            ),
            _display_associations(event['incident_associations']),
        ))
    widths = [min(max(len(headings[i]), *(len(row[i]) for row in rows)), 50) for i in range(len(headings))]
    header = '  '.join(headings[i].ljust(widths[i]) for i in range(len(headings)))
    divider = '  '.join('-' * width for width in widths)
    body = ['  '.join(value[:widths[i]].ljust(widths[i]) for i, value in enumerate(row)) for row in rows]
    return '\n'.join([header, divider] + body)


def format_detail(event):
    """Render original evidence fields and explicit station-resolution results."""
    lines = [
        'Event: {}'.format(event['event_id']),
        'Status: {} (index {})'.format(event['event_status'], event['event_index']),
        'Disruption: {} | severity: {}'.format(event['disruption_type'], event['severity']),
        'Source author: {}'.format(_display_author(event)),
        'Source post: {}'.format(event.get('source_post_url') or '(URL unavailable)'),
        'Published at: {}'.format(_display_time(event.get('published_at'))),
        'Event start: {}'.format(_display_time(event.get('event_start_time'))),
        'Event end: {}'.format(_display_time(event.get('event_end_time'))),
        'Review status: {} (revision {})'.format(
            event['review_status'], event.get('event_review_revision') or 'unavailable'
        ),
        'Serving association: {}'.format(
            '; '.join('{} [{}; {}; {}]'.format(
                item['incident_id'], item['link_status'], item['relationship'],
                'primary' if item['is_primary'] else 'non-primary',
            ) for item in event['incident_associations'])
            if event['incident_associations'] else 'no serving incident association'
        ),
        'Explanation: {}'.format(event['explanation']),
        'Uncertainty: {} — {}'.format(
            event['uncertainty_status'], event.get('uncertainty_note') or '(no note)'
        ),
        'Affected entities and station resolution:',
    ]
    if not event['station_resolutions']:
        lines.append('  (none extracted)')
    for result in event['station_resolutions']:
        line = '  {entity_type}: {value} [{resolution_status}] ({reason})'.format(
            entity_type=result['entity_type'], value=result['value'],
            resolution_status=result['resolution_status'],
            reason=result.get('resolution_reason') or 'no reason supplied',
        )
        if result['resolution_status'] == 'resolved':
            line += ' -> {} / {} ({}, {})'.format(
                result['canonical_name'], result['station_id'],
                result['latitude'], result['longitude'],
            )
            line += ' [mentioned station only; not confirmed incident location]'
        elif result['resolution_status'] == 'ambiguous':
            line += ' -> {} exact station matches; choose manually'.format(result['candidate_count'])
        lines.append(line)
        lines.append('    Evidence: {}'.format(result['evidence_quote']))
    lines.append('Evidence quotes:')
    if event['evidence_quotes']:
        lines.extend('  - {!r}'.format(quote) for quote in event['evidence_quotes'])
    else:
        lines.append('  (none)')
    return '\n'.join(lines)


def _load_resolver(db_params):
    try:
        return load_reference_stations(db_params)
    except StationResolutionError as error:
        raise CurationError(str(error)) from error


def run_list(db_params, limit=None, resolver=None, connect=None):
    events = fetch_candidates(db_params, connect=connect)
    if resolver is None:
        resolver = _load_resolver(db_params)
    resolve_candidates(events, resolver)
    if limit is not None:
        events = events[:limit]
    return format_list(events)


def run_inspect(event_id, db_params, resolver=None, connect=None):
    try:
        event_id = str(uuid.UUID(str(event_id)))
    except (ValueError, TypeError, AttributeError) as error:
        raise CurationError('Event ID must be a UUID.') from error
    events = fetch_candidates(db_params, connect=connect, event_id=event_id)
    event = next((item for item in events if item['event_id'] == event_id), None)
    if event is None:
        raise CurationError('No potential-disruption event found with that ID.')
    if resolver is None:
        resolver = _load_resolver(db_params)
    resolve_candidates([event], resolver)
    return format_detail(event)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    list_parser = subparsers.add_parser('list', help='List potential-disruption candidates.')
    list_parser.add_argument('--limit', type=int, default=50, help='Maximum displayed candidates (default: 50).')
    inspect_parser = subparsers.add_parser('inspect', help='Inspect one candidate event by UUID.')
    inspect_parser.add_argument('event_id')
    from scripts.incident_curation_writes import add_write_subcommands
    add_write_subcommands(subparsers)
    args = parser.parse_args(argv)
    if args.command == 'list' and args.limit < 1:
        parser.error('--limit must be a positive integer')
    try:
        if args.command not in {'list', 'inspect'}:
            from scripts.incident_curation_writes import run_write_command
            print(run_write_command(args))
            return 0
        params = load_curation_connection()
        if args.command == 'list':
            print(run_list(params, limit=args.limit))
        else:
            print(run_inspect(args.event_id, params))
    except (CurationError, StationResolutionError) as error:
        print('Incident curation failed: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
