# Upload Supabase otomatis setelah scraping

Cron tetap setiap 10 menit pada 04.00–23.50 WIB. Setelah collection sukses, runner membuat batch v2 lokal dan memproses antrean upload. Batch Supabase memakai sumber `x:greenline_search`, menyimpan data mentah, dan tidak mengelompokkan kejadian.

## Setup sekali di terminal lokal

Pastikan migration `db/004_snapshot_extraction_metadata.sql` dan `db/005_greenline_search_source.sql` sudah diterapkan di project Supabase existing. Jangan membuat ulang tabel melalui 001.

```bash
python scripts/configure_supabase_upload.py
```

Masukkan URI Session pooler dan password role database tim pada prompt tersembunyi. Setup memeriksa koneksi secara read-only, metadata extraction, sumber aktif, serta hak baca/tambah/update yang dibutuhkan uploader. Setelah berhasil, credential disimpan di `data/automation/greenline/supabase_connection.json` dengan izin 0600 (hanya pemilik file), dan upload otomatis diaktifkan di config scheduler. File tidak di-commit Git; log tidak menyimpan password atau connection string. Password disimpan lokal agar cron dapat terhubung tanpa prompt. Jangan mengirim credential lewat chat. Rotasi credential dengan menjalankan setup lagi.

Sebelum setup berhasil, scraping dan preparation tetap berjalan; upload berstatus `setup_required`. Tidak ada prompt password dari cron. File yang sudah dipersiapkan, termasuk session awal, menjadi antrean upload setelah konfigurasi siap. Setup sendiri tidak mengunggah data.

Pemeriksaan ulang koneksi (read-only):

```bash
python scripts/configure_supabase_upload.py --check
```

Upload antrean sekarang tanpa menjalankan scraping:

```bash
python scripts/configure_supabase_upload.py --retry-pending
```

Command tersebut berbagi lock dengan cron agar tidak menulis receipt bersamaan. Credential yang tidak tersedia/izin terlalu terbuka ditolak sebelum koneksi. Manual uploader lama tetap bisa memakai prompt tanpa menyimpan credential.

## Retry dan transaksi

Antrean berasal dari `data/greenline_sessions/*/supabase/manifest.json`. Maksimal lima manifest dicoba setiap giliran, dimulai dari yang paling lama. Semua batch dalam satu manifest disimpan dalam satu transaksi. `upload_status.json` di sebelah manifest hanya ditandai `uploaded` setelah commit dikonfirmasi; berisi fingerprint file, run ID, jumlah percobaan, waktu, dan hasil uploader.

Jika koneksi terputus saat commit, file tetap pending/failed dan diulang dengan UUID run yang sama. Uploader existing memverifikasi run serta observasi agar retry tidak menggandakan data. File uploaded yang berubah ditolak. Manifest yang berhasil tidak dikirim ulang. Batch gagal tetap disimpan lokal; upload gagal tidak memajukan/mengubah isi batch atau menghentikan pengambilan berikutnya. Kegagalan database dicatat hanya sebagai nama tipe error dan SQLSTATE, tanpa pesan yang mungkin berisi credential.

`data/automation/greenline/last_run.json` mencatat status collection dan bagian `upload` terpisah. Status scraping sukses bukan berarti upload sukses; periksa `upload_status.json`. Collection parsial atau OCR gagal tidak otomatis dibuatkan batch baru, tetapi hasil mentah/checkpoint tetap tersedia untuk diperiksa. Antrean lama tetap dapat diproses pada giliran yang gagal scraping selama scheduler tidak sedang pause/cooldown. Pause/cooldown scheduler juga menunda pemrosesan antrean; `--retry-pending` dapat dipakai terpisah setelah memeriksa masalahnya.
