// Unggah paket konten ke rilis GitHub yang sama dengan installer-nya.
//
// Dipasang sebagai skrip "postrelease" di package.json, jadi npm menjalankannya
// sendiri sesudah "release". Letaknya di belakang memang disengaja: kalau
// installer-nya gagal terbit, tidak ada gunanya menerbitkan kontennya.
//
// Kenapa tidak lewat electron-builder: ia hanya mengunggah artefak yang ia buat
// sendiri. Dua berkas ini bukan artefaknya.
//
// Aman diulang: aset dengan nama yang sama dihapus lebih dulu, jadi rilis ulang
// tidak menumpuk konten-X.Y.Z.zip berlapis-lapis di satu rilis.
//
// Bisa dijalankan sendiri:
//   node electron/unggah-konten.js

const fs = require('fs');
const path = require('path');

const AKAR = path.join(__dirname, '..');
const KELUAR = path.join(AKAR, 'dist-desktop');
// Alamat GitHub bisa dialihkan HANYA oleh tes (tests/test_konten.js menyalakan
// server tiruan). Jalur rilis sungguhan tidak pernah menyetel keduanya - dan
// kalau sampai tersetel di CI, yang terjadi cuma unggahan gagal terhubung,
// bukan aset yang terbit ke tempat lain.
const API = process.env.PADELIN_UJI_API || 'https://api.github.com';
const UNGGAH = process.env.PADELIN_UJI_UNGGAH || 'https://uploads.github.com';

function gagal(pesan) {
  throw new Error(`Paket konten GAGAL diunggah\n    ${pesan}`);
}

// Urutan token yang sama dengan ensure-tag.js dan electron-builder, supaya
// tidak mungkin skrip ini memakai token yang berbeda dari yang mengunggah
// installer-nya.
function ambilToken() {
  const token = process.env.GITHUB_RELEASE_TOKEN
    || process.env.GH_TOKEN
    || process.env.GITHUB_TOKEN;
  if (!token || !token.trim()) {
    gagal('Token GitHub tidak ada. Setel GH_TOKEN sebelum merilis.');
  }
  return token.trim();
}

function ambilTujuan() {
  const pkg = JSON.parse(fs.readFileSync(path.join(AKAR, 'package.json'), 'utf8'));
  const daftar = [].concat((pkg.build && pkg.build.publish) || []);
  const github = daftar.find((p) => p && p.provider === 'github');
  if (!github) gagal('Tidak ada publish provider "github" di package.json.');
  return { tag: `v${pkg.version}`, owner: github.owner, repo: github.repo };
}

async function api(token, jalur, { method = 'GET' } = {}) {
  const res = await fetch(API + jalur, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
  });
  const teks = await res.text();
  let data = null;
  try { data = teks ? JSON.parse(teks) : null; } catch { /* biarkan mentah */ }
  return { status: res.status, data, teks };
}

function ringkas(res) {
  const pesan = res.data && res.data.message ? res.data.message : res.teks;
  return `HTTP ${res.status} ${String(pesan).slice(0, 300)}`;
}

async function unggahSatu(token, owner, repo, rilisId, berkas, tipe) {
  const isi = fs.readFileSync(berkas);
  const nama = path.basename(berkas);

  const ada = await api(token, `/repos/${owner}/${repo}/releases/${rilisId}/assets`);
  if (ada.status === 200) {
    const lama = (ada.data || []).find((a) => a.name === nama);
    if (lama) {
      const hapus = await api(token, `/repos/${owner}/${repo}/releases/assets/${lama.id}`,
        { method: 'DELETE' });
      if (hapus.status >= 300) gagal(`hapus aset lama ${nama}: ${ringkas(hapus)}`);
      console.log(`  * aset lama ${nama} dihapus`);
    }
  }

  const res = await fetch(
    `${UNGGAH}/repos/${owner}/${repo}/releases/${rilisId}/assets`
      + `?name=${encodeURIComponent(nama)}`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': tipe,
        'Content-Length': String(isi.length),
      },
      body: isi,
    });
  if (res.status >= 300) {
    gagal(`unggah ${nama}: HTTP ${res.status} ${(await res.text()).slice(0, 300)}`);
  }
  console.log(`  * ${nama} terunggah  ${(isi.length / 1024).toFixed(1)} KB`);
}

async function main() {
  const zipManifes = path.join(KELUAR, 'konten.json');
  if (!fs.existsSync(zipManifes)) {
    gagal(`${zipManifes} tidak ada - jalankan "npm run paket:konten" lebih dulu.`);
  }
  const manifes = JSON.parse(fs.readFileSync(zipManifes, 'utf8'));
  const zip = path.join(KELUAR, manifes.berkas);
  if (!fs.existsSync(zip)) gagal(`${zip} tidak ada, padahal manifesnya menyebutnya.`);

  const token = ambilToken();
  const { tag, owner, repo } = ambilTujuan();
  if (manifes.versi !== tag.replace(/^v/, '')) {
    gagal(`manifes menyebut versi ${manifes.versi}, sedangkan tag rilisnya ${tag} `
      + '- paketnya dibuat sebelum versi dinaikkan.');
  }

  const rilis = await api(token, `/repos/${owner}/${repo}/releases/tags/${tag}`);
  if (rilis.status !== 200) gagal(`rilis ${tag} tidak ditemukan: ${ringkas(rilis)}`);

  await unggahSatu(token, owner, repo, rilis.data.id, zip, 'application/zip');
  // konten.json diunggah TERAKHIR. Aplikasi memeriksa manifes dulu lalu menarik
  // zip-nya; kalau urutannya dibalik, ada jendela waktu ketika manifes sudah
  // menunjuk berkas yang belum ada.
  await unggahSatu(token, owner, repo, rilis.data.id, zipManifes, 'application/json');
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
