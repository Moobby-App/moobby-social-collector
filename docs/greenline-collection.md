# Green Line: data mentah, awal 24 jam lalu incremental

Jalankan dari root repository dengan virtualenv aktif:

```bash
python collect_greenline.py --collector-label yuki
```

Pengambilan awal memakai `[waktu mulai - 24 jam, waktu mulai)`. Run berikutnya otomatis melanjutkan sejak akhir run berhasil terakhir dikurangi overlap 30 menit. Gunakan `--full-window` untuk kembali mengambil seluruh 24 jam. Detail: [pengambilan bertahap dan cache OCR](greenline-efficiency.md). Window tetap selama satu run.
Timestamp postingan diperiksa dalam UTC; tanggal kalender pada query hanya mempersempit pencarian browser. Postingan di luar window dan timestamp yang tidak dapat dibaca tidak dimasukkan ke dataset.

Collector menjalankan 10 pencarian Latest secara berurutan: dua akun pilot (CommuterLine dan Jalur5), nama lintas, enam kelompok nama stasiun dari Tanah Abang sampai Rangkasbitung, serta mention operator dengan nama lokasi. Reply, quote, dan repost tidak sengaja dikecualikan. Pencarian publik menggabungkan lokasi dengan konteks KRL/kereta. Laporan tanpa konteks tersebut atau informasi yang hanya ada di gambar dari akun lain masih dapat terlewat.

Pengambilan akun pilot sengaja luas, termasuk postingan lintas lain, untuk mempertahankan pengumuman yang hanya menyebut lokasi di gambar. Semua hasil adalah kandidat mentah, bukan klasifikasi gangguan, pengelompokan kejadian, atau verifikasi kebenaran.

Pratinjau query tanpa browser, session, atau jaringan:

```bash
python collect_greenline.py --collector-label yuki --plan-only
```

Jika OCR belum siap:

```bash
python collect_greenline.py --collector-label yuki --no-ocr
```

Persiapan dependency Node/browser sama seperti pilot:

```bash
npm --prefix experiments/tweet_harvest ci --ignore-scripts
python try_tweet_harvest.py --install-browser
python try_tweet_harvest.py --check
```

Node.js harus tersedia di PATH. Session disiapkan sendiri melalui `python test_x_existing_session.py`. Untuk OCR ikuti `docs/ocr.md`.

Hasil di `data/greenline_sessions/<timestamp>/`:

- `plan.json`: window, query, dan label pelaksana.
- `<query_id>.json`: checkpoint hasil tiap pencarian beserta alasan berhenti.
- `raw_posts.json`: identitas postingan unik, versi konten, hubungan balasan, URL, media, timestamp, dan semua query yang menemukannya.
- `raw_posts.csv`: satu baris per versi konten, cocok dengan OCR existing.
- `ocr_<timestamp>/`: CSV OCR serta hasil OCR dan gambar lokal bila diaktifkan.
- `manifest.json`: jumlah postingan unik, status tiap query, dan lokasi output OCR.

Deduplikasi menggunakan ID string; konten berbeda dengan ID sama tetap disimpan sebagai snapshot. Ini bukan pengelompokan berita. Data tidak diunggah otomatis dan bukan batch uploader Supabase v2; jangan masukkan manifest ini ke uploader pilot.

Default batas keselamatan adalah 500 snapshot per query dan 120 putaran scroll. Ubah batas jumlah dengan `--max-posts-per-query 1000` bila perlu (maksimum 5000). Hasil yang sudah termuat disimpan seluruhnya, sehingga checkpoint dapat sedikit melebihi batas. Batas tercapai ditandai `partial`, bukan seluruh data berhasil diambil. Delapan putaran tanpa hasil baru menghasilkan `no_new_matching_posts`; ini bukan bukti semua postingan tersedia sudah ditemukan. Pencarian X tidak menjamin kelengkapan, hasil juga dapat berubah. Tidak ada filter engagement, keyword gangguan wajib, atau skor sumber.

Rate limit, penolakan akses, pengalihan login/verifikasi, error response, atau kegagalan halaman menghentikan pencarian berikutnya. Hasil checkpoint dipertahankan. Periksa status sebelum mencoba ulang; jangan mengulang permintaan ketika akun sedang dibatasi. Tidak ada scheduler atau pengambilan browser yang dijalankan otomatis saat instalasi.

Pengujian offline:

```bash
python -m unittest discover -s tests -v
node experiments/tweet_harvest/test_greenline_collection.cjs
```
