"""Uji Tweet Harvest terpisah; token hanya dimasukkan pada prompt tool, bukan argumen."""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from prepare_tweet_harvest import prepare_entry

ROOT = Path(__file__).resolve().parent
EXPERIMENT = ROOT / 'experiments' / 'tweet_harvest'
TOOL = EXPERIMENT / 'node_modules' / 'tweet-harvest' / 'dist' / 'bin.js'
VERSION = '2.7.1'


def command(node, account, limit):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', account):
        raise ValueError('Username X tidak valid.')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Limit harus antara 1 dan 100.')
    query = 'from:{} -filter:replies -filter:retweets'.format(account)
    return [node, str(TOOL.with_name('moobby-bin.js')), '--search-keyword', query,
            '--limit', str(limit), '--tab', 'LATEST', '--export-format', 'csv',
            '--delay', '3', '--output-filename', account.lower() + '_trial']


def summary(csv_file):
    with csv_file.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'id_str', 'full_text', 'created_at'}.issubset(reader.fieldnames or []):
            raise ValueError('Header CSV tidak sesuai format Tweet Harvest yang diharapkan.')
        fields = reader.fieldnames or []
        rows = list(reader)
    ids = [row.get('id_str', '') for row in rows if row.get('id_str')]
    return {
        'rows': len(rows), 'unique_ids': len(set(ids)),
        'duplicate_ids': len(ids) - len(set(ids)),
        'rows_with_text': sum(bool(row.get('full_text')) for row in rows),
        'rows_with_timestamp': sum(bool(row.get('created_at')) for row in rows),
        'rows_with_image_url': sum(bool(row.get('image_url')) for row in rows),
        'reply_markers': sum(bool((row.get('in_reply_to_screen_name') or '').strip()) for row in rows) if 'in_reply_to_screen_name' in fields else None,
        'rows_with_note_tweet': sum(row.get('text_extraction_method') == 'note_tweet' for row in rows),
        'rows_with_legacy_fallback': sum(row.get('text_extraction_method') == 'legacy.full_text' for row in rows),
        'total_images': sum(int(row.get('image_count') or 0) for row in rows),
        'posts_with_multiple_images': sum(int(row.get('image_count') or 0) > 1 for row in rows),
        'repost_text_markers': sum(bool(re.match(r'^RT\s+@', row.get('full_text', ''))) for row in rows),
    }


