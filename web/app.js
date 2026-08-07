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
let presets = {};
let analyzeTimer = null;

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
function statTileHTML(k, v, sub, state) {
  const mark = STATE_MARK[state];
  return `<div class="stat${state ? ' ' + state : ''}">` +
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
function buildPayload() {
  return {
    club_id: $('club_id').value ? +$('club_id').value : null,
    venue_id: $('venue_id').value ? +$('venue_id').value : null,
    title: $('title').value || 'Meet Padel',
    event_date: $('event_date').value,
    venue: $('venue').value,
    start_clock: $('start_clock').value,
    courts: +$('courts').value,
    duration_minutes: +$('duration').value,
    round_minutes: +$('round_min').value,
    warmup_minutes: +$('warmup').value,
    mode: $('mode').value,
    tier_count: +$('tier_count').value,
    referees_per_court: +$('referees').value,
    ballboys_per_court: +$('ballboys').value,
    seed: +$('seed').value,
    effort: +$('effort').value,
    segments: getSegments(),
    interleave_segments: $('interleave').checked,
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

  const cost = courts * hours * price + other;
  const revenue = n * fee;
  const profit = revenue - cost;
  const margin = revenue > 0 ? (profit / revenue) * 100 : 0;
  const perPlayer = n ? cost / n : 0;
  const minutes = report ? report.playing_minutes_per_player : 0;

  // Ambang bermakna, bukan selera: rugi itu bad, margin tipis (<15%) perlu
  // diperhatikan, sisanya aman.
  const state = profit < 0 ? 'bad' : margin < 15 ? 'warn' : 'good';

  host.innerHTML = '<div class="stat-grid" style="margin-top:12px">'
    + statTileHTML('Biaya total', rp(cost), `${courts} court x ${hours} jam`)
    + statTileHTML('Pemasukan', rp(revenue), `${n} x ${rp(fee)}`)
    + statTileHTML('Untung', rp(profit), `margin ${margin.toFixed(1)}%`, state)
    + statTileHTML('Titik impas', rp(perPlayer), 'fee minimal / peserta')
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
    grid.appendChild(tile('Main / orang', r.avg_plays_per_player, `${r.playing_minutes_per_player} menit`));
    grid.appendChild(tile('Duduk / ronde', r.byes_per_round, pct(r.rest_ratio), restCls));
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
 'ballboys', 'court_price', 'fee', 'other_costs']
  .forEach((id) => $(id).addEventListener('input', scheduleAnalyze));

$('mode').addEventListener('change', () => {
  $('tier-row').style.display = $('mode').value === 'tiered' ? '' : 'none';
});

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

$('generate').onclick = async () => {
  const btn = $('generate');
  btn.disabled = true; btn.textContent = 'Menghitung...';
  $('gen-msg').innerHTML = '';
  $('prog-log').textContent = '';
  $('gen-progress').style.display = '';
  setProgress(0, 'Mengirim data ke generator');

  let failed = null;
  try {
    await streamSSE('/api/schedule/stream', buildPayload(), (event, data) => {
      if (event === 'progress') {
        setProgress(data.pct, data.message);
        logLine(`${String(data.pct).padStart(5)}%  ${data.message}`);
      } else if (event === 'done') {
        schedule = data;
        currentEventId = null;
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
    toast('Jadwal siap');
  } catch (e) {
    $('gen-msg').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    logLine(e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Generate';
  }
};

function renderSchedule() {
  if (!schedule) return;
  const st = schedule.stats;
  const plays = Object.values(st.plays_per_player);

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
  grid.appendChild(tile('Duduk beruntun', st.back_to_back_byes, 'kejadian',
    st.back_to_back_byes ? 'warn' : 'good'));
  $('sched-stats').innerHTML = '';
  $('sched-stats').appendChild(grid);

  // Ronde. Card disusun grid, jumlah kolom mengikuti isi tiap card: dengan
  // 1 court satu card hanya memuat satu match, jadi kolom tunggal membuang
  // lebar panel dan memaksa scroll berkali-kali lipat.
  const maxMatches = schedule.rounds.reduce(
    (t, r) => Math.max(t, r.matches.length), 1);
  const cols = maxMatches === 1 ? 3 : maxMatches === 2 ? 2 : 1;
  const box = el('div', `rounds cols-${cols}`);

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
        duty.push(`${x.role === 'wasit' ? 'W' : 'B'} ${esc(nameOf(x.player_id))}`);
      });
      const pool = m.pool ? `<span class="pool">${esc(m.pool)}</span>` : '';
      card.appendChild(el('div', 'match',
        `<span class="c">C${m.court}</span>` +
        `<span class="tm">${esc(m.team_a.map((x) => x.name).join(' & '))}${pool}</span>` +
        `<span class="vs">vs</span>` +
        `<span class="tm b">${esc(m.team_b.map((x) => x.name).join(' & '))}</span>` +
        `<span class="duty">${duty.join(' · ')}</span>`));
    });

    const busy = new Set((r.roles || []).map((x) => x.player_id));
    const idle = r.byes.filter((b) => !busy.has(b.id));
    if (idle.length) {
      card.appendChild(el('div', 'resting',
        'Istirahat: ' + esc(idle.map((b) => b.name).join(', '))));
    }
    box.appendChild(card);
  });
  $('rounds').innerHTML = '';
  $('rounds').appendChild(box);

  // Rekap
  // Kolom dibuat ADITIF: main + wasit + ballboy + istirahat = jumlah ronde.
  // Sebelumnya "Duduk" menghitung semua ronde tidak main termasuk ronde saat
  // orangnya bertugas, jadi angkanya tidak bisa dijumlah dan menyesatkan -
  // seseorang yang 3 kali jadi wasit tetap tercatat "duduk" 3 kali itu.
  const showRoles = schedule.config.referees_per_court || schedule.config.ballboys_per_court;
  let html = '<table class="data"><thead><tr><th>Nama</th>'
    + '<th class="num">Main</th>'
    + (showRoles ? '<th class="num">Wasit</th><th class="num">Ballboy</th>' : '')
    + '<th class="num">Istirahat</th></tr></thead><tbody>';
  schedule.players.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach((p) => {
    const roles = st.roles_per_player[p.id] || {};
    const idle = (st.byes_per_player[p.id] || 0) - (roles.total || 0);
    html += `<tr><td>${esc(p.name)}</td>`
      + `<td class="num">${st.plays_per_player[p.id] || 0}</td>`
      + (showRoles ? `<td class="num">${roles.wasit || 0}</td>`
                     + `<td class="num">${roles.ballboy || 0}</td>` : '')
      + `<td class="num">${Math.max(0, idle)}</td></tr>`;
  });
  $('recap').innerHTML = html + '</tbody></table>';

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
}

