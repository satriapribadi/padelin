// Pembaruan isi aplikasi tanpa menjalankan installer.
//
// Alasan modul ini ada: di Windows 11 dengan Smart App Control aktif, installer
// yang belum bertandatangan ditolak di tingkat kernel dan TIDAK punya "Run
// anyway". Terukur pada 1.3.1 -> 1.3.2: installer terunduh utuh, checksum-nya
// benar, lalu `spawn UNKNOWN` - prosesnya tidak pernah lahir (CodeIntegrity
// 3077 + 3118 di Event Log). Selama installer tidak ditandatangani, mesin
// seperti itu tidak akan pernah bisa memperbarui lewat jalur installer.
//
// Yang berubah di hampir setiap rilis bukan biner, melainkan kode terjemahan:
// web/ (antarmuka), padel_scheduler/ (mesin jadwal + laporan), dan run.py
// (server). Ketiganya sudah tinggal sebagai berkas biasa di app.asar.unpacked,
// dan seluruh penunjuknya cuma satu: pythonAppRoot() di main.js. run.py sendiri
// menghitung WEB_DIR dari letak berkasnya, dan padel_scheduler diimpor dari
// folder yang sama - jadi menggeser satu akar itu sekaligus menggeser
// antarmuka, mesin jadwal, dan server.
//
// Karena tidak ada berkas yang bisa dieksekusi yang lahir baru - python.exe dan
// Padelin.exe tetap yang lama, yang sudah diizinkan - tidak ada yang bisa
// diblokir SAC, SmartScreen, maupun Defender.
//
// Yang TIDAK bisa lewat jalur ini, dan tetap butuh installer:
//   - runtime Electron
//   - Python bundel beserta OR-Tools
//   - electron/*.js (proses utama, termasuk modul ini sendiri) - dimuat Electron
//     sebelum satu baris pun kode kita jalan, jadi mustahil ditukar dari dalam
//
// Konsekuensi yang harus disadari, bukan disembunyikan: jalur ini menghapus hak
// veto Windows atas kode baru. Yang menggantikannya di sini: HTTPS ke repo
// rilis yang sudah ditentukan (bukan URL dari berkas yang diunduh), sha512 yang
// harus cocok SEBELUM satu berkas pun dibongkar, nama berkas yang harus cocok
// pola, isinya sumber terjemahan dan bukan biner, tujuannya userData dan bukan
// folder aplikasi, dan tidak ada elevasi sama sekali.

const { app, dialog, Notification } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// Sumber kebenarannya "build.publish" di package.json; disalin ke sini karena
// package.json yang sampai ke paket sudah dirapikan electron-builder dan tidak
// dijamin memuat blok build. Supaya salinan ini tidak diam-diam menyimpang,
// tests/test_konten.js membandingkan keduanya.
const REPO = 'satriapribadi/padelin-rilis';
const DASAR = `https://github.com/${REPO}/releases`;

// Nama berkas yang boleh diunduh. Manifes datang dari jaringan, jadi namanya
// diperlakukan sebagai masukan, bukan sebagai perintah.
const POLA_BERKAS = /^konten-\d+\.\d+\.\d+\.zip$/;

const AKAR = path.join(app.getPath('userData'), 'konten');
const AKTIF = path.join(AKAR, 'aktif.json');
const RUSAK = path.join(AKAR, 'rusak.json');
const LOG = path.join(app.getPath('userData'), 'konten.log');
const LOG_MAKS = 128 * 1024;

let sedangPeriksa = false;
let logDimulai = false;

/** Catatan ke berkas, dengan aturan yang sama seperti updater.log: ditambahi
 *  antar sesi (yang memuat kegagalan justru sesi sebelumnya), dan dipangkas
 *  dari DEPAN supaya yang terbuang riwayat terjauh, bukan kejadian terbaru. */
