# Moobby Social Collector

Collection layer untuk mengumpulkan postingan X, menyiapkan batch raw content, dan mengunggahnya ke PostgreSQL bersama di Supabase.

**Status:** collector X dengan session lokal dan batas 20 postingan sudah digunakan. Pengujian koneksi Supabase sudah berhasil. Uploader tersedia; pengunggahan pertama ke cloud masih perlu dikonfirmasi oleh operator. LLM, pengambilan Threads/RSS yang terintegrasi, dan GraphQL bukan bagian alur aktif repository ini.

## Alur utama

```text
Session X milik masing-masing anggota
  -> collect_public_x.py
  -> data/x_<account>_posts.json
  -> prepare_batch.py
  -> data/raw_batches/<batch>.json
  -> upload_collection_batch.py
  -> Supabase / schema collection
```

Collector menyimpan konten mentah; tidak menyatakan bahwa laporan benar atau gangguan masih berlangsung. Kurasi keyword lama tersedia sebagai eksperimen terpisah.

## 1. Persiapan teman yang baru bergabung

- Git, VS Code, dan Google Chrome terpasang.
- Disarankan Python 3.11; alur existing menggunakan Python 3.9. Dependency Psycopg dibatasi untuk kompatibilitas tersebut.
- Akses repository GitHub dari pemilik organisasi jika repository private.
- Akun X sendiri yang dapat login melalui Chrome biasa.
- Akses database individual dari pemilik project untuk tahap upload. Tidak diperlukan untuk sekadar mengambil data ke JSON.

Clone repository dan buka foldernya di VS Code:

