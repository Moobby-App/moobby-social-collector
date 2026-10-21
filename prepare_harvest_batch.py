"""Adapt Tweet Harvest CSV (+ optional OCR) to existing collection tables, v2."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import uuid

from prepare_batch import canonical_x, capture_hash, timestamp


def number(value):
    return value if type(value) is int and value > 0 else None


def prepare_harvest(rows, metadata, source_key, collector_label, ocr_models=None):
    if not re.fullmatch(r'x:[a-z0-9_]{1,15}', source_key) or not collector_label.strip():
        raise ValueError('Sumber/collector label tidak valid.')
    account = source_key.split(':')[1]
    expected = 'from:{} -filter:replies -filter:retweets'.format(account)
    if metadata.get('search_query', '').casefold() != expected.casefold():
        raise ValueError('Query run tidak cocok dengan akun dan batas cakupan pilot.')
    if metadata.get('tool') != 'tweet-harvest' or metadata.get('search_tab') != 'LATEST':
        raise ValueError('Metadata bukan run Tweet Harvest Latest.')
    start, finish = timestamp(metadata['started_at']), timestamp(metadata['finished_at'])
    records = []
    identities = {}
    for row in rows:
        post_id = row.get('id_str', '')
        if not re.fullmatch(r'\d+', post_id):
            raise ValueError('ID X harus string digit asli.')
        url, author = canonical_x(row.get('tweet_url', ''), post_id)
        if author.casefold() != account or row.get('username', '').casefold() != account:
            raise ValueError('Penulis postingan berbeda dengan akun pilot.')
        if row.get('in_reply_to_screen_name') or re.match(r'^RT\s+@', row.get('full_text', '')):
            raise ValueError('Balasan/repost masih ditemukan pada input pilot.')
        media_input = json.loads(row.get('media_json') or '[]')
        if not isinstance(media_input, list):
            raise ValueError('media_json harus array.')
        ocr_records = json.loads(row.get('ocr_results_json') or '[]')
        ocr_by_index = {}
        for item in ocr_records:
            index = item['media_index']
            if index in ocr_by_index:
                raise ValueError('Urutan OCR ganda.')
            ocr_by_index[index] = item
        media = []
        for item in media_input:
            kind = item.get('type')
            image = kind == 'photo'
            link = item.get('image_url') if image else item.get('thumbnail_url')
            if not link:
                raise ValueError('Media ada tetapi URL tidak tersedia; jangan hilangkan diam-diam.')
            entry = {
                'type': 'image' if image else 'unknown',
                'url': link, 'alt_text': item.get('alt_text'),
                'thumbnail_url': None if image else link,
                'media_id': item.get('media_id'), 'media_index': item['media_index'],
                'width': number(item.get('width')), 'height': number(item.get('height')),
                'original_media_type': kind or 'unknown',
                'url_role': 'photo' if image else 'thumbnail',
            }
            ocr = ocr_by_index.pop(item['media_index'], None)
            if ocr:
                if not image or ocr.get('media_url') != link or (ocr.get('media_id') and ocr['media_id'] != item.get('media_id')):
                    raise ValueError('OCR tidak cocok dengan gambar sumber.')
                entry['ocr'] = {
                    'status': ocr['ocr_status'], 'raw_text': ocr.get('ocr_text', ''),
                    'engine': ocr.get('ocr_engine'), 'languages': ocr.get('ocr_languages'),
                    'psm': ocr.get('ocr_psm'), 'processed_at': ocr.get('ocr_processed_at'),
                    'verification_status': 'not_reviewed', 'model_sha256': ocr_models or {},
                }
                if ocr.get('image_hash'):
                    entry['image_hash'] = ocr['image_hash']
            media.append(entry)
        if ocr_by_index:
            raise ValueError('Ada hasil OCR tanpa media yang cocok.')
        body = row.get('full_text')
        if not isinstance(body, str):
            raise ValueError('Teks caption tidak valid.')
        basis = row.get('media_extraction_basis')
        record = {
            'platform': 'x', 'external_id': post_id,
            'canonical_url': 'https://x.com/{}/status/{}'.format(account, post_id),
            'author_handle': account, 'author_display_name': None, 'title': None,
            'raw_text': body, 'text_kind': 'post_text' if body else 'none',
            'text_completeness': row.get('text_completeness') or 'unknown',
            'published_at': timestamp(row['created_at']) if row.get('created_at') else None,
            'published_at_basis': 'page_timestamp' if row.get('created_at') else 'unknown',
            'published_label': None, 'captured_at': finish,
            'media': media, 'media_capture_status': 'captured' if media or basis == 'extended_entities' else 'not_attempted',
            'position_in_run': len(records) + 1, 'is_pinned': None,
            'post_type': row.get('post_type') or 'unknown', 'related_urls': [],
            'extraction_metadata': {
                'input_format': 'tweet-harvest-csv',
                'text_extraction_method': row.get('text_extraction_method') or 'unknown',
                'media_extraction_basis': basis or 'not_available',
            },
        }
        record['capture_hash'] = capture_hash(record)
        if post_id in identities:
            if identities[post_id] != record['capture_hash']:
                raise ValueError('ID sama dengan isi berbeda dalam satu export; perlu diperiksa.')
            continue
        identities[post_id] = record['capture_hash']
        records.append(record)
    if not records:
        raise ValueError('Tidak ada records untuk disiapkan.')
    target = metadata.get('target')
    completed = metadata.get('status') == 'finished' and type(target) is int and len(records) >= target
    run = {
        'id': str(uuid.uuid4()), 'collector_label': collector_label.strip(),
        'collector_version': 'tweet-harvest/{}+moobby-adapter/2'.format(metadata.get('version', 'unknown')),
        'collection_method': 'playwright', 'started_at': start, 'finished_at': finish,
        'run_status': 'completed' if completed else 'partial',
        'stop_reason': 'target_reached' if completed else 'partial_results',
        'coverage': 'search_results_only_non_reply_non_repost', 'complete_history': False,
        'requested_count': target, 'captured_count': len(records), 'time_basis': 'collector_clock',
    }
    # Same capture prepared twice (even in another folder) retains upload identity.
    identity = {'source': source_key, 'run': {k:v for k,v in run.items() if k != 'id'},
                'records': [(r['external_id'], r['capture_hash']) for r in records]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    run['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'moobby:harvest-batch:' + digest))
    return {'schema_version': 'raw.collection.v2', 'source_key': source_key, 'run': run, 'records': records}


def prepare_file(input_file, run_info, source_key, label, output):
    from upload_collection_batch import validate_batch
    with input_file.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    models = {}
    if rows and 'ocr_results_json' in rows[0]:
        proof = json.loads(input_file.with_name('ocr_results.json').read_text(encoding='utf-8'))
        models = proof['model_sha256']
    batch = prepare_harvest(rows, json.loads(run_info.read_text()), source_key, label, models)
    schema = json.loads(Path(__file__).with_name('raw_batch_v2.schema.json').read_text())
    validate_batch(batch, schema)
    if output.exists():
        if json.loads(output.read_text()) != batch:
            raise ValueError('Output sudah ada dengan isi berbeda; pilih file baru.')
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as handle:
            json.dump(batch, handle, ensure_ascii=False, indent=2)
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--run-info', type=Path, required=True)
    parser.add_argument('--source-key', required=True)
    parser.add_argument('--collector-label', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        batch = prepare_file(args.input, args.run_info, args.source_key, args.collector_label, args.output)
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(1, 'Input/metadata tidak cocok. Batch tidak diunggah.\n')
    print('Batch siap: {} records, run {}.'.format(len(batch['records']), batch['run']['id']))
    print('Output:', args.output)


if __name__ == '__main__': main()
