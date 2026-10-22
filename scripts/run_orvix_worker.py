"""Run one queue-driven Orvix cycle or poll continuously in watch mode."""

import argparse
import csv
import math
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import export_orvix_input, process_orvix_csv  # noqa: E402
from scripts.export_orvix_input import ExportError  # noqa: E402
from scripts.import_orvix_results import (  # noqa: E402
    ImportConflictError,
    ImportDatabaseError,
    ImportJobCompletionError,
    ImportValidationError,
    import_claimed_run,
    validate_run,
)
from scripts.orvix_job_queue import (  # noqa: E402
    LeaseRejected,
    OrvixJobQueue,
    QueueClientError,
    load_worker_connection,
)
from scripts.process_orvix_csv import (  # noqa: E402
    MaxTokensError,
    ProcessingError,
    execute_run,
    parse_positive_int,
    resolve_max_tokens,
)


DEFAULT_LEASE_DURATION = '5 minutes'
DEFAULT_RENEW_INTERVAL_SECONDS = 60
FAILURE_CODE_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


class WorkerCycleError(RuntimeError):
    """Sanitized single-cycle worker failure."""

    def __init__(self, message, failure_code='worker_failed'):
        super().__init__(message)
        self.failure_code = failure_code


class LeaseLostError(WorkerCycleError):
    """At least one claimed job no longer has a renewable lease."""

    def __init__(self):
        super().__init__('A claimed job lease could not be renewed; import was stopped.',
                         'lease_renewal_failed')


class WorkerShutdown(WorkerCycleError):
    """Shutdown requested before another snapshot request could safely start."""

    def __init__(self):
        super().__init__('Worker shutdown requested; outstanding claims were scheduled for retry.',
                         'worker_shutdown')


class LeaseKeeper:
    """Renew each claim on short independent DB connections while work runs."""

    def __init__(self, queue, claims, lease_duration=DEFAULT_LEASE_DURATION,
                 interval_seconds=DEFAULT_RENEW_INTERVAL_SECONDS):
        if interval_seconds <= 0:
            raise ValueError('Lease renewal interval must be positive.')
        self.queue = queue
        self.claims = list(claims)
        self.lease_duration = lease_duration
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failure = None
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='orvix-lease-renewal', daemon=True)
        self._thread.start()

    def renew_once(self):
        with self._lock:
            if self._failure is not None:
                raise self._failure
            for claim in self.claims:
                try:
                    renewed = self.queue.renew_lease(
                        claim['job_id'], claim['claim_token'], self.lease_duration
                    )
                except Exception as error:
                    self._failure = LeaseLostError()
                    self._failure.__cause__ = error
                    raise self._failure
                if not renewed:
                    self._failure = LeaseLostError()
                    raise self._failure
        return True

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            try:
                self.renew_once()
            except LeaseLostError:
                return

    def check(self):
        with self._lock:
            if self._failure is not None:
                raise self._failure

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                self._failure = LeaseLostError()
                raise self._failure


def _snapshot_ids_from_input(path, expected_ids):
    try:
        rows = process_orvix_csv.read_input(path, len(expected_ids) + 1)
    except (OSError, csv.Error, ProcessingError) as error:
        raise WorkerCycleError('Claimed snapshot export could not be validated.', 'export_invalid') from error
    found = [row['snapshot_id'] for row in rows]
    canonical = []
    try:
        canonical = [str(uuid.UUID(value)) for value in found]
    except (ValueError, TypeError, AttributeError) as error:
        raise WorkerCycleError('Claimed snapshot export contains an invalid ID.', 'export_invalid') from error
    if len(canonical) != len(expected_ids) or len(set(canonical)) != len(canonical):
        raise WorkerCycleError('Exported snapshot count does not match the claims.', 'export_mismatch')
    if set(canonical) != expected_ids:
        raise WorkerCycleError('Exported snapshots do not match the claims.', 'export_mismatch')
    return canonical


def _worker_failure_code(error):
    code = getattr(error, 'failure_code', None)
    if isinstance(code, str) and FAILURE_CODE_RE.fullmatch(code):
        return code
    if isinstance(error, ExportError):
        return 'export_failed'
    if isinstance(error, ImportValidationError):
        return 'invalid_output'
    if isinstance(error, ImportConflictError):
        return 'import_conflict'
    if isinstance(error, ImportJobCompletionError):
        return 'atomic_import_failed'
    if isinstance(error, ImportDatabaseError):
        return 'import_failed'
    if isinstance(error, QueueClientError):
        return 'database_failed'
    return 'worker_failed'


