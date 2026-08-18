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

Lima lapis yang terpisah rapi:

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
5. **Perataan giliran** — annealing tahap kedua yang hanya mengurus siapa duduk
   kapan, dengan jumlah pasang berulang yang sudah dicapai dipasang sebagai
   batas keras. Diulang selama tunggu terpanjang masih di atas batas yang tak
   terhindarkan, maksimal 3 putaran tambahan.

Lapis keempat itu ada karena optimasi saja tidak cukup: annealing meminimalkan
biaya gabungan, jadi kerataan main bisa tergadai demi variasi lawan — dan makin
lama optimasinya, makin sering tergadai. Kerataan main sengaja diberi bobot di
atas variasi lawan: peserta membayar fee yang sama, kehilangan satu ronde main
itu kerugian nyata sedangkan sekali bertemu lawan yang sama hampir tak terasa.

Fungsi biayanya memakai bentuk `c·(c-1)` yang konveks, sehingga pengulangan yang
tidak terhindarkan tersebar rata — sistem lebih memilih "4 orang mengulang 1×"
daripada "1 orang mengulang 4×".

Putaran tambahan di lapis kelima ada karena satu anggaran tetap ternyata kurang,
dan kekurangannya paling terasa justru di setup yang paling bagus. Diukur pada 16
konfigurasi × 12 seed: **8 setup membaik, 0 memburuk, 8 tidak berubah** — dan
yang tidak berubah tidak membayar apa pun, karena syaratnya sudah padam sebelum
putaran pertama berjalan. Yang paling banyak dipungut adalah 26 peserta di 13
ronde (96,6 → 97,8), satu-satunya jumlah ronde yang membagi jatah main rata untuk
26 orang — jadi setup yang paling sering disarankan panel kelayakan sekaligus
yang paling dirugikan anggaran lama.

Syaratnya berpatokan pada tunggu terpanjang, bukan sekadar anggaran yang lebih
besar untuk semua, karena sebagian setup memang tidak bisa mencapai batasnya:
format yang dibatasi sesama-bentuk tidak sampai di batas walau anggarannya
dikali sepuluh. Setup seperti itulah yang menanggung ongkosnya — 1,5 → 3,6 detik
untuk +0,4 poin — sementara setup yang sudah di batasnya berongkos nol.

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

**Kapan menyalakannya: 11 ronde ke bawah.** Ini aturan operasi yang paling
penting soal mode ini, dan tidak bisa ditebak dari mana pun — ia harus diukur.

Yang menentukan bukan jumlah peserta, melainkan **jumlah ronde**. Ukuran model
tumbuh dengan peserta × ronde × court, dan rondelah yang paling cepat
membunuhnya. Diukur pada 4 court dengan roster nyata 26 orang, batas solver 60
detik:

| sewa | ronde | 10 org | 14 org | 18 org | 22 org | 26 org |
|---|---|---|---|---|---|---|
| 2 jam | 9 | terbukti | terbukti | **lebih baik** | terbukti | terbukti |
| 2,5 jam | 11 | — | — | terbukti | tidak ada | **lebih baik** |
| 2,7 jam | 12 | — | — | tidak ada | tidak ada | tidak ada |
| 3 jam | 14 | terbukti | tidak ada | tidak ada | tidak ada | tidak ada |

Tabel ini menunjukkan arah, bukan hasil yang akan Anda dapat persis: solver
berjalan dengan 8 worker dan batas waktu jam-dinding, jadi sel yang berbunyi
"terbukti" bisa berbunyi lain saat diulang di mesin lain atau saat mesin yang
sama sedang sibuk. Lihat catatan soal determinisme di bagian *Catatan*. Yang
tidak berayun adalah letak dindingnya, dan itulah yang dipakai mengambil
keputusan.

