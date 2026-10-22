"""Validate and import one completed Orvix processing run."""

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.export_orvix_input import CSV_FIELDS as INPUT_FIELDS  # noqa: E402
from scripts.export_orvix_input import ExportError, connection_parameters  # noqa: E402
from scripts.process_orvix_csv import (  # noqa: E402
    MANIFEST_VERSION,
    OUTPUT_FIELDS,
    PROMPT_VERSION,
)


EVENT_STATUS_VALUES = {'potential_disruption', 'no_disruption', 'unclear'}
DISRUPTION_TYPES = {
    'delay', 'service_suspension', 'route_change', 'signal_or_power_issue',
    'infrastructure_issue', 'accident_or_safety_issue', 'overcrowding',
    'flooding_or_weather', 'other', 'unknown', 'not_applicable',
}
EVIDENCE_VALUES = {'explicit', 'inferred', 'unknown', 'not_applicable'}
SEVERITY_VALUES = {'low', 'moderate', 'high', 'critical', 'unknown', 'not_applicable'}
UNCERTAINTY_VALUES = {'yes', 'no', 'unknown'}
ENTITY_TYPES = {'line', 'station', 'route'}
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

RUN_COLUMNS = (
    'manifest_version', 'manifest_status', 'schema_version', 'model',
    'processor_version', 'prompt_version', 'started_at', 'completed_at',
    'input_csv_sha256', 'output_csv_sha256',
)
EVENT_COLUMNS = (
    'snapshot_id', 'event_index', 'event_status', 'disruption_type',
    'disruption_type_evidence', 'affected_entities', 'severity',
    'severity_evidence', 'event_start_time', 'event_start_time_evidence',
    'event_end_time', 'event_end_time_evidence', 'uncertainty_status',
    'uncertainty_note', 'explanation', 'evidence_quotes',
)


class ImportValidationError(ValueError):
    """Artifact error with no source text or credential content."""


class ImportConflictError(ValueError):
    """An existing run ID is associated with different imported data."""


class ImportDatabaseError(RuntimeError):
    """Database error with sensitive connection details omitted."""


class ImportJobCompletionError(RuntimeError):
    """Claimed jobs could not be safely completed with the imported results."""


