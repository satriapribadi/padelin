// Uji jalur pelaporan updater dengan MENJALANKANNYA, bukan membacanya.
//
// Yang diuji di sini justru sifat "kalau gagal, harus berbunyi" - dan sifat
// seperti itu paling sering rusak tanpa suara. Dua cacat nyata yang ditangkap
// berkas ini:
//
//   1. updater.log ditimpa tiap sesi, sehingga sesi yang memuat kegagalan
//      terhapus tepat ketika host membuka aplikasi untuk mencari tahu.
//   2. kegagalan memasang dibungkam oleh `if (!diam)`, padahal host baru saja
//      menekan tombol "Pasang & mulai ulang" sendiri.
//
// electron dan electron-updater disuntik lewat require.cache, jadi tesnya jalan
// di Node biasa tanpa membuka jendela apa pun.
//
// Jalankan: node tests/test_updater.js

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');

const AKAR = path.join(__dirname, '..');
const UPDATER = path.join(AKAR, 'electron', 'updater.js');

let lulus = 0;
const gagal = [];

function periksa(nama, fn) {
  try {
    fn();
    console.log(`  [OK ] ${nama}`);
    lulus += 1;
  } catch (err) {
    console.log(`  [GAGAL] ${nama}\n         ${err.message}`);
    gagal.push(nama);
  }
}

/** Pasang electron & electron-updater tiruan, lalu muat ulang updater.js. */
function pasangPanggung({ userData, quitAndInstall, versi = '1.2.2' }) {
  const dialogs = [];
  const au = new EventEmitter();
  au.autoDownload = false;
  au.autoInstallOnAppQuit = false;
  au.checkForUpdates = () => Promise.resolve(null);
  au.quitAndInstall = quitAndInstall;

  const app = {
    isPackaged: true,
    isQuitting: false,
    getVersion: () => versi,
    getPath: () => userData,
  };

  const electron = {
    app,
    dialog: {
      // Tombol 0 = "Pasang & mulai ulang": inilah yang sedang diuji.
      showMessageBox: (opsi) => {
        dialogs.push(opsi);
        return Promise.resolve({ response: 0 });
      },
      showErrorBox: (title, content) => dialogs.push({ title, detail: content }),
    },
    Notification: class {
      static isSupported() { return false; }

      show() {}
    },
  };

  const suntik = (nama, exports) => {
    let id;
    try {
      id = require.resolve(nama);
    } catch {
      id = nama;                       // paket tidak terpasang: cukup id palsu
    }
    require.cache[id] = { id, filename: id, loaded: true, exports, children: [], paths: [] };
  };
  suntik('electron', electron);
  suntik('electron-updater', { autoUpdater: au });

  delete require.cache[require.resolve(UPDATER)];
  // eslint-disable-next-line global-require, import/no-dynamic-require
  const mod = require(UPDATER);
  return { mod, au, app, dialogs };
}

function dirBaru() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'padelin-updater-uji-'));
}

function isiLog(dir) {
  const f = path.join(dir, 'updater.log');
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : '';
}

/** Jalankan sampai host menekan "Pasang & mulai ulang". */
async function sampaiMemasang(panggung) {
  panggung.mod.periksaPembaruan({ diam: true });
  await Promise.resolve();
  panggung.au.emit('update-downloaded', { version: '1.3.0' });
  // Dua putaran mikrotask: satu untuk await dialog, satu untuk lanjutannya.
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((r) => setImmediate(r));
}

