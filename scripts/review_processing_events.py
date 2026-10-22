"""Read and append human review revisions for Orvix processing events.

Uses a dedicated least-privilege database login. Review commands default to a
rollback dry-run; persistent decisions require both --apply and --confirm.
"""

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
CONNECTION_FILE = ROOT / 'data/automation/incident/processing_reviewer_connection.json'
REVIEWER_LOGIN = 'moobby_processing_reviewer_login'
REVIEWER_LOGIN_PREFIX = REVIEWER_LOGIN + '.'
ADVISORY_LOCK_SEED = 8171901

DISRUPTION_TYPES = {
    'delay', 'service_suspension', 'route_change', 'signal_or_power_issue',
    'infrastructure_issue', 'accident_or_safety_issue', 'overcrowding',
    'flooding_or_weather', 'other', 'unknown',
}
EVIDENCE_STATUSES = {'explicit', 'inferred', 'unknown', 'not_applicable'}
SEVERITIES = {'low', 'moderate', 'high', 'critical', 'unknown'}
UNCERTAINTY_STATUSES = {'yes', 'no', 'unknown'}
CORRECTION_FIELDS = {
    'disruption_type', 'disruption_type_evidence', 'affected_entities',
    'severity', 'severity_evidence', 'event_start_time',
    'event_start_time_evidence', 'event_end_time', 'event_end_time_evidence',
    'uncertainty_status', 'uncertainty_note', 'explanation', 'evidence_quotes',
}


class ProcessingReviewError(Exception):
    """Safe-to-display input, connection, or review error."""


def _event_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise ProcessingReviewError('Event ID must be a UUID.') from error


