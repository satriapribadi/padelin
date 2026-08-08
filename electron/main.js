// Pembungkus desktop untuk Padelin.
//
// Aplikasinya sendiri tidak berubah: tetap server HTTP Python (run.py) yang
// menyajikan web/. Electron hanya menyalakan server itu, menunggunya siap, lalu
// menampilkannya di jendela sendiri. Kode di web/ tetap nol dependency dan tetap
// bisa dibuka lewat browser biasa - membungkusnya di sini tidak menguncinya.
//
// Tiga hal yang tidak boleh salah:
//   1. Port. Memakai 8770 tetap akan bentrok kalau host sudah menjalankan
//      run.py sendiri, jadi port dipilih dari yang benar-benar kosong.
//   2. Kesiapan. Memuat jendela sebelum server mendengarkan menghasilkan layar
//      "tidak bisa terhubung", jadi ditunggu sampai benar-benar menjawab.
//   3. Mematikan. Proses Python harus ikut mati saat aplikasi ditutup; kalau
//      tidak, ia menggantung di latar dan port-nya bocor tiap kali dibuka.

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const { periksaPembaruan } = require('./updater');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');

const ROOT = path.join(__dirname, '..');
const DEV = process.argv.includes('--dev');

let serverProc = null;
let serverPort = 0;
let mainWindow = null;
// Baris terakhir dari server, dipakai kalau ia mati mendadak - tanpa ini pesan
// errornya cuma "gagal jalan" tanpa alasan.
const serverLog = [];

function logServer(chunk) {
  const text = String(chunk).trimEnd();
  if (!text) return;
  for (const line of text.split(/\r?\n/)) {
    serverLog.push(line);
    if (serverLog.length > 40) serverLog.shift();
    if (DEV) console.log('[server]', line);
  }
}

/** Minta satu port kosong ke OS, lalu lepaskan lagi.
 *
 * Ada celah kecil antara dilepas dan dipakai Python. Itu diterima: alternatifnya
 * membiarkan Python memilih sendiri lalu membaca portnya dari stdout, yang
 * bergantung pada buffering dan format teks - lebih rapuh daripada celah ini.
 */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Folder resources paket yang dibagikan, atau null kalau jalan dari repo.
 *
 * SENGAJA tidak memakai app.isPackaged. Electron menentukan nilai itu dari nama
 * berkas executable-nya - kalau namanya masih electron.exe, ia dianggap "belum
 * dipaketkan". Paket portable justru MEMBIARKAN nama itu apa adanya (biner yang
 * diganti nama kehilangan reputasinya di Smart App Control), jadi app.isPackaged
 * di sana bernilai false padahal jelas-jelas sudah dipaketkan.
 *
 * Akibatnya kalau dipercaya: aplikasi diam-diam memakai Python SISTEM alih-alih
 * Python bundel, dan database ditulis ke dalam folder paket. Di mesin
 * pengembang keduanya tidak terlihat salah - baru ketahuan di komputer yang
 * belum punya Python, yaitu tepat yang menjadi alasan Python dibundel.
 *
 * Jadi yang diperiksa keberadaan berkasnya, bukan nama executable-nya.
 */
function distRoot() {
  const rp = process.resourcesPath;
  if (!rp) return null;
  const tanda = [
    path.join(rp, 'python'),
    path.join(rp, 'app.asar.unpacked'),
    path.join(rp, 'app', 'run.py'),
  ];
  return tanda.some((p) => fs.existsSync(p)) ? rp : null;
}

/** Akar berkas aplikasi: di dalam paket isinya ada di resources. */
function resourcesRoot() {
  return distRoot() || ROOT;
}

/** Akar berkas PYTHON. Sengaja dipisah dari resourcesRoot().
 *
 * Berkas aplikasi dikemas ke dalam app.asar, dan Python tidak bisa membaca isi
 * arsip itu - `python run.py` cuma melaporkan berkasnya tidak ada, dan
 * aplikasinya gagal jalan padahal versi dari repo baik-baik saja. Karena itu
 * run.py, padel_scheduler/, dan web/ dikeluarkan dari asar (asarUnpack) dan
 * tinggal sebagai berkas biasa di app.asar.unpacked.
 */
function pythonAppRoot() {
  const dist = distRoot();
  if (!dist) return ROOT;
  // Dua tata letak yang sah:
  //   resources/app.asar.unpacked  <- hasil electron-builder (asar + unpack)
  //   resources/app                <- paket portable, berkas apa adanya
  const kandidat = [
    path.join(dist, 'app.asar.unpacked'),
    path.join(dist, 'app'),
  ];
  return kandidat.find((p) => fs.existsSync(path.join(p, 'run.py'))) || kandidat[0];
}

