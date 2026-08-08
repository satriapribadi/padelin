# Padelin

> Jadwal meet, beres.

Web app lokal untuk menyusun jadwal meet padel: Americano, pool rating, Mexicano,
pasangan tetap, dan format bersegmen (putra / putri / mixed) — lengkap dengan
pembagian tugas wasit & ballboy, analisa biaya, laporan siap cetak, dan database.

**Nol dependency.** Cukup Python 3.10+, tanpa `pip install` apa pun.

```bash
python run.py
```

Lalu buka <http://127.0.0.1:8770> (browser terbuka otomatis).
Ganti port dengan `--port 9000`, matikan auto-open dengan `--no-browser`.

---

## Masalah yang diselesaikan

Menyusun jadwal Americano yang adil itu bukan soal ketelitian, tapi soal
kombinatorik. Tiap ronde seorang pemain dapat **1 partner dan 2 lawan**, jadi:

| Syarat | Batas |
|---|---|
| Partner 100% unik | ronde main per orang ≤ `N - 1` |
| Lawan 100% unik | ronde main per orang ≤ `(N - 1) / 2` |

Artinya untuk 8 pemain, lawan unik hanya mungkin sampai **3 ronde**. Ronde ke-4
dan seterusnya pasti mengulang — itu batas matematis, bukan kelemahan algoritma.
App ini menghitung batas tersebut di muka dan memberitahu sebelum kamu terlanjur
menjanjikan sesuatu ke peserta.

Yang sering disalahpahami: **grup besar justru lebih mudah**. Dengan 26 pemain di
4 court, banyak yang duduk bergiliran sehingga tiap orang cuma main ~6 ronde —
jauh di bawah batas 12, jadi keunikan penuh tercapai. Yang sulit adalah grup
kecil dengan durasi panjang.

## Cara kerja

Tiga lapis yang terpisah rapi:

1. **Konstruksi eksak** — 1-factorization (circle method) pada graf lengkap
   menjamin setiap kombinasi partner muncul tepat sekali, secara struktural.
   Untuk babak mixed dipakai rotasi Latin square pada graf bipartit.
2. **Optimasi** — simulated annealing dengan evaluasi delta O(1) merapikan siapa
   lawan siapa, giliran istirahat, dan keseimbangan rating.
3. **Batas keras** — aturan gender, partner terkunci, dan pemisahan pool rating
   ditegakkan dengan menolak gerakan ilegal, bukan lewat penalti. Jadwal yang
   keluar mustahil melanggarnya.
4. **Perataan jumlah main** — pass deterministik setelah optimasi. Selama masih
   ada pemain yang main dua ronde lebih banyak daripada yang lain dan ada
   pertukaran sah yang memperbaikinya, tukar. Hasilnya selisih maksimal 1, dan
   rata sempurna kalau total slot habis dibagi jumlah pemain.

Lapis keempat itu ada karena optimasi saja tidak cukup: annealing meminimalkan
biaya gabungan, jadi kerataan main bisa tergadai demi variasi lawan — dan makin
lama optimasinya, makin sering tergadai. Kerataan main sengaja diberi bobot di
atas variasi lawan: peserta membayar fee yang sama, kehilangan satu ronde main
itu kerugian nyata sedangkan sekali bertemu lawan yang sama hampir tak terasa.

Fungsi biayanya memakai bentuk `c·(c-1)` yang konveks, sehingga pengulangan yang
tidak terhindarkan tersebar rata — sistem lebih memilih "4 orang mengulang 1×"
daripada "1 orang mengulang 4×".

## Fitur

**Format**
- Americano, pool berdasarkan rating, Mexicano (tim diseimbangkan), pasangan tetap
- Babak bersegmen, mis. `Putra 3 – Putri 3 – Mixed 6`. Preset bisa ditambahkan
  ke susunan yang ada atau menggantinya - memilihnya saja tidak mengubah apa pun.
  Tiap babak bisa digandakan dan diurutkan dengan diseret (atau panah atas/bawah
  pada gagangnya), dengan total ronde dan menit per ronde terhitung di bawahnya
