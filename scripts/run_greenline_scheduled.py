"""macOS cron entry point: collect, prepare, and upload with a durable retry queue."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / 'data/automation/greenline'
ZONE = ZoneInfo('Asia/Jakarta')


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_json(path, fallback=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else (fallback or {})


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def due(now, config, state):
    local = now.astimezone(ZONE)
    if now < datetime.fromisoformat(config['start_at']):
        return False
    if not 4 <= local.hour <= 23 or local.minute % 10:
        return False
    if state.get('paused'):
        return False
    if state.get('cooldown_until') and now < datetime.fromisoformat(state['cooldown_until']):
        return False
    return True


def next_state(report, now):
    statuses = {query.get('status') for query in report.get('queries', [])}
    if 'login_or_verification' in statuses:
        return {'paused': True, 'reason': 'login_or_verification', 'action': 'Periksa akun X secara manual, perbarui session, lalu jalankan --resume.'}
    if 'access_or_rate_limited' in statuses:
        return {'cooldown_until': (now + timedelta(hours=1)).isoformat(), 'reason': 'access_or_rate_limited'}
    return {}


def environment():
    env = dict(os.environ)
    env['PATH'] = '/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    env['TZ'] = 'Asia/Jakarta'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def check():
    env = environment()
    checks = {
        'python_venv': (ROOT / '.venv/bin/python').is_file(),
        'node': bool(shutil.which('node', path=env['PATH'])),
        'tesseract': bool(shutil.which('tesseract', path=env['PATH'])),
        'session_file': (ROOT / 'playwright/.auth/x_reused_session.json').is_file(),
        'playwright': (ROOT / 'experiments/tweet_harvest/node_modules/playwright').is_dir(),
        'browser_directory': (ROOT / 'experiments/tweet_harvest/.browsers').is_dir(),
        'ocr_models': all((ROOT / 'data/ocr_models' / (lang + '.traineddata')).is_file() for lang in ('ind', 'eng')),
    }
    config = load_json(STATE / 'config.json')
    if config.get('auto_upload_requested'):
        try:
            from greenline_upload import connection_params
            connection_params()
            checks['supabase_credential_local'] = True
        except (ValueError, OSError, TypeError):
            checks['supabase_credential_local'] = False
    print(json.dumps(checks, indent=2))
    print('Pemeriksaan lokal; tidak membuka browser atau memeriksa validitas login X. Izin daemon cron belum dibuktikan sampai last_run.json tercatat dari jadwal otomatis.')
    return 0 if all(checks.values()) else 1


def upload_after_collection(config, session_path, session_report, collect_code):
    if not config.get('auto_upload_requested') and not config.get('auto_upload'):
        return {'status': 'disabled'}
    from greenline_upload import process_queue
    preparation = 'not_needed'
    try:
        if collect_code == 0 and session_path and session_report.get('unique_posts', 0):
            from prepare_greenline_batch import prepare_session
            prepare_session(Path(session_path))
            preparation = 'prepared'
        if not config.get('auto_upload'):
            return {'status': 'setup_required', 'preparation': preparation}
        summary = process_queue()
        summary['preparation'] = preparation
        return summary
    except Exception as error:
        # Upload/formatting failure must not pause collection or leak secrets.
        return {'status': 'retry_pending', 'preparation': preparation, 'error_type': type(error).__name__}


def collect(label, log_path, result_path):
    cmd = [str(ROOT / '.venv/bin/python'), '-u', str(ROOT / 'collect_greenline.py'),
           '--collector-label', label, '--result-manifest', str(result_path)]
    with log_path.open('a', encoding='utf-8') as stream:
        process = subprocess.Popen(cmd, cwd=ROOT, env=environment(), stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true')
    action.add_argument('--run-now', action='store_true', help='Run once now, still respecting pause/cooldown and lock.')
    action.add_argument('--pause', action='store_true')
    action.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.check:
        return check()
    with process_lock(STATE / 'scheduler.lock') as acquired:
        if not acquired:
            return 0
        if args.pause or args.resume:
            save_json(STATE / 'state.json', {'paused': True, 'reason': 'manual'} if args.pause else {})
            print('Jadwal dijeda.' if args.pause else 'Jadwal dilanjutkan pada giliran berikutnya.')
            return 0
        config = load_json(STATE / 'config.json')
        if not config:
            print('Config jadwal belum dipasang.')
            return 1
        now = datetime.now(timezone.utc)
        state = load_json(STATE / 'state.json')
        if not args.run_now and not due(now, config, state):
            return 0
        if args.run_now and (state.get('paused') or (state.get('cooldown_until') and now < datetime.fromisoformat(state['cooldown_until']))):
            print('Masih dijeda/cooldown. Periksa akun dan state sebelum --resume.')
            return 1
        # A delayed/duplicate cron launch must not collect twice in one slot.
        slot = now.astimezone(ZONE).strftime('%Y-%m-%dT%H:') + str(now.minute // 10)
        previous = load_json(STATE / 'last_run.json')
        if not args.run_now and previous.get('slot') == slot:
            return 0
        stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
        logs = STATE / 'logs'
        logs.mkdir(exist_ok=True)
        log = logs / (stamp + '.log')
        result_file = logs / (stamp + '.result.json')
        report = {'slot': slot, 'started_at': now.isoformat(), 'status': 'running', 'log_path': str(log)}
        save_json(STATE / 'last_run.json', report)
        try:
            code = collect(config['collector_label'], log, result_file)
            result = load_json(result_file)
            session_report = {}
            if result:
                manifest_path = Path(result['manifest_path']).resolve()
                if (ROOT / 'data/greenline_sessions').resolve() not in manifest_path.parents:
                    raise ValueError('Hasil collector di luar folder session.')
                session_report = load_json(manifest_path)
                report['session_path'] = result['session_path']
            report.update(exit_code=code, status='collected' if code == 0 else 'failed', finished_at=datetime.now(timezone.utc).isoformat())
            report['upload'] = upload_after_collection(config, report.get('session_path'), session_report, code)
            pause = next_state(session_report, datetime.now(timezone.utc))
            # Repeated unclassified failures also back off, instead of opening
            # the browser every ten minutes indefinitely with a broken setup.
            failures = 0 if code == 0 else previous.get('consecutive_failures', 0) + 1
            report['consecutive_failures'] = failures
            if failures >= 3 and not pause:
                pause = {'cooldown_until': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'reason': 'repeated_failures'}
            save_json(STATE / 'state.json', pause)
            save_json(STATE / 'last_run.json', report)
            return code
        except (OSError, ValueError, KeyError, TypeError) as error:
            report.update(status='failed', error_type=type(error).__name__, finished_at=datetime.now(timezone.utc).isoformat())
            save_json(STATE / 'last_run.json', report)
            save_json(STATE / 'state.json', {'paused': True, 'reason': 'scheduler_error'})
            return 1


if __name__ == '__main__':
    sys.exit(main())
