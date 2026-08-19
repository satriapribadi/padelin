'use strict';

import { tradeoffChart, engagementChart, restShareChart, hideTip } from './charts.js';
import { createCombo } from './combo.js';

// ---------------------------------------------------------------------------
// Util
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const rp = (n) => 'Rp ' + Math.round(n || 0).toLocaleString('id-ID');
// Angka yang dipakai host sebagai AMBANG ("fee minimal", "supaya tidak nombok")
// harus dibulatkan ke atas, bukan ke terdekat. Biaya 306.593 dibagi 8 orang =
// 38.324,125; dibulatkan ke terdekat jadi 38.324, dan host yang menuruti angka
// itu justru rugi Rp 1 - lalu panelnya sendiri menyebutnya "bermasalah".
const rpUp = (n) => 'Rp ' + Math.ceil(n || 0).toLocaleString('id-ID');

const HARI = ['Minggu', 'Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu'];
const BULAN = ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli',
  'Agustus', 'September', 'Oktober', 'November', 'Desember'];

/** ISO (2026-08-09) -> "Sabtu, 9 Agustus 2026". Nilai lain lewat apa adanya. */
function tanggalID(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((v || '').trim());
  if (!m) return v || '-';
  // Konstruktor UTC, lalu dibaca sebagai UTC juga: kalau tidak, zona waktu
  // negatif menggeser tanggalnya mundur satu hari.
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return `${HARI[d.getUTCDay()]}, ${d.getUTCDate()} ${BULAN[d.getUTCMonth()]} `
    + `${d.getUTCFullYear()}`;
}
const pct = (x) => (x * 100).toFixed(0) + '%';

function toast(msg) {
  const t = el('div', 'toast', esc(msg));
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2400);
}

async function api(path, payload) {
  const res = await fetch(path, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({ error: 'Respons tidak valid' }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let players = [];
let nextId = 0;
let schedule = null;
let currentEventId = null;
// Sidik jari setup saat jadwal yang sekarang dibuat. null = belum ada jadwal.
let scheduleStamp = null;
let presets = {};
let analyzeTimer = null;
// Nama court pilihan host. Indeks 0 = court 1; entri kosong berarti court itu
// memakai nama bawaan "C1", "C2", ... Yang tersimpan bersama acaranya adalah
// schedule.config.court_names - daftar ini salinannya, supaya nama tetap
// terbawa saat host menekan Generate lagi.
let courtNames = [];
// Batas panjang nama court. Datang dari server (/api/presets) supaya kotak
// isian memotong di angka yang sama dengan yang dipotong Config.
let courtNameMax = 14;

// ---------------------------------------------------------------------------
// Kartu statistik
// ---------------------------------------------------------------------------
// Hijau dan merah hanya berjarak dE 6.5 di bawah deuteranopia, jadi status
// tidak pernah disampaikan lewat warna saja: selalu ada glif + kata.
const STATE_MARK = {
  good: { icon: '✓', word: 'aman' },
  warn: { icon: '!', word: 'perhatikan' },
  bad: { icon: '✕', word: 'bermasalah' },
};

/** Bentuk HTML-nya dipisah supaya panel yang merakit string bisa memakai ulang
 *  kartu yang sama - kalau tidak, panel itu diam-diam kehilangan glif status. */
function statTileHTML(k, v, sub, state, extra) {
  const mark = STATE_MARK[state];
  const kelas = (state ? ' ' + state : '') + (extra?.cls ? ' ' + extra.cls : '');
  return `<div class="stat${kelas}"${extra?.attrs ? ' ' + extra.attrs : ''}>` +
    `<div class="k">${esc(k)}</div>` +
    `<div class="v">${mark ? `<span class="ico" aria-hidden="true">${mark.icon}</span>` : ''}${esc(v)}</div>` +
    `<div class="s">${esc(sub)}${mark ? ` <span class="state-word">· ${mark.word}</span>` : ''}</div>` +
    `</div>`;
}

function statTile(k, v, sub, state) {
  const box = el('div');
  box.innerHTML = statTileHTML(k, v, sub, state);
  return box.firstElementChild;
}

// ---------------------------------------------------------------------------
// Tab
// ---------------------------------------------------------------------------
document.querySelectorAll('.tabs button').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.remove('on'));
    document.querySelectorAll('.view').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    $('view-' + b.dataset.view).classList.add('on');
    if (b.dataset.view === 'riwayat') loadEvents();
    if (b.dataset.view === 'master') { loadMaster(); loadMasterTables(); loadClubSummary(); }
  };
});

// ---------------------------------------------------------------------------
// Peserta
// ---------------------------------------------------------------------------
function renderPlayers() {
  const t = $('ptable');
  t.innerHTML = '';
  if (!players.length) {
    t.innerHTML = '<tr><td class="empty">Belum ada peserta.</td></tr>';
  } else {
    const head = el('tr', null,
      '<th>Nama</th><th style="width:64px">Rating</th><th style="width:56px">L/P</th>' +
      '<th style="width:120px">Partner tetap</th><th style="width:130px">Permintaan</th><th style="width:26px"></th>');
    t.appendChild(el('thead')).appendChild(head);
    const body = el('tbody');

    players.forEach((p, i) => {
      const tr = el('tr');
      const opts = players.filter((o) => o.id !== p.id)
        .map((o) => `<option value="${o.id}" ${p.partner_id === o.id ? 'selected' : ''}>${esc(o.name)}</option>`)
        .join('');
      tr.innerHTML =
        `<td><input class="nm" data-f="name" data-i="${i}" value="${esc(p.name)}"></td>` +
        `<td><input type="number" step="0.5" min="0" max="7" data-f="rating" data-i="${i}" value="${p.rating}"></td>` +
        `<td><select data-f="gender" data-i="${i}">
            <option value="" ${!p.gender ? 'selected' : ''}>-</option>
            <option value="M" ${p.gender === 'M' ? 'selected' : ''}>L</option>
            <option value="F" ${p.gender === 'F' ? 'selected' : ''}>P</option>
          </select></td>` +
        `<td><select data-f="partner_id" data-i="${i}"><option value="">bebas</option>${opts}</select></td>` +
        `<td><select data-f="court_preference" data-i="${i}">
            <option value="" ${!p.court_preference ? 'selected' : ''}>bebas</option>
            <option value="women_only" ${p.court_preference === 'women_only' ? 'selected' : ''}>4 perempuan</option>
            <option value="men_only" ${p.court_preference === 'men_only' ? 'selected' : ''}>4 laki-laki</option>
            <option value="same_gender" ${p.court_preference === 'same_gender' ? 'selected' : ''}>satu gender</option>
            <option value="mixed_team" ${p.court_preference === 'mixed_team' ? 'selected' : ''}>partner beda gender</option>
          </select></td>` +
        `<td><button class="x" data-del="${i}">&times;</button></td>`;
      body.appendChild(tr);
    });
    t.appendChild(body);
  }

  const men = players.filter((p) => p.gender === 'M').length;
  const women = players.filter((p) => p.gender === 'F').length;
  const locked = players.filter((p) => p.partner_id !== null).length;
  $('counts').innerHTML =
    `<span>Total <b>${players.length}</b></span><span>Putra <b>${men}</b></span>` +
    `<span>Putri <b>${women}</b></span><span>Partner tetap <b>${locked}</b></span>`;

  scheduleAnalyze();
  // Biaya dibagi jumlah peserta, jadi menambah atau menghapus satu orang
  // mengubah seluruh panel biaya - termasuk saran fee per margin.
  scheduleEconomics();
}

$('ptable').addEventListener('input', (e) => {
  const f = e.target.dataset.f, i = +e.target.dataset.i;
  if (!f) return;
  const p = players[i];
  if (f === 'rating') p.rating = parseFloat(e.target.value) || 0;
  else if (f === 'name') p.name = e.target.value;
  scheduleAnalyze();
});

$('ptable').addEventListener('change', (e) => {
  const f = e.target.dataset.f, i = +e.target.dataset.i;
  if (!f) return;
  const p = players[i];
  if (f === 'gender') p.gender = e.target.value || null;
  else if (f === 'court_preference') p.court_preference = e.target.value || null;
  else if (f === 'partner_id') {
    // Partner tetap harus timbal balik: kalau A pilih B, B otomatis pilih A.
    const old = p.partner_id;
    if (old !== null) { const o = players.find((x) => x.id === old); if (o) o.partner_id = null; }
    const v = e.target.value === '' ? null : +e.target.value;
    if (v !== null) {
      const mate = players.find((x) => x.id === v);
      if (mate) {
        if (mate.partner_id !== null) {
          const prev = players.find((x) => x.id === mate.partner_id);
          if (prev) prev.partner_id = null;
        }
        mate.partner_id = p.id;
      }
    }
    p.partner_id = v;
    renderPlayers();
    return;
  }
  scheduleAnalyze();
});

$('ptable').addEventListener('click', (e) => {
  const d = e.target.dataset.del;
  if (d === undefined) return;
  const removed = players[+d];
  if (removed.partner_id !== null) {
    const mate = players.find((x) => x.id === removed.partner_id);
    if (mate) mate.partner_id = null;
  }
  players.splice(+d, 1);
  renderPlayers();
});

$('parse-bulk').onclick = () => {
  const lines = $('bulk').value.split('\n').map((s) => s.trim()).filter(Boolean);
  // Nama yang sudah ada dilewati, tanpa membedakan huruf besar-kecil - menempel
  // daftar dua kali seharusnya tidak menggandakan pesertanya.
  let added = 0, skipped = 0;
  lines.forEach((line) => {
    const parts = line.split(/[,;\t]/).map((s) => s.trim());
    const name = parts[0];
    if (!name) return;
    let rating = 3.0, gender = null;
    for (let i = 1; i < parts.length; i++) {
      const v = parts[i];
      if (!v) continue;
      const num = parseFloat(v.replace(',', '.'));
      if (!isNaN(num) && /^[\d.,]+$/.test(v)) rating = num;
      else {
        const g = v.toUpperCase();
        if (['L', 'M', 'COWOK', 'PRIA', 'PUTRA'].includes(g)) gender = 'M';
        else if (['P', 'F', 'W', 'CEWEK', 'WANITA', 'PUTRI'].includes(g)) gender = 'F';
      }
    }
    if (addParticipant(name, rating, gender)) added++;
    else skipped++;
  });
  $('bulk').value = '';
  renderPlayers();
  const bits = [];
  if (added) bits.push(`${added} peserta ditambahkan`);
  if (skipped) bits.push(`${skipped} dilewati (sudah ada)`);
  if (bits.length) toast(bits.join(', '));
};

$('clear-players').onclick = () => {
  if (players.length && !confirm('Hapus semua peserta?')) return;
  players = []; renderPlayers();
};

// ---------------------------------------------------------------------------
// Babak / segmen
// ---------------------------------------------------------------------------
function segRows() { return Array.from(document.querySelectorAll('.seg-editor')); }

const SEG_RULES = [
  ['open', 'Bebas'], ['men', 'Putra saja'], ['women', 'Putri saja'],
  ['mixed', 'Mixed (1L+1P)'], ['same_gender', 'Tim satu gender'],
];

function addSeg(label = '', rounds = 3, rule = 'open', after = null) {
  const row = el('div', 'seg-editor');
  const opts = SEG_RULES.map(
    ([v, t]) => `<option value="${v}" ${rule === v ? 'selected' : ''}>${t}</option>`
  ).join('');
  row.innerHTML =
    `<span class="seg-grip" tabindex="0" role="button"` +
    ` title="Seret untuk mengurutkan (atau panah atas/bawah)">⠿</span>` +
    `<input placeholder="Nama babak" value="${esc(label)}">` +
    `<input type="number" min="1" max="40" value="${rounds}">` +
    `<select>${opts}</select>` +
    `<button class="seg-dup" title="Gandakan babak ini">⧉</button>` +
    `<button class="x" title="Hapus babak ini">&times;</button>`;

  row.querySelector('.x').onclick = () => { row.remove(); onSegChange(); };
  row.querySelector('.seg-dup').onclick = () => {
    const [name, num, sel] = [row.children[1], row.children[2], row.children[3]];
    addSeg(name.value, +num.value || 1, sel.value, row);
    onSegChange();
  };

  // Baris hanya bisa diseret lewat gagangnya. Kalau seluruh baris draggable,
  // menyeleksi teks di dalam input justru ikut memulai drag.
  const grip = row.querySelector('.seg-grip');
  grip.addEventListener('mousedown', () => { row.draggable = true; });
  grip.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    const sib = e.key === 'ArrowUp'
      ? row.previousElementSibling : row.nextElementSibling;
    if (!sib) return;
    if (e.key === 'ArrowUp') row.parentNode.insertBefore(row, sib);
    else row.parentNode.insertBefore(sib, row);
    grip.focus();
    onSegChange();
  });

  row.addEventListener('dragstart', () => row.classList.add('dragging'));
  row.addEventListener('dragend', () => {
    row.classList.remove('dragging');
    row.draggable = false;
    onSegChange();
  });

  row.addEventListener('change', onSegChange);
  row.addEventListener('input', onSegChange);

  const host = $('segments');
  if (after && after.parentNode === host) after.after(row);
  else host.appendChild(row);
  return row;
}

/** Sisipkan baris yang sedang diseret di posisi terdekat dengan kursor. */
function segDropTarget(container, y) {
  const others = [...container.querySelectorAll('.seg-editor:not(.dragging)')];
  return others.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    return offset < 0 && offset > closest.offset
      ? { offset, element: child } : closest;
  }, { offset: Number.NEGATIVE_INFINITY, element: null }).element;
}

$('interleave').addEventListener('change', onSegChange);

$('segments').addEventListener('dragover', (e) => {
  e.preventDefault();
  const dragging = document.querySelector('.seg-editor.dragging');
  if (!dragging) return;
  const target = segDropTarget($('segments'), e.clientY);
  if (target) $('segments').insertBefore(dragging, target);
  else $('segments').appendChild(dragging);
});

/**
 * Cerminan round_plan() di server, hanya untuk pratinjau urutan.
 *
 * Tiap kemunculan ke-k dari babak bercount c ditaruh di posisi (k + 0.5) / c,
 * lalu semuanya diurutkan - urutan babak jadi pemutus seri.
 */
function segmentOrder(segs, interleave) {
  if (!interleave) {
    return segs.flatMap((s) => Array.from({ length: s.rounds }, () => s));
  }
  const slots = [];
  segs.forEach((s, order) => {
    for (let k = 0; k < s.rounds; k++) {
      slots.push({ pos: (k + 0.5) / s.rounds, order, seg: s });
    }
  });
  slots.sort((a, b) => a.pos - b.pos || a.order - b.order);
  return slots.map((x) => x.seg);
}

/** Total ronde ikut diperlihatkan: itu yang menentukan menit per ronde. */
function onSegChange() {
  const segs = getSegments();
  const total = segs.reduce((t, s) => t + s.rounds, 0);
  const box = $('seg-total');
  if (!total) {
    box.textContent = '';
  } else {
    const usable = (+$('duration').value || 0) - (+$('warmup').value || 0);
    const per = total ? Math.max(1, Math.floor(usable / total)) : 0;
    const order = segmentOrder(segs, $('interleave').checked)
      .map((s) => s.label.slice(0, 6)).join(' → ');
    box.textContent = `${segs.length} babak · ${total} ronde · `
      + `${per} menit per ronde agar pas ${$('duration').value} menit sewa`
      + (segs.length > 1 ? `
Urutan: ${order}` : '');
  }
  scheduleAnalyze();
}

