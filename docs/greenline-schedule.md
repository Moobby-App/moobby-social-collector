# Cron Green Line di macOS

Jadwal: setiap 10 menit pada 04.00–23.50 WIB, pertama 22 Oktober 2026 pukul 04.00 WIB. Tengah malam sampai sebelum 04.00 tidak scraping. Config lokal di `data/automation/greenline/config.json` menetapkan batas pelaksanaan pertama; pemeriksaan Asia/Jakarta juga dilakukan oleh runner. Upload Supabase otomatis tersedia setelah setup credential sekali di terminal; lihat [panduan auto-upload](greenline-auto-upload.md). Sebelum setup selesai, data tetap disiapkan lokal.

Cron memanggil Python virtualenv melalui path absolut, menyiapkan PATH Node/Tesseract Homebrew, lalu menjalankan collector incremental dengan OCR cache. Tidak memerlukan VS Code/Codex terbuka. Mac perlu menyala, tidak sleep, dan memiliki sesi pengguna yang mendukung browser terlihat. Browser Chromium tetap memakai mode terlihat seperti collector yang sudah diuji; membuka jendela saat run adalah perilaku yang diharapkan. Cron tidak membangunkan Mac dan run yang terlewat karena Mac mati/sleep tidak dijalankan ulang satu per satu. Collector melanjutkan dari checkpoint saat giliran berikutnya tersedia.

Pada macOS, akses cron ke folder Documents dapat memerlukan izin privasi. Tes preflight tidak membuktikan izin daemon cron atau validitas login X. Periksa log setelah pelaksanaan pertama. Session habis/ditolak harus diperbarui manual, bukan diisi otomatis.

Runner memakai lock sehingga giliran cron tidak tumpang tindih; jika satu run lebih lama dari 10 menit, giliran berikutnya dilewati. Jangan menjalankan `collect_greenline.py` langsung bersamaan dengan cron karena lock scheduler hanya melindungi runner. Untuk run manual yang berbagi lock, gunakan `--run-now` di bawah. Batas durasi satu run adalah satu jam; jika terlampaui, process group dihentikan dan checkpoint yang sempat tersimpan tetap tersedia.

Login/verifikasi menjeda scheduler sampai dilanjutkan manual. Penolakan akses/rate limit memberi cooldown satu jam. Tiga kegagalan lainnya berturut-turut juga memberi cooldown satu jam. Karena itu scraping pada setiap giliran tidak dijamin saat ada pembatasan atau kegagalan.

Command dari root repository dengan `.venv` aktif:

```bash
python scripts/run_greenline_scheduled.py --check
python scripts/run_greenline_scheduled.py --pause
python scripts/run_greenline_scheduled.py --resume
python scripts/run_greenline_scheduled.py --run-now
```

`--check` hanya memeriksa dependency/file lokal, tidak membuka browser. `--run-now` menjalankan scraping segera walaupun di luar jam jadwal; pause/cooldown tetap dihormati. `--resume` menghapus status pause/cooldown, maka pastikan masalah akun sudah selesai dahulu.

Log masing-masing run: `data/automation/greenline/logs/`. Ringkasan terakhir: `data/automation/greenline/last_run.json`. Status pause/cooldown: `data/automation/greenline/state.json`. Hasil collection tetap di `data/greenline_sessions/`. Log launcher: `data/automation/greenline/launcher.log`. Semua berada di `data/` yang diabaikan Git. Retensi log belum otomatis; tinjau penggunaan disk bila dijalankan lama.

Lihat jadwal yang terpasang dengan `crontab -l`. Entri dikelola dalam blok komentar `MOOBBY_GREENLINE_BEGIN`/`MOOBBY_GREENLINE_END`; jangan menimpa jadwal lain saat mengubahnya.


## Jika cron gagal membaca folder Documents

Kasus terkonfirmasi 22 Oktober 2026: cron terpicu, tetapi Python gagal membaca `.venv/pyvenv.cfg` dengan `Operation not permitted`. Tidak ada `last_run.json` karena runner belum sempat dimulai. Perbaikan memerlukan izin pengguna di System Settings → Privacy & Security → Full Disk Access: tambahkan `/usr/sbin/cron` lewat tombol + dan ⌘⇧G, lalu aktifkan. Ini izin akses file terlindungi macOS, berbeda dari permission Unix dan persetujuan tool Codex. Jangan mengganti chmod proyek atau password database untuk error ini.

Setelah izin diberikan, tunggu giliran 10 menit berikutnya dan pastikan `last_run.json` muncul serta status upload pada session baru menjadi `uploaded`. Log launcher dapat tetap berisi error lama; bedakan berdasarkan adanya run baru dan timestamp. Preflight terminal saja tidak membuktikan akses daemon. Data tersimpan dalam schema `collection`, bukan `public`.