async function utama() {
  console.log('Uji pelaporan updater\n');

  // --- 1. Log menumpuk antar sesi, tidak menimpa ------------------------
  {
    const dir = dirBaru();
    for (let i = 0; i < 3; i++) {
      const p = pasangPanggung({ dir, userData: dir, quitAndInstall: () => {} });
      p.mod.periksaPembaruan({ diam: true });
      await Promise.resolve();
    }
    const isi = isiLog(dir);
    const sesi = (isi.match(/--- sesi baru/g) || []).length;
    periksa('Tiga sesi menyisakan tiga catatan, bukan satu', () => {
      assert.strictEqual(sesi, 3, `ketemu ${sesi} sesi di:\n${isi}`);
    });
    periksa('Catatan memuat versi yang sedang berjalan', () => {
      assert.ok(isi.includes('v1.2.2'), isi);
    });
  }

  // --- 2. quitAndInstall melempar -> host diberi tahu -------------------
  {
    const dir = dirBaru();
    const p = pasangPanggung({
      userData: dir,
      quitAndInstall: () => { throw new Error('diblokir kebijakan'); },
    });
    await sampaiMemasang(p);
    const err = p.dialogs.filter((d) => d.title === 'Pembaruan gagal dipasang');
    periksa('Pengecualian saat memasang memunculkan dialog', () => {
      assert.strictEqual(err.length, 1, `dialog: ${JSON.stringify(p.dialogs.map((d) => d.title))}`);
    });
    periksa('Dialognya menyebut Smart App Control sebagai arah periksa', () => {
      assert.ok(/Smart App Control/.test(err[0].detail), err[0] && err[0].detail);
    });
    periksa('Kegagalannya ikut tercatat di berkas', () => {
      assert.ok(/ERROR.*pemasangan gagal/.test(isiLog(dir)), isiLog(dir));
    });
    periksa('Aplikasi tidak ditinggal dalam keadaan sedang-keluar', () => {
      assert.strictEqual(p.app.isQuitting, false);
    });
  }

  // --- 3. quitAndInstall diam saja (blokir tingkat kernel) --------------
  // Inilah bentuk kegagalan yang sesungguhnya terjadi: prosesnya tidak pernah
  // lahir, tidak ada pengecualian, tidak ada event error. Sebelum ada penjaga
  // waktu, yang dilihat host cuma jendela tertutup lalu versinya tetap sama.
  {
    const dir = dirBaru();
    const p = pasangPanggung({ userData: dir, quitAndInstall: () => {} });
    await sampaiMemasang(p);
    periksa('Sebelum tenggat: belum ada dialog gagal (tidak berteriak dini)', () => {
      const n = p.dialogs.filter((d) => d.title === 'Pembaruan gagal dipasang').length;
      assert.strictEqual(n, 0);
    });
    await new Promise((r) => setTimeout(r, 6500));
    periksa('Sesudah tenggat: penjaga waktu melaporkan pemasangan yang bisu', () => {
      const n = p.dialogs.filter((d) => d.title === 'Pembaruan gagal dipasang').length;
      assert.strictEqual(n, 1, `dialog: ${JSON.stringify(p.dialogs.map((d) => d.title))}`);
    });
    periksa('Alasannya tercatat, bukan cuma muncul di layar', () => {
      assert.ok(/masih berjalan/.test(isiLog(dir)), isiLog(dir));
    });
  }

  // --- 4. Event error sesudah diminta memasang tidak boleh dibungkam ----
  {
    const dir = dirBaru();
    const p = pasangPanggung({ userData: dir, quitAndInstall: () => {} });
    await sampaiMemasang(p);
    p.au.emit('error', new Error('spawn EPERM'));
    await Promise.resolve();
    periksa('Error sesudah "Pasang" dilaporkan walau pemeriksaannya senyap', () => {
      const d = p.dialogs.filter((x) => x.title === 'Pembaruan gagal dipasang');
      assert.strictEqual(d.length, 1, `dialog: ${JSON.stringify(p.dialogs.map((x) => x.title))}`);
    });
  }

  // --- 5. Error pemeriksaan biasa tetap senyap saat diam ---------------
  // Perbaikan di atas tidak boleh berubah jadi kebocoran ke arah sebaliknya:
  // jaringan mati saat start bukan urusan host.
  {
    const dir = dirBaru();
    const p = pasangPanggung({ userData: dir, quitAndInstall: () => {} });
    p.mod.periksaPembaruan({ diam: true });
    await Promise.resolve();
    p.au.emit('error', new Error('getaddrinfo ENOTFOUND'));
    await Promise.resolve();
    periksa('Jaringan mati saat start tidak mengganggu layar host', () => {
      assert.strictEqual(p.dialogs.length, 0,
        `dialog: ${JSON.stringify(p.dialogs.map((x) => x.title))}`);
    });
    periksa('Tapi tetap tercatat di berkas', () => {
      assert.ok(/ENOTFOUND/.test(isiLog(dir)), isiLog(dir));
    });
  }

  // --- 6. Log dipangkas dari depan, bukan dari belakang -----------------
  {
    const dir = dirBaru();
    const f = path.join(dir, 'updater.log');
    fs.writeFileSync(f, `PALING-TUA\n${'x'.repeat(300 * 1024)}\nPALING-BARU\n`);
    const p = pasangPanggung({ userData: dir, quitAndInstall: () => {} });
    p.mod.periksaPembaruan({ diam: true });
    await Promise.resolve();
    const isi = isiLog(dir);
    periksa('Pemangkasan membuang riwayat terjauh', () => {
      assert.ok(!isi.includes('PALING-TUA'), 'baris tertua masih ada');
      assert.ok(isi.includes('dipangkas'), 'tidak ada penanda pemangkasan');
    });
    periksa('Pemangkasan mempertahankan yang terbaru', () => {
      assert.ok(isi.includes('PALING-BARU'), 'baris terbaru ikut terbuang');
      assert.ok(isi.includes('v1.2.2'), 'sesi baru tidak tercatat');
    });
    periksa('Ukurannya kembali di bawah batas', () => {
      assert.ok(fs.statSync(f).size < 256 * 1024, `${fs.statSync(f).size} byte`);
    });
  }

  console.log(`\n${lulus} lulus, ${gagal.length} gagal`);
  process.exit(gagal.length ? 1 : 0);
}

utama();
