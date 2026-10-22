"""Export a small, read-only sample of stored raw snapshots to CSV."""

import argparse
import csv
import getpass
import json
import re
import sys
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from greenline_upload import CONNECTION, connection_params  # noqa: E402


OUTPUT_PATH = ROOT / 'data/orvix/raw_snapshots.csv'
CSV_FIELDS = (
    'snapshot_id',
    'raw_content_id',
    'platform',
    'external_id',
    'canonical_url',
    'author_handle',
    'raw_text',
    'text_kind',
    'text_completeness',
    'published_at',
    'published_at_basis',
    'captured_at',
    'capture_hash',
    'extraction_metadata_json',
    'observation_provenance_json',
)

FETCH_FIELDS = (
    'snapshot_id',
    'raw_content_id',
    'platform',
    'external_id',
    'canonical_url',
    'author_handle',
    'raw_text',
    'text_kind',
    'text_completeness',
    'published_at',
    'published_at_basis',
    'captured_at',
    'capture_hash',
    'extraction_metadata',
    'observation_provenance',
)

SNAPSHOT_QUERY = """
SELECT
    s.id AS snapshot_id,
    c.id AS raw_content_id,
    c.platform,
    c.external_id,
    c.canonical_url,
    s.author_handle,
    s.raw_text,
    s.text_kind,
    s.text_completeness,
    s.published_at,
    s.published_at_basis,
    s.captured_at,
    s.capture_hash,
    s.extraction_metadata,
    COALESCE(provenance.observations, '[]'::jsonb) AS observation_provenance
FROM collection.raw_snapshots AS s
JOIN collection.raw_contents AS c ON c.id = s.raw_content_id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
        jsonb_build_object(
            'observation_id', o.id,
            'run_id', r.id,
            'source_key', src.source_key,
            'collection_method', r.collection_method,
            'observed_at', o.captured_at
        ) ORDER BY o.captured_at, r.id, o.id
    ) AS observations
    FROM collection.content_observations AS o
    JOIN collection.collection_runs AS r ON r.id = o.run_id
    JOIN collection.sources AS src ON src.id = r.source_id
    WHERE o.snapshot_id = s.id AND o.raw_content_id = s.raw_content_id
) AS provenance ON TRUE
{processed_filter}
ORDER BY s.captured_at DESC, s.id ASC
LIMIT %s
"""

UNPROCESSED_FILTER = """WHERE NOT EXISTS (
    SELECT 1
    FROM processing.events AS e
    JOIN processing.processing_runs AS pr ON pr.id = e.run_id
    WHERE e.snapshot_id = s.id
      AND pr.manifest_status = 'completed'
      AND pr.imported_at IS NOT NULL
)"""


class ExportError(Exception):
    """Safe-to-display exporter error without connection or row contents."""


def connection_parameters():
    """Load saved private credentials or prompt without persisting credentials."""
    if CONNECTION.exists() or CONNECTION.is_symlink():
        try:
            return connection_params(CONNECTION)
        except (ValueError, OSError, TypeError, json.JSONDecodeError) as error:
            raise ExportError('Saved database credentials are invalid or not private.') from error

    if not sys.stdin.isatty():
        raise ExportError('No saved credentials; run from an interactive terminal to enter them securely.')

    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as error:
        raise ExportError('Install the repository PostgreSQL dependency before exporting.') from error

    uri = getpass.getpass('Supabase PostgreSQL URI (hidden): ').strip()
    uri = re.sub(r'\[(?:YOUR[-_ ]?PASSWORD|PASSWORD)\]', '', uri, flags=re.I)
    if not uri.startswith(('postgresql://', 'postgres://')):
        raise ExportError('A PostgreSQL URI is required.')
    try:
        raw = conninfo_to_dict(uri)
    except Exception as error:
        raise ExportError('The PostgreSQL URI could not be parsed.') from error
    params = {key: raw[key] for key in ('host', 'port', 'dbname', 'user', 'password') if raw.get(key)}
    params.setdefault('dbname', 'postgres')
    if not params.get('password'):
        params['password'] = getpass.getpass('Database role password (hidden): ')
    if (not all(params.get(key) for key in ('host', 'dbname', 'user', 'password'))
            or not params['host'].lower().endswith(('.supabase.com', '.supabase.co'))):
        raise ExportError('URI must identify a Supabase PostgreSQL host and database role.')
    return dict(params, sslmode='require', connect_timeout=15)


def record_mapping(record):
    """Map a fetched record to the agreed CSV fields without altering raw_text."""
    row = dict(record) if hasattr(record, 'keys') else dict(zip(FETCH_FIELDS, record))
    return row