def runtime():
    node = shutil.which('node')
    if not node:
        raise ValueError('Node.js belum ditemukan di PATH terminal VS Code.')
    if not TOOL.is_file():
        raise ValueError('Tweet Harvest belum terpasang di experiments/tweet_harvest.')
    pkg = json.loads(TOOL.parent.parent.joinpath('package.json').read_text())
    if pkg.get('version') != VERSION:
        raise ValueError('Versi Tweet Harvest berbeda dari versi uji yang ditetapkan.')
    prepare_entry(EXPERIMENT)
    env = os.environ.copy()
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(EXPERIMENT / '.browsers')
    env['PLAYWRIGHT_SKIP_BROWSER_GC'] = '1'
    env['HEADLESS_MODE'] = 'false'
    return node, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', default='CommuterLine')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--install-browser', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--result-manifest', type=Path, help='Metadata hasil untuk orchestrator lokal.')
    parser.add_argument('--with-ocr', action='store_true', help='Baca teks seluruh foto setelah collection selesai.')
    parser.add_argument('--use-saved-session', action='store_true', help='Gunakan session X lokal tanpa prompt token.')
    args = parser.parse_args()
    try:
        node, env = runtime()
        account = args.account.lstrip('@')
        cmd = command(node, account, args.limit)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    cli = EXPERIMENT / 'node_modules' / 'playwright' / 'cli.js'
    if args.install_browser:
        if not cli.is_file():
            parser.exit(1, 'CLI Playwright Node belum tersedia; periksa instalasi npm.\n')
        return subprocess.call([node, str(cli), 'install', 'chromium'], cwd=str(EXPERIMENT), env=env)
    if args.check:
        print('Node.js:', subprocess.check_output([node, '--version'], text=True).strip())
        print('Tweet Harvest:', VERSION)
        probe = 'const fs=require("fs");const p=require("playwright");process.stdout.write(fs.existsSync(p.chromium.executablePath())?"ready":"missing");'
        status = subprocess.check_output([node, '-e', probe], cwd=str(EXPERIMENT), env=env, text=True).strip()
        print('Chromium khusus uji:', status)
        print('Tidak ada login, scraping, atau upload dalam pemeriksaan ini.')
        return 0 if status == 'ready' else 1

    if args.use_saved_session:
        session = ROOT / 'playwright/.auth/x_reused_session.json'
        if not session.is_file():
            parser.exit(1, 'Session X lokal belum tersedia.\n')
        env['MOOBBY_X_SESSION_FILE'] = str(session)

    # The stock CLI uses a masked password prompt when --token is omitted.
    # Never add a token argument. Saved session is opt-in and read inside Node.
    if not args.use_saved_session and not sys.stdin.isatty():
        parser.exit(1, 'Jalankan dari terminal interaktif VS Code agar prompt token bekerja.\n')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = ROOT / 'data' / 'tweet_harvest' / (account.lower() + '_' + stamp)
    folder.mkdir(parents=True, exist_ok=False)
    query = cmd[cmd.index('--search-keyword') + 1]
    metadata = {'tool': 'tweet-harvest', 'version': VERSION, 'search_query': query,
                'text_adapter': 'note_tweet_first_v1',
                'media_adapter': 'extended_entities_media_v1',
                'requested_post_types': 'non_reply_non_repost',
                'target': args.limit, 'search_tab': 'LATEST', 'coverage': 'search_results_only',
                'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
    print('Uji Tweet Harvest {} | query {} | target {}'.format(VERSION, query, args.limit), flush=True)
    print('Memakai session X lokal.' if args.use_saved_session else 'Masukkan auth_token pada prompt tool; pilih CSV.', flush=True)
    print('Hasil tersimpan terpisah di:', folder.relative_to(ROOT), flush=True)
    try:
        result = subprocess.run(cmd, cwd=str(folder), env=env, timeout=600)
        metadata['exit_code'] = result.returncode
        metadata['status'] = 'finished' if result.returncode == 0 else 'tool_error'
    except subprocess.TimeoutExpired:
        metadata['status'] = 'timeout'
        print('Uji dihentikan setelah 10 menit. Periksa hasil parsial jika ada.')
    except KeyboardInterrupt:
        metadata['status'] = 'cancelled'
        print('\nUji dihentikan pengguna.')
    metadata['finished_at'] = datetime.now(timezone.utc).isoformat()
    files = list(folder.joinpath('tweets-data').glob('*.csv'))
    if files:
        try:
            metadata['csv_path'] = str(files[0].relative_to(ROOT))
            stats = summary(files[0])
            metadata['csv_summary'] = stats
            print('CSV:', files[0].relative_to(ROOT))
            print('Baris: {rows}; ID unik: {unique_ids}; duplikat ID: {duplicate_ids}.'.format(**stats))
            print('Ada teks: {rows_with_text}; timestamp: {rows_with_timestamp}; URL gambar: {rows_with_image_url}.'.format(**stats))
            print('Balasan terdeteksi: {}.'.format(stats['reply_markers'] if stats['reply_markers'] is not None else 'kolom tidak tersedia'))
            print('Penanda RT pada teks: {} (indikasi, bukan bukti lengkap tipe repost).'.format(stats['repost_text_markers']))
            if stats['reply_markers'] is None:
                metadata['status'] = 'reply_type_unknown'
                print('Tipe balasan belum dapat diperiksa dari CSV ini.')
            elif stats['reply_markers'] or stats['repost_text_markers']:
                metadata['status'] = 'unexpected_reply_or_repost_marker'
                print('Ada hasil yang perlu diperiksa; jangan anggap batch ini seluruhnya postingan utama.')
            print('Teks panjang note_tweet: {}; fallback legacy: {}.'.format(stats['rows_with_note_tweet'], stats['rows_with_legacy_fallback']))
            print('Total gambar: {}; postingan dengan beberapa gambar: {}.'.format(stats['total_images'], stats['posts_with_multiple_images']))
            with files[0].open(encoding='utf-8-sig', newline='') as stream:
                media_records = [{'post_id': row['id_str'], 'source_url': row.get('tweet_url'), 'media_extraction_basis': row.get('media_extraction_basis'), 'media': json.loads(row.get('media_json') or '[]')} for row in csv.DictReader(stream)]
            media_file = files[0].with_suffix('.media.json')
            with media_file.open('x', encoding='utf-8') as stream:
                json.dump(media_records, stream, ensure_ascii=False, indent=2)
            print('Daftar media:', media_file.relative_to(ROOT))
            print('Target adalah permintaan ke tool; jumlah hasil aktual dapat berbeda.')
        except (ValueError, csv.Error, OSError):
            metadata['status'] = 'export_unreadable'
            print('CSV ditemukan tetapi belum dapat diringkas; periksa file lokal.')
    else:
        if metadata['status'] == 'finished':
            metadata['status'] = 'no_csv'
        print('Belum ada CSV. Simpan pesan error terminal untuk diagnosis, tanpa token.')
    folder.joinpath('run_info.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    if args.with_ocr and metadata['status'] == 'finished' and files:
        try:
            from ocr_tweet_images import enrich
            ocr_output = enrich(files[0])
            metadata['ocr_output'] = str(ocr_output.relative_to(ROOT))
            metadata['ocr_run_status'] = 'exported'
        except (ValueError, OSError, csv.Error):
            metadata['ocr_run_status'] = 'failed'
            print('OCR gagal disiapkan; CSV sumber tetap tersimpan. Periksa docs/ocr.md.')
        folder.joinpath('run_info.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    metadata['run_info_path'] = str(folder.joinpath('run_info.json').relative_to(ROOT))
    folder.joinpath('run_info.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    if args.result_manifest:
        with args.result_manifest.open('x', encoding='utf-8') as stream:
            json.dump(metadata, stream, indent=2)
    print('Uji ini tidak mengubah file collector lama dan tidak mengunggah ke Supabase.')
    if metadata.get('ocr_run_status') == 'failed':
        return 1
    return 0 if metadata['status'] == 'finished' and metadata.get('csv_summary', {}).get('rows', 0) > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
