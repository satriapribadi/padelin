// Cetak dan pratinjau.
// ============================================================================
// Dialog cetak Windows memperlihatkan panel kosong bertuliskan "This app doesn't
// support print preview", dan itu bukan bug yang bisa kita tutup: panel itu
// digambar dari aliran halaman yang harus DISEDIAKAN aplikasi pemanggil, dan UI
// pratinjau milik Chrome (chrome://print) hidup di lapisan //chrome yang tidak
// diikutkan Electron. Tidak ada opsi JS yang menyalakannya; satu-satunya jalan
// lain adalah menambal Chromium lalu membangun ulang Electron.
//
// Untuk laporan sepanjang ini, memeriksa jadi berapa lembar dan di mana
// halamannya terpotong justru SATU-SATUNYA alasan membuka pratinjau. Jadi
// pratinjaunya dibuat sendiri:
//
//   printToPDF()   -> halaman yang persis akan tercetak (CSS @media print dan
//                     @page yang sama dengan laporan)
//   pratinjau.html -> PDF itu ditampilkan penampil PDF bawaan Chromium, yang
//                     ternyata memang ikut di Electron: thumbnail, nomor
//                     halaman, zoom - semuanya di dalam jendela Padelin
//
// Dokumennya dilayani dari MEMORI lewat protokol padelin-pratinjau://, bukan
// berkas temp. Laporan memuat nama peserta, dan tidak ada alasan menuliskannya
// ke disk hanya untuk dilihat sebentar.
//
// Dialog printer Windows tetap ada, tapi pindah ke urutan terakhir: ia dipanggil
// dari dalam pratinjau, setelah host melihat halamannya. Panelnya masih kosong -
// itu batas Electron - tapi tidak ada lagi yang perlu diperiksa di situ.
//
// Modul ini dipisah dari main.js supaya harness verifikasi bisa memanggil kode
// yang SAMA dengan yang dipakai aplikasi. Uji yang meniru-niru jalur cetak
// pernah lulus sementara aplikasinya tetap salah; sejak itu tidak lagi.
// ============================================================================
const { app, BrowserWindow, dialog, ipcMain, protocol, shell } = require('electron');
const path = require('path');
const fs = require('fs');

const SKEMA_PRATINJAU = 'padelin-pratinjau';
const PRELOAD = path.join(__dirname, 'preload.js');
const HALAMAN_PRATINJAU = path.join(__dirname, 'pratinjau.html');

// registerSchemesAsPrivileged HARUS dipanggil sebelum app siap, jadi tempatnya
// di badan modul - main.js me-require berkas ini di baris atas.
//
// Skemanya wajib "standard" + "secure": tanpa standard ia tidak punya origin,
// sehingga src relatif di pratinjau.html tidak terselesaikan; tanpa secure
// Chromium menganggapnya konteks tidak aman dan penampil PDF menolak memuat.
protocol.registerSchemesAsPrivileged([
  { scheme: SKEMA_PRATINJAU, privileges: { standard: true, secure: true } },
]);

/** Pratinjau yang sedang terbuka: id jendela -> dokumennya.
 *
 * PDF-nya disimpan di sini, bukan di disk, dan dibuang saat jendelanya ditutup.
 * Kuncinya id jendela supaya menu tahu jendela mana yang sedang berisi
 * pratinjau - "Simpan sebagai PDF" di jendela pratinjau harus menyimpan PDF
 * yang SEDANG DILIHAT, bukan merender ulang halaman penampil PDF-nya.
 */
const pratinjauAktif = new Map();

let DEV = false;
function log(...pesan) {
  if (DEV) console.log('[cetak]', ...pesan);
}

/** Judul halaman jadi nama berkas yang aman untuk Windows. */
function namaBerkasPdf(judul) {
  const bersih = String(judul || '')
    .replace(/[<>:"/\\|?*]/g, ' ')
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x1f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 100);
  return `${bersih || 'Laporan Padelin'}.pdf`;
}

/** Jumlah halaman PDF, atau 0 kalau tidak terbaca.
 *
 * Dihitung dari objek /Type /Page di berkasnya. Cara ini tidak umum benar untuk
 * PDF apa pun - sejak PDF 1.5 objeknya boleh dipadatkan ke dalam object stream
 * dan tidak lagi terlihat sebagai teks - tapi di sini sumbernya cuma satu:
 * printToPDF() milik Chromium, yang menulisnya apa adanya. Karena itu
 * kegagalannya dijawab dengan 0, bukan dengan menebak: label jumlah halaman
 * lebih baik hilang daripada salah.
 */
function jumlahHalamanPdf(pdf) {
  const cocok = pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g);
  return cocok ? cocok.length : 0;
}

