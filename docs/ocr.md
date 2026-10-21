# OCR gambar postingan

Teks OCR adalah hasil ekstraksi mesin, bukan verifikasi informasi. `full_text` tetap berisi caption; `ocr_text` berisi tulisan yang terbaca dari foto. Tidak memakai API key, LLM, atau upload ke Supabase.

## Setup anggota tim

Pasang Tesseract 5 melalui package manager sistem. Pada macOS:

```bash
brew install tesseract
python setup_ocr.py
```

Script setup mengambil model Indonesia (`ind`) dan Inggris (`eng`) dari commit resmi tessdata_fast `87416418657359cb625c412a48b6e1d6d41c29bd`. Model tersimpan di `data/ocr_models/` yang diabaikan Git. SHA256 setiap model dicatat pada hasil OCR. Tidak ada tambahan dependency Python.

## CSV yang sudah dikumpulkan

```bash
python ocr_tweet_images.py --input "data/tweet_harvest/<batch>/tweets-data/commuterline_trial.csv"
```

Untuk mencoba satu foto saja, tambahkan `--max-images 1`. Foto lain akan tetap dicatat sebagai `not_attempted`, bukan dianggap sudah dibaca.

## Collection sekaligus OCR

```bash
python try_tweet_harvest.py --account commuterline --limit 20 --use-saved-session --with-ocr
```

## Output

Folder `ocr_<timestamp>/` dibuat di sebelah CSV sumber. Berisi CSV `_ocr.csv`, `ocr_results.json`, dan folder gambar asli beserta teks `.txt`. Nama gambar menggunakan SHA256 dan ekstensi `.img`; isinya tetap bytes asli JPEG/PNG/WebP. CSV sumber tidak ditimpa.

Kolom tambahan:

- `ocr_text`: teks gabungan per postingan dengan label urutan gambar.
- `ocr_status`: `completed`, `partial`, `failed`, `not_attempted`, `no_images`, atau `media_unknown`.
- `ocr_results_json`: hasil setiap foto, URL sumber/unduhan, hash, path gambar lokal relatif folder OCR, engine, bahasa, mode PSM, waktu, status, dan teks mentah.

`completed` berarti OCR berhasil dijalankan, bukan jaminan semua tulisan terbaca benar. Teks kosong dapat berarti tidak ada tulisan yang dikenali. `ocr_verification_status` tetap `not_reviewed`.

Foto diambil dari pbs.twimg.com pada ukuran original, dibatasi 20 MB/foto. Thumbnail video/GIF, avatar, quote, link preview, dan frame video tidak dibaca pada tahap ini. OCR berjalan berurutan dengan batas waktu, dan gagal pada satu gambar tidak menghapus hasil gambar lain. Gambar identik dalam satu run menggunakan ulang hasil OCR berdasarkan hash. Cache ini belum lintas run.

Gambar warna asli diproses Tesseract LSTM dengan `ind+eng`, PSM 11 (sparse text). Tidak ada perbaikan kalimat atau tafsir otomatis. Poster kompleks, tabel, dan tulisan kecil tetap perlu dibandingkan dengan gambar asli.

Referensi: https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html dan https://github.com/tesseract-ocr/tessdata_fast (Apache-2.0).
