// Pembaruan tanpa pasang ulang.
//
// Aplikasi terpasang memeriksa rilis terbaru sendiri, mengunduhnya di latar,
// dan memasangnya saat aplikasi ditutup. Terbukti ujung ke ujung: 1.0.0 ->
// 1.0.1 -> 1.0.2, semuanya lewat jalur ini.
//
// Hanya bagian yang berubah yang diunduh. Terukur pada 1.0.5 -> 1.0.6:
// 1,44 MB terkirim, bukan 96,33 MB.
//
// Syaratnya installer versi sebelumnya masih ada di cache updater; cache itu
// terisi sendiri saat pembaruan sebelumnya dipasang lewat updater. Kalau tidak
// ada, selisihnya tetap dihitung tapi tidak bisa dipakai, dan updater mundur ke
// unduhan utuh sambil mencatat alasannya. Karena itu pembaruan pertama sesudah
// pemasangan manual selalu utuh - itu perilaku yang benar, bukan kegagalan.
//
// Yang perlu disiapkan sekali di sisi Anda: satu tempat menaruh hasil build
// (GitHub Releases, atau folder mana pun yang bisa diakses lewat HTTP). Tanpa
// itu tidak ada yang bisa diperiksa - lihat "publish" di package.json.
//
// Catatan jujur soal batasnya: mekanisme ini memperbarui SELURUH aplikasi,
// termasuk runtime Electron kalau memang berubah. Ia tidak menambal berkas satu
// per satu di folder terpasang - itu sengaja, karena menulis kode yang diunduh
// langsung ke folder aplikasi berarti membangun jalur eksekusi kode tanpa
// tanda tangan, dan itu risiko yang tidak sebanding dengan hematnya.

const { app, dialog, Notification } = require('electron');
const fs = require('fs');
const path = require('path');

let updater = null;      // dimuat malas: modulnya opsional saat pengembangan
let sedangPeriksa = false;
// Host sudah menekan "Pasang & mulai ulang". Dibedakan dari `diam` karena
// keduanya menjawab pertanyaan yang berbeda: `diam` soal bagaimana PEMERIKSAAN
// dimulai, ini soal apakah host MEMINTA pemasangan. Dulu keduanya dicampur,
// jadi kegagalan memasang pada pemeriksaan otomatis dibungkam oleh `if (!diam)`
// - padahal host baru saja menekan tombolnya sendiri.
let memintaPasang = false;

// Berapa lama menunggu aplikasi benar-benar keluar setelah quitAndInstall.
// Kalau kita masih hidup setelah ini, pemasangannya tidak jalan.
const BATAS_KELUAR_MS = 6000;

/** Catatan pemeriksaan pembaruan ke berkas.
 *
 * Pemeriksaan saat start sengaja senyap - host membuka aplikasi untuk menyusun
 * jadwal, bukan mengurus pembaruan. Tapi senyap di layar tidak boleh berarti
 * senyap sepenuhnya: tanpa jejak, "kenapa pembaruan tidak muncul" mustahil
 * dijawab. Itu bukan kekhawatiran teoretis - dua kegagalan nyata sudah terjadi
 * dan keduanya tidak memunculkan apa pun: rilis yang terbit sebagai draft
 * (tidak terlihat tanpa token), dan satu percobaan yang tidak mengirim satu
 * permintaan pun ke server tanpa alasan yang bisa dilacak.
 *
 * Berkasnya DITAMBAHI, tidak ditimpa. Dulu penulisan pertama tiap sesi memakai
 * flag 'w', jadi tiap kali aplikasi dibuka catatan sesi sebelumnya lenyap - dan
 * justru sesi sebelumnya yang memuat kegagalannya. Itu bukan kekhawatiran
 * teoretis: satu pembaruan gagal memasang, host membuka lagi aplikasinya untuk
 * melihat apa yang terjadi, dan tindakan membuka itu sendiri yang menghapus
 * buktinya. Penyebabnya akhirnya harus dicari di Event Log Windows, bukan di
 * berkas yang memang dibuat untuk itu.
 *
 * Supaya tetap tidak tumbuh tanpa batas, berkasnya dipangkas dari DEPAN begitu
 * melewati batas - yang dibuang riwayat terjauh, bukan kejadian terbaru.
 */
const LOG = path.join(app.getPath('userData'), 'updater.log');
const LOG_MAKS = 256 * 1024;
let logDimulai = false;

/** Buang bagian tertua kalau berkasnya sudah kebesaran. */
function pangkasLog() {
  try {
    if (fs.statSync(LOG).size <= LOG_MAKS) return;
    const buntut = fs.readFileSync(LOG, 'utf8').slice(-Math.floor(LOG_MAKS / 2));
    // Dipotong di batas baris supaya berkasnya tidak dimulai dari tengah baris.
    const awal = buntut.indexOf('\n');
    fs.writeFileSync(
      LOG,
      `--- bagian tertua dipangkas ---\n`
        + `${awal >= 0 ? buntut.slice(awal + 1) : buntut}`,
      'utf8');
  } catch {
    // Gagal memangkas tidak boleh menghalangi pencatatan.
  }
}

