"""Upload an existing raw.collection.v1 batch atomically to the collection schema."""
import argparse
import getpass
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from prepare_batch import capture_hash, timestamp


class BatchError(ValueError):
    pass


def check_shape(value, spec, path='batch'):
    """Validate the closed types/keys used by our contract, not a general JSON Schema engine."""
    if 'const' in spec and value != spec['const']:
        raise BatchError(path + ': versi/nilai kontrak tidak sesuai.')
    types = spec.get('type', [])
    if isinstance(types, str):
        types = [types]
    matches = {'null': value is None, 'boolean': type(value) is bool,
               'integer': type(value) is int, 'string': isinstance(value, str),
               'array': isinstance(value, list), 'object': isinstance(value, dict)}
    if types and not any(matches[k] for k in types):
        raise BatchError(path + ': tipe data tidak sesuai.')
    if value is None:
        return
    if 'enum' in spec and value not in spec['enum']:
        raise BatchError(path + ': nilai tidak termasuk pilihan kontrak.')
    if isinstance(value, dict):
        props = spec.get('properties', {})
        if not set(spec.get('required', [])).issubset(value):
            raise BatchError(path + ': field wajib belum lengkap.')
        if spec.get('additionalProperties') is False and set(value) - set(props):
            raise BatchError(path + ': ada field tambahan di luar kontrak raw.')
        for key in props:
            if key in value:
                check_shape(value[key], props[key], path + '.' + key)
    elif isinstance(value, list):
        if len(value) > spec.get('maxItems', float('inf')):
            raise BatchError(path + ': jumlah item melewati batas.')
        for index, item in enumerate(value):
            check_shape(item, spec['items'], path + '[{}]'.format(index))
    elif isinstance(value, str):
        if len(value) < spec.get('minLength', 0) or ('pattern' in spec and not re.search(spec['pattern'], value)):
            raise BatchError(path + ': format string tidak sesuai.')
        fmt = spec.get('format')
        try:
            if fmt == 'date-time':
                timestamp(value)
            elif fmt == 'uuid':
                uuid.UUID(value)
            elif fmt == 'uri':
                url = urlsplit(value)
                if url.scheme != 'https' or not url.hostname or url.username or url.password:
                    raise ValueError()
        except ValueError:
            raise BatchError(path + ': format waktu/UUID/URL tidak valid.') from None
    elif type(value) is int:
        if value < spec.get('minimum', float('-inf')) or value > spec.get('maximum', float('inf')):
            raise BatchError(path + ': angka di luar batas.')


def validate_batch(batch, schema):
    check_shape(batch, schema)
    run, records = batch['run'], batch['records']
    if not records or run['captured_count'] != len(records):
        raise BatchError('Jumlah records harus tidak kosong dan sama dengan captured_count.')
    if run['finished_at'] is None or run['run_status'] == 'running':
        raise BatchError('Uploader ini menerima batch pengambilan yang sudah selesai.')
    parse = lambda s: datetime.fromisoformat(timestamp(s).replace('Z', '+00:00'))
    start, finish = parse(run['started_at']), parse(run['finished_at'])
    if finish < start:
        raise BatchError('Rentang waktu run terbalik.')
    if run['stop_reason'] == 'target_reached' and run['requested_count'] is not None and len(records) < run['requested_count']:
        raise BatchError('Keterangan target_reached tidak sesuai jumlah records.')
    if ':' not in batch['source_key']:
        raise BatchError('source_key harus mencantumkan platform.')
    platform = batch['source_key'].split(':', 1)[0]
    seen = set()
    for row in records:
        identity = (row['platform'], row['external_id'])
        if row['platform'] != platform or identity in seen:
            raise BatchError('Platform sumber tidak cocok atau ada identitas berulang dalam batch.')
        seen.add(identity)
        if not start <= parse(row['captured_at']) <= finish:
            raise BatchError('Waktu pengamatan berada di luar rentang run.')
        if (row['published_at'] is None) != (row['published_at_basis'] == 'unknown'):
            raise BatchError('published_at dan asal tanggal tidak konsisten.')
        if row['media_capture_status'] != 'captured' and row['media']:
            raise BatchError('Media belum diambil tetapi daftar media tidak kosong.')
        if batch['schema_version'] == 'raw.collection.v2':
            for media in row['media']:
                ocr = media.get('ocr')
                if ocr and ocr['status'] == 'completed':
                    if not media.get('image_hash') or not ocr['engine'] or not ocr['processed_at'] or set(ocr['model_sha256']) != {'ind', 'eng'}:
                        raise BatchError('OCR selesai harus mempunyai hash gambar, engine, waktu, dan model.')
                if ocr and media['type'] != 'image':
                    raise BatchError('OCR hanya didukung untuk foto.')
        if row['capture_hash'] != capture_hash(row):
            raise BatchError('Hash tidak sesuai isi snapshot; buat ulang batch dari raw collector.')


