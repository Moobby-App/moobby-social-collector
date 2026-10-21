import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ocr_tweet_images import original_url, aggregate, enrich


class OCRTests(unittest.TestCase):
    def test_original_size_and_allowed_host(self):
        url = original_url('https://pbs.twimg.com/media/abc.jpg?name=small')
        self.assertIn('format=jpg', url)
        self.assertIn('name=orig', url)
        for url in ['http://pbs.twimg.com/media/a.jpg', 'https://example.com/a.jpg', 'https://pbs.twimg.com/profile_images/a.jpg']:
            with self.assertRaises(ValueError): original_url(url)

    def test_status_does_not_confuse_missing_media_with_no_images(self):
        self.assertEqual(aggregate([], 'not_available'), 'media_unknown')
        self.assertEqual(aggregate([], 'extended_entities'), 'no_images')
        self.assertEqual(aggregate([{'ocr_status':'completed'}, {'ocr_status':'failed'}], 'extended_entities'), 'partial')

    def test_separate_output_keeps_caption_and_failure_per_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for lang in ['ind','eng']: (root/(lang+'.traineddata')).write_bytes(b'model')
            source = root/'input.csv'
            media = [{'type':'photo','media_index':1,'image_url':'https://pbs.twimg.com/media/a.jpg'}, {'type':'photo','media_index':2,'image_url':'https://pbs.twimg.com/media/b.jpg'}, {'type':'video','thumbnail_url':'https://pbs.twimg.com/media/c.jpg'}]
            with source.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['id_str','full_text','media_json','media_extraction_basis'])
                writer.writeheader();writer.writerow({'id_str':'2112490082422976818','full_text':'Caption asli','media_json':json.dumps(media),'media_extraction_basis':'extended_entities'})
            before=source.read_bytes()
            with patch('ocr_tweet_images.shutil.which',return_value='tesseract'), patch('ocr_tweet_images.subprocess.check_output',return_value='tesseract test\n'), patch('ocr_tweet_images.download',side_effect=[b'image',OSError('failed')]), patch('ocr_tweet_images.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='Tulisan poster\n')):
                output=enrich(source,tessdata=root)
            self.assertEqual(source.read_bytes(),before)
            with output.open(encoding='utf-8-sig',newline='') as f: row=next(csv.DictReader(f))
            self.assertEqual(row['full_text'],'Caption asli')
            self.assertEqual(row['ocr_status'],'partial')
            self.assertIn('Tulisan poster',row['ocr_text'])
            self.assertEqual(len(json.loads(row['ocr_results_json'])),2)


if __name__ == '__main__': unittest.main()
