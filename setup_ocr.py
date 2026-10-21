"""Unduh hanya model ind/eng dari versi resmi tessdata_fast yang ditetapkan."""
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
REVISION = '87416418657359cb625c412a48b6e1d6d41c29bd'


def main():
    folder = ROOT / 'data/ocr_models'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('ind.traineddata', 'eng.traineddata'):
        target = folder / name
        if target.exists():
            print(name, 'sudah tersedia; tidak ditimpa.')
            continue
        url = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{}/{}'.format(REVISION, name)
        with urlopen(url, timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
        if not 100000 < len(data) <= 20 * 1024 * 1024:
            raise ValueError('Ukuran model tidak sesuai.')
        with target.open('xb') as handle:
            handle.write(data)
        print(name, 'tersimpan. SHA256:', hashlib.sha256(data).hexdigest())


if __name__ == '__main__':
    main()