/** Rakit PDF dari sebuah webContents. Pemanggilnya yang memutuskan mau diapakan. */
function rakitPdf(wc) {
  // preferCSSPageSize: laporan sudah menetapkan @page A4 portrait beserta
  // marginnya. Tanpa ini ukuran kertas Electron yang menang dan isinya
  // diskalakan ulang - tabel rekap bergeser dari tata letak yang diuji.
  return wc.printToPDF({ printBackground: true, preferCSSPageSize: true });
}

/** Layani halaman pratinjau dan dokumennya dari memori.
 *
 * Host URL-nya membawa id jendela (p12), jadi dua pratinjau yang terbuka
 * sekaligus tidak saling menimpa dokumen. Pembungkus dan PDF berada di origin
 * yang sama, jadi src embed cukup relatif dan tidak ada aturan file:// yang
 * perlu dilonggarkan.
 */
function layaniPratinjau(req) {
  const url = new URL(req.url);
  const sesi = pratinjauAktif.get(Number(url.hostname.replace(/^p/, '')));
  if (!sesi) {
    return new Response('Pratinjaunya sudah ditutup.', {
      status: 404,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
  if (url.pathname === '/dokumen.pdf') {
    return new Response(sesi.pdf, {
      headers: {
        'Content-Type': 'application/pdf',
        'Content-Length': String(sesi.pdf.length),
      },
    });
  }
  if (url.pathname === '/pratinjau.html') {
    return new Response(fs.readFileSync(HALAMAN_PRATINJAU), {
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
    });
  }
  // Origin ini hanya punya dua berkas. Sisanya - favicon, apa pun - dijawab
  // tidak ada, bukan dijawab HALAMAN pratinjau.
  return new Response('Tidak ada.', {
    status: 404,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

/** Buka jendela pratinjau untuk isi `win`. */
async function bukaPratinjau(win) {
  if (!win || win.isDestroyed()) return { status: 'gagal' };
  // Menekan Ctrl+P di dalam pratinjau tidak boleh membuat pratinjau dari
  // pratinjau; yang diinginkan host jelas: yang ini, tetap di depan.
  if (pratinjauAktif.has(win.id)) {
    win.focus();
    return { status: 'sudah-terbuka' };
  }

  const sumber = win.webContents;
  const judul = sumber.getTitle();
  let pdf;
  try {
    pdf = await rakitPdf(sumber);
  } catch (err) {
    dialog.showErrorBox('Pratinjau gagal dibuat', err.message);
    return { status: 'gagal' };
  }

  const pratinjau = new BrowserWindow({
    width: 1040,
    height: 940,
    // Anak dari laporannya: ia tidak bisa tersembunyi di belakang jendela
    // induknya, dan ikut tertutup kalau laporannya ditutup - tidak ada
    // pratinjau yatim yang isinya tidak bisa dicetak lagi.
    parent: win,
    backgroundColor: '#0f1419',   // sama dengan --bg aplikasi
    title: 'Pratinjau cetak',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // Penampil PDF bawaan Chromium ikut di Electron, tapi hanya menyala
      // kalau plugin diizinkan. Tanpa ini halamannya kosong.
      plugins: true,
      preload: PRELOAD,
    },
  });

  pratinjauAktif.set(pratinjau.id, { pdf, judul, sumber });
  pratinjau.on('closed', () => pratinjauAktif.delete(pratinjau.id));
  pratinjau.loadURL(`${SKEMA_PRATINJAU}://p${pratinjau.id}/pratinjau.html`);
  log('pratinjau dibuka:', judul,
    `${jumlahHalamanPdf(pdf)} halaman, ${Math.round(pdf.length / 1024)} KB`);
  return { status: 'terbuka', id: pratinjau.id };
}

/** Simpan PDF ke lokasi pilihan host.
 *
 * Dari jendela pratinjau: yang ditulis adalah byte yang SEDANG dilihat, bukan
 * hasil render ulang - jadi tidak ada celah antara pratinjau dan berkasnya.
 * Dari jendela laporan (menu, tanpa membuka pratinjau): dirender saat itu.
 */
async function simpanPdf(win) {
  if (!win || win.isDestroyed()) return { status: 'gagal' };
  const sesi = pratinjauAktif.get(win.id);
  const judul = sesi ? sesi.judul : win.webContents.getTitle();
  log('simpan PDF:', judul, sesi ? '(dari pratinjau)' : '(render langsung)');

  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title: 'Simpan sebagai PDF',
    defaultPath: path.join(app.getPath('documents'), namaBerkasPdf(judul)),
    filters: [{ name: 'PDF', extensions: ['pdf'] }],
  });
  if (canceled || !filePath) return { status: 'batal' };

  try {
    fs.writeFileSync(filePath, sesi ? sesi.pdf : await rakitPdf(win.webContents));
  } catch (err) {
    dialog.showErrorBox('PDF gagal disimpan', err.message);
    return { status: 'gagal' };
  }

  // Dari pratinjau, halamannya sudah dilihat - cukup kabari di header jendela
  // itu (nama berkasnya dikembalikan ke halaman). Dari menu, tidak ada tempat
  // untuk mengabari, jadi berkasnya disorot di Explorer: itu bukti tanpa modal,
  // dan tidak bergantung pada ada-tidaknya aplikasi pembuka PDF.
  if (!sesi) shell.showItemInFolder(filePath);
  log('PDF tersimpan:', filePath);
  return { status: 'tersimpan', nama: path.basename(filePath) };
}

/** Cetak ke printer. Dari pratinjau, yang dicetak laporan sumbernya.
 *
 * Sengaja bukan jendela pratinjaunya: mencetak halaman yang isinya penampil PDF
 * berarti menyerahkan hasilnya ke jalur plugin, sementara mencetak laporan
 * langsung adalah jalur yang sudah terbukti - dan keluarannya sama, karena PDF
 * yang dilihat tadi dirender dari halaman yang sama.
 */
function cetakKePrinter(win) {
  if (!win || win.isDestroyed()) return Promise.resolve({ status: 'gagal' });
  const sesi = pratinjauAktif.get(win.id);
  const wc = sesi ? sesi.sumber : win.webContents;
  if (!wc || wc.isDestroyed()) {
    dialog.showErrorBox('Laporannya sudah ditutup',
      'Buka laporannya lagi, lalu cetak dari situ.');
    return Promise.resolve({ status: 'gagal' });
  }

  log('dialog printer Windows dibuka (panelnya memang tanpa pratinjau)');
  return new Promise((resolve) => {
    wc.print({ printBackground: true }, (ok, alasan) => {
      // Membatalkan dialog bukan kegagalan; sisanya harus terdengar, kalau
      // tidak tombol Cetak cuma diam dan host mengira aplikasinya menggantung.
      if (!ok && alasan && !/cancel/i.test(alasan)) {
        dialog.showErrorBox('Gagal mencetak', alasan);
      }
      resolve({ status: ok ? 'tercetak' : 'batal' });
    });
  });
}

/** Info untuk header jendela pratinjau. null kalau bukan jendela pratinjau. */
function infoPratinjau(win) {
  const sesi = win && pratinjauAktif.get(win.id);
  if (!sesi) return null;
  return { judul: sesi.judul, halaman: jumlahHalamanPdf(sesi.pdf) };
}

/** Pasang protokol dan jembatan IPC. Dipanggil sekali, setelah app siap. */
function siapkan({ dev = false } = {}) {
  DEV = dev;
  protocol.handle(SKEMA_PRATINJAU, layaniPratinjau);

  const jendela = (e) => BrowserWindow.fromWebContents(e.sender);
  ipcMain.handle('padelin:pratinjau', (e) => bukaPratinjau(jendela(e)));
  ipcMain.handle('padelin:simpan', (e) => simpanPdf(jendela(e)));
  ipcMain.handle('padelin:cetak', (e) => cetakKePrinter(jendela(e)));
  ipcMain.handle('padelin:info', (e) => infoPratinjau(jendela(e)));
}

module.exports = {
  PRELOAD,
  SKEMA_PRATINJAU,
  siapkan,
  bukaPratinjau,
  simpanPdf,
  cetakKePrinter,
  // Dibuka untuk harness verifikasi.
  jumlahHalamanPdf,
  namaBerkasPdf,
};
