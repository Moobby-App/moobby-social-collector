"""OCR foto lampiran Tweet Harvest; caption dan CSV sumber tidak diubah."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 20 * 1024 * 1024


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


def enrich(input_file, max_images=None, tessdata=None):
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
                else:
                    data = download(url)
                    digest = hashlib.sha256(data).hexdigest()
                    image_file = images / (digest + '.img')
                    if not image_file.exists():
                        image_file.write_bytes(data)
                    text_file = images / (digest + '.txt')
                    if text_file.exists():
                        text = text_file.read_text(encoding='utf-8')
                    else:
                        process = subprocess.run([executable, str(image_file), 'stdout', '--tessdata-dir', str(tessdata), '-l', 'ind+eng', '--oem', '1', '--psm', '11'], capture_output=True, text=True, timeout=90)
                        if process.returncode:
                            raise ValueError('tesseract_failed')
                        text = process.stdout.strip()
                        text_file.write_text(text, encoding='utf-8')
                    record.update({'download_url': url, 'image_hash': digest,
                                   'local_image': str(image_file.relative_to(folder)),
                                   'ocr_text': text, 'ocr_status': 'completed',
                                   'text_detected': bool(text),
                                   'ocr_processed_at': datetime.now(timezone.utc).isoformat()})
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
    (folder / 'ocr_results.json').write_text(json.dumps({'input_csv': str(input_file.resolve()), 'input_sha256': hashlib.sha256(input_file.read_bytes()).hexdigest(), 'model_sha256': model_hashes, 'records': records}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('CSV dengan OCR:', output)
    print('Caption asli dan CSV sumber tidak diubah. OCR belum diverifikasi manusia.')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--max-images', type=int)
    parser.add_argument('--tessdata-dir', type=Path)
    args = parser.parse_args()
    if args.max_images is not None and args.max_images < 1:
        parser.error('--max-images minimal 1.')
    try:
        enrich(args.input, args.max_images, args.tessdata_dir)
    except (ValueError, OSError, csv.Error) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
