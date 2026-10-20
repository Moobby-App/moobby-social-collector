# Data dictionary: collection only

Versi kontrak: `raw.collection.v1`. Field schema berbeda dengan atribut hasil kurasi. Tidak ada pemeriksaan benar/salah atau relevansi berita di tahap ini.

## 1. sources - satu konfigurasi profil/feed

| Field | Tipe / ketentuan | Arti |
|---|---|---|
| id | UUID, dibuat DB | ID internal sumber |
| source_key | text, unique | Kunci stabil, misalnya x:commuterline |
| platform | x / threads / rss / web | Saluran asal |
| source_url | HTTPS URL | Profil/feed/halaman yang dibuka |
| display_name | text | Nama sumber untuk tim |
| handle | text / null | Username profil; feed RSS boleh null |
| source_kind | operator / community / media / other | Jenis pemilik sumber, bukan skor kebenaran |
| enabled | boolean | Apakah sudah disetujui tim untuk pengambilan |
| priority | 1-3 | Urutan kerja collector; bukan skor akurasi |
| page_kind | profile / replies / feed / page | Bagian sumber yang akan diambil jika adapter mendukung |
| target_count | 1-100, default 20 | Batas awal per run |
| collection_mode | manual / scheduled | Awalnya manual; belum ada scheduler |
| purpose / setup_notes | text | Alasan memantau dan batas/kendala akses |

## 2. collection_runs - satu sesi pengambilan satu sumber

| Field | Tipe / ketentuan | Arti |
|---|---|---|
| id | UUID dari client | Idempotency key; upload ulang batch sama memakai ID sama |
| source_id | UUID FK sources | Profil/feed yang dipantau |
| collector_label | text | Nama anggota/mesin yang menjalankan; self-declared, bukan auth identity |
| collector_version | text | Versi collector atau adapter untuk menelusuri masalah ekstraksi |
| collection_method | playwright / rss / manual | Metode pengambilan |
| started_at / finished_at | timestamptz / nullable finish | Waktu run yang benar-benar tercatat |
| time_basis | collector_clock / item_capture_range | File lama tidak mencatat run clock; rentang item hanya pendekatan yang ditandai |
| run_status | running / completed / partial / failed / unknown | Hasil pengambilan, bukan hasil kurasi |
| stop_reason | text / null | Misalnya target_reached, no_new_posts, rate_limited |
| coverage | text | Misalnya loaded_items_only; tidak mengklaim seluruh timeline |
| complete_history | boolean | false untuk koleksi kartu timeline terbatas |
| requested_count | integer / null | Target dari config; null jika file lama tidak punya informasi |
| captured_count | integer | Jumlah item unik dalam batch |
| recorded_at | DB time | Waktu batch masuk ke cloud, tidak sama dengan waktu browser membaca |

## 3. raw_contents - satu identitas postingan/artikel

| Field | Tipe / ketentuan | Arti |
|---|---|---|
| id | UUID DB | ID internal konten |
| platform | enum | Platform identitas konten |
| external_id | text | ID platform/GUID. ID X disimpan string agar tidak kehilangan digit |
| canonical_url | HTTPS URL | URL post/artikel, tanpa suffix photo/video pada X |
| first_seen_at / last_seen_at | timestamptz | Rentang item pernah ditangkap oleh tim |

Unique(platform, external_id). Satu post yang sama pada beberapa profil/anggota tetap satu row. RSS tanpa GUID memakai aturan ID berbasis canonical URL pada adapter RSS; adapter tersebut belum dibuat.

## 4. raw_snapshots - isi hasil tangkapan yang dipertahankan