/** Python mana yang dipakai, berurutan dari yang paling pasti.
 *
 * Padelin tidak memakai satu pun paket pihak ketiga - seluruhnya pustaka
 * standar - jadi Python bisa ikut dibundel apa adanya dan pengguna tidak perlu
 * memasang apa pun. Itu yang didahulukan. Sisanya untuk menjalankan dari repo.
 */
function pythonCommand() {
  const R = resourcesRoot();
  const win = process.platform === 'win32';
  const kandidat = [
    // 1. Python yang ikut dibundel di installer.
    win ? path.join(R, 'python', 'python.exe')
      : path.join(R, 'python', 'bin', 'python3'),
    // 2. Virtualenv repo, kalau dijalankan dari sumber.
    win ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
      : path.join(ROOT, '.venv', 'bin', 'python'),
  ];
  for (const p of kandidat) {
    if (fs.existsSync(p)) return { cmd: p, args: [], bundled: p.startsWith(R) };
  }
  // 3. Python sistem. Hanya relevan saat pengembangan; di paket terpasang
  //    seharusnya tidak pernah sampai ke sini.
  return win
    ? { cmd: 'python', args: [], bundled: false }
    : { cmd: 'python3', args: [], bundled: false };
}

/** Di mana database disimpan.
 *
 * Folder aplikasi yang terpasang lewat installer umumnya hanya-baca, dan
 * datanya milik pengguna - bukan bagian dari program. Jadi database ditaruh di
 * folder data pengguna, yang juga membuatnya selamat saat aplikasi diperbarui
 * atau dipasang ulang. Dijalankan dari repo, tetap padel.db di repo.
 */
function databasePath() {
  // Sama seperti di atas: yang menentukan tata letak berkasnya, bukan
  // app.isPackaged. Kalau tertukar, paket portable menulis database ke DALAM
  // foldernya sendiri - ikut tersalin saat foldernya dibagikan, membawa data
  // acara orang lain tanpa ada yang menyadarinya.
  return distRoot()
    ? path.join(app.getPath('userData'), 'padel.db')
    : path.join(ROOT, 'padel.db');
}

/** Tunggu sampai server benar-benar menjawab, bukan sekadar prosesnya hidup. */
function waitForServer(port, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const coba = () => {
      if (serverProc && serverProc.exitCode !== null) {
        reject(new Error(
          `Server berhenti dengan kode ${serverProc.exitCode}.\n\n`
          + serverLog.slice(-12).join('\n')));
        return;
      }
      const req = http.get(
        { host: '127.0.0.1', port, path: '/', timeout: 1500 },
        (res) => { res.resume(); resolve(); },
      );
      req.on('error', () => {
        if (Date.now() > deadline) {
          reject(new Error(
            'Server tidak merespons dalam 25 detik.\n\n'
            + serverLog.slice(-12).join('\n')));
        } else {
          setTimeout(coba, 200);
        }
      });
      req.on('timeout', () => req.destroy());
    };
    coba();
  });
}

async function startServer() {
  serverPort = await freePort();
  const { cmd, args } = pythonCommand();
  // -u: keluaran tidak di-buffer, jadi log server terbaca saat kejadian, bukan
  // menumpuk lalu muncul sekaligus ketika prosesnya mati.
  const appRoot = pythonAppRoot();
  const argv = [...args, '-u', path.join(appRoot, 'run.py'),
    '--port', String(serverPort), '--host', '127.0.0.1', '--no-browser'];

  serverProc = spawn(cmd, argv, {
    cwd: appRoot,
    windowsHide: true,
    env: { ...process.env, PADELIN_DB: databasePath(), PYTHONUTF8: '1' },
  });
  serverProc.stdout.on('data', logServer);
  serverProc.stderr.on('data', logServer);

  serverProc.on('error', (err) => {
    fatal('Python tidak bisa dijalankan',
      `Perintah: ${cmd}\n\n${err.message}\n\n`
      + 'Pastikan Python terpasang, atau buat virtualenv di folder .venv.');
  });
  serverProc.on('exit', (code, signal) => {
    // Keluar wajar saat aplikasi ditutup tidak perlu dilaporkan.
    if (app.isQuitting || signal === 'SIGTERM') return;
    fatal('Server berhenti mendadak',
      `Kode keluar: ${code}\n\n${serverLog.slice(-12).join('\n')}`);
  });

  await waitForServer(serverPort);
}

function stopServer() {
  if (!serverProc || serverProc.exitCode !== null) return;
  serverProc.kill();
  // Kalau belum juga mati, paksa - jangan tinggalkan proses menggantung.
  const paksa = setTimeout(() => {
    if (serverProc && serverProc.exitCode === null) serverProc.kill('SIGKILL');
  }, 2500);
  paksa.unref();
}