Di 9 ronde solver menembus SELURUH rentang peserta, termasuk 26 orang dalam 30
detik. Di 12 ronde ia mati total, bahkan pada 18 orang. Jadi batasnya tajam dan
letaknya di sekitar **11 ronde** — bukan di jumlah peserta, seperti yang mudah
dikira.

Sebagian besar yang dibelinya bukan jadwal yang lebih baik melainkan
**kepastian**: "23 pasang berulang itu memang batasnya, berhenti mengulang
dengan seed lain". Tapi tidak selalu — pada 26 orang / 11 ronde kualitasnya naik
92,1 → 94,3 dengan pengulangan lawan yang sama-sama nol; yang diperbaiki solver
di situ adalah keadilan istirahat.

Bandingkan sendiri di setup Anda:

```
python tools/banding_cpsat.py --detik 30 --seed 1 2 3
```

**Tuas lain sering mengalahkannya.** Untuk 26 peserta, memakai 13 ronde (satu-
satunya jumlah ronde yang membuat jatah main habis dibagi rata — lihat *Jumlah
ronde yang membagi rata* di bawah) memberi mutu 97,2, jauh di atas 94,3 yang
bisa dicapai solver. Kalau acara Anda cukup panjang untuk 13 ronde, pakai itu
dan Americano biasa; solver tidak akan menyusul dan cuma menambah waktu tunggu.

Ongkosnya dua: Anda menunggu selama batas waktu yang dipilih, dan installer
membengkak sekitar 200 MB karena OR-Tools membawa numpy, pandas, dan protobuf.
Kalau OR-Tools tidak terpasang, modenya otomatis disembunyikan dari UI dan sisa
aplikasi berjalan seperti biasa.

**Sempurnakan jadwal ini**
Tombol yang muncul di panel Hasil, dan hanya kalau salah satu dari dua hal ini
masih di atas batasnya — keduanya ambang numerik, bukan selera:

- **giliran**: ada peserta yang duduk lebih lama beruntun daripada batas yang tak
  terhindarkan untuk jumlah mainnya, ambang yang sama yang membuat kartu *Tunggu
  terpanjang* berwarna kuning
- **lawan berulang**: ada pasangan yang berhadapan lebih dari sekali, *dan*
  jadwalnya belum menyentuh batas bawah teoretis pengulangan. Syarat kedua itu
  yang menjaganya tetap berguna: pada 16 orang / 4 court yang pengulangannya
  memang wajib, penyempurnaan berhenti dalam 2,2 detik tanpa mencoba apa pun

Kalau dua-duanya sudah di batas, tombolnya tidak ditawarkan sama sekali.

Syarat lawan berulang ditambahkan setelah versi pertama terbukti terlalu ketat.
Waktu itu tertulis di sini bahwa setup yang tanda gilirannya mati tidak pernah
menemukan apa pun — dan itu cuma benar untuk enam setup yang kebetulan disapu.
Diukur ulang pada kasus yang gilirannya sudah rapi: 12 orang / 2 court naik
92,6 → 94,6 dan 93,8 → 94,8, dan mexicano 16 turun dari 53 ke 50 pasang lawan
berulang. Empat dari dua belas kasus membaik padahal tombolnya tidak pernah
ditawarkan di sana.

Yang dijalankannya bukan solver seutuh-jadwal. Ia membuka **tiga ronde
sekaligus**, memaku sisanya, dan menyelesaikan submasalah itu secara eksak.
Bedanya besar, dan itu yang membuat pendekatan ini ada: model utuh mati di 12
ronde ke atas, sementara submodel selalu kecil. Diukur pada 26 orang / 4 court /
11 ronde, setup yang model utuhnya kembali tanpa perbaikan setelah 15 detik:

| jendela | terbukti optimal | detik |
|---|---|---|
| kesembilannya | ya, semua | 1,6–2,9 |

Empat dari sembilan jendela itu menemukan perbaikan yang tidak terjangkau
pertukaran biasa: 92,1 → 94,6, dengan pengulangan lawan tetap nol. Yang
diperbaiki giliran — dan giliran memang yang paling sering tertinggal, karena
pertukaran berpasangan tidak bisa menggeser tiga ronde sekaligus.

