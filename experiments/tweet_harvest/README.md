# Uji pembanding Tweet Harvest 2.7.1

Tujuan: membandingkan 20 hasil pencarian dari akun CommuterLine dengan collector DOM Python yang sudah ada. Bukan mengganti collector atau mengunggah ke database.

Jalankan dari root repository setelah dependensi disiapkan:

```bash
python try_tweet_harvest.py --check
python try_tweet_harvest.py --account commuterline --limit 20
```

Tool meminta auth_token melalui prompt password tersembunyi. Ambil Value auth_token akun sendiri dari Chrome yang sudah login: DevTools > Application > Cookies > https://x.com. Jangan gunakan opsi --token pada command line karena dapat masuk shell history atau process arguments. Jika diminta format, pilih CSV (Enter pada pilihan default).

Uji memakai tab LATEST dan query `from:commuterline -filter:replies -filter:retweets`, untuk mengecualikan balasan dan repost tanpa filter kata gangguan. Hasil pencarian tidak menjamin seluruh postingan akun terambil; bisa berbeda dari tab profile. Target 20 dari tool tidak menjamin tepat 20 baris atau 20 ID unik, sehingga wrapper melaporkan keduanya.

Output per run: `data/tweet_harvest/<account>_<timestamp>/tweets-data/*.csv`, ditambah `run_info.json` yang hanya memuat metadata nonrahasia. Periksa field full_text, created_at, id_str, username, tweet_url, dan image_url. URL gambar hanya yang tersedia dari export tool; bukan semua media/hasil OCR. Jangan menganggap text full_text menjamin semua long posts lengkap.

## Instalasi ulang untuk anggota lain

Dari root repository:

```bash
npm --prefix experiments/tweet_harvest ci --ignore-scripts
python try_tweet_harvest.py --install-browser
python try_tweet_harvest.py --check
```

Instalasi ignore-scripts sengaja memisahkan download browser dari npm. Browser uji disimpan di `experiments/tweet_harvest/.browsers/`, sehingga tidak memakai/menghapus cache browser Python. Jangan commit node_modules, .browsers atau hasil tweets-data.

Jika muncul pembatasan/login challenge, hentikan dengan Ctrl+C dan jangan mengulang login terus-menerus. Wrapper memberi batas total 10 menit karena mekanisme retry internal tool. Pemeriksaan instalasi tidak membuktikan scraping berhasil; lihat run_info.json untuk hasil setiap pengambilan.

Sumber paket: https://github.com/helmisatria/tweet-harvest . CLI upstream menggunakan Playwright dan cookie login; tidak memerlukan X developer API key.

## Runtime pada macOS ini

Chromium bawaan Playwright 1.41.1 langsung tertutup pada uji lokal. Eksperimen mempertahankan Tweet Harvest 2.7.1, dengan npm overrides Playwright 1.60.0 untuk mencoba kompatibilitas browser. Ini konfigurasi uji Moobby, bukan dependency default upstream. Tidak mengubah paket Python.

Wrapper juga melaporkan jumlah penanda balasan pada kolom in_reply_to_screen_name dan penanda RT pada teks. Jika masih terdeteksi, hasil tetap disimpan untuk diperiksa, bukan diam-diam dihapus atau dianggap seluruhnya postingan utama.

## Ekstraksi teks panjang

Launcher menyiapkan `node_modules/tweet-harvest/dist/moobby-bin.js` dari entry point paket 2.7.1; file upstream `bin.js` tidak diubah. Adapter yang bisa dibagikan berada di `text_extraction.cjs`, dengan pembuat entry point di root `prepare_tweet_harvest.py`. Jika struktur upstream berubah, proses berhenti agar patch tidak diterapkan pada lokasi yang salah. Instalasi ulang npm tetap dapat dipakai karena entry point dibuat kembali saat launcher dijalankan.

Prioritas teks: `note_tweet.note_tweet_results.result.text` yang tidak kosong, kemudian `legacy.full_text`. Mendukung hasil tweet langsung dan pembungkus `result.tweet`, serta tidak mengambil teks quoted post sebagai teks utama.

