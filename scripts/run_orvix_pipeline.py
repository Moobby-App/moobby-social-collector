"""Export, process, validate, and import one Orvix snapshot batch."""

import argparse
import csv
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import export_orvix_input  # noqa: E402
from scripts.export_orvix_input import ExportError, connection_parameters  # noqa: E402
from scripts.import_orvix_results import (  # noqa: E402
    ImportConflictError,
    ImportDatabaseError,
    ImportValidationError,
    import_payload,
    validate_run,
)
from scripts.process_orvix_csv import (  # noqa: E402
    MaxTokensError,
    ProcessingError,
    execute_run,
    parse_positive_int,
    resolve_max_tokens,
)


class PipelineError(RuntimeError):
    """A safe pipeline consistency error."""


def run_pipeline(
        limit=5,
        *,
        max_tokens=None,
        include_processed=False,
        db_params_provider=None,
        export_fn=None,
        process_fn=None,
        validate_fn=None,
        import_fn=None):
    """Run each existing stage in order, stopping before downstream work on failure."""
    db_params_provider = db_params_provider or connection_parameters
    export_fn = export_fn or export_orvix_input.export
    process_fn = process_fn or execute_run
    validate_fn = validate_fn or validate_run
    import_fn = import_fn or import_payload

    max_tokens = resolve_max_tokens(max_tokens)
    db_params = db_params_provider()
    exported_count = export_fn(
        limit, db_params=db_params, include_processed=include_processed
    )
    if exported_count == 0:
        return {
            'run_id': None,
            'snapshot_count': 0,
            'event_count': 0,
            'import_status': 'skipped_no_snapshots',
        }
    run_id, run_directory, processed_count, processed_event_count = process_fn(
        export_orvix_input.OUTPUT_PATH, limit, max_tokens=max_tokens
    )
    payload = validate_fn(run_directory)

    result_run_id = str(payload['run']['id'])
    if result_run_id != str(run_id):
        raise PipelineError('Validated manifest run ID differs from the processor result.')
    if (exported_count != processed_count
            or payload['input_snapshot_count'] != exported_count):
        raise PipelineError('Exported and processed snapshot counts do not match.')
    event_count = len(payload['events'])
    snapshot_ids = {str(event['snapshot_id']) for event in payload['events']}
    if len(snapshot_ids) != exported_count or event_count != processed_event_count:
        raise PipelineError('Validated output does not contain results for every exported snapshot.')

    import_status = import_fn(payload, db_params)
    return {
        'run_id': result_run_id,
        'snapshot_count': exported_count,
        'event_count': event_count,
        'import_status': import_status,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=5, help='Maximum snapshots to process (default: 5).')
    parser.add_argument('--max-tokens', type=parse_positive_int,
                        help='Maximum completion tokens (or use ORVIX_MAX_TOKENS).')
    parser.add_argument('--include-processed', action='store_true',
                        help='Reprocess snapshots already present in imported completed runs.')
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('--limit must be a positive integer')

    try:
        summary = run_pipeline(
            args.limit, max_tokens=args.max_tokens,
            include_processed=args.include_processed,
        )
    except (ExportError, ProcessingError, ImportValidationError,
            ImportConflictError, ImportDatabaseError, PipelineError) as error:
        print('Orvix pipeline failed: {}'.format(error), file=sys.stderr)
        return 1
    except MaxTokensError as error:
        print('Orvix pipeline failed: {}'.format(error), file=sys.stderr)
        return 1
    except (OSError, csv.Error) as error:
        print('Orvix pipeline failed: {}'.format(type(error).__name__), file=sys.stderr)
        return 1
    except Exception as error:
        print('Orvix pipeline failed: {}'.format(type(error).__name__), file=sys.stderr)
        return 1

    if summary['run_id'] is None:
        print('No snapshots selected; Orvix run and database import were skipped.')
        return 0
    print('Orvix pipeline {}: run {}, {} snapshot(s), {} event row(s).'.format(
        summary['import_status'], summary['run_id'],
        summary['snapshot_count'], summary['event_count'],
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
