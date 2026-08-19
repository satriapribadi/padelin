// Pastikan tag DAN rilisnya sudah ada sebelum electron-builder mengunggah apa pun.
//
// GitHub menolak membuat tag secara implisit lewat endpoint rilis dengan token
// yang dipakai proyek ini: POST /releases dengan draft:false untuk tag yang
// belum ada dijawab 422 "Published releases must have a valid tag". Yang dikirim
// electron-builder sendiri tidak salah - ia memang sengaja tidak menyertakan
// target_commitish (lihat createRelease di electron-publish/out/gitHubPublisher.js),
// dan permintaan yang sama persis langsung berhasil begitu tagnya sudah ada.
//
// Dugaan penyebabnya: membuat tag bisa memicu workflow, jadi endpoint rilis
// menuntut izin workflows=write yang tidak dimiliki token fine-grained kita,
// sementara POST /git/refs cukup dengan contents=write. Itu belum dibuktikan -
// kalau suatu saat izin tokennya dinaikkan dan rilis implisit jalan lagi,
// berkas ini boleh dibuang.
//
// Dipasang sebagai skrip "prerelease" di package.json, jadi npm menjalankannya
// sendiri sebelum "release". Letaknya di depan sengaja: gagal di sini berarti
// gagal dalam hitungan detik, bukan setelah build 200 MB yang sia-sia.
//
// Tagnya menunjuk ke ujung branch default repo RILIS (padelin-rilis), bukan repo
// sumber - itu yang ditunjuk semua tag sebelumnya, dan repo rilis memang tidak
// menyimpan kode.
//
// Rilisnya ikut dibuat di sini karena electron-builder menjalankan SATU
// publisher per artefak, dan keduanya memeriksa "apakah rilisnya sudah ada" pada
// detik yang sama. Terukur di runner CI: dua publisher (installer dan blockmap)
// sama-sama dijawab "release doesn't exist", sama-sama membuatnya, dan GitHub
// dengan senang hati menerima DUA rilis yang menunjuk tag yang sama - satu
// berisi installer + latest.yml, satu lagi cuma blockmap. Buildnya melapor
// sukses; yang rusak baru terlihat dari daftar rilis. Di mesin sendiri
// balapannya tidak pernah menang karena unggahannya berjalan lebih lambat.
//
// Dengan rilisnya sudah ada lebih dulu, kedua publisher menemukan yang sama dan
// tidak ada yang membuat apa pun.
//
// Aman diulang: kalau tag dan rilisnya sudah ada, ia tidak melakukan apa-apa.
//
// Bisa dijalankan sendiri:
//
//   node electron/ensure-tag.js

const fs = require('fs');
const path = require('path');

const AKAR = path.join(__dirname, '..');
const API = 'https://api.github.com';

function gagal(pesan) {
  throw new Error(`Tag rilis GAGAL disiapkan\n    ${pesan}`);
}

// Urutan yang sama dengan yang dipakai electron-builder, supaya tidak mungkin
// skrip ini memakai token yang berbeda dari yang dipakai saat mengunggah.
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
  if (!github) {
    gagal('Tidak ada publish provider "github" di package.json.');
  }
  return { tag: `v${pkg.version}`, owner: github.owner, repo: github.repo };
}

