// Uji jalur cetak di dalam Electron, memakai electron/cetak.js YANG ASLI.
//
// Dijalankan oleh tools/cetaktest.py, bukan langsung:
//   electron tools/cetak_e2e.js <akar-repo> <laporan.html>
//
// Kenapa harus di dalam Electron: yang diuji printToPDF(), protokol
// padelin-pratinjau://, preload, dan penampil PDF Chromium - tidak satu pun ada
// di luar Electron. Dan kenapa memakai modul aslinya, bukan tiruannya: versi
// pertama uji ini membuat jendelanya sendiri dengan webPreferences yang
// disalin-tangan, lalu LULUS sementara aplikasinya tetap salah.
//
// Yang TIDAK diuji di sini: dialog Save dan dialog printer. Keduanya memblokir
// proses sampai ada yang menekan tombol, jadi keduanya memang harus dicoba
// dengan tangan.
const path = require('path');
const fs = require('fs');
const http = require('http');

const REPO = process.argv[process.argv.length - 2];
const LAPORAN = process.argv[process.argv.length - 1];

const { app, BrowserWindow } = require('electron');
const cetak = require(path.join(REPO, 'electron', 'cetak.js'));

const laporanHtml = fs.readFileSync(LAPORAN, 'utf8');
const hasil = [];
const tunggu = (ms) => new Promise((r) => setTimeout(r, ms));

function catat(nama, ok, detail) {
  hasil.push({ ok, nama, detail: detail || '' });
  console.log(`  [${ok ? 'OK ' : 'GAGAL'}] ${nama}${detail ? ' - ' + detail : ''}`);
}

