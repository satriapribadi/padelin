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
 * Berkasnya ditimpa tiap kali aplikasi dibuka, jadi ia tidak tumbuh tanpa batas
 * dan isinya selalu tentang sesi yang sedang berjalan.
 */
const LOG = path.join(app.getPath('userData'), 'updater.log');
let logDimulai = false;

function catat(taraf, ...pesan) {
  const teks = pesan
    .map((p) => (typeof p === 'string' ? p : JSON.stringify(p)))
    .join(' ');
  const baris = `[${new Date().toISOString()}] ${taraf} ${teks}\n`;
  try {
    fs.appendFileSync(LOG, logDimulai ? baris : `--- sesi baru ---\n${baris}`,
                      { flag: logDimulai ? 'a' : 'w' });
    logDimulai = true;
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
      detail: 'Memasang sekarang akan menutup jendela yang terbuka. '
        + 'Kalau sedang menyiapkan acara, pilih "Nanti saat ditutup" - '
        + 'pembaruannya dipasang otomatis begitu aplikasi ditutup.',
    });
    if (response === 0) {
      app.isQuitting = true;
      au.quitAndInstall();
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