Anggaran waktunya milik tahap penyempurnaan, bukan lama host menunggu: menekan
tombol menyusun ulang jadwalnya lebih dulu, dan biaya itu tumbuh dengan ukuran
acara. Diukur dari penekanan sampai selesai — 18,9 detik pada 26 orang / 4 court
(7,4 di antaranya penyempurnaan) dan 41,9 detik pada 60 orang / 6 court (14,4
penyempurnaan). Karena itu tombolnya tidak memasang angka: menjanjikan "maks 20
detik" berarti melanggarnya sendiri di acara besar.

Roster besar justru paling tidak membutuhkannya. Pada 40 orang / 6 court dan 60
orang / 15 court tombolnya tidak muncul sama sekali — dengan court sebanyak itu
hampir semua orang turun tiap ronde, jadi tidak ada rentetan duduk untuk
diperbaiki. Yang paling terbantu adalah roster menengah dengan court sedikit:
gain terbesar yang terukur bukan di 26 orang melainkan **20 orang / 3 court,
+3,4 poin**.

Jendela yang dicoba dipilih dari lokasi pelanggarannya, bukan disapu semua.
Diukur pada 18 kasus: menyapu seluruh jendela memberi mutu rata-rata yang sama
(+0,56 lawan +0,57) dengan **empat kali** waktunya. Anggaran waktunya total,
bukan per jendela — host memilih berapa lama ia mau menunggu, bukan berapa
jendela yang akan dicoba.

Jadwalnya tidak bisa jadi lebih buruk: hasil solver hanya dipakai kalau ukuran
yang sama yang memilih di antara percobaan menyatakan ia lebih baik. Pada 54
percobaan di tiga varian penyetelan, tidak satu pun jadwal memburuk. Kalau tidak
ada yang bisa diperbaiki, jadwal sekarang dipertahankan dan alasannya ditulis di
panel Catatan — termasuk kalau anggaran waktunya habis lebih dulu, karena itu
satu-satunya keadaan yang layak dicoba ulang dengan angka lebih besar.

Butuh OR-Tools, sama seperti mode CP-SAT. Tanpa paket itu tombolnya tidak
ditampilkan.

**Jumlah ronde yang membagi rata**
Slot main per ronde = 4 × court. Supaya jatah main habis dibagi rata ke `N`
peserta, `4·C·R` harus habis dibagi `N` — dan kalau tidak, sebagian orang main
satu ronde lebih banyak, keadilan giliran rusak, dan skor kualitas jatuh lebih
jauh daripada yang disebabkan pengulangan lawan mana pun.

Untuk 26 peserta: `4·C·R ≡ 0 (mod 26)` ⟺ `13 | C·R`. Karena 13 prima dan jumlah
court selalu di bawah 13, **R harus kelipatan 13** — jadi 13 ronde, berapa pun
court-nya. Diukur, dan selisihnya besar:

| court | ronde | main/orang | lawan berulang | mutu |
|---|---|---|---|---|
| 4 | **13** | 8,0 (rata) | 0 | **97,2** |
| 4 | 14 | 8,6 (5–6 duduk) | 0 | 91,7 |
| 6 | **13** | 12,0 (rata) | 13 | **98,6** |
| 6 | 14 | 12,9 (lewat batas) | 26 | 93,6 |

Perhatikan baris 6 court: 13 ronde punya LEBIH BANYAK pengulangan lawan
daripada 12 ronde (13 lawan 4) tapi mutunya jauh lebih tinggi. Pembagian yang
rata menggerakkan kualitas lebih besar daripada keunikan lawan.

