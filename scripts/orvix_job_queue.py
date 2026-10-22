"""Small client for the PostgreSQL Orvix job queue.

Worker credentials live separately from collector credentials at
data/automation/orvix/worker_connection.json. Keep that file private (0600).
Expected JSON keys: host, port, dbname, user, password. The user must be
moobby_orvix_worker_login (or its Supabase pooler-qualified form).

Every operation uses its own short transaction. Claim tokens are passed only
to the database's SECURITY DEFINER functions; this module never updates jobs.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
WORKER_CONNECTION = ROOT / 'data/automation/orvix/worker_connection.json'
WORKER_LOGIN = 'moobby_orvix_worker_login'
MAX_CLAIM_LIMIT = 5
FAILURE_CODE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


class QueueClientError(Exception):
    """Safe queue-client error that does not include connection details."""


class LeaseRejected(QueueClientError):
    """The database rejected a token-fenced lease operation."""


def load_worker_connection(path: Optional[Path] = None):
    """Read a separate, private worker-login config without exposing values."""
    config_path = Path(path or os.environ.get('ORVIX_WORKER_DB_CONFIG') or WORKER_CONNECTION)
    if config_path.is_symlink() or not config_path.is_file():
        raise QueueClientError('Worker database credentials are not configured.')
    if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise QueueClientError('Worker credential file permissions must be 0600.')
    try:
        value = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QueueClientError('Worker credential file is unreadable or malformed.') from error
    allowed = {'host', 'port', 'dbname', 'user', 'password'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not all(isinstance(value.get(key), str) and value[key]
                       for key in ('host', 'dbname', 'user', 'password'))):
        raise QueueClientError('Worker credential file has an invalid format.')
    username = value['user']
    if username != WORKER_LOGIN and not username.startswith(WORKER_LOGIN + '.'):
        raise QueueClientError('Worker credential file must use the dedicated worker login.')
    if not value['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise QueueClientError('Worker database host must be a Supabase PostgreSQL host.')
    if 'port' in value and not value['port'].isdigit():
        raise QueueClientError('Worker database port is invalid.')
    return dict(value, sslmode='require', connect_timeout=15)


@contextmanager
def _connect(db_params=None):
    """Connect as the dedicated worker login and commit before returning."""
    params = db_params if db_params is not None else load_worker_connection()
    if params.get('user') != WORKER_LOGIN and not str(params.get('user', '')).startswith(WORKER_LOGIN + '.'):
        raise QueueClientError('Queue client requires the dedicated worker login.')
    try:
        import psycopg
    except ImportError as error:
        raise QueueClientError('Install the repository PostgreSQL dependency first.') from error
    try:
        with psycopg.connect(**params, prepare_threshold=None) as conn:
            role = conn.execute('SELECT current_user').fetchone()[0]
            if role != WORKER_LOGIN:
                raise QueueClientError('Connected database role is not the dedicated worker login.')
            conn.execute("SET LOCAL statement_timeout = '20s'")
            yield conn
    except QueueClientError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise QueueClientError('Worker database operation failed: {}{}'.format(type(error).__name__, suffix)) from error


class OrvixJobQueue:
    """Calls only the queue's granted functions and eligible-job INSERT."""

    def __init__(self, db_params=None, connection_factory=None):
        self._db_params = db_params
        self._connection_factory = connection_factory or _connect

    def _connection(self):
        return self._connection_factory(self._db_params)

    def enqueue_eligible(self, limit=5):
        """Enqueue newest eligible snapshots, excluding completed and prior auto jobs."""
        _validate_limit(limit)
        query = """
            INSERT INTO processing.jobs (snapshot_id, manual_reprocess)
            SELECT s.id, false
            FROM collection.raw_snapshots AS s
            WHERE NOT EXISTS (
                SELECT 1
                FROM processing.events AS e
                JOIN processing.processing_runs AS pr ON pr.id = e.run_id
                WHERE e.snapshot_id = s.id
                  AND pr.manifest_status = 'completed'
                  AND pr.imported_at IS NOT NULL
            )
              AND NOT EXISTS (
                SELECT 1 FROM processing.jobs AS j
                WHERE j.snapshot_id = s.id AND j.manual_reprocess = false
            )
            ORDER BY s.captured_at DESC, s.id ASC
            LIMIT %s
            ON CONFLICT DO NOTHING
            RETURNING id, snapshot_id
        """
        with self._connection() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [{'job_id': row[0], 'snapshot_id': row[1]} for row in rows]

    def enqueue_manual(self, snapshot_id):
        """Explicitly queue one snapshot for reprocessing, even if previously done."""
        _validate_uuid(snapshot_id, 'snapshot_id')
        with self._connection() as conn:
            rows = conn.execute(
                "INSERT INTO processing.jobs (snapshot_id, manual_reprocess) "
                "VALUES (%s, true) ON CONFLICT DO NOTHING RETURNING id, snapshot_id",
                (snapshot_id,),
            ).fetchall()
        return [{'job_id': row[0], 'snapshot_id': row[1]} for row in rows]

    def claim_jobs(self, limit=5, lease_duration='5 minutes'):
        """Atomically claim at most five jobs; transaction commits before return."""
        _validate_limit(limit)
        with self._connection() as conn:
            rows = conn.execute(
                'SELECT * FROM processing.claim_jobs(%s, %s::interval)',
                (limit, lease_duration),
            ).fetchall()
        claims = [dict(zip(('job_id', 'snapshot_id', 'claim_token', 'attempt_count',
                            'lease_expires_at'), row)) for row in rows]

        # A snapshot may have been imported just before a worker crashed without
        # completing its queue row. Retire that automatic job without another
        # model request. Explicit manual-reprocess jobs remain eligible.
        ready = []
        for claim in claims:
            with self._connection() as conn:
                state = conn.execute(
                    """
                    SELECT j.manual_reprocess, (
                        SELECT pr.id
                        FROM processing.events AS e
                        JOIN processing.processing_runs AS pr ON pr.id = e.run_id
                        WHERE e.snapshot_id = j.snapshot_id
                          AND pr.manifest_status = 'completed'
                          AND pr.imported_at IS NOT NULL
                        ORDER BY pr.imported_at DESC, pr.id
                        LIMIT 1
                    ) AS completed_run_id
                    FROM processing.jobs AS j
                    WHERE j.id = %s AND j.claim_token = %s AND j.status = 'claimed'
                    """,
                    (claim['job_id'], claim['claim_token']),
                ).fetchone()
            if state and not state[0] and state[1] is not None:
                self.complete(claim['job_id'], claim['claim_token'], state[1])
            else:
                ready.append(claim)
        return ready

    def renew_lease(self, job_id, claim_token, lease_duration='5 minutes'):
        _validate_uuid(job_id, 'job_id')
        _validate_uuid(claim_token, 'claim_token')
        return self._token_operation(
            'SELECT processing.renew_job_lease(%s, %s, %s::interval)',
            (job_id, claim_token, lease_duration),
        )

    def schedule_retry(self, job_id, claim_token, failure_code, delay):
        _validate_uuid(job_id, 'job_id')
        _validate_uuid(claim_token, 'claim_token')
        if not isinstance(failure_code, str) or not FAILURE_CODE.fullmatch(failure_code):
            raise ValueError('failure_code must be a sanitized lowercase code.')
        return self._token_operation(
            'SELECT processing.retry_job(%s, %s, %s, %s::interval)',
            (job_id, claim_token, failure_code, delay),
        )

    def complete(self, job_id, claim_token, processing_run_id):
        _validate_uuid(job_id, 'job_id')
        _validate_uuid(claim_token, 'claim_token')
        _validate_uuid(processing_run_id, 'processing_run_id')
        return self._token_operation(
            'SELECT processing.complete_job(%s, %s, %s)',
            (job_id, claim_token, processing_run_id),
        )

    def _token_operation(self, query, params):
        with self._connection() as conn:
            accepted = conn.execute(query, params).fetchone()[0]
        if not accepted:
            raise LeaseRejected('Database rejected the claim token or lease.')
        return True


def _validate_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CLAIM_LIMIT:
        raise ValueError('limit must be an integer from 1 to 5.')


def _validate_uuid(value, field):
    import uuid
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('{} must be a UUID.'.format(field)) from error
