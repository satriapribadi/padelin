// Periksa installer yang baru dibangun SEBELUM ia diunggah.
//
// Bukan untuk menangkap build yang gagal - electron-builder sudah menangani itu
// sendiri: begitu ada task yang gagal, `isErrorOccurred` menyalakan
// publishManager.cancelTasks() dan tidak ada yang terunggah. Exit code-nya juga
// benar; yang pernah menyesatkan cuma `| tail` di terminal, yang mengambil
// status dari perintah terakhir pipeline, bukan dari npm.
//
// Yang dijaga di sini adalah bahaya yang justru tidak berbunyi: build yang
// BERHASIL tapi isinya salah. Kalau suatu saat pasangOrtools() di
// fetch-python.js gagal atau sengaja dilewati, yang keluar adalah installer
// ~96 MB yang sah sepenuhnya - lolos semua tes, nol error - tapi tanpa mode
// "Americano + solver eksak", karena UI menyembunyikan mode itu sendiri begitu
// OR-Tools tidak ada. Tidak ada satu pun sinyal sampai pengguna mengeluh.
//
// Ukuran artefak adalah tanda paling murah yang membedakan keduanya, dan
// selisihnya besar: ~96 MB tanpa OR-Tools, ~186 MB dengan.
//
// Dipasang sebagai afterAllArtifactBuild di package.json, jadi ia berjalan di
// SETIAP build - bukan cuma saat rilis. Melemparnya membuat electron-builder
// membatalkan unggahan.
//
// Batasnya: unggahan dijadwalkan saat artefak dibuat, jadi berkas besar bisa
// sudah mulai terkirim ketika hook ini berjalan. Untuk memastikan tidak ada
// yang terlanjur, bangun dulu tanpa publish (`npm run dist`) lalu rilis.
//
// Bisa juga dijalankan sendiri terhadap dist-desktop yang sudah ada:
//
//   npm run verify:dist

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const AKAR = path.join(__dirname, '..');
const DIST = path.join(AKAR, 'dist-desktop');

// Rentang ukuran installer yang wajar, dalam MB.
//
// Lantainya sengaja jauh DI ATAS ukuran tanpa OR-Tools (~96 MB): itulah satu-
// satunya cara pemeriksaan ini membedakan "OR-Tools ikut" dari "tidak ikut".
// Kalau suatu hari Anda memang memutuskan merilis tanpa OR-Tools, angka ini
// harus diturunkan bersamaan - dan kegagalan di sini adalah pengingat untuk
// melakukannya secara sadar, bukan gangguan.
//
// Atapnya menangkap arah sebaliknya: berkas yang tidak sengaja ikut terbundel.
const MIN_MB = 130;
const MAX_MB = 400;

const MB = 1024 * 1024;

// MELEMPAR, bukan process.exit. Bedanya menentukan: dipanggil sebagai hook,
// process.exit membunuh electron-builder di tempat sehingga ia tidak sempat
// menjalankan publishManager.cancelTasks() - dan unggahan yang sudah terjadwal
// tetap jalan. Yang dilempar ditangkap oleh jalur mandiri di bawah.
function gagal(pesan) {
  throw new Error(`Pemeriksaan artefak GAGAL\n    ${pesan}`);
}

function sha512(file) {
  return crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64');
}

