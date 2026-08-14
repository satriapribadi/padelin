# Padelin

> Jadwal meet, beres.

Web app lokal untuk menyusun jadwal meet padel: Americano, pool rating, Mexicano,
pasangan tetap, dan format bersegmen (putra / putri / mixed) — lengkap dengan
pembagian tugas wasit & ballboy, analisa biaya, laporan siap cetak, dan database.

**Nol dependency wajib.** Cukup Python 3.10+, tanpa `pip install` apa pun.

```bash
python run.py
```

Satu-satunya paket opsional adalah [OR-Tools](https://developers.google.com/optimization),
yang menyalakan mode *Americano + solver eksak (CP-SAT)*. Tanpa paket itu semua
fitur lain berjalan penuh dan modenya tidak muncul di UI. Installer Windows
sudah membundelnya; untuk menjalankan dari repo:

```bash
pip install ortools
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
- **Americano + solver eksak (CP-SAT)** — aturan yang sama persis dengan
  Americano, mesin pencarian yang berbeda. Lihat di bawah
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

**Americano + solver eksak (CP-SAT)**
Mode ini tidak mengganti apa pun soal aturan jadwal — yang berganti cuma mesin
pencariannya, dan bahkan itu pun hanya sebagai tahap TAMBAHAN di ujung.

Seluruh rangkaian biasa tetap jalan lebih dulu (konstruksi, annealing,
pemerataan, perapian giliran). Hasilnya baru diserahkan ke solver eksak
[OR-Tools CP-SAT](https://developers.google.com/optimization) sebagai titik
awal. Dari situ solver mengerjakan dua hal yang tidak bisa dikerjakan pencarian
acak:

1. memungut sisa perbaikan yang tidak terjangkau gerakan lokal;
2. **membuktikan** bahwa tidak ada lagi yang tersisa.

Poin kedua itulah alasan mode ini ada. Annealing tidak pernah bisa mengatakan
apakah "2 pasang lawan berulang" itu memang batasnya atau cuma sejauh yang
ia temukan. Solver bisa — dan kalau ia berhasil, catatan jadwalnya berkata
begitu apa adanya.

Urutan ini hasil pengukuran, bukan selera. Versi pertama menyuruh CP-SAT
menggantikan annealing dan mulai dari konstruksi greedy; hasilnya kalah telak
(26 orang / 4 court: annealing nol lawan berulang dalam 7 detik, CP-SAT masih
13 pasang setelah 20 detik). Penjadwalan ini sangat simetris dan ruang solusinya
raksasa — medan yang memang menguntungkan pencarian lokal.

Diukur pada 6 setup × 3 seed dengan batas 30 detik: **lebih baik di 2 kasus,
lebih buruk di 0, sama di 16**. Lima kasus selesai TERBUKTI optimal, dan
karenanya berhenti jauh sebelum batas waktunya (8 orang / 2 court: 1,4–1,7
detik; 12 orang / 2 court: 8,9–12,7 detik, salah satunya sekaligus menurunkan
lawan berulang dari 15 ke 14).

Bacalah angka itu apa adanya: pada meet besar yang annealing-nya sudah menyentuh
nol pengulangan, solver tidak punya apa pun untuk diperbaiki dan Anda cuma
membayar waktu. Yang dibelinya di situ bukan jadwal yang lebih baik, melainkan
jawaban atas "apakah ini memang sudah mentok" — dan itu jawaban yang sebelumnya
tidak pernah tersedia. Bandingkan sendiri:

```
python tools/banding_cpsat.py --detik 30 --seed 1 2 3
```

Ongkosnya dua: Anda menunggu selama batas waktu yang dipilih, dan installer
membengkak sekitar 200 MB karena OR-Tools membawa numpy, pandas, dan protobuf.
Kalau OR-Tools tidak terpasang, modenya otomatis disembunyikan dari UI dan sisa
aplikasi berjalan seperti biasa.

**Court berkurang di tengah acara**
Untuk sewa yang tidak sama panjang: 2 court dua jam, lalu 1 court sejam lagi.
Centang di *Setup lapangan*, isi sisa court dan mulai ronde berapa — dan yang
ikut menyesuaikan bukan cuma jadwalnya:

- **jatah main tetap rata.** Ronde 1 court hanya butuh separuh pasangan, jadi
  rencana slot gender ikut dihitung ulang. Ini bukan detail: dengan format match
  dibatasi ke sesama-bentuk, menukar seorang putri dengan seorang putra ditolak
  batas format, sehingga kerataan yang tidak lahir saat konstruksi tidak bisa
  ditebus belakangan
- **biaya mengikuti court-jam nyata**, bukan court × jam. 2 court × 120 menit +
  1 court × 60 menit = 5 court-jam, bukan 6
- **batas keunikan dan jumlah yang duduk ikut turun/berayun.** Court yang lebih
  sedikit berarti lebih sedikit ronde main per orang — yang justru memperbaiki
  keunikan partner & lawan — dan yang duduk jadi rentang (2–6 orang), bukan satu
  angka
- **tunggu terpanjang yang dipaksa kapasitas tidak didenda.** Dengan 1 court, 6
  dari 10 orang duduk; dua ronde berurutan menyediakan 12 tempat duduk untuk 10
  orang, jadi minimal 2 orang duduk dua kali beruntun. Batas yang dilaporkan
  menghitungnya, sehingga skor kualitas tidak menghukum jadwal untuk sesuatu
  yang tidak bisa ia perbaiki

Pola yang lebih rumit daripada satu kali pengurangan (mis. `2x8, 1x4, 2x3`)
belum ada di UI; pakai `tools/laporan_court_turun.py`.

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
  cpsat.py                  solver eksak OR-Tools (mode americano_cpsat)
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
  banding_cpsat.py          adu annealing lawan solver eksak pada setup yang sama
tests/                      170 tes unit
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

**Pembaruan tanpa pasang ulang.** Aplikasi terpasang memeriksa rilis terbaru
sendiri, mengunduhnya di latar belakang, dan memasangnya saat aplikasi ditutup.
Ada juga **Bantuan → Periksa pembaruan** untuk memeriksa manual.

Terbukti ujung ke ujung: 1.0.0 terpasang menemukan 1.0.1, mengunduhnya, dan
memasangnya; lalu 1.0.1 melakukan hal yang sama ke 1.0.2 — tanpa kredensial apa
pun karena repo rilisnya publik.

**Hanya bagian yang berubah yang diunduh.** `electron-builder` menerbitkan
`.blockmap` di samping tiap installer; `electron-updater` membandingkan blockmap
versi lama dan baru, lalu meminta blok yang berbeda saja lewat satu permintaan
multi-rentang. Terukur pada pembaruan 1.0.5 → 1.0.6:

```
diferensial : 1,44 MB terkirim   (62 blok berubah, 1 permintaan, 9 potongan)
unduhan utuh:  96,33 MB terkirim
```

Syaratnya satu, dan gampang terlewat: installer versi sebelumnya harus masih ada
di cache updater (`%LOCALAPPDATA%\padelin-updater\installer.exe`). Cache itu
terisi sendiri ketika pembaruan sebelumnya dipasang lewat updater. Kalau tidak
ada — pemasangan pertama, atau cache dibersihkan — updater menghitung
selisihnya, gagal membukanya, lalu mundur ke unduhan utuh dan mencatatnya di
`updater.log`. Jadi pembaruan pertama sesudah pemasangan manual memang selalu
utuh; yang berikutnya baru hemat.

Satu jebakan yang sudah ditutup: `electron-builder` membuat rilis sebagai
**draft** secara bawaan, dan draft tidak terlihat tanpa token. Build sukses,
unggah sukses, tapi aplikasi tidak pernah menemukan pembaruannya — gagal tanpa
satu pun pesan error. Karena itu `releaseType: release` dipasang eksplisit.

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
python -m unittest discover -s tests    # 170 tes unit
python tools/uitest.py                  # 27 uji interaksi di browser sungguhan
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