def same_run(existing, run, source_id):
    # Database timestamptz values may have a different timezone representation.
    expected = [str(source_id), run['collector_label'], run['collector_version'], run['collection_method'],
                timestamp(run['started_at']), timestamp(run['finished_at']), run['run_status'],
                run['stop_reason'], run['coverage'], run['complete_history'], run['requested_count'],
                run['captured_count'], run['time_basis']]
    actual = list(existing)
    actual[0] = str(actual[0])
    for i in (4, 5):
        actual[i] = timestamp(actual[i].isoformat()) if actual[i] is not None else None
    return actual == expected


def observation_signature(platform, external_id, snapshot_hash, captured_at, label, position, pinned, post_type, urls):
    captured_at = captured_at.isoformat() if isinstance(captured_at, datetime) else captured_at
    return (platform, external_id, snapshot_hash, timestamp(captured_at), label, position, pinned,
            post_type, json.dumps(urls, ensure_ascii=False, sort_keys=True))


def upload(conn, batch):
    """Caller owns commit/rollback. All SQL values are bound parameters."""
    run = batch['run']
    is_v2 = batch['schema_version'] == 'raw.collection.v2'
    if is_v2:
        column = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='collection' AND table_name='raw_snapshots' AND column_name='extraction_metadata'").fetchone()
        if not column:
            raise BatchError('Jalankan db/004_snapshot_extraction_metadata.sql dahulu pada Supabase SQL Editor.')
    # Concurrent retries of the same UUID serialize; a collision only serializes unrelated work.
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (run['id'],))
    source = conn.execute(
        'SELECT id, platform, enabled FROM collection.sources WHERE source_key = %s',
        (batch['source_key'],),
    ).fetchone()
    if source is None or not source[2]:
        raise BatchError('Sumber belum terdaftar atau masih nonaktif. Tidak ada data disimpan.')
    source_id = source[0]
    if any(row['platform'] != source[1] for row in batch['records']):
        raise BatchError('Platform records berbeda dari sumber database.')
    existing = conn.execute('''
        SELECT source_id, collector_label, collector_version, collection_method,
               started_at, finished_at, run_status, stop_reason, coverage, complete_history,
               requested_count, captured_count, time_basis
        FROM collection.collection_runs WHERE id = %s
    ''', (run['id'],)).fetchone()
    if existing is not None:
        if not same_run(existing, run, source_id):
            raise BatchError('Run ID sudah dipakai untuk metadata berbeda; batch tidak ditimpa.')
        rows = conn.execute('''
            SELECT c.platform, c.external_id, s.capture_hash, o.captured_at,
                   o.published_label, o.position_in_run, o.is_pinned, o.post_type, o.related_urls
            FROM collection.content_observations o
            JOIN collection.raw_contents c ON c.id = o.raw_content_id
            JOIN collection.raw_snapshots s ON s.id = o.snapshot_id
            WHERE o.run_id = %s
        ''', (run['id'],)).fetchall()
        stored = {observation_signature(*row) for row in rows}
        expected = {observation_signature(row['platform'], row['external_id'], row['capture_hash'],
                    row['captured_at'], row['published_label'], row['position_in_run'], row['is_pinned'],
                    row['post_type'], row['related_urls']) for row in batch['records']}
        if stored != expected or len(rows) != len(batch['records']):
            raise BatchError('Run ID ditemukan tetapi observasinya berbeda/tidak lengkap; tidak ditimpa.')
        return {'already_uploaded': True, 'contents': 0, 'snapshots': 0, 'observations': 0}

    conn.execute('''
        INSERT INTO collection.collection_runs
          (id, source_id, collector_label, collector_version, collection_method, started_at,
           finished_at, run_status, stop_reason, coverage, complete_history,
           requested_count, captured_count, time_basis)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ''', (run['id'], source_id, run['collector_label'], run['collector_version'],
          run['collection_method'], run['started_at'], run['finished_at'], run['run_status'],
          run['stop_reason'], run['coverage'], run['complete_history'], run['requested_count'],
          run['captured_count'], run['time_basis']))
    result = {'already_uploaded': False, 'contents': 0, 'snapshots': 0, 'observations': 0}
    # Stable ordering limits deadlocks between two members uploading overlapping posts.
    for row in sorted(batch['records'], key=lambda r: (r['platform'], r['external_id'])):
        created = conn.execute('''
            INSERT INTO collection.raw_contents
              (platform, external_id, canonical_url, first_seen_at, last_seen_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (platform, external_id) DO NOTHING RETURNING id
        ''', (row['platform'], row['external_id'], row['canonical_url'], row['captured_at'], row['captured_at'])).fetchone()
        if created is not None:
            content_id = created[0]
            result['contents'] += 1
        else:
            content_id = conn.execute('''
                SELECT id FROM collection.raw_contents WHERE platform=%s AND external_id=%s
            ''', (row['platform'], row['external_id'])).fetchone()[0]
            conn.execute('''
                UPDATE collection.raw_contents
                SET first_seen_at = LEAST(first_seen_at, %s::timestamptz),
                    last_seen_at = GREATEST(last_seen_at, %s::timestamptz),
                    canonical_url = CASE WHEN %s::timestamptz >= last_seen_at THEN %s ELSE canonical_url END
                WHERE id=%s
            ''', (row['captured_at'], row['captured_at'], row['captured_at'], row['canonical_url'], content_id))
        snapshot_sql = '''
            INSERT INTO collection.raw_snapshots
              (raw_content_id, capture_hash, author_handle, author_display_name, title,
               raw_text, text_kind, text_completeness, published_at, published_at_basis,
               media, media_capture_status, captured_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (raw_content_id, capture_hash) DO NOTHING RETURNING id
        '''
        snapshot_params = (content_id, row['capture_hash'], row['author_handle'], row['author_display_name'],
              row['title'], row['raw_text'], row['text_kind'], row['text_completeness'],
              row['published_at'], row['published_at_basis'], json.dumps(row['media']),
              row['media_capture_status'], row['captured_at'])
        if is_v2:
            snapshot_sql = snapshot_sql.replace('media, media_capture_status, captured_at)', 'media, media_capture_status, captured_at, extraction_metadata)').replace('%s::jsonb,%s,%s)', '%s::jsonb,%s,%s,%s::jsonb)')
            snapshot_params += (json.dumps(row['extraction_metadata']),)
        snapshot = conn.execute(snapshot_sql, snapshot_params).fetchone()
        if snapshot is not None:
            snapshot_id = snapshot[0]
            result['snapshots'] += 1
        else:
            snapshot_id = conn.execute('''
                SELECT id FROM collection.raw_snapshots WHERE raw_content_id=%s AND capture_hash=%s
            ''', (content_id, row['capture_hash'])).fetchone()[0]
        conn.execute('''
            INSERT INTO collection.content_observations
              (run_id, raw_content_id, snapshot_id, captured_at, published_label,
               position_in_run, is_pinned, post_type, related_urls)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ''', (run['id'], content_id, snapshot_id, row['captured_at'], row['published_label'],
              row['position_in_run'], row['is_pinned'], row['post_type'], json.dumps(row['related_urls'])))
        result['observations'] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', action='append', type=Path)
    inputs.add_argument('--manifest', type=Path)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.manifest:
            manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
            paths = [args.manifest.parent / item['path'] for item in manifest['batches']]
        else:
            paths = args.input
        batches = []
        for filename in paths:
            batch = json.loads(filename.read_text(encoding='utf-8'))
            schema_name = {'raw.collection.v1':'raw_batch.schema.json', 'raw.collection.v2':'raw_batch_v2.schema.json'}.get(batch.get('schema_version'))
            if not schema_name:
                raise BatchError('Versi batch tidak dikenali.')
            schema = json.loads(Path(__file__).with_name(schema_name).read_text(encoding='utf-8'))
            validate_batch(batch, schema)
            batches.append(batch)
        if not batches or len({batch['run']['id'] for batch in batches}) != len(batches):
            raise BatchError('Daftar batch kosong atau run ID berulang.')
    except BatchError as error:
        print('Batch ditolak: {}'.format(error))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('File batch atau kontrak schema tidak dapat dibaca. Gunakan file output prepare_batch.py.')
        return 1
    print('Lolos validasi: {} batch; {} records.'.format(len(batches), sum(len(b['records']) for b in batches)))
    for batch in batches:
        print('Sumber: {} | Run: {}'.format(batch['source_key'], batch['run']['id']))
    if args.validate_only:
        print('Validasi lokal selesai. Tidak ada koneksi atau upload.')
        return 0
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        print('Pasang psycopg pada venv yang sama dengan pengujian koneksi.')
        return 1
    print('Akan menyimpan batch raw ini ke schema collection, tanpa kurasi.')
    uri = getpass.getpass('URI Session pooler (disembunyikan): ').strip()
    if not uri.startswith(('postgresql://', 'postgres://')):
        print('Gunakan URI PostgreSQL dari panel Connect.')
        return 1
    uri = re.sub(r'\[(?:YOUR[-_ ]?PASSWORD|PASSWORD)\]', '', uri, flags=re.I)
    try:
        params = conninfo_to_dict(uri)
    except psycopg.Error:
        print('URI tidak dapat dibaca.')
        return 1
    if not params.get('host', '').lower().endswith(('.supabase.com', '.supabase.co')):
        print('Host belum dikenali sebagai Supabase.')
        return 1
    if not params.get('password'):
        params['password'] = getpass.getpass('Database password (disembunyikan): ')
    if not params.get('password'):
        print('Password kosong; upload dibatalkan.')
        return 1
    params.update(sslmode='require', connect_timeout=15)
    try:
        with psycopg.connect(**params, prepare_threshold=None) as conn:
            conn.execute("SET LOCAL statement_timeout = '20s'")
            conn.execute("SET LOCAL lock_timeout = '15s'")
            results = [(b['source_key'], upload(conn, b)) for b in sorted(batches, key=lambda b: b['source_key'])]
        # Reaching here means the connection context committed successfully.
    except BatchError as error:
        print('Upload dibatalkan dan transaksi di-rollback: {}'.format(error))
        return 1
    except psycopg.Error as error:
        print('Upload belum dikonfirmasi: {} | SQLSTATE: {}'.format(type(error).__name__, error.sqlstate or 'tidak tersedia'))
        print('Koneksi terputus saat commit bisa membuat hasil belum pasti. Ulangi file batch yang SAMA agar aman dari duplikasi.')
        return 1
    for source_key, result in results:
        print('Sumber:', source_key)
        if result['already_uploaded']:
            print('Batch sudah tersimpan identik; tidak ada baris baru.')
        else:
            print('Upload berhasil: {} konten baru; {} snapshot baru; {} observasi.'.format(result['contents'], result['snapshots'], result['observations']))
    print('Tidak ada kurasi atau verifikasi berita yang dijalankan.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nPengunggahan dihentikan. Bila hasil commit belum pasti, ulangi batch yang sama.')
        sys.exit(1)