async function api(token, jalur, { method = 'GET', body } = {}) {
  const res = await fetch(API + jalur, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const teks = await res.text();
  let data = null;
  try { data = teks ? JSON.parse(teks) : null; } catch { /* biarkan mentah */ }
  return { status: res.status, data, teks };
}

// Pesan GitHub dikutip apa adanya: menerjemahkannya cuma menyembunyikan sebab.
function ringkas(res) {
  const pesan = res.data && res.data.message ? res.data.message : res.teks;
  return `HTTP ${res.status} ${String(pesan).slice(0, 300)}`;
}

async function main() {
  const token = ambilToken();
  const { tag, owner, repo } = ambilTujuan();

  const ada = await api(token, `/repos/${owner}/${repo}/git/ref/tags/${tag}`);
  if (ada.status === 200) {
    console.log(`  * tag ${tag} sudah ada  ${ada.data.object.sha.slice(0, 7)}`);
    await pastikanRilis(token, owner, repo, tag, tag.replace(/^v/, ''));
    return;
  }
  if (ada.status !== 404) {
    gagal(`Tidak bisa memeriksa tag ${tag} di ${owner}/${repo}.\n    ${ringkas(ada)}`);
  }

  const repoInfo = await api(token, `/repos/${owner}/${repo}`);
  if (repoInfo.status !== 200) {
    gagal(`Tidak bisa membaca repo ${owner}/${repo}.\n    ${ringkas(repoInfo)}`);
  }
  const cabang = repoInfo.data.default_branch;

  const kepala = await api(token, `/repos/${owner}/${repo}/git/ref/heads/${cabang}`);
  if (kepala.status !== 200) {
    gagal(`Tidak bisa membaca ujung branch ${cabang}.\n    ${ringkas(kepala)}`);
  }
  const sha = kepala.data.object.sha;

  const buat = await api(token, `/repos/${owner}/${repo}/git/refs`, {
    method: 'POST',
    body: { ref: `refs/tags/${tag}`, sha },
  });
  if (buat.status === 201) {
    console.log(`  * tag ${tag} dibuat  ${sha.slice(0, 7)} (${cabang})`);
    await pastikanRilis(token, owner, repo, tag, tag.replace(/^v/, ''));
    return;
  }
  // Balapan dengan proses lain yang membuat tag sama: hasil akhirnya tetap yang
  // kita inginkan, jadi bukan kegagalan.
  if (buat.status === 422 && /already exists/i.test(buat.teks)) {
    console.log(`  * tag ${tag} sudah ada  ${sha.slice(0, 7)}`);
    await pastikanRilis(token, owner, repo, tag, tag.replace(/^v/, ''));
    return;
  }
  gagal(`Tidak bisa membuat tag ${tag} di ${owner}/${repo}.\n    ${ringkas(buat)}\n`
    + `    Token butuh izin contents=write pada repo itu.`);
}

/**
 * Pastikan rilis untuk tag ini ada, supaya tidak ada yang membuatnya berdua.
 *
 * Namanya dibuat sama dengan yang dipakai electron-builder (versi tanpa "v"),
 * jadi rilis yang lahir di sini tidak bisa dibedakan dari yang lahir di sana -
 * daftar rilis tetap seragam dengan versi-versi sebelumnya.
 *
 * draft:false disengaja dan penting: rilis draft tidak terlihat tanpa token,
 * jadi aplikasi terpasang tidak akan pernah menemukan pembaruannya. Itu alasan
 * yang sama yang membuat releaseType:release dipasang eksplisit di package.json.
 */
async function pastikanRilis(token, owner, repo, tag, versi) {
  const ada = await api(token, `/repos/${owner}/${repo}/releases/tags/${tag}`);
  if (ada.status === 200) {
    console.log(`  * rilis ${tag} sudah ada  id=${ada.data.id}`);
    return;
  }
  if (ada.status !== 404) {
    gagal(`Tidak bisa memeriksa rilis ${tag} di ${owner}/${repo}.\n    ${ringkas(ada)}`);
  }

  const buat = await api(token, `/repos/${owner}/${repo}/releases`, {
    method: 'POST',
    body: { tag_name: tag, name: versi, draft: false, prerelease: false },
  });
  if (buat.status === 201) {
    console.log(`  * rilis ${tag} dibuat  id=${buat.data.id}`);
    return;
  }
  // Kalah balapan dengan proses lain: yang kita mau tetap tercapai.
  if (buat.status === 422 && /already_exists|already exists/i.test(buat.teks)) {
    console.log(`  * rilis ${tag} sudah ada (dibuat proses lain)`);
    return;
  }
  gagal(`Tidak bisa membuat rilis ${tag} di ${owner}/${repo}.\n    ${ringkas(buat)}\n`
    + `    Token butuh izin contents=write pada repo itu.`);
}

main().catch((err) => {
  console.error(`\n  x ${err.message}\n`);
  process.exit(1);
});