def load_reviewer_connection(path=None):
    """Load a private reviewer login config without displaying its contents."""
    config_path = Path(
        path or os.environ.get('PROCESSING_REVIEWER_DB_CONFIG') or CONNECTION_FILE
    )
    if config_path.is_symlink() or not config_path.is_file():
        raise ProcessingReviewError('Processing reviewer credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
        raise ProcessingReviewError('Processing reviewer credential file permissions must be 0600.')
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProcessingReviewError('Processing reviewer credential file is unreadable or malformed.') from error
    required = {'host', 'dbname', 'user', 'password'}
    allowed = required | {'port'}
    if (not isinstance(config, dict) or set(config) - allowed or not required <= set(config)
            or not all(isinstance(config[key], str) and config[key] for key in required)):
        raise ProcessingReviewError('Processing reviewer credential file has an invalid format.')
    username = config['user']
    if username != REVIEWER_LOGIN and not username.startswith(REVIEWER_LOGIN_PREFIX):
        raise ProcessingReviewError('Use the dedicated processing reviewer database login.')
    if not config['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise ProcessingReviewError('Processing reviewer database host must be a Supabase PostgreSQL host.')
    if 'port' in config and not str(config['port']).isdigit():
        raise ProcessingReviewError('Processing reviewer database port is invalid.')
    return dict(config, sslmode='require', connect_timeout=15)


def _connect(connect=None):
    if connect is not None:
        return connect
    try:
        import psycopg
    except ImportError as error:
        raise ProcessingReviewError('Install the repository PostgreSQL dependency first.') from error
    return psycopg.connect


def _safe_db_error(error, action):
    sqlstate = getattr(error, 'sqlstate', None)
    suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
    return ProcessingReviewError('{} failed: {}{}'.format(action, type(error).__name__, suffix))


def list_candidate_events(connection):
    rows = connection.execute(
        """SELECT event.id, event.event_status, event.disruption_type, event.severity,
                  snapshot.published_at, latest.revision, latest.review_status
           FROM processing.events AS event
           JOIN collection.raw_snapshots AS snapshot ON snapshot.id = event.snapshot_id
           LEFT JOIN LATERAL (
             SELECT review.revision, review.review_status
             FROM processing.event_reviews AS review
             WHERE review.event_id = event.id
             ORDER BY review.revision DESC LIMIT 1
           ) AS latest ON true
           WHERE event.event_status = 'potential_disruption' AND event.event_index > 0
           ORDER BY snapshot.published_at DESC NULLS LAST, event.id"""
    ).fetchall()
    return [dict(zip(
        ('event_id', 'event_status', 'disruption_type', 'severity', 'published_at',
         'latest_review_revision', 'latest_review_status'), row
    )) for row in rows]


def inspect_event(connection, event_id):
    event_id = _event_uuid(event_id)
    row = connection.execute(
        """SELECT event.id, event.snapshot_id, event.event_index, event.event_status,
                  event.disruption_type, event.disruption_type_evidence,
                  event.affected_entities, event.severity, event.severity_evidence,
                  event.event_start_time, event.event_start_time_evidence,
                  event.event_end_time, event.event_end_time_evidence,
                  event.uncertainty_status, event.uncertainty_note,
                  event.explanation, event.evidence_quotes,
                  snapshot.author_handle, snapshot.author_display_name, snapshot.title,
                  snapshot.published_at, content.canonical_url, snapshot.raw_text
           FROM processing.events AS event
           JOIN collection.raw_snapshots AS snapshot ON snapshot.id = event.snapshot_id
           JOIN collection.raw_contents AS content ON content.id = snapshot.raw_content_id
           WHERE event.id = %s::uuid
             AND event.event_status = 'potential_disruption'
             AND event.event_index > 0""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ProcessingReviewError('No reviewable potential-disruption event found with that ID.')
    event = dict(zip(
        ('event_id', 'snapshot_id', 'event_index', 'event_status', 'disruption_type',
         'disruption_type_evidence', 'affected_entities', 'severity', 'severity_evidence',
         'event_start_time', 'event_start_time_evidence', 'event_end_time',
         'event_end_time_evidence', 'uncertainty_status', 'uncertainty_note',
         'explanation', 'evidence_quotes', 'author_handle', 'author_display_name',
         'source_title', 'published_at', 'source_post_url', 'raw_text'),
        row,
    ))
    event['reviews'] = [dict(zip(
        ('revision', 'review_status', 'reviewer_id', 'review_notes', 'corrections', 'recorded_at'),
        review,
    )) for review in connection.execute(
        """SELECT revision, review_status, reviewer_id, review_notes, corrections, recorded_at
           FROM processing.event_reviews WHERE event_id = %s::uuid ORDER BY revision""",
        (event_id,),
    ).fetchall()]
    return event


def validate_corrections(decision, corrections):
    if not isinstance(corrections, dict):
        raise ProcessingReviewError('Corrections must be a JSON object.')
    unknown = set(corrections) - CORRECTION_FIELDS
    if unknown:
        raise ProcessingReviewError(
            'Unsupported correction field(s): {}.'.format(', '.join(sorted(unknown)))
        )
    if decision == 'corrected' and not corrections:
        raise ProcessingReviewError('A corrected decision requires at least one correction.')
    if decision != 'corrected' and corrections:
        raise ProcessingReviewError('Only corrected decisions may include correction values.')
    for field, allowed in (
        ('disruption_type', DISRUPTION_TYPES),
        ('disruption_type_evidence', EVIDENCE_STATUSES),
        ('severity', SEVERITIES),
        ('severity_evidence', EVIDENCE_STATUSES),
        ('uncertainty_status', UNCERTAINTY_STATUSES),
        ('event_start_time_evidence', EVIDENCE_STATUSES),
        ('event_end_time_evidence', EVIDENCE_STATUSES),
    ):
        if field in corrections and corrections[field] not in allowed:
            raise ProcessingReviewError('Invalid correction value for {}.'.format(field))
    for field in ('affected_entities', 'evidence_quotes'):
        if field in corrections and not isinstance(corrections[field], list):
            raise ProcessingReviewError('{} correction must be a JSON array.'.format(field))
    for field in ('uncertainty_note', 'explanation'):
        if field in corrections and not isinstance(corrections[field], str):
            raise ProcessingReviewError('{} correction must be text.'.format(field))
    for field in ('event_start_time', 'event_end_time'):
        if field in corrections and corrections[field] is not None and not isinstance(corrections[field], str):
            raise ProcessingReviewError('{} correction must be an ISO timestamp or null.'.format(field))
    # event_status is deliberately not correctable here. A reviewer who finds
    # that an event is not a disruption must reject it instead of leaving a
    # corrected revision that serving might mistake for a potential event.
    return corrections


def append_event_review(connection, event_id, decision, rationale, corrections):
    event_id = _event_uuid(event_id)
    if decision not in {'accepted', 'corrected', 'rejected'}:
        raise ProcessingReviewError('Decision must be accepted, corrected, or rejected.')
    if not isinstance(rationale, str) or not rationale.strip():
        raise ProcessingReviewError('Review rationale is required.')
    corrections = validate_corrections(decision, corrections)

    # The matching database trigger also takes this per-event advisory lock and
    # checks max(revision)+1, protecting against clients that race the CLI.
    connection.execute(
        'SELECT pg_advisory_xact_lock(hashtextextended(%s::text, %s))',
        (event_id, ADVISORY_LOCK_SEED),
    )
    event = connection.execute(
        """SELECT id FROM processing.events
           WHERE id = %s::uuid AND event_status = 'potential_disruption' AND event_index > 0""",
        (event_id,),
    ).fetchone()
    if event is None:
        raise ProcessingReviewError('No reviewable potential-disruption event found with that ID.')
    next_revision = connection.execute(
        'SELECT COALESCE(max(revision), 0) + 1 FROM processing.event_reviews WHERE event_id = %s::uuid',
        (event_id,),
    ).fetchone()[0]
    reviewer_id = connection.execute('SELECT session_user').fetchone()[0]
    row = connection.execute(
        """INSERT INTO processing.event_reviews
             (event_id, revision, review_status, reviewer_id, review_notes, corrections)
           VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb)
           RETURNING revision, recorded_at""",
        (event_id, next_revision, decision, reviewer_id, rationale.strip(),
         json.dumps(corrections, ensure_ascii=False, sort_keys=True)),
    ).fetchone()
    return {
        'event_id': event_id,
        'revision': row[0],
        'review_status': decision,
        'reviewer_id': reviewer_id,
        'review_notes': rationale.strip(),
        'corrections': corrections,
        'recorded_at': row[1],
    }


def run_review(db_params, event_id, decision, rationale, corrections, *,
               apply=False, confirmed=False, connect=None):
    if apply and not confirmed:
        raise ProcessingReviewError('Persistent review requires both --apply and --confirm.')
    factory = _connect(connect)
    try:
        with factory(**db_params, prepare_threshold=None) as connection:
            try:
                connection.execute("SET LOCAL statement_timeout = '15s'")
                result = append_event_review(connection, event_id, decision, rationale, corrections)
                connection.execute('SET CONSTRAINTS ALL IMMEDIATE')
                if apply:
                    connection.commit()
                else:
                    connection.rollback()
                return result
            except Exception:
                connection.rollback()
                raise
    except ProcessingReviewError:
        raise
    except Exception as error:
        raise _safe_db_error(error, 'Processing-event review transaction') from error


def _read_only(db_params, operation, *, connect=None):
    factory = _connect(connect)
    try:
        with factory(**db_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            connection.execute("SET LOCAL statement_timeout = '15s'")
            return operation(connection)
    except ProcessingReviewError:
        raise
    except Exception as error:
        raise _safe_db_error(error, 'Processing-event review query') from error


def _parse_corrections(value):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise argparse.ArgumentTypeError('corrections must be valid JSON') from error


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('list', help='List reviewable potential-disruption events.')
    inspect = subparsers.add_parser('inspect', help='Inspect one event, source text, and review history.')
    inspect.add_argument('event_id')
    review = subparsers.add_parser('review', help='Append an accepted, corrected, or rejected human revision.')
    review.add_argument('event_id')
    review.add_argument('--decision', choices=('accepted', 'corrected', 'rejected'), required=True)
    review.add_argument('--rationale', required=True, help='Required human rationale stored in review_notes.')
    review.add_argument('--corrections-json', type=_parse_corrections, default={},
                        help='JSON object of field corrections; required and non-empty for corrected.')
    review.add_argument('--apply', action='store_true',
                        help='Commit this decision; default is a rollback dry-run. Requires --confirm.')
    review.add_argument('--confirm', action='store_true',
                        help='Confirm the persistent review; required together with --apply.')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        params = load_reviewer_connection()
        if args.command == 'list':
            result = _read_only(params, list_candidate_events)
            print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        elif args.command == 'inspect':
            event_id = _event_uuid(args.event_id)
            result = _read_only(params, lambda connection: inspect_event(connection, event_id))
            print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        else:
            result = run_review(
                params, args.event_id, args.decision, args.rationale, args.corrections_json,
                apply=args.apply, confirmed=args.confirm,
            )
            prefix = 'Applied' if args.apply else 'DRY RUN; transaction rolled back'
            print('{}: {}'.format(prefix, json.dumps(result, default=str, ensure_ascii=False)))
    except ProcessingReviewError as error:
        print('Processing-event review failed: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
