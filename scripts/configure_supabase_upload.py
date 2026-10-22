"""One-time local setup for unattended Green Line upload; never prints secrets."""
import argparse
import getpass
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from greenline_upload import CONNECTION, connection_params, process_queue, save_private


class SetupError(ValueError):
    pass


def validate_remote(params):
    import psycopg
    with psycopg.connect(**params, prepare_threshold=None) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        column = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='collection' AND table_name='raw_snapshots' AND column_name='extraction_metadata'").fetchone()
        if not column:
            raise SetupError('Migration 004 belum tersedia. Jalankan melalui Supabase SQL Editor.')
        source = conn.execute("SELECT enabled FROM collection.sources WHERE source_key='x:greenline_search'").fetchone()
        if not source or not source[0]:
            raise SetupError('Sumber x:greenline_search belum aktif. Periksa migration 005.')
        for table in ('collection_runs', 'raw_contents', 'raw_snapshots', 'content_observations'):
            select = conn.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\')', ('collection.' + table,)).fetchone()
            insert = conn.execute('SELECT has_table_privilege(current_user,%s,\'INSERT\')', ('collection.' + table,)).fetchone()
            if not select[0] or not insert[0]:
                raise SetupError('Role database belum mempunyai akses baca/tambah collection.')
        for column_name in ('first_seen_at', 'last_seen_at', 'canonical_url'):
            privilege = conn.execute("SELECT has_column_privilege(current_user,'collection.raw_contents',%s,'UPDATE')", (column_name,)).fetchone()
            if not privilege[0]:
                raise SetupError('Role database belum mempunyai akses update metadata identitas.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only connection check using saved credentials.')
    parser.add_argument('--retry-pending', action='store_true', help='Upload prepared pending batches now without scraping.')
    args = parser.parse_args()
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        if args.check:
            validate_remote(connection_params())
            print('Koneksi, sumber Green Line, metadata, dan hak akses siap.')
            return 0
        if args.retry_pending:
            from scripts.run_greenline_scheduled import process_lock, STATE
            with process_lock(STATE / 'scheduler.lock') as acquired:
                if not acquired:
                    print('Scheduler masih berjalan; antrean akan diproses pada giliran berikutnya.')
                    return 0
                summary = process_queue()
            print(json.dumps(summary, indent=2))
            return 0 if summary['status'] in ('idle', 'uploaded', 'pending') else 1
        if not sys.stdin.isatty():
            print('Jalankan setup sekali dari terminal interaktif VS Code.')
            return 1
        print('Masukkan URI Session pooler dan password role database melalui prompt tersembunyi.')
        print('Credential akan disimpan lokal dengan izin 0600 di data/automation/greenline, diabaikan Git.')
        uri = getpass.getpass('URI Session pooler: ').strip()
        uri = re.sub(r'\[(?:YOUR[-_ ]?PASSWORD|PASSWORD)\]', '', uri, flags=re.I)
        if not uri.startswith(('postgresql://', 'postgres://')):
            print('Gunakan URI PostgreSQL dari panel Connect Supabase.')
            return 1
        raw = conninfo_to_dict(uri)
        params = {key: raw[key] for key in ('host', 'port', 'dbname', 'user', 'password') if raw.get(key)}
        params.setdefault('dbname', 'postgres')
        if not params.get('password'):
            params['password'] = getpass.getpass('Password role database: ')
        if not all(params.get(key) for key in ('host', 'user', 'dbname', 'password')) or not params['host'].lower().endswith(('.supabase.com', '.supabase.co')):
            print('URI, host Supabase, atau password belum lengkap.')
            return 1
        validate_remote(dict(params, sslmode='require', connect_timeout=15))
        save_private(CONNECTION, params)
        config_path = ROOT / 'data/automation/greenline/config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config.update(auto_upload=True, auto_upload_requested=True)
        save_private(config_path, config)
        print('Upload otomatis aktif. Batch pending dan hasil baru akan diunggah pada giliran cron berikutnya.')
        print('Untuk mengunggah antrean sekarang tanpa scraping: python scripts/configure_supabase_upload.py --retry-pending')
        return 0
    except SetupError as error:
        print(str(error))
        return 1
    except ValueError:
        # Only our explicit validation errors may be printed. URI parse errors
        # from psycopg are handled separately below without their message.
        print('Setup ditolak. Periksa migration 004/005, role tim, dan file credential lokal.')
        return 1
    except Exception as error:
        print('Setup belum berhasil: {}. Credential tidak ditampilkan.'.format(type(error).__name__))
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nSetup dibatalkan.')
        sys.exit(1)