function getSegments() {
  // Kolom 0 adalah gagang seret, jadi field-nya mulai dari indeks 1.
  return segRows().map((r) => ({
    label: r.children[1].value || 'Babak',
    rounds: +r.children[2].value || 0,
    rule: r.children[3].value,
  })).filter((s) => s.rounds > 0);
}

$('add-seg').onclick = (e) => {
  e.preventDefault();
  addSeg('Babak', 3, 'open');
  onSegChange();
};

$('clear-seg').onclick = (e) => {
  e.preventDefault();
  if (segRows().length && !confirm('Hapus semua babak?')) return;
  $('segments').innerHTML = '';
  onSegChange();
};

// Memilih preset TIDAK mengubah apa pun - hanya memperlihatkan penjelasannya.
// Sebelumnya pemilihan langsung menghapus seluruh babak yang sudah disusun,
// dan itu kejutan yang merugikan: susunan hilang tanpa bisa dibatalkan.
$('preset').onchange = () => {
  const p = presets[$('preset').value];
  $('preset-desc').textContent = p ? p.description : '';
};

function applyPreset(replace) {
  const p = presets[$('preset').value];
  if (!p || !p.segments.length) {
    toast(replace ? 'Preset ini memang tanpa babak' : 'Preset ini tidak punya babak');
    if (replace) { $('segments').innerHTML = ''; onSegChange(); }
    return;
  }
  if (replace && segRows().length
      && !confirm(`Ganti ${segRows().length} babak yang ada dengan preset ini?`)) {
    return;
  }
  if (replace) $('segments').innerHTML = '';
  p.segments.forEach((s) => addSeg(s.label, s.rounds, s.rule));
  onSegChange();
  toast(replace ? 'Preset diterapkan' : `${p.segments.length} babak ditambahkan`);
}

$('preset-append').onclick = (e) => { e.preventDefault(); applyPreset(false); };
$('preset-replace').onclick = (e) => { e.preventDefault(); applyPreset(true); };

// ---------------------------------------------------------------------------
// Payload
// ---------------------------------------------------------------------------
/**
 * Pilihan "Kualitas optimasi" membawa DUA angka, bukan satu.
 *
 * Kesabaran host bisa dibelanjakan ke effort (seberapa dalam satu penjadwalan
 * dioptimasi) atau ke percobaan (berapa lintasan acak dijajal lalu diambil yang
 * terbaik), dan keduanya berongkos waktu. Selector ini dulu cuma menggerakkan
 * effort, jadi separuh anggaran yang tersedia tidak pernah terpakai.
 *
 * Diukur pada 4 ukuran meet x 6 seed, effort 160.000 dengan 3 percobaan kalah
 * dari effort 80.000 dengan 6 percobaan di keempatnya, pada waktu yang praktis
 * sama - 15,2 lawan 12,0 pasang lawan berulang pada 6 putra + 4 putri di 1
 * court, dan 41,8 lawan 38,5 pada 6 putra + 6 putri di 2 court format campur.
 */
function effortSetting() {
  const [effort, attempts] = $('effort').value.split(':').map(Number);
  return { effort, attempts: attempts || 3 };
}

/**
 * Berapa ronde yang muat di jam sewa. Sama dengan hitungan server
 * (capacity.rounds_from_duration): sisa waktu yang tidak cukup untuk satu ronde
 * penuh tidak dipakai.
 */
function rondeMuat() {
  const pakai = (+$('duration').value || 0) - (+$('warmup').value || 0);
  const per = +$('round_min').value || 0;
  return per > 0 ? Math.max(0, Math.floor(pakai / per)) : 0;
}

/**
 * Court yang dilepas di tengah acara, dalam bentuk yang dikirim ke server.
 *
 * Dikirim null-null kalau tidak dipakai, BUKAN dihilangkan dari payload: acara
 * tersimpan yang dulu punya pengurangan lalu dimatikan host harus ikut
 * terhapus saat disimpan ulang, dan field yang hilang tidak menghapus apa pun.
 *
 * Ronde mulai dijepit ke jumlah ronde yang benar-benar ada. Tanpa itu, host yang
 * memperpendek durasi setelah mengisi "mulai ronde 11" mengirim rencana di luar
 * acara, dan yang ia lihat cuma jadwal yang court-nya tidak pernah berkurang.
 */
function courtDropPayload() {
  if (!$('courts_drop').checked) {
    return { courts_after: null, courts_from_round: null };
  }
  const ronde = rondeMuat();
  const mulai = Math.min(Math.max(2, +$('courts_from_round').value || 2),
                         Math.max(2, ronde));
  return {
    courts_after: Math.min(Math.max(1, +$('courts_after').value || 1),
                           +$('courts').value || 1),
    courts_from_round: mulai,
  };
}

function buildPayload(tambahan) {
  return Object.assign(_buildPayload(), tambahan || {});
}

function _buildPayload() {
  const opt = effortSetting();
  return {
    club_id: $('club_id').value ? +$('club_id').value : null,
    venue_id: $('venue_id').value ? +$('venue_id').value : null,
    title: $('title').value || 'Meet Padel',
    event_date: $('event_date').value,
    venue: $('venue').value,
    start_clock: $('start_clock').value,
    courts: +$('courts').value,
    ...courtDropPayload(),
    duration_minutes: +$('duration').value,
    round_minutes: +$('round_min').value,
    warmup_minutes: +$('warmup').value,
    mode: $('mode').value,
    tier_count: +$('tier_count').value,
    referees_per_court: +$('referees').value,
    ballboys_per_court: +$('ballboys').value,
    seed: +$('seed').value,
    effort: opt.effort,
    attempts: opt.attempts,
    cpsat_seconds: +$('cpsat_seconds').value,
    segments: getSegments(),
    // Nama court ikut dikirim supaya ia bertahan melewati Generate berikutnya
    // dan ikut tersimpan di riwayat. Ia tidak mengubah susunan apa pun - itu
    // sebabnya ia juga tidak masuk schedulingStamp(): mengganti nama court
    // tidak membuat jadwal yang di layar jadi basi.
    court_names: courtNames,
    interleave_segments: $('interleave').checked,
    allowed_matchups: selectedMatchups(),
    players: players.map((p) => ({
      id: p.id, name: p.name, rating: p.rating, gender: p.gender,
      partner_id: p.partner_id, court_preference: p.court_preference,
    })),
    economics: {
      court_price_per_hour: +$('court_price').value,
      fee_per_player: +$('fee').value,
      other_costs: +$('other_costs').value,
    },
  };
}

// ---------------------------------------------------------------------------
// Analisa kelayakan
// ---------------------------------------------------------------------------
function scheduleAnalyze() {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(runAnalyze, 250);
}

/**
 * Ringkasan uang di tab Setup: biaya, pemasukan, untung, margin.
 *
 * Dihitung langsung di browser dari angka yang sama yang dipakai panel Biaya,
 * supaya host melihat konsekuensi fee-nya sambil menyusun acara - bukan setelah
 * pindah tab dan menekan tombol.
 */
function renderSetupEconomics(report) {
  const host = $('setup-econ');
  const n = players.length;
  const price = +$('court_price').value || 0;
  const fee = +$('fee').value || 0;
  const other = +$('other_costs').value || 0;
  const hours = (+$('duration').value || 0) / 60;
  const courts = +$('courts').value || 0;

  if (n < 4 || (!price && !fee)) { host.textContent = ''; return; }

  // Court-jam yang benar-benar dibayar. Court yang dilepas di tengah acara
  // memotongnya, dan itu justru alasan host memakai fitur itu - kartu biaya yang
  // tetap menagih court penuh membuat penghematannya tidak terlihat di mana pun.
  const drop = courtDropPayload();
  let courtHours = courts * hours;
  let hoursLabel = `${courts} court x ${hours} jam`;
  if (drop.courts_after) {
    const menitAwal = (+$('warmup').value || 0)
      + (drop.courts_from_round - 1) * (+$('round_min').value || 0);
    const menit = +$('duration').value || 0;
    const awal = Math.min(menit, menitAwal);
    courtHours = (courts * awal + drop.courts_after * (menit - awal)) / 60;
    hoursLabel = `${(+courtHours.toFixed(2))} court-jam, court berkurang`;
  }
  const cost = courtHours * price + other;
  const revenue = n * fee;
  const profit = revenue - cost;
  const margin = revenue > 0 ? (profit / revenue) * 100 : 0;
  const perPlayer = n ? cost / n : 0;
  const minutes = report ? report.playing_minutes_per_player : 0;

  // Fee yang persis titik impas menyisakan receh: biaya 306.593 dibagi 8 tidak
  // bulat, jadi menagih 38.325 menghasilkan "untung" Rp 7. Itu sisa pembulatan,
  // bukan laba, dan menandainya oranye "perhatikan" membuat acara patungan yang
  // memang disengaja terbaca seperti ada yang salah. Ambangnya tetap numerik:
  // kelebihan di bawah Rp 1 per peserta = impas.
  const impas = profit >= 0 && profit < n;
  // Ambang bermakna, bukan selera: rugi itu bad, margin tipis (<15%) perlu
  // diperhatikan, sisanya aman.
  const state = profit < 0 ? 'bad' : impas ? 'good' : margin < 15 ? 'warn' : 'good';

  host.innerHTML = '<div class="stat-grid" style="margin-top:12px">'
    + statTileHTML('Biaya total', rp(cost), hoursLabel)
    + statTileHTML('Pemasukan', rp(revenue), `${n} x ${rp(fee)}`)
    + (impas
      ? statTileHTML('Untung', 'Impas', `sisa pembulatan ${rp(profit)}`, 'good')
      : statTileHTML('Untung', rp(profit), `margin ${margin.toFixed(1)}%`, state))
    + statTileHTML('Titik impas', rpUp(perPlayer), 'fee minimal / peserta')
    + (minutes ? statTileHTML('Harga / menit main', rp(fee / minutes),
                              `${minutes} menit di lapangan`) : '')
    + '</div>';
}

