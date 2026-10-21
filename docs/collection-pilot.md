# Pilot raw collection: CommuterLine + Jalur5

## Jalur data

`config/collection_sources.json` menetapkan dua sumber aktif, masing-masing target 20 postingan utama. Collector menggunakan pencarian Latest `from:<account> -filter:replies -filter:retweets`. Quote post masih disertakan; payload baru mencatat `post_type`. Tidak ada filter gangguan, stasiun, kebenaran, atau penilaian sumber. Daftar sumber di Supabase tetap menjadi pemeriksaan izin terakhir saat upload.

```text
Tweet Harvest → CSV sumber → foto + OCR → CSV OCR
  → prepare_harvest_batch → raw.collection.v2 JSON
  → validasi → upload satu transaksi → lima tabel collection
```

## Sekali saja pada Supabase yang sudah ada

Jalankan `db/004_snapshot_extraction_metadata.sql` di SQL Editor. Migration menambahkan satu kolom JSONB `extraction_metadata` pada `raw_snapshots`. Tidak membuat ulang tabel, menghapus data, atau mematikan RLS. Grant INSERT/SELECT tingkat tabel yang sudah ada tetap berlaku. Jangan menjalankan ulang 001.

Batch JSON lama `raw.collection.v1` tetap dapat digunakan. Uploader memilih schema berdasarkan versi. Batch v2 memerlukan migration 004 dan gagal secara eksplisit bila kolom belum tersedia.

## Penggunaan

```bash
python collect_pilot.py --collector-label yuki
```

Menjalankan kedua akun secara berurutan, termasuk OCR, lalu membuat folder baru `data/collection_sessions/<timestamp>/`. Isinya batch JSON per akun dan `manifest.json`. Collector memakai session milik anggota yang menjalankan, bukan session yang dibagikan. `--no-ocr` tersedia jika hanya membutuhkan caption dan metadata gambar. Dependency OCR dijelaskan di `docs/ocr.md`.

Jika satu collection gagal, akun berikutnya tidak dipanggil lagi. Hasil yang sudah disiapkan tetap dicatat sebagai manifest parsial; periksa sebelum upload. Target 20 adalah batas sampel per akun, belum pemantauan incremental dengan jaminan cakupan.

Periksa format lokal:

```bash
python upload_collection_batch.py --manifest "data/collection_sessions/<timestamp>/manifest.json" --validate-only
```

Upload menggunakan manifest yang sama:

```bash
python upload_collection_batch.py --manifest "data/collection_sessions/<timestamp>/manifest.json"
```

URI Session pooler dan password dimasukkan lewat prompt tersembunyi di terminal. Tidak disimpan dalam JSON, config, atau Git. Gunakan role database tim yang sudah disiapkan, bukan anon key. Kedua batch diunggah dalam satu transaksi: jika salah satunya gagal, perubahan transaksi ini di-rollback. Upload yang commit-nya belum pasti diulang memakai manifest/batch yang sama.

## Pemetaan ke lima tabel

| Tabel | Data |
|---|---|
| `sources` | Registry `x:commuterline` dan `x:jalur5_` yang sudah ada |
| `collection_runs` | Satu run per akun; pelaksana, waktu, tool, target, jumlah, batas cakupan |
| `raw_contents` | Identitas unik platform + ID postingan; URL dan first/last seen |
| `raw_snapshots` | Caption, waktu publikasi, media, OCR per gambar, metadata ekstraksi, hash |
| `content_observations` | Hubungan run ↔ konten ↔ snapshot; waktu pengamatan dan jenis postingan |

`raw_text` hanya caption. `media[].ocr.raw_text` adalah teks mesin per gambar; bukan caption dan bukan verifikasi. OCR menyimpan hash gambar, engine, bahasa, PSM, hash model, waktu proses, status, dan `not_reviewed`. Tidak ada cookie, path gambar lokal, atau byte gambar yang diunggah ke PostgreSQL. Gambar asli dan CSV tetap lokal; URL media dapat berubah/tidak tersedia kemudian. Cloud Storage untuk arsip gambar belum dibuat.

Timestamp publikasi dari payload browser menggunakan basis `page_timestamp`, bukan `dom_datetime`. CSV tidak mencatat waktu setiap item diterima; `captured_at` memakai waktu selesai collection dari run_info, dengan `time_basis=collector_clock`. Ini bukan waktu upload atau waktu publikasi.

## Pencegahan duplikasi

1. **Di dalam satu CSV:** ID identik + isi identik dilipat menjadi satu record. ID sama + isi berbeda ditolak untuk diperiksa.
2. **Antar-run/anggota:** UNIQUE `(platform, external_id)` pada raw_contents memastikan satu identitas konten. Kendala database melindungi upload yang bersamaan; bukan sekadar pengecekan file lokal.
3. **Versi isi:** UNIQUE `(raw_content_id, capture_hash)` pada raw_snapshots menggunakan ulang isi yang sama. Perubahan caption, media, OCR, model, atau metadata ekstraksi dapat membuat snapshot baru. Like/reply count, waktu pengambilan, path lokal, dan waktu proses OCR tidak ikut fingerprint. Waktu OCR yang tersimpan adalah dari snapshot pertama dengan fingerprint tersebut.
4. **Retry upload:** run UUID deterministik untuk hasil preparation yang sama. Uploader memverifikasi metadata dan observasi; retry identik tidak menambah baris. `(run_id, raw_content_id)` juga unik. Run baru yang menemukan postingan lama membuat observasi baru, bukan identitas baru.

Migration dari adapter lama/DOM ke v2 dapat menambah satu snapshot karena provenance/representasinya berbeda. Itu bukan duplikasi identitas dan bukan bukti penulis mengedit postingan.

Dua postingan berbeda dengan isi sama tetap disimpan sebagai dua sumber. Menggabungkan laporan tentang kejadian yang sama adalah pekerjaan kurasi, bukan collection.

## Yang belum dilakukan

Belum ada scheduler, incremental checkpoint, jaminan seluruh riwayat, merge kejadian, cloud image archive, atau publication ke app. Pengujian offline dan collection lokal tidak membuktikan upload cloud berhasil. Upload baru dianggap berhasil setelah transaksi dikonfirmasi uploader.

## Hasil pengujian implementasi

21 Oktober 2026: CommuterLine 20 postingan, Jalur5 20 postingan; batch v2 lolos validasi lokal. 38 unit tests lulus. Integration test memakai PostgreSQL 18 sementara dengan Unix socket saja, terpisah dari Supabase: v1 sebelum migration, v2 setelah migration, retry identik, dua anggota/run berbeda, konkurensi, perubahan snapshot, rollback lintas batch, migration berulang, dan penolakan akses anon lulus. Server sementara ditutup dan datanya dibersihkan setelah test. Belum ada konfirmasi upload ke cloud.