function main() {
  const { version } = JSON.parse(
    fs.readFileSync(path.join(AKAR, 'package.json'), 'utf8'));
  const exe = path.join(DIST, `Padelin-${version}-x64.exe`);

  if (!fs.existsSync(exe)) {
    gagal(`Installer tidak ada: ${exe}\n`
      + `    Build-nya tidak sampai selesai.`);
  }

  const bytes = fs.statSync(exe).size;
  const mb = bytes / MB;

  if (mb < MIN_MB) {
    // Dua sebab yang paling mungkin, dan keduanya perlu disebut - kalau cuma
    // "terlalu kecil", orang akan menebak-nebak.
    gagal(`Installer cuma ${mb.toFixed(1)} MB, di bawah batas ${MIN_MB} MB.\n`
      + `    Kemungkinan besar OR-Tools tidak ikut terbundel, sehingga mode\n`
      + `    "Americano + solver eksak" akan hilang diam-diam dari UI.\n`
      + `    Periksa electron/vendor/python/Lib/site-packages/ortools.\n`
      + `    Kalau memang sengaja dirilis tanpa OR-Tools, turunkan MIN_MB di\n`
      + `    berkas ini supaya keputusan itu tercatat.`);
  }
  if (mb > MAX_MB) {
    gagal(`Installer ${mb.toFixed(1)} MB, melewati batas ${MAX_MB} MB.\n`
      + `    Ada yang ikut terbundel tanpa sengaja. Periksa "files" dan\n`
      + `    "extraResources" di package.json.`);
  }

  // latest.yml yang dipakai auto-update. Angka di dalamnya HARUS cocok dengan
  // berkasnya: kalau tidak, pembaruan pengguna gagal di tengah unduhan dengan
  // pesan checksum - kegagalan yang menimpa semua orang sekaligus dan tidak
  // bisa ditarik kembali tanpa rilis baru.
  //
  // Tapi ia hanya diregenerasi saat MEMPUBLIKASIKAN. Build biasa
  // (`npm run dist`, yang memakai --publish never) meninggalkan latest.yml dari
  // rilis sebelumnya, jadi membandingkannya dengan exe yang baru selalu meleset
  // dan build yang sehat ditolak. Itu bukan hipotesis: pemeriksaan ini memang
  // menggagalkan build pertamanya sendiri seperti itu.
  //
  // Karena itu ymlnya cuma diperiksa kalau ia memang milik build ini, yang
  // ditandai oleh waktu tulis tidak lebih tua daripada exe-nya.
  const yml = path.join(DIST, 'latest.yml');
  const ymlSeumuran = fs.existsSync(yml)
    && fs.statSync(yml).mtimeMs >= fs.statSync(exe).mtimeMs;
  if (!ymlSeumuran) {
    console.log(`  * artefak diperiksa  ${path.basename(exe)}  `
      + `${mb.toFixed(1)} MB (batas ${MIN_MB}-${MAX_MB} MB)`);
    console.log('    latest.yml dilewati - belum ditulis untuk build ini '
      + '(baru dibuat saat publish)');
    return;
  }
  {
    const isi = fs.readFileSync(yml, 'utf8');
    const ukuranYml = /^\s*size:\s*(\d+)\s*$/m.exec(isi);
    if (ukuranYml && Number(ukuranYml[1]) !== bytes) {
      gagal(`latest.yml menyebut ${Number(ukuranYml[1]).toLocaleString('id-ID')} byte, `
        + `berkasnya ${bytes.toLocaleString('id-ID')} byte.\n`
        + `    Auto-update akan menolak unduhannya.`);
    }
    const shaYml = /^sha512:\s*(\S+)\s*$/m.exec(isi);
    if (shaYml && shaYml[1] !== sha512(exe)) {
      gagal('sha512 di latest.yml tidak cocok dengan installer-nya.\n'
        + '    Auto-update akan menolak unduhannya.');
    }
  }

  console.log(`  * artefak diperiksa  ${path.basename(exe)}  `
    + `${mb.toFixed(1)} MB (batas ${MIN_MB}-${MAX_MB} MB), latest.yml cocok`);
}

// Dipanggil electron-builder sebagai afterAllArtifactBuild. Mengembalikan array
// kosong: tidak ada artefak baru yang ditambahkan, cuma diperiksa.
module.exports = function afterAllArtifactBuild() {
  main();
  return [];
};

// Jalur mandiri (`npm run verify:dist`): pesannya dicetak rapi, lalu keluar
// dengan status gagal supaya bisa dipakai di rantai perintah.
if (require.main === module) {
  try {
    main();
  } catch (err) {
    console.error(`\n  x ${err.message}\n`);
    process.exit(1);
  }
}