function catat(taraf, ...pesan) {
  const teks = pesan
    .map((p) => (typeof p === 'string' ? p : JSON.stringify(p)))
    .join(' ');
  const baris = `[${new Date().toISOString()}] ${taraf} ${teks}\n`;
  try {
    if (!logDimulai) {
      pangkasLog();
      // Versi ikut dicatat: "gagal memasang" hanya bisa dibaca kalau terlihat
      // versi mana yang sedang berjalan waktu itu.
      fs.appendFileSync(LOG, `\n--- sesi baru: v${app.getVersion()} ---\n`);
      logDimulai = true;
    }
    fs.appendFileSync(LOG, baris);
  } catch {
    // Gagal mencatat tidak boleh menjatuhkan aplikasi.
  }
}

// Bentuk yang diharapkan electron-updater; debug dibuang supaya berkasnya
// tetap terbaca manusia.
const logger = {
  info: (...a) => catat('INFO ', ...a),
  warn: (...a) => catat('WARN ', ...a),
  error: (...a) => catat('ERROR', ...a),
  debug: () => {},
};

/** Lokasi berkas catatan, untuk ditunjukkan ke host saat pemeriksaan gagal. */
function berkasLog() {
  return LOG;
}

/** Pemasangan yang diminta host tapi tidak terjadi.
 *
 * Dipanggil dari dua arah - pengecualian saat memanggil quitAndInstall, dan
 * penjaga waktu yang berbunyi kalau aplikasi ternyata tidak keluar. Keduanya
 * bermuara ke satu tempat supaya pesannya sama dan tercatat sekali.
 *
 * Penyebab paling sering di Windows 11 disebut lebih dulu: Smart App Control
 * memblokir berkas tak bertandatangan tanpa dialog apa pun, dan berbeda dari
 * SmartScreen ia TIDAK punya "Run anyway". Host tidak akan pernah menduga itu
 * sendiri, jadi arahnya harus disebut - bukan cuma "gagal".
 */
function gagalMemasang(err) {
  if (!memintaPasang) return;   // sudah dilaporkan, atau bukan permintaan host
  memintaPasang = false;
  app.isQuitting = false;       // batal keluar; host masih memakai aplikasinya
  catat('ERROR', 'pemasangan gagal:', String(err && err.stack ? err.stack : err));
  dialog.showMessageBox({
    type: 'error',
    title: 'Pembaruan gagal dipasang',
    message: 'Installer-nya sudah terunduh, tapi Windows tidak mengizinkannya '
      + 'dijalankan.',
    detail: 'Penyebab paling sering: Smart App Control memblokir installer yang '
      + 'belum bertandatangan. Berbeda dari SmartScreen, ia tidak menawarkan '
      + '"Run anyway".\n\nPeriksa di Windows Security -> App & browser control '
      + '-> Smart App Control. Padelin yang sedang berjalan tetap aman dipakai; '
      + `hanya pembaruannya yang tertahan.\n\nRincian tercatat di:\n${berkasLog()}`,
  });
}

function muatUpdater() {
  if (updater !== null) return updater;
  try {
    // eslint-disable-next-line global-require
    updater = require('electron-updater').autoUpdater;
  } catch (err) {
    updater = false;     // false = tidak tersedia, beda dari null = belum dicoba
  }
  return updater;
}

/**
 * @param {object} opts
 * @param {boolean} opts.diam  true untuk pemeriksaan otomatis saat start:
 *   tidak ada dialog kalau sudah versi terbaru atau kalau jaringan mati.
 *   Pemeriksaan dari menu memakai false supaya host tahu hasilnya.
 */