| Field | Tipe / ketentuan | Arti |
|---|---|---|
| id / raw_content_id | UUID / FK | Snapshot terhubung ke identitas konten |
| capture_hash | SHA256 hex | Identitas hasil tangkapan berdasarkan field snapshot, bukan timestamp pengambilan |
| author_handle | text / null | Penulis dari permalink/DOM yang tersedia; dapat berbeda dengan sources.handle |
| author_display_name | text / null | Nama tampilan asli jika diambil; jangan mengarang dari handle |
| title | text / null | Judul artikel jika ada; post X boleh null |
| raw_text | text, boleh kosong | Teks persis hasil tangkapan; media-only boleh tanpa teks |
| text_kind | post_text / article_text / feed_excerpt / card_text / none | Apakah isi utama atau seluruh kartu yang tercampur UI |
| text_completeness | full / truncated / unknown | Fakta kualitas ekstraksi; bukan kualitas atau kebenaran berita |
| published_at | timestamptz / null | Timestamp sumber yang terbaca pasti; null jika hanya label tanpa timestamp |
| published_at_basis | dom_datetime / feed_timestamp / page_timestamp / unknown | Bukti asal tanggal; unknown jika published_at null |
| media | JSON array | Hanya media yang terlihat dan diambil; tidak memuat cookies/headers |
| media_capture_status | captured / not_attempted / failed | [] + not_attempted berarti belum memeriksa, bukan tidak ada gambar |
| captured_at | timestamptz | Pertama kali representasi ini ditangkap; pengamatan ulang memiliki waktu sendiri |
| recorded_at | DB time | Waktu snapshot masuk cloud |

Setiap item media memakai `{type, url, alt_text, thumbnail_url}`. `type`: image/video/unknown; URL HTTPS; alt dan thumbnail boleh null. Pada tahap awal tidak download file media, tidak melakukan OCR, dan tidak menyimpulkan gambar kosong karena media belum diekstrak.

Unique(raw_content_id, capture_hash). Snapshot tidak diedit oleh role pengunggah. Tampilan terpotong vs lengkap dapat menghasilkan dua snapshot meski postingan sumber tidak diedit. Bandingkan hanya waktu/kualitas ekstraksi pada tahap ini; pemilihan snapshot untuk LLM dibahas di milestone berikutnya.

## 5. content_observations - kemunculan pada suatu pengambilan

| Field | Tipe / ketentuan | Arti |
|---|---|---|
| id | UUID DB | ID pengamatan |
| run_id | UUID FK | Menunjukkan sumber yang dipantau dan anggota yang menjalankan |
| raw_content_id / snapshot_id | UUID FK | Konten dan snapshot yang terlihat pada run tersebut |
| captured_at | timestamptz | Waktu pengamatan ini; tidak perlu mengubah snapshot lama |
| published_label | text / null | Label asli seperti 1j, 18 Okt, atau 16 Sep |
| position_in_run | integer / null | Urutan item saat pertama terlihat pada run; bukan urutan kronologis pasti |
| is_pinned | boolean / null | true jika marker pinned terbaca, false jika diperiksa tidak pinned, null jika belum diperiksa |
| post_type | original / reply / repost / quote / unknown | unknown hingga collector benar-benar mengenali konteks |
| related_urls | JSON array | URL konten kutipan/reply yang benar-benar terbaca; jangan menebak |

Unique(run_id, raw_content_id). Foreign key memastikan snapshot memang milik konten yang sama. Tidak semua card berarti postingan asli akun yang sedang dipantau.

## Batas input

- Jangan mengunggah cookies, browser session, authorization headers, API keys atau connection strings.
- Jangan memasukkan ringkasan buatan, kategori gangguan, penilaian verified, koordinat tebakan, atau tanggal hasil inferensi ke field raw.
- Jangan membuang post lama, imbauan, repost, atau pinned karena dianggap tidak relevan. Simpan metadata yang tersedia; keputusan relevansi berada di milestone lain.
- Pembatasan jumlah konten / sumber pilot merupakan batas pengambilan, bukan filter makna berita.
- Payload JSON per row tidak disimpan sebagai batch raksasa; uploader membagi sesuai tabel dalam satu transaksi. Pengunggahan pertama ke cloud tetap perlu dikonfirmasi operator.
