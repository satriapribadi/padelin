// Uji pembaruan konten dengan MENJALANKANNYA: server HTTP sungguhan, zip
// sungguhan, bongkaran sungguhan ke folder sementara.
//
// Yang diuji di sini adalah jalur yang menggantikan installer, jadi yang paling
// penting justru penolakannya - paket yang sha512-nya tidak cocok, manifes yang
// namanya aneh, isi yang tidak lengkap, dan konten yang lebih tua dari
// aplikasinya. Semua itu berakhir sama dari luar (aplikasi tetap memakai
// bawaan paket), sehingga tanpa tes tidak ada yang bisa membedakan "ditolak
// dengan benar" dari "tidak pernah dicoba".
//
// electron disuntik lewat require.cache, jadi tesnya jalan di Node biasa tanpa
// membuka jendela apa pun.
//
// Jalankan: node tests/test_konten.js

const assert = require('assert');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { execFileSync, spawn, spawnSync } = require('child_process');

const AKAR = path.join(__dirname, '..');
const KONTEN = path.join(AKAR, 'electron', 'konten.js');

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

function dirBaru() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'padelin-konten-uji-'));
}

/** Pasang electron tiruan, lalu muat ulang konten.js. */
function pasangPanggung({ userData, versi = '1.3.3' }) {
  const dialogs = [];
  const app = {
    getVersion: () => versi,
    getPath: () => userData,
  };
  const electron = {
    app,
    dialog: {
      showMessageBox: (opsi) => { dialogs.push(opsi); return Promise.resolve({ response: 0 }); },
      showErrorBox: (title, content) => dialogs.push({ title, detail: content }),
    },
    Notification: class {
      static isSupported() { return false; }

      show() {}
    },
  };
  let id;
  try { id = require.resolve('electron'); } catch { id = 'electron'; }
  require.cache[id] = { id, filename: id, loaded: true, exports: electron, children: [], paths: [] };

  delete require.cache[require.resolve(KONTEN)];
  // eslint-disable-next-line global-require, import/no-dynamic-require
  const mod = require(KONTEN);
  return { mod, app, dialogs };
}

/** Zip konten sungguhan. `isi` = { 'jalur/relatif': 'teks' }. */
function buatZip(versi, isi) {
  const panggung = fs.mkdtempSync(path.join(os.tmpdir(), 'padelin-zip-'));
  Object.entries(isi).forEach(([rel, teks]) => {
    const tujuan = path.join(panggung, rel);
    fs.mkdirSync(path.dirname(tujuan), { recursive: true });
    fs.writeFileSync(tujuan, teks, 'utf8');
  });
  const zip = path.join(panggung, `konten-${versi}.zip`);
  execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
    `Compress-Archive -Path '${panggung}\\*' -DestinationPath '${zip}' -Force`],
  { stdio: 'pipe' });
  const buf = fs.readFileSync(zip);
  fs.rmSync(panggung, { recursive: true, force: true });
  return buf;
}

const ISI_SAH = {
  'run.py': '# server\n',
  'web/index.html': '<title>Padelin</title>\n',
  'padel_scheduler/__init__.py': '# mesin\n',
};

/** Server rilis tiruan: melayani /latest/download/konten.json dan
 *  /download/vX.Y.Z/<berkas>, persis seperti GitHub Releases. */
function layanRilis({ manifes, zip, namaZip }) {
  const srv = http.createServer((req, res) => {
    if (req.url === '/latest/download/konten.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(manifes));
      return;
    }
    if (namaZip && req.url === `/download/v${manifes.versi}/${namaZip}`) {
      res.writeHead(200, { 'Content-Type': 'application/zip' });
      res.end(zip);
      return;
    }
    res.writeHead(404);
    res.end('tidak ada');
  });
  return new Promise((resolve) => {
    srv.listen(0, '127.0.0.1', () => resolve({
      dasar: `http://127.0.0.1:${srv.address().port}`,
      tutup: () => new Promise((r) => srv.close(r)),
    }));
  });
}

