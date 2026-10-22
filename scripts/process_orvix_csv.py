"""Process exported raw snapshots with Orvix and write validated event rows."""

import argparse
import csv
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from datetime import datetime, timezone
import uuid

import openai
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / 'data/orvix/raw_snapshots.csv'
RUNS_PATH = ROOT / 'data/orvix/runs'
BASE_URL = 'https://api.orvix.id/v1'
MODEL = 'orvix/deepseek-v4.1-flash'
TIMEOUT_SECONDS = 120
USER_AGENT = 'moobby-collector/0.1'
PROCESSOR_VERSION = '1.0.0'
PROMPT_VERSION = 'orvix.transport.v1'
MANIFEST_VERSION = 'orvix.processing_manifest.v1'
DEFAULT_MAX_TOKENS = 10000
API_KEY_FILE = ROOT / 'data/automation/orvix/orvix.env'

EVENT_FIELDS = (
    'event_index',
    'event_status',
    'disruption_type',
    'disruption_type_evidence',
    'affected_entities',
    'severity',
    'severity_evidence',
    'event_start_time',
    'event_start_time_evidence',
    'event_end_time',
    'event_end_time_evidence',
    'uncertainty_status',
    'uncertainty_note',
    'explanation',
    'evidence_quotes',
)

OUTPUT_FIELDS = (
    'schema_version',
    'snapshot_id',
    *EVENT_FIELDS[:1],
    *EVENT_FIELDS[1:4],
    'affected_entities_json',
    *EVENT_FIELDS[5:13],
    'explanation',
    'evidence_quotes_json',
    'model',
    'processor_version',
    'prompt_version',
    'review_status',
    'review_notes',
)

EVIDENCE_VALUES = {'explicit', 'inferred', 'unknown', 'not_applicable'}
EVENT_STATUS_VALUES = {'potential_disruption', 'no_disruption', 'unclear'}
DISRUPTION_TYPES = {
    'delay',
    'service_suspension',
    'route_change',
    'signal_or_power_issue',
    'infrastructure_issue',
    'accident_or_safety_issue',
    'overcrowding',
    'flooding_or_weather',
    'other',
    'unknown',
    'not_applicable',
}
SEVERITY_VALUES = {'low', 'moderate', 'high', 'critical', 'unknown', 'not_applicable'}
ENTITY_TYPES = {'line', 'station', 'route'}
UNCERTAINTY_VALUES = {'yes', 'no', 'unknown'}

SYSTEM_PROMPT = """Analyze only the original raw_text supplied by the user. Identify potential disruptions affecting KRL Jabodetabek. Do not use outside knowledge, claim verification, or guess missing details. A post may contain multiple distinct events; return one event object per distinct event, keeping each event's fields together. Always return at least one event: use one no_disruption row with event_index 0 when the text clearly describes no potential disruption, or one unclear row with event_index 0 when the whole post cannot be classified. For actual potential disruption events, use event_index values 1 through N in order. Never combine an index-0 row with disruption event rows. Use null for unknown times and empty arrays for unknown affected entities. Every evidence quote must be copied verbatim from raw_text. Use only the allowed values in the requested JSON shape. Return JSON only, shaped as {\"events\":[event objects]} with each event containing exactly these fields: event_index (0 for the single no_disruption or whole-post unclear row; otherwise positive sequential integer starting at 1), event_status (potential_disruption|no_disruption|unclear), disruption_type (delay|service_suspension|route_change|signal_or_power_issue|infrastructure_issue|accident_or_safety_issue|overcrowding|flooding_or_weather|other|unknown|not_applicable), disruption_type_evidence (explicit|inferred|unknown|not_applicable), affected_entities (array of objects with exactly entity_type (line|station|route), value (string), evidence_type (explicit|inferred), evidence_quote (verbatim string)), severity (low|moderate|high|critical|unknown|not_applicable), severity_evidence (explicit|inferred|unknown|not_applicable), event_start_time and event_end_time (ISO 8601 timestamp with timezone or null), event_start_time_evidence and event_end_time_evidence (explicit|inferred|unknown|not_applicable), uncertainty_status (yes|no|unknown), uncertainty_note (string), explanation (concise evidence-based string), evidence_quotes (array of verbatim strings). For no_disruption use not_applicable for disruption type, its evidence, severity, severity evidence, and both times' evidence; use null for both times and an empty affected_entities array. For unclear, use unknown values where information is unavailable. Do not add fields."""


