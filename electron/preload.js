// Jembatan sempit ke proses utama, khusus urusan cetak.
//
// Alasannya ada di sini dan bukan di halaman: window.print() di Electron
// bermuara ke dialog cetak bawaan Windows, dan dialog itu menggambar panel
// pratinjaunya dari aliran halaman yang harus DISEDIAKAN aplikasi pemanggil.
// UI pratinjau milik Chrome (chrome://print) ada di lapisan //chrome yang tidak
// diikutkan Electron, jadi panel itu selalu berisi "This app doesn't support
// print preview" - dan tidak ada opsi JS yang bisa mengubahnya.
//
// Jadi pratinjaunya dibuat sendiri: printToPDF() di proses utama menghasilkan
// halaman yang persis akan tercetak, lalu PDF itu ditampilkan penampil PDF
// bawaan Chromium di jendela Padelin sendiri (lihat pratinjau.html).
//
// Yang dibuka hanya fungsi tanpa argumen. Halaman tidak boleh bisa menentukan
// nama berkas atau tujuan simpan; itu urusan dialog milik OS.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('padelin', {
  // Dipakai laporan: rakit PDF lalu buka jendela pratinjau.
  pratinjau: () => ipcRenderer.invoke('padelin:pratinjau'),
  // Dipakai jendela pratinjau: simpan PDF yang SEDANG dilihat, dan cetak.
  simpan: () => ipcRenderer.invoke('padelin:simpan'),
  cetak: () => ipcRenderer.invoke('padelin:cetak'),
  // Judul dan jumlah halaman untuk header pratinjau.
  info: () => ipcRenderer.invoke('padelin:info'),
});
