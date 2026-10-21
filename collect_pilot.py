"""Collect configured pilot accounts and prepare upload batches; cloud upload is separate."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from prepare_harvest_batch import prepare_file

ROOT = Path(__file__).resolve().parent


def load_sources(path):
    sources = json.loads(path.read_text())['sources']
    selected = [item for item in sources if item.get('enabled')]
    if not selected or len({item['source_key'] for item in selected}) != len(selected):
        raise ValueError('Daftar sumber kosong atau duplikat.')
    for item in selected:
        if item['source_key'] != 'x:' + item['account'].lower() or item['account'] not in ('commuterline', 'jalur5_'):
            raise ValueError('Konfigurasi pilot hanya untuk CommuterLine dan Jalur5.')
        if type(item['limit']) is not int or not 1 <= item['limit'] <= 100:
            raise ValueError('Target per akun harus 1–100.')
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-label', required=True)
    parser.add_argument('--no-ocr', action='store_true')
    parser.add_argument('--config', type=Path, default=ROOT/'config/collection_sources.json')
    args = parser.parse_args()
    try:
        sources = load_sources(args.config)
        if not args.collector_label.strip():
            raise ValueError('Collector label wajib.')
    except (ValueError, KeyError, OSError):
        parser.exit(1, 'Konfigurasi sumber/label tidak valid.\n')
    if not args.no_ocr:
        import shutil
        if not shutil.which('tesseract') or not all((ROOT/'data/ocr_models'/(lang+'.traineddata')).is_file() for lang in ['ind','eng']):
            parser.exit(1, 'OCR belum siap. Ikuti docs/ocr.md dahulu.\n')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = ROOT/'data/collection_sessions'/stamp
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {'schema_version':'collection.session.v1', 'created_at':datetime.now(timezone.utc).isoformat(),
                'status':'running', 'batches':[], 'errors':[]}
    try:
        for source in sources:
            print('\nMengumpulkan:', source['source_key'], flush=True)
            result_file = folder/(source['account']+'.result.json')
            cmd = [sys.executable, str(ROOT/'try_tweet_harvest.py'), '--account', source['account'], '--limit', str(source['limit']), '--use-saved-session', '--result-manifest', str(result_file)]
            if not args.no_ocr: cmd.append('--with-ocr')
            result = subprocess.run(cmd, cwd=str(ROOT))
            if result.returncode != 0 or not result_file.exists():
                manifest['errors'].append({'source_key':source['source_key'], 'stage':'collection_or_ocr'})
                break  # Do not keep making browser requests after an unsuccessful run.
            metadata = json.loads(result_file.read_text())
            input_csv = ROOT/(metadata['ocr_output'] if not args.no_ocr else metadata['csv_path'])
            run_info = ROOT/metadata['run_info_path']
            batch_file = folder/(source['account']+'.raw.json')
            batch = prepare_file(input_csv,run_info,source['source_key'],args.collector_label,batch_file)
            manifest['batches'].append({'source_key':source['source_key'],'path':batch_file.name,'run_id':batch['run']['id'],'records':len(batch['records'])})
        manifest['status'] = 'prepared' if len(manifest['batches']) == len(sources) else 'partial'
    except (ValueError, OSError, KeyError, TypeError):
        manifest['status']='partial'
        manifest['errors'].append({'stage':'prepare_batch','reason':'input_or_contract_error'})
    except KeyboardInterrupt:
        manifest['status']='cancelled'
    finally:
        output = folder/'manifest.json'
        output.write_text(json.dumps(manifest,indent=2))
        print('\nManifest:',output)
        print('Batch siap:',len(manifest['batches']), '/',len(sources))
        print('Belum ada upload Supabase. Gunakan manifest yang sama untuk retry upload.')
    return 0 if manifest['status']=='prepared' else 1


if __name__ == '__main__': sys.exit(main())
