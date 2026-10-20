"""Read-only Supabase connection check. No secrets are saved or printed."""
import getpass
import re
import sys


def main():
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        print('Dependency belum tersedia. Jalankan: python -m pip install "psycopg[binary]>=3.2,<3.3"')
        return 1

    print('Tempel URI Session pooler dari Supabase. Placeholder password boleh tetap ada.')
    uri = getpass.getpass('Connection URI (disembunyikan): ').strip()
    if not uri.startswith(('postgresql://', 'postgres://')):
        print('Format tidak sesuai. Gunakan Connection String berformat URI PostgreSQL.')
        return 1
    # Remove placeholder before libpq parsing; actual password is passed separately.
    uri = re.sub(r'\[(?:YOUR[-_ ]?PASSWORD|PASSWORD)\]', '', uri, flags=re.I)
    try:
        params = conninfo_to_dict(uri)
    except psycopg.Error:
        print('URI tidak dapat dibaca. Salin URI lengkap dari panel Connect.')
        return 1
    host = params.get('host', '').lower()
    if not host.endswith(('.supabase.com', '.supabase.co')):
        print('Host belum dikenali sebagai Supabase. Periksa URI dari panel Connect.')
        return 1
    if not params.get('password'):
        params['password'] = getpass.getpass('Database password project (disembunyikan): ')
    if not params.get('password'):
        print('Password kosong. Pengujian dibatalkan.')
        return 1
    params.update(sslmode='require', connect_timeout=15)

    try:
        with psycopg.connect(**params, prepare_threshold=None) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '10s'")
            tables = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'collection' ORDER BY tablename"
            ).fetchall()
            expected = {'sources', 'collection_runs', 'raw_contents', 'raw_snapshots', 'content_observations'}
            present = {row[0] for row in tables}
            if not expected.issubset(present):
                print('Koneksi berhasil, tetapi tabel collection belum lengkap atau tidak terlihat oleh role ini.')
                print('Jumlah tabel yang diharapkan dan terlihat: {}/5'.format(len(expected & present)))
                return 1
            sources = conn.execute(
                'SELECT source_key, enabled FROM collection.sources ORDER BY priority, source_key'
            ).fetchall()
            print('Koneksi Supabase berhasil.')
            print('Tabel collection: 5/5 tersedia.')
            print('Daftar sumber: {} baris.'.format(len(sources)))
            for key, enabled in sources:
                print('- {}: {}'.format(key, 'aktif' if enabled else 'nonaktif'))
            print('Pengujian read-only selesai. Tidak ada data ditambahkan atau diubah.')
            return 0
    except psycopg.Error as error:
        # Never print the exception string: connection errors can contain credentials.
        state = error.sqlstate or 'tidak tersedia'
        print('Koneksi/pembacaan gagal: {}. SQLSTATE: {}.'.format(type(error).__name__, state))
        print('Periksa URI Session pooler, password database project, serta koneksi internet.')
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nPengujian dibatalkan.')
        sys.exit(1)
