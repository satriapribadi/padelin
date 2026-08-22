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
function pasangPanggung({ userData, quitAndInstall, versi = '1.2.2', jawaban = 0 }) {
  const dialogs = [];
  // Pendengar app.on disimpan supaya tes bisa memicu 'will-quit' - jalur
  // autoInstallOnAppQuit tidak lewat tombol mana pun, jadi tanpa ini ia tidak
  // bisa diuji sama sekali.
  const pendengar = {};
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
    on: (nama, fn) => { (pendengar[nama] = pendengar[nama] || []).push(fn); },
  };

  const electron = {
    app,
    dialog: {
      // Tombol 0 = "Pasang & mulai ulang": inilah yang sedang diuji.
      showMessageBox: (opsi) => {
        dialogs.push(opsi);
        return Promise.resolve({ response: jawaban });
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
  const picu = (nama) => (pendengar[nama] || []).forEach((fn) => fn());
  return { mod, au, app, dialogs, picu };
}

function dirBaru() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'padelin-updater-uji-'));
}

function isiLog(dir) {
  const f = path.join(dir, 'updater.log');
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : '';
}

function penanda(dir) {
  const f = path.join(dir, 'pemasangan-tertunda.json');
  return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : null;
}

/** Jalankan sampai host menjawab dialog "Pembaruan siap". */
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

  // --- 7. Keluar normal tapi tidak ada yang terpasang -------------------
  // Bentuk kegagalan yang sesungguhnya terjadi pada 1.3.1 -> 1.3.2: Smart App
  // Control menolak installer di tingkat kernel, electron-updater menelan
  // penolakannya sebagai INFO, dan aplikasi keluar dengan tenang. Penjaga
  // waktu tidak berbunyi - aplikasinya memang keluar - jadi satu-satunya bukti
  // baru muncul di sesi berikutnya: versinya masih yang lama.
  {
    const dir = dirBaru();

    // Sesi 1: host memilih "Nanti saat ditutup", lalu menutup aplikasinya.
    const s1 = pasangPanggung({ userData: dir, versi: '1.3.1', jawaban: 1,
      quitAndInstall: () => { throw new Error('tidak boleh dipanggil'); } });
    await sampaiMemasang(s1);
    periksa('"Nanti saat ditutup" tidak memasang saat itu juga', () => {
      assert.strictEqual(penanda(dir), null, 'penanda ditulis terlalu dini');
    });
    s1.picu('will-quit');
    periksa('Keluar dengan pembaruan menunggu meninggalkan penanda', () => {
      const t = penanda(dir);
      assert.ok(t, 'tidak ada penanda');
      assert.strictEqual(t.versi, '1.3.0');
      assert.strictEqual(t.dari, '1.3.1');
    });

    // Sesi 2: aplikasi hidup lagi, versinya TIDAK berubah.
    const s2 = pasangPanggung({ userData: dir, versi: '1.3.1',
      quitAndInstall: () => {} });
    s2.mod.laporkanPemasanganTertunda();
    periksa('Sesi berikutnya melaporkan pemasangan yang tidak pernah terjadi', () => {
      const d = s2.dialogs.filter((x) => x.title === 'Pembaruan gagal dipasang');
      assert.strictEqual(d.length, 1,
        `dialog: ${JSON.stringify(s2.dialogs.map((x) => x.title))}`);
      assert.ok(/masih di versi 1\.3\.1/.test(d[0].detail), d[0].detail);
      assert.ok(/Smart App Control/.test(d[0].detail), d[0].detail);
    });
    periksa('Kegagalan senyap itu ikut tercatat di berkas', () => {
      assert.ok(/ERROR.*pemasangan 1\.3\.0 tidak terjadi/.test(isiLog(dir)),
        isiLog(dir));
    });
    periksa('Penandanya dibuang, jadi tidak berbunyi tiap membuka aplikasi', () => {
      assert.strictEqual(penanda(dir), null);
      const s3 = pasangPanggung({ userData: dir, versi: '1.3.1',
        quitAndInstall: () => {} });
      s3.mod.laporkanPemasanganTertunda();
      assert.strictEqual(s3.dialogs.length, 0,
        `dialog: ${JSON.stringify(s3.dialogs.map((x) => x.title))}`);
    });
  }

  // --- 8. Pemasangan yang BERHASIL tidak boleh berbunyi -----------------
  // Sisi sebaliknya dari tes 7, dan yang paling mudah rusak: penanda yang sama
  // ada di kedua kasus, yang membedakan cuma versi yang berjalan.
  {
    const dir = dirBaru();
    const s1 = pasangPanggung({ userData: dir, versi: '1.3.1',
      quitAndInstall: () => {} });
    await sampaiMemasang(s1);
    periksa('Menekan "Pasang & mulai ulang" menulis penanda lebih dulu', () => {
      const t = penanda(dir);
      assert.ok(t && t.versi === '1.3.0' && t.dari === '1.3.1', JSON.stringify(t));
    });

    const s2 = pasangPanggung({ userData: dir, versi: '1.3.0',
      quitAndInstall: () => {} });
    s2.mod.laporkanPemasanganTertunda();
    periksa('Versi yang berubah dibaca sebagai berhasil, bukan gagal', () => {
      assert.strictEqual(s2.dialogs.length, 0,
        `dialog: ${JSON.stringify(s2.dialogs.map((x) => x.title))}`);
      assert.ok(/1\.3\.1 -> 1\.3\.0 terpasang/.test(isiLog(dir)), isiLog(dir));
      assert.strictEqual(penanda(dir), null);
    });
  }

  // --- 9. Tanpa penanda, sesi biasa tetap sunyi -------------------------
  {
    const dir = dirBaru();
    const p = pasangPanggung({ userData: dir, quitAndInstall: () => {} });
    p.mod.laporkanPemasanganTertunda();
    periksa('Buka aplikasi tanpa pembaruan tertunda tidak memunculkan apa pun', () => {
      assert.strictEqual(p.dialogs.length, 0,
        `dialog: ${JSON.stringify(p.dialogs.map((x) => x.title))}`);
    });
  }

  // --- 10. Gagal yang sudah dilaporkan seketika tidak diulang -----------
  // Dua alarm untuk satu kegagalan sama buruknya dengan nol alarm: host tidak
  // bisa lagi membedakan "gagal lagi" dari "gema yang kemarin".
  {
    const dir = dirBaru();
    const s1 = pasangPanggung({
      userData: dir,
      versi: '1.3.1',
      quitAndInstall: () => { throw new Error('diblokir kebijakan'); },
    });
    await sampaiMemasang(s1);
    periksa('Gagal seketika membuang penandanya sendiri', () => {
      const d = s1.dialogs.filter((x) => x.title === 'Pembaruan gagal dipasang');
      assert.strictEqual(d.length, 1);
      assert.strictEqual(penanda(dir), null);
    });
    const s2 = pasangPanggung({ userData: dir, versi: '1.3.1',
      quitAndInstall: () => {} });
    s2.mod.laporkanPemasanganTertunda();
    periksa('Sesi berikutnya tidak mengulang alarm yang sama', () => {
      assert.strictEqual(s2.dialogs.length, 0,
        `dialog: ${JSON.stringify(s2.dialogs.map((x) => x.title))}`);
    });
  }

  console.log(`\n${lulus} lulus, ${gagal.length} gagal`);
  process.exit(gagal.length ? 1 : 0);
}

utama();
