// Bangun electron/build/icon.ico dari electron/build/icon.html.
//
// Tanpa dependensi gambar apa pun: Chromium yang sudah ada (Edge/Chrome, atau
// Electron sendiri) merender tiap ukuran ke PNG, lalu PNG-nya dikemas jadi ICO.
// ICO sejak Windows Vista boleh berisi PNG apa adanya, jadi pengemasannya cuma
// menyusun header - tidak perlu encoder BMP.
//
//   node electron/make-icon.js

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

// 256 untuk tampilan besar, 16 untuk pojok jendela. Ukuran di antaranya dipakai
// Windows di taskbar, Alt+Tab, dan daftar berkas; tanpa itu Windows menskalakan
// sendiri dan hasilnya buram di ukuran kecil.
const SIZES = [256, 128, 64, 48, 32, 16];
const SRC = path.join(__dirname, 'build', 'icon.html');
const OUT = path.join(__dirname, 'build', 'icon.ico');

const BROWSERS = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];

function chromium() {
  const found = BROWSERS.find((b) => fs.existsSync(b));
  if (!found) throw new Error('Butuh Edge atau Chrome untuk merender ikon.');
  return found;
}

function renderPng(exe, size, tmp) {
  const out = path.join(tmp, `icon-${size}.png`);
  // Latar transparan dipertahankan supaya sudut membulatnya tidak berubah jadi
  // kotak putih di taskbar.
  execFileSync(exe, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    '--default-background-color=00000000',
    `--force-device-scale-factor=${size / 256}`,
    '--virtual-time-budget=2000',
    `--screenshot=${out}`, `--window-size=256,256`,
    'file:///' + SRC.replace(/\\/g, '/'),
  ], { stdio: 'ignore' });
  if (!fs.existsSync(out)) throw new Error(`Gagal merender ukuran ${size}`);
  return fs.readFileSync(out);
}

function packIco(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);              // reserved
  header.writeUInt16LE(1, 2);              // 1 = ikon
  header.writeUInt16LE(images.length, 4);

  const entries = [];
  let offset = 6 + images.length * 16;
  for (const { size, data } of images) {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0);   // 0 berarti 256
    e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt8(0, 2);                        // jumlah warna (0 = truecolor)
    e.writeUInt8(0, 3);                        // reserved
    e.writeUInt16LE(1, 4);                     // color planes
    e.writeUInt16LE(32, 6);                    // bit per piksel
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    entries.push(e);
    offset += data.length;
  }
  return Buffer.concat([header, ...entries, ...images.map((i) => i.data)]);
}

const exe = chromium();
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'padelin-icon-'));
try {
  const images = SIZES.map((size) => {
    const data = renderPng(exe, size, tmp);
    console.log(`  ${size}x${size}  ${data.length} byte`);
    return { size, data };
  });
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, packIco(images));
  console.log('icon.ico ditulis:', OUT,
    `(${Math.round(fs.statSync(OUT).size / 1024)} KB, ${images.length} ukuran)`);
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
