import copy
import json
from pathlib import Path
import unittest

from prepare_harvest_batch import prepare_harvest
from prepare_batch import capture_hash
from upload_collection_batch import validate_batch, BatchError


def sample():
    row = {'id_str':'2112490082422976818','tweet_url':'https://x.com/CommuterLine/status/2112490082422976818',
           'username':'CommuterLine','full_text':'Caption lengkap','created_at':'2026-10-20T10:24:18Z',
           'media_json':'[]','text_completeness':'unknown','text_extraction_method':'note_tweet',
           'media_extraction_basis':'not_available','in_reply_to_screen_name':'','post_type':'original'}
    run = {'tool':'tweet-harvest','version':'2.7.1','search_tab':'LATEST','search_query':'from:commuterline -filter:replies -filter:retweets',
           'started_at':'2026-10-21T01:00:00Z','finished_at':'2026-10-21T01:01:00Z','target':1,'status':'finished'}
    return row,run


class HarvestBatchTests(unittest.TestCase):
    def setUp(self):
        self.row,self.run = sample()
        self.schema=json.loads(Path(__file__).resolve().parents[1].joinpath('raw_batch_v2.schema.json').read_text())

    def batch(self,rows=None):
        return prepare_harvest(rows or [self.row],self.run,'x:commuterline','yuki',{'ind':'a'*64,'eng':'b'*64})

    def test_contract_and_deterministic_retry(self):
        a,b=self.batch(),self.batch()
        validate_batch(a,self.schema)
        self.assertEqual(a,b)
        self.assertEqual(a['records'][0]['raw_text'],'Caption lengkap')
        self.assertEqual(a['records'][0]['published_at_basis'],'page_timestamp')

    def test_same_post_new_run_reuses_snapshot_hash(self):
        a=self.batch()
        self.run['started_at']='2026-10-22T01:00:00Z';self.run['finished_at']='2026-10-22T01:01:00Z'
        b=self.batch()
        self.assertNotEqual(a['run']['id'],b['run']['id'])
        self.assertEqual(a['records'][0]['capture_hash'],b['records'][0]['capture_hash'])

    def test_engagement_counters_do_not_create_snapshots(self):
        a=self.batch()
        self.row['favorite_count']='999';self.row['reply_count']='100'
        self.assertEqual(a['records'][0]['capture_hash'],self.batch()['records'][0]['capture_hash'])

    def test_identical_csv_duplicates_collapse_but_conflicts_rejected(self):
        self.assertEqual(len(self.batch([self.row,copy.deepcopy(self.row)])['records']),1)
        other=copy.deepcopy(self.row);other['full_text']='Different caption'
        with self.assertRaises(ValueError):self.batch([self.row,other])

    def test_source_reply_and_wrong_query_rejected(self):
        self.row['username']='other'
        with self.assertRaises(ValueError):self.batch()
        self.row['username']='CommuterLine';self.row['in_reply_to_screen_name']='someone'
        with self.assertRaises(ValueError):self.batch()
        self.row['in_reply_to_screen_name']='';self.run['search_query']='from:jalur5_'
        with self.assertRaises(ValueError):self.batch()

    def add_ocr(self):
        self.row['media_json']=json.dumps([{'media_id':'123','media_index':1,'type':'photo','image_url':'https://pbs.twimg.com/media/a.jpg','alt_text':None,'width':100,'height':200}])
        self.row['media_extraction_basis']='extended_entities'
        self.row['ocr_results_json']=json.dumps([{'media_id':'123','media_index':1,'media_url':'https://pbs.twimg.com/media/a.jpg','image_hash':'c'*64,'ocr_status':'completed','ocr_text':'Tulisan gambar','ocr_engine':'tesseract 5','ocr_languages':'ind+eng','ocr_psm':11,'ocr_processed_at':'2026-10-21T01:03:00Z','local_image':'images/private-local.jpg'}])

    def test_ocr_kept_separate_without_local_paths_or_volatile_hash(self):
        self.add_ocr();a=self.batch();validate_batch(a,self.schema)
        row=a['records'][0]
        self.assertEqual(row['raw_text'],'Caption lengkap')
        self.assertEqual(row['media'][0]['ocr']['raw_text'],'Tulisan gambar')
        self.assertNotIn('private-local',json.dumps(a))
        row['media'][0]['ocr']['processed_at']='2026-10-23T01:00:00Z'
        self.assertEqual(capture_hash(row),a['records'][0]['capture_hash'])
        row['media'][0]['ocr']['raw_text']='Different OCR'
        self.assertNotEqual(capture_hash(row),a['records'][0]['capture_hash'])

    def test_ocr_wrong_image_and_unproven_completed_rejected(self):
        self.add_ocr()
        ocr=json.loads(self.row['ocr_results_json']);ocr[0]['media_url']='https://pbs.twimg.com/media/other.jpg'
        self.row['ocr_results_json']=json.dumps(ocr)
        with self.assertRaises(ValueError):self.batch()
        self.add_ocr();a=self.batch();a['records'][0]['media'][0].pop('image_hash');a['records'][0]['capture_hash']=capture_hash(a['records'][0])
        with self.assertRaises(BatchError):validate_batch(a,self.schema)

    def test_different_post_ids_never_merge_by_same_text(self):
        other=copy.deepcopy(self.row);other['id_str']='2112490082422976819';other['tweet_url']='https://x.com/commuterline/status/'+other['id_str']
        self.assertEqual(len(self.batch([self.row,other])['records']),2)


if __name__ == '__main__': unittest.main()