class ProcessingError(Exception):
    """An error that is safe to report without source text or credentials."""

    def __init__(self, message, failure_code='invalid_response', response_diagnostics=None):
        super().__init__(message)
        self.failure_code = failure_code
        self.response_diagnostics = response_diagnostics


class MaxTokensError(ValueError):
    """Invalid ORVIX_MAX_TOKENS or --max-tokens value."""


class ApiKeyConfigurationError(RuntimeError):
    """Safe Orvix key setup error without including the key value."""


def parse_positive_int(value):
    """Argparse type for strictly positive integer options."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError('must be a positive integer') from error
    if isinstance(value, bool) or parsed < 1 or str(value).strip() != str(parsed):
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def resolve_max_tokens(cli_value=None, env_value=None):
    """Resolve CLI, environment, or compatible default and validate it."""
    value = cli_value
    if value is None:
        value = os.environ.get('ORVIX_MAX_TOKENS') if env_value is None else env_value
    if value is None:
        return DEFAULT_MAX_TOKENS
    try:
        return parse_positive_int(value)
    except argparse.ArgumentTypeError as error:
        raise MaxTokensError(
            'ORVIX_MAX_TOKENS and --max-tokens must be a positive integer.'
        ) from error


FAILURE_CODES = {
    'authentication_error',
    'permission_denied',
    'rate_limited',
    'api_timeout',
    'api_connection_error',
    'api_error',
    'unsupported_parameter',
    'empty_response',
    'response_refusal',
    'truncated_response',
    'invalid_json',
    'invalid_response',
    'processing_failed',
}

FINISH_REASONS = {'stop', 'length', 'tool_calls', 'function_call', 'content_filter'}


def _get_field(value, field):
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _token_usage(response):
    usage = _get_field(response, 'usage')
    if usage is None:
        return None
    result = {}
    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = _get_field(usage, field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[field] = value
    details = _get_field(usage, 'completion_tokens_details')
    reasoning_tokens = _get_field(details, 'reasoning_tokens') if details is not None else None
    if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool) and reasoning_tokens >= 0:
        result['reasoning_tokens'] = reasoning_tokens
    return result or None


def _response_details(response):
    """Extract only bounded, non-content response metadata."""
    choices = _get_field(response, 'choices')
    if not isinstance(choices, (list, tuple)):
        choices = []
    choice = choices[0] if choices else None
    message = _get_field(choice, 'message') if choice is not None else None
    content = _get_field(message, 'content') if message is not None else None
    refusal = _get_field(message, 'refusal') if message is not None else None
    finish_reason = _get_field(choice, 'finish_reason') if choice is not None else None
    if finish_reason not in FINISH_REASONS:
        finish_reason = 'other' if finish_reason is not None else None
    details = {
        'choices_count': len(choices),
        'finish_reason': finish_reason,
        'content_present': content is not None,
        'content_nonempty': isinstance(content, str) and bool(content.strip()),
        'refusal_present': isinstance(refusal, str) and bool(refusal.strip()),
        'token_usage': _token_usage(response),
    }
    return content, details


def _is_unsupported_reasoning_effort(error):
    """Recognize only explicit unsupported-parameter API errors without logging them."""
    if not isinstance(error, openai.BadRequestError):
        return False

    param = getattr(error, 'param', None)
    code = getattr(error, 'code', None)
    message = str(error).lower()
    body = getattr(error, 'body', None)
    if isinstance(body, dict):
        details = body.get('error', body)
        if isinstance(details, dict):
            param = param or details.get('param')
            code = code or details.get('code')
            message = '{} {}'.format(message, details.get('message', '')).lower()

    if param == 'reasoning_effort':
        return True
    return (
        isinstance(code, str)
        and code in {'unsupported_parameter', 'unknown_parameter'}
        and 'reasoning_effort' in message
    )


def load_api_key(prompt=False, env_value=None, path=None):
    """Load the key from the environment or a private env file, optionally prompting."""
    key = os.environ.get('ORVIX_API_KEY', '') if env_value is None else env_value
    if isinstance(key, str) and key.strip():
        return key.strip()

    secret_path = Path(path or API_KEY_FILE)
    if secret_path.is_symlink():
        raise ApiKeyConfigurationError('Orvix key file must not be a symbolic link.')
    if secret_path.exists():
        try:
            mode = stat.S_IMODE(secret_path.stat().st_mode)
            if mode != 0o600:
                raise ApiKeyConfigurationError(
                    'Orvix key file permissions must be exactly 0600.'
                )
            key = _read_api_key_env_file(secret_path)
        except ApiKeyConfigurationError:
            raise
        except (OSError, UnicodeError) as error:
            raise ApiKeyConfigurationError('Orvix key file could not be read safely.') from error
        if key:
            return key

    if prompt:
        key = getpass.getpass('Orvix API key (hidden): ').strip()
        if key:
            return key
    raise ApiKeyConfigurationError(
        'Orvix API key is not configured; set ORVIX_API_KEY or create the private '
        'data/automation/orvix/orvix.env file.'
    )


def get_api_key():
    """Compatibility helper retaining the hidden interactive fallback."""
    return load_api_key(prompt=True)


def _read_api_key_env_file(path):
    key = None
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, separator, value = line.partition('=')
        if not separator or name.strip() != 'ORVIX_API_KEY':
            continue
        if key is not None:
            raise ApiKeyConfigurationError('Orvix key file contains duplicate ORVIX_API_KEY entries.')
        value = value.strip()
        if value.startswith(('\"', "'")):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ApiKeyConfigurationError('Orvix key file has invalid quoting.')
            value = value[1:-1]
        key = value.strip()
    return key or ''


def _require_enum(value, allowed, field):
    if not isinstance(value, str) or value not in allowed:
        raise ProcessingError('Invalid value for {}.'.format(field))


def _validate_time(value, field):
    if value is None:
        return
    if not isinstance(value, str):
        raise ProcessingError('Invalid value for {}.'.format(field))
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ProcessingError('Invalid value for {}.'.format(field)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProcessingError('Invalid value for {}.'.format(field))


def _validate_quote(quote, raw_text):
    if not isinstance(quote, str) or not quote or quote not in raw_text:
        raise ProcessingError('Evidence quote is absent from the source text.')


def validate_response(payload, raw_text):
    """Validate event structure, values, indices, and verbatim source evidence."""
    if not isinstance(payload, dict) or set(payload) != {'events'}:
        raise ProcessingError('Response must be a JSON object containing events only.')
    events = payload['events']
    if not isinstance(events, list) or not events:
        raise ProcessingError('Response must contain at least one event row.')

    has_sentinel = any(isinstance(event, dict) and event.get('event_index') == 0 for event in events)
    if has_sentinel:
        if len(events) != 1 or not isinstance(events[0], dict) or events[0].get('event_status') not in {
                'no_disruption', 'unclear'}:
            raise ProcessingError('An index-0 sentinel must be the only no_disruption or unclear row.')
    elif any(not isinstance(event, dict) or event.get('event_status') != 'potential_disruption'
             for event in events):
        raise ProcessingError('No-disruption and whole-post unclear rows must use index 0 alone.')

    validated = []
    expected_indices = [0] if has_sentinel else list(range(1, len(events) + 1))
    for expected_index, event in zip(expected_indices, events):
        if not isinstance(event, dict) or set(event) != set(EVENT_FIELDS):
            raise ProcessingError('Event fields do not match the output contract.')
        index = event['event_index']
        if isinstance(index, bool) or not isinstance(index, int) or index != expected_index:
            raise ProcessingError('Event indices must be 0 for a sentinel or sequential from 1 for disruption events.')

        _require_enum(event['event_status'], EVENT_STATUS_VALUES, 'event_status')
        _require_enum(event['disruption_type'], DISRUPTION_TYPES, 'disruption_type')
        _require_enum(event['disruption_type_evidence'], EVIDENCE_VALUES, 'disruption_type_evidence')
        _require_enum(event['severity'], SEVERITY_VALUES, 'severity')
        _require_enum(event['severity_evidence'], EVIDENCE_VALUES, 'severity_evidence')
        _require_enum(event['event_start_time_evidence'], EVIDENCE_VALUES, 'event_start_time_evidence')
        _require_enum(event['event_end_time_evidence'], EVIDENCE_VALUES, 'event_end_time_evidence')
        _require_enum(event['uncertainty_status'], UNCERTAINTY_VALUES, 'uncertainty_status')

        for field in ('event_start_time', 'event_end_time'):
            _validate_time(event[field], field)
        for value_field, evidence_field in (
            ('event_start_time', 'event_start_time_evidence'),
            ('event_end_time', 'event_end_time_evidence'),
        ):
            value, evidence = event[value_field], event[evidence_field]
            allowed_unknown_evidence = {'unknown', 'not_applicable'} if event['event_status'] == 'no_disruption' else {'unknown'}
            if value is None and evidence not in allowed_unknown_evidence:
                raise ProcessingError('Unknown times must retain unknown evidence.')
            if value is not None and evidence not in {'explicit', 'inferred'}:
                raise ProcessingError('Known times require explicit or inferred evidence.')

        if event['event_status'] != 'no_disruption':
            if event['disruption_type'] == 'not_applicable':
                raise ProcessingError('not_applicable disruption type is reserved for no_disruption rows.')
            expected_type_evidence = 'unknown' if event['disruption_type'] == 'unknown' else None
            if expected_type_evidence and event['disruption_type_evidence'] != expected_type_evidence:
                raise ProcessingError('Unknown disruption types require unknown evidence.')
            if not expected_type_evidence and event['disruption_type_evidence'] not in {'explicit', 'inferred'}:
                raise ProcessingError('Known disruption types require explicit or inferred evidence.')
            if event['severity'] == 'not_applicable':
                raise ProcessingError('not_applicable severity is reserved for no_disruption rows.')
            expected_severity_evidence = 'unknown' if event['severity'] == 'unknown' else None
            if expected_severity_evidence and event['severity_evidence'] != expected_severity_evidence:
                raise ProcessingError('Unknown severity requires unknown evidence.')
            if not expected_severity_evidence and event['severity_evidence'] not in {'explicit', 'inferred'}:
                raise ProcessingError('Known severity requires explicit or inferred evidence.')

        entities = event['affected_entities']
        if not isinstance(entities, list):
            raise ProcessingError('affected_entities must be an array.')
        for entity in entities:
            if not isinstance(entity, dict) or set(entity) != {'entity_type', 'value', 'evidence_type', 'evidence_quote'}:
                raise ProcessingError('Affected entity fields do not match the output contract.')
            _require_enum(entity['entity_type'], ENTITY_TYPES, 'entity_type')
            _require_enum(entity['evidence_type'], {'explicit', 'inferred'}, 'entity.evidence_type')
            if not isinstance(entity['value'], str) or not entity['value'].strip():
                raise ProcessingError('Affected entity value must be non-empty.')
            _validate_quote(entity['evidence_quote'], raw_text)

        quotes = event['evidence_quotes']
        if not isinstance(quotes, list):
            raise ProcessingError('evidence_quotes must be an array.')
        for quote in quotes:
            _validate_quote(quote, raw_text)

        if event['event_status'] == 'potential_disruption':
            if not quotes:
                raise ProcessingError('Potential disruptions require verbatim evidence.')
        elif event['event_status'] == 'no_disruption':
            if (event['disruption_type'] != 'not_applicable'
                    or event['disruption_type_evidence'] != 'not_applicable'
                    or entities or event['severity'] != 'not_applicable'
                    or event['severity_evidence'] != 'not_applicable'
                    or event['event_start_time'] is not None or event['event_end_time'] is not None
                    or event['event_start_time_evidence'] != 'not_applicable'
                    or event['event_end_time_evidence'] != 'not_applicable'):
                raise ProcessingError('no_disruption rows must use not_applicable event details.')
        for field in ('uncertainty_note', 'explanation'):
            if not isinstance(event[field], str):
                raise ProcessingError('{} must be text.'.format(field))
        if event['uncertainty_status'] == 'yes' and not event['uncertainty_note'].strip():
            raise ProcessingError('Uncertain events require an uncertainty note.')
        validated.append(event)
    return validated


def parse_model_response(content, raw_text, response_diagnostics=None):
    if not isinstance(content, str) or not content.strip():
        raise ProcessingError(
            'Orvix returned no usable message content.',
            failure_code='empty_response',
            response_diagnostics=response_diagnostics,
        )
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise ProcessingError(
            'Orvix returned invalid JSON; retry the snapshot and inspect the safe response diagnostics.',
            failure_code='invalid_json',
            response_diagnostics=response_diagnostics,
        ) from error
    return validate_response(payload, raw_text)


def process_snapshot(client, row, diagnostics_callback=None, max_tokens=None):
    raw_text = row.get('raw_text')
    if not isinstance(row.get('snapshot_id'), str) or not row['snapshot_id']:
        raise ProcessingError('Input row is missing snapshot_id.')
    if not isinstance(raw_text, str):
        raise ProcessingError('Input row is missing raw_text.')
    max_tokens = resolve_max_tokens(max_tokens)
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': raw_text},
            ],
            response_format={'type': 'json_object'},
            reasoning_effort='low',
            max_tokens=max_tokens,
        )
    except openai.AuthenticationError as error:
        raise ProcessingError(
            'Orvix authentication failed; verify that the API key is valid and has ai:invoke scope.',
            failure_code='authentication_error',
        ) from error
    except openai.PermissionDeniedError as error:
        raise ProcessingError(
            'Orvix denied access; verify ai:invoke scope and project access.',
            failure_code='permission_denied',
        ) from error
    except openai.RateLimitError as error:
        raise ProcessingError('Orvix rate limit reached; wait before retrying.', failure_code='rate_limited') from error
    except openai.APITimeoutError as error:
        raise ProcessingError(
            'Orvix request timed out after {} seconds; retry the run.'.format(TIMEOUT_SECONDS),
            failure_code='api_timeout',
        ) from error
    except openai.APIConnectionError as error:
        raise ProcessingError(
            'Could not connect to Orvix; check network access and retry.',
            failure_code='api_connection_error',
        ) from error
    except openai.BadRequestError as error:
        if _is_unsupported_reasoning_effort(error):
            raise ProcessingError(
                'Orvix rejected reasoning_effort for this endpoint or model; verify support before retrying.',
                failure_code='unsupported_parameter',
            ) from error
        raise ProcessingError(
            'Orvix rejected the request (BadRequestError); check request compatibility and retry.',
            failure_code='api_error',
        ) from error
    except openai.APIError as error:
        raise ProcessingError(
            'Orvix API request failed ({}); inspect service status and retry.'.format(type(error).__name__),
            failure_code='api_error',
        ) from error
    content, diagnostics = _response_details(response)
    if diagnostics_callback is not None:
        diagnostics_callback(diagnostics)
    if diagnostics['finish_reason'] == 'length':
        raise ProcessingError(
            'Orvix response was truncated at max_tokens={}; retry with a shorter input or prompt.'.format(
                max_tokens
            ),
            failure_code='truncated_response',
            response_diagnostics=diagnostics,
        )
    if not isinstance(content, str) or not content.strip():
        if diagnostics['refusal_present']:
            raise ProcessingError(
                'Orvix refused the request; review the request and retry.',
                failure_code='response_refusal',
                response_diagnostics=diagnostics,
            )
        raise ProcessingError(
            'Orvix response had no usable message content; inspect safe response diagnostics and retry.',
            failure_code='empty_response',
            response_diagnostics=diagnostics,
        )
    events = parse_model_response(content, raw_text, diagnostics)
    return [output_row(row['snapshot_id'], event) for event in events]


def output_row(snapshot_id, event):
    row = {
        'schema_version': PROMPT_VERSION,
        'snapshot_id': snapshot_id,
        'event_index': event['event_index'],
        'event_status': event['event_status'],
        'disruption_type': event['disruption_type'],
        'disruption_type_evidence': event['disruption_type_evidence'],
        'affected_entities_json': json.dumps(
            event['affected_entities'], ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ),
        'severity': event['severity'],
        'severity_evidence': event['severity_evidence'],
        'event_start_time': event['event_start_time'] or '',
        'event_start_time_evidence': event['event_start_time_evidence'],
        'event_end_time': event['event_end_time'] or '',
        'event_end_time_evidence': event['event_end_time_evidence'],
        'uncertainty_status': event['uncertainty_status'],
        'uncertainty_note': event['uncertainty_note'],
        'explanation': event['explanation'],
        'evidence_quotes_json': json.dumps(
            event['evidence_quotes'], ensure_ascii=False, separators=(',', ':')
        ),
        'model': MODEL,
        'processor_version': PROCESSOR_VERSION,
        'prompt_version': PROMPT_VERSION,
        'review_status': 'pending',
        'review_notes': '',
    }
    return row


def write_output(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', newline='', dir=path.parent,
                prefix='.processed_events.', suffix='.tmp', delete=False) as stream:
            temporary_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, extrasaction='raise')
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_manifest_atomic(path, manifest):
    """Replace a run manifest atomically so readers never see partial JSON."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', newline='\n', dir=path.parent,
                prefix='.processing_manifest.', suffix='.tmp', delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def create_orvix_client(api_key=None):
    return OpenAI(
        api_key=api_key if api_key is not None else get_api_key(),
        base_url=BASE_URL,
        default_headers={'User-Agent': USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
    )


def execute_run(input_path, limit, client_factory=None, processor=None, runs_path=None,
                max_tokens=None, before_snapshot=None, api_key=None):
    """Create an immutable per-execution input/output bundle and its manifest."""
    client_factory = client_factory or (lambda: create_orvix_client(api_key=api_key))
    use_response_diagnostics = processor is None
    processor = processor or process_snapshot
    runs_path = Path(runs_path or RUNS_PATH)
    max_tokens = resolve_max_tokens(max_tokens)

    input_bytes = Path(input_path).read_bytes()
    run_id = str(uuid.uuid4())
    run_directory = runs_path / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    copied_input = run_directory / 'input.csv'
    output_path = run_directory / 'processed_events.csv'
    manifest_path = run_directory / 'processing_manifest.json'
    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'run_id': run_id,
        'status': 'processing',
        'started_at': _utc_now(),
        'completed_at': None,
        'model': MODEL,
        'schema_version': PROMPT_VERSION,
        'processor_version': PROCESSOR_VERSION,
        'prompt_version': PROMPT_VERSION,
        'max_tokens': max_tokens,
        'input_csv': {'path': 'input.csv', 'sha256': _sha256(input_bytes)},
        'output_csv': {'path': 'processed_events.csv', 'sha256': None},
    }
    write_manifest_atomic(manifest_path, manifest)

    client = None
    response_diagnostics = []
    try:
        copied_input.write_bytes(input_bytes)
        rows = read_input(copied_input, limit)
        output_rows = []
        if rows:
            client = client_factory()
            for snapshot_number, row in enumerate(rows, start=1):
                if before_snapshot is not None:
                    before_snapshot()
                if use_response_diagnostics:
                    output_rows.extend(process_snapshot(
                        client,
                        row,
                        diagnostics_callback=lambda details, number=snapshot_number: response_diagnostics.append(
                            {'snapshot_number': number, **details}
                        ),
                        max_tokens=max_tokens,
                    ))
                else:
                    output_rows.extend(processor(client, row))
            client.close()
            client = None
        write_output(output_path, output_rows)
        manifest['output_csv']['sha256'] = _sha256(output_path.read_bytes())
        manifest['status'] = 'completed'
        manifest['completed_at'] = _utc_now()
        write_manifest_atomic(manifest_path, manifest)
    except BaseException as error:
        if output_path.exists():
            output_path.unlink()
        manifest['status'] = 'failed'
        manifest['completed_at'] = None
        manifest['output_csv']['sha256'] = None
        failure_code = getattr(error, 'failure_code', 'processing_failed')
        manifest['failure_code'] = (
            failure_code if isinstance(failure_code, str) and failure_code in FAILURE_CODES
            else 'processing_failed'
        )
        if response_diagnostics:
            manifest['response_diagnostics'] = response_diagnostics
        write_manifest_atomic(manifest_path, manifest)
        try:
            error.failure_manifest_path = manifest_path
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        if client is not None:
            client.close()
    return run_id, run_directory, len(rows), len(output_rows)


