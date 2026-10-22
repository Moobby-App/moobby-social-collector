"""Collect raw Green Line candidates, initially 24 hours then since the last successful run."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
EXPERIMENT = ROOT / 'experiments/tweet_harvest'
STATIONS = [
    ['Tanah Abang', 'Tanahabang', 'Palmerah', 'Kebayoran'],
    ['Pondok Ranji', 'Pondokranji', 'Jurang Mangu', 'Jurangmangu', 'Sudimara'],
    ['Rawa Buntu', 'Rawabuntu', 'Serpong', 'Cisauk'],
    ['Cicayur', 'Parung Panjang', 'Parungpanjang', 'Cilejit'],
    ['Daru', 'Tenjo', 'Tigaraksa', 'Cikoya'],
    ['Maja', 'Citeras', 'Rangkasbitung', 'Rangkas Bitung'],
]


def alternatives(words):
    return '(' + ' OR '.join('"' + word + '"' for word in words) + ')'


def search_plan(end=None, start=None):
    end = end or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError('Window end must have a timezone.')
    end = end.astimezone(timezone.utc)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or start >= end:
        raise ValueError('Window start must have a timezone and precede window end.')
    start = start.astimezone(timezone.utc)
    # Browser search uses calendar dates; the collector applies the exact UTC
    # interval to every returned timestamp, including both boundary dates.
    dates = ' since:{} until:{}'.format(start.date(), (end + timedelta(days=1)).date())
    routes = alternatives(['green line', 'greenline', 'jalur hijau', 'lintas Serpong', 'lintas Rangkasbitung'])
    trains = '(KRL OR kereta OR commuterline OR "commuter line")'
    searches = [
        ('account_commuterline', 'from:commuterline'),
        ('account_jalur5', 'from:jalur5_'),
        ('route_names', routes + ' ' + trains),
    ]
    for index, names in enumerate(STATIONS, 1):
        searches.append(('stations_{}'.format(index), alternatives(names) + ' ' + trains))
    # Complaints may only mention the operator and a station, without saying KRL.
    locations = alternatives(['Serpong', 'Rangkasbitung', 'Cisauk', 'Sudimara', 'Parungpanjang', 'Parung Panjang'])
    searches.append(('operator_mentions', '@commuterline ' + locations))
    return {
        'schema_version': 'greenline.raw.v1',
        'window_start': start.isoformat(), 'window_end': end.isoformat(),
        'queries': [{'id': key, 'query': query + dates} for key, query in searches],
        'scope': 'Raw candidates; account searches deliberately include other lines. No relevance or truth classification.',
        'coverage': 'X browser search results only; completeness is not guaranteed.',
    }


def query_signature(queries):
    return [(item['id'], re.sub(r' since:\d{4}-\d{2}-\d{2} until:\d{4}-\d{2}-\d{2}$', '', item['query'])) for item in queries]


def incremental_plan(sessions, end=None, overlap_minutes=30, full_window=False):
    end = end or datetime.now(timezone.utc)
    baseline = search_plan(end)
    baseline.update(window_mode='full_24h', overlap_minutes=overlap_minutes)
    if not 1 <= overlap_minutes <= 1440:
        raise ValueError('Overlap harus 1–1440 menit.')
    if full_window:
        return baseline
    checkpoints = []
    for path in sessions.glob('*/manifest.json'):
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
            if report.get('schema_version') != 'greenline.raw.v1' or report.get('status') != 'collected':
                continue
            if report.get('ocr_status') not in (None, 'exported', 'skipped', 'not_needed'):
                continue
            if not (path.parent / 'raw_posts.json').is_file():
                continue
            if query_signature(report['queries']) != query_signature(baseline['queries']):
                continue
            if any(item.get('status') != 'no_new_matching_posts' for item in report['queries']):
                continue
            previous_end = datetime.fromisoformat(report['window_end'].replace('Z', '+00:00'))
            if previous_end.tzinfo is not None and previous_end < end:
                checkpoints.append(previous_end)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if not checkpoints:
        return baseline
    previous_end = max(checkpoints)
    plan = search_plan(end, previous_end - timedelta(minutes=overlap_minutes))
    plan.update(window_mode='incremental', checkpoint_window_end=previous_end.isoformat(), overlap_minutes=overlap_minutes)
    return plan


def query_metrics(records):
    counts, exclusive = {}, {}
    for record in records:
        matches = {match['id'] for snapshot in record['snapshots'] for match in snapshot['query_matches']}
        for query_id in matches:
            counts[query_id] = counts.get(query_id, 0) + 1
        if len(matches) == 1:
            query_id = next(iter(matches))
            exclusive[query_id] = exclusive.get(query_id, 0) + 1
    return {query_id: {'unique_posts': count, 'exclusive_posts': exclusive.get(query_id, 0)} for query_id, count in counts.items()}


def in_window(value, start, end):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.tzinfo is not None and start <= parsed < end
    except (ValueError, TypeError, AttributeError):
        return False


def merge_results(plan, folder):
    """Deduplicate identities while preserving distinct content and query evidence."""
    start = datetime.fromisoformat(plan['window_start'])
    end = datetime.fromisoformat(plan['window_end'])
    records = {}
    for query in plan['queries']:
        source = folder / (query['id'] + '.json')
        if not source.exists():
            continue
        for post in json.loads(source.read_text(encoding='utf-8'))['posts']:
            if not in_window(post.get('published_at'), start, end):
                continue
            post_id = post['id_str']
            record = records.setdefault(post_id, {'platform': 'x', 'external_id': post_id, 'snapshots': []})
            content = {key: value for key, value in post.items() if key != 'captured_at'}
            digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            snapshot = next((item for item in record['snapshots'] if item['content_hash'] == digest), None)
            if snapshot is None:
                snapshot = dict(post, content_hash=digest, query_matches=[])
                record['snapshots'].append(snapshot)
            evidence = {'id': query['id'], 'query': query['query']}
            if evidence not in snapshot['query_matches']:
                snapshot['query_matches'].append(evidence)
    return list(records.values())


def export_csv(records, path):
    fields = ['id_str', 'username', 'tweet_url', 'created_at', 'full_text', 'post_type',
              'text_completeness', 'media_json', 'media_extraction_basis', 'query_matches_json']
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            for post in record['snapshots']:
                writer.writerow({
                    'id_str': record['external_id'], 'username': post['username'],
                    'tweet_url': post['tweet_url'], 'created_at': post['published_at'],
                    'full_text': post['full_text'], 'post_type': post['post_type'],
                    'text_completeness': post['text_completeness'],
                    'media_json': json.dumps(post['media'], ensure_ascii=False),
                    'media_extraction_basis': post['media_extraction_basis'],
                    'query_matches_json': json.dumps(post['query_matches'], ensure_ascii=False),
                })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-label', required=True)
    parser.add_argument('--no-ocr', action='store_true')
    parser.add_argument('--plan-only', action='store_true', help='Show queries without login/browser/network.')
    parser.add_argument('--max-posts-per-query', type=int, default=500)
    parser.add_argument('--full-window', action='store_true', help='Ignore checkpoints and fetch the last 24 hours.')
    parser.add_argument('--overlap-minutes', type=int, default=30)
    parser.add_argument('--result-manifest', type=Path, help='Write the resulting session location for the local scheduler.')
    args = parser.parse_args()
    if not args.collector_label.strip() or not 1 <= args.max_posts_per_query <= 5000:
        parser.error('Label wajib dan max-posts-per-query harus 1–5000.')
    try:
        plan = incremental_plan(ROOT / 'data/greenline_sessions', overlap_minutes=args.overlap_minutes, full_window=args.full_window)
    except ValueError as error:
        parser.error(str(error))
    plan.update(collector_label=args.collector_label.strip(), max_posts_per_query=args.max_posts_per_query)
    if args.plan_only:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    node = shutil.which('node')
    if not node or not (EXPERIMENT / 'node_modules/playwright').is_dir():
        parser.exit(1, 'Node/Playwright belum siap. Ikuti docs/greenline-collection.md.\n')
    if not (ROOT / 'playwright/.auth/x_reused_session.json').is_file():
        parser.exit(1, 'Session X lokal belum tersedia. Jalankan python test_x_existing_session.py.\n')
    if not args.no_ocr:
        if not shutil.which('tesseract') or not all((ROOT / 'data/ocr_models' / (lang + '.traineddata')).is_file() for lang in ('ind', 'eng')):
            parser.exit(1, 'OCR belum siap. Ikuti docs/ocr.md atau gunakan --no-ocr.\n')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = ROOT / 'data/greenline_sessions' / stamp
    folder.mkdir(parents=True, exist_ok=False)
    plan_file = folder / 'plan.json'
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = dict(plan, status='running', complete_history=False,
                    ocr_status='skipped' if args.no_ocr else 'pending')
    env = dict(os.environ)
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(EXPERIMENT / '.browsers')
    print('Window UTC: {} → {}'.format(plan['window_start'], plan['window_end']), flush=True)
    print('Mode:', plan['window_mode'], '| overlap:', args.overlap_minutes, 'menit', flush=True)
    print('Folder:', folder, flush=True)
    try:
        result = subprocess.run([node, str(EXPERIMENT / 'collect_greenline.cjs'), str(plan_file)], env=env)
        manifest['browser_exit_code'] = result.returncode
        manifest['status'] = 'collected' if result.returncode == 0 else 'partial'
    except KeyboardInterrupt:
        manifest['status'] = 'cancelled'
    except OSError:
        manifest['status'] = 'partial'
        manifest['error'] = 'browser_launch_failed'
    finally:
        records = merge_results(plan, folder)
        (folder / 'raw_posts.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        export_csv(records, folder / 'raw_posts.csv')
        manifest['unique_posts'] = len(records)
        if not records:
            manifest['ocr_status'] = 'not_needed'
        manifest['query_metrics'] = query_metrics(records)
        manifest['queries'] = []
        for query in plan['queries']:
            path = folder / (query['id'] + '.json')
            report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            manifest['queries'].append(dict(query, status=report.get('status', 'not_run'), count=len(report.get('posts', []))))
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Postingan unik:', len(records), '| Status:', manifest['status'], flush=True)
        print('Data mentah lokal; belum dikelompokkan atau diunggah ke Supabase.', flush=True)
    if records and not args.no_ocr and manifest['status'] != 'cancelled':
        try:
            from ocr_tweet_images import enrich
            manifest['ocr_output'] = str(enrich(folder / 'raw_posts.csv', cache_dir=ROOT / 'data/ocr_cache').relative_to(ROOT))
            manifest['ocr_status'] = 'exported'
        except (ValueError, OSError, csv.Error):
            manifest['ocr_status'] = 'failed'
            print('OCR gagal; data mentah tetap tersedia.')
        except KeyboardInterrupt:
            manifest['ocr_status'] = 'cancelled'
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.result_manifest:
        args.result_manifest.write_text(json.dumps({'session_path': str(folder), 'manifest_path': str(folder / 'manifest.json')}) + '\n', encoding='utf-8')
    return 0 if manifest['status'] == 'collected' and manifest.get('ocr_status') not in ('failed', 'cancelled') else 1


if __name__ == '__main__':
    sys.exit(main())