def _aware_datetime(value, field):
    if not isinstance(value, str) or not value:
        raise ImportValidationError('Invalid {} timestamp.'.format(field))
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ImportValidationError('Invalid {} timestamp.'.format(field)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ImportValidationError('{} must include a timezone.'.format(field))
    return parsed


def _uuid(value, field):
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ImportValidationError('Invalid {} UUID.'.format(field)) from error


def _enum(value, allowed, field):
    if value not in allowed:
        raise ImportValidationError('Invalid {} value.'.format(field))


def _read_csv(path, expected_fields, label):
    try:
        with path.open('r', encoding='utf-8', newline='') as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(expected_fields):
                raise ImportValidationError('{} CSV header does not match its contract.'.format(label))
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ImportValidationError('{} CSV has a malformed row.'.format(label))
                rows.append(row)
    except ImportValidationError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise ImportValidationError('{} CSV could not be read.'.format(label)) from error
    return rows


def _safe_artifact(run_directory, artifact, expected_name):
    if not isinstance(artifact, dict) or set(artifact) != {'path', 'sha256'}:
        raise ImportValidationError('Manifest artifact metadata is malformed.')
    if artifact['path'] != expected_name or not isinstance(artifact['path'], str):
        raise ImportValidationError('Manifest artifact path does not match the run contract.')
    digest = artifact['sha256']
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ImportValidationError('Manifest artifact hash is malformed.')
    path = run_directory / expected_name
    if not path.is_file() or path.is_symlink():
        raise ImportValidationError('Required run artifact is unavailable.')
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ImportValidationError('Required run artifact could not be read.') from error
    if actual != digest:
        raise ImportValidationError('Run artifact SHA-256 does not match the manifest.')
    return path, digest


def _parse_json_array(value, field):
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ImportValidationError('{} must contain valid JSON.'.format(field)) from error
    if not isinstance(parsed, list):
        raise ImportValidationError('{} must be a JSON array.'.format(field))
    return parsed


def _validate_entity(entity, raw_text):
    if not isinstance(entity, dict) or set(entity) != {
            'entity_type', 'value', 'evidence_type', 'evidence_quote'}:
        raise ImportValidationError('Affected entity does not match the event contract.')
    _enum(entity['entity_type'], ENTITY_TYPES, 'entity_type')
    _enum(entity['evidence_type'], {'explicit', 'inferred'}, 'entity evidence')
    if not isinstance(entity['value'], str) or not entity['value'].strip():
        raise ImportValidationError('Affected entity value is empty.')
    quote = entity['evidence_quote']
    if not isinstance(quote, str) or not quote or quote not in raw_text:
        raise ImportValidationError('Affected entity evidence is not present in the input snapshot.')


def _parse_event(row, input_text):
    event = {}
    event['snapshot_id'] = _uuid(row['snapshot_id'], 'snapshot_id')
    if not row['event_index'].isdigit():
        raise ImportValidationError('event_index must be a non-negative integer.')
    event['event_index'] = int(row['event_index'])
    if str(event['event_index']) != row['event_index']:
        raise ImportValidationError('event_index must use canonical integer formatting.')

    for field, allowed in (
        ('event_status', EVENT_STATUS_VALUES),
        ('disruption_type', DISRUPTION_TYPES),
        ('disruption_type_evidence', EVIDENCE_VALUES),
        ('severity', SEVERITY_VALUES),
        ('severity_evidence', EVIDENCE_VALUES),
        ('event_start_time_evidence', EVIDENCE_VALUES),
        ('event_end_time_evidence', EVIDENCE_VALUES),
        ('uncertainty_status', UNCERTAINTY_VALUES),
    ):
        _enum(row[field], allowed, field)
        event[field] = row[field]

    event['affected_entities'] = _parse_json_array(row['affected_entities_json'], 'affected_entities_json')
    event['evidence_quotes'] = _parse_json_array(row['evidence_quotes_json'], 'evidence_quotes_json')
    for entity in event['affected_entities']:
        _validate_entity(entity, input_text)
    for quote in event['evidence_quotes']:
        if not isinstance(quote, str) or not quote or quote not in input_text:
            raise ImportValidationError('Evidence quote is not present in the input snapshot.')
    if event['event_status'] == 'potential_disruption' and not event['evidence_quotes']:
        raise ImportValidationError('Potential disruption rows require evidence quotes.')

    status = event['event_status']
    for field, evidence_field in (
        ('event_start_time', 'event_start_time_evidence'),
        ('event_end_time', 'event_end_time_evidence'),
    ):
        raw_value = row[field]
        evidence = event[evidence_field]
        if raw_value == '':
            allowed_unknown = {'not_applicable'} if status == 'no_disruption' else {'unknown'}
            if evidence not in allowed_unknown:
                raise ImportValidationError('Blank {} requires unknown or not_applicable evidence.'.format(field))
            event[field] = None
        else:
            event[field] = _aware_datetime(raw_value, field)
            if evidence not in {'explicit', 'inferred'}:
                raise ImportValidationError('Known {} requires explicit or inferred evidence.'.format(field))

    if status == 'no_disruption':
        if (event['event_index'] != 0 or event['disruption_type'] != 'not_applicable'
                or event['disruption_type_evidence'] != 'not_applicable'
                or event['affected_entities'] or event['severity'] != 'not_applicable'
                or event['severity_evidence'] != 'not_applicable'
                or event['event_start_time'] is not None or event['event_end_time'] is not None
                or event['event_start_time_evidence'] != 'not_applicable'
                or event['event_end_time_evidence'] != 'not_applicable'):
            raise ImportValidationError('no_disruption rows must use the index-0 sentinel contract.')
    elif status == 'unclear':
        if event['event_index'] != 0 or event['disruption_type'] == 'not_applicable' or event['severity'] == 'not_applicable':
            raise ImportValidationError('unclear rows must use index 0 and unknown or stated event details.')
    else:
        if event['event_index'] < 1:
            raise ImportValidationError('potential_disruption rows require a positive event_index.')

    for value_field, evidence_field in (
        ('disruption_type', 'disruption_type_evidence'),
        ('severity', 'severity_evidence'),
    ):
        value, evidence = event[value_field], event[evidence_field]
        if value == 'unknown':
            if evidence != 'unknown':
                raise ImportValidationError('Unknown {} requires unknown evidence.'.format(value_field))
        elif value == 'not_applicable':
            if status != 'no_disruption' or evidence != 'not_applicable':
                raise ImportValidationError('not_applicable {} is reserved for no_disruption rows.'.format(value_field))
        elif evidence not in {'explicit', 'inferred'}:
            raise ImportValidationError('Known {} requires explicit or inferred evidence.'.format(value_field))

    event['uncertainty_note'] = row['uncertainty_note']
    event['explanation'] = row['explanation']
    if event['uncertainty_status'] == 'yes' and not event['uncertainty_note'].strip():
        raise ImportValidationError('Uncertain rows require an uncertainty note.')
    for field in ('schema_version', 'model', 'processor_version', 'prompt_version'):
        if not row[field]:
            raise ImportValidationError('CSV {} is required.'.format(field))
    if row['review_status'] != 'pending' or row['review_notes'] != '':
        raise ImportValidationError('Initial review metadata must be pending with empty notes.')
    return event


def validate_run(run_directory):
    """Validate run artifacts and return normalized database payloads without connecting."""
    run_directory = Path(run_directory)
    manifest_path = run_directory / 'processing_manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ImportValidationError('processing_manifest.json is unavailable or invalid.') from error
    expected_manifest_fields = {
        'manifest_version', 'run_id', 'status', 'started_at', 'completed_at', 'model',
        'schema_version', 'processor_version', 'prompt_version', 'input_csv', 'output_csv',
    }
    manifest_fields = set(manifest) if isinstance(manifest, dict) else set()
    if manifest_fields not in (expected_manifest_fields, expected_manifest_fields | {'max_tokens'}):
        raise ImportValidationError('Manifest fields do not match the processing manifest contract.')
    if 'max_tokens' in manifest:
        max_tokens = manifest['max_tokens']
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ImportValidationError('Manifest max_tokens must be a positive integer.')
    if manifest['manifest_version'] != MANIFEST_VERSION:
        raise ImportValidationError('Unsupported processing manifest version.')
    if manifest['status'] != 'completed':
        raise ImportValidationError('Only completed processing runs can be imported.')
    run_id = _uuid(manifest['run_id'], 'run_id')
    if manifest['schema_version'] != PROMPT_VERSION or manifest['prompt_version'] != PROMPT_VERSION:
        raise ImportValidationError('Unsupported Orvix output schema or prompt version.')
    for field in ('model', 'processor_version'):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise ImportValidationError('Manifest {} is required.'.format(field))
    started_at = _aware_datetime(manifest['started_at'], 'started_at')
    completed_at = _aware_datetime(manifest['completed_at'], 'completed_at')
    if completed_at < started_at:
        raise ImportValidationError('Manifest completion precedes its start time.')

    input_path, input_hash = _safe_artifact(run_directory, manifest['input_csv'], 'input.csv')
    output_path, output_hash = _safe_artifact(run_directory, manifest['output_csv'], 'processed_events.csv')
    input_rows = _read_csv(input_path, INPUT_FIELDS, 'Input')
    raw_text_by_snapshot = {}
    for row in input_rows:
        snapshot_id = _uuid(row['snapshot_id'], 'input snapshot_id')
        if snapshot_id in raw_text_by_snapshot:
            raise ImportValidationError('Input CSV contains duplicate snapshot identities.')
        raw_text_by_snapshot[snapshot_id] = row['raw_text']

    output_rows = _read_csv(output_path, OUTPUT_FIELDS, 'Output')
    if not output_rows and raw_text_by_snapshot:
        raise ImportValidationError('Output CSV contains no event rows for a non-empty input.')
    events = []
    seen_keys = set()
    grouped = {}
    for row in output_rows:
        if row['schema_version'] != manifest['schema_version']:
            raise ImportValidationError('CSV schema_version differs from the manifest.')
        if row['model'] != manifest['model']:
            raise ImportValidationError('CSV model differs from the manifest.')
        if row['processor_version'] != manifest['processor_version']:
            raise ImportValidationError('CSV processor_version differs from the manifest.')
        if row['prompt_version'] != manifest['prompt_version']:
            raise ImportValidationError('CSV prompt_version differs from the manifest.')
        snapshot_id = _uuid(row['snapshot_id'], 'snapshot_id')
        if snapshot_id not in raw_text_by_snapshot:
            raise ImportValidationError('Output snapshot_id is absent from the input CSV.')
        event = _parse_event(row, raw_text_by_snapshot[snapshot_id])
        key = (event['snapshot_id'], event['event_index'])
        if key in seen_keys:
            raise ImportValidationError('Output CSV contains duplicate snapshot/event identities.')
        seen_keys.add(key)
        grouped.setdefault(event['snapshot_id'], []).append(event)
        events.append(event)

    for snapshot_events in grouped.values():
        snapshot_events.sort(key=lambda event: event['event_index'])
        indices = [event['event_index'] for event in snapshot_events]
        statuses = {event['event_status'] for event in snapshot_events}
        if statuses <= {'no_disruption', 'unclear'}:
            valid = len(snapshot_events) == 1 and indices == [0]
        else:
            valid = statuses == {'potential_disruption'} and indices == list(range(1, len(indices) + 1))
        if not valid:
            raise ImportValidationError('Snapshot rows violate the event-index convention.')

    run = {
        'id': run_id,
        'manifest_version': manifest['manifest_version'],
        'manifest_status': manifest['status'],
        'schema_version': manifest['schema_version'],
        'model': manifest['model'],
        'processor_version': manifest['processor_version'],
        'prompt_version': manifest['prompt_version'],
        'started_at': started_at,
        'completed_at': completed_at,
        'input_csv_sha256': input_hash,
        'output_csv_sha256': output_hash,
    }
    return {
        'run': run,
        'events': events,
        'input_snapshot_count': len(input_rows),
        'input_snapshot_ids': list(raw_text_by_snapshot),
    }


def compare_existing_import(existing_run, existing_events, existing_reviews, payload):
    """Return only when stored data exactly matches a retry of this run ID."""
    expected_run = payload['run']
    for column in RUN_COLUMNS:
        if existing_run.get(column) != expected_run[column]:
            raise ImportConflictError('This run_id is already imported with different manifest data.')

    expected = {
        (event['snapshot_id'], event['event_index']): event
        for event in payload['events']
    }
    actual = {
        (event['snapshot_id'], event['event_index']): event
        for event in existing_events
    }
    if len(actual) != len(existing_events) or actual.keys() != expected.keys():
        raise ImportConflictError('This run_id is already imported with different event identities.')
    for key, wanted in expected.items():
        stored = actual[key]
        if any(stored.get(column) != wanted[column] for column in EVENT_COLUMNS):
            raise ImportConflictError('This run_id is already imported with different event data.')
    reviews = {
        (review['snapshot_id'], review['event_index']): review
        for review in existing_reviews
    }
    if reviews.keys() != expected.keys() or any(
            review.get('review_status') != 'pending'
            or review.get('review_notes') != ''
            or review.get('corrections') != {}
            for review in reviews.values()):
        raise ImportConflictError('This run_id has incomplete initial review records.')


def _insert_events(conn, payload):
    from psycopg.types.json import Jsonb

    sql = """INSERT INTO processing.events (
        run_id,snapshot_id,event_index,event_status,disruption_type,
        disruption_type_evidence,affected_entities,severity,severity_evidence,
        event_start_time,event_start_time_evidence,event_end_time,
        event_end_time_evidence,uncertainty_status,uncertainty_note,
        explanation,evidence_quotes
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING id"""
    review_sql = """INSERT INTO processing.event_reviews
        (event_id,revision,review_status,review_notes)
        VALUES (%s,1,'pending','')"""
    for event in payload['events']:
        values = [payload['run']['id']]
        values.extend(event[column] for column in EVENT_COLUMNS)
        values[6] = Jsonb(values[6])
        values[-1] = Jsonb(values[-1])
        event_id = conn.execute(sql, values).fetchone()['id']
        conn.execute(review_sql, (event_id,))


def _check_existing(conn, payload):
    run_id = payload['run']['id']
    existing_run = conn.execute(
        """SELECT manifest_version,manifest_status,schema_version,model,processor_version,
                  prompt_version,started_at,completed_at,input_csv_sha256,output_csv_sha256
           FROM processing.processing_runs WHERE id=%s""", (run_id,)
    ).fetchone()
    if existing_run is None:
        raise ImportConflictError('Run ID conflict could not be resolved safely.')
    existing_events = conn.execute(
        'SELECT {} FROM processing.events WHERE run_id=%s ORDER BY snapshot_id,event_index'.format(
            ','.join(EVENT_COLUMNS)), (run_id,)
    ).fetchall()
    existing_reviews = conn.execute(
        """SELECT e.snapshot_id,e.event_index,r.review_status,r.review_notes,r.corrections
           FROM processing.events e LEFT JOIN processing.event_reviews r
             ON r.event_id=e.id AND r.revision=1
           WHERE e.run_id=%s""", (run_id,)
    ).fetchall()
    if any(review['review_status'] is None for review in existing_reviews):
        raise ImportConflictError('This run_id has incomplete initial review records.')
    compare_existing_import(existing_run, existing_events, existing_reviews, payload)


def _normalize_claimed_jobs(payload, claimed_jobs):
    """Validate that each input snapshot has exactly one corresponding claim."""
    if not isinstance(claimed_jobs, (list, tuple)) or not claimed_jobs:
        raise ImportJobCompletionError('Worker import requires at least one claimed job.')
    normalized = []
    job_ids = set()
    snapshot_ids = set()
    for item in claimed_jobs:
        if not isinstance(item, dict) or not {'job_id', 'snapshot_id', 'claim_token'} <= set(item):
            raise ImportJobCompletionError('Claim metadata is incomplete.')
        try:
            job_id = _uuid(str(item['job_id']), 'job_id')
            snapshot_id = _uuid(str(item['snapshot_id']), 'snapshot_id')
            claim_token = _uuid(str(item['claim_token']), 'claim_token')
        except ImportValidationError as error:
            raise ImportJobCompletionError('Claim metadata contains an invalid UUID.') from error
        if job_id in job_ids or snapshot_id in snapshot_ids:
            raise ImportJobCompletionError('Claim metadata contains duplicate job or snapshot identities.')
        job_ids.add(job_id)
        snapshot_ids.add(snapshot_id)
        normalized.append({
            'job_id': job_id,
            'snapshot_id': snapshot_id,
            'claim_token': claim_token,
        })

    input_ids = set(payload.get('input_snapshot_ids', ()))
    event_ids = {event['snapshot_id'] for event in payload.get('events', ())}
    if not input_ids or snapshot_ids != input_ids or event_ids != input_ids:
        raise ImportJobCompletionError(
            'Claimed snapshots, input snapshots, and event results must match exactly.'
        )
    return normalized


def _complete_claimed_jobs(conn, payload, claimed_jobs):
    """Complete claims on the importer connection before its transaction commits."""
    for claim in claimed_jobs:
        job = conn.execute(
            """SELECT snapshot_id,status,processing_run_id
               FROM processing.jobs WHERE id=%s""",
            (claim['job_id'],),
        ).fetchone()
        if job is None or job['snapshot_id'] != claim['snapshot_id']:
            raise ImportJobCompletionError('Claimed job does not match its imported snapshot.')

        # A retry after a successful commit is already complete. Accept that
        # exact run/job pairing without inserting or reviewing rows again.
        if job['status'] == 'completed' and job['processing_run_id'] == payload['run']['id']:
            continue

        completed = conn.execute(
            'SELECT processing.complete_job(%s,%s,%s) AS completed',
            (claim['job_id'], claim['claim_token'], payload['run']['id']),
        ).fetchone()['completed']
        if not completed:
            raise ImportJobCompletionError(
                'A claimed job could not be completed; the import transaction was rolled back.'
            )


def import_payload(payload, connection_params):
    """Atomically insert a validated run and, optionally, complete its claims."""
    return _import_payload(payload, connection_params, claimed_jobs=None)


def import_claimed_payload(payload, connection_params, claimed_jobs):
    """Atomically store validated results and complete every matching claim."""
    normalized_claims = _normalize_claimed_jobs(payload, claimed_jobs)
    return _import_payload(payload, connection_params, claimed_jobs=normalized_claims)


def _import_payload(payload, connection_params, claimed_jobs):
    """Shared transaction implementation for manual and worker imports."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise ImportDatabaseError('Install the repository PostgreSQL dependency before importing.') from error

    try:
        with psycopg.connect(**connection_params, prepare_threshold=None, row_factory=dict_row) as conn:
            conn.execute("SET LOCAL statement_timeout = '20s'")
            conn.execute("SET LOCAL lock_timeout = '15s'")
            if claimed_jobs is not None:
                from scripts.orvix_job_queue import WORKER_LOGIN
                role = conn.execute('SELECT current_user AS role_name').fetchone()['role_name']
                if role != WORKER_LOGIN:
                    raise ImportDatabaseError('Worker import requires the dedicated worker database login.')
            run = payload['run']
            inserted = conn.execute(
                """INSERT INTO processing.processing_runs (
                    id,manifest_version,manifest_status,schema_version,model,processor_version,
                    prompt_version,started_at,completed_at,input_csv_sha256,output_csv_sha256
                ) VALUES (%(id)s,%(manifest_version)s,%(manifest_status)s,%(schema_version)s,
                    %(model)s,%(processor_version)s,%(prompt_version)s,%(started_at)s,
                    %(completed_at)s,%(input_csv_sha256)s,%(output_csv_sha256)s)
                ON CONFLICT (id) DO NOTHING RETURNING id""", run
            ).fetchone()
            if inserted is None:
                _check_existing(conn, payload)
                if claimed_jobs is not None:
                    _complete_claimed_jobs(conn, payload, claimed_jobs)
                return 'already_imported'
            _insert_events(conn, payload)
            if claimed_jobs is not None:
                _complete_claimed_jobs(conn, payload, claimed_jobs)
        return 'imported'
    except (ImportConflictError, ImportDatabaseError, ImportJobCompletionError):
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise ImportDatabaseError('Database import failed: {}{}'.format(type(error).__name__, suffix)) from error


def import_run(run_directory):
    payload = validate_run(run_directory)
    try:
        params = connection_parameters()
    except (ExportError, ValueError, OSError, TypeError, json.JSONDecodeError) as error:
        raise ImportDatabaseError('Secure database credentials are unavailable or invalid.') from error
    return payload, import_payload(payload, params)


def import_claimed_run(run_directory, claimed_jobs, db_params=None):
    """Worker import using the separate worker login and one atomic transaction."""
    payload = validate_run(run_directory)
    try:
        if db_params is None:
            from scripts.orvix_job_queue import load_worker_connection
            db_params = load_worker_connection()
    except Exception as error:
        # Do not surface credential paths or values from configuration errors.
        raise ImportDatabaseError('Secure worker database credentials are unavailable or invalid.') from error
    return payload, import_claimed_payload(payload, db_params, claimed_jobs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directory', type=Path, help='Completed data/orvix/runs/<run_id> directory.')
    parser.add_argument('--validate-only', action='store_true', help='Validate manifest and CSV artifacts without connecting.')
    args = parser.parse_args(argv)
    try:
        payload = validate_run(args.run_directory)
        if args.validate_only:
            result = 'validated'
        else:
            try:
                params = connection_parameters()
            except (ExportError, ValueError, OSError, TypeError, json.JSONDecodeError) as error:
                raise ImportDatabaseError('Secure database credentials are unavailable or invalid.') from error
            result = import_payload(payload, params)
    except ImportValidationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ImportConflictError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ImportDatabaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    print('{} run {}: {} event row(s), {} input snapshot(s).'.format(
        result, payload['run']['id'], len(payload['events']), payload['input_snapshot_count']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
