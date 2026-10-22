# Menyiapkan batch Green Line untuk Supabase

Window awal 24 jam dan window incremental dengan checkpoint/overlap sama-sama didukung. Converter menggunakan `raw_posts.json`, manifest collection, dan `ocr_results.json` dari session. CSV asli dan input collector tidak diubah. Hash CSV OCR harus cocok dengan CSV sumber, dan CSV direkonstruksi dari JSON untuk memeriksa kaitan snapshot/OCR.

```bash
python prepare_greenline_batch.py --session data/greenline_sessions/20261021T125849425734Z
python upload_collection_batch.py --manifest data/greenline_sessions/20261021T125849425734Z/supabase/manifest.json --validate-only
```

Hasil berupa `raw.collection.v2`, maksimal 100 records per batch. Identitas yang sama dengan beberapa versi konten dipisahkan ke run lain agar seluruh versi tetap dapat diunggah. UUID run deterministik untuk preparation input yang sama; file yang berbeda tidak menimpa output existing. Ulangi file manifest yang sama bila status commit upload belum pasti.

Sumber batch adalah **`x:greenline_search`**, sebuah pencarian, bukan akun penulis. Penulis tiap postingan tetap berada di snapshot. Rentang waktu dan query lengkap disimpan sebagai JSON dalam `collection_runs.coverage`; query ID, hubungan reply/quote/conversation dan URL asli berada di `raw_snapshots.extraction_metadata`. Query ID tersebut mengacu ke daftar query di coverage. Data tidak dikelompokkan menjadi kejadian dan tidak dinyatakan terverifikasi. `media_unknown` tetap menjadi metadata ekstraksi tidak tersedia; bukan klaim bahwa gambar tidak ada. OCR tetap per foto dan berstatus `not_reviewed`. Path lokal, cookies, byte gambar, dan credentials tidak disertakan dalam batch.

Sebelum upload, pemilik project menjalankan melalui Supabase SQL Editor:

1. `db/004_snapshot_extraction_metadata.sql` jika migration ini belum diterapkan.
2. `db/005_greenline_search_source.sql` untuk mendaftarkan sumber pencarian.

Jangan membuat ulang tabel dengan migration 001 pada database tim yang sudah ada. Migration 005 hanya menambahkan sumber bila belum ada; sumber yang sengaja dinonaktifkan tidak diaktifkan ulang.

Upload manual setelah preparation/validasi dan migration siap:

```bash
python upload_collection_batch.py --manifest data/greenline_sessions/20261021T125849425734Z/supabase/manifest.json
```

Gunakan URI Session pooler dan password role tim melalui prompt tersembunyi di terminal. Tidak diperlukan tabel baru untuk konten Green Line. Ekstensi schema v2 di repository menambahkan format input dan field provenance opsional, tetap kompatibel dengan batch v2 pilot lama. Semua batch dalam manifest diunggah melalui satu transaksi uploader existing. Preparation dan validasi tidak terhubung ke cloud.
