// Ambil Python "embeddable" untuk dibundel ke installer, plus OR-Tools.
//
// Penjadwalnya sendiri seluruhnya pustaka standar, jadi distribusi embeddable
// dari python.org sudah cukup apa adanya. Satu-satunya paket pihak ketiga
// adalah OR-Tools, yang dipakai mode "Americano + solver eksak (CP-SAT)".
//
// OR-Tools tidak murah: bersama numpy, pandas, dan protobuf yang dibawanya, ia
// menambah sekitar 200 MB ke folder Python yang tadinya 22 MB. Itu keputusan
// yang disengaja - modenya harus tersedia untuk semua pengguna, bukan cuma
// yang bisa memasang paket Python sendiri. Kalau suatu saat ukuran installer
// jadi masalah, di sinilah tempat memutuskannya: hapus langkah pasangnya, dan
// UI otomatis menyembunyikan modenya (lihat /api/presets -> "cpsat").
//
// Dijalankan otomatis oleh `npm run dist`. Kalau foldernya sudah ada, tidak
// mengunduh ulang.
//
//   node electron/fetch-python.js [versi]

const fs = require('fs');
const path = require('path');
const https = require('https');
const { execFileSync } = require('child_process');

const VERSI = process.argv[2] || '3.12.7';
const ARCH = 'amd64';
// Dipatok, bukan dibiarkan mengambang. Wheel OR-Tools memuat modul biner, dan
// versi yang berbeda-beda antar build berarti bug yang muncul di installer
// tertentu saja tidak bisa dilacak dari repo ini.
const ORTOOLS = 'ortools==9.15.6755';
const TUJUAN = path.join(__dirname, 'vendor', 'python');
const ZIP = path.join(__dirname, 'vendor', `python-${VERSI}-embed-${ARCH}.zip`);
const URL = `https://www.python.org/ftp/python/${VERSI}/python-${VERSI}-embed-${ARCH}.zip`;