- **Selang-seling babak**: ronde tiap babak disebar merata, bukan berjalan
  sebagai blok. Tanpa ini, "Putri 4" lalu "Putra 4" berarti para putri main
  4 ronde beruntun sementara para putra duduk 4 ronde beruntun. Dengan
  selang-seling keduanya turun jadi 1
- Preferensi per peserta: partner tetap, atau minta court khusus 4 perempuan /
  4 laki-laki. Boleh sebagian — peserta lain tetap rotasi bebas
- 4–26+ pemain, meet satu gender penuh juga didukung

**Wasit & ballboy**
Diambil dari yang sedang istirahat dan dirotasi adil - rata per peran, bukan
cuma totalnya. Di rekap pemain kolomnya aditif: main + wasit + ballboy +
istirahat = jumlah ronde, sehingga "istirahat" benar-benar berarti tidak main
dan tidak bertugas. Dengan 26 pemain di 4 court,
10 orang duduk tiap ronde — tapi 8 di antaranya bertugas, jadi hanya 2 yang
benar-benar menganggur. Ini yang membuat court sedikit tetap masuk akal.

**Biaya & margin**
Rekomendasi tidak berhenti di "sewa lebih banyak court". Panel ini menunjukkan
biaya, pemasukan, margin, dan waktu main per peserta untuk tiap kombinasi
court × durasi, plus berapa fee harus naik kalau menambah satu court — supaya
keputusannya sadar, bukan menebak.

**Laporan**
- HTML + CSS siap cetak: buka laporan, Ctrl+P, Save as PDF
- Dipadatkan agar muat sesedikit mungkin halaman: card ronde disusun grid
  (3 kolom untuk 1 court, 2 untuk 2 court, 1 untuk 3+), tim A rata kiri dan
  tim B rata kanan dalam kolom terkunci, rekap pemain dipecah dua kolom kalau
  peserta banyak. Meet 8 orang 12 ronde muat satu halaman; 26 orang 11 ronde
  jadi dua
- Logo klub tertanam di kepala laporan, dan fee per peserta jadi kartu paling
  depan - lengkap dengan harga per menit main, karena peserta menilai harga dari
  waktu di lapangan, bukan dari lama acara
- Teks siap tempel ke grup WhatsApp, plus jadwal per pemain
- CSV untuk Excel / Google Sheets

**Master data (SQLite)**
Klub (dengan logo), venue (harga sewa mengisi panel Biaya otomatis), pemain,
riwayat acara, dan statistik lintas acara — siapa yang rajin datang, siapa yang
paling sering kebagian duduk. Tabelnya berhalaman dan bisa dicari.

Venue, klub, dan peserta bisa ditambahkan langsung dari tab Setup: ketik
namanya, kalau belum ada di master muncul tawaran menyimpannya di tempat, tanpa
pindah menu. Peserta juga bisa ditempel sekaligus satu nama per baris.

Nama yang sama dianggap orang yang sama walau beda huruf besar-kecil: "Nisa"
dan "NISA" tidak akan jadi dua anggota. Ejaan yang sudah dipakai dipertahankan -
menyimpan variasi kapital memperbarui datanya, bukan mengganti namanya.

Fee peserta dan sewa court diisi di tab Setup, dan biaya, untung, serta margin
langsung terhitung di sana sambil kamu menyusun acara.

**Grafik**
Tiga grafik yang menjawab pertanyaan yang tidak terjawab oleh satu angka:
trade-off waktu main vs untung antar skenario, komposisi ronde tiap peserta
(main / bertugas / istirahat), dan porsi istirahat lintas acara dengan garis
acuan rata-rata. Paletnya divalidasi terhadap ambang colorblind-safety, dan tiap
grafik punya kembaran tabel sehingga tidak ada angka yang hanya bisa diraih
lewat hover.

## Struktur