```bash
git clone https://github.com/Moobby-App/moobby-social-collector.git
cd moobby-social-collector
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Script utama memakai `channel="chrome"`, sehingga Google Chrome harus tersedia pada mesin. Instal Chromium bawaan Playwright saja tidak menggantikan kebutuhan ini. Browser akan terlihat selama pengumpulan; jangan menjalankannya sebagai job headless tanpa meninjau ulang kebutuhan.

## 2. Siapkan session X sendiri

1. Login ke X di Chrome biasa menggunakan akun Anda.
2. Buka DevTools > Application > Storage > Cookies > https://x.com.
3. Jalankan:

   ```bash
   python test_x_existing_session.py
   ```

4. Tempel Value `auth_token` dan `ct0` ke prompt terminal lokal. Karakter disembunyikan.
5. Pastikan browser membuka Home dan terminal menyatakan login terkonfirmasi.

Session disimpan di `playwright/.auth/x_reused_session.json`, hanya pada mesin Anda dan dikecualikan dari Git. Jangan mengirim cookie, file session, atau browser profile melalui GitHub/chat. Keberhasilan sebelumnya tidak menjamin session selalu berlaku; jika ditolak, hentikan percobaan berulang dan periksa akun secara manual.

## 3. Ambil 20 postingan

```bash
python collect_public_x.py --account commuterline --limit 20
```

Hasil: `data/x_commuterline_posts.json`. Script scroll bertahap, dedup berdasarkan post ID, menyimpan timestamp DOM bila tersedia, dan berhenti ketika target tercapai, tidak ada tambahan, atau pembatasan terdeteksi. Hasil lama dibackup.

Keterbatasan yang perlu diketahui:

- Jumlah 20 adalah batas postingan unik yang termuat, bukan cakupan seluruh timeline atau 20 berita relevan.
- Saat ini masih mengambil `raw_card_text`, yang dapat mencakup label UI dan angka engagement.
- Teks panjang belum otomatis diekspansi. Converter memberi label truncated/unknown, bukan mengklaim teks lengkap.
- Media belum diekstrak; tidak dilakukan OCR.
- Pinned/reply/repost/quote belum sepenuhnya dibedakan. Halaman profile tidak menjamin seluruh balasan operator ikut terbaca.
- Jeda scroll membantu pemuatan konten; bukan jaminan terhindar dari deteksi otomatisasi.

## 4. Bentuk batch raw

Ganti `nama_anda` dengan label anggota. Gunakan nama file output baru untuk pengambilan baru; jangan menimpa batch yang sudah diupload.

```bash
python prepare_batch.py --input data/x_commuterline_posts.json --source-key x:commuterline --collector-label nama_anda --output data/raw_batches/commuterline_run01.json
```

Converter mempertahankan teks asli, menghapus field kurasi legacy dari kontrak upload, menyimpan waktu yang belum pasti sebagai null, dan menghasilkan run UUID. **Label anggota hanya atribusi, bukan mekanisme login.**

Validasi lokal, tanpa koneksi cloud:

```bash
python upload_collection_batch.py --input data/raw_batches/commuterline_run01.json --validate-only
```

Kontrak: [raw_batch.schema.json](raw_batch.schema.json). Penjelasan: [docs/raw-fields.md](docs/raw-fields.md).

## 5. Uji akses dan upload Supabase

Minta pemilik project menyiapkan DB login individual sesuai [docs/team-access.md](docs/team-access.md). Jangan memakai password postgres pemilik bersama-sama.

```bash
python test_collection_connection.py
```

Prompt meminta URI Session pooler dan database password. Placeholder `[YOUR-PASSWORD]` boleh dibiarkan dalam URI karena password diminta terpisah. Input disembunyikan dan tidak disimpan. Pastikan 5 tabel serta 3 sumber terlihat.

Kemudian:

```bash
python upload_collection_batch.py --input data/raw_batches/commuterline_run01.json
```

Uploader menyimpan satu run, identitas konten, snapshot dan observasi dalam satu transaksi. **Ulangi FILE BATCH YANG SAMA bila upload gagal atau hasil commit belum pasti**; run UUID yang sama diperiksa supaya retry tidak menggandakan baris. Jangan membuat ulang batch hanya untuk mengulang upload.

## Sumber pilot

| Source key | Profil | Konfigurasi awal |
|---|---|---|
| x:commuterline | @CommuterLine | Aktif, prioritas 1 |
| x:jalur5_ | @jalur5_ | Aktif sebagai pilot, prioritas 2 |
| x:jalurserpong | @JalurSerpong | Nonaktif sampai pengambilan ulang dengan session login diperiksa |

Registry sumber cloud berada di `collection.sources`. Flag aktif mengendalikan uploader; collector CLI saat ini masih dapat mengambil akun yang dipilih manual. Tidak ada scheduler atau klaim sumber paling cepat/selalu benar.

Untuk Jalur5: jalankan collector dengan `--account jalur5_`, lalu converter dengan `--source-key x:jalur5_` dan input `data/x_jalur5__posts.json`.

## Struktur repository

```text
collect_public_x.py               collector utama X
test_x_existing_session.py        membuat session lokal dari login milik sendiri
prepare_batch.py                 converter JSON existing -> kontrak raw
raw_batch.schema.json            format batch collection
test_collection_connection.py    uji database read-only
upload_collection_batch.py       upload transaksional dan idempotent
db/                              migration, seed sumber, query inspeksi
docs/                            format data dan akses tim
tests/                           pengujian tanpa browser/cloud/credential
scripts/check_repository.py      periksa file Git dan pola secret sebelum push
```

`login_x.py`, `login_persistent_x.py`, `test_firefox_login.py`, `debug_login_x.py`, dan `check_public_x.py` adalah script eksperimen/diagnostik lama; bukan langkah setup yang direkomendasikan. Sebagian langsung membuka browser jika dijalankan. `curate_x.py` / `post_dates.py` merupakan kurasi keyword legacy, bukan jalur raw upload atau LLM.

## Tes dan kontribusi

```bash
python -m unittest discover -s tests -v
python scripts/check_repository.py --tracked
```

Tes tidak login X, tidak membuka browser, tidak membaca secret, dan tidak menulis database cloud. GitHub Actions menjalankan tes offline serta pemeriksaan file. Tidak ada credential CI yang perlu ditambahkan untuk tes ini.

Saat membuat perubahan, gunakan branch dan PR. Sebelum commit/push, periksa stage:

```bash
git status --short
python scripts/check_repository.py --staged
```

Jangan commit `.venv/`, `data/`, `.env`, `playwright/.auth/`, screenshots debug, HAR, atau connection string berpassword. Scanner membantu mendeteksi pola umum; ia bukan jaminan semua secret terdeteksi.

## Menyiapkan database baru (pemilik project saja)

Pada project Supabase baru, jalankan `db/001_collection_schema.sql` lalu `db/002_sources.sql` dari SQL Editor. **Jangan menjalankan migration 001 lagi pada project tim yang sudah memiliki tabel.** Anggota baru cukup terhubung ke database yang sama.

Migration memakai schema privat `collection` dan RLS. `db/003_inspect.sql` adalah query baca untuk memeriksa hasil. Supabase Free memiliki kuota dan dapat pause setelah tidak aktif; biaya/limit diperiksa pada https://supabase.com/pricing . Database Free bukan backup otomatis; simpan export sesuai kebutuhan tim.