function catat(taraf, ...pesan) {
  const teks = pesan
    .map((p) => (typeof p === 'string' ? p : JSON.stringify(p)))
    .join(' ');
  try {
    if (!logDimulai) {
      try {
        if (fs.statSync(LOG).size > LOG_MAKS) {
          const buntut = fs.readFileSync(LOG, 'utf8').slice(-Math.floor(LOG_MAKS / 2));
          const awal = buntut.indexOf('\n');
          fs.writeFileSync(LOG, '--- bagian tertua dipangkas ---\n'
            + (awal >= 0 ? buntut.slice(awal + 1) : buntut), 'utf8');
        }
      } catch { /* gagal memangkas tidak boleh menghalangi pencatatan */ }
      fs.appendFileSync(LOG, `\n--- sesi baru: aplikasi v${app.getVersion()}, `
        + `konten v${versiKonten() || app.getVersion()} ---\n`);
      logDimulai = true;
    }
    fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${taraf} ${teks}\n`);
  } catch { /* gagal mencatat tidak boleh menjatuhkan aplikasi */ }
}

/** Lokasi berkas catatan, untuk ditunjukkan ke host saat ada yang gagal. */
function berkasLog() {
  return LOG;
}

function bacaJson(berkas) {
  try {
    return JSON.parse(fs.readFileSync(berkas, 'utf8'));
  } catch {
    return null;
  }
}

/** Urutan versi "1.10.0" > "1.9.0". Perbandingan teks biasa menjawab
 *  sebaliknya, dan salahnya baru terasa di rilis kesepuluh. */
function bandingVersi(a, b) {
  const pa = String(a).split('.').map((x) => parseInt(x, 10) || 0);
  const pb = String(b).split('.').map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
}

/** Isi folder konten yang sah, atau null.
 *
 * Diperiksa isinya, bukan cuma keberadaan foldernya: bongkaran yang terpotong
 * di tengah meninggalkan folder yang ADA tapi tidak bisa dipakai, dan aplikasi
 * yang memilihnya akan gagal hidup dengan pesan yang menyesatkan.
 */
function isiLengkap(dir) {
  return ['run.py',
    path.join('web', 'index.html'),
    path.join('padel_scheduler', '__init__.py')]
    .every((b) => fs.existsSync(path.join(dir, b)));
}

/** Versi konten yang sedang dipakai, atau null kalau memakai bawaan paket. */
function versiKonten() {
  const aktif = bacaJson(AKTIF);
  if (!aktif || !aktif.versi) return null;
  const rusak = bacaJson(RUSAK);
  if (rusak && rusak.versi === aktif.versi) return null;
  // Konten TIDAK PERNAH boleh lebih tua dari aplikasinya. Setelah installer
  // membawa versi yang lebih baru, konten lama yang tertinggal di userData
  // akan memundurkan aplikasi ke antarmuka dan mesin jadwal lama - pembaruan
  // yang terasa seperti kemunduran, tanpa jejak kenapa.
  if (bandingVersi(aktif.versi, app.getVersion()) <= 0) return null;
  return isiLengkap(path.join(AKAR, aktif.versi)) ? aktif.versi : null;
}

/** Akar berkas Python+web yang harus dipakai, atau null untuk bawaan paket. */
function akarKonten() {
  const versi = versiKonten();
  return versi ? path.join(AKAR, versi) : null;
}

/** Konten aktif ternyata tidak bisa dipakai; kembali ke bawaan paket.
 *
 * Dipanggil main.js ketika server Python gagal hidup. Tanpa ini, satu paket
 * konten yang rusak membuat aplikasi gagal dibuka SELAMANYA - dan bedanya
 * dengan kerusakan biasa tidak terlihat oleh siapa pun, karena berkas
 * bawaannya baik-baik saja.
 */
function tandaiRusak(alasan) {
  const aktif = bacaJson(AKTIF);
  if (!aktif || !aktif.versi) return null;
  try {
    fs.mkdirSync(AKAR, { recursive: true });
    fs.writeFileSync(RUSAK, JSON.stringify(
      { versi: aktif.versi, waktu: new Date().toISOString(), alasan: String(alasan) }),
    'utf8');
  } catch (err) {
    catat('WARN ', 'penanda rusak gagal ditulis:', String(err));
  }
  catat('ERROR', `konten ${aktif.versi} ditandai rusak, kembali ke bawaan paket:`,
    String(alasan));
  return aktif.versi;
}

/** Unduh ke memori. Paket konten berukuran ratusan KB - sumber terjemahan,
 *  bukan biner - jadi tidak ada gunanya menyalurkannya lewat berkas dulu. */
async function unduh(url, maks) {
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`HTTP ${res.status} untuk ${url}`);
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.length > maks) {
    throw new Error(`${url} berukuran ${buf.length} byte, di atas batas ${maks}`);
  }
  return buf;
}

function sha512(buf) {
  return crypto.createHash('sha512').update(buf).digest('base64');
}

/** Bongkar zip ke folder sementara lalu pindahkan sekali jadi.
 *
 * Membongkar langsung ke folder tujuan berarti ada saat ketika foldernya sudah
 * ada tapi isinya belum lengkap; aplikasi yang dibuka tepat pada saat itu akan
 * memilih konten setengah jadi. Nama sementara + rename membuat pergantiannya
 * satu langkah.
 */
function bongkar(zip, versi) {
  const tmpZip = path.join(AKAR, `unduh-${versi}.zip`);
  const tmpDir = path.join(AKAR, `unduh-${versi}`);
  const tujuan = path.join(AKAR, versi);
  fs.mkdirSync(AKAR, { recursive: true });
  fs.rmSync(tmpDir, { recursive: true, force: true });
  fs.writeFileSync(tmpZip, zip);
  try {
    // Expand-Archive ada di setiap Windows yang didukung; tidak menambah
    // dependensi, dan sudah dipakai jalur build (fetch-python.js).
    execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
      `Expand-Archive -LiteralPath '${tmpZip}' -DestinationPath '${tmpDir}' -Force`],
    { windowsHide: true, stdio: 'pipe' });
    if (!isiLengkap(tmpDir)) {
      throw new Error('paket konten tidak memuat run.py, web/index.html, '
        + 'dan padel_scheduler/__init__.py');
    }
    fs.rmSync(tujuan, { recursive: true, force: true });
    fs.renameSync(tmpDir, tujuan);
  } finally {
    fs.rmSync(tmpZip, { force: true });
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
  return tujuan;
}

/** Buang paket konten lama; yang dipakai sekarang dan yang bawaan tidak
 *  disentuh. Tanpa ini folder userData tumbuh satu paket per rilis. */
function bersihkan(simpan) {
  let entri = [];
  try {
    entri = fs.readdirSync(AKAR, { withFileTypes: true });
  } catch {
    return;
  }
  entri
    .filter((e) => e.isDirectory() && e.name !== simpan)
    .forEach((e) => {
      try {
        fs.rmSync(path.join(AKAR, e.name), { recursive: true, force: true });
      } catch { /* biarkan; ruang bukan alasan menggagalkan pembaruan */ }
    });
}

/**
 * Periksa, unduh, dan pasang paket konten terbaru untuk dipakai saat aplikasi
 * dibuka lagi.
 *
 * TIDAK ditukar di tengah sesi. Server Python sudah hidup dengan berkas yang
 * sekarang, jadwal yang tampil datang dari sana, dan menukar berkasnya di
 * belakang layar berarti separuh aplikasi berjalan dengan versi yang berbeda
 * dari separuh lainnya.
 *
 * @param {object} opts
 * @param {boolean} opts.diam  true untuk pemeriksaan otomatis saat start.
 * @param {string} opts.dasar  URL dasar rilis; diganti hanya oleh tes.
 */
async function periksaKonten({ diam = true, dasar = DASAR } = {}) {
  if (sedangPeriksa) return null;
  sedangPeriksa = true;
  try {
    const manifes = JSON.parse(
      (await unduh(`${dasar}/latest/download/konten.json`, 64 * 1024)).toString('utf8'));
    const versi = String(manifes.versi || '');
    const berkas = String(manifes.berkas || '');
    if (!/^\d+\.\d+\.\d+$/.test(versi) || !POLA_BERKAS.test(berkas)) {
      throw new Error(`manifes tidak masuk akal: versi=${versi} berkas=${berkas}`);
    }

    const sekarang = versiKonten() || app.getVersion();
    if (bandingVersi(versi, sekarang) <= 0) {
      catat('INFO ', `konten ${sekarang} sudah yang terbaru (tersedia ${versi})`);
      return null;
    }
    // Konten baru di atas proses utama yang lama hanya sah selama kontraknya
    // tidak berubah. Kalau main.js perlu ikut berubah, rilis itu menaikkan
    // app_minimal, dan mesin yang installernya tertahan berhenti di sini -
    // dengan alasan yang tercatat, bukan dengan aplikasi yang rusak.
    const minimal = String(manifes.app_minimal || '0.0.0');
    if (bandingVersi(app.getVersion(), minimal) < 0) {
      catat('WARN ', `konten ${versi} butuh aplikasi >= ${minimal}, `
        + `yang terpasang ${app.getVersion()} - dilewati`);
      if (!diam) {
        dialog.showMessageBox({
          type: 'info',
          title: 'Pembaruan',
          message: `Pembaruan ${versi} tidak bisa dipasang tanpa installer.`,
          detail: `Bagian ini ikut mengubah kerangka aplikasi, jadi ia butuh `
            + `Padelin ${minimal} atau lebih baru, sedangkan yang terpasang `
            + `${app.getVersion()}.\n\nRincian tercatat di:\n${berkasLog()}`,
        });
      }
      return null;
    }

    catat('INFO ', `mengunduh konten ${versi} (${berkas})`);
    // URL zip DIRAKIT dari versi dan nama berkas, tidak diambil dari manifes:
    // manifes datang dari jaringan, dan URL di dalamnya berarti isi berkas itu
    // boleh menentukan dari mana kode berikutnya diunduh.
    const zip = await unduh(`${dasar}/download/v${versi}/${berkas}`, 32 * 1024 * 1024);
    const sha = sha512(zip);
    if (manifes.sha512 && sha !== manifes.sha512) {
      throw new Error(`sha512 tidak cocok untuk ${berkas}: manifes menyebut `
        + `${manifes.sha512}, yang terunduh ${sha}`);
    }
    if (manifes.ukuran && zip.length !== Number(manifes.ukuran)) {
      throw new Error(`ukuran tidak cocok: manifes ${manifes.ukuran}, `
        + `terunduh ${zip.length}`);
    }

    bongkar(zip, versi);
    fs.writeFileSync(AKTIF, JSON.stringify(
      { versi, dipasang: new Date().toISOString() }), 'utf8');
    fs.rmSync(RUSAK, { force: true });   // penanda rusak versi lama tidak berlaku lagi
    bersihkan(versi);
    catat('INFO ', `konten ${versi} siap, dipakai saat Padelin dibuka lagi`);

    if (Notification.isSupported()) {
      new Notification({
        title: 'Pembaruan siap',
        body: `Versi ${versi} sudah terpasang dan dipakai saat Padelin dibuka lagi.`,
      }).show();
    }
    if (!diam) {
      dialog.showMessageBox({
        type: 'info',
        title: 'Pembaruan',
        message: `Versi ${versi} sudah terpasang.`,
        detail: 'Tanpa installer, jadi tidak ada yang perlu diklik. Perubahannya '
          + 'berlaku begitu Padelin ditutup lalu dibuka lagi.',
      });
    }
    return versi;
  } catch (err) {
    catat('ERROR', 'pembaruan konten gagal:', String(err && err.stack ? err.stack : err));
    // Saat start, jaringan mati bukan urusan host - tapi tetap tercatat.
    if (!diam) {
      dialog.showMessageBox({
        type: 'error',
        title: 'Pembaruan gagal',
        message: 'Tidak bisa memperbarui isi aplikasi.',
        detail: `${String(err && err.message ? err.message : err)}\n\n`
          + `Rincian tercatat di:\n${berkasLog()}`,
      });
    }
    return null;
  } finally {
    sedangPeriksa = false;
  }
}

module.exports = {
  akarKonten,
  versiKonten,
  tandaiRusak,
  periksaKonten,
  bandingVersi,
  berkasLog,
  REPO,
};
