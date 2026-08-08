// Pembaruan tanpa pasang ulang.
//
// electron-builder menerbitkan berkas .blockmap di samping tiap installer.
// electron-updater membandingkan blockmap versi terpasang dengan versi baru,
// lalu MENGUNDUH BLOK YANG BERUBAH SAJA - pembaruan yang hanya menyentuh kode
// Python dan web/ biasanya beberapa ratus KB, bukan puluhan MB installer utuh.
// Pemasangannya berjalan sendiri saat aplikasi ditutup.
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

let updater = null;      // dimuat malas: modulnya opsional saat pengembangan
let sedangPeriksa = false;

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
    // Saat start, kegagalan jaringan bukan urusan host - jangan ganggu.
    if (!diam) {
      dialog.showMessageBox({
        type: 'error',
        title: 'Pembaruan gagal',
        message: 'Tidak bisa memeriksa pembaruan.',
        detail: String(err && err.message ? err.message : err),
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

  au.checkForUpdates().catch(() => { sedangPeriksa = false; });
}

module.exports = { periksaPembaruan };
