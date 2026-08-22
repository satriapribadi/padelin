// Rakit paket konten: isi aplikasi yang bisa diperbarui tanpa installer.
//
// Yang masuk cuma kode terjemahan - run.py, padel_scheduler/, web/. Tidak ada
// biner, tidak ada Electron, tidak ada Python. Itu bukan kebetulan: paket ini
// dipasang tanpa menjalankan apa pun yang baru, dan begitu ia memuat berkas
// yang bisa dieksekusi, sifat itu hilang. Lihat electron/konten.js.
//
// Keluarannya dua berkas di dist-desktop/:
//   konten-X.Y.Z.zip
//   konten.json      { versi, berkas, sha512, ukuran, app_minimal }
//
// Pakai:
//   node tools/paket-konten.js

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const AKAR = path.join(__dirname, '..');
const KELUAR = path.join(AKAR, 'dist-desktop');
const ISI = ['run.py', 'padel_scheduler', 'web'];

/** Salin, buang yang tidak perlu ikut.
 *
 * __pycache__ dan *.pyc ditinggal bukan karena boros - ukurannya kecil - tapi
 * karena bytecode yang dikompilasi untuk versi Python lain akan diam-diam
 * dipakai di mesin pengguna, dan salahnya muncul jauh dari sebabnya.
 */
function salin(dari, ke) {
  const st = fs.statSync(dari);
  if (st.isDirectory()) {
    if (path.basename(dari) === '__pycache__') return;
    fs.mkdirSync(ke, { recursive: true });
    fs.readdirSync(dari).forEach((n) => salin(path.join(dari, n), path.join(ke, n)));
    return;
  }
  if (dari.endsWith('.pyc')) return;
  fs.copyFileSync(dari, ke);
}

function main() {
  const pkg = JSON.parse(fs.readFileSync(path.join(AKAR, 'package.json'), 'utf8'));
  const versi = pkg.version;
  const minimal = pkg.kontenAppMinimal || '0.0.0';
  const berkas = `konten-${versi}.zip`;

  const panggung = path.join(KELUAR, `konten-panggung-${versi}`);
  const zip = path.join(KELUAR, berkas);
  fs.rmSync(panggung, { recursive: true, force: true });
  fs.mkdirSync(panggung, { recursive: true });
  ISI.forEach((n) => salin(path.join(AKAR, n), path.join(panggung, n)));

  fs.rmSync(zip, { force: true });
  // Compress-Archive ada di setiap Windows yang didukung; tidak menambah
  // dependensi, sama seperti Expand-Archive di sisi pemasangan.
  execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
    `Compress-Archive -Path '${panggung}\\*' -DestinationPath '${zip}' -Force`],
  { stdio: 'inherit' });
  fs.rmSync(panggung, { recursive: true, force: true });

  const isi = fs.readFileSync(zip);
  const manifes = {
    versi,
    berkas,
    sha512: crypto.createHash('sha512').update(isi).digest('base64'),
    ukuran: isi.length,
    // Batas bawah kerangka aplikasi yang sanggup menjalankan isi ini. Dinaikkan
    // tangan setiap kali electron/*.js ikut berubah - isi baru di atas proses
    // utama lama hanya sah selama kontraknya tidak berubah, dan yang tahu itu
    // berubah cuma yang mengubahnya.
    app_minimal: minimal,
  };
  fs.writeFileSync(path.join(KELUAR, 'konten.json'),
    `${JSON.stringify(manifes, null, 2)}\n`, 'utf8');

  console.log(`  * ${berkas}  ${(isi.length / 1024).toFixed(1)} KB`);
  console.log(`  * konten.json  versi ${versi}, butuh aplikasi >= ${minimal}`);
}

main();