function fatal(judul, detail) {
  dialog.showErrorBox(judul, detail);
  app.isQuitting = true;
  stopServer();
  app.exit(1);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1024,
    minHeight: 640,
    backgroundColor: '#0f1419',   // sama dengan --bg, supaya tidak berkedip putih
    show: false,
    title: 'Padelin',
    // Dibutuhkan saat jalan dari kode sumber: tanpa ini taskbar memakai ikon
    // bawaan Electron. Versi terpasang mengambilnya dari exe.
    icon: path.join(__dirname, 'build', 'icon.ico'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  // Tampilkan setelah siap: memperlihatkan jendela kosong lebih dulu membuat
  // aplikasi terasa lebih lambat daripada sebenarnya.
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });

  aturJendelaBaru(mainWindow);
  mainWindow.loadURL(`http://127.0.0.1:${serverPort}/`);
  if (DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

/** Laporan dibuka dengan target=_blank supaya bisa dicetak Ctrl+P.
 *
 * Tanpa penanganan ini Electron memblokirnya dan tombol "Buka laporan" diam
 * saja. Jendela laporan dibuat sungguhan - bukan tab - supaya dialog cetak
 * bawaan Chromium tetap bekerja seperti di browser.
 */
function aturJendelaBaru(win) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    const lokal = url.startsWith(`http://127.0.0.1:${serverPort}`)
      || url === 'about:blank';
    if (!lokal) {
      shell.openExternal(url);          // tautan luar dibuka di browser, bukan di sini
      return { action: 'deny' };
    }
    return {
      action: 'allow',
      overrideBrowserWindowOptions: {
        width: 1000,
        height: 900,
        backgroundColor: '#ffffff',     // laporan bertema terang
        title: 'Laporan',
        webPreferences: { contextIsolation: true, nodeIntegration: false },
      },
    };
  });

  // Jangan biarkan jendela utama berpindah keluar dari aplikasinya sendiri.
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith(`http://127.0.0.1:${serverPort}`)) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });
}

function buatMenu() {
  const template = [
    {
      label: 'Berkas',
      submenu: [
        {
          label: 'Cetak / Simpan PDF',
          accelerator: 'CmdOrCtrl+P',
          click: (_i, win) => { if (win) win.webContents.print(); },
        },
        { type: 'separator' },
        { label: 'Tutup jendela', accelerator: 'CmdOrCtrl+W', role: 'close' },
        { label: 'Keluar', accelerator: 'CmdOrCtrl+Q', role: 'quit' },
      ],
    },
    {
      label: 'Ubah',
      submenu: [
        { label: 'Urungkan', role: 'undo' },
        { label: 'Ulangi', role: 'redo' },
        { type: 'separator' },
        { label: 'Potong', role: 'cut' },
        { label: 'Salin', role: 'copy' },
        { label: 'Tempel', role: 'paste' },
        { label: 'Pilih semua', role: 'selectAll' },
      ],
    },
    {
      label: 'Tampilan',
      submenu: [
        { label: 'Muat ulang', accelerator: 'CmdOrCtrl+R', role: 'reload' },
        { label: 'Perbesar', role: 'zoomIn' },
        { label: 'Perkecil', role: 'zoomOut' },
        { label: 'Ukuran normal', role: 'resetZoom' },
        { type: 'separator' },
        { label: 'Layar penuh', role: 'togglefullscreen' },
        { label: 'Alat pengembang', accelerator: 'F12', role: 'toggleDevTools' },
      ],
    },
    {
      label: 'Bantuan',
      submenu: [
        {
          label: 'Periksa pembaruan',
          click: () => periksaPembaruan({ diam: false }),
        },
        {
          label: 'Buka folder data',
          click: () => shell.openPath(path.dirname(databasePath())),
        },
        { type: 'separator' },
        {
          label: `Versi ${app.getVersion()}`,
          enabled: false,
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// Satu instance saja. Membuka aplikasi dua kali berarti dua server Python dan
// dua jendela yang menulis ke database yang sama.
if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    buatMenu();
    try {
      await startServer();
    } catch (err) {
      fatal('Padelin gagal dijalankan', err.message);
      return;
    }
    createWindow();

    // Periksa pembaruan setelah jendela tampil, dan diam-diam: host membuka
    // aplikasi untuk menyusun jadwal, bukan untuk mengurus pembaruan. Kalau ada
    // yang baru, ia diunduh di latar dan dipasang saat aplikasi ditutup.
    setTimeout(() => periksaPembaruan({ diam: true }), 4000);

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    app.isQuitting = true;
    stopServer();
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => { app.isQuitting = true; });
  app.on('will-quit', stopServer);
  // Ctrl+C di terminal saat mode dev.
  process.on('SIGINT', () => { app.isQuitting = true; stopServer(); app.exit(0); });
}
