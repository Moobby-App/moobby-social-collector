"""Explicit, transactional write workflow for manually curated incidents.

Candidate-event listing and inspection remain in curate_incidents.py and keep
using the dedicated read-only login. This module requires a separate curator
login configuration for serving-schema changes.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import uuid

from scripts.curate_incidents import (
    CONNECTION_FILE as READONLY_CONNECTION_FILE,
    CURATION_LOGIN,
    CurationError,
    load_curation_connection,
)
from scripts.krl_station_resolver import (
    load_reference_stations,
)


ROOT = Path(__file__).resolve().parents[1]
CURATOR_CONNECTION_FILE = ROOT / 'data/automation/incident/curator_connection.json'
CURATOR_LOGIN = 'moobby_incident_curator_login'
CURATOR_LOGIN_PREFIX = 'moobby_incident_curator_login_'
CATEGORIES = {
    'delay', 'service_suspension', 'route_change', 'signal_or_power_issue',
    'infrastructure_issue', 'accident_or_safety_issue', 'overcrowding',
    'flooding_or_weather', 'other', 'unknown',
}
REVIEW_STATUSES = {'pending', 'accepted', 'rejected'}
VERIFICATION_STATUSES = {'corroborated', 'disputed', 'inconclusive'}


class CurationWriteError(CurationError):
    """Safe-to-display validation or write error for manual curation."""


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CurationWriteError('{} is required.'.format(label))
    return value.strip()


def _assert_database_identity(connection, identity, label):
    """Bind audit identity to the authenticated per-curator PostgreSQL login."""
    identity = _required_text(identity, label)
    row = connection.execute('SELECT current_user').fetchone()
    if row is None or not row[0] or identity != row[0]:
        raise CurationWriteError(
            '{} must exactly match the authenticated PostgreSQL login.'.format(label)
        )
    return identity


def _uuid(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise CurationWriteError('{} must be a UUID.'.format(label)) from error


def load_curator_connection(path=None):
    """Load a private write-login config distinct from the read-only config."""
    config_path = Path(path or os.environ.get('INCIDENT_CURATOR_DB_CONFIG') or CURATOR_CONNECTION_FILE)
    if config_path == Path(READONLY_CONNECTION_FILE):
        raise CurationWriteError('Write workflow requires the separate curator credential file.')
    if config_path.is_symlink() or not config_path.is_file():
        raise CurationWriteError('Dedicated curator database credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise CurationWriteError('Curator credential file permissions must be 0600.')
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CurationWriteError('Curator credential file is unreadable or malformed.') from error
    required = {'host', 'dbname', 'user', 'password'}
    allowed = required | {'port'}
    if (not isinstance(config, dict) or set(config) - allowed or not required <= set(config)
            or not all(isinstance(config[key], str) and config[key] for key in required)):
        raise CurationWriteError('Curator credential file has an invalid format.')
    username = config['user']
    if not (username == CURATOR_LOGIN or username.startswith(CURATOR_LOGIN + '.')
            or username.startswith(CURATOR_LOGIN_PREFIX)):
        raise CurationWriteError('Write workflow must use the dedicated curator login.')
    if not config['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise CurationWriteError('Curator database host must be a Supabase PostgreSQL host.')
    if 'port' in config and not str(config['port']).isdigit():
        raise CurationWriteError('Curator database port is invalid.')
    return dict(config, sslmode='require', connect_timeout=15)


def _connect(connect=None):
    if connect is not None:
        return connect
    try:
        import psycopg
    except ImportError as error:
        raise CurationWriteError('Install the repository PostgreSQL dependency first.') from error
    return psycopg.connect


def run_transaction(db_params, operation, *, apply=False, confirmed=False, connect=None):
    """Run a single short transaction; default dry-run validates then rolls back."""
    if apply and not confirmed:
        raise CurationWriteError('Applying this action requires the explicit --confirm flag.')
    connection_factory = _connect(connect)
    try:
        with connection_factory(**db_params, prepare_threshold=None) as connection:
            try:
                connection.execute("SET LOCAL statement_timeout = '15s'")
                result = operation(connection)
                # Migration 009 defers cross-table publication checks to commit.
                # Force them now so a dry run proves the same constraints.
                connection.execute('SET CONSTRAINTS ALL IMMEDIATE')
                if apply:
                    connection.commit()
                else:
                    connection.rollback()
                return result
            except Exception:
                connection.rollback()
                raise
    except CurationWriteError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise CurationWriteError(
            'Curation transaction failed: {}{}'.format(type(error).__name__, suffix)
        ) from error


def _incident_lock(connection, incident_id):
    row = connection.execute(
        'SELECT id, title, category, publication_status FROM serving.incidents WHERE id = %s::uuid FOR UPDATE',
        (incident_id,),
    ).fetchone()
    if row is None:
        raise CurationWriteError('Incident not found.')
    return row


def create_draft(connection, *, title=None, category=None):
    if category is not None and category not in CATEGORIES:
        raise CurationWriteError('Category is not in the serving schema vocabulary.')
    row = connection.execute(
        "INSERT INTO serving.incidents (title, category, publication_status) "
        "VALUES (%s, %s, 'draft') RETURNING id",
        (title.strip() if title else None, category),
    ).fetchone()
    return {'incident_id': str(row[0]), 'publication_status': 'draft'}


def inspect_incident(connection, incident_id):
    incident_id = _uuid(incident_id, 'Incident ID')
    incident = connection.execute(
        'SELECT id, title, category, location_name, location_station_id, latitude, longitude, '
        'event_start_at, event_end_at, activity_status, publication_status, withdrawn_at '
        'FROM serving.incidents WHERE id = %s::uuid', (incident_id,),
    ).fetchone()
    if incident is None:
        raise CurationWriteError('Incident not found.')
    evidence = connection.execute(
        'SELECT id, processing_event_id, event_review_revision, relationship, link_status, '
        'is_primary, correlation_rationale, reviewed_by, reviewed_at '
        'FROM serving.incident_evidence WHERE incident_id = %s::uuid ORDER BY created_at, id',
        (incident_id,),
    ).fetchall()
    reviews = connection.execute(
        'SELECT revision, review_status, reviewer_id, reviewed_title, reviewed_category, '
        'review_notes, changes, recorded_at FROM serving.incident_reviews '
        'WHERE incident_id = %s::uuid ORDER BY revision', (incident_id,),
    ).fetchall()
    verifications = connection.execute(
        'SELECT revision, verification_status, verifier_id, source_name, source_url, '
        'evidence_summary, checked_at, recorded_at FROM serving.incident_verifications '
        'WHERE incident_id = %s::uuid ORDER BY revision', (incident_id,),
    ).fetchall()
    return {'incident': incident, 'evidence': evidence, 'reviews': reviews,
            'verifications': verifications}


def attach_evidence(connection, *, incident_id, event_id, event_review_revision,
                    event_status, event_index, event_review_status, rationale,
                    relationship='supports'):
    incident_id = _uuid(incident_id, 'Incident ID')
    event_id = _uuid(event_id, 'Event ID')
    rationale = _required_text(rationale, 'Correlation rationale')
    if relationship not in {'supports', 'contradicts'}:
        raise CurationWriteError('Evidence relationship must be supports or contradicts.')
    if event_status != 'potential_disruption' or not isinstance(event_index, int) or event_index <= 0:
        raise CurationWriteError('Only positive-index potential-disruption events can be attached.')
    if not isinstance(event_review_revision, int) or event_review_revision <= 0:
        raise CurationWriteError('A positive, existing event review revision is required.')
    if event_review_status not in {'pending', 'accepted', 'corrected'}:
        raise CurationWriteError('A rejected processing-event revision cannot be proposed as evidence.')
    _incident_lock(connection, incident_id)
    existing = connection.execute(
        'SELECT id FROM serving.incident_evidence '
        'WHERE incident_id = %s::uuid AND processing_event_id = %s::uuid '
        "AND (link_status IN ('proposed','accepted') OR event_review_revision = %s) LIMIT 1",
        (incident_id, event_id, event_review_revision),
    ).fetchone()
    if existing:
        raise CurationWriteError(
            'An active event association exists or this review revision was already linked.'
        )
    row = connection.execute(
        "INSERT INTO serving.incident_evidence "
        "(incident_id, processing_event_id, event_review_revision, relationship, link_status, "
        "correlation_rationale) VALUES (%s::uuid, %s::uuid, %s, %s, 'proposed', %s) "
        'RETURNING id',
        (incident_id, event_id, event_review_revision, relationship, rationale),
    ).fetchone()
    return {'evidence_id': str(row[0]), 'link_status': 'proposed'}


def review_evidence(connection, *, evidence_id, decision, reviewer_id, rationale,
                    event_review_status, primary=False):
    evidence_id = _uuid(evidence_id, 'Evidence ID')
    reviewer_id = _required_text(reviewer_id, 'Reviewer identity')
    rationale = _required_text(rationale, 'Review rationale')
    if decision not in {'accepted', 'rejected'}:
        raise CurationWriteError('Evidence decision must be accepted or rejected.')
    row = connection.execute(
        'SELECT id, incident_id, processing_event_id, event_review_revision, relationship, '
        'link_status, is_primary, correlation_rationale FROM serving.incident_evidence '
        'WHERE id = %s::uuid', (evidence_id,),
    ).fetchone()
    if row is None:
        raise CurationWriteError('Evidence association not found.')
    incident = _incident_lock(connection, row[1])
    row = connection.execute(
        'SELECT id, incident_id, processing_event_id, event_review_revision, relationship, '
        'link_status, is_primary, correlation_rationale FROM serving.incident_evidence '
        'WHERE id = %s::uuid FOR UPDATE', (evidence_id,),
    ).fetchone()
    if row is None:
        raise CurationWriteError('Evidence association not found.')
    if decision == 'accepted' and event_review_status not in {'accepted', 'corrected'}:
        raise CurationWriteError('Evidence cannot be accepted without an accepted or corrected event review revision.')
    if primary and (decision != 'accepted' or row[4] != 'supports'):
        raise CurationWriteError('Primary evidence must be accepted supporting evidence.')
    target_primary = bool(primary and decision == 'accepted')
    if row[5] == decision and bool(row[6]) == target_primary:
        raise CurationWriteError('Evidence already has this decision; no review change was made.')
    current = row[7] or ''
    audit_rationale = current + '\nReview by {}: {}'.format(reviewer_id, rationale)
    connection.execute(
        'UPDATE serving.incident_evidence SET link_status = %s, is_primary = %s, '
        'correlation_rationale = %s, reviewed_by = %s, reviewed_at = clock_timestamp() '
        'WHERE id = %s::uuid',
        (decision, bool(primary and decision == 'accepted'), audit_rationale, reviewer_id, evidence_id),
    )
    history_status = 'accepted' if incident[3] == 'approved' else 'pending'
    _append_review(
        connection, incident, reviewer_id=reviewer_id, status=history_status,
        rationale=rationale, title=incident[1], category=incident[2],
        changes={'evidence_decision': {
            'evidence_id': evidence_id, 'decision': decision,
            'previous_decision': row[5],
            'event_id': str(row[2]), 'event_review_revision': row[3],
            'primary': target_primary,
        }},
    )
    return {'evidence_id': evidence_id, 'link_status': decision, 'is_primary': bool(primary)}


def _append_review(connection, incident, *, reviewer_id, status, rationale,
                   title=None, category=None, changes=None):
    incident_id, current_title, current_category, _publication_status = incident
    reviewer_id = _assert_database_identity(connection, reviewer_id, 'Reviewer identity')
    rationale = _required_text(rationale, 'Review rationale')
    if status not in REVIEW_STATUSES:
        raise CurationWriteError('Incident review status is invalid.')
    if status == 'accepted':
        title = _required_text(title, 'Reviewed title')
        if category not in CATEGORIES:
            raise CurationWriteError('An accepted review requires a valid category.')
        connection.execute(
            'UPDATE serving.incidents SET title = %s, category = %s, updated_at = clock_timestamp() '
            'WHERE id = %s', (title, category, incident_id),
        )
    else:
        title = title if title is not None else current_title
        category = category if category is not None else current_category
    next_revision = connection.execute(
        'SELECT COALESCE(max(revision), 0) + 1 FROM serving.incident_reviews '
        'WHERE incident_id = %s', (incident_id,),
    ).fetchone()[0]
    note = rationale
    connection.execute(
        'INSERT INTO serving.incident_reviews '
        '(incident_id, revision, review_status, reviewer_id, reviewed_title, reviewed_category, '
        'review_notes, changes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)',
        (incident_id, next_revision, status, reviewer_id, title, category, note,
         json.dumps(changes or {}, ensure_ascii=False)),
    )
    return next_revision


def record_incident_review(connection, *, incident_id, reviewer_id, status,
                           rationale, title=None, category=None):
    incident = _incident_lock(connection, _uuid(incident_id, 'Incident ID'))
    revision = _append_review(
        connection, incident, reviewer_id=reviewer_id, status=status,
        rationale=rationale, title=title, category=category,
    )
    return {'incident_id': str(incident[0]), 'review_revision': revision, 'review_status': status}


def _next_revision(connection, table, incident_id):
    # Table is selected from this fixed internal allowlist, never from user input.
    if table not in {'incident_reviews', 'incident_verifications'}:
        raise AssertionError('unsupported revision table')
    return connection.execute(
        'SELECT COALESCE(max(revision), 0) + 1 FROM serving.{} WHERE incident_id = %s'.format(table),
        (incident_id,),
    ).fetchone()[0]


def approve_incident(connection, *, incident_id, reviewer_id, rationale):
    incident = _incident_lock(connection, _uuid(incident_id, 'Incident ID'))
    title, category = incident[1], incident[2]
    if not title or not title.strip() or category not in CATEGORIES:
        raise CurationWriteError('Incident needs a title and valid category before approval.')
    revision = _append_review(
        connection, incident, reviewer_id=reviewer_id, status='accepted',
        rationale=rationale, title=title, category=category,
        changes={'publication_status': {'from': incident[3], 'to': 'approved'}},
    )
    connection.execute(
        "UPDATE serving.incidents SET publication_status = 'approved', withdrawn_at = NULL, "
        'updated_at = clock_timestamp() WHERE id = %s', (incident[0],),
    )
    return {'incident_id': str(incident[0]), 'publication_status': 'approved',
            'review_revision': revision}


def withdraw_incident(connection, *, incident_id, reviewer_id, rationale):
    incident = _incident_lock(connection, _uuid(incident_id, 'Incident ID'))
    if incident[3] != 'approved':
        raise CurationWriteError('Only an approved incident can be withdrawn.')
    title = _required_text(incident[1], 'Incident title')
    if incident[2] not in CATEGORIES:
        raise CurationWriteError('Incident category is invalid.')
    revision = _append_review(
        connection, incident, reviewer_id=reviewer_id, status='accepted',
        rationale=rationale, title=title, category=incident[2],
        changes={'publication_status': {'from': 'approved', 'to': 'withdrawn'}},
    )
    connection.execute(
        "UPDATE serving.incidents SET publication_status = 'withdrawn', "
        'withdrawn_at = clock_timestamp(), updated_at = clock_timestamp() WHERE id = %s',
        (incident[0],),
    )
    return {'incident_id': str(incident[0]), 'publication_status': 'withdrawn',
            'review_revision': revision}


def confirm_location(connection, *, incident_id, station, reviewer_id, rationale):
    incident = _incident_lock(connection, _uuid(incident_id, 'Incident ID'))
    reviewer_id = _required_text(reviewer_id, 'Reviewer identity')
    rationale = _required_text(rationale, 'Location rationale')
    if station.get('resolution_status') != 'resolved' or station.get('location_semantics') != 'mentioned_station_only':
        raise CurationWriteError('Location requires an explicitly selected, unambiguously resolved station.')
    title, category = incident[1], incident[2]
    # Keep an approved incident publishable by appending a matching accepted
    # review; for a draft, record the location decision as pending review.
    review_status = 'accepted' if incident[3] == 'approved' else 'pending'
    changes = {
        'location_confirmation': {
            'station_id': station['station_id'],
            'canonical_name': station['canonical_name'],
            'source_station_code': station['source_station_code'],
            'latitude': station['latitude'], 'longitude': station['longitude'],
            'explicitly_confirmed': True,
        }
    }
    revision = _append_review(
        connection, incident, reviewer_id=reviewer_id, status=review_status,
        rationale=rationale, title=title, category=category, changes=changes,
    )
    connection.execute(
        'UPDATE serving.incidents SET location_name = %s, location_station_id = %s, '
        'latitude = %s, longitude = %s, updated_at = clock_timestamp() WHERE id = %s',
        (station['canonical_name'], station['station_id'], station['latitude'],
         station['longitude'], incident[0]),
    )
    return {'incident_id': str(incident[0]), 'station_id': station['station_id'],
            'review_revision': revision, 'review_status': review_status}


def record_verification(connection, *, incident_id, verifier_id, status, source_name,
                        source_url, evidence_summary, checked_at=None):
    incident_id = _uuid(incident_id, 'Incident ID')
    verifier_id = _required_text(verifier_id, 'Verifier identity')
    source_name = _required_text(source_name, 'Verification source name')
    evidence_summary = _required_text(evidence_summary, 'Verification rationale/evidence summary')
    if status not in VERIFICATION_STATUSES:
        raise CurationWriteError('Verification status is invalid.')
    if source_url and not source_url.startswith('https://'):
        raise CurationWriteError('Verification source URL must use HTTPS.')
    checked_at = checked_at or datetime.now(timezone.utc)
    if isinstance(checked_at, str):
        try:
            checked_at = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
        except ValueError as error:
            raise CurationWriteError('checked-at must be an ISO timestamp.') from error
    if checked_at.tzinfo is None:
        raise CurationWriteError('checked-at must include a timezone.')
    _incident_lock(connection, incident_id)
    verifier_id = _assert_database_identity(connection, verifier_id, 'Verifier identity')
    revision = _next_revision(connection, 'incident_verifications', incident_id)
    connection.execute(
        'INSERT INTO serving.incident_verifications '
        '(incident_id, revision, verification_status, verifier_id, source_name, source_url, '
        'evidence_summary, checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
        (incident_id, revision, status, verifier_id, source_name, source_url or None,
         evidence_summary, checked_at),
    )
    return {'incident_id': incident_id, 'verification_revision': revision,
            'verification_status': status}


def read_event_review(readonly_params, event_id, revision, *, connect=None):
    """Read immutable event/review facts using the separate read-only login."""
    event_id = _uuid(event_id, 'Event ID')
    if not isinstance(revision, int) or revision <= 0:
        raise CurationWriteError('A positive event review revision is required.')
    connection_factory = _connect(connect)
    query = (
        'SELECT e.id, e.event_status, e.event_index, r.review_status '
        'FROM processing.events AS e JOIN processing.event_reviews AS r ON r.event_id = e.id '
        'WHERE e.id = %s::uuid AND r.revision = %s'
    )
    try:
        with connection_factory(**readonly_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            row = connection.execute(query, (event_id, revision)).fetchone()
        if row is None:
            raise CurationWriteError('The specified processing-event review revision does not exist.')
        if row[1] != 'potential_disruption' or row[2] <= 0:
            raise CurationWriteError('Only positive-index potential-disruption events may be associated.')
        return {'event_status': row[1], 'event_index': row[2], 'review_status': row[3]}
    except CurationWriteError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise CurationWriteError(
            'Read-only event validation failed: {}{}'.format(type(error).__name__, suffix)
        ) from error


def read_event_entities(readonly_params, event_id, *, connect=None):
    event_id = _uuid(event_id, 'Event ID')
    connection_factory = _connect(connect)
    try:
        with connection_factory(**readonly_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            row = connection.execute(
                'SELECT e.affected_entities FROM processing.events AS e '
                "WHERE e.id = %s::uuid AND e.event_status = 'potential_disruption' AND e.event_index > 0",
                (event_id,),
            ).fetchone()
        if row is None:
            raise CurationWriteError('Potential-disruption event not found.')
        return row[0] or []
    except CurationWriteError:
        raise
    except Exception as error:
        raise CurationWriteError(
            'Read-only event lookup failed: {}'.format(type(error).__name__)
        ) from error


def read_event_association(readonly_params, incident_id, event_id, *, connect=None):
    incident_id = _uuid(incident_id, 'Incident ID')
    event_id = _uuid(event_id, 'Event ID')
    factory = _connect(connect)
    try:
        with factory(**readonly_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            row = connection.execute(
                'SELECT id FROM serving.incident_evidence '
                'WHERE incident_id = %s::uuid AND processing_event_id = %s::uuid LIMIT 1',
                (incident_id, event_id),
            ).fetchone()
        return row is not None
    except Exception as error:
        raise CurationWriteError(
            'Read-only association check failed: {}'.format(type(error).__name__)
        ) from error


def _rationale_options(parser, reviewer=True):
    parser.add_argument('--reviewer-id' if reviewer else '--verifier-id', required=True)
    parser.add_argument('--rationale', required=True, help='Human rationale; stored in the audit record.')


def _write_options(parser, needs_confirmation=True):
    parser.add_argument(
        '--apply', action='store_true',
        help='Commit changes; by default the transaction is rolled back. Requires --confirm.',
    )
    if needs_confirmation:
        parser.add_argument(
            '--confirm', action='store_true',
            help='Confirm this persistent change; required together with --apply.',
        )


def add_write_subcommands(subparsers):
    create = subparsers.add_parser(
        'create', help='Create a draft incident (persistent only with --apply --confirm).'
    )
    create.add_argument('--title')
    create.add_argument('--category', choices=sorted(CATEGORIES))
    _write_options(create, needs_confirmation=True)

    inspect = subparsers.add_parser('inspect-incident', help='Inspect a draft or published incident and its audit history.')
    inspect.add_argument('incident_id')

    attach = subparsers.add_parser(
        'attach-evidence',
        help='Attach a proposed event correlation (persistent only with --apply --confirm).',
    )
    attach.add_argument('incident_id')
    attach.add_argument('event_id')
    attach.add_argument('--event-review-revision', required=True, type=int)
    attach.add_argument('--relationship', choices=('supports', 'contradicts'), default='supports')
    attach.add_argument('--rationale', required=True)
    _write_options(attach, needs_confirmation=True)

    evidence_review = subparsers.add_parser('review-evidence', help='Accept or reject a proposed association.')
    evidence_review.add_argument('evidence_id')
    evidence_review.add_argument('--decision', choices=('accepted', 'rejected'), required=True)
    evidence_review.add_argument('--reviewer-id', required=True)
    evidence_review.add_argument('--rationale', required=True)
    evidence_review.add_argument('--primary', action='store_true')
    _write_options(evidence_review, needs_confirmation=True)

    review = subparsers.add_parser('review', help='Append an incident review revision.')
    review.add_argument('incident_id')
    review.add_argument('--status', choices=sorted(REVIEW_STATUSES), required=True)
    review.add_argument('--reviewer-id', required=True)
    review.add_argument('--rationale', required=True)
    review.add_argument('--title')
    review.add_argument('--category', choices=sorted(CATEGORIES))
    _write_options(review, needs_confirmation=True)

    location = subparsers.add_parser('confirm-location', help='Explicitly confirm a uniquely resolved station as incident location.')
    location.add_argument('incident_id')
    location.add_argument('event_id')
    location.add_argument('--entity-index', required=True, type=int, help='Zero-based index in affected_entities.')
    _rationale_options(location)
    _write_options(location, needs_confirmation=True)

    approve = subparsers.add_parser('approve', help='Record an accepted review and approve publication.')
    approve.add_argument('incident_id')
    _rationale_options(approve)
    _write_options(approve, needs_confirmation=True)

    withdraw = subparsers.add_parser('withdraw', help='Withdraw an approved incident with an audit revision.')
    withdraw.add_argument('incident_id')
    _rationale_options(withdraw)
    _write_options(withdraw, needs_confirmation=True)

    verify = subparsers.add_parser('verify', help='Append a human factual-verification assessment.')
    verify.add_argument('incident_id')
    verify.add_argument('--status', choices=sorted(VERIFICATION_STATUSES), required=True)
    _rationale_options(verify, reviewer=False)
    verify.add_argument('--source-name', required=True)
    verify.add_argument('--source-url')
    verify.add_argument('--checked-at', help='ISO timestamp with timezone; defaults to now.')
    _write_options(verify, needs_confirmation=True)
    return subparsers


def _incident_read_transaction(db_params, incident_id, *, connect=None):
    factory = _connect(connect)
    try:
        with factory(**db_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            return inspect_incident(connection, incident_id)
    except CurationWriteError:
        raise
    except Exception as error:
        raise CurationWriteError('Incident inspection failed: {}'.format(type(error).__name__)) from error


def run_write_command(args, *, connect=None, readonly_connect=None):
    curator_params = load_curator_connection()
    needs_readonly = args.command in {'attach-evidence', 'review-evidence', 'confirm-location'}
    readonly_params = load_curation_connection() if needs_readonly else None
    if args.command == 'inspect-incident':
        result = _incident_read_transaction(curator_params, args.incident_id, connect=connect)
        return 'Incident details loaded. {}'.format(json.dumps(result, default=str, ensure_ascii=False))

    if args.command == 'create':
        operation = lambda db: create_draft(db, title=args.title, category=args.category)
        action = 'Created draft incident'
    elif args.command == 'attach-evidence':
        event_review = read_event_review(
            readonly_params, args.event_id, args.event_review_revision, connect=readonly_connect or connect,
        )
        operation = lambda db: attach_evidence(
            db, incident_id=args.incident_id, event_id=args.event_id,
            event_review_revision=args.event_review_revision,
            event_status=event_review['event_status'], event_index=event_review['event_index'],
            event_review_status=event_review['review_status'],
            rationale=args.rationale, relationship=args.relationship,
        )
        action = 'Proposed evidence association'
    elif args.command == 'review-evidence':
        # A missing or pending pinned revision will be rejected again by the
        # deferred migration constraint when applying the transaction.
        evidence_read = None
        operation = lambda db: review_evidence(
            db, evidence_id=args.evidence_id, decision=args.decision,
            reviewer_id=args.reviewer_id, rationale=args.rationale,
            event_review_status=evidence_read['review_status'] if evidence_read else 'pending',
            primary=args.primary,
        )
        # Read the pinned immutable review through the read-only role after the
        # evidence row has been loaded in a short read-only lookup.
        # The read-only role intentionally cannot see event_review_revision on
        # serving.incident_evidence. Fetch that pin with the curator login in a
        # read-only transaction, then validate the immutable processing review
        # using the separate read-only login.
        factory = _connect(connect)
        with factory(**curator_params, prepare_threshold=None) as ro_connection:
            ro_connection.execute('SET TRANSACTION READ ONLY')
            row = ro_connection.execute(
                'SELECT processing_event_id, event_review_revision FROM serving.incident_evidence '
                'WHERE id = %s::uuid', (_uuid(args.evidence_id, 'Evidence ID'),),
            ).fetchone()
        if row is None:
            raise CurationWriteError('Evidence association not found.')
        evidence_read = read_event_review(
            readonly_params, row[0], row[1], connect=readonly_connect or connect,
        )
        action = 'Recorded evidence decision'
    elif args.command == 'review':
        operation = lambda db: record_incident_review(
            db, incident_id=args.incident_id, reviewer_id=args.reviewer_id,
            status=args.status, rationale=args.rationale, title=args.title, category=args.category,
        )
        action = 'Recorded incident review'
    elif args.command == 'confirm-location':
        entities = read_event_entities(readonly_params, args.event_id, connect=readonly_connect or connect)
        if args.entity_index < 0 or args.entity_index >= len(entities):
            raise CurationWriteError('Entity index is outside the event entity list.')
        resolver = load_reference_stations(readonly_params, connect=readonly_connect or connect)
        resolved = resolver.resolve_entity(entities[args.entity_index])
        if resolved['resolution_status'] != 'resolved' or resolved['entity_type'] != 'station':
            raise CurationWriteError('Selected entity is not one unambiguous station; location was not changed.')
        operation = lambda db: confirm_location(
            db, incident_id=args.incident_id, station=resolved,
            reviewer_id=args.reviewer_id, rationale=args.rationale,
        )
        if not read_event_association(
            readonly_params, args.incident_id, args.event_id,
            connect=readonly_connect or connect,
        ):
            raise CurationWriteError('Confirm location only from an event already associated with this incident.')
        action = 'Confirmed incident location'
    elif args.command == 'approve':
        operation = lambda db: approve_incident(
            db, incident_id=args.incident_id, reviewer_id=args.reviewer_id, rationale=args.rationale,
        )
        action = 'Approved incident publication'
    elif args.command == 'withdraw':
        operation = lambda db: withdraw_incident(
            db, incident_id=args.incident_id, reviewer_id=args.reviewer_id, rationale=args.rationale,
        )
        action = 'Withdrew incident publication'
    elif args.command == 'verify':
        operation = lambda db: record_verification(
            db, incident_id=args.incident_id, verifier_id=args.verifier_id,
            status=args.status, source_name=args.source_name, source_url=args.source_url,
            evidence_summary=args.rationale, checked_at=args.checked_at,
        )
        action = 'Recorded explicit factual assessment'
    else:
        raise CurationWriteError('Unsupported curation command.')

    result = run_transaction(
        curator_params, operation, apply=args.apply,
        confirmed=getattr(args, 'confirm', False), connect=connect,
    )
    prefix = action if args.apply else 'DRY RUN; transaction rolled back. ' + action
    return '{}: {}'.format(prefix, json.dumps(result, ensure_ascii=False))


__all__ = [
    'CATEGORIES', 'CURATOR_LOGIN', 'CurationWriteError', 'add_write_subcommands',
    'attach_evidence', 'approve_incident', 'confirm_location', 'create_draft',
    'inspect_incident', 'load_curator_connection', 'read_event_review',
    'record_incident_review', 'record_verification', 'review_evidence',
    'read_event_association', 'run_transaction', 'run_write_command', 'withdraw_incident',
]