/** Jalankan skrip Node dan tunggu selesai TANPA memblokir event loop.
 *
 * spawnSync tidak bisa dipakai di sini: server GitHub tiruannya hidup di proses
 * ini juga, dan proses yang sedang diblokir tidak pernah menerima koneksi -
 * yang terlihat dari child cuma "fetch failed", jauh dari sebabnya.
 */
function jalankanNode(skrip, env) {
  return new Promise((resolve) => {
    const anak = spawn(process.execPath, [skrip], { cwd: AKAR, env });
    let keluaran = '';
    anak.stdout.on('data', (b) => { keluaran += b; });
    anak.stderr.on('data', (b) => { keluaran += b; });
    anak.on('close', (kode) => resolve({ kode, keluaran }));
  });
}

function manifesUntuk(versi, zip, tambahan = {}) {
  return {
    versi,
    berkas: `konten-${versi}.zip`,
    sha512: crypto.createHash('sha512').update(zip).digest('base64'),
    ukuran: zip.length,
    app_minimal: '1.3.3',
    ...tambahan,
  };
}

async function utama() {
  console.log('Uji pembaruan konten\n');

  // --- 1. Urutan versi ---------------------------------------------------
  // Perbandingan teks biasa menjawab "1.10.0" < "1.9.0", dan salahnya baru
  // terasa di rilis kesepuluh - jauh dari tempat ia ditulis.
  {
    const dir = dirBaru();
    const { mod } = pasangPanggung({ userData: dir });
    periksa('Versi dibandingkan sebagai angka, bukan teks', () => {
      assert.strictEqual(mod.bandingVersi('1.10.0', '1.9.0'), 1);
      assert.strictEqual(mod.bandingVersi('1.3.2', '1.3.10'), -1);
      assert.strictEqual(mod.bandingVersi('1.3.3', '1.3.3'), 0);
    });
  }

  // --- 2. Repo rilis tidak boleh menyimpang dari package.json ------------
  {
    const dir = dirBaru();
    const { mod } = pasangPanggung({ userData: dir });
    periksa('REPO di konten.js sama dengan build.publish di package.json', () => {
      const pkg = JSON.parse(fs.readFileSync(path.join(AKAR, 'package.json'), 'utf8'));
      const gh = [].concat(pkg.build.publish).find((p) => p.provider === 'github');
      assert.strictEqual(mod.REPO, `${gh.owner}/${gh.repo}`);
    });
  }

  // --- 3. Jalur normal: unduh, verifikasi, bongkar, aktif ----------------
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.4', ISI_SAH);
    const srv = await layanRilis({
      manifes: manifesUntuk('1.3.4', zip), zip, namaZip: 'konten-1.3.4.zip',
    });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    periksa('Sebelum apa pun, aplikasi memakai bawaan paket', () => {
      assert.strictEqual(p.mod.akarKonten(), null);
      assert.strictEqual(p.mod.versiKonten(), null);
    });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Paket yang sah dipasang dan jadi akar berkas', () => {
      assert.strictEqual(hasil, '1.3.4');
      assert.strictEqual(p.mod.versiKonten(), '1.3.4');
      const akar = p.mod.akarKonten();
      assert.ok(akar && akar.endsWith(path.join('konten', '1.3.4')), String(akar));
      assert.ok(fs.existsSync(path.join(akar, 'run.py')));
      assert.ok(fs.existsSync(path.join(akar, 'web', 'index.html')));
      assert.ok(fs.existsSync(path.join(akar, 'padel_scheduler', '__init__.py')));
    });
    periksa('Tidak ada berkas yang bisa dieksekusi di dalam paket', () => {
      const bahaya = [];
      const telusur = (d) => fs.readdirSync(d, { withFileTypes: true }).forEach((e) => {
        const f = path.join(d, e.name);
        if (e.isDirectory()) telusur(f);
        else if (/\.(exe|dll|bat|cmd|ps1|com|scr)$/i.test(e.name)) bahaya.push(f);
      });
      telusur(p.mod.akarKonten());
      assert.deepStrictEqual(bahaya, []);
    });

    // --- 4. Konten rusak: aplikasi mundur ke bawaan paket ---------------
    periksa('Konten yang ditandai rusak berhenti dipakai', () => {
      const ditandai = p.mod.tandaiRusak('server tidak merespons');
      assert.strictEqual(ditandai, '1.3.4');
      assert.strictEqual(p.mod.akarKonten(), null,
        'masih memakai konten yang sudah ditandai rusak');
    });
    periksa('Alasannya tercatat, bukan cuma disimpan diam-diam', () => {
      const log = fs.readFileSync(path.join(dir, 'konten.log'), 'utf8');
      assert.ok(/ditandai rusak/.test(log), log);
      assert.ok(/server tidak merespons/.test(log), log);
    });
  }

  // --- 5. sha512 tidak cocok -> tolak ------------------------------------
  // Inilah satu-satunya yang berdiri di antara "isi dari rilis kita" dan "isi
  // dari mana saja", karena jalur ini memang melewati pemeriksaan Windows.
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.4', ISI_SAH);
    const lain = crypto.createHash('sha512').update('bukan zip ini').digest('base64');
    const srv = await layanRilis({
      manifes: manifesUntuk('1.3.4', zip, { sha512: lain }),
      zip,
      namaZip: 'konten-1.3.4.zip',
    });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('sha512 yang tidak cocok membatalkan pemasangan', () => {
      assert.strictEqual(hasil, null);
      assert.strictEqual(p.mod.akarKonten(), null);
      assert.ok(!fs.existsSync(path.join(dir, 'konten', '1.3.4')),
        'paketnya telanjur dibongkar padahal checksum-nya salah');
    });
    periksa('Penolakannya tercatat dengan kedua checksum', () => {
      const log = fs.readFileSync(path.join(dir, 'konten.log'), 'utf8');
      assert.ok(/sha512 tidak cocok/.test(log), log);
    });
  }

  // --- 6. Nama berkas di manifes tidak boleh jadi perintah ---------------
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.4', ISI_SAH);
    const srv = await layanRilis({
      manifes: manifesUntuk('1.3.4', zip, { berkas: '../../pergi-ke-mana-saja.zip' }),
      zip,
      namaZip: 'konten-1.3.4.zip',
    });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Nama berkas yang tidak sesuai pola ditolak', () => {
      assert.strictEqual(hasil, null);
      assert.strictEqual(p.mod.akarKonten(), null);
    });
  }

  // --- 7. Isi paket tidak lengkap -> tolak, jangan aktifkan --------------
  // Bongkaran yang terpotong meninggalkan folder yang ADA tapi tidak bisa
  // dipakai; aplikasi yang memilihnya gagal hidup dengan pesan yang
  // menyesatkan.
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.4', { 'web/index.html': '<title>x</title>' });
    const srv = await layanRilis({
      manifes: manifesUntuk('1.3.4', zip), zip, namaZip: 'konten-1.3.4.zip',
    });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Paket tanpa run.py ditolak dan tidak diaktifkan', () => {
      assert.strictEqual(hasil, null);
      assert.strictEqual(p.mod.akarKonten(), null);
      assert.ok(!fs.existsSync(path.join(dir, 'konten', '1.3.4')),
        'paket setengah jadi tertinggal di folder tujuan');
    });
  }

  // --- 8. Kerangka aplikasi terlalu tua -> lewati, jangan paksa ----------
  {
    const dir = dirBaru();
    const zip = buatZip('1.4.0', ISI_SAH);
    const srv = await layanRilis({
      manifes: manifesUntuk('1.4.0', zip, { app_minimal: '1.4.0' }),
      zip,
      namaZip: 'konten-1.4.0.zip',
    });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Konten yang butuh kerangka lebih baru dilewati', () => {
      assert.strictEqual(hasil, null);
      assert.strictEqual(p.mod.akarKonten(), null);
      const log = fs.readFileSync(path.join(dir, 'konten.log'), 'utf8');
      assert.ok(/butuh aplikasi >= 1\.4\.0/.test(log), log);
    });
  }

  // --- 9. Konten lama tidak boleh memundurkan aplikasi -------------------
  // Terjadi setelah installer akhirnya berhasil: paket konten lama masih
  // tertinggal di userData, dan tanpa penjaga ini pembaruan justru terasa
  // seperti kemunduran - antarmuka dan mesin jadwal kembali ke yang lama,
  // tanpa jejak kenapa.
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.4', ISI_SAH);
    const srv = await layanRilis({
      manifes: manifesUntuk('1.3.4', zip), zip, namaZip: 'konten-1.3.4.zip',
    });
    const lama = pasangPanggung({ userData: dir, versi: '1.3.3' });
    await lama.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Sebelum installer: konten 1.3.4 dipakai di atas aplikasi 1.3.3', () => {
      assert.strictEqual(lama.mod.versiKonten(), '1.3.4');
    });

    const baru = pasangPanggung({ userData: dir, versi: '1.5.0' });
    periksa('Sesudah installer 1.5.0: konten 1.3.4 diabaikan', () => {
      assert.strictEqual(baru.mod.versiKonten(), null);
      assert.strictEqual(baru.mod.akarKonten(), null);
    });
  }

  // --- 10. Sudah yang terbaru -> tidak mengunduh apa-apa -----------------
  {
    const dir = dirBaru();
    const zip = buatZip('1.3.3', ISI_SAH);
    // namaZip sengaja tidak dilayani: kalau ia sampai mencoba mengunduh, server
    // menjawab 404 dan hasilnya tetap null - jadi yang dibuktikan adalah
    // catatannya, bukan sekadar hasil akhirnya.
    const srv = await layanRilis({ manifes: manifesUntuk('1.3.3', zip), zip });
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({ diam: true, dasar: srv.dasar });
    await srv.tutup();
    periksa('Versi yang sama tidak diunduh ulang', () => {
      assert.strictEqual(hasil, null);
      const log = fs.readFileSync(path.join(dir, 'konten.log'), 'utf8');
      assert.ok(/sudah yang terbaru/.test(log), log);
      assert.ok(!/mengunduh konten/.test(log), log);
    });
  }

  // --- 11. Jaringan mati saat start tidak mengganggu layar host ----------
  {
    const dir = dirBaru();
    const p = pasangPanggung({ userData: dir, versi: '1.3.3' });
    const hasil = await p.mod.periksaKonten({
      diam: true, dasar: 'http://127.0.0.1:1',
    });
    periksa('Gagal menghubungi rilis tidak memunculkan dialog saat start', () => {
      assert.strictEqual(hasil, null);
      assert.strictEqual(p.dialogs.length, 0,
        `dialog: ${JSON.stringify(p.dialogs.map((d) => d.title))}`);
    });
    periksa('Tapi tetap tercatat di berkas', () => {
      const log = fs.readFileSync(path.join(dir, 'konten.log'), 'utf8');
      assert.ok(/pembaruan konten gagal/.test(log), log);
    });
  }

  // --- 12. Jalur unggah rilis --------------------------------------------
  // Kalau langkah ini diam-diam gagal, rilisnya tetap terlihat "berhasil" -
  // installer terbit, kontennya tidak - dan mesin yang installer-nya diblokir
  // Smart App Control kehilangan satu-satunya jalur pembaruannya tanpa satu pun
  // pesan. GitHub-nya ditiru di sini; yang diuji urutan dan isinya.
  {
    const jejak = [];
    const srv = http.createServer((req, res) => {
      let isi = 0;
      req.on('data', (b) => { isi += b.length; });
      req.on('end', () => {
        const nama = new URL(req.url, 'http://x').searchParams.get('name');
        jejak.push(`${req.method} ${req.url.split('?')[0]}${nama ? ` name=${nama}` : ''}`
          + `${isi ? ` (${isi} byte)` : ''}`);
        if (req.method === 'GET' && /\/releases\/tags\//.test(req.url)) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ id: 42 }));
          return;
        }
        if (req.method === 'GET' && /\/releases\/42\/assets$/.test(req.url)) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          // Satu aset lama dengan nama yang sama: rilis ulang tidak boleh
          // menumpuk dua zip di satu rilis.
          res.end(JSON.stringify([{ id: 7, name: 'konten.json' }]));
          return;
        }
        if (req.method === 'DELETE') { res.writeHead(204); res.end(); return; }
        if (req.method === 'POST') {
          res.writeHead(201, { 'Content-Type': 'application/json' });
          res.end('{}');
          return;
        }
        res.writeHead(404); res.end('tidak ada');
      });
    });
    await new Promise((r) => srv.listen(0, '127.0.0.1', r));
    const dasar = `http://127.0.0.1:${srv.address().port}`;

    // Paket konten harus sudah dirakit; kalau belum, rakit di sini supaya tes
    // ini tidak menuntut urutan perintah tertentu dari yang menjalankannya.
    const manifes = path.join(AKAR, 'dist-desktop', 'konten.json');
    if (!fs.existsSync(manifes)) {
      spawnSync(process.execPath, [path.join(AKAR, 'tools', 'paket-konten.js')],
        { cwd: AKAR, stdio: 'pipe' });
    }

    const jalan = await jalankanNode(path.join(AKAR, 'electron', 'unggah-konten.js'), {
      ...process.env,
      GH_TOKEN: 'token-uji',
      GITHUB_RELEASE_TOKEN: '',
      PADELIN_UJI_API: dasar,
      PADELIN_UJI_UNGGAH: dasar,
    });
    await new Promise((r) => srv.close(r));

    const versi = JSON.parse(fs.readFileSync(path.join(AKAR, 'package.json'), 'utf8')).version;
    periksa('Unggah konten berhasil dan menyebut kedua berkasnya', () => {
      assert.strictEqual(jalan.kode, 0, `keluar ${jalan.kode}: ${jalan.keluaran}`);
      assert.ok(jalan.keluaran.includes(`konten-${versi}.zip terunggah`), jalan.keluaran);
      assert.ok(jalan.keluaran.includes('konten.json terunggah'), jalan.keluaran);
    });
    periksa('Aset lama dengan nama sama dihapus dulu, tidak ditumpuk', () => {
      assert.ok(jejak.some((j) => j.startsWith('DELETE')), jejak.join('\n'));
      assert.ok(jalan.keluaran.includes('aset lama konten.json dihapus'), jalan.keluaran);
    });
    periksa('Zip diunggah SEBELUM manifesnya', () => {
      const unggahan = jejak.filter((j) => j.startsWith('POST'));
      assert.strictEqual(unggahan.length, 2, jejak.join('\n'));
      assert.ok(unggahan[0].includes(`name=konten-${versi}.zip`), unggahan.join('\n'));
      assert.ok(unggahan[1].includes('name=konten.json'), unggahan.join('\n'));
    });
    periksa('Yang terkirim benar-benar berisi paketnya, bukan berkas kosong', () => {
      const zip = jejak.find((j) => j.includes(`name=konten-${versi}.zip`));
      const byte = Number((zip.match(/\((\d+) byte\)/) || [])[1] || 0);
      assert.ok(byte > 50 * 1024, `cuma ${byte} byte terkirim`);
    });
  }

  console.log(`\n${lulus} lulus, ${gagal.length} gagal`);
  process.exit(gagal.length ? 1 : 0);
}

utama();