CSV menambahkan `legacy_full_text`, `text_extraction_method` (`note_tweet` / `legacy.full_text`), dan `text_completeness`. Kehadiran teks panjang bukan verifikasi independen kelengkapan; status tetap `unknown`, atau `truncated` bila fallback secara eksplisit ditandai terpotong. Setiap run mempunyai folder baru dan tidak menimpa CSV sebelumnya.

Untuk memakai session lokal yang sudah disimpan oleh collector Python:

```bash
python try_tweet_harvest.py --account commuterline --limit 20 --use-saved-session
```

Opsi ini membaca auth_token langsung di proses Node dari file private `playwright/.auth/x_reused_session.json`; token tidak dimasukkan ke argumen command atau metadata. Opsi tanpa flag tetap menggunakan prompt token tersembunyi. Jangan membagikan file session kepada anggota tim.

Uji lokal 21 Oktober 2026: 20 ID unik, 4 memakai note_tweet, 16 fallback, nol penanda balasan/RT. Teks KA 1344 cocok persis dengan teks halaman yang diperiksa (504 karakter dibanding preview 279). Ini tidak menyatakan seluruh postingan telah diverifikasi lengkap. Search LATEST berubah mengikuti postingan baru, sehingga batch baru tidak selalu memiliki ID yang sama dengan batch sebelumnya.

## Seluruh gambar pada postingan

Adapter memprioritaskan daftar `legacy.extended_entities.media`, dengan fallback ke `legacy.entities.media`. CSV menambahkan `media_json`, `image_urls_json`, `media_count`, `image_count`, dan `media_extraction_basis`. `image_url` lama tetap dipertahankan untuk kompatibilitas; gunakan `image_urls_json` untuk semua foto yang tersedia dalam respons. Daftar tersebut hanya mencakup lampiran postingan utama; quoted post, link preview, dan avatar tidak dicampurkan. Video/GIF dicatat sebagai media dengan thumbnail terpisah, bukan sebagai foto untuk OCR.

Setiap CSV baru mempunyai file pendamping `.media.json` berisi ID postingan, URL sumber, dan daftar media terstruktur. Urutan media serta ID, ukuran, dan alt text dicatat jika tersedia. Respons X dapat tidak menyertakan semua data: basis `entities` atau `not_available` tidak boleh dianggap bukti seluruh lampiran berhasil dikumpulkan. Tahap ini belum mengunduh gambar, membaca OCR, atau mengunggah ke database.

Uji adapter: `node experiments/tweet_harvest/test_media_extraction.cjs`.

## Uji perbandingan profil dan pencarian

`compare_sources.cjs` adalah alat audit terpisah, belum menggantikan launcher collection utama. Menggunakan session lokal, memantau respons SearchTimeline dan UserOriginalsTimeline/UserTweets, serta memakai adapter teks/media yang sama. Akun penulis, balasan, repost, dan rentang waktu difilter dengan aturan yang sama. Quote post tetap boleh masuk, tetapi isi quoted post tidak dicatat sebagai postingan utama.

Output di `data/source_comparisons/<account>_<timestamp>/`: `search.json`, `profile.json`, dan `comparison.json`. Rentang tetap 24 jam sampai waktu mulai pengujian, target default 20 postingan per jalur. Postingan pinned mengikuti filter waktu yang sama. Kondisi berhenti profil memperhitungkan ujung halaman, karena respons awal dapat memuat banyak data meskipun hanya beberapa kartu terlihat.

Ini audit sampel dengan batas scrolling; selisih tidak otomatis berarti postingan hilang dari X, dan hasil sama tidak membuktikan kelengkapan seluruh riwayat atau kecepatan pembaruan. Alat ini belum mengukur keterlambatan pengindeksan pencarian. Tidak menjalankan OCR atau upload database.

Pengujian offline: `node experiments/tweet_harvest/test_compare_sources.cjs`.