Host tidak perlu menghitung ini sendiri. Panel *Analisa kelayakan* memeriksanya
sebelum Generate dan menyebut menit per ronde yang mendaratkan acara di angka
yang membagi rata — angka itu diverifikasi lewat `rounds_from_duration`, bukan
dibagi lalu diharapkan pas, karena saran yang meleset satu ronde justru
mengulang masalah yang mau diperbaiki. Sarannya diam sendiri kalau setupnya
sudah rata, kalau tidak ada yang duduk, atau kalau acaranya bersegmen (kolam
pesertanya pecah, jadi rumus di atas tidak berlaku).

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
tests/                      218 tes unit
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

### Menjalankan tanpa memasang

Tidak ada paket portable. Yang dulu ada — `npm run portable`, penghasil
`dist-portable/` berisi folder aplikasi plus pintasan `Padelin.lnk` — sudah
dihentikan: ia tidak pernah ikut diuji bersama rilis, dan pada pemeriksaan
terakhir memang tidak naik (prosesnya hidup, jendelanya tidak pernah muncul).
Yang dirilis dan diuji hanya installer NSIS.

Untuk menjalankan dari kode sumber tanpa memasang apa pun, pakai `npm start`,
atau `npm run shortcut` yang membuat `Padelin.lnk` menunjuk ke `electron.exe` di
`node_modules`. Alasan memakai pintasan dan bukan executable baru tetap berlaku:
Windows menilai executable dari **reputasi**, dan biner yang baru dibuat belum
punya reputasi apa pun — di mesin dengan Smart App Control aktif ia bisa ditolak
mentah-mentah. Pintasan bukan executable, jadi tidak kena penilaian itu.

Data acara ada di `%APPDATA%\Padelin` saat dipasang lewat installer, dan
`padel.db` di folder repo saat dijalankan dari kode sumber.

## Tes

```bash
python -m unittest discover -s tests    # 218 tes unit
python tools/uitest.py                  # 29 uji interaksi di browser sungguhan
python tools/uitest.py --roster daftar.txt   # pakai peserta sungguhan
python tools/cetaktest.py               # 15 uji jalur cetak & pratinjau (Electron)
python tools/apptest.py                 # 18 uji aplikasi desktop sungguhan
python tools/pakettest.py               # uji paket hasil `npm run dist:dir`
```

`tools/uitest.py` menjalankan Edge/Chrome headless, menyambung ke DevTools
Protocol, lalu benar-benar mengetik, mengklik, dan hover di halaman: tempel 26
peserta, generate jadwal, tukar grafik ke tabel, munculkan tooltip, ketik venue
baru sampai tersimpan ke master. Klien WebSocket-nya ditulis sendiri agar tetap
nol dependency. Tesnya idempotent - data uji dihapus lagi di akhir.

Ini bukan pelengkap: tiga bug lolos dari seluruh tes unit dan pemeriksaan statis,
dan baru ketahuan dari menjalankan serta melihat aplikasinya sungguhan.

### Tiga lapis yang tidak tersentuh browser

Cetak, pratinjau, dan pengemasan tidak ada di browser, jadi ketiganya punya
alatnya sendiri. Semuanya memakai kode produksi yang sama - bukan tiruannya,
karena versi tiruan pernah LULUS sementara aplikasinya tetap salah.

`cetaktest.py` merakit laporan contoh, menjalankan Electron, mengklik tombol di
toolbar laporan, lalu **melihat piksel** di jendela pratinjau yang muncul. Yang
terakhir itu bukan hiasan: pernah ada CSP yang memblokir dokumen PDF-nya, dan
seluruh pemeriksaan lain tetap lulus karena elemen `embed`-nya tetap ada dan
tetap setinggi jendela - hanya kosong. Piksel yang membongkarnya: 0% terang saat
rusak, 94% saat benar.

`apptest.py` menjalankan aplikasi desktop dari `main.js` dan mengendalikannya
lewat DevTools Protocol: server Python menyala, tempel peserta, Generate, Buka
laporan (jendela baru lewat `window.open`), lalu Pratinjau cetak. Yang dijaga di
sini adalah preload yang harus sampai ke jendela anak - tanpa itu tombol cetak
jatuh diam-diam ke dialog Windows yang panel pratinjaunya kosong.

