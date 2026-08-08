// Sematkan ikon dan metadata versi ke Padelin.exe setelah packaging.
//
// Normalnya electron-builder melakukan ini sendiri lewat rcedit. Tapi rcedit
// dibawa paket "winCodeSign" yang berisi symlink macOS, dan mengekstraknya di
// Windows butuh hak istimewa yang tidak dimiliki sesi biasa (Developer Mode
// mati, bukan admin) - ekstraksinya gagal, dan seluruh build ikut gagal.
//
// Jalan keluarnya bukan mematikan penyematan ikon: exe tanpa ikon tampil
// dengan lambang Electron bawaan di Start Menu, Desktop, dan taskbar, dan
// pengguna tidak punya cara tahu itu Padelin. Yang dimatikan cuma
// KETERGANTUNGAN pada paket bermasalah itu - rcedit dipasang langsung sebagai
// dev dependency, lalu dipanggil di sini.
//
// Dirujuk dari package.json (build.afterPack).

const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== 'win32') return;

  const exe = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  const icon = path.join(__dirname, 'build', 'icon.ico');
  const rcedit = path.join(__dirname, '..', 'node_modules', 'rcedit', 'bin', 'rcedit-x64.exe');

  for (const [label, p] of [['exe', exe], ['ikon', icon], ['rcedit', rcedit]]) {
    if (!fs.existsSync(p)) {
      console.warn(`  ! ${label} tidak ada (${p}) - ikon TIDAK disematkan`);
      return;
    }
  }

  const versi = context.packager.appInfo.version;
  execFileSync(rcedit, [
    exe,
    '--set-icon', icon,
    '--set-version-string', 'ProductName', 'Padelin',
    '--set-version-string', 'FileDescription', 'Padelin - jadwal meet, beres.',
    '--set-version-string', 'CompanyName', 'Padelin',
    '--set-version-string', 'LegalCopyright', 'Padelin',
    '--set-file-version', versi,
    '--set-product-version', versi,
  ], { stdio: 'inherit' });

  console.log(`  • ikon & metadata disematkan ke ${path.basename(exe)}`);
};
