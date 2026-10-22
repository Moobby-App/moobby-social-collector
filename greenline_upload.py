"""Local durable upload queue. A batch is acknowledged only after database commit."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from upload_collection_batch import BatchError, upload, validate_batch

ROOT = Path(__file__).resolve().parent
CONNECTION = ROOT / 'data/automation/greenline/supabase_connection.json'


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def connection_params(path=CONNECTION):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('Credential belum tersedia atau izin file harus 0600.')
    data = json.loads(path.read_text(encoding='utf-8'))
    allowed = {'host', 'port', 'dbname', 'user', 'password'}
    if set(data) - allowed or not all(isinstance(data.get(key), str) and data[key] for key in ('host', 'dbname', 'user', 'password')):
        raise ValueError('Format credential lokal tidak valid.')
    if not data['host'].lower().endswith(('.supabase.com', '.supabase.co')):
        raise ValueError('Host bukan Supabase.')
    return dict(data, sslmode='require', connect_timeout=15)


def read_batches(manifest_path):
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    batches = []
    fingerprint = hashlib.sha256(manifest_path.read_bytes())
    for item in data['batches']:
        path = (manifest_path.parent / item['path']).resolve()
        if path.parent != manifest_path.parent.resolve():
            raise ValueError('Path batch keluar dari folder manifest.')
        raw = path.read_bytes()
        fingerprint.update(item['path'].encode())
        fingerprint.update(raw)
        batch = json.loads(raw)
        if batch.get('schema_version') != 'raw.collection.v2' or batch.get('source_key') != 'x:greenline_search':
            raise ValueError('Antrean ini hanya untuk batch Green Line v2.')
        schema = json.loads((ROOT / 'raw_batch_v2.schema.json').read_text(encoding='utf-8'))
        validate_batch(batch, schema)
        if item.get('run_id') != batch['run']['id']:
            raise ValueError('Run ID pada manifest berbeda.')
        batches.append(batch)
    if not batches or len({b['run']['id'] for b in batches}) != len(batches):
        raise ValueError('Batch kosong atau Run ID duplikat.')
    return batches, fingerprint.hexdigest()


def commit_batches(batches, params):
    import psycopg
    with psycopg.connect(**params, prepare_threshold=None) as conn:
        conn.execute("SET LOCAL statement_timeout = '20s'")
        conn.execute("SET LOCAL lock_timeout = '15s'")
        results = [upload(conn, batch) for batch in batches]
    # The connection context has committed before this function returns.
    return results


def process_queue(sessions=None, connection_file=None, limit=5):
    sessions = Path(sessions or ROOT / 'data/greenline_sessions')
    connection_file = Path(connection_file or CONNECTION)
    summary = {'status': 'idle', 'uploaded_manifests': 0, 'pending_manifests': 0}
    candidates = []
    for path in sorted(sessions.glob('*/supabase/manifest.json')):
        try:
            batches, fingerprint = read_batches(path)
            receipt_path = path.with_name('upload_status.json')
            receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
            if receipt.get('status') == 'uploaded':
                if receipt.get('fingerprint') != fingerprint:
                    raise ValueError('Batch yang sudah diunggah berubah.')
                continue
            candidates.append((path, batches, fingerprint, receipt))
        except (ValueError, OSError, KeyError, TypeError) as error:
            # Do not put arbitrary error strings or connection values in logs.
            summary.update(status='invalid_batch', error_type=type(error).__name__)
            return summary
    summary['pending_manifests'] = len(candidates)
    if not candidates:
        return summary
    try:
        params = connection_params(connection_file)
    except (ValueError, OSError, TypeError):
        summary['status'] = 'setup_required'
        return summary
    for path, batches, fingerprint, previous in candidates[:limit]:
        receipt = {'status': 'pending', 'fingerprint': fingerprint,
                   'run_ids': [batch['run']['id'] for batch in batches],
                   'attempts': previous.get('attempts', 0) + 1,
                   'last_attempt_at': datetime.now(timezone.utc).isoformat()}
        save_private(path.with_name('upload_status.json'), receipt)
        try:
            results = commit_batches(batches, params)
        except Exception as error:
            # Connection/SQL exceptions may contain passwords or raw SQL values.
            # Retain only the class and SQLSTATE; retry the SAME files next tick.
            receipt.update(status='failed', error_type=type(error).__name__)
            code = getattr(error, 'sqlstate', None)
            if isinstance(code, str) and len(code) == 5 and code.isalnum():
                receipt['sqlstate'] = code
            save_private(path.with_name('upload_status.json'), receipt)
            summary.update(status='retry_pending', error_type=type(error).__name__)
            return summary
        receipt.update(status='uploaded', committed_at=datetime.now(timezone.utc).isoformat(), results=results)
        save_private(path.with_name('upload_status.json'), receipt)
        summary['uploaded_manifests'] += 1
        summary['pending_manifests'] -= 1
    summary['status'] = 'uploaded' if not summary['pending_manifests'] else 'pending'
    return summary
