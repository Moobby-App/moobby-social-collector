# Green Line: pengambilan bertahap dan cache OCR

Command utama tetap:

```bash
python collect_greenline.py --collector-label yuki
```

Tanpa checkpoint yang cocok, collector mengambil 24 jam terakhir. Setelah ada run berhasil, collector mengambil sejak akhir run berhasil tersebut dikurangi overlap 30 menit, sampai waktu command baru dimulai. Checkpoint dipilih dari manifest lokal dengan query yang sama, semua pencarian berhenti tanpa hasil baru, dan OCR selesai atau sengaja dilewati. Run parsial, OCR gagal/dibatalkan, serta OCR yang masih berjalan tidak memajukan titik lanjut. Checkpoint menandakan upaya pencarian selesai, bukan jaminan seluruh postingan X sudah ditemukan.

Contoh: run berhasil berakhir 20.00 WIB. Command berikutnya dijalankan 21.00 WIB, maka window baru 19.30–21.00 WIB. Jika tidak dijalankan selama tiga hari, collector tetap mencoba mencakup jeda tiga hari beserta overlap. Batas postingan/scroll tetap berlaku; hasil yang mencapai batas ditandai partial dan tidak menjadi checkpoint berikutnya. Retry berikutnya dapat mengulang rentang itu; tinjau batas jumlah dan gunakan full refresh sesuai kebutuhan.

Preview window dan query tanpa browser:

```bash
python collect_greenline.py --collector-label yuki --plan-only
```

Mengulang seluruh 24 jam terakhir, misalnya untuk mengejar postingan yang terlambat muncul dalam pencarian:

```bash
python collect_greenline.py --collector-label yuki --full-window
```

Overlap dapat disesuaikan dengan `--overlap-minutes 60` (1–1440 menit). Tidak ada scheduler otomatis. Perubahan query menyebabkan pengambilan awal 24 jam lagi. Query browser masih menggunakan tanggal kalender; penyaringan timestamp menetapkan window yang tepat. Jadi window lebih pendek mengurangi data yang disimpan dan di-OCR, tetapi penurunan jumlah request browser tidak selalu sebanding karena batas tanggal dan pemuatan X.

## Cache OCR

Collector Green Line memakai cache lokal `data/ocr_cache/`. Hasil OCR hanya dipakai ulang bila hash gambar, versi Tesseract, hash kedua model bahasa, bahasa, OEM, dan PSM sama. Perubahan salah satu unsur menyebabkan cache miss dan OCR diproses ulang. Waktu `ocr_processed_at` tetap waktu pengenalan asli, bukan waktu cache dibaca. Cache yang tidak dapat dibaca atau rusak dianggap miss. Penulisan cache gagal tidak menghilangkan hasil OCR yang sudah diperoleh.

Gambar tetap diunduh untuk memastikan hash byte aktual, sehingga penghematan utama adalah pemrosesan Tesseract, bukan jaminan penghematan download. File gambar dan teks tetap tersedia dalam folder hasil run masing-masing. `cache_stats` pada `ocr_results.json` mencatat jumlah download, OCR baru, cache hit antarrun/dalam run, dan error penulisan cache. Cache tidak diunggah ke Supabase. Script OCR standalone dapat memilih cache lewat `--cache-dir data/ocr_cache`; alur pilot lama tetap bekerja tanpa cache antarrun.

Hasil OCR lama dapat diimpor tanpa download atau OCR baru melalui fungsi `seed_cache` di `ocr_tweet_images.py`. Import memeriksa hash CSV sumber dan hash file gambar lokal; model/engine dari proof OCR membentuk key cache.

## Mengukur kontribusi query

Manifest run baru memiliki `query_metrics`:

- `unique_posts`: jumlah ID postingan berbeda yang ditemukan suatu query, termasuk yang ditemukan query lain.
- `exclusive_posts`: jumlah ID yang hanya ditemukan query tersebut pada run ini.

Angka ini mengukur kontribusi pencarian, bukan relevansi atau kebenaran berita. Query yang menghasilkan nol dalam satu run tidak otomatis dimatikan. Hasil tetap mentah, tanpa pengelompokan kejadian. Adapter Supabase menerima window incremental sambil memeriksa kesesuaiannya dengan checkpoint dan overlap; file batch lama tetap dapat dipakai ulang tanpa perubahan UUID.
