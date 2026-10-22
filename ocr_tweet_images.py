"""OCR foto lampiran Tweet Harvest; caption dan CSV sumber tidak diubah."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 20 * 1024 * 1024


def cache_key(image_hash, engine, models):
    signature = {'image_hash': image_hash, 'engine': engine, 'models': models,
                 'languages': 'ind+eng', 'psm': 11, 'oem': 1, 'format': 1}
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


def read_cache(folder, image_hash, engine, models):
    if folder is None:
        return None
    key = cache_key(image_hash, engine, models)
    try:
        entry = json.loads((Path(folder) / (key + '.json')).read_text(encoding='utf-8'))
        result = entry['result']
        if entry['key'] != key or result['image_hash'] != image_hash or result['ocr_engine'] != engine:
            return None
        if result['ocr_status'] != 'completed' or not isinstance(result['ocr_text'], str):
            return None
        if result['ocr_languages'] != 'ind+eng' or result['ocr_psm'] != 11:
            return None
        if datetime.fromisoformat(result['ocr_processed_at'].replace('Z', '+00:00')).tzinfo is None:
            return None
        if entry['text_sha256'] != hashlib.sha256(result['ocr_text'].encode()).hexdigest():
            return None
        return {field: result[field] for field in (
            'image_hash', 'ocr_engine', 'ocr_languages', 'ocr_psm', 'ocr_status',
            'ocr_text', 'ocr_processed_at', 'text_detected')}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def write_cache(folder, result, models):
    if folder is None:
        return
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = cache_key(result['image_hash'], result['ocr_engine'], models)
    safe = {field: result[field] for field in (
        'image_hash', 'ocr_engine', 'ocr_languages', 'ocr_psm', 'ocr_status',
        'ocr_text', 'ocr_processed_at', 'text_detected')}
    entry = {'key': key, 'result': safe, 'text_sha256': hashlib.sha256(safe['ocr_text'].encode()).hexdigest()}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(entry, handle, ensure_ascii=False)
    try:
        os.replace(temporary, folder / (key + '.json'))
    finally:
        temporary.unlink(missing_ok=True)


def seed_cache(proof_path, folder):
    """Import verified local OCR results; never trust an unverified image hash."""
    proof_path = Path(proof_path)
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    source = Path(proof['input_csv'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != proof['input_sha256']:
        raise ValueError('CSV sumber OCR berubah; cache tidak diimpor.')
    count = 0
    for record in proof['records']:
        for result in record['images']:
            if result['ocr_status'] != 'completed':
                continue
            image = (proof_path.parent / result['local_image']).resolve()
            if proof_path.parent.resolve() not in image.parents:
                raise ValueError('Gambar OCR di luar folder hasil.')
            if hashlib.sha256(image.read_bytes()).hexdigest() != result['image_hash']:
                raise ValueError('Hash gambar OCR tidak cocok.')
            if result['ocr_languages'] != 'ind+eng' or result['ocr_psm'] != 11:
                continue
            if set(proof['model_sha256']) != {'ind', 'eng'}:
                raise ValueError('Hash model OCR tidak lengkap.')
            write_cache(folder, result, proof['model_sha256'])
            count += 1
    return count


def original_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'pbs.twimg.com' or not parts.path.startswith('/media/'):
        raise ValueError('unsupported_media_url')
    path = parts.path
    query = parse_qs(parts.query)
    suffix = Path(path).suffix.lower().lstrip('.')
    if suffix in ('jpg', 'jpeg', 'png', 'webp'):
        path = path[:-(len(suffix) + 1)]
        query['format'] = [suffix]
    query['name'] = ['orig']
    return urlunsplit(('https', 'pbs.twimg.com', path, urlencode(query, doseq=True), ''))


def download(url):
    request = Request(url, headers={'User-Agent': 'Moobby-Collector/1.0'})
    with urlopen(request, timeout=30) as response:
        if urlsplit(response.url).hostname != 'pbs.twimg.com':
            raise ValueError('unexpected_download_host')
        if response.headers.get_content_type() not in ('image/jpeg', 'image/png', 'image/webp'):
            raise ValueError('unsupported_content_type')
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('image_too_large')
    return data


def aggregate(results, basis):
    if not results:
        return 'no_images' if basis == 'extended_entities' else 'media_unknown'
    statuses = [item['ocr_status'] for item in results]
    if all(status == 'completed' for status in statuses):
        return 'completed'
    if any(status == 'completed' for status in statuses):
        return 'partial'
    return 'failed' if 'failed' in statuses else 'not_attempted'


def enrich(input_file, max_images=None, tessdata=None, cache_dir=None):
    executable = shutil.which('tesseract')
    if not executable:
        raise ValueError('Tesseract belum terpasang. Lihat docs/ocr.md.')
    tessdata = Path(tessdata or ROOT / 'data/ocr_models')
    for language in ('ind', 'eng'):
        if not (tessdata / (language + '.traineddata')).is_file():
            raise ValueError('Model ind/eng belum tersedia. Lihat docs/ocr.md.')
    version = subprocess.check_output([executable, '--version'], text=True, stderr=subprocess.STDOUT).splitlines()[0]
    model_hashes = {lang: hashlib.sha256((tessdata / (lang + '.traineddata')).read_bytes()).hexdigest() for lang in ('ind', 'eng')}
    with input_file.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if not {'id_str', 'full_text', 'media_json'}.issubset(fields):
            raise ValueError('CSV harus berasal dari collector dengan media_json.')
        if 'ocr_results_json' in fields:
            raise ValueError('Gunakan CSV sumber, bukan CSV hasil OCR.')
        rows = list(reader)
    # Parse first: invalid input must not leave a partly-created output batch.
    all_media = [json.loads(row['media_json']) for row in rows]
    if any(not isinstance(media, list) for media in all_media):
        raise ValueError('media_json harus berupa array.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = input_file.parent / ('ocr_' + stamp)
    folder.mkdir(exist_ok=False)
    images = folder / 'images'
    images.mkdir()
    cache = {}
    statistics = {'downloads': 0, 'ocr_runs': 0, 'persistent_hits': 0, 'memory_hits': 0, 'cache_write_errors': 0}
    attempted = 0
    for row, media in zip(rows, all_media):
        results = []
        for item in media:
            if item.get('type') != 'photo' or not item.get('image_url'):
                continue
            record = {'media_id': item.get('media_id'), 'media_index': item.get('media_index'),
                      'media_url': item['image_url'], 'ocr_status': 'not_attempted', 'ocr_text': '',
                      'ocr_engine': version, 'ocr_languages': 'ind+eng', 'ocr_psm': 11,
                      'ocr_verification_status': 'not_reviewed'}
            if max_images is not None and attempted >= max_images:
                results.append(record)
                continue
            attempted += 1
            try:
                url = original_url(item['image_url'])
                if url in cache:
                    record.update(cache[url])
                    statistics['memory_hits'] += 1
                else:
                    data = download(url)
                    statistics['downloads'] += 1
                    digest = hashlib.sha256(data).hexdigest()
                    image_file = images / (digest + '.img')
                    if not image_file.exists():
                        image_file.write_bytes(data)
                    text_file = images / (digest + '.txt')
                    cached = read_cache(cache_dir, digest, version, model_hashes)
                    if cached:
                        record.update(cached)
                        statistics['persistent_hits'] += 1
                        text = cached['ocr_text']
                    elif text_file.exists():
                        text = text_file.read_text(encoding='utf-8')
                    else:
                        statistics['ocr_runs'] += 1
                        process = subprocess.run([executable, str(image_file), 'stdout', '--tessdata-dir', str(tessdata), '-l', 'ind+eng', '--oem', '1', '--psm', '11'], capture_output=True, text=True, timeout=90)
                        if process.returncode:
                            raise ValueError('tesseract_failed')
                        text = process.stdout.strip()
                    text_file.write_text(text, encoding='utf-8')
                    record.update({'download_url': url, 'image_hash': digest,
                                   'local_image': str(image_file.relative_to(folder)),
                                   'ocr_text': text, 'ocr_status': 'completed',
                                   'text_detected': bool(text),
                                   'ocr_processed_at': cached['ocr_processed_at'] if cached else datetime.now(timezone.utc).isoformat()})
                    if not cached:
                        try:
                            write_cache(cache_dir, record, model_hashes)
                        except OSError:
                            statistics['cache_write_errors'] += 1
                    cache[url] = {key: value for key, value in record.items() if key not in ('media_id', 'media_index', 'media_url')}
            except Exception as error:
                record.update(ocr_status='failed', error_type=type(error).__name__, ocr_processed_at=datetime.now(timezone.utc).isoformat())
            results.append(record)
        row['ocr_status'] = aggregate(results, row.get('media_extraction_basis'))
        row['ocr_text'] = '\n\n'.join('[Gambar {}]\n{}'.format(item['media_index'], item['ocr_text']) for item in results if item['ocr_status'] == 'completed' and item['ocr_text'])
        row['ocr_results_json'] = json.dumps(results, ensure_ascii=False)
        print('{}: {} | {} gambar'.format(row['id_str'], row['ocr_status'], len(results)), flush=True)
    output = folder / (input_file.stem + '_ocr.csv')
    with output.open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + ['ocr_status', 'ocr_text', 'ocr_results_json'])
        writer.writeheader()
        writer.writerows(rows)
    records = [{'post_id': row['id_str'], 'source_url': row.get('tweet_url'), 'ocr_status': row['ocr_status'], 'images': json.loads(row['ocr_results_json'])} for row in rows]
    (folder / 'ocr_results.json').write_text(json.dumps({'input_csv': str(input_file.resolve()), 'input_sha256': hashlib.sha256(input_file.read_bytes()).hexdigest(), 'model_sha256': model_hashes, 'cache_stats': statistics, 'records': records}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OCR baru: {} | cache antarrun: {} | cache dalam run: {}'.format(statistics['ocr_runs'], statistics['persistent_hits'], statistics['memory_hits']), flush=True)
    print('CSV dengan OCR:', output)
    print('Caption asli dan CSV sumber tidak diubah. OCR belum diverifikasi manusia.')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--max-images', type=int)
    parser.add_argument('--tessdata-dir', type=Path)
    parser.add_argument('--cache-dir', type=Path, help='Optional persistent OCR cache directory.')
    args = parser.parse_args()
    if args.max_images is not None and args.max_images < 1:
        parser.error('--max-images minimal 1.')
    try:
        enrich(args.input, args.max_images, args.tessdata_dir, args.cache_dir)
    except (ValueError, OSError, csv.Error) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