def _provenance_items(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError('Observation provenance must be a JSON array.')
    return value


def deduplicate_rows(rows):
    """Keep one row per snapshot and merge any observation provenance."""
    result = []
    by_snapshot = {}
    for source in rows:
        row = dict(source)
        snapshot_id = str(row['snapshot_id'])
        row['snapshot_id'] = snapshot_id
        current = by_snapshot.get(snapshot_id)
        if current is None:
            row['observation_provenance'] = _provenance_items(row.get('observation_provenance'))
            by_snapshot[snapshot_id] = row
            result.append(row)
            continue

        for field in FETCH_FIELDS:
            if field in ('observation_provenance',):
                continue
            if current.get(field) != row.get(field):
                raise ValueError('Conflicting rows share a snapshot_id.')

        merged = {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')): item
            for item in current['observation_provenance']
        }
        for item in _provenance_items(row.get('observation_provenance')):
            merged[json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))] = item
        current['observation_provenance'] = sorted(
            merged.values(),
            key=lambda item: (str(item.get('observed_at', '')), str(item.get('run_id', '')),
                              str(item.get('observation_id', ''))),
        )
    return result


def _isoformat(value):
    return value.isoformat() if hasattr(value, 'isoformat') else ('' if value is None else value)


def csv_row(record):
    row = record_mapping(record)
    extraction_metadata = row.get('extraction_metadata')
    provenance = row.get('observation_provenance')
    return {
        'snapshot_id': str(row['snapshot_id']),
        'raw_content_id': str(row['raw_content_id']),
        'platform': row['platform'],
        'external_id': row['external_id'],
        'canonical_url': row['canonical_url'],
        'author_handle': row.get('author_handle') or '',
        'raw_text': row['raw_text'],
        'text_kind': row['text_kind'],
        'text_completeness': row['text_completeness'],
        'published_at': _isoformat(row.get('published_at')),
        'published_at_basis': row['published_at_basis'],
        'captured_at': _isoformat(row.get('captured_at')),
        'capture_hash': row['capture_hash'],
        'extraction_metadata_json': json.dumps(
            extraction_metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ) if extraction_metadata is not None else '',
        'observation_provenance_json': json.dumps(
            _provenance_items(provenance), ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ),
    }


def write_csv(stream, records):
    rows = deduplicate_rows(record_mapping(record) for record in records)
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='raise')
    writer.writeheader()
    writer.writerows(csv_row(row) for row in rows)
    return len(rows)


def export(limit, db_params=None, include_processed=False, snapshot_ids=None, output_path=None):
    try:
        import psycopg
    except ImportError as error:
        raise ExportError('Install the repository PostgreSQL dependency before exporting.') from error

    try:
        params = db_params if db_params is not None else connection_parameters()
        with psycopg.connect(**params, prepare_threshold=None) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '20s'")
            metadata_column = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'collection' AND table_name = 'raw_snapshots' "
                "AND column_name = 'extraction_metadata'"
            ).fetchone()
            if not metadata_column:
                raise ExportError('collection.raw_snapshots.extraction_metadata is unavailable.')
            if snapshot_ids is not None:
                try:
                    selected_ids = [str(uuid.UUID(str(value))) for value in snapshot_ids]
                except (ValueError, TypeError, AttributeError) as error:
                    raise ExportError('A claimed snapshot ID is invalid.') from error
                processed_filter = 'WHERE s.id = ANY(%s::uuid[])'
                query_params = (selected_ids, limit)
            else:
                processed_filter = '' if include_processed else UNPROCESSED_FILTER
                query_params = (limit,)
            records = conn.execute(
                SNAPSHOT_QUERY.format(processed_filter=processed_filter), query_params
            ).fetchall()
    except ExportError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise ExportError('Read-only snapshot export failed: {}{}'.format(type(error).__name__, suffix)) from error

    destination = Path(output_path or OUTPUT_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('w', encoding='utf-8', newline='') as stream:
        count = write_csv(stream, records)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=5, help='Maximum snapshots to export (default: 5).')
    parser.add_argument('--include-processed', action='store_true',
                        help='Include snapshots already processed in imported completed runs.')
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('--limit must be a positive integer')
    try:
        count = export(args.limit, include_processed=args.include_processed)
    except ExportError as error:
        print(str(error), file=sys.stderr)
        return 1
    print('Exported {} snapshots to {}'.format(count, OUTPUT_PATH.relative_to(ROOT)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