def read_input(path, limit):
    with path.open('r', encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {'snapshot_id', 'raw_text'}.issubset(reader.fieldnames):
            raise ProcessingError('Input CSV must contain snapshot_id and raw_text columns.')
        return list(row for _, row in zip(range(limit), reader))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=1, help='Maximum snapshots to process (default: 1).')
    parser.add_argument('--max-tokens', type=parse_positive_int, help='Maximum completion tokens (or use ORVIX_MAX_TOKENS).')
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('--limit must be a positive integer')
    if not INPUT_PATH.is_file():
        print('Input CSV not found: data/orvix/raw_snapshots.csv', file=sys.stderr)
        return 1
    try:
        run_id, run_directory, snapshot_count, event_count = execute_run(
            INPUT_PATH, args.limit, max_tokens=args.max_tokens
        )
    except MaxTokensError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ProcessingError as error:
        _print_failure(error)
        return 1
    except (OSError, csv.Error) as error:
        _print_failure(error)
        return 1
    except Exception as error:
        _print_failure(error)
        return 1
    print('Run {} completed: {} snapshot(s), {} event row(s) in {}'.format(
        run_id, snapshot_count, event_count, run_directory.relative_to(ROOT)
    ))
    return 0


def _print_failure(error):
    failure_code = getattr(error, 'failure_code', 'processing_failed')
    if not isinstance(failure_code, str) or failure_code not in FAILURE_CODES:
        failure_code = 'processing_failed'
    if isinstance(error, ProcessingError):
        print('Processing failed [{}]: {}'.format(failure_code, str(error)), file=sys.stderr)
    else:
        print('Processing failed [{}]: {}'.format(failure_code, type(error).__name__), file=sys.stderr)
    manifest_path = getattr(error, 'failure_manifest_path', None)
    if manifest_path is not None:
        try:
            location = Path(manifest_path).relative_to(ROOT)
        except ValueError:
            return
        print('Safe diagnostics: {}'.format(location), file=sys.stderr)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nProcessing cancelled.', file=sys.stderr)
        raise SystemExit(1)