`pakettest.py` memeriksa paket hasil build: daftar isi `app.asar` dibaca dari
header-nya (`preload.js` pernah tertinggal, dan akibatnya bukan error - fiturnya
cuma lenyap), berkas yang harus di luar asar, lalu menjalankan paketnya. Kalau
Windows menolak biner barunya (Application Control, `WinError 4551`), lapis
runtime-nya dilaporkan **dilewati** - bukan lulus - dan harus dicoba dengan
tangan sekali lewat prompt yang mengizinkan.

Klien CDP bersamanya ada di `tools/cdp.py`, dan ia memakai ulang klien WebSocket
di `uitest.py` supaya di repo ini tetap hanya ada satu.

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

Satu batasnya: kalau jadwalnya dibuat dengan mode CP-SAT atau lewat tombol
*Sempurnakan jadwal ini*, menjalankan ulang setup yang sama belum tentu
menghasilkan jadwal yang sama - lihat catatan soal determinisme di bawah.
Matikan dulu keduanya saat melacak masalah penjadwalan.

## Catatan

- Database ada di `padel.db` (satu file, gampang di-backup).
- Jadwal deterministik **selama solver eksak tidak ikut**: seed yang sama
  menghasilkan jadwal yang sama. Ganti seed untuk variasi lain dengan kualitas
  setara. Yang menjaminnya adalah seluruh rangkaian penjadwalan - konstruksi,
  annealing, perataan, perapian giliran - berjalan dari satu sumber acak yang
  ditentukan seed.
- **Dua jalur yang TIDAK deterministik**, dan sebaiknya diketahui sebelum
  dipakai untuk melacak masalah: mode *Americano + solver eksak (CP-SAT)* dan
  tombol *Sempurnakan jadwal ini*. Keduanya menjalankan solver dengan 8 worker
  dan batas waktu jam-dinding, dan di sana hasilnya bergantung pada urutan
  selesainya thread - bukan pada seed. Diukur dengan input yang sama persis,
  lima kali berturut-turut di mesin yang sama: mode CP-SAT mendarat di 92,1
  tanpa bukti optimal tiga kali dan di 94,7 dengan bukti dua kali. Tombol
  penyempurnaan berayun serupa: delapan kali jalan menghasilkan **empat jadwal
  yang berbeda** - mutu 94,6 enam kali dan 94,7 dua kali - dan pada sapuan lain
  ia pernah kembali tanpa perbaikan sama sekali di 92,1.

  Yang berayun cuma sisi mana yang ditemukan solver, bukan kesahihan jadwalnya:
  hasil solver hanya dipakai kalau benar-benar lebih baik, jadi hasil terburuk
  dari ayunan itu adalah jadwal yang sama dengan tanpa solver. Untuk melacak
  masalah, jalankan ulang dengan modenya dimatikan lebih dulu - bagian yang
  deterministik itulah yang bisa dibandingkan.

  Determinisme di sini bukan hal yang tinggal dinyalakan. Diukur pada setup yang
  sama: memaksanya dengan `interleave_search` plus batas waktu deterministik
  memang berhasil - empat kali jalan, satu jadwal - tapi berongkos 4,4× lebih
  lambat (22 → 98 detik) dan terkunci di 92,1 tanpa bukti, jadi yang hilang
  justru kemungkinan mendarat di 94,7. Menurunkannya ke satu worker
  menghilangkan pencarian portofolio yang membuat mode ini berguna. Jadi yang
  dipilih adalah solver yang kuat dengan hasil yang berayun, dan ayunannya
  disebut di sini apa adanya alih-alih dijanjikan tidak ada.
- App ini sengaja dibuat tanpa dependency agar jalan offline dan tidak rusak
  saat Python naik versi.
