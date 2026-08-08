// Ambil Python "embeddable" untuk dibundel ke installer.
//
// Padelin tidak memakai satu pun paket pihak ketiga - seluruhnya pustaka
// standar - jadi distribusi embeddable dari python.org sudah cukup apa adanya.
// Tidak ada pip, tidak ada langkah pasang dependensi, dan pengguna akhir tidak
// perlu memasang Python sama sekali.
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
  if (fs.existsSync(path.join(TUJUAN, 'python.exe'))) {
    console.log('Python bundel sudah ada di', TUJUAN);
    return;
  }

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

  // Distribusi embeddable mengabaikan folder kerja saat mencari modul, jadi
  // `import padel_scheduler` gagal. Berkas ._pth mengatur jalur pencarian;
  // menambahkan '.' membuat modul aplikasi ikut terbaca.
  const pth = fs.readdirSync(TUJUAN).find((f) => f.endsWith('._pth'));
  if (pth) {
    const berkas = path.join(TUJUAN, pth);
    let isi = fs.readFileSync(berkas, 'utf8');
    if (!/^\.$/m.test(isi)) isi += '\n.\n';
    isi = isi.replace(/^#\s*import site/m, 'import site');
    fs.writeFileSync(berkas, isi);
    console.log('Jalur modul disesuaikan di', pth);
  }

  console.log('Selesai. Ukuran bundel Python:',
    Math.round(fs.readdirSync(TUJUAN)
      .reduce((n, f) => n + fs.statSync(path.join(TUJUAN, f)).size, 0) / 1e6),
    'MB');
}

main().catch((err) => { console.error(err.message); process.exit(1); });
