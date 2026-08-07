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
- Babak bersegmen, mis. `Putra 3 – Putri 3 – Mixed 6` (ada preset siap pakai)
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
- Logo klub tertanam di kepala laporan
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

## Tes

```bash
python -m unittest discover -s tests    # 62 tes unit
python tools/uitest.py                  # 17 uji interaksi di browser sungguhan
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

## Catatan

- Database ada di `padel.db` (satu file, gampang di-backup).
- Jadwal deterministik: seed yang sama menghasilkan jadwal yang sama. Ganti
  seed untuk variasi lain dengan kualitas setara.
- App ini sengaja dibuat tanpa dependency agar jalan offline dan tidak rusak
  saat Python naik versi.
