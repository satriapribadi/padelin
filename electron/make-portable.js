// Susun paket PORTABLE yang tidak membuat satu pun executable baru.
//
// Kenapa ini ada. Smart App Control tidak menilai tanda tangan semata, tapi
// REPUTASI - dan itu terbukti di mesin uji: electron.exe bawaan npm sama-sama
// tidak bertanda tangan, tapi jalan mulus, sementara Padelin.exe hasil
// electron-builder diblokir (WinError 4551). Bedanya cuma satu: electron.exe
// sudah dikenal luas, Padelin.exe biner baru yang belum pernah dilihat siapa
// pun.
//
// Jadi paket ini sengaja TIDAK menyalin-ganti-nama electron.exe. Ia dibiarkan
// utuh apa adanya, dan identitas aplikasi (nama + ikon) dibawa oleh pintasan
// .lnk - yang bukan executable, jadi tidak kena aturan itu sama sekali.
//
// Konsekuensi jujurnya: di Task Manager prosesnya bernama electron.exe, bukan
// Padelin.exe. Itu harga yang dibayar untuk bisa jalan tanpa sertifikat.
//
//   npm run portable

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const OUT = path.join(ROOT, 'dist-portable', 'Padelin');
const ELECTRON_DIST = path.join(ROOT, 'node_modules', 'electron', 'dist');
const PYTHON_SRC = path.join(__dirname, 'vendor', 'python');

// Yang dibutuhkan aplikasi saat jalan. Sengaja daftar putih, bukan "salin semua
// lalu buang" - repo ini berisi database dan roster milik host, dan keduanya
// tidak boleh ikut terbawa ke paket yang dibagikan.
const APP_FILES = [
  ['electron/main.js', 'electron/main.js'],
  ['electron/updater.js', 'electron/updater.js'],
  ['electron/build/icon.ico', 'electron/build/icon.ico'],
  ['run.py', 'run.py'],
  ['package.json', 'package.json'],
];
const APP_DIRS = [
  ['padel_scheduler', 'padel_scheduler'],
  ['web', 'web'],
];

function salinDir(dari, ke, lewati = () => false) {
  fs.mkdirSync(ke, { recursive: true });
  for (const entry of fs.readdirSync(dari, { withFileTypes: true })) {
    const src = path.join(dari, entry.name);
    const dst = path.join(ke, entry.name);
    if (lewati(entry.name, src)) continue;
    if (entry.isDirectory()) salinDir(src, dst, lewati);
    else fs.copyFileSync(src, dst);
  }
}

function ukuran(dir) {
  let total = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    total += e.isDirectory() ? ukuran(p) : fs.statSync(p).size;
  }
  return total;
}

if (!fs.existsSync(ELECTRON_DIST)) {
  throw new Error('node_modules/electron belum ada. Jalankan `npm install` dulu.');
}
if (!fs.existsSync(PYTHON_SRC)) {
  throw new Error('Python bundel belum ada. Jalankan `npm run fetch-python` dulu.');
}
if (!fs.existsSync(path.join(__dirname, 'build', 'icon.ico'))) {
  throw new Error('Ikon belum dibuat. Jalankan `npm run icon` dulu.');
}

fs.rmSync(path.join(ROOT, 'dist-portable'), { recursive: true, force: true });

console.log('Menyalin runtime Electron (electron.exe dibiarkan UTUH)...');
salinDir(ELECTRON_DIST, OUT);

console.log('Menyalin berkas aplikasi...');
const APP = path.join(OUT, 'resources', 'app');
for (const [dari, ke] of APP_FILES) {
  const tujuan = path.join(APP, ke);
  fs.mkdirSync(path.dirname(tujuan), { recursive: true });
  fs.copyFileSync(path.join(ROOT, dari), tujuan);
}
for (const [dari, ke] of APP_DIRS) {
  salinDir(path.join(ROOT, dari), path.join(APP, ke),
    (nama) => nama === '__pycache__' || nama.endsWith('.pyc'));
}

console.log('Menyalin Python bundel...');
salinDir(PYTHON_SRC, path.join(OUT, 'resources', 'python'));

// Jalur modul di ._pth relatif ke folder python.exe. Di tata letak portable,
// aplikasinya ada di ../app - bukan ../app.asar.unpacked seperti di installer.
const pyDir = path.join(OUT, 'resources', 'python');
const pth = fs.readdirSync(pyDir).find((f) => f.endsWith('._pth'));
if (pth) {
  const nama = path.basename(pth, '._pth');
  fs.writeFileSync(path.join(pyDir, pth),
    [`${nama}.zip`, '.', '..\\app', '', 'import site', ''].join('\n'));
  console.log('  jalur modul diarahkan ke ..\\app');
}

// Pintasan pembawa nama & ikon. Inilah yang diklik pengguna.
console.log('Membuat pintasan...');
const ps = `
  $shell = New-Object -ComObject WScript.Shell
  $lnk = $shell.CreateShortcut('${path.join(ROOT, 'dist-portable', 'Padelin.lnk')}')
  $lnk.TargetPath = '${path.join(OUT, 'electron.exe')}'
  $lnk.WorkingDirectory = '${OUT}'
  $lnk.IconLocation = '${path.join(APP, 'electron', 'build', 'icon.ico')},0'
  $lnk.Description = 'Padelin - jadwal meet, beres.'
  $lnk.Save()
`;
execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', ps],
  { stdio: 'inherit' });

fs.writeFileSync(path.join(ROOT, 'dist-portable', 'BACA-DULU.txt'),
  [
    'Padelin - versi portable',
    '',
    'Klik dua kali Padelin.lnk untuk menjalankan.',
    'Tidak perlu memasang apa pun: Python sudah ikut di dalam folder ini.',
    '',
    'Seluruh folder ini bisa disalin ke flashdisk atau komputer lain apa adanya.',
    'Data acara TIDAK disimpan di folder ini, melainkan di folder data pengguna',
    'masing-masing komputer (%APPDATA%\\Padelin), jadi menyalin folder ini tidak',
    'ikut membawa data siapa pun.',
    '',
    'Kalau Padelin.lnk dipindah, ia tetap menunjuk ke folder Padelin di lokasi',
    'lamanya. Pindahkan seluruh isi dist-portable sekaligus, jangan pintasannya',
    'saja.',
    '',
  ].join('\r\n'));

console.log(`\nSelesai: ${path.join(ROOT, 'dist-portable')}`);
console.log(`Ukuran : ${Math.round(ukuran(path.join(ROOT, 'dist-portable')) / 1e6)} MB`);