function unduh(url, tujuan, sisaRedirect = 5) {
  return new Promise((resolve, reject) => {
    if (sisaRedirect < 0) { reject(new Error('Terlalu banyak pengalihan')); return; }
    https.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        unduh(res.headers.location, tujuan, sisaRedirect - 1).then(resolve, reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode} saat mengunduh ${url}`));
        return;
      }
      const out = fs.createWriteStream(tujuan);
      res.pipe(out);
      out.on('finish', () => out.close(resolve));
      out.on('error', reject);
    }).on('error', reject);
  });
}

async function main() {
  if (process.platform !== 'win32') {
    console.log('Skrip ini menyiapkan Python embeddable untuk Windows.');
    console.log('Untuk platform lain, sediakan Python di electron/vendor/python '
      + 'atau biarkan aplikasi memakai Python sistem.');
    return;
  }
  // Unduhannya yang dilewati kalau sudah ada, BUKAN seluruh sisanya. Dulu
  // fungsi ini langsung return di sini, dan akibatnya baru terasa saat ada
  // langkah baru: siapa pun yang sudah punya folder vendor dari build
  // sebelumnya tidak akan pernah mendapat jalur modul yang diperbarui maupun
  // OR-Tools, dan installer-nya keluar tanpa mode CP-SAT tanpa satu pun pesan.
  // Dua langkah di bawah aman diulang - keduanya memeriksa keadaannya sendiri.
  if (fs.existsSync(path.join(TUJUAN, 'python.exe'))) {
    console.log('Python bundel sudah ada di', TUJUAN);
  } else {
    fs.mkdirSync(path.dirname(ZIP), { recursive: true });
    console.log('Mengunduh', URL);
    await unduh(URL, ZIP);

    fs.mkdirSync(TUJUAN, { recursive: true });
    console.log('Membuka paket ke', TUJUAN);
    // Expand-Archive ada di setiap Windows yang didukung; tidak menambah dependensi.
    execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
      `Expand-Archive -LiteralPath '${ZIP}' -DestinationPath '${TUJUAN}' -Force`],
    { stdio: 'inherit' });
    fs.unlinkSync(ZIP);
  }

  // Distribusi embeddable berjalan terisolasi: folder kerja TIDAK ikut dicari,
  // dan PYTHONPATH diabaikan. Jalur modulnya hanya diatur berkas ._pth.
  //
  // Yang membuat ini mudah salah: entri di ._pth relatif terhadap folder
  // python.exe, BUKAN folder kerja. Menambahkan '.' saja terlihat masuk akal
  // tapi cuma menunjuk balik ke folder Python itu sendiri, dan
  // `import padel_scheduler` tetap gagal - aplikasinya jalan dari repo, lalu
  // mati begitu dipaketkan. Jadi jalurnya ditulis relatif ke tata letak paket:
  //
  //   resources/python/            <- python.exe ada di sini
  //   resources/app.asar.unpacked/ <- run.py & padel_scheduler ada di sini
  const pth = fs.readdirSync(TUJUAN).find((f) => f.endsWith('._pth'));
  if (!pth) {
    throw new Error('Berkas ._pth tidak ditemukan; jalur modul tidak bisa diatur.');
  }
  //
  // 'Lib\\site-packages' ada di daftar karena OR-Tools dipasang ke situ di
  // langkah berikutnya. Distribusi embeddable tidak punya folder itu secara
  // bawaan dan tidak mencarinya sendiri.
  const nama = path.basename(pth, '._pth');
  fs.writeFileSync(path.join(TUJUAN, pth),
    [`${nama}.zip`, '.', 'Lib\\site-packages', '..\\app.asar.unpacked', '',
      'import site', ''].join('\n'));
  console.log('Jalur modul disesuaikan di', pth);

  pasangOrtools();

  console.log('Selesai. Ukuran bundel Python:', ukuranMB(TUJUAN), 'MB');
}

/** Total ukuran satu folder, rekursif, dalam MB bulat. */
function ukuranMB(dir) {
  let n = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    n += e.isDirectory() ? ukuranMB(p) * 1e6 : fs.statSync(p).size;
  }
  return Math.round(n / 1e6);
}

/**
 * Pasang OR-Tools ke dalam Python embeddable.
 *
 * Distribusi embeddable tidak punya pip, jadi yang dipakai pip milik Python
 * pembangun lewat `--target`. Tag-nya WAJIB ditulis eksplisit: tanpa itu pip
 * memasang wheel yang cocok untuk Python pembangun, dan kalau versinya berbeda
 * dari Python yang dibundel, modul .pyd-nya gagal dimuat di komputer pengguna -
 * sebuah kegagalan yang tidak muncul sama sekali di mesin pembangun.
 *
 * Gagal keras kalau tidak berhasil. Melanjutkan diam-diam berarti installer
 * keluar tanpa mode CP-SAT sementara semua tulisan di sekitarnya bilang mode itu
 * ada, dan itu baru ketahuan setelah sampai di tangan pengguna.
 */
function pasangOrtools() {
  const site = path.join(TUJUAN, 'Lib', 'site-packages');
  if (fs.existsSync(path.join(site, 'ortools'))) {
    console.log('OR-Tools sudah ada di', site);
    return;
  }
  const [mayor, minor] = VERSI.split('.');
  const py = process.env.PADELIN_PIP_PYTHON || 'python';
  console.log(`Memasang OR-Tools untuk cp${mayor}${minor} win_amd64 (perlu unduhan ~100 MB)`);
  execFileSync(py, [
    '-m', 'pip', 'install', ORTOOLS,
    '--target', site,
    '--only-binary=:all:',
    '--python-version', `${mayor}.${minor}`,
    '--implementation', 'cp',
    `--platform`, `win_${ARCH}`,
    '--upgrade',
  ], { stdio: 'inherit' });
  console.log('OR-Tools terpasang. Ukuran site-packages:', ukuranMB(site), 'MB');
}

main().catch((err) => { console.error(err.message); process.exit(1); });