function nameOf(id) {
  const p = schedule.players.find((x) => x.id === id);
  return p ? p.name : '?';
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
  input.value = JSON.stringify(buildPayload());
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
  form.remove();
};

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------
$('save-event').onclick = async () => {
  if (!schedule) return toast('Belum ada jadwal');
  try {
    const d = await api('/api/events/save', { ...buildPayload(), event_id: currentEventId });
    currentEventId = d.id;
    $('save-msg').innerHTML = `<div class="msg ok">Tersimpan (#${d.id}).</div>`;
    toast('Tersimpan ke database');
  } catch (e) {
    $('save-msg').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
};

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
  $('tier-row').style.display = req.mode === 'tiered' ? '' : 'none';
  $('segments').innerHTML = '';
  $('interleave').checked = !!req.interleave_segments;
  (req.segments || []).forEach((s) => addSeg(s.label, s.rounds, s.rule));
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

async function renderPlayers_() {
  const d = await loadPaged('players');
  $('players-table').innerHTML = d.items.length
    ? '<table class="data"><thead><tr><th>Nama</th><th>Panggilan</th>'
      + '<th class="num">Rating</th><th class="num">L/P</th><th>Level</th>'
      + '<th></th></tr></thead><tbody>' +
      d.items.map((p) =>
        `<tr><td>${esc(p.name)}</td><td>${esc(p.nickname || '-')}</td>` +
        `<td class="num">${p.rating}</td>` +
        `<td class="num">${p.gender === 'M' ? 'L' : p.gender === 'F' ? 'P' : '-'}</td>` +
        `<td>${esc(p.level_label || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-pl="${p.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-pl="${p.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : `<div class="empty">${pager.players.search ? 'Tidak ada yang cocok.' : 'Belum ada pemain. Tambahkan lewat form di atas, atau dari tab Setup klik "Simpan ke master pemain".'}</div>`;
  renderPager($('players-pager'), d, (p) => { pager.players.page = p; renderPlayers_(); });
}

async function renderClubs_() {
  const d = await loadPaged('clubs');
  $('clubs-table').innerHTML = d.items.length
    ? '<table class="data"><thead><tr><th>Klub</th><th>Kota</th><th>Kontak</th><th></th></tr></thead><tbody>' +
      d.items.map((c) =>
        `<tr><td>${c.logo ? `<img class="logo-mini" src="${esc(c.logo)}" alt="">` : ''}${esc(c.name)}</td>` +
        `<td>${esc(c.city || '-')}</td>` +
        `<td>${esc(c.contact || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-cl="${c.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-cl="${c.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : '<div class="empty">Belum ada klub.</div>';
  renderPager($('clubs-pager'), d, (p) => { pager.clubs.page = p; renderClubs_(); });
}

async function renderVenues_() {
  const d = await loadPaged('venues');
  $('venues-table').innerHTML = d.items.length
    ? '<table class="data"><thead><tr><th>Nama</th><th class="num">Court</th>'
      + '<th class="num">Harga/jam</th><th>Alamat</th><th></th></tr></thead><tbody>' +
      d.items.map((v) =>
        `<tr><td>${esc(v.name)}</td><td class="num">${v.court_count}</td>` +
        `<td class="num">${rp(v.price_per_hour)}</td><td>${esc(v.address || '-')}</td>` +
        `<td><button class="btn ghost sm" data-ed-vn="${v.id}">Ubah</button> ` +
        `<button class="btn ghost sm" data-del-vn="${v.id}">Hapus</button></td></tr>`
      ).join('') + '</tbody></table>'
    : '<div class="empty">Belum ada venue. Isi harga sewa di sini supaya panel Biaya terisi otomatis.</div>';
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
$('calc-econ').onclick = async () => {
  if (players.length < 4) return toast('Butuh minimal 4 peserta');
  try {
    const d = await api('/api/economics', buildPayload());
    const c = d.current;

    const tile = statTileHTML;

    $('econ-now').innerHTML = '<div class="stat-grid">' +
      tile('Biaya total', rp(c.total_cost), `${c.courts} court x ${c.hours} jam`) +
      tile('Pemasukan', rp(c.revenue), `${c.n_players} x fee`) +
      tile('Untung', rp(c.profit), `margin ${c.margin_pct}%`, c.profit >= 0 ? 'good' : 'bad') +
      tile('Modal / peserta', rp(c.cost_per_player), 'titik impas') +
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

    let fs = '<div style="font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Fee untuk target margin</div><div class="stat-grid">';
    Object.entries(d.fee_suggestions).forEach(([m, f]) => {
      fs += tile(`Margin ${m}%`, rp(f), 'per peserta');
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
};

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
  } catch (e) { /* biarkan default */ }

  setupCombos();
  setupParticipantPicker();
  try { await loadMaster(); } catch (e) { /* database belum siap, abaikan */ }
  renderPlayers();
})();
