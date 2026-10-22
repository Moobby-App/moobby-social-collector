"""Prepare local Green Line JSON/OCR as retry-safe raw.collection.v2 batches."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid

from prepare_batch import canonical_x, capture_hash, timestamp
from upload_collection_batch import validate_batch

ROOT = Path(__file__).resolve().parent
SOURCE_KEY = 'x:greenline_search'


def parsed(value):
    return datetime.fromisoformat(timestamp(value).replace('Z', '+00:00'))


def build_batches(manifest, posts, proof=None):
    if manifest.get('schema_version') != 'greenline.raw.v1' or not manifest.get('collector_label', '').strip():
        raise ValueError('Manifest Green Line/label tidak valid.')
    if manifest.get('status') not in ('collected', 'partial', 'cancelled'):
        raise ValueError('Pengambilan belum selesai.')
    start, end = parsed(manifest['window_start']), parsed(manifest['window_end'])
    if manifest.get('window_mode', 'full_24h') == 'incremental':
        overlap = manifest.get('overlap_minutes')
        if type(overlap) is not int or not 1 <= overlap <= 1440:
            raise ValueError('Overlap incremental tidak valid.')
        checkpoint = parsed(manifest['checkpoint_window_end'])
        if not start < end or checkpoint >= end or (checkpoint - start).total_seconds() != overlap * 60:
            raise ValueError('Rentang incremental tidak cocok dengan checkpoint.')
    elif manifest.get('window_mode', 'full_24h') != 'full_24h' or (end - start).total_seconds() != 86400:
        raise ValueError('Window awal Green Line harus tepat 24 jam.')
    queries = {item['id']: item for item in manifest['queries']}
    if not queries or len(queries) != len(manifest['queries']):
        raise ValueError('Daftar query kosong/duplikat.')
    flat = []
    seen_ids = set()
    for post in posts:
        post_id = post['external_id']
        if post.get('platform') != 'x' or not isinstance(post_id, str) or not re.fullmatch(r'\d+', post_id) or post_id in seen_ids:
            raise ValueError('Identitas postingan tidak valid/duplikat.')
        seen_ids.add(post_id)
        if not post['snapshots']:
            raise ValueError('Snapshot kosong.')
        for snapshot in post['snapshots']:
            if snapshot['id_str'] != post_id or not start <= parsed(snapshot['published_at']) < end:
                raise ValueError('Snapshot di luar window atau ID tidak cocok.')
            flat.append(snapshot)
    if not flat or manifest['unique_posts'] != len(posts):
        raise ValueError('Jumlah postingan tidak cocok/kosong.')
    proof_rows = proof['records'] if proof else [None] * len(flat)
    if len(proof_rows) != len(flat):
        raise ValueError('Jumlah OCR tidak cocok dengan snapshot.')
    models = proof['model_sha256'] if proof else {}
    records = []
    for post, ocr_record in zip(flat, proof_rows):
        url, author = canonical_x(post['tweet_url'], post['id_str'])
        if author.casefold() != post['username'].casefold():
            raise ValueError('Penulis berbeda dari URL.')
        if ocr_record and (ocr_record['post_id'] != post['id_str'] or ocr_record['source_url'] != post['tweet_url']):
            raise ValueError('OCR berasal dari postingan/urutan berbeda.')
        ocr_by_index = {}
        for ocr in (ocr_record['images'] if ocr_record else []):
            if ocr['media_index'] in ocr_by_index:
                raise ValueError('Index OCR duplikat.')
            ocr_by_index[ocr['media_index']] = ocr
        media = []
        for item in post['media']:
            kind = item.get('type') or 'unknown'
            photo = kind == 'photo'
            link = item.get('image_url') if photo else item.get('thumbnail_url')
            if not link:
                raise ValueError('Media tanpa URL.')
            entry = {
                'type': 'image' if photo else 'unknown', 'url': link,
                'alt_text': item.get('alt_text'), 'thumbnail_url': None if photo else link,
                'media_id': item.get('media_id'), 'media_index': item['media_index'],
                'width': item.get('width') if type(item.get('width')) is int and item['width'] > 0 else None,
                'height': item.get('height') if type(item.get('height')) is int and item['height'] > 0 else None,
                'original_media_type': kind, 'url_role': 'photo' if photo else 'thumbnail',
            }
            ocr = ocr_by_index.pop(item['media_index'], None)
            if ocr:
                if not photo or ocr['media_url'] != link or (ocr.get('media_id') and ocr['media_id'] != item.get('media_id')):
                    raise ValueError('OCR tidak cocok dengan gambar sumber.')
                entry['ocr'] = {
                    'status': ocr['ocr_status'], 'raw_text': ocr.get('ocr_text', ''),
                    'engine': ocr.get('ocr_engine'), 'languages': ocr.get('ocr_languages'),
                    'psm': ocr.get('ocr_psm'), 'processed_at': ocr.get('ocr_processed_at'),
                    'verification_status': 'not_reviewed', 'model_sha256': models,
                }
                if ocr.get('image_hash'):
                    entry['image_hash'] = ocr['image_hash']
            media.append(entry)
        if ocr_by_index:
            raise ValueError('OCR tanpa media sumber.')
        matches = post['query_matches']
        if not matches or any(item['id'] not in queries or item['query'] != queries[item['id']]['query'] for item in matches):
            raise ValueError('Jejak query tidak cocok dengan manifest.')
        related = post.get('related_urls', [])
        body = post['full_text']
        record = {
            'platform': 'x', 'external_id': post['id_str'], 'canonical_url': url,
            'author_handle': post['username'], 'author_display_name': post.get('author_display_name'),
            'title': None, 'raw_text': body, 'text_kind': 'post_text' if body else 'none',
            'text_completeness': post['text_completeness'],
            'published_at': timestamp(post['published_at']), 'published_at_basis': 'page_timestamp',
            'published_label': None, 'captured_at': timestamp(post['captured_at']),
            'media': media, 'media_capture_status': 'captured' if media or post['media_extraction_basis'] == 'extended_entities' else 'not_attempted',
            'position_in_run': 1, 'is_pinned': None, 'post_type': post['post_type'],
            'related_urls': [link for link in related if link.startswith('https://')],
            'extraction_metadata': {
                'input_format': 'greenline-raw-json',
                'text_extraction_method': post['text_extraction_method'],
                'media_extraction_basis': post['media_extraction_basis'],
                'search_query_ids': sorted({item['id'] for item in matches}),
                'conversation_id': post.get('conversation_id'),
                'in_reply_to_status_id': post.get('in_reply_to_status_id'),
                'in_reply_to_screen_name': post.get('in_reply_to_screen_name'),
                'quoted_status_id': post.get('quoted_status_id'),
                'original_related_urls': related,
            },
        }
        records.append(record)
    # A run permits one observation per content identity. Preserve additional
    # versions in another batch/run, rather than silently choosing one version.
    chunks = []
    for record in sorted(records, key=lambda row: (row['external_id'], parsed(row['captured_at']))):
        if not chunks or len(chunks[-1]) == 100 or record['external_id'] in {r['external_id'] for r in chunks[-1]}:
            chunks.append([])
        chunks[-1].append(record)
    coverage_data = {
        'method': 'greenline_search_results_only',
        'window_start': manifest['window_start'], 'window_end': manifest['window_end'],
        'queries': manifest['queries'],
    }
    if 'window_mode' in manifest:
        coverage_data.update(window_mode=manifest['window_mode'],
                             checkpoint_window_end=manifest.get('checkpoint_window_end'),
                             overlap_minutes=manifest.get('overlap_minutes'))
    coverage = json.dumps(coverage_data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    batches = []
    for chunk in chunks:
        for position, record in enumerate(chunk, 1):
            record['position_in_run'] = position
            record['capture_hash'] = capture_hash(record)
        captures = [record['captured_at'] for record in chunk]
        run = {
            'collector_label': manifest['collector_label'], 'collector_version': 'moobby-greenline/1',
            'collection_method': 'playwright', 'started_at': min(captures, key=parsed),
            'finished_at': max(captures, key=parsed),
            'run_status': 'completed' if manifest['status'] == 'collected' else 'partial',
            'stop_reason': 'searches_finished' if manifest['status'] == 'collected' else 'partial_collection',
            'coverage': coverage, 'complete_history': False, 'requested_count': None,
            'captured_count': len(chunk), 'time_basis': 'item_capture_range',
        }
        identity = json.dumps({'source': SOURCE_KEY, 'run': run, 'records': chunk}, sort_keys=True, ensure_ascii=False)
        run['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        batches.append({'schema_version': 'raw.collection.v2', 'source_key': SOURCE_KEY, 'run': run, 'records': chunk})
    return batches


def prepare_session(session):
    manifest = json.loads((session / 'manifest.json').read_text(encoding='utf-8'))
    posts = json.loads((session / 'raw_posts.json').read_text(encoding='utf-8'))
    proof = None
    if manifest.get('ocr_output'):
        ocr_output = (ROOT / manifest['ocr_output']).resolve()
        if session.resolve() not in ocr_output.parents:
            raise ValueError('Lokasi OCR di luar session.')
        proof = json.loads(ocr_output.with_name('ocr_results.json').read_text(encoding='utf-8'))
        source_csv = session / 'raw_posts.csv'
        if proof['input_sha256'] != hashlib.sha256(source_csv.read_bytes()).hexdigest():
            raise ValueError('CSV sumber berubah setelah OCR.')
        # Reconstruct the CSV from raw JSON to bind OCR order and caption/media
        # to the exact snapshots, rather than accepting matching IDs alone.
        import tempfile
        from collect_greenline import export_csv
        with tempfile.TemporaryDirectory() as temp:
            reconstructed = Path(temp) / 'raw.csv'
            export_csv(posts, reconstructed)
            if reconstructed.read_bytes() != source_csv.read_bytes():
                raise ValueError('CSV sumber berbeda dari snapshot JSON.')
    batches = build_batches(manifest, posts, proof)
    schema = json.loads((ROOT / 'raw_batch_v2.schema.json').read_text(encoding='utf-8'))
    for batch in batches:
        validate_batch(batch, schema)
    output = session / 'supabase'
    files = { 'batch_{:03d}.json'.format(i): json.dumps(batch, ensure_ascii=False, indent=2) + '\n'
             for i, batch in enumerate(batches, 1) }
    upload_manifest = {'schema_version': 'collection.session.v1', 'status': 'prepared', 'batches': [
        {'source_key': SOURCE_KEY, 'path': name, 'run_id': batch['run']['id'], 'records': len(batch['records'])}
        for name, batch in zip(files, batches)]}
    files['manifest.json'] = json.dumps(upload_manifest, ensure_ascii=False, indent=2) + '\n'
    output.mkdir(exist_ok=True)
    for name, content in files.items():
        path = output / name
        if path.exists() and path.read_text(encoding='utf-8') != content:
            raise ValueError('Output berbeda sudah ada; jangan menimpa batch untuk retry.')
    for name, content in files.items():
        path = output / name
        if not path.exists():
            with path.open('x', encoding='utf-8') as stream:
                stream.write(content)
    return output / 'manifest.json', batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args()
    try:
        output, batches = prepare_session(args.session.resolve())
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, 'Preparation ditolak: {}\n'.format(error))
    print('Siap: {} batch, {} snapshot.'.format(len(batches), sum(len(b['records']) for b in batches)))
    print('Manifest:', output)
    print('Belum ada upload. Daftarkan sumber melalui db/005_greenline_search_source.sql dan pastikan migration 004 tersedia.')


if __name__ == '__main__':
    main()
