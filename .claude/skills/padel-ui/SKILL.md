---
name: padel-ui
description: Design system untuk web app generator jadwal padel. Pakai skill ini SEBELUM menulis atau mengubah HTML/CSS/JS apa pun di folder web/, padel_scheduler/html_report.py, atau padel_scheduler/pdf_report.py — termasuk menambah panel, kartu statistik, tabel, tombol, badge, atau halaman laporan cetak. Memuat token warna, komponen baku, aturan layar-vs-cetak, dan bahasa antarmuka.
---

# Design system app jadwal padel

App ini punya dua permukaan yang **beda aturan** dan tidak boleh dicampur:

| Permukaan | Berkas | Tema | Tujuan |
|---|---|---|---|
| Aplikasi | `web/style.css`, `web/index.html`, `web/app.js` | gelap | dipakai host sambil menyusun acara |
| Laporan | `padel_scheduler/html_report.py` | terang | dibaca peserta & dicetak jadi PDF |

Jangan pakai palet gelap di laporan (boros tinta, jelek dicetak), dan jangan
pakai palet terang di aplikasi.

## Token warna

Selalu pakai variabel CSS, jangan tulis hex langsung di komponen.

**Aplikasi** (`web/style.css`, sudah terdefinisi di `:root`):

```
--bg:#0f1419  --panel:#161c24  --panel-2:#1c2531  --line:#2a3644
--ink:#e8edf3 --muted:#8b98a9  --dim:#5f6b7a
--accent:#3d9be9  --accent-dim:#1d4f75
--good:#3ec98a    --good-dim:#17402e
--warn:#f0a94c    --warn-dim:#4a3418
--bad:#f2685f     --bad-dim:#4a1f1c
--radius:10px
```

**Laporan** (`html_report.py`, konstanta `CSS`):

```
--ink:#12151a --muted:#5b6472 --line:#e2e6ec --band:#f5f7fa
--accent:#0d5c8c --accent-soft:#e8f1f7
--warn:#a2560b --warn-soft:#fdf3e7 --good:#1a7a4c --good-soft:#e8f6ef
```

### Arti warna (konsisten di dua permukaan)

| Warna | Dipakai untuk | JANGAN untuk |
|---|---|---|
| `accent` | angka utama, nomor court, judul seksi | status baik/buruk |
| `good` | keunikan tercapai, untung, semua main | sekadar hal positif |
| `warn` | batas matematis terlampaui, banyak yang duduk, margin tipis | error teknis |
| `bad` | setup mustahil, rugi | peringatan biasa |

Aturan penting: **status warna harus punya arti numerik**, bukan selera.
Contoh benar: `rest_ratio > 1/3` → `warn`. Contoh salah: mewarnai kartu merah
"biar kelihatan penting".

## Komponen baku

Pakai ulang kelas yang sudah ada. Jangan bikin varian baru tanpa alasan.

### Kartu angka (`.stat`)

Struktur wajib tiga baris — label, angka, keterangan:

```html
<div class="stat good">
  <div class="k">LABEL KECIL</div>
  <div class="v">123</div>
  <div class="s">satuan / konteks</div>
</div>
```

Bungkus dalam `.stat-grid` (auto-fit, min 115px). Angka tanpa satuan itu
tidak berguna bagi host — `.s` wajib diisi.

### Kartu panel (`.card`)

```html
<div class="card">
  <h2>Judul seksi <span class="hint">penjelasan opsional</span></h2>
  ...
</div>
```

`h2` selalu uppercase kecil + letter-spacing (sudah diatur CSS). `.hint` untuk
kalimat penjelas yang tidak berteriak.

### Peringatan (`.issue`)

```html
<div class="issue warning">
  <div class="t">Judul singkat</div>
  <div class="d">Penjelasan: apa yang terjadi dan angkanya</div>
  <div class="f">Saran: apa yang bisa dilakukan host</div>
</div>
```

Kelas: `error` | `warning` | `info`. Bagian `.f` (saran) **wajib** untuk
`error` dan `warning` — peringatan tanpa jalan keluar cuma bikin panik.

### Tabel (`table.data`)

Kolom angka pakai `class="num"` (rata tengah + tabular-nums). Baris tersorot
saat hover. Header selalu uppercase kecil.

### Pil status (`.pill`)

`.pill.g` / `.pill.w` / `.pill.b`. Maksimal 2 pil per baris tabel — lebih dari
itu jadi ramai dan tidak terbaca.

## Aturan laporan cetak

Saat mengubah `html_report.py`, jaga hal-hal ini atau PDF-nya rusak:

- `@page { size:A4 portrait; margin:14mm 12mm }`
- `.round { break-inside:avoid; page-break-inside:avoid }` — kartu ronde tidak
  boleh terpotong antar halaman
- `-webkit-print-color-adjust:exact; print-color-adjust:exact` di `body`
- `.toolbar { display:none }` di dalam `@media print`
- `thead { display:table-header-group }` supaya header tabel berulang tiap halaman

Uji dengan buka laporan → Ctrl+P → periksa preview, bukan cuma tampilan layar.

## Grafik & visualisasi

Sebelum menambah chart apa pun (sebaran istirahat, perbandingan margin,
statistik pemain), **muat skill `dataviz` lebih dulu**. Jangan tulis kode chart
dari nol. Petakan palet `dataviz` ke token di atas: `accent` untuk seri utama,
`good`/`warn`/`bad` hanya untuk seri yang memang berstatus.

Chart hanya ditambahkan kalau menjawab pertanyaan yang tidak terjawab oleh
angka biasa. Untuk satu angka, kartu `.stat` selalu lebih baik daripada grafik.

## Bahasa antarmuka

- Semua teks yang dilihat host **berbahasa Indonesia**, termasuk pesan error.
- Istilah padel yang lazim dibiarkan Inggris: court, americano, mexicano,
  rating, ballboy, bye (tapi pakai "istirahat" untuk teks umum).
- Nada: jelaskan angkanya, jangan menggurui. Sebut trade-off biaya kalau saran
  melibatkan sewa court tambahan — host membatasi court karena alasan margin,
  bukan karena tidak tahu.
- Hindari kata "optimal" tanpa angka. Sebut batasnya: "lawan unik maksimal 3
  ronde untuk 8 pemain", bukan "jadwal sudah optimal".

## Yang tidak boleh dilakukan

- Menambah dependency frontend (framework, CDN, font eksternal). App ini
  sengaja nol dependency dan harus jalan offline.
- Menulis hex warna langsung di komponen — pakai variabel.
- Membuat animasi/transisi dekoratif. Host memakai ini sambil menyiapkan acara,
  kecepatan lebih penting daripada gerak.
- Menaruh angka tanpa satuan atau konteks.