```
run.py                      web server (stdlib http.server)
padel_scheduler/
  models.py                 tipe data inti
  capacity.py               analisa kelayakan + batas matematis
  factorization.py          1-factorization & Latin square
  optimizer.py              simulated annealing + batas keras
  scheduler.py              perakit jadwal
  roles.py                  pembagian wasit & ballboy
  economics.py              biaya, margin, trade-off court
  storage.py                SQLite: klub, venue, pemain, acara
  report.py                 ekspor teks / CSV / JSON
  html_report.py            laporan HTML siap cetak
  presets.py                format meet siap pilih
web/
  app.js                    antarmuka (module, tanpa framework)
  charts.js                 grafik SVG buatan sendiri
  combo.js                  combobox autocomplete + quick-add
  _selftest.html            halaman verifikasi visual grafik (bukan bagian app)
tools/
  uitest.py                 uji interaksi UI lewat DevTools Protocol
tests/                      62 tes unit
```

## Aplikasi desktop (Electron)

Pembungkus opsional supaya Padelin jalan sebagai aplikasi biasa: ikon sendiri,
jendela sendiri, tanpa membuka browser. Aplikasinya tidak berubah — Electron
hanya menyalakan `run.py` di port kosong, menunggunya siap, lalu menampilkannya.
Kode di `web/` tetap nol dependency dan tetap bisa dibuka lewat browser.

```bash
npm install
npm run icon       # bangun ikon aplikasi dari web/logo.svg
npm start          # jalankan dari kode sumber
npm run shortcut   # buat Padelin.lnk (tambah -- -Desktop untuk taruh di Desktop)
npm run dist       # bangun installer Windows (.exe)
npm run portable   # bangun paket portable, tanpa pemasangan
```

**Apa yang perlu dipasang pengguna akhir: tidak ada.**

| Komponen | Ikut di installer? | Alasan |
|---|---|---|
| Node.js | Tidak perlu | Electron sudah membawa Node + Chromium sendiri |
| Python | **Ya**, ~15 MB | Distribusi *embeddable* dari python.org, diambil `npm run fetch-python` |
| Paket Python | Tidak ada | Padelin hanya memakai pustaka standar |

Node.js hanya dibutuhkan di mesin yang **membangun** installer, bukan di mesin
yang memakainya.

**Database.** Dijalankan dari repo, tetap `padel.db` di folder repo. Versi
terpasang menaruhnya di folder data pengguna (`%APPDATA%\Padelin`), karena
folder Program Files umumnya hanya-baca dan data acara itu milik pengguna, bukan
bagian dari program — jadi ia selamat saat aplikasi diperbarui atau dipasang
ulang. Menu **Bantuan → Buka folder data** membuka lokasinya. Jalur ini bisa
diarahkan lewat variabel lingkungan `PADELIN_DB`.

**Pembaruan tanpa pasang ulang.** `electron-builder` menerbitkan berkas
`.blockmap` di samping tiap installer; `electron-updater` membandingkannya
dengan versi terpasang lalu mengunduh **blok yang berubah saja** — pembaruan
yang cuma menyentuh kode Python dan `web/` biasanya ratusan KB, bukan installer
utuh. Pengunduhan berjalan di latar, pemasangan saat aplikasi ditutup, dan
**Bantuan → Periksa pembaruan** untuk memeriksa manual.

**Kode privat, installer publik.** Repo kode `satriapribadi/padelin` tetap
privat; hasil build diunggah ke repo terpisah `satriapribadi/padelin-rilis`
yang publik dan isinya **hanya installer, tanpa satu baris kode pun**.

Pemisahan ini bukan kerapian belaka. `electron-updater` membaca rilis lewat API
GitHub, dan rilis di repo privat hanya bisa dibaca dengan token — token yang
mau tak mau ikut terdistribusi ke setiap pengguna, sehingga siapa pun yang
memegang installer bisa membacanya lalu membuka repo privat Anda. Dengan repo
rilis yang publik, tidak ada kredensial apa pun yang perlu ikut.

Merilis:

