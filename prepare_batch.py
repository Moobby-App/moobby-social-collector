"""Prepare a collection-only batch offline. No browser, credentials, or cloud writes."""
import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA_VERSION = 'raw.collection.v1'
SNAPSHOT_FIELDS = (
    'author_handle', 'author_display_name', 'title', 'raw_text', 'text_kind',
    'text_completeness', 'published_at', 'published_at_basis',
    'media', 'media_capture_status',
)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp wajib string RFC3339 dengan timezone.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Timestamp tidak valid.') from None
    if parsed.tzinfo is None or 'T' not in value:
        raise ValueError('Timestamp harus memuat waktu dan timezone.')
    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def capture_hash(record):
    # Exact capture, not a relevance score, event fingerprint, or truth assessment.
    snapshot = {key: record[key] for key in SNAPSHOT_FIELDS}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def canonical_x(url, expected_id=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('x.com', 'www.x.com') or parsed.username or parsed.password:
        raise ValueError('URL postingan harus HTTPS x.com tanpa kredensial.')
    match = re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/(?:photo|video)/\d+)?/?', parsed.path)
    if not match:
        raise ValueError('URL postingan X tidak dikenali.')
    author, post_id = match.groups()
    if expected_id is not None and post_id != expected_id:
        raise ValueError('ID postingan tidak sesuai URL.')
    return 'https://x.com/{}/status/{}'.format(author, post_id), author


def prepare(data, source_key, collector_label, run_id=None):
    if not re.fullmatch(r'x:[a-z0-9_]{1,15}', source_key):
        raise ValueError('Adapter existing saat ini hanya menerima source_key x:username lowercase.')
    if not isinstance(collector_label, str) or not collector_label.strip():
        raise ValueError('collector_label wajib diisi.')
    if not isinstance(data, dict) or not isinstance(data.get('posts'), list) or not data['posts']:
        raise ValueError('Input harus JSON collector dengan daftar posts yang tidak kosong.')
    profile = urlsplit(data.get('profile_url', ''))
    if profile.scheme != 'https' or profile.hostname != 'x.com' or profile.path.strip('/').lower() != source_key.split(':', 1)[1]:
        raise ValueError('source_key tidak cocok dengan profile_url batch.')
    records = []
    seen = set()
    for position, post in enumerate(data['posts'], 1):
        if not isinstance(post, dict):
            raise ValueError('Setiap post harus object.')
        if any(key in post for key in ('matched_events', 'curation_reason', 'relevance_status', 'category')):
            raise ValueError('Gunakan file posts mentah, bukan file curated.')
        post_id = post.get('id')
        if not isinstance(post_id, str) or not re.fullmatch(r'\d+', post_id):
            raise ValueError('ID X harus string digit, bukan number.')
        if post_id in seen:
            raise ValueError('Batch memuat ID berulang; perbaiki collector sebelum import.')
        seen.add(post_id)
        url, author = canonical_x(post.get('source_url', ''), post_id)
        raw_text = post.get('raw_card_text')
        if not isinstance(raw_text, str):
            raise ValueError('raw_card_text harus string; boleh kosong untuk media-only.')
        published = None
        if post.get('date_basis') == 'dom_datetime' and post.get('published_at'):
            published = timestamp(post['published_at'])
        record = {
            'platform': 'x', 'external_id': post_id, 'canonical_url': url,
            'author_handle': author, 'author_display_name': None, 'title': None,
            'raw_text': raw_text, 'text_kind': 'card_text' if raw_text else 'none',
            'text_completeness': 'truncated' if re.search(r'show more|tampilkan lebih banyak', raw_text, re.I) else 'unknown',
            'published_at': published,
            'published_at_basis': 'dom_datetime' if published else 'unknown',
            'published_label': post.get('published_label'),
            'captured_at': timestamp(post.get('collected_at')),
            'media': [], 'media_capture_status': 'not_attempted',
            'position_in_run': position,
            'is_pinned': True if any(line.strip().casefold() in ('disematkan', 'pinned') for line in raw_text.splitlines()) else None,
            'post_type': 'unknown', 'related_urls': [],
        }
        record['capture_hash'] = capture_hash(record)
        records.append(record)
    times = [record['captured_at'] for record in records]
    # Compare parsed datetimes, not variable-length ISO fractional strings.
    ordered = sorted(times, key=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')))
    reason = data.get('stop_reason')
    target = data.get('target_count')
    batch = {
        'schema_version': SCHEMA_VERSION,
        'source_key': source_key,
        'run': {
            'id': str(uuid.UUID(run_id)) if run_id else str(uuid.uuid4()),
            'collector_label': collector_label.strip(),
            'collector_version': 'legacy-x-json-adapter/1.0',
            'collection_method': 'playwright',
            'started_at': ordered[0], 'finished_at': ordered[-1],
            'run_status': 'completed' if reason == 'target_reached' else 'unknown',
            'stop_reason': reason if isinstance(reason, str) else None,
            'coverage': 'loaded_items_only', 'complete_history': False,
            'requested_count': target if type(target) is int and 1 <= target <= 100 else None,
            'captured_count': len(records), 'time_basis': 'item_capture_range',
        },
        'records': records,
    }
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--source-key', required=True)
    parser.add_argument('--collector-label', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Output tidak boleh menimpa file input.')
    if args.output.exists():
        parser.error('Output sudah ada; pilih nama baru agar batch/run UUID lama tetap utuh.')
    try:
        data = json.loads(args.input.read_text(encoding='utf-8'))
        batch = prepare(data, args.source_key, args.collector_label)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(batch, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (ValueError, OSError) as error:
        # Do not print arbitrary source/credential values.
        parser.exit(1, 'Batch tidak dibuat. {}\n'.format(type(error).__name__))
    print('Batch valid untuk adapter: {} records; file asli tidak diubah.'.format(len(batch['records'])))
    print('Belum ada upload atau perubahan database cloud.')


if __name__ == '__main__':
    main()