async function runAnalyze() {
  renderSetupEconomics(null);
  if (players.length < 4) {
    $('analysis').innerHTML = '<div class="empty">Butuh minimal 4 peserta.</div>';
    return;
  }
  try {
    const d = await api('/api/analyze', buildPayload());
    const r = d.report;
    const box = el('div');
    const grid = el('div', 'stat-grid');

    const tile = statTile;
    const restCls = r.rest_ratio > 1 / 3 ? 'warn' : (r.rest_ratio > 0 ? '' : 'good');
    grid.appendChild(tile('Ronde', r.rounds, `${d.effective_round_minutes} mnt/ronde`));
    // Rata-rata seluruh peserta menggambarkan nol orang begitu ada babak
    // putra/putri: 20 putra + 4 putri dengan babak putra/putri/mixed memberi
    // "5.0" sementara para putra main 3 dan para putri 10. Kalau server bisa
    // memisahkannya, rentangnya yang ditampilkan - dan kelompoknya disebut di
    // baris satuan, karena angka tanpa konteks tidak berguna buat host.
    if (r.groups && r.groups.length) {
      const nilai = r.groups.map((g) => g.plays);
      grid.appendChild(tile('Main / orang',
        `${Math.min(...nilai)}-${Math.max(...nilai)}`,
        r.groups.map((g) => `${g.label} ${g.plays}`).join(' · '), 'warn'));
    } else {
      grid.appendChild(tile('Main / orang', r.avg_plays_per_player,
        `${r.playing_minutes_per_player} menit`));
    }
    // Jumlah duduk bisa berayun karena DUA sebab yang berbeda, dan sebabnya
    // ikut disebut - kalau tidak, host membaca "berayun antar babak" untuk acara
    // satu babak yang court-nya berkurang, lalu mencari babak yang tidak ada.
    //   babak: 4 putri cuma cukup untuk satu court, jadi babak putri
    //     mendudukkan lebih banyak orang daripada babak putra
    //   court berkurang: tempatnya yang menyusut, bukan yang berhak turun
    // Satu angka di situ selalu ujung yang paling lengang - dan host memakai
    // kartu ini untuk memutuskan berapa court disewa.
    if (r.byes_per_round_max) {
      const sebab = $('courts_drop').checked
        ? (r.groups ? ' · berayun antar babak & court' : ' · court berkurang')
        : ' · berayun antar babak';
      grid.appendChild(tile('Duduk / ronde',
        `${r.byes_per_round}-${r.byes_per_round_max}`,
        `${pct(r.rest_ratio)}-${pct(r.byes_per_round_max / r.n_players)}`
          + sebab, restCls));
    } else {
      grid.appendChild(tile('Duduk / ronde', r.byes_per_round,
        pct(r.rest_ratio), restCls));
    }
    grid.appendChild(tile('Partner unik', r.partner_unique_feasible ? 'Bisa' : 'Tidak',
      `maks ${r.max_unique_partner_rounds} ronde`, r.partner_unique_feasible ? 'good' : 'warn'));
    grid.appendChild(tile('Lawan unik', r.opponent_unique_feasible ? 'Bisa' : 'Tidak',
      `maks ${r.max_unique_opponent_rounds} ronde`, r.opponent_unique_feasible ? 'good' : 'warn'));
    box.appendChild(grid);

    if (d.issues.length) {
      const wrap = el('div', null, '');
      wrap.style.marginTop = '14px';
      d.issues.forEach((i) => {
        const n = el('div', 'issue ' + i.severity);
        n.innerHTML = `<div class="t">${esc(i.title)}</div><div class="d">${esc(i.detail)}</div>` +
          (i.fix ? `<div class="f">${esc(i.fix)}</div>` : '');
        wrap.appendChild(n);
      });
      box.appendChild(wrap);
    }
    $('analysis').innerHTML = '';
    $('analysis').appendChild(box);
    renderSetupEconomics(r);
  } catch (e) {
    $('analysis').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

['courts', 'duration', 'round_min', 'warmup', 'mode', 'tier_count', 'referees',
 'ballboys', 'court_price', 'fee', 'other_costs',
 'courts_after', 'courts_from_round']
  .forEach((id) => $(id).addEventListener('input', scheduleAnalyze));

// Jumlah ronde berubah begitu durasi atau menit per ronde diubah, dan bersama
// itu berubah pula apakah mode CP-SAT masih ada gunanya.
['duration', 'round_min', 'warmup']
  .forEach((id) => $(id).addEventListener('input', renderCpsatRonde));

$('mode').addEventListener('change', () => {
  $('tier-row').style.display = $('mode').value === 'tiered' ? '' : 'none';
  $('cpsat-block').style.display = $('mode').value === 'americano_cpsat' ? '' : 'none';
  renderCpsatRonde();
});

/** Ronde tempat solver eksak berhenti membantu. Diukur, bukan ditebak. */
const CPSAT_RONDE_MAX = 11;

/**
 * Katakan di muka kalau acara ini terlalu panjang untuk solver.
 *
 * Yang menentukan berguna-tidaknya mode CP-SAT bukan jumlah peserta melainkan
 * jumlah RONDE - ukuran modelnya tumbuh dengan peserta x ronde x court, dan
 * ronde yang paling cepat membunuhnya. Diukur pada 4 court dengan roster nyata:
 * di 9 ronde solver menembus seluruh rentang sampai 26 peserta, di 12 ronde ia
 * mati total bahkan pada 18 peserta.
 *
 * Tanpa kalimat ini host menunggu satu batas waktu penuh di acara 3 jam lalu
 * mendapat jadwal yang sama persis dengan Americano - dan tidak punya cara tahu
 * bahwa itu memang sudah bisa diramalkan sebelum ia menekan Generate.
 */
function renderCpsatRonde() {
  const box = $('cpsat-ronde-hint');
  if (!box) return;
  if ($('mode').value !== 'americano_cpsat') { box.textContent = ''; return; }
  const ronde = rondeMuat();
  if (!ronde) { box.textContent = ''; return; }
  box.textContent = ronde > CPSAT_RONDE_MAX
    ? `Acara ini ${ronde} ronde. Di atas ${CPSAT_RONDE_MAX} ronde solver hampir `
      + `tidak pernah menemukan perbaikan maupun sempat membuktikan apa pun, `
      + `jadi Anda menunggu tanpa dapat apa-apa - pakai Americano biasa. `
      + `Perpanjang menit per ronde kalau mau turun ke ${CPSAT_RONDE_MAX} ronde.`
    : `Acara ini ${ronde} ronde - di dalam jangkauan solver.`;
}

// Apakah server membawa OR-Tools. Dipakai dua tempat: mode CP-SAT di bawah, dan
// tawaran "Sempurnakan jadwal ini" - keduanya memakai solver yang sama.
let cpsatAda = false;

/**
 * Tampilkan mode CP-SAT hanya kalau OR-Tools benar-benar ada di server.
 *
 * Modenya ditandai `hidden` di HTML dan baru dibuka di sini. Menawarkan mode
 * yang pasti gagal berarti host mengisi seluruh formulir lebih dulu, menekan
 * Generate, lalu baru diberi tahu - dan pada titik itu ia tidak punya cara tahu
 * bahwa yang kurang adalah sebuah paket Python.
 */
function applyCpsatAvailability(ada) {
  cpsatAda = !!ada;
  const opt = $('mode').querySelector('option[value="americano_cpsat"]');
  if (!opt) return;
  opt.hidden = !ada;
  if (!ada && $('mode').value === 'americano_cpsat') {
    $('mode').value = 'americano';
    $('cpsat-block').style.display = 'none';
  }
}

/**
 * Terjemahkan "sisa 1 court mulai ronde 11" jadi kalimat yang bisa diperiksa
 * host: blok rondenya, jam sewa yang dibayar, dan slot main yang tersisa.
 *
 * Ada alasannya kenapa ini kalimat dan bukan cuma dua kotak angka: dua kotak itu
 * tidak memperlihatkan bahwa mengurangi court memotong jatah main semua orang.
 * Host membatasi court demi margin, jadi yang ia butuh lihat bersamaan adalah
 * penghematannya DAN harganya.
 */
function renderCourtDrop() {
  const on = $('courts_drop').checked;
  $('courts-drop-row').style.display = on ? '' : 'none';
  const box = $('courts-drop-hint');
  if (!on) { box.textContent = ''; return; }

  const ronde = rondeMuat();
  const p = courtDropPayload();
  const courts = +$('courts').value || 1;
  const per = +$('round_min').value || 0;
  const n = players.length;

  if (ronde < 2) {
    box.textContent = 'Jam sewa ini cuma memuat ' + ronde
      + ' ronde - belum ada ronde kedua untuk mengurangi court.';
    return;
  }
  const sisaRonde = ronde - p.courts_from_round + 1;
  const menitAwal = (+$('warmup').value || 0) + (p.courts_from_round - 1) * per;
  const jam = (courts * menitAwal
    + p.courts_after * Math.max(0, (+$('duration').value || 0) - menitAwal)) / 60;
  const hemat = (courts * ((+$('duration').value || 0) / 60) - jam)
    * (+$('court_price').value || 0);

  const bit = [
    `Ronde 1-${p.courts_from_round - 1} pakai ${courts} court, `
      + `ronde ${p.courts_from_round}-${ronde} pakai ${p.courts_after} court `
      + `(${sisaRonde} ronde).`,
    `Sewa jadi ${(+jam.toFixed(2))} court-jam`
      + (hemat > 0 ? `, hemat ${rp(hemat)}.` : '.'),
  ];
  if (n >= 4) {
    const slot = 4 * (Math.min(courts, Math.floor(n / 4))
      * (p.courts_from_round - 1)
      + Math.min(p.courts_after, Math.floor(n / 4)) * sisaRonde);
    bit.push(`Slot main ${slot} untuk ${n} peserta `
      + `= rata-rata ${(slot / n).toFixed(1)} ronde main per orang.`);
  }
  box.textContent = bit.join('\n');
}

['courts_drop', 'courts_after', 'courts_from_round', 'courts',
 'duration', 'round_min', 'warmup', 'court_price']
  .forEach((id) => $(id).addEventListener('input', renderCourtDrop));
renderCourtDrop();

// ---------------------------------------------------------------------------
// Generate
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Generate dengan log kemajuan
// ---------------------------------------------------------------------------
function logLine(text, cls) {
  const box = $('prog-log');
  const line = el('div');
  const t = el('span', 't');
  const d = new Date();
  t.textContent = `${String(d.getHours()).padStart(2, '0')}:`
    + `${String(d.getMinutes()).padStart(2, '0')}:`
    + `${String(d.getSeconds()).padStart(2, '0')}`;
  const msg = el('span', cls || '');
  msg.textContent = text;                 // pesan server, jangan lewat innerHTML
  line.append(t, msg);
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function setProgress(pct, stage) {
  $('prog-fill').style.width = Math.max(0, Math.min(100, pct)) + '%';
  $('prog-pct').textContent = Math.round(pct) + '%';
  if (stage) $('prog-stage').textContent = stage;
}

/**
 * Baca Server-Sent Events dari respons streaming.
 *
 * EventSource hanya bisa GET, sedangkan payload peserta terlalu besar untuk
 * query string - jadi framenya diurai sendiri dari body fetch.
 */
async function streamSSE(path, payload, onEvent) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    // Frame SSE dipisahkan baris kosong; tiap frame berisi baris "event:"
    // dan "data:".
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = 'message', data = '';
      frame.split('\n').forEach((line) => {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      });
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

/**
 * Susun jadwal lewat server, lalu tampilkan. Dipakai dua tombol:
 * "Generate" (tanpa argumen) dan "Sempurnakan jadwal ini" (dengan anggaran
 * penyempurnaan). Keduanya mengirim setup yang sama persis - itu yang membuat
 * penyempurnaan mendarat di jadwal yang sama sebelum solver mulai, karena
 * seluruh rangkaian penjadwalan deterministik dari seed.
 */
async function jalankanGenerate(opsi) {
  const o = opsi || {};
  const btn = $(o.tombol || 'generate');
  const labelAsli = btn.textContent;
  btn.disabled = true; btn.textContent = o.sedang || 'Menghitung...';
  $('gen-msg').innerHTML = '';
  $('prog-log').textContent = '';
  $('gen-progress').style.display = '';
  setProgress(0, o.awal || 'Mengirim data ke generator');

  let failed = null;
  try {
    await streamSSE('/api/schedule/stream', buildPayload(o.payload), (event, data) => {
      if (event === 'progress') {
        setProgress(data.pct, data.message);
        logLine(`${String(data.pct).padStart(5)}%  ${data.message}`);
      } else if (event === 'done') {
        schedule = data;
        scheduleStamp = schedulingStamp();
        // currentEventId TIDAK direset di sini. Dulu direset, jadi "buka dari
        // riwayat -> ubah sedikit -> Buat jadwal -> Simpan" diam-diam membuat
        // acara baru alih-alih memperbarui yang dibuka, dan riwayat host penuh
        // salinan hampir kembar. Kalau memang ingin salinan, ada tombol
        // "Simpan sebagai baru" yang menyatakan maksud itu secara eksplisit.
        setProgress(100, `Selesai dalam ${data.elapsed} detik`);
        logLine(`Selesai dalam ${data.elapsed} detik`, 'ok');
      } else if (event === 'error') {
        failed = data.error;
        logLine(data.error, 'err');
      }
    });
    if (failed) throw new Error(failed);
    if (!schedule) throw new Error('Server tidak mengirim jadwal.');

    document.querySelector('.tabs button[data-view="jadwal"]').click();
    renderSchedule();
    toast(o.selesai || 'Jadwal siap');
  } catch (e) {
    $('gen-msg').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    logLine(e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = labelAsli;
  }
}

$('generate').onclick = () => jalankanGenerate();

/**
 * Tawaran "Sempurnakan jadwal ini".
 *
 * Ditawarkan kalau salah satu dari dua hal ini masih di atas batasnya, dan
 * keduanya ambang numerik - bukan selera:
 *
 *   giliran : tunggu terpanjang > batas yang tak terhindarkan (ambang yang sama
 *             yang membuat kartu "Tunggu terpanjang" kuning). Terukur memberi
 *             +2,3 sampai +3,8 poin dalam 7-22 detik.
 *   lawan   : ada pasangan yang berhadapan lebih dari sekali, DAN jadwalnya
 *             belum di batas bawah teoretis pengulangan. Syarat kedua penting:
 *             di 16 orang / 4 court yang pengulangannya memang wajib,
 *             penyempurnaan berhenti dalam 2,2 detik tanpa mencoba apa pun.
 *
 * Syarat lawan ditambahkan setelah klaim di sini terbukti SALAH. Dulu tertulis
 * "pada setup yang tandanya mati ia tidak pernah menemukan apa pun", dan itu
 * cuma benar untuk enam setup yang kebetulan disapu waktu itu. Diukur ulang
 * pada kasus yang gilirannya sudah rapi: 12 orang / 2 court naik 92,6 -> 94,6
 * dan 93,8 -> 94,8, dan mexicano 16 turun dari 53 ke 50 pasang lawan berulang
 * (99,0 -> 99,5). Empat dari dua belas kasus membaik padahal tombolnya tidak
 * pernah ditawarkan.
 *
 * Juga butuh OR-Tools di server. Kalau paket itu tidak ada, tombolnya tidak
 * ditampilkan sama sekali daripada gagal setelah ditekan.
 */
const LNS_DETIK = 20;

function renderPenyempurnaan(st) {
  const box = $('lns-box');
  if (!box) return;
  box.innerHTML = '';
  if (!cpsatAda || st.longest_wait === undefined) return;
  const giliran = st.longest_wait > st.wait_floor;
  const lawan = st.opponent_repeat_pairs > 0 && !st.at_theoretical_floor;
  if (!giliran && !lawan) return;

  const wrap = el('div', 'issue info');
  wrap.appendChild(el('div', 't', giliran
    ? 'Masih ada giliran yang bisa dirapikan'
    : 'Masih ada lawan berulang yang mungkin bisa dikurangi'));
  // Penjelasannya menyebut yang sedang dikejar, dan angkanya. Host yang membaca
  // "giliran" lalu melihat lawan berulang yang berubah - atau sebaliknya - akan
  // menyangka tombolnya mengerjakan hal lain daripada yang dijanjikan.
  const bagian = [];
  if (giliran) {
    bagian.push(`Ada peserta yang duduk ${st.longest_wait} ronde beruntun, `
      + `sementara pembagian paling merata untuk jumlah mainnya cuma menuntut `
      + `${st.wait_floor} ronde.`);
  }
  if (lawan) {
    bagian.push(`Ada ${st.opponent_repeat_pairs} pasang yang berhadapan lebih `
      + `dari sekali, dan jadwal ini belum menyentuh batas bawah pengulangan - `
      + `jadi mungkin masih bisa dikurangi.`);
  }
  bagian.push('Solver eksak bisa menyusun ulang tiga ronde sekaligus - '
    + 'jangkauan yang tidak dimiliki pertukaran biasa.');
  wrap.appendChild(el('div', 'd', bagian.join(' ')));
  const saran = el('div', 'f');
  // Tanpa angka di labelnya, dan itu disengaja. Angka 20 detik adalah anggaran
  // TAHAP PENYEMPURNAAN, bukan lama host menunggu: menekan tombol menyusun ulang
  // jadwalnya lebih dulu, dan biaya itu tumbuh dengan ukuran acara. Diukur dari
  // penekanan sampai selesai - 18,9 detik pada 26 orang / 4 court (7,4 untuk
  // penyempurnaan, sisanya menyusun ulang) dan 41,9 detik pada 60 orang / 6
  // court (14,4 untuk penyempurnaan). Menaruh "maks 20 detik" di tombol berarti
  // menjanjikan angka yang dilanggar sendiri di acara besar.
  const tombol = el('button', 'btn ghost sm', 'Sempurnakan jadwal ini');
  tombol.id = 'lns-run';
  tombol.onclick = () => jalankanGenerate({
    tombol: 'lns-run',
    payload: { lns_seconds: LNS_DETIK },
    sedang: 'Menyempurnakan...',
    awal: 'Menyusun jadwal yang sama, lalu menyempurnakannya',
    selesai: 'Penyempurnaan selesai',
  });
  saran.appendChild(tombol);
  // Kalimat ini pernah berbunyi "jadwalnya tidak akan jadi lebih buruk", dan itu
  // menyesatkan ke arah yang paling merugikan: yang dijaga adalah URUTANNYA,
  // dan urutan itu menaruh keunikan lawan di atas skor kualitas. Terukur, satu
  // jadwal berpindah dari 18 ke 14 pasang lawan berulang dengan skor kualitas
  // turun 90,5 -> 89,6. Host yang membaca janji lama lalu melihat angka
  // kualitas turun akan menyimpulkan tombolnya rusak, padahal app sedang
  // melakukan pertukaran yang dianutnya di semua tempat lain.
  saran.appendChild(el('span', 'hint',
    `Jadwalnya disusun ulang dulu, lalu disempurnakan dengan anggaran `
    + `${LNS_DETIK} detik - jadi menunggunya kira-kira selama Generate ditambah `
    + `itu. Yang dijaga urutannya: partner berulang, lalu lawan berulang, baru `
    + `skor kualitas, jadi skor kualitas bisa turun sedikit kalau lawan `
    + `berulangnya ikut berkurang - itu pertukaran yang disengaja.`));
  wrap.appendChild(saran);
  box.appendChild(wrap);
}

function renderSchedule() {
  if (!schedule) return;
  // Nama court dibaca dari jadwal yang tampil, bukan dari sisa isian
  // sebelumnya: jadwal yang dibuka dari riwayat membawa namanya sendiri, dan
  // itu yang harus muncul di kartu ronde maupun di kotak isiannya.
  courtNames = ((schedule.config || {}).court_names || []).slice();
  playerById = new Map(schedule.players.map((p) => [p.id, p]));
  const st = schedule.stats;
  const plays = Object.values(st.plays_per_player);
  const showGender = schedule.players.some((p) => p.gender);

  const grid = el('div', 'stat-grid');
  const tile = statTile;
  const q = st.quality_score;
  grid.appendChild(tile('Kualitas', q, 'dari 100', q >= 85 ? 'good' : q >= 65 ? 'warn' : 'bad'));
  grid.appendChild(tile('Ronde', schedule.rounds.length, `${schedule.config.round_minutes} mnt`));
  grid.appendChild(tile('Main / orang', `${Math.min(...plays)}-${Math.max(...plays)}`, 'ronde'));
  grid.appendChild(tile('Partner ulang', st.partner_repeat_pairs, 'pasang',
    st.partner_repeat_pairs ? 'warn' : 'good'));
  grid.appendChild(tile('Lawan ulang', st.opponent_repeat_pairs, 'pasang',
    st.opponent_repeat_pairs ? 'warn' : 'good'));
  // Duduk beruntun tanpa status warna. Angkanya berguna, tapi ambang "lebih
  // dari nol berarti buruk" tidak: dengan 1 court dan 10 peserta, jadwal
  // terbaik yang mungkin pun punya puluhan kejadian duduk beruntun, jadi
  // kartunya akan selalu kuning dan berhenti memberi tahu apa pun.
  grid.appendChild(tile('Duduk beruntun', st.back_to_back_byes, 'kejadian'));
  // Dua kartu di bawah ini punya ambang numerik yang sungguhan.
  //
  // Tunggu terpanjang dibandingkan dengan batas yang memang tak terhindarkan:
  // peserta yang main m dari R ronde punya R-m ronde duduk untuk dibagi ke
  // paling banyak m+1 sela. Sama dengan batas = sudah sebaik yang mungkin.
  if (st.longest_wait !== undefined) {
    grid.appendChild(tile('Tunggu terpanjang', st.longest_wait,
      `batas ${st.wait_floor} ronde`,
      st.longest_wait <= st.wait_floor ? 'good' : 'warn'));
    // Giliran terlewat: berapa kali seseorang turun lagi padahal ada peserta
    // lain yang sedang duduk dan belum kebagian putaran yang sama.
    grid.appendChild(tile('Giliran terlewat', st.turn_skips, 'kali',
      st.turn_skips ? 'warn' : 'good'));
  }
  $('sched-stats').innerHTML = '';
  $('sched-stats').appendChild(grid);
  renderPenyempurnaan(st);

  renderCourtNames();
  renderRounds();

  // Rekap
  // Kolom dibuat ADITIF: main + wasit + ballboy + istirahat = jumlah ronde.
  // Sebelumnya "Duduk" menghitung semua ronde tidak main termasuk ronde saat
  // orangnya bertugas, jadi angkanya tidak bisa dijumlah dan menyesatkan -
  // seseorang yang 3 kali jadi wasit tetap tercatat "duduk" 3 kali itu.
  const showRoles = schedule.config.referees_per_court || schedule.config.ballboys_per_court;
  // Kolom L/P ikut ditampilkan supaya warna nama di susunan pertandingan punya
  // padanan berupa HURUF di halaman yang sama - warna saja tidak cukup, dan
  // mengirim orang ke tab Peserta hanya untuk memastikan itu memutus alurnya.
  let html = '<table class="data"><thead><tr><th>Nama</th>'
    + (showGender ? '<th class="num">L/P</th>' : '')
    + '<th class="num">Main</th>'
    + (showRoles ? '<th class="num">Wasit</th><th class="num">Ballboy</th>' : '')
    + '<th class="num">Istirahat</th></tr></thead><tbody>';
  schedule.players.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach((p) => {
    const roles = st.roles_per_player[p.id] || {};
    const idle = (st.byes_per_player[p.id] || 0) - (roles.total || 0);
    const gp = p.gender === 'M' ? '<span class="gp m">L</span>'
      : p.gender === 'F' ? '<span class="gp f">P</span>' : '-';
    html += `<tr><td>${gname(p.id)}</td>`
      + (showGender ? `<td class="num">${gp}</td>` : '')
      + `<td class="num">${st.plays_per_player[p.id] || 0}</td>`
      + (showRoles ? `<td class="num">${roles.wasit || 0}</td>`
                     + `<td class="num">${roles.ballboy || 0}</td>` : '')
      + `<td class="num">${Math.max(0, idle)}</td></tr>`;
  });
  $('recap').innerHTML = html + '</tbody></table>'
    + roleTimeline(schedule, showRoles);

  // Grafik komposisi ronde: mencari ketimpangan di antara 26 orang jauh
  // lebih cepat lewat batang daripada lewat tabel 26 baris.
  hideTip();
  const host = $('engagement');
  host.textContent = '';
  // Lebar wadah dibaca saat ini juga; tab sudah ditampilkan lebih dulu supaya
  // clientWidth-nya nyata, bukan nol.
  host.appendChild(engagementChart(schedule.players, st,
                                   schedule.rounds.length, host.clientWidth));

  // Catatan
  let notes = '';
  (schedule.notes || []).forEach((n) => { notes += `<div class="issue info"><div class="d">${esc(n)}</div></div>`; });
  (schedule.violations || []).forEach((v) => { notes += `<div class="issue warning"><div class="d">${esc(v.reason)}</div></div>`; });
  $('notes').innerHTML = notes || '<div class="empty">Tidak ada catatan.</div>';

  renderMatrix();
}

/**
 * Kartu ronde. Dipisah dari renderSchedule supaya mengganti nama court cukup
 * menggambar ulang bagian ini - kartu statistik, rekap, grafik keterlibatan,
 * dan matriks tidak berubah sedikit pun oleh sebuah label, dan menggambar
 * ulang semuanya berarti kotak isian nama kehilangan fokus di tengah ketik.
 */
function renderRounds() {
  const showGender = schedule.players.some((p) => p.gender);
  // Ronde. Card disusun grid, jumlah kolom mengikuti isi tiap card: dengan
  // 1 court satu card hanya memuat satu match, jadi kolom tunggal membuang
  // lebar panel dan memaksa scroll berkali-kali lipat.
  const maxMatches = schedule.rounds.reduce(
    (t, r) => Math.max(t, r.matches.length), 1);
  // Jumlah kolom TIDAK diturunkan lagi mengikuti banyaknya match. Card dengan
  // 4 match tidak butuh card yang lebih LEBAR - ia hanya lebih tinggi. Dulu
  // 3 court atau lebih jatuh ke satu kolom, dan itu membatalkan pemadatan:
  // card selebar halaman membuat kolom tim (1fr) melar, sehingga nama kedua tim
  // terlempar ke ujung kiri dan kanan dengan "vs" terdampar di tengah - mata
  // harus menyeberangi ruang kosong untuk membaca satu pertandingan.
  const cols = maxMatches === 1 ? 3 : 2;
  const box = el('div', `rounds cols-${cols}`);
  // Lebar kolom court dikunci di sini, bukan dibiarkan melar mengikuti isi:
  // dengan lebar otomatis, "C1" dan "Indoor A" di kartu yang sama menggeser
  // kolom tim baris demi baris, dan mata kehilangan garis lurus untuk
  // menyusuri lawan.
  box.style.setProperty('--courtw', `${courtColWidth()}px`);

  let seg = null;
  schedule.rounds.forEach((r) => {
    if (r.segment && r.segment !== seg) {
      seg = r.segment;
      box.appendChild(el('div', 'segbar', esc(seg)));
    }
    const card = el('div', 'round');
    const time = schedule.config.warmup_minutes !== undefined
      ? `+${r.start_min}m` : '';
    card.appendChild(el('div', 'round-head',
      `<span class="n">R${r.index}</span><span class="t">${esc(time)}</span>`));

    r.matches.forEach((m) => {
      // Peran disingkat W/B: di card sempit nama lengkap "wasit"/"ballboy"
      // memakan ruang yang dibutuhkan namanya sendiri.
      const duty = [];
      (r.roles || []).forEach((x) => {
        if (x.court !== m.court) return;
        duty.push(`${x.role === 'wasit' ? 'W' : 'B'} ${gname(x.player_id)}`);
      });
      const pool = m.pool ? `<span class="pool">${esc(m.pool)}</span>` : '';
      const team = (t) => t.map((x) => gname(x.id)).join(' &amp; ');
      card.appendChild(el('div', 'match',
        `<span class="c">${esc(courtLabel(m.court))}</span>` +
        `<span class="tm">${team(m.team_a)}${pool}</span>` +
        `<span class="vs">vs</span>` +
        `<span class="tm b">${team(m.team_b)}</span>` +
        `<span class="duty">${duty.join(' · ')}</span>`));
    });

    const busy = new Set((r.roles || []).map((x) => x.player_id));
    const idle = r.byes.filter((b) => !busy.has(b.id));
    if (idle.length) {
      card.appendChild(el('div', 'resting',
        'Istirahat: ' + idle.map((b) => gname(b.id)).join(', ')));
    }
    box.appendChild(card);
  });
  $('rounds').innerHTML = '';
  // Legenda hanya muncul kalau gendernya memang terisi. Roster tanpa L/P
  // menghasilkan nama netral semua, dan menjelaskan warna yang tidak ada di
  // mana pun cuma bikin bingung.
  if (showGender) {
    $('rounds').appendChild(el('div', 'gkey',
      '<span><b class="g-m">&#9679; Nama biru</b> laki-laki</span>'
      + '<span><b class="g-f">&#9679; Nama pink</b> perempuan</span>'));
  }
  $('rounds').appendChild(box);
}

/** Court yang benar-benar bermain di jadwal ini, urut naik.
 *
 * Bukan config.courts: court yang dilepas di tengah acara tetap tercatat di
 * setup, tapi tidak punya satu pun match untuk diberi nama - menawarkan kotak
 * isian untuknya berarti host menamai court yang tidak muncul di mana pun.
 */
function courtsInSchedule() {
  const set = new Set();
  (schedule?.rounds || []).forEach(
    (r) => (r.matches || []).forEach((m) => set.add(m.court)));
  return [...set].sort((a, b) => a - b);
}

/** Nama tampilan satu court: pilihan host, atau "C1" kalau belum diganti. */
function courtLabel(n) {
  return (courtNames[n - 1] || '').trim() || `C${n}`;
}

/** Lebar kolom court (px) supaya nama terpanjang muat tanpa membuat kolom tim
 *  bergoyang antar baris. 24px = lebar lama, cukup untuk "C1".."C9". */
function courtColWidth() {
  const panjang = courtsInSchedule()
    .reduce((t, c) => Math.max(t, courtLabel(c).length), 2);
  return Math.max(24, Math.min(96, Math.round(panjang * 6.4) + 2));
}

/**
 * Kotak isian nama court, satu per court yang bermain.
 *
 * Nama disimpan di schedule.config.court_names, bukan cuma di variabel modul,
 * karena di situlah tempatnya ikut serta ke tiga tujuan sekaligus: laporan
 * cetak dan tombol Simpan mengirim objek `schedule` apa adanya, dan endpoint
 * yang menulis ulang teks WhatsApp juga membacanya dari sana.
 */
function renderCourtNames() {
  const host = $('court-names');
  if (!host) return;
  const courts = courtsInSchedule();
  if (!courts.length) { host.innerHTML = ''; return; }

  host.innerHTML = '<div class="cn-h">Nama court '
    + `<span class="hint">kosongkan untuk kembali ke C1, C2, ...; `
    + `maksimal ${courtNameMax} huruf. Nama ikut ke teks WhatsApp, CSV, `
    + `dan laporan cetak - tekan Simpan lagi supaya ikut tersimpan.</span>`
    + '</div><div class="cn-row">'
    + courts.map((c) => `<label>C${c}`
      + `<input type="text" data-court="${c}" maxlength="${courtNameMax}" `
      + `placeholder="C${c}" value="${esc(courtNames[c - 1] || '')}"></label>`)
      .join('')
    + '</div>';

  host.querySelectorAll('input[data-court]').forEach((inp) => {
    const idx = +inp.dataset.court - 1;
    // Mengetik cuma menggambar ulang kartu ronde - murah, dan host melihat
    // namanya mendarat di tempatnya sambil mengetik.
    inp.oninput = () => {
      courtNames[idx] = inp.value;
      simpanNamaCourt();
      renderRounds();
    };
    // Teks WhatsApp, jadwal per pemain, dan CSV lahir di server, jadi ia
    // disegarkan saat isian ditinggalkan - bukan tiap huruf.
    inp.onchange = () => { simpanNamaCourt(); refreshScheduleTexts(); };
  });
}

/** Tempelkan nama court ke jadwal yang sedang tampil. */
function simpanNamaCourt() {
  if (!schedule) return;
  // Entri kosong di ekor dibuang supaya jadwal yang namanya tidak pernah
  // diganti tersimpan persis seperti jadwal lama: daftar kosong.
  // Array.from, bukan map: kalau host menamai court 3 lebih dulu, indeks 0
  // dan 1 masih lubang - dan lubang tetap lubang di hasil map, lalu
  // JSON.stringify mengubahnya jadi null di tengah daftar nama.
  const bersih = Array.from(courtNames, (n) => (n || '').trim());
  while (bersih.length && !bersih[bersih.length - 1]) bersih.pop();
  courtNames = bersih;
  schedule.config.court_names = bersih.slice();
}

/**
 * Tulis ulang teks WhatsApp, jadwal per pemain, dan CSV di server.
 *
 * Ketiganya dibuat server saat jadwal jadi, jadi mengganti nama court tanpa
 * ini akan menyalin teks yang masih berbunyi "C1" padahal layar sudah
 * berbunyi lain. Jadwal yang tampil dikirim apa adanya - server cuma menulis
 * ulang teksnya, tidak menjadwal ulang apa pun.
 */
async function refreshScheduleTexts() {
  if (!schedule) return;
  try {
    const d = await api('/api/schedule/text', { ...buildPayload(), schedule });
    schedule.text = d.text;
    schedule.personal_text = d.personal_text;
    schedule.csv = d.csv;
  } catch (e) {
    toast(`Nama court belum masuk ke teks salinan: ${e.message}`);
  }
}

// Peta id -> peserta untuk jadwal yang sedang ditampilkan. Dulu tiap nama
// dicari dengan find() linear; sekarang nama dipanggil sekali per orang per
// match, jadi roster 40 orang dengan 12 ronde x 4 court berarti ribuan
// penelusuran hanya untuk menggambar satu tab.
let playerById = new Map();

/** Nama peserta sebagai HTML, diwarnai menurut gendernya.
 *
 * Yang diberi kelas hanya nama itu sendiri - pemisah "&" dan awalan tugas
 * "W"/"B" ditulis di luar span supaya tetap netral. */
function gname(id) {
  const p = playerById.get(id);
  const nm = esc(p ? p.name : '?');
  const cls = !p ? '' : p.gender === 'M' ? 'g-m' : p.gender === 'F' ? 'g-f' : '';
  return cls ? `<span class="${cls}">${nm}</span>` : nm;
}

// ---------------------------------------------------------------------------
// Susunan per ronde
// ---------------------------------------------------------------------------

/** Tabel peran tiap orang di tiap ronde: M main, W wasit, B ballboy, R istirahat.
 *
 * Angka rekap menjawab "berapa kali", bukan "kapan". Dua orang sama-sama main
 * 9 dari 13 ronde bisa punya pengalaman yang jauh berbeda kalau yang satu
 * duduk berturut-turut di ronde 3-4-5 dan yang lain duduknya tersebar - dan
 * itu hanya kelihatan kalau urutannya digambar.
 *
 * Hurufnya selalu tercetak di dalam sel. Warna cuma mempercepat pemindaian;
 * baris tetap terbaca penuh tanpanya.
 */
function roleTimeline(schedule, showRoles) {
  const rounds = schedule.rounds || [];
  if (!rounds.length) return '';

  // Satu peta per ronde. Dibangun sekali di sini, bukan dicari ulang per sel:
  // 26 orang x 13 ronde berarti 338 sel, dan tiap sel kalau ditelusuri
  // linear harus menyisir seluruh match plus daftar tugas ronde itu.
  const perRound = rounds.map((r) => {
    const m = new Map();
    (r.matches || []).forEach((mt) => {
      mt.team_a.concat(mt.team_b).forEach((x) => m.set(x.id, 'm'));
    });
    (r.roles || []).forEach((x) => {
      m.set(x.player_id, x.role === 'wasit' ? 'w' : 'b');
    });
    (r.byes || []).forEach((b) => { if (!m.has(b.id)) m.set(b.id, 'r'); });
    return m;
  });

  const LABEL = { m: 'M', w: 'W', b: 'B', r: 'R' };
  const NAMA = { m: 'main', w: 'wasit', b: 'ballboy', r: 'istirahat' };

  const kunci = [['m', 'Main'], ['r', 'Istirahat']];
  if (showRoles) kunci.splice(1, 0, ['w', 'Wasit'], ['b', 'Ballboy']);
  let out = `<h3 class="tl-h">Susunan per ronde `
    + `<span class="hint">ronde 1 &rarr; ${rounds.length}, kiri ke kanan</span></h3>`
    + '<div class="mx-legend">'
    + kunci.map(([k, t]) =>
      `<span class="mx-legend-item"><span class="tl-c ${k}">${LABEL[k]}</span>`
      + `${t}</span>`).join('')
    + '</div><div class="mx-wrap"><table class="data mx tl"><thead><tr>'
    + '<th class="mx-corner mx-row">Nama</th>'
    + rounds.map((r) => `<th class="num">${r.index}</th>`).join('')
    + '</tr></thead><tbody>';

  schedule.players.slice().sort((a, b) => a.name.localeCompare(b.name))
    .forEach((p) => {
      out += `<tr><th class="mx-row">${gname(p.id)}</th>`;
      perRound.forEach((m, i) => {
        const k = m.get(p.id);
        out += k
          ? `<td class="num"><span class="tl-c ${k}" `
            + `title="Ronde ${rounds[i].index}: ${NAMA[k]}">${LABEL[k]}</span></td>`
          : '<td class="num tl-none">&middot;</td>';
      });
      out += '</tr>';
    });
  return out + '</tbody></table></div>';
}

// ---------------------------------------------------------------------------
// Matriks pertemuan
// ---------------------------------------------------------------------------

/** Hitung berapa kali tiap pasang orang jadi partner dan jadi lawan.
 *
 * Dihitung dari `schedule.rounds`, bukan dari statistik server, supaya jadwal
 * yang dibuka dari riwayat ikut terlayani - yang tersimpan di database memang
 * susunan rondenya.
 */
function meetingCounts() {
  const kunci = (a, b) => (a < b ? `${a}:${b}` : `${b}:${a}`);
  const partner = new Map();
  const lawan = new Map();
  const tambah = (m, a, b) => m.set(kunci(a, b), (m.get(kunci(a, b)) || 0) + 1);

  (schedule.rounds || []).forEach((r) => (r.matches || []).forEach((m) => {
    const A = m.team_a.map((x) => x.id);
    const B = m.team_b.map((x) => x.id);
    tambah(partner, A[0], A[1]);
    tambah(partner, B[0], B[1]);
    A.forEach((x) => B.forEach((y) => tambah(lawan, x, y)));
  }));
  return { partner, lawan, kunci };
}

function renderMatrix() {
  const host = $('matrix');
  if (!schedule || !schedule.players || schedule.players.length < 2) {
    host.innerHTML = '<div class="empty">Belum ada jadwal.</div>';
    return;
  }
  const { partner, lawan, kunci } = meetingCounts();
  const orang = schedule.players.slice().sort((a, b) => a.name.localeCompare(b.name));

  // Ambang bermakna, bukan selera: 0 = belum pernah bertemu, 1 = tepat sekali
  // (yang dikejar rotasi), 2+ = berulang. Angkanya sendiri yang menyampaikan
  // informasi; warna cuma menguatkan, jadi tetap terbaca tanpa warna.
  const kelas = (n) => (n === 0 ? 'm0' : n === 1 ? 'm1' : 'm2');

  // Kolom diberi NOMOR, bukan nama yang dipendekkan. Nama peserta sering
  // berbagi kata depan ("Pemain 1", "Pemain 2"; atau satu keluarga di klub
  // yang sama), sehingga label pendek jadi identik semua dan matriksnya tidak
  // terbaca sama sekali. Nomornya dicetak juga di depan nama tiap baris, jadi
  // memetakan kolom ke orang tinggal membaca ke kiri.
  const nomor = new Map(orang.map((p, i) => [p.id, i + 1]));

  function tabel(peta, judul) {
    let h = `<table class="data mx"><thead><tr><th class="mx-corner">${esc(judul)}</th>`;
    orang.forEach((p) => {
      h += `<th class="num mx-col" title="${esc(p.name)}">${nomor.get(p.id)}</th>`;
    });
    h += '</tr></thead><tbody>';
    orang.forEach((a) => {
      h += `<tr><th class="mx-row" title="${esc(a.name)}">`
        + `<span class="mx-no">${nomor.get(a.id)}</span>${gname(a.id)}</th>`;
      orang.forEach((b) => {
        if (a.id === b.id) { h += '<td class="num mx-self">-</td>'; return; }
        const n = peta.get(kunci(a.id, b.id)) || 0;
        h += `<td class="num ${kelas(n)}" title="${esc(a.name)} & ${esc(b.name)}: ${n}x">${n}</td>`;
      });
      h += '</tr>';
    });
    return h + '</tbody></table>';
  }

  // Ringkasan di atas tabel: pada 26 orang, memindai 676 sel untuk mencari
  // yang belum pernah bertemu itu pekerjaan yang seharusnya dikerjakan mesin.
  let belumPartner = 0, ulangPartner = 0, belumLawan = 0, ulangLawan = 0;
  for (let i = 0; i < orang.length; i++) {
    for (let j = i + 1; j < orang.length; j++) {
      const k = kunci(orang[i].id, orang[j].id);
      const p = partner.get(k) || 0, l = lawan.get(k) || 0;
      if (p === 0) belumPartner++; else if (p > 1) ulangPartner++;
      if (l === 0) belumLawan++; else if (l > 1) ulangLawan++;
    }
  }
  const total = (orang.length * (orang.length - 1)) / 2;

  // Berapa pertemuan yang MUAT di acara ini. Satu match memberi 2 pasang
  // partner (tim A dan tim B) dan 4 pasang lawan (tiap orang tim A melawan
  // tiap orang tim B); tidak ada jadwal yang bisa melampauinya.
  //
  // Tanpa angka ini, matriks penuh 0 terbaca seperti jadwal yang gagal -
  // padahal 26 orang punya 325 pasang sementara 13 ronde di 4 court cuma
  // memuat 208 pertemuan. Selisihnya disebut apa adanya supaya tidak ada yang
  // mengejar angka yang memang mustahil.
  const nMatch = (schedule.rounds || []).reduce(
    (t, r) => t + (r.matches || []).length, 0);
  const mustahil = (muat) => Math.max(0, total - muat);
  const ceilingBits = [];
  if (mustahil(nMatch * 2)) {
    ceilingBits.push(`${mustahil(nMatch * 2)} pasang mustahil berpartner`);
  }
  if (mustahil(nMatch * 4)) {
    ceilingBits.push(`${mustahil(nMatch * 4)} mustahil berhadapan`);
  }
  const ceiling = ceilingBits.length
    ? `<div class="mx-ceiling">${schedule.rounds.length} ronde `
      + `&times; ${schedule.config.courts} court `
      + `= ${nMatch} match, cukup untuk ${nMatch * 2} pasang partner dan `
      + `${nMatch * 4} pasang lawan. Jadi dari ${total} pasang, `
      + `${ceilingBits.join(' dan ')} - berapa pun bagusnya jadwalnya.</div>`
    : '';

  host.innerHTML =
    '<div class="viz-head">'
    + `<span>${total} pasang orang &middot; partner: ${belumPartner} belum pernah, `
    + `${ulangPartner} berulang &middot; lawan: ${belumLawan} belum pernah, `
    + `${ulangLawan} berulang</span>`
    + '<button class="viz-toggle" id="mx-toggle" type="button">Lihat lawan</button>'
    + '</div>'
    + ceiling
    + '<div class="mx-legend">'
    + '<span class="mx-legend-item"><span class="mx-chip m0">0</span> belum pernah</span>'
    + '<span class="mx-legend-item"><span class="mx-chip m1">1</span> tepat sekali</span>'
    + '<span class="mx-legend-item"><span class="mx-chip m2">2+</span> berulang</span>'
    + '</div>'
    + `<div class="mx-wrap" id="mx-partner">${tabel(partner, 'Partner')}</div>`
    + `<div class="mx-wrap" id="mx-lawan" style="display:none">${tabel(lawan, 'Lawan')}</div>`;

  $('mx-toggle').onclick = () => {
    const lihatLawan = $('mx-lawan').style.display === 'none';
    $('mx-lawan').style.display = lihatLawan ? '' : 'none';
    $('mx-partner').style.display = lihatLawan ? 'none' : '';
    $('mx-toggle').textContent = lihatLawan ? 'Lihat partner' : 'Lihat lawan';
  };
}

// ---------------------------------------------------------------------------
// Ekspor
// ---------------------------------------------------------------------------
function download(filename, content, type) {
  const blob = content instanceof Blob ? content : new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = el('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/**
 * Salinan setup lengkap untuk dilaporkan saat ada yang janggal.
 *
 * Nama peserta DIGANTI jadi P1..Pn. Yang dibutuhkan untuk mereproduksi masalah
 * penjadwalan hanyalah strukturnya - jumlah orang, gender, rating, pasangan
 * terkunci, dan setelan babak. Nama anggota klub itu data pribadi dan tidak
 * perlu ikut keluar dari mesin ini.
 */
function debugSnapshot() {
  const alias = new Map();
  players.forEach((p, i) => alias.set(p.id, `P${i + 1}`));

  const payload = buildPayload();
  const lines = [
    '--- INFO DEBUG PADELIN ---',
    // Court berkurang WAJIB ikut: ia mengubah jumlah match seluruh acara, jadi
    // jatah main, keunikan, dan tunggu terpanjang semuanya bergeser. Laporan
    // tanpa barisnya tidak bisa direproduksi - setupnya terbaca identik.
    `court=${payload.courts}${payload.courts_after
      ? ` (jadi ${payload.courts_after} dari ronde ${payload.courts_from_round})`
      : ''} durasi=${payload.duration_minutes}m `
      + `ronde=${payload.round_minutes}m pemanasan=${payload.warmup_minutes}m`,
    `mode=${payload.mode} pool_rating=${payload.tier_count} `
      + `wasit=${payload.referees_per_court} ballboy=${payload.ballboys_per_court}`,
    // percobaan ikut dicatat: jumlahnya mengubah hasil, jadi laporan tanpa
    // angka ini tidak bisa direproduksi.
    `seed=${payload.seed} effort=${payload.effort} `
      + `percobaan=${payload.attempts ?? 3} `
      + (payload.mode === 'americano_cpsat'
        ? `batas_solver=${payload.cpsat_seconds}s ` : '')
      + `selang_seling=${payload.interleave_segments}`,
    // Format yang diizinkan WAJIB ikut. Batasan ini menentukan siapa yang bisa
    // turun bareng, jadi ia mengubah kerataan main dan keunikan lawan sekaligus
    // - dan tanpa barisnya, laporan "lawan berulang" mustahil direproduksi:
    // setup yang kelihatan identik bisa berperilaku sama sekali berbeda.
    `format_diizinkan=[${payload.allowed_matchups === null
      ? 'semua' : payload.allowed_matchups.join(', ') || '(tidak ada)'}]`,
    `babak=[${payload.segments.map((s) => `${s.label}:${s.rounds}:${s.rule}`)
      .join(', ') || '(satu babak)'}]`,
    `peserta=${players.length} `
      + `(L${players.filter((p) => p.gender === 'M').length} `
      + `P${players.filter((p) => p.gender === 'F').length} `
      + `?${players.filter((p) => !p.gender).length})`,
    '',
    'peserta (nama disamarkan):',
  ];
  players.forEach((p) => {
    const bits = [alias.get(p.id), `rating=${p.rating}`, `g=${p.gender || '-'}`];
    if (p.partner_id !== null) bits.push(`partner=${alias.get(p.partner_id)}`);
    if (p.court_preference) bits.push(`minta=${p.court_preference}`);
    lines.push('  ' + bits.join(' '));
  });

  if (schedule) {
    const st = schedule.stats;
    lines.push('', 'hasil:',
      `  ronde=${schedule.rounds.length} kualitas=${st.quality_score}`,
      `  partner_ulang=${st.partner_repeat_pairs} `
        + `lawan_ulang=${st.opponent_repeat_pairs} `
        + `duduk_beruntun=${st.back_to_back_byes}`,
      `  main_per_orang=${Math.min(...Object.values(st.plays_per_player))}-`
        + `${Math.max(...Object.values(st.plays_per_player))}`,
      `  giliran_terlewat=${st.turn_skips} `
        + `tunggu_terpanjang=${st.longest_wait} (batas ${st.wait_floor}) `
        + `main_pertama_terakhir=R${st.last_first_play}`);
    lines.push('', 'jadwal:');
    schedule.rounds.forEach((r) => {
      r.matches.forEach((m) => {
        const a = m.team_a.map((x) => alias.get(x.id) || x.name).join('+');
        const bb = m.team_b.map((x) => alias.get(x.id) || x.name).join('+');
        lines.push(`  R${r.index} C${m.court} ${a} vs ${bb}`);
      });
    });
    if (schedule.notes && schedule.notes.length) {
      // Catatan bisa memuat nama peserta - "tidak kebagian main sama sekali:
      // Budi", "yang dilewati: Sari 2x". Info debug ini dibuat untuk DIBAGIKAN
      // saat melapor, jadi namanya harus ikut disamarkan seperti di tabel
      // peserta dan jadwal di atas; sebelumnya catatan disisipkan apa adanya
      // dan nama asli lolos ke teks yang disalin host.
      //
      // Diganti dari yang TERPANJANG dulu supaya nama yang kebetulan menjadi
      // bagian dari nama lain ("Ani" di dalam "Anisa") tidak memotongnya
      // duluan dan menyisakan potongan yang tidak tersamarkan.
      const samarkan = (teks) => [...players]
        .filter((p) => p.name)
        .sort((a, b) => b.name.length - a.name.length)
        .reduce((s, p) => s.split(p.name).join(alias.get(p.id)), teks);
      lines.push('', 'catatan:');
      schedule.notes.forEach((nt) => lines.push('  - ' + samarkan(nt)));
    }
  } else {
    lines.push('', '(jadwal belum dibuat)');
  }
  return lines.join('\n');
}

$('copy-debug').onclick = async () => {
  const text = debugSnapshot();
  // Teksnya ditampilkan juga, bukan hanya disalin: clipboard bisa ditolak
  // browser, dan kalau begitu host tidak punya jalan lain untuk mengambilnya.
  const box = $('debug-out');
  box.value = text;
  box.style.display = '';
  box.rows = Math.min(14, text.split('\n').length);
  try {
    await navigator.clipboard.writeText(text);
    toast('Info debug tersalin - nama peserta sudah disamarkan');
  } catch (e) {
    box.select();
    toast('Clipboard ditolak browser - teksnya sudah diseleksi, tekan Ctrl+C');
  }
};

$('copy-wa').onclick = async () => {
  if (!schedule) return toast('Belum ada jadwal');
  await navigator.clipboard.writeText(schedule.text);
  toast('Tersalin. Tinggal tempel di grup WA');
};

$('copy-personal').onclick = async () => {
  if (!schedule) return toast('Belum ada jadwal');
  await navigator.clipboard.writeText(schedule.personal_text);
  toast('Jadwal per pemain tersalin');
};

$('dl-csv').onclick = () => {
  if (!schedule) return toast('Belum ada jadwal');
  download('jadwal-padel.csv', '﻿' + schedule.csv, 'text/csv;charset=utf-8');
};

$('open-html').onclick = () => {
  if (!schedule) return toast('Belum ada jadwal');
  const form = el('form');
  form.method = 'POST'; form.action = '/api/report'; form.target = '_blank';
  const input = el('input');
  input.type = 'hidden'; input.name = 'payload';
  // Jadwal yang tampil ikut dikirim, sama seperti saat menyimpan. Dulu yang
  // dikirim cuma setup, jadi server menyusun ulang seluruh jadwal sebelum
  // mengirim satu byte pun - dan jendela laporan sudah telanjur terbuka putih
  // selama itu. Host yang menekan Ctrl+P di situ mencetak halaman kosong.
  input.value = JSON.stringify({ ...buildPayload(), schedule });
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
  form.remove();
};

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Format match yang diizinkan
// ---------------------------------------------------------------------------
// Daftarnya datang dari server (/api/presets) supaya tidak ada dua sumber
// kebenaran. Semua tercentang = tanpa batasan, dan itu yang dikirim sebagai
// null - jadi jadwal lama yang belum punya field ini tetap sama artinya.
let matchupTypes = [];

function renderMatchups(dipilih = null) {
  const host = $('matchups');
  if (!host || !matchupTypes.length) return;
  const aktif = dipilih === null ? null : new Set(dipilih);
  host.innerHTML = matchupTypes.map((m) => {
    const on = aktif === null || aktif.has(m.code);
    return `<label class="${on ? '' : 'off'}">`
      + `<input type="checkbox" data-matchup="${esc(m.code)}" ${on ? 'checked' : ''}>`
      + `<span>${esc(m.label)}</span></label>`;
  }).join('');
  host.querySelectorAll('input[data-matchup]').forEach((cb) => {
    cb.onchange = () => {
      // Nol format = tidak ada susunan yang sah sama sekali. Ditolak di sini
      // supaya host tidak menunggu generate hanya untuk menerima error.
      if (!host.querySelectorAll('input[data-matchup]:checked').length) {
        cb.checked = true;
        toast('Minimal satu format harus diizinkan');
        return;
      }
      cb.closest('label').classList.toggle('off', !cb.checked);
      renderMatchupNote();
      scheduleAnalyze();
    };
  });
  renderMatchupNote();
}

function selectedMatchups() {
  const cb = document.querySelectorAll('#matchups input[data-matchup]');
  if (!cb.length) return null;
  const on = [...cb].filter((x) => x.checked).map((x) => x.dataset.matchup);
  return on.length === cb.length ? null : on;   // semua = tanpa batasan
}

function renderMatchupNote() {
  const el2 = $('matchup-note');
  if (!el2) return;
  const on = selectedMatchups();
  el2.textContent = on === null
    ? 'Semua format boleh — tidak ada pembatasan.'
    : `${matchupTypes.length - on.length} format dilarang. Kalau susunan `
      + 'peserta tidak menyisakan lawan yang sah, jadwal tetap dibuat dan '
      + 'pelanggarannya disebut di catatan.';
}

/** Sidik jari field yang benar-benar MEMBENTUK jadwal.
 *
 * Judul, venue, dan fee boleh diubah setelah generate tanpa membuat jadwalnya
 * basi. Yang di bawah ini tidak: mengubahnya berarti jadwal di layar bukan lagi
 * hasil dari setup yang tertulis. Karena Simpan kini menyimpan jadwal yang
 * tampil (bukan generate ulang), ketidakcocokan itu harus dikatakan.
 */
function schedulingStamp() {
  const p = buildPayload();
  return JSON.stringify([
    p.courts, p.duration_minutes, p.round_minutes, p.warmup_minutes, p.mode,
    p.tier_count, p.referees_per_court, p.ballboys_per_court, p.seed, p.effort,
    // Percobaan ikut: ia mengubah jadwal yang keluar, jadi menggantinya membuat
    // yang di layar bukan lagi hasil dari setup yang tertulis.
    p.attempts,
    // Batas waktu solver ikut, dan hanya berarti di mode CP-SAT. Di mode lain
    // ia diabaikan penjadwal, jadi memasukkannya tanpa syarat akan membuat
    // jadwal Americano dianggap basi cuma karena angka yang tidak dipakainya.
    p.mode === 'americano_cpsat' ? p.cpsat_seconds : null,
    p.segments, p.interleave_segments, p.players, p.allowed_matchups,
  ]);
}

/** Tombol simpan harus menyebut tujuannya sebelum ditekan, bukan sesudah. */
function renderSaveTarget() {
  const editing = currentEventId !== null;
  $('save-event').textContent = editing
    ? `Simpan ke #${currentEventId}` : 'Simpan ke database';
  $('save-event-new').style.display = editing ? '' : 'none';
}

async function saveEvent(asNew) {
  if (!schedule) return toast('Belum ada jadwal');
  const target = asNew ? null : currentEventId;
  const basi = scheduleStamp !== null && scheduleStamp !== schedulingStamp();
  try {
    // Jadwal yang tampil ikut dikirim dan itulah yang disimpan, supaya jadwal
    // yang sudah diumumkan ke peserta tidak berubah hanya karena disimpan ulang.
    const d = await api('/api/events/save',
      { ...buildPayload(), event_id: target, schedule });
    currentEventId = d.id;
    renderSaveTarget();
    $('save-msg').innerHTML = `<div class="msg ok">${
      target ? `Jadwal #${d.id} diperbarui.` : `Tersimpan sebagai jadwal baru (#${d.id}).`
    }</div>` + (basi
      ? '<div class="msg warn">Setup diubah setelah jadwal ini dibuat. Yang '
        + 'tersimpan adalah jadwal yang tampil, bukan hasil setup yang baru - '
        + 'tekan Generate kalau ingin setup barunya diterapkan.</div>'
      : '');
    toast(target ? 'Jadwal diperbarui' : 'Tersimpan ke database');
  } catch (e) {
    $('save-msg').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

$('save-event').onclick = () => saveEvent(false);
$('save-event-new').onclick = () => saveEvent(true);

async function loadEvents() {
  try {
    const d = await loadPaged('events');
    if (!d.items.length) {
      $('events').innerHTML = `<div class="empty">${pager.events.search ? 'Tidak ada yang cocok.' : 'Belum ada jadwal tersimpan.'}</div>`;
      renderPager($('events-pager'), d, () => {});
      return;
    }
    let html = '<table class="data"><thead><tr><th>Judul</th><th>Tanggal</th>'
      + '<th>Venue</th><th class="num">Peserta</th><th class="num">Court</th>'
      + '<th class="num">Ronde</th><th class="num">Kualitas</th><th></th>'
      + '</tr></thead><tbody>';
    d.items.forEach((e) => {
      html += `<tr><td>${esc(e.title)}</td><td>${esc(tanggalID(e.event_date))}</td>` +
        `<td>${esc(e.venue || '-')}</td><td class="num">${e.n_players}</td>` +
        `<td class="num">${e.courts}</td><td class="num">${e.rounds}</td>` +
        `<td class="num">${e.quality_score}</td>` +
        `<td><button class="btn ghost sm" data-open="${e.id}">Buka</button> ` +
        `<button class="btn ghost sm" data-del-ev="${e.id}">Hapus</button></td></tr>`;
    });
    $('events').innerHTML = html + '</tbody></table>';
    renderPager($('events-pager'), d, (p) => { pager.events.page = p; loadEvents(); });
  } catch (e) {
    $('events').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

let eventSearchTimer = null;
$('search-event').addEventListener('input', () => {
  clearTimeout(eventSearchTimer);
  eventSearchTimer = setTimeout(() => {
    pager.events.search = $('search-event').value;
    pager.events.page = 1;
    loadEvents();
  }, 300);
});

$('events').addEventListener('click', async (e) => {
  const open = e.target.dataset.open, del = e.target.dataset.delEv;
  if (open) {
    const d = await api('/api/events/get?id=' + open);
    applyRequest(d.event.request);
    schedule = d.event.schedule;
    currentEventId = +open;
    // Setup baru saja diisi dari acara ini, jadi jadwal yang dimuat memang
    // hasil dari setup yang tampil - belum basi.
    scheduleStamp = schedulingStamp();
    renderSaveTarget();
    document.querySelector('.tabs button[data-view="jadwal"]').click();
    renderSchedule();
    toast('Jadwal dimuat');
  } else if (del) {
    if (!confirm('Hapus jadwal ini?')) return;
    await api('/api/events/delete', { id: +del });
    loadEvents(); toast('Terhapus');
  }
});

function applyRequest(req) {
  $('title').value = req.title || ''; $('event_date').value = req.event_date || '';
  $('venue').value = req.venue || ''; $('start_clock').value = req.start_clock || '';
  $('venue_name').value = req.venue || '';
  $('venue_id').value = req.venue_id || '';
  if (req.club_id) {
    $('club_id').value = req.club_id;
    const c = master.clubs.find((x) => x.id === req.club_id);
    if (c) $('club_name').value = c.name;
  }
  $('courts').value = req.courts; $('duration').value = req.duration_minutes;
  $('round_min').value = req.round_minutes; $('warmup').value = req.warmup_minutes;
  $('mode').value = req.mode; $('tier_count').value = req.tier_count || 2;
  $('referees').value = req.referees_per_court || 0;
  $('ballboys').value = req.ballboys_per_court || 0;
  $('seed').value = req.seed || 42;
  // Effort ikut tersimpan tapi dulu tidak ikut dipulihkan, jadi jadwal yang
  // dibuat dengan "Teliti" kembali sebagai "Normal". Diam-diam berbahaya:
  // menyimpan menghasilkan jadwal ulang, dan effort yang berbeda memberi
  // susunan berbeda walau seed-nya sama.
  // Hanya kalau nilainya memang salah satu pilihan: menyetel <select> ke nilai
  // asing membuatnya kosong, dan buildPayload lalu mengirim effort 0.
  //
  // Dicocokkan berpasangan dengan percobaan, karena dua pilihan sekarang
  // berbagi effort 80.000 dan hanya percobaannya yang membedakan. Jadwal lama
  // tidak menyimpan percobaan sama sekali; nilainya dulu selalu 3, jadi itu
  // yang diandaikan.
  //
  // Kalau tidak ada yang cocok persis, diambil yang effort-nya PALING DEKAT,
  // dan di antara yang sama dekat diambil yang percobaannya lebih banyak.
  // Bukan sekadar penjaga dari selector kosong: jadwal yang tersimpan dengan
  // effort 160.000 - pilihan yang sudah dihapus - kalau dibiarkan tidak cocok
  // akan kembali sebagai apa pun yang kebetulan sedang terpilih, dan diuji,
  // itu berarti host yang dulu memilih setelan paling teliti dipulihkan ke
  // "Cepat". Aturan terdekat ini memulangkannya ke "Sangat teliti".
  if (req.effort) {
    const opts = [...$('effort').options].map((o) => {
      const [eff, att] = o.value.split(':').map(Number);
      return { value: o.value, eff, att: att || 3 };
    });
    const pas = opts.find((o) => o.eff === +req.effort
        && o.att === +(req.attempts ?? 3))
      || opts.slice().sort((a, b) =>
        Math.abs(a.eff - req.effort) - Math.abs(b.eff - req.effort)
        || b.att - a.att)[0];
    if (pas) $('effort').value = pas.value;
  }
  $('tier-row').style.display = req.mode === 'tiered' ? '' : 'none';
  $('cpsat-block').style.display = req.mode === 'americano_cpsat' ? '' : 'none';
  renderCpsatRonde();
  // Acara lama tidak punya field ini; dipulihkan ke bawaan, bukan dibiarkan
  // mewarisi angka dari acara yang dibuka sebelumnya.
  $('cpsat_seconds').value = req.cpsat_seconds || 30;
  // Court berkurang. Acara lama tidak punya field ini, dan itu harus dipulihkan
  // sebagai "tidak dipakai" - bukan dibiarkan mewarisi centang dari acara yang
  // dibuka sebelumnya.
  $('courts_drop').checked = !!(req.courts_after && req.courts_from_round);
  if (req.courts_after) $('courts_after').value = req.courts_after;
  if (req.courts_from_round) $('courts_from_round').value = req.courts_from_round;
  renderCourtDrop();
  $('segments').innerHTML = '';
  $('interleave').checked = !!req.interleave_segments;
  (req.segments || []).forEach((s) => addSeg(s.label, s.rounds, s.rule));
  renderMatchups(req.allowed_matchups || null);
  // Acara lama tidak punya nama court; dipulihkan sebagai kosong, bukan
  // dibiarkan mewarisi nama dari acara yang dibuka sebelumnya - "Indoor A"
  // milik venue lain yang menempel di jadwal ini akan terbaca sebagai fakta.
  courtNames = (req.court_names || []).slice();
  players = (req.players || []).map((p) => ({ ...p }));
  nextId = players.reduce((m, p) => Math.max(m, p.id + 1), 0);
  if (req.economics) {
    $('court_price').value = req.economics.court_price_per_hour || 0;
    $('fee').value = req.economics.fee_per_player || 0;
    $('other_costs').value = req.economics.other_costs || 0;
  }
  renderPlayers();
}

// ---------------------------------------------------------------------------
// Master data: klub, venue, pemain
// ---------------------------------------------------------------------------
let master = { clubs: [], venues: [], players: [], default_club_id: null };
const combos = {};
// Status paging tiap tabel master.
const pager = {
  players: { page: 1, search: '' },
  clubs: { page: 1, search: '' },
  venues: { page: 1, search: '' },
  events: { page: 1, search: '' },
};

function currentClubId() {
  const v = $('club_id').value;
  return v ? +v : (master.default_club_id || null);
}

function clubVenues() {
  const cid = currentClubId();
  return master.venues.filter((v) => !v.club_id || v.club_id === cid);
}

function clubPlayers() {
  // Server sudah menyaring per klub; ini jaring pengaman kalau data lama
  // masih punya pemain tanpa klub (mis. klubnya pernah dihapus).
  const cid = currentClubId();
  return master.players.filter((p) => !p.club_id || p.club_id === cid);
}

/** Muat data master untuk mengisi combobox. Tabel dimuat terpisah per halaman. */
async function loadMaster() {
  // Klub yang aktif dikirim ke server supaya penyaringan terjadi di sana,
  // bukan setelah data klub lain terlanjur ikut terkirim.
  const cid = $('club_id').value;
  master = await api('/api/master' + (cid ? `?club_id=${encodeURIComponent(cid)}` : ''));
  if (!$('club_id').value && master.default_club_id) {
    const c = master.clubs.find((x) => x.id === master.default_club_id);
    if (c) { $('club_id').value = c.id; $('club_name').value = c.name; }
  }
  $('vn_club').innerHTML = master.clubs
    .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  Object.values(combos).forEach((c) => c.refresh());
}

// -- pemilih peserta --------------------------------------------------------
/** Tambahkan satu orang ke daftar peserta. Return false kalau sudah ada. */
function addParticipant(name, rating, gender) {
  const key = (name || '').trim().toLowerCase();
  if (!key) return false;
  if (players.some((p) => p.name.trim().toLowerCase() === key)) return false;
  players.push({
    id: nextId++, name: name.trim(),
    rating: Number.isFinite(+rating) ? +rating : 3.0,
    gender: gender || null, partner_id: null, court_preference: null,
  });
  return true;
}

function setupParticipantPicker() {
  const input = $('pick_player');
  combos.pick = createCombo({
    input,
    hidden: $('pick_player_id'),
    // Yang sudah masuk daftar tidak ditawarkan lagi - kalau ditawarkan, host
    // mengklik lalu tidak terjadi apa-apa dan itu terasa seperti bug.
    getItems: () => {
      const taken = new Set(players.map((p) => p.name.trim().toLowerCase()));
      return clubPlayers().filter((m) => !taken.has(m.name.trim().toLowerCase()));
    },
    meta: (m) => [m.gender === 'M' ? 'L' : m.gender === 'F' ? 'P' : null,
                  `rating ${m.rating}`].filter(Boolean).join(' · '),
    emptyText: 'Semua anggota master sudah masuk daftar.',
    onSelect: (m) => {
      const ok = addParticipant(m.name, m.rating, m.gender);
      // Kotak dikosongkan supaya bisa langsung mengetik nama berikutnya.
      input.value = '';
      $('pick_player_id').value = '';
      renderPlayers();
      toast(ok ? `${m.name} ditambahkan` : `${m.name} sudah ada di daftar`);
      input.focus();
    },
    quickAdd: {
      title: 'Peserta baru',
      fields: [
        { key: 'rating', label: 'Rating', type: 'number', step: '0.5',
          min: 0, value: 3 },
        { key: 'gender', label: 'L/P', type: 'select', value: '',
          options: [{ value: '', label: '-' }, { value: 'M', label: 'Laki-laki' },
                    { value: 'F', label: 'Perempuan' }] },
      ],
      validate: (name, v) => {
        const r = Number(v.rating);
        if (!Number.isFinite(r) || r < 0 || r > 7) return 'Rating harus 0-7.';
        if (players.some((p) => p.name.trim().toLowerCase()
                                === name.trim().toLowerCase())) {
          return 'Nama itu sudah ada di daftar peserta.';
        }
        return null;
      },
      // Disimpan ke master sekaligus dimasukkan ke daftar peserta - host yang
      // mengetik nama baru jelas bermaksud mengundangnya ke acara ini juga.
      save: async (name, v) => {
        const r = await api('/api/players/save', {
          club_id: currentClubId(), name,
          rating: +v.rating || 3, gender: v.gender || null,
        });
        await loadMaster();
        addParticipant(name, +v.rating || 3, v.gender || null);
        input.value = '';
        $('pick_player_id').value = '';
        renderPlayers();
        toast(`${name} disimpan ke master & ditambahkan`);
        return { id: r.id, name };
      },
    },
  });

  // Enter pada satu-satunya saran = langsung tambah, tanpa perlu klik.
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const typed = input.value.trim();
    if (!typed) return;
    const hit = clubPlayers().find(
      (m) => m.name.trim().toLowerCase() === typed.toLowerCase());
    if (!hit) return;                  // biarkan combobox yang menangani
    e.preventDefault();
    if (addParticipant(hit.name, hit.rating, hit.gender)) {
      input.value = '';
      renderPlayers();
      toast(`${hit.name} ditambahkan`);
    }
  });
}

// -- combobox Setup ---------------------------------------------------------
function setupCombos() {
  combos.club = createCombo({
    input: $('club_name'), hidden: $('club_id'),
    getItems: () => master.clubs,
    meta: (c) => c.city || '',
    emptyText: 'Belum ada klub tersimpan.',
    onSelect: () => { loadMasterTables(); loadClubSummary(); },
    quickAdd: {
      title: 'Klub baru',
      fields: [{ key: 'city', label: 'Kota', placeholder: 'opsional' }],
      save: async (name, v) => {
        const r = await api('/api/clubs/save', { name, city: v.city });
        await loadMaster();
        return master.clubs.find((c) => c.id === r.id) || { id: r.id, name };
      },
    },
  });

  combos.venue = createCombo({
    input: $('venue_name'), hidden: $('venue_id'),
    getItems: () => clubVenues(),
    meta: (v) => `${v.court_count} court · ${rp(v.price_per_hour)}/jam`,
    emptyText: 'Belum ada venue untuk klub ini.',
    onSelect: (v) => {
      // Venue membawa jumlah court & harga sewanya sendiri.
      $('venue').value = v.name;
      if (v.court_count) $('courts').value = v.court_count;
      if (v.price_per_hour) $('court_price').value = v.price_per_hour;
      scheduleAnalyze();
    },
    onClear: () => { $('venue').value = $('venue_name').value; },
    quickAdd: {
      title: 'Venue baru',
      fields: [
        { key: 'court_count', label: 'Jumlah court', type: 'number', min: 1, value: 4 },
        { key: 'price_per_hour', label: 'Harga sewa / jam (Rp)', type: 'number', min: 0, step: 10000, value: 200000 },
      ],
      validate: (name, v) => {
        const c = Number(v.court_count), p = Number(v.price_per_hour);
        if (!Number.isFinite(c) || c < 1) return 'Jumlah court minimal 1.';
        if (!Number.isInteger(c)) return 'Jumlah court harus bilangan bulat.';
        if (!Number.isFinite(p) || p < 0) return 'Harga sewa tidak boleh negatif.';
        return null;
      },
      save: async (name, v) => {
        const r = await api('/api/venues/save', {
          name, club_id: currentClubId(),
          court_count: +v.court_count, price_per_hour: +v.price_per_hour,
        });
        await loadMaster();
        return master.venues.find((x) => x.id === r.id) || { id: r.id, name };
      },
    },
  });
}

// -- pager ------------------------------------------------------------------
function renderPager(host, data, onGo) {
  host.textContent = '';
  if (!data || data.pages <= 1) {
    if (data && data.total) {
      const info = el('div', 'pager-info', `${data.total} baris`);
      host.appendChild(info);
    }
    return;
  }
  const bar = el('div', 'pager');
  const mk = (label, page, disabled, on) => {
    const b = el('button', 'pager-btn' + (on ? ' on' : ''), esc(label));
    b.type = 'button';
    b.disabled = !!disabled;
    if (!disabled) b.onclick = () => onGo(page);
    return b;
  };
  bar.appendChild(mk('‹', data.page - 1, data.page <= 1));

  // Jendela halaman di sekitar halaman aktif, supaya tidak meluber.
  const span = 2;
  let from = Math.max(1, data.page - span);
  let to = Math.min(data.pages, data.page + span);
  if (from > 1) { bar.appendChild(mk('1', 1, false, data.page === 1)); if (from > 2) bar.appendChild(el('span', 'pager-gap', '…')); }
  for (let i = from; i <= to; i++) bar.appendChild(mk(String(i), i, false, i === data.page));
  if (to < data.pages) { if (to < data.pages - 1) bar.appendChild(el('span', 'pager-gap', '…')); bar.appendChild(mk(String(data.pages), data.pages, false, false)); }

  bar.appendChild(mk('›', data.page + 1, data.page >= data.pages));
  bar.appendChild(el('span', 'pager-info',
    `Halaman ${data.page} dari ${data.pages} · ${data.total} baris`));
  host.appendChild(bar);
}

async function loadPaged(entity, extra = '') {
  const st = pager[entity];
  const cid = currentClubId();
  const q = new URLSearchParams({ page: st.page, search: st.search });
  if (entity !== 'clubs' && cid) q.set('club_id', cid);
  return api(`/api/${entity}/list?${q}${extra}`);
}

async function loadMasterTables() {
  await Promise.all([renderPlayers_(), renderClubs_(), renderVenues_()]);
}

// ---------------------------------------------------------------------------
// Pilih banyak lalu hapus sekaligus
// ---------------------------------------------------------------------------
// Pilihan disimpan per tabel dan BERTAHAN antar halaman: membersihkan master
// biasanya berarti menyisir beberapa halaman, dan pilihan yang hilang tiap kali
// ganti halaman memaksa host menghapus berulang kali per halaman.
const dipilih = { players: new Set(), clubs: new Set(), venues: new Set() };

const BULK = {
  players: { url: '/api/players/delete', label: 'pemain', render: () => renderPlayers_() },
  clubs: { url: '/api/clubs/delete', label: 'klub', render: () => renderClubs_() },
  venues: { url: '/api/venues/delete', label: 'venue', render: () => renderVenues_() },
};

/** Kolom centang untuk satu baris. */
function pickSel(key, id) {
  const on = dipilih[key].has(id) ? ' checked' : '';
  return `<td class="pick-col"><input type="checkbox" data-pick="${key}" `
    + `data-id="${id}" aria-label="Pilih baris"${on}></td>`;
}

/** Batang aksi di atas tabel. Selalu ada tempatnya, isinya muncul saat ada
 *  yang dipilih - supaya tabel tidak melompat naik-turun saat mencentang. */
function bulkBar(key) {
  const n = dipilih[key].size;
  if (!n) return '';
  return `<div class="bulk-bar"><span>${n} ${BULK[key].label} dipilih</span>`
    + `<button class="btn ghost sm" data-bulk-clear="${key}">Batal pilih</button>`
    + `<button class="btn ghost sm danger" data-bulk-del="${key}">Hapus ${n} terpilih</button>`
    + '</div>';
}

function bindBulk(host, key) {
  host.querySelectorAll('input[data-pick]').forEach((cb) => {
    cb.onchange = () => {
      const id = +cb.dataset.id;
      if (cb.checked) dipilih[key].add(id); else dipilih[key].delete(id);
      BULK[key].render();
    };
  });
  const all = host.querySelector('input[data-pick-all]');
  if (all) {
    all.onchange = () => {
      host.querySelectorAll('input[data-pick]').forEach((cb) => {
        const id = +cb.dataset.id;
        if (all.checked) dipilih[key].add(id); else dipilih[key].delete(id);
      });
      BULK[key].render();
    };
  }
  const clr = host.querySelector('[data-bulk-clear]');
  if (clr) clr.onclick = () => { dipilih[key].clear(); BULK[key].render(); };
  const del = host.querySelector('[data-bulk-del]');
  if (del) del.onclick = async () => {
    const ids = [...dipilih[key]];
    if (!ids.length) return;
    if (!confirm(`Hapus ${ids.length} ${BULK[key].label} dari master? `
                 + 'Tindakan ini tidak bisa dibatalkan.')) return;
    try {
      await api(BULK[key].url, { ids });
      dipilih[key].clear();
      await loadMaster();
      await BULK[key].render();
      toast(`${ids.length} ${BULK[key].label} terhapus`);
    } catch (e) { toast(e.message); }
  };
}

/** Header centang: tercentang kalau SEMUA baris halaman ini terpilih. */
function pickAllHead(key, items) {
  const semua = items.length > 0 && items.every((x) => dipilih[key].has(x.id));
  return `<th class="pick-col"><input type="checkbox" data-pick-all="${key}" `
    + `aria-label="Pilih semua di halaman ini"${semua ? ' checked' : ''}></th>`;
}

async function renderPlayers_() {
  const d = await loadPaged('players');
  $('players-table').innerHTML = (d.items.length
    ? bulkBar('players')
      + '<table class="data"><thead><tr>' + pickAllHead('players', d.items)
      + '<th>Nama</th><th>Panggilan</th>'
      + '<th class="num">Rating</th><th class="num">L/P</th><th>Level</th>'
      + '<th></th></tr></thead><tbody>' +
      d.items.map((p) =>
        `<tr>${pickSel('players', p.id)}<td>${esc(p.name)}</td><td>${esc(p.nickname || '-')}</td>` +
        `<td class="num">${p.rating}</td>` +
        `<td class="num">${p.gender === 'M' ? 'L' : p.gender === 'F' ? 'P' : '-'}</td>` +
        `<td>${esc(p.level_label || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-pl="${p.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-pl="${p.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : `<div class="empty">${pager.players.search ? 'Tidak ada yang cocok.' : 'Belum ada pemain. Tambahkan lewat form di atas, atau dari tab Setup klik "Simpan ke master pemain".'}</div>`);
  bindBulk($('players-table'), 'players');
  renderPager($('players-pager'), d, (p) => { pager.players.page = p; renderPlayers_(); });
}

async function renderClubs_() {
  const d = await loadPaged('clubs');
  $('clubs-table').innerHTML = (d.items.length
    ? bulkBar('clubs')
      + '<table class="data"><thead><tr>' + pickAllHead('clubs', d.items)
      + '<th>Klub</th><th>Kota</th><th>Kontak</th><th></th></tr></thead><tbody>' +
      d.items.map((c) =>
        `<tr>${pickSel('clubs', c.id)}<td>${c.logo ? `<img class="logo-mini" src="${esc(c.logo)}" alt="">` : ''}${esc(c.name)}</td>` +
        `<td>${esc(c.city || '-')}</td>` +
        `<td>${esc(c.contact || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-cl="${c.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-cl="${c.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : '<div class="empty">Belum ada klub.</div>');
  bindBulk($('clubs-table'), 'clubs');
  renderPager($('clubs-pager'), d, (p) => { pager.clubs.page = p; renderClubs_(); });
}

async function renderVenues_() {
  const d = await loadPaged('venues');
  $('venues-table').innerHTML = (d.items.length
    ? bulkBar('venues')
      + '<table class="data"><thead><tr>' + pickAllHead('venues', d.items)
      + '<th>Nama</th><th class="num">Court</th>'
      + '<th class="num">Harga/jam</th><th>Alamat</th><th></th></tr></thead><tbody>' +
      d.items.map((v) =>
        `<tr>${pickSel('venues', v.id)}<td>${esc(v.name)}</td><td class="num">${v.court_count}</td>` +
        `<td class="num">${rp(v.price_per_hour)}</td><td>${esc(v.address || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-vn="${v.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-vn="${v.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : '<div class="empty">Belum ada venue. Isi harga sewa di sini supaya panel Biaya terisi otomatis.</div>');
  bindBulk($('venues-table'), 'venues');
  renderPager($('venues-pager'), d, (p) => { pager.venues.page = p; renderVenues_(); });
}

// Pencarian tiap tabel: kembali ke halaman 1 dan tunggu ketikan berhenti.
[['search-players', 'players', renderPlayers_],
 ['search-clubs', 'clubs', renderClubs_],
 ['search-venues', 'venues', renderVenues_]].forEach(([id, key, fn]) => {
  let t;
  $(id).addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { pager[key].search = $(id).value; pager[key].page = 1; fn(); }, 250);
  });
});

// Sub-tab master
document.querySelectorAll('#master-tabs button').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('#master-tabs button').forEach((x) => x.classList.remove('on'));
    document.querySelectorAll('.mview').forEach((x) => { x.style.display = 'none'; });
    b.classList.add('on');
    $('m-' + b.dataset.m).style.display = '';
    if (b.dataset.m === 'stats') loadPlayerStats();
  };
});

// -- form pemain ------------------------------------------------------------
const PL_FIELDS = ['id', 'name', 'nickname', 'contact', 'gender', 'rating', 'level', 'notes'];
function resetPlayerForm() {
  PL_FIELDS.forEach((f) => { const n = $('pl_' + f); if (n) n.value = f === 'rating' ? 3 : ''; });
}
$('pl-reset').onclick = resetPlayerForm;
$('pl-save').onclick = async () => {
  const name = $('pl_name').value.trim();
  if (!name) return toast('Nama wajib diisi');
  try {
    await api('/api/players/save', {
      id: $('pl_id').value ? +$('pl_id').value : null,
      club_id: currentClubId(), name,
      nickname: $('pl_nickname').value, contact: $('pl_contact').value,
      gender: $('pl_gender').value || null, rating: +$('pl_rating').value || 3,
      level_label: $('pl_level').value, notes: $('pl_notes').value,
    });
    resetPlayerForm(); await loadMaster(); await renderPlayers_(); toast('Pemain tersimpan');
  } catch (e) { toast(e.message); }
};

$('players-table').addEventListener('click', async (e) => {
  const ed = e.target.dataset.edPl, del = e.target.dataset.delPl;
  if (ed) {
    const p = master.players.find((x) => x.id === +ed);
    if (!p) return;
    $('pl_id').value = p.id; $('pl_name').value = p.name;
    $('pl_nickname').value = p.nickname || ''; $('pl_contact').value = p.contact || '';
    $('pl_gender').value = p.gender || ''; $('pl_rating').value = p.rating;
    $('pl_level').value = p.level_label || ''; $('pl_notes').value = p.notes || '';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } else if (del) {
    if (!confirm('Hapus pemain ini dari master?')) return;
    await api('/api/players/delete', { id: +del });
    await loadMaster(); await renderPlayers_(); toast('Terhapus');
  }
});

// -- form klub --------------------------------------------------------------
function setLogoPreview(uri) {
  $('cl_logo').value = uri || '';
  const box = $('cl_logo_prev');
  box.textContent = '';
  if (uri) {
    const img = document.createElement('img');
    img.src = uri;
    box.appendChild(img);
  } else {
    const sp = document.createElement('span');
    sp.textContent = 'belum ada logo';
    box.appendChild(sp);
  }
}

// Berkas dibaca jadi data URI di browser, lalu divalidasi lagi di server.
$('cl_logo_file').addEventListener('change', () => {
  const f = $('cl_logo_file').files[0];
  if (!f) return;
  if (!['image/png', 'image/jpeg'].includes(f.type)) {
    toast('Logo harus PNG atau JPEG'); $('cl_logo_file').value = ''; return;
  }
  if (f.size > 400 * 1024) {
    toast(`Ukuran ${Math.round(f.size / 1024)} KB melebihi batas 400 KB`);
    $('cl_logo_file').value = ''; return;
  }
  const reader = new FileReader();
  reader.onload = () => setLogoPreview(reader.result);
  reader.onerror = () => toast('Gagal membaca berkas');
  reader.readAsDataURL(f);
});

$('cl-logo-clear').onclick = () => {
  setLogoPreview('');
  $('cl_logo_file').value = '';
  toast('Logo dikosongkan, klik Simpan untuk menerapkan');
};

$('cl-reset').onclick = () => {
  ['id', 'name', 'city', 'contact', 'wa', 'notes']
    .forEach((f) => { $('cl_' + f).value = ''; });
  $('cl_logo_file').value = '';
  setLogoPreview('');
};
$('cl-save').onclick = async () => {
  if (!$('cl_name').value.trim()) return toast('Nama klub wajib diisi');
  try {
    await api('/api/clubs/save', {
      id: $('cl_id').value ? +$('cl_id').value : null,
      name: $('cl_name').value, city: $('cl_city').value,
      contact: $('cl_contact').value, wa_group: $('cl_wa').value,
      notes: $('cl_notes').value, logo: $('cl_logo').value,
    });
    $('cl-reset').onclick(); await loadMaster(); await renderClubs_(); toast('Klub tersimpan');
  } catch (e) { toast(e.message); }
};
$('clubs-table').addEventListener('click', async (e) => {
  const ed = e.target.dataset.edCl, del = e.target.dataset.delCl;
  if (ed) {
    const c = master.clubs.find((x) => x.id === +ed);
    if (!c) return;
    $('cl_id').value = c.id; $('cl_name').value = c.name; $('cl_city').value = c.city || '';
    $('cl_contact').value = c.contact || ''; $('cl_wa').value = c.wa_group || '';
    $('cl_notes').value = c.notes || '';
    $('cl_logo_file').value = '';
    setLogoPreview(c.logo || '');
  } else if (del) {
    if (!confirm('Hapus klub ini?')) return;
    await api('/api/clubs/delete', { id: +del });
    await loadMaster(); await renderClubs_(); toast('Terhapus');
  }
});

// -- form venue -------------------------------------------------------------
$('vn-reset').onclick = () => {
  $('vn_id').value = ''; $('vn_name').value = ''; $('vn_address').value = '';
  $('vn_courts').value = 4; $('vn_price').value = 200000;
};
$('vn-save').onclick = async () => {
  if (!$('vn_name').value.trim()) return toast('Nama venue wajib diisi');
  try {
    await api('/api/venues/save', {
      id: $('vn_id').value ? +$('vn_id').value : null,
      club_id: +$('vn_club').value || null, name: $('vn_name').value,
      address: $('vn_address').value, court_count: +$('vn_courts').value,
      price_per_hour: +$('vn_price').value,
    });
    $('vn-reset').onclick(); await loadMaster(); await renderVenues_(); toast('Venue tersimpan');
  } catch (e) { toast(e.message); }
};
$('venues-table').addEventListener('click', async (e) => {
  const ed = e.target.dataset.edVn, del = e.target.dataset.delVn;
  if (ed) {
    const v = master.venues.find((x) => x.id === +ed);
    if (!v) return;
    $('vn_id').value = v.id; $('vn_name').value = v.name;
    $('vn_address').value = v.address || ''; $('vn_courts').value = v.court_count;
    $('vn_price').value = v.price_per_hour; $('vn_club').value = v.club_id || '';
  } else if (del) {
    if (!confirm('Hapus venue ini?')) return;
    await api('/api/venues/delete', { id: +del });
    await loadMaster(); await renderVenues_(); toast('Terhapus');
  }
});

// -- statistik pemain -------------------------------------------------------
async function loadPlayerStats() {
  try {
    const cid = currentClubId();
    const d = await api('/api/stats/players' + (cid ? '?club_id=' + cid : ''));
    if (!d.stats.length) {
      $('stats-chart').textContent = '';
      $('stats-table').innerHTML = '<div class="empty">Belum ada acara tersimpan. Simpan jadwal dulu di tab Jadwal.</div>';
      return;
    }
    hideTip();
    const statsHost = $('stats-chart');
    statsHost.textContent = '';
    statsHost.appendChild(restShareChart(d.stats, statsHost.clientWidth));
    $('stats-table').innerHTML =
      '<table class="data"><thead><tr><th>Nama</th>'
      + '<th class="num">Ikut acara</th><th class="num">Ronde main</th>'
      + '<th class="num">Ronde duduk</th><th class="num">% duduk</th>'
      + '<th class="num">Tugas</th><th>Terakhir</th></tr></thead><tbody>' +
      d.stats.map((s) =>
        `<tr><td>${esc(s.name)}</td><td class="num">${s.events}</td>` +
        `<td class="num">${s.rounds_played}</td><td class="num">${s.rounds_rested}</td>` +
        `<td class="num" style="color:${s.rest_pct > 40 ? 'var(--warn)' : 'inherit'}">${s.rest_pct}%</td>` +
        `<td class="num">${s.duties}</td>` +
        `<td>${esc(tanggalID((s.last_seen || '').slice(0, 10)))}</td></tr>`
      ).join('') + '</tbody></table>';
  } catch (e) {
    $('stats-table').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

// -- integrasi dengan tab Setup --------------------------------------------
$('save-roster').onclick = async () => {
  if (!players.length) return toast('Belum ada peserta');
  try {
    const d = await api('/api/players/bulk', { club_id: currentClubId(), players });
    await loadMaster();
    await renderPlayers_();
    toast(`${d.saved} pemain disimpan ke master`);
  } catch (e) { toast(e.message); }
};

$('load-roster').onclick = async () => {
  await loadMaster();
  const pl = clubPlayers();
  if (!pl.length) return toast('Master pemain masih kosong');
  let added = 0;
  pl.forEach((m) => {
    if (players.some((p) => p.name.toLowerCase() === m.name.toLowerCase())) return;
    players.push({
      id: nextId++, name: m.name, rating: m.rating, gender: m.gender,
      partner_id: null, court_preference: null,
    });
    added++;
  });
  renderPlayers();
  toast(added ? `${added} pemain dimuat` : 'Semua pemain sudah ada di daftar');
};

// ---------------------------------------------------------------------------
// Ekonomi
// ---------------------------------------------------------------------------
/**
 * Panel biaya, digambar ulang dari server.
 *
 * Dulu ini cuma jalan lewat tombol "Hitung ulang", dan itu membuat seluruh
 * panel BOHONG diam-diam: host mengubah harga court atau jumlah peserta, semua
 * angka di layar tetap angka lama, dan yang paling menyesatkan adalah "Fee
 * untuk target margin" - empat angka bulat yang tidak bergerak persis seperti
 * nilai yang ditulis mati di kode. Panel Analisa kelayakan di sebelahnya sejak
 * awal memperbarui diri tiap ketikan, jadi ketidakkonsistenannya sendiri yang
 * membuat panel ini terbaca rusak.
 *
 * Aman dipanggil sesering itu: /api/economics tidak menjalankan penjadwalan,
 * dan diukur 1-29 ms untuk 12 sampai 40 peserta.
 */
async function renderEconomics() {
  if (players.length < 4) return;
  try {
    const d = await api('/api/economics', buildPayload());
    const c = d.current;

    const tile = statTileHTML;

    $('econ-now').innerHTML = '<div class="stat-grid">' +
      tile('Biaya total', rp(c.total_cost), `${c.courts} court x ${c.hours} jam`) +
      tile('Pemasukan', rp(c.revenue), `${c.n_players} x fee`) +
      tile('Untung', rp(c.profit), `margin ${c.margin_pct}%`, c.profit >= 0 ? 'good' : 'bad') +
      tile('Modal / peserta', rpUp(c.break_even_fee), 'fee minimal / peserta') +
      tile('Main / peserta', `${c.play_minutes_per_player}`, `menit (${pct(c.rest_ratio)} duduk)`,
        c.rest_ratio > 1 / 3 ? 'warn' : 'good') +
      '</div>' +
      (c.labels.length ? '<div style="margin-top:10px">' +
        c.labels.map((l) => `<span class="pill ${l === 'rugi' ? 'b' : l.includes('layak') || l.includes('semua main') ? 'g' : 'w'}">${esc(l)}</span>`).join('') +
        '</div>' : '');

    const u = d.upgrade;
    $('econ-up').innerHTML =
      `<div style="font-size:13px;margin-bottom:10px">${esc(u.note)}</div>` +
      '<div class="stat-grid">' +
      tile('Tambahan biaya', rp(u.extra_cost), 'total') +
      tile('Tambahan main', `+${u.extra_play_minutes_per_player}`, 'menit / peserta',
        u.extra_play_minutes_per_player > 0 ? 'good' : '') +
      tile('Fee naik', rp(u.fee_bump_to_break_even), 'agar tidak nombok') +
      tile('Fee jaga margin', rp(u.fee_to_keep_same_margin), 'margin tetap sama') +
      '</div>';

    // Kartunya bisa diklik untuk memakai angkanya. Tanpa itu host membaca
    // "margin 30% berarti Rp 90.000", lalu menyalinnya sendiri ke kolom fee -
    // dan salah ketik di situ tidak terlihat, karena panelnya lalu menghitung
    // margin dari angka yang salah tanpa ada yang janggal.
    let fs = '<div style="font-size:11px;color:var(--muted);font-weight:700;'
      + 'text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">'
      + 'Fee untuk target margin <span class="hint" style="text-transform:none;'
      + 'letter-spacing:0;font-weight:400">klik untuk memakainya</span></div>'
      + '<div class="stat-grid">';
    Object.entries(d.fee_suggestions).forEach(([m, f]) => {
      fs += tile(`Margin ${m}%`, rp(f), 'per peserta', '', {
        cls: 'stat-pick',
        attrs: `role="button" tabindex="0" data-fee="${f}" `
          + `title="Pakai ${rp(f)} sebagai fee per peserta"`,
      });
    });
    $('fee-suggest').innerHTML = fs + '</div>';

    hideTip();
    const chartHost = $('econ-chart');
    chartHost.textContent = '';
    chartHost.appendChild(tradeoffChart(d.options, c, chartHost.clientWidth));

    let html = '<table class="data"><thead><tr><th>Court</th><th>Jam</th><th>Ronde</th>' +
      '<th>Duduk</th><th>Main/org</th><th>Biaya</th><th>Untung</th><th>Margin</th><th></th></tr></thead><tbody>';
    d.options.forEach((o) => {
      const cur = o.courts === c.courts && Math.abs(o.hours - c.hours) < 0.01;
      html += `<tr><td class="num${cur ? ' pick' : ''}">${o.courts}${cur ? ' *' : ''}</td>` +
        `<td class="num">${o.hours}</td><td class="num">${o.rounds}</td>` +
        `<td class="num">${o.byes_per_round} (${pct(o.rest_ratio)})</td>` +
        `<td class="num">${o.play_minutes_per_player}m</td>` +
        `<td class="num">${rp(o.total_cost)}</td>` +
        `<td class="num" style="color:${o.profit >= 0 ? 'var(--good)' : 'var(--bad)'}">${rp(o.profit)}</td>` +
        `<td class="num">${o.margin_pct}%</td>` +
        `<td>${o.labels.slice(0, 2).map((l) => `<span class="pill ${l === 'rugi' ? 'b' : l.includes('layak') || l.includes('semua') ? 'g' : 'w'}">${esc(l)}</span>`).join('')}</td></tr>`;
    });
    $('econ-table').innerHTML = html + '</tbody></table>' +
      '<div style="font-size:11.5px;color:var(--muted);margin-top:10px">* = setup yang sedang kamu pilih.</div>';
  } catch (e) {
    $('econ-now').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

let econTimer = null;
function scheduleEconomics() {
  clearTimeout(econTimer);
  econTimer = setTimeout(renderEconomics, 250);
}

// Tombolnya tetap ada: kalau panelnya pernah gagal (server sibuk menyusun
// jadwal), host butuh cara memaksa tanpa harus mengubah isian dulu.
$('calc-econ').onclick = () => {
  if (players.length < 4) return toast('Butuh minimal 4 peserta');
  renderEconomics();
};

// Semua isian yang benar-benar mengubah hitungan biaya. Jumlah peserta ikut
// lewat renderPlayers(), yang memanggil scheduleEconomics() sendiri.
['court_price', 'fee', 'other_costs', 'courts', 'duration',
 'courts_after', 'courts_from_round', 'courts_drop']
  .forEach((id) => {
    const e = $(id);
    if (e) {
      e.addEventListener('input', scheduleEconomics);
      e.addEventListener('change', scheduleEconomics);
    }
  });

/** Pakai satu saran fee sebagai fee per peserta. */
function pakaiFee(node) {
  const nilai = +node.dataset.fee;
  if (!nilai) return;
  $('fee').value = nilai;
  $('fee').dispatchEvent(new Event('input', { bubbles: true }));
  toast(`Fee per peserta jadi ${rp(nilai)}`);
}

$('fee-suggest').addEventListener('click', (e) => {
  const t = e.target.closest('.stat-pick');
  if (t) pakaiFee(t);
});
// Bisa dicapai keyboard juga - kartunya role="button", jadi ia harus benar-benar
// bekerja seperti tombol, bukan cuma terbaca sebagai tombol oleh pembaca layar.
$('fee-suggest').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target.closest('.stat-pick');
  if (t) { e.preventDefault(); pakaiFee(t); }
});

async function loadClubSummary() {
  const cid = currentClubId();
  if (!cid) return;
  try {
    const d = await api('/api/stats/club?club_id=' + cid);
    const s = d.summary;
    const tile = (k, v, sub) =>
      `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>` +
      `<div class="s">${esc(sub)}</div></div>`;
    // "Anggota 0" di samping puluhan kehadiran itu akurat tapi membingungkan:
    // peserta yang ditempel di tab Setup tidak otomatis jadi anggota master.
    const memberHint = (!s.members && s.attendances)
      ? 'belum ada · simpan peserta ke master' : 'aktif';
    $('club-summary').innerHTML = '<div class="stat-grid">' +
      tile('Anggota', s.members, memberHint) +
      tile('Acara', s.events, 'tersimpan') +
      tile('Total hadir', s.attendances, 'kehadiran') +
      tile('Pemasukan', rp(s.revenue), 'akumulasi') +
      tile('Untung', rp(s.profit), 'akumulasi') +
      tile('Rata kualitas', s.avg_quality, 'dari 100') +
      '</div>';
  } catch (e) {
    $('club-summary').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
(async function init() {
  try {
    const d = await api('/api/presets');
    presets = d.presets;
    $('preset').innerHTML = Object.entries(presets)
      .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
    $('preset-desc').textContent = presets.single ? presets.single.description : '';
    matchupTypes = d.matchups || [];
    if (d.court_name_max) courtNameMax = d.court_name_max;
    renderMatchups();
    applyCpsatAvailability(!!d.cpsat);
  } catch (e) { /* biarkan default */ }

  setupCombos();
  setupParticipantPicker();
  renderSaveTarget();
  try { await loadMaster(); } catch (e) { /* database belum siap, abaikan */ }
  renderPlayers();
})();
