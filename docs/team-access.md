# Akses teman ke repository dan database

Ada tiga akses berbeda:

1. **GitHub collaborator/team**: clone/push kode. Diatur pemilik organisasi Moobby-App.
2. **Supabase organization member**: akses dashboard melalui akun Supabase masing-masing. Ini tidak otomatis membuat login SQL untuk script.
3. **DB login individual**: dipakai uploader Python. Setiap anggota memakai password sendiri, terpisah dari password pemilik dan akun Supabase.

Session X juga dibuat sendiri oleh masing-masing anggota. Tidak ada secret yang perlu ditempel di repository atau dikirim ke chat ini.

## Pemilik project: buat DB login per anggota

Di SQL Editor project existing, ganti nama role dan placeholder password secara lokal. Gunakan nama role lowercase berawalan `collector_`, password unik yang kuat, dan jangan commit SQL yang sudah berisi password asli.

```sql
CREATE ROLE collector_budi WITH LOGIN PASSWORD '[GANTI_PASSWORD_UNIK_SECARA_LOKAL]';
GRANT moobby_collection_team TO collector_budi;
GRANT CONNECT ON DATABASE postgres TO moobby_collection_team;
```

`collector_budi` hanya contoh. Jalankan sekali per nama anggota. Migration awal sudah memberi group `moobby_collection_team` hak baca/tambah tabel collection serta update metadata tertentu. Group tidak mendapat hak delete, edit snapshot, atau akses anon/mobile. Pemilik dan SQL Editor admin memiliki hak yang lebih luas.

## Anggota: gunakan Session pooler

Ambil format URI Session pooler dari Connect. Ganti bagian user `postgres.PROJECT_REF` menjadi `collector_budi.PROJECT_REF`, pertahankan host/port/database yang diberikan dashboard. Jangan menebak region/host.

```text
postgresql://collector_budi.PROJECT_REF:[YOUR-PASSWORD]@POOLER_HOST:5432/postgres
```

Tempel URI pada prompt `test_collection_connection.py`; masukkan password **role collector_budi** saat diminta, bukan password postgres pemilik. Placeholder password boleh tetap berada di URI; script mengirim password sebagai parameter terpisah.

Setelah uji read-only berhasil, gunakan URI yang sama untuk uploader. Nama `--collector-label budi` hanya label audit yang diisi sendiri; hak akses berasal dari DB login.

Apabila teman hanya perlu melihat dashboard, invite dari organisasi Supabase dengan role Developer yang sesuai kebutuhan. Free memberi role tingkat organisasi, sehingga gunakan organization khusus Moobby. Jangan memberi Owner hanya agar dapat mengunggah konten. Jangan meminta anggota menjalankan schema migration pada project existing.

Referensi: https://supabase.com/docs/guides/database/postgres/roles dan https://supabase.com/docs/guides/platform/access-control . Dukungan koneksi pooler disesuaikan dengan informasi Connect pada project.
