"""Integration check in a disposable Unix-socket-only PostgreSQL. Never uses cloud credentials."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_batch import capture_hash
from upload_collection_batch import upload, validate_batch, BatchError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    batches = [json.loads((args.manifest.parent/item['path']).read_text()) for item in manifest['batches']]
    schema = json.loads((ROOT/'raw_batch_v2.schema.json').read_text())
    for batch in batches: validate_batch(batch,schema)
    initdb, pg_ctl = shutil.which('initdb'), shutil.which('pg_ctl')
    if not initdb or not pg_ctl:
        parser.exit(1,'PostgreSQL binaries diperlukan hanya untuk integration test.\n')
    with tempfile.TemporaryDirectory(prefix='moobby_pg_',dir='/private/tmp') as temporary:
        temp=Path(temporary); data=temp/'cluster'
        subprocess.run([initdb,'-D',str(data),'-U','moobby_test_admin','-A','trust','--no-locale','--encoding=UTF8'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        started=False
        try:
            subprocess.run([pg_ctl,'-D',str(data),'-l',str(temp/'server.log'),'-o',"-k {} -c listen_addresses=''".format(temp),'-w','start'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            started=True
            params={'host':str(temp),'dbname':'postgres','user':'moobby_test_admin','prepare_threshold':None}
            admin=psycopg.connect(**params,autocommit=True)
            admin.execute('CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN;')
            for name in ['001_collection_schema.sql','002_sources.sql']:
                admin.execute((ROOT/'db'/name).read_text())
            admin.execute('CREATE ROLE collector_test LOGIN; GRANT moobby_collection_team TO collector_test;')
            writer_params=dict(params,user='collector_test')
            v1=json.loads((ROOT/'data/raw_batches/commuterline_20261020_01.json').read_text())
            with psycopg.connect(**writer_params) as writer:
                result=upload(writer,v1)
            print('v1 upload sebelum migration:',result)
            try:
                with psycopg.connect(**writer_params) as writer: upload(writer,batches[0])
                raise AssertionError('v2 should require migration')
            except BatchError: pass
            for _ in range(2):admin.execute((ROOT/'db/004_snapshot_extraction_metadata.sql').read_text())
            with psycopg.connect(**writer_params) as writer:
                initial=[upload(writer,batch) for batch in batches]
            print('v2 initial:',initial)
            tables=['raw_contents','raw_snapshots','content_observations','collection_runs']
            counts=lambda:[admin.execute('SELECT count(*) FROM collection.'+name).fetchone()[0] for name in tables]
            baseline=counts()
            with psycopg.connect(**writer_params) as writer:
                retry=[upload(writer,batch) for batch in batches]
            assert all(result['already_uploaded'] for result in retry)
            assert counts()==baseline
            print('Retry batch identik: tidak ada baris tambahan.')
            fresh=copy.deepcopy(batches[0]);fresh['run']['id']=str(uuid.uuid4());fresh['run']['collector_label']='member-b'
            with psycopg.connect(**writer_params) as writer: repeated=upload(writer,fresh)
            assert repeated['contents']==repeated['snapshots']==0
            assert repeated['observations']==len(fresh['records'])
            print('Run anggota lain: identitas/snapshot digunakan ulang, observasi baru dicatat.')
            edited=copy.deepcopy(fresh);edited['run']['id']=str(uuid.uuid4());edited['records']=edited['records'][:1]
            edited['run']['captured_count']=edited['run']['requested_count']=1
            edited['records'][0]['raw_text']+=' [test snapshot change]'
            edited['records'][0]['capture_hash']=capture_hash(edited['records'][0])
            with psycopg.connect(**writer_params) as writer: changed=upload(writer,edited)
            assert changed['contents']==0 and changed['snapshots']==1
            concurrent=copy.deepcopy(batches[1]);concurrent['run']['id']=str(uuid.uuid4())
            def send(_):
                with psycopg.connect(**writer_params) as writer:return upload(writer,concurrent)
            with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(send,range(2)))
            assert sum(result['already_uploaded'] for result in results)==1
            print('Dua upload bersamaan dengan run sama: satu insert, satu retry identik.')
            racing=copy.deepcopy(edited)
            racing['records'][0]['external_id']='9999999999999990001'
            racing['records'][0]['canonical_url']='https://x.com/commuterline/status/9999999999999990001'
            def independent_send(_):
                value=copy.deepcopy(racing);value['run']['id']=str(uuid.uuid4())
                with psycopg.connect(**writer_params) as writer:return upload(writer,value)
            with ThreadPoolExecutor(max_workers=2) as pool: races=list(pool.map(independent_send,range(2)))
            assert sum(result['contents'] for result in races)==1
            assert sum(result['snapshots'] for result in races)==1
            assert sum(result['observations'] for result in races)==2
            print('Run berbeda bersamaan untuk ID baru yang sama: satu identitas, satu snapshot, dua observasi.')
            previous=counts()
            admin.execute("UPDATE collection.sources SET enabled=false WHERE source_key='x:jalur5_'")
            try:
                with psycopg.connect(**writer_params) as writer:
                    for batch in batches:
                        value=copy.deepcopy(batch);value['run']['id']=str(uuid.uuid4());upload(writer,value)
                raise AssertionError('disabled source must reject transaction')
            except BatchError:pass
            assert counts()==previous
            admin.execute("UPDATE collection.sources SET enabled=true WHERE source_key='x:jalur5_'")
            print('Gagal pada batch kedua: seluruh transaksi di-rollback.')
            assert admin.execute('SELECT count(*) FROM (SELECT platform,external_id FROM collection.raw_contents GROUP BY 1,2 HAVING count(*)>1) t').fetchone()[0]==0
            assert admin.execute('SELECT count(*) FROM (SELECT raw_content_id,capture_hash FROM collection.raw_snapshots GROUP BY 1,2 HAVING count(*)>1) t').fetchone()[0]==0
            try:
                with admin.transaction():
                    admin.execute('SET LOCAL ROLE anon')
                    admin.execute('SELECT * FROM collection.raw_contents')
                raise AssertionError('anon should not have access')
            except psycopg.errors.InsufficientPrivilege:pass
            print('LULUS: perubahan snapshot, uniqueness database, migration idempotent, dan anon ditolak.')
            admin.close()
        finally:
            if started:subprocess.run([pg_ctl,'-D',str(data),'-m','immediate','-w','stop'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)


if __name__=='__main__':main()