```bash
# sekali saja: buat repo publik satriapribadi/padelin-rilis di GitHub
export GH_TOKEN=...        # butuh izin tulis ke repo rilis itu saja
npm run release            # build + unggah installer, latest.yml, blockmap
```

`GH_TOKEN` hanya dipakai saat mengunggah di mesin Anda — ia tidak pernah masuk
ke aplikasi maupun ke repo.

Naikkan `version` di `package.json` tiap merilis — itu yang dibandingkan.

### Paket portable, tanpa pemasangan

`npm run portable` menghasilkan `dist-portable/` berisi folder aplikasi dan satu
pintasan `Padelin.lnk`. Bisa disalin ke flashdisk atau komputer lain apa adanya;
Python sudah ikut di dalamnya, jadi tidak ada yang perlu dipasang.

Bedanya dengan installer bukan cuma kepraktisan. Windows menilai executable dari
**reputasi**, dan biner yang baru dibuat belum punya reputasi apa pun — di mesin
dengan Smart App Control aktif, executable baru bisa ditolak mentah-mentah pada
percobaan pertama. Paket portable sengaja tidak membuat executable baru:
`electron.exe` dibiarkan utuh, dan nama serta ikon aplikasi dibawa oleh pintasan
— yang bukan executable, jadi tidak kena penilaian itu.

Harganya: di Task Manager prosesnya bernama `electron.exe`, bukan `Padelin.exe`.

Data acara tetap di `%APPDATA%\Padelin`, bukan di dalam folder portable — jadi
menyalin foldernya tidak ikut membawa data siapa pun.

## Tes

```bash
python -m unittest discover -s tests    # 62 tes unit
python tools/uitest.py                  # 22 uji interaksi di browser sungguhan
python tools/uitest.py --roster daftar.txt   # pakai peserta sungguhan
```

`tools/uitest.py` menjalankan Edge/Chrome headless, menyambung ke DevTools
Protocol, lalu benar-benar mengetik, mengklik, dan hover di halaman: tempel 26
peserta, generate jadwal, tukar grafik ke tabel, munculkan tooltip, ketik venue
baru sampai tersimpan ke master. Klien WebSocket-nya ditulis sendiri agar tetap
nol dependency. Tesnya idempotent - data uji dihapus lagi di akhir.

Ini bukan pelengkap: tiga bug lolos dari seluruh tes unit dan pemeriksaan statis,
dan baru ketahuan dari menjalankan serta melihat aplikasinya sungguhan.

Yang diuji adalah properti keras: tidak ada pemain di dua court sekaligus,
aturan gender ditegakkan 100%, partner terkunci tetap terkunci, pemula tidak
pernah melawan pemain kuat di mode pool, tugas hanya jatuh ke yang sedang duduk,
dan nama pemain selalu di-escape di laporan.

Yang dijaga paling ketat adalah kerataan: jumlah main diuji di delapan
konfigurasi, pembagian tugas diuji rata PER PERAN (bukan cuma totalnya), dan
diuji bahwa optimasi yang lebih lama tidak pernah membuatnya lebih timpang.

Saat generate, kemajuan dikirim ke UI lewat Server-Sent Events - persentase,
tahap, dan biaya terbaik yang sedang dicapai optimizer. Angkanya nyata, jadi
kalau prosesnya lambat kamu tahu di tahap mana.

## Kalau ada yang janggal

Tombol **Salin info debug** di tab Setup menyalin seluruh setup: court, durasi,
babak, daftar peserta beserta rating/gender/pasangan terkunci, dan jadwal yang
dihasilkan. Nama peserta diganti jadi P1..Pn - yang dibutuhkan untuk
mereproduksi masalah penjadwalan hanyalah strukturnya, bukan nama anggota klub.

## Catatan

- Database ada di `padel.db` (satu file, gampang di-backup).
- Jadwal deterministik: seed yang sama menghasilkan jadwal yang sama. Ganti
  seed untuk variasi lain dengan kualitas setara.
- App ini sengaja dibuat tanpa dependency agar jalan offline dan tidak rusak
  saat Python naik versi.