app.whenReady().then(async () => {
  cetak.siapkan({ dev: false });

  // Laporan disajikan lewat HTTP, sama seperti aplikasi menyajikannya - bukan
  // file://, supaya asal halamannya sama dengan yang sesungguhnya.
  const srv = http.createServer((_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(laporanHtml);
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const asal = `http://127.0.0.1:${srv.address().port}`;

  // --- 1. Jendela laporan, webPreferences seperti aturJendelaBaru() -------
  const laporan = new BrowserWindow({
    show: false,
    width: 1000,
    height: 900,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: cetak.PRELOAD,
    },
  });
  laporan.webContents.on('preload-error', (_e, p, err) =>
    catat('preload laporan tanpa error', false, `${p}: ${err}`));
  await laporan.loadURL(asal + '/laporan');

  const toolbar = JSON.parse(await laporan.webContents.executeJavaScript(
    'JSON.stringify({'
    + 'jembatan: typeof window.padelin,'
    + 'fungsi: window.padelin ? Object.keys(window.padelin).sort() : [],'
    + 'label: (document.getElementById("pdf")||{}).textContent,'
    + 'tombol: document.querySelectorAll(".toolbar button").length,'
    + 'judul: document.title'
    + '})'));

  catat('jembatan padelin ada di laporan', toolbar.jembatan === 'object',
    `typeof=${toolbar.jembatan}`);
  catat('fungsi jembatan lengkap',
    JSON.stringify(toolbar.fungsi) === '["cetak","info","pratinjau","simpan"]',
    toolbar.fungsi.join(', '));
  catat('tombol memakai jalur pratinjau', toolbar.label === 'Pratinjau cetak',
    `label="${toolbar.label}"`);
  catat('toolbar tidak memasang tombol printer', toolbar.tombol === 2,
    `${toolbar.tombol} tombol`);

  // --- 2. Klik tombolnya. Dari sini semuanya kode aplikasi. ---------------
  const munculPratinjau = new Promise((resolve) => {
    app.once('browser-window-created', (_e, win) => {
      win.webContents.once('did-finish-load', () => resolve(win));
      win.webContents.on('preload-error', (_e2, p, err) =>
        catat('preload pratinjau tanpa error', false, `${p}: ${err}`));
    });
  });
  await laporan.webContents.executeJavaScript('document.getElementById("pdf").click()');
  const pratinjau = await munculPratinjau;
  await tunggu(3000);   // penampil PDF perlu waktu merender halaman pertama

  const isi = JSON.parse(await pratinjau.webContents.executeJavaScript(
    'JSON.stringify({'
    + 'url: location.href,'
    + 'judul: document.title,'
    + 'halaman: document.getElementById("halaman").textContent,'
    + 'kabar: document.getElementById("kabar").textContent,'
    + 'embed: !!document.getElementById("dokumen"),'
    + 'tinggi: (document.getElementById("dokumen")||{}).clientHeight,'
    + 'aktif: !document.getElementById("simpan").disabled'
    + '})'));

  catat('pratinjau dilayani protokol padelin-pratinjau',
    isi.url.startsWith('padelin-pratinjau://p'), isi.url);
  catat('judul laporan ada di bilah jendela',
    isi.judul === `Pratinjau cetak - ${toolbar.judul}`, `"${isi.judul}"`);
  catat('jumlah halaman disebut', /^\d+ halaman A4$/.test(isi.halaman),
    `"${isi.halaman}"`);
  catat('tidak ada pesan galat di header', isi.kabar === '', `"${isi.kabar}"`);
  catat('tombol aksi hidup', isi.aktif === true);
  catat('penampil PDF mengisi jendela', isi.embed && isi.tinggi > 400,
    `tinggi=${isi.tinggi}px`);
  catat('pratinjau jadi anak jendela laporan',
    !!pratinjau.getParentWindow() && pratinjau.getParentWindow().id === laporan.id);

  // --- 3. Ctrl+P di dalam pratinjau tidak boleh menumpuk pratinjau --------
  const ulang = await cetak.bukaPratinjau(pratinjau);
  catat('pratinjau dari pratinjau ditolak', ulang.status === 'sudah-terbuka',
    `status=${ulang.status}`);

  // --- 4. Halaman PDF-nya BENAR-BENAR terender? --------------------------
  //
  // Memeriksa elemen embed saja tidak cukup, dan ini bukan kehati-hatian
  // berlebihan: saat dokumennya pernah ditolak CSP, embed-nya tetap ada dan
  // tetap setinggi jendela, dan seluruh pemeriksaan di atas LULUS sementara
  // jendelanya kosong. Jadi yang dilihat pikselnya - di area dokumen harus ada
  // kertas A4 putih, bukan latar penampil yang gelap.
  const gambar = await pratinjau.webContents.capturePage();
  const { width, height } = gambar.getSize();
  const bitmap = gambar.toBitmap();           // BGRA
  let terang = 0;
  let dicek = 0;
  for (let y = Math.floor(height * 0.30); y < height * 0.75; y += 4) {
    for (let x = Math.floor(width * 0.45); x < width * 0.80; x += 4) {
      const i = (y * width + x) * 4;
      if ((bitmap[i] + bitmap[i + 1] + bitmap[i + 2]) / 3 > 200) terang++;
      dicek++;
    }
  }
  const persen = Math.round((terang / dicek) * 100);
  catat('halaman PDF terender (area dokumen berupa kertas putih)', persen > 70,
    `${persen}% piksel terang dari ${dicek} sampel`);

  if (process.env.PADELIN_UJI_TANGKAPAN) {
    fs.writeFileSync(process.env.PADELIN_UJI_TANGKAPAN, gambar.toPNG());
    console.log('  tangkapan layar:', process.env.PADELIN_UJI_TANGKAPAN);
  }

  // --- 5. Jalur browser: tanpa preload, perilaku lama bertahan -----------
  const browser = new BrowserWindow({
    show: false,
    webPreferences: { contextIsolation: true },
  });
  await browser.loadURL(asal + '/laporan');
  const tanpa = JSON.parse(await browser.webContents.executeJavaScript(
    'JSON.stringify({jembatan: typeof window.padelin,'
    + ' label: document.getElementById("pdf").textContent})'));
  catat('tanpa jembatan, label tetap untuk browser',
    tanpa.jembatan === 'undefined' && tanpa.label === 'Simpan sebagai PDF',
    `typeof=${tanpa.jembatan}, label="${tanpa.label}"`);

  // --- 6. Dokumen dilepas saat pratinjau ditutup -------------------------
  pratinjau.destroy();
  await tunggu(400);
  const lagi = await cetak.bukaPratinjau(laporan);
  catat('pratinjau bisa dibuka lagi setelah ditutup', lagi.status === 'terbuka',
    `status=${lagi.status}`);

  const gagal = hasil.filter((h) => !h.ok);
  console.log(`\n${hasil.length - gagal.length} lulus, ${gagal.length} gagal`);
  app.exit(gagal.length ? 1 : 0);
}).catch((err) => {
  console.log('  [GAGAL] harness berhenti -', err && err.stack ? err.stack : err);
  app.exit(1);
});