def _retry_claims(queue, claims, failure_code):
    for claim in claims:
        attempt = claim.get('attempt_count', 1)
        attempt = attempt if isinstance(attempt, int) and attempt > 0 else 1
        delay_seconds = min(3600, 30 * (2 ** min(attempt - 1, 7)))
        try:
            queue.schedule_retry(
                claim['job_id'], claim['claim_token'], failure_code,
                '{} seconds'.format(delay_seconds),
            )
        except (LeaseRejected, QueueClientError, ValueError):
            # A stale/expired claim cannot be retried by this worker. The DB
            # lease recovery path will make it eligible when appropriate.
            continue


def parse_positive_interval(value):
    try:
        interval = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError('interval must be a positive number of seconds.') from error
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError('interval must be a positive number of seconds.')
    return interval


def run_worker_cycle(
        limit=5,
        *,
        max_tokens=None,
        api_key=None,
        db_params_provider=None,
        queue_factory=None,
        export_fn=None,
        process_fn=None,
        validate_fn=None,
        import_fn=None,
        heartbeat_factory=None,
        staging_parent=None,
        lease_duration=DEFAULT_LEASE_DURATION,
        renew_interval_seconds=DEFAULT_RENEW_INTERVAL_SECONDS,
        shutdown_event=None):
    """Enqueue, claim, export, process, validate, and atomically import once."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ValueError('limit must be an integer from 1 to 5.')
    max_tokens = resolve_max_tokens(max_tokens)
    db_params_provider = db_params_provider or load_worker_connection
    queue_factory = queue_factory or OrvixJobQueue
    export_fn = export_fn or export_orvix_input.export
    process_fn = process_fn or execute_run
    validate_fn = validate_fn or validate_run
    import_fn = import_fn or import_claimed_run
    heartbeat_factory = heartbeat_factory or LeaseKeeper
    shutdown_event = shutdown_event or threading.Event()

    db_params = db_params_provider()
    queue = queue_factory(db_params)
    queue.enqueue_eligible(limit=limit)
    claims = queue.claim_jobs(limit=limit, lease_duration=lease_duration)
    if not claims:
        return {
            'run_id': None, 'snapshot_count': 0, 'event_count': 0,
            'import_status': 'skipped_no_jobs',
        }
    keeper = None
    try:
        if len(claims) > limit:
            raise WorkerCycleError('Queue returned more claims than requested.', 'invalid_claim_batch')
        keeper = heartbeat_factory(
            queue, claims, lease_duration=lease_duration,
            interval_seconds=renew_interval_seconds,
        )
        keeper.start()
        keeper.renew_once()
        _check_worker_continue(keeper, shutdown_event)
        snapshot_ids = {str(uuid.UUID(str(claim['snapshot_id']))) for claim in claims}
        staging_parent = Path(staging_parent or ROOT / 'data/orvix/worker_staging')
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='cycle_', dir=staging_parent) as directory:
            input_path = Path(directory) / 'raw_snapshots.csv'
            exported_count = export_fn(
                len(claims), db_params=db_params,
                snapshot_ids=[claim['snapshot_id'] for claim in claims],
                output_path=input_path,
            )
            if exported_count != len(claims):
                raise WorkerCycleError('Exporter did not return every claimed snapshot.', 'export_mismatch')
            _snapshot_ids_from_input(input_path, snapshot_ids)

            process_options = {
                'max_tokens': max_tokens,
                'before_snapshot': lambda: _check_worker_continue(keeper, shutdown_event),
            }
            if api_key is not None:
                process_options['api_key'] = api_key
            run_id, run_directory, processed_count, processor_event_count = process_fn(
                input_path, len(claims), **process_options,
            )
            keeper.check()
            payload = validate_fn(run_directory)
            validated_run_id = str(payload['run']['id'])
            input_ids = {str(value) for value in payload.get('input_snapshot_ids', ())}
            event_ids = {str(event['snapshot_id']) for event in payload['events']}
            event_count = len(payload['events'])
            if (validated_run_id != str(run_id)
                    or payload['input_snapshot_count'] != len(claims)
                    or input_ids != snapshot_ids or event_ids != snapshot_ids
                    or processed_count != len(claims)
                    or processor_event_count != event_count):
                raise WorkerCycleError('Validated run does not match the claimed snapshots.', 'result_mismatch')

            # Let the heartbeat finish any in-flight renewal before the importer
            # takes job-row locks and completes all claims atomically.
            keeper.renew_once()
            keeper.stop()
            keeper.check()
            _import_payload, import_status = import_fn(
                run_directory, claims, db_params=db_params,
            )
        return {
            'run_id': validated_run_id,
            'snapshot_count': len(claims),
            'event_count': event_count,
            'import_status': import_status,
        }
    except Exception as error:
        if keeper is not None:
            try:
                keeper.stop()
            except LeaseLostError:
                pass
        code = _worker_failure_code(error)
        _retry_claims(queue, claims, code)
        if isinstance(error, WorkerCycleError):
            raise
        raise WorkerCycleError(
            'Worker cycle failed [{}] ({}).'.format(code, type(error).__name__), code
        ) from error
    finally:
        if keeper is not None:
            try:
                keeper.stop()
            except LeaseLostError:
                pass


def _check_worker_continue(keeper, shutdown_event):
    keeper.check()
    if shutdown_event.is_set():
        raise WorkerShutdown()


def run_watch(
        limit=5,
        interval_seconds=60,
        *,
        max_tokens=None,
        api_key=None,
        stop_event=None,
        cycle_fn=None,
        wait_fn=None,
        log_fn=print):
    """Run synchronous worker cycles until a shutdown event is set."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
        raise ValueError('interval must be a positive number of seconds.')
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError('interval must be a positive number of seconds.')
    if not isinstance(api_key, str) or not api_key.strip():
        api_key = process_orvix_csv.load_api_key(prompt=False)
    else:
        api_key = api_key.strip()
    stop_event = stop_event or threading.Event()
    cycle_fn = cycle_fn or run_worker_cycle
    wait_fn = wait_fn or stop_event.wait

    while not stop_event.is_set():
        try:
            summary = cycle_fn(limit, max_tokens=max_tokens, api_key=api_key,
                               shutdown_event=stop_event)
        except (WorkerCycleError, ExportError, ProcessingError, ImportValidationError,
                ImportConflictError, ImportDatabaseError, ImportJobCompletionError,
                QueueClientError, OSError, csv.Error) as error:
            if isinstance(error, WorkerShutdown):
                log_fn('Shutdown requested; outstanding claims were scheduled for retry.')
            else:
                code = getattr(error, 'failure_code', _worker_failure_code(error))
                log_fn('Orvix worker cycle failed [{}]: {}'.format(code, type(error).__name__))
        else:
            if summary['run_id'] is None:
                log_fn('No jobs available; next poll in {} second(s).'.format(interval_seconds))
            else:
                log_fn('Orvix worker {}: run {}, {} snapshot(s), {} event row(s).'.format(
                    summary['import_status'], summary['run_id'],
                    summary['snapshot_count'], summary['event_count'],
                ))

        if stop_event.is_set():
            break
        if wait_fn(interval_seconds):
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=5, help='Maximum jobs to claim (1-5, default: 5).')
    parser.add_argument('--max-tokens', type=parse_positive_int,
                        help='Maximum completion tokens (or use ORVIX_MAX_TOKENS).')
    parser.add_argument('--watch', action='store_true', help='Poll continuously instead of running one cycle.')
    parser.add_argument('--interval', type=parse_positive_interval, default=60,
                        help='Seconds between watch cycles (default: 60).')
    args = parser.parse_args(argv)
    stop_event = threading.Event()
    previous_handlers = {}

    def request_shutdown(_signum, _frame):
        stop_event.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, request_shutdown)
    try:
        if args.watch:
            run_watch(args.limit, args.interval, max_tokens=args.max_tokens,
                      stop_event=stop_event)
            return 0
        summary = run_worker_cycle(args.limit, max_tokens=args.max_tokens,
                                   shutdown_event=stop_event)
    except process_orvix_csv.ApiKeyConfigurationError as error:
        print('Orvix worker configuration failed: {}'.format(error), file=sys.stderr)
        return 1
    except MaxTokensError as error:
        print('Orvix worker failed: {}'.format(error), file=sys.stderr)
        return 1
    except (WorkerCycleError, ExportError, ProcessingError, ImportValidationError,
            ImportConflictError, ImportDatabaseError, ImportJobCompletionError,
            QueueClientError, OSError, csv.Error, ValueError) as error:
        if stop_event.is_set():
            print('Orvix worker stopped.')
            return 0
        code = getattr(error, 'failure_code', 'worker_failed')
        print('Orvix worker failed [{}]: {}'.format(code, type(error).__name__), file=sys.stderr)
        return 1
    except Exception as error:
        print('Orvix worker failed [worker_failed]: {}'.format(type(error).__name__), file=sys.stderr)
        return 1
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)

    if stop_event.is_set():
        print('Orvix worker stopped.')
        return 0
    if summary['run_id'] is None:
        print('No jobs available; Orvix processing was skipped.')
        return 0
    print('Orvix worker {}: run {}, {} snapshot(s), {} event row(s).'.format(
        summary['import_status'], summary['run_id'],
        summary['snapshot_count'], summary['event_count'],
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