function periksaPembaruan({ diam = true } = {}) {
  const au = muatUpdater();

  if (!app.isPackaged) {
    if (!diam) {
      dialog.showMessageBox({
        type: 'info',
        title: 'Pembaruan',
        message: 'Pemeriksaan pembaruan hanya berlaku pada versi terpasang.',
        detail: 'Sedang berjalan dari kode sumber, jadi pembaruannya lewat git.',
      });
    }
    return;
  }
  if (!au) {
    if (!diam) {
      dialog.showMessageBox({
        type: 'warning',
        title: 'Pembaruan',
        message: 'Modul pembaruan tidak tersedia di paket ini.',
        detail: 'Pasang dependensi electron-updater lalu build ulang.',
      });
    }
    return;
  }
  if (sedangPeriksa) return;
  sedangPeriksa = true;

  au.logger = logger;
  catat('INFO ', `versi terpasang ${app.getVersion()}, memeriksa pembaruan `
    + `(${diam ? 'otomatis' : 'diminta host'})`);
  au.autoDownload = true;
  // Jangan pasang diam-diam di tengah host menyiapkan acara; pasang saat keluar.
  au.autoInstallOnAppQuit = true;
  au.removeAllListeners();

  au.on('update-available', (info) => {
    if (Notification.isSupported()) {
      new Notification({
        title: 'Pembaruan tersedia',
        body: `Versi ${info.version} sedang diunduh di latar belakang.`,
      }).show();
    }
  });

  au.on('update-not-available', () => {
    sedangPeriksa = false;
    if (!diam) {
      dialog.showMessageBox({
        type: 'info',
        title: 'Pembaruan',
        message: `Sudah versi terbaru (${app.getVersion()}).`,
      });
    }
  });

  au.on('error', (err) => {
    sedangPeriksa = false;
    // Error yang datang SESUDAH host menekan "Pasang & mulai ulang" bukan lagi
    // soal pemeriksaan - itu pemasangan yang gagal, dan harus dilaporkan berapa
    // pun senyapnya pemeriksaan yang mengawalinya. Dulu `if (!diam)` di bawah
    // menelannya, jadi jalur yang paling penting justru yang paling bisu.
    if (memintaPasang) {
      gagalMemasang(err);
      return;
    }
    catat('ERROR', 'pemeriksaan gagal:', String(err && err.stack ? err.stack : err));
    // Saat start, kegagalan jaringan bukan urusan host - jangan ganggu layarnya.
    // Tapi tetap tercatat di berkas, supaya bisa ditelusuri belakangan.
    if (!diam) {
      dialog.showMessageBox({
        type: 'error',
        title: 'Pembaruan gagal',
        message: 'Tidak bisa memeriksa pembaruan.',
        detail: `${String(err && err.message ? err.message : err)}\n\n`
          + `Rincian tercatat di:\n${berkasLog()}`,
      });
    }
  });

  au.on('update-downloaded', async (info) => {
    sedangPeriksa = false;
    const { response } = await dialog.showMessageBox({
      type: 'question',
      buttons: ['Pasang & mulai ulang', 'Nanti saat ditutup'],
      defaultId: 1,
      cancelId: 1,
      title: 'Pembaruan siap',
      message: `Versi ${info.version} sudah diunduh.`,
      detail: 'Memasang sekarang menutup jendela yang terbuka, lalu Padelin '
        + 'terbuka lagi sendiri.\n\n'
        + 'Kalau sedang menyiapkan acara, pilih "Nanti saat ditutup" - '
        + 'pembaruannya dipasang begitu Padelin ditutup, dan tidak dibuka lagi '
        + 'setelahnya.',
    });
    if (response === 0) {
      memintaPasang = true;
      app.isQuitting = true;
      catat('INFO ', `memasang ${info.version} (senyap, jalankan lagi setelah pasang)`);
      // (senyap, jalankan lagi setelah pasang).
      //
      // Bawaannya quitAndInstall() TIDAK senyap, jadi wizard installer muncul
      // dan host harus mengklik Next beberapa kali - padahal ia sudah menekan
      // "Pasang & mulai ulang", yang artinya "kerjakan, jangan tanya lagi".
      //
      // Argumen kedua dipasang eksplisit meski bawaannya sudah menjalankan
      // ulang: dalam mode senyap, electron-updater memakai nilai yang dikirim
      // apa adanya, bukan autoRunAppAfterInstall. Tanpa itu aplikasi terpasang
      // tapi tidak pernah terbuka lagi, dan tombolnya jadi berbohong.
      try {
        au.quitAndInstall(true, true);
      } catch (err) {
        gagalMemasang(err);
        return;
      }
      // Penjaga waktu, bukan hiasan. quitAndInstall() sukses berarti aplikasi
      // ini keluar - jadi kalau timer ini sampai berbunyi, pemasangannya TIDAK
      // jalan. Itu satu-satunya cara mendeteksi blokir yang tidak melempar apa
      // pun: Windows Smart App Control menolak installer tak bertandatangan di
      // tingkat kernel, prosesnya tidak pernah lahir, dan electron-updater
      // tidak selalu punya error untuk dilaporkan. Sebelum ada penjaga ini,
      // yang dilihat host cuma jendela tertutup lalu versinya tetap sama.
      const jaga = setTimeout(() => gagalMemasang(
        new Error('Installer tidak bisa dijalankan; aplikasi masih berjalan '
          + `${BATAS_KELUAR_MS / 1000} detik setelah diminta memasang.`)), BATAS_KELUAR_MS);
      jaga.unref();
    }
  });

  // Error di sini TIDAK ditelan: dulu ia dibuang diam-diam, dan akibatnya
  // kegagalan yang tidak pernah memunculkan apa pun jadi mustahil didiagnosis.
  au.checkForUpdates().catch((err) => {
    sedangPeriksa = false;
    catat('ERROR', 'checkForUpdates ditolak:',
          String(err && err.stack ? err.stack : err));
  });
}

module.exports = { periksaPembaruan };
